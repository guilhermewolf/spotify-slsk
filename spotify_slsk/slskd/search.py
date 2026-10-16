"""slskd searches: the query waterfall, one search run, and search+grab."""
from __future__ import annotations

import logging
import re
import sqlite3
import time

from .. import config, runtime_state
from ..db import get_tried_files, load_blocklist, record_history
from ..matching.candidates import (
    evaluate_candidates,
    sort_candidates,
    summarize_evaluation,
)
from ..models import Candidate, IsBlocked
from . import client as slskd_client
from . import transfers
from .models import Search, SearchResponse, remote_basename


def _is_ascii(s: str) -> bool:
    return s.isascii() if isinstance(s, str) else True


def _normalize_query_text(text: str) -> str:
    """Lowercase the text, replace separators with spaces, drop other
    punctuation, collapse runs of whitespace.

    Hyphens, underscores, and slashes need to become *spaces* (not be
    deleted) so titles like "Mind Twister - Radio-Edit" produce
    "mind twister radio edit" — slskd peers tag those files as
    "(Radio Edit)" and nothing matches "radioedit".
    """
    if not text:
        return ""
    text = re.sub(r"[-_/]+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.lower().split())


def _build_search_queries(artist: str, title: str, album: str | None = None) -> list:
    """
    Return a waterfall of slskd queries, most- to least-specific. Non-ASCII
    (CJK, etc.) titles bypass punctuation stripping because it mangles them.
    """
    queries = []
    seen = set()

    def add(q: str):
        q = (q or "").strip()
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            queries.append(q)

    raw_title = (title or "").strip()
    raw_artist = (artist or "").strip()
    raw_album = (album or "").strip()

    if not _is_ascii(raw_title + raw_artist):
        add(f"{raw_title} {raw_artist}")
        add(raw_title)

    clean_title = _normalize_query_text(raw_title)
    clean_artist = _normalize_query_text(raw_artist)
    clean_album = _normalize_query_text(raw_album)

    if clean_title and clean_artist:
        add(f"{clean_title} {clean_artist}")

    if clean_album and clean_title:
        album_tokens = set(clean_album.split())
        title_wo_album = " ".join(
            t for t in clean_title.split() if t not in album_tokens
        )
        if title_wo_album and title_wo_album != clean_title and clean_artist:
            add(f"{title_wo_album} {clean_artist}")

    if clean_title:
        add(clean_title)

    if not queries and raw_title:
        add(_normalize_query_text(raw_title) or raw_title)

    return queries


def _run_one_search(query: str, timeout: int = 60, client=None) -> list:
    """Run a single slskd search to completion and return its responses.

    Returns [] on timeout, error, or shutdown. Always cleans up the slskd-side
    search record in the finally block so we don't leak ids on the daemon.
    `client` defaults to the daemon's shared client.
    """
    client = client or slskd_client.get_client()
    logging.info(f"Searching for: {query}")
    search_id = None
    try:
        # `responseLimit` caps the search at N responses on the slskd side —
        # slskd then marks the search Completed and the polling loop picks it
        # up. Slskd does not expose in-flight responses, so server-side
        # capping is the only sane way to cut per-search latency.
        search = client.searches.search_text(
            searchText=query,
            minimumPeerUploadSpeed=config.MIN_PEER_UPLOAD_SPEED,
            responseLimit=config.EARLY_STOP_RESPONSES,
        )
        search_id = Search.from_json(search).id
        if not search_id:
            return []

        start = time.time()
        while time.time() - start < timeout:
            if not Search.from_json(client.searches.state(search_id)).in_progress:
                raw = client.searches.search_responses(search_id)
                results = [
                    SearchResponse.from_json(r)
                    for r in (raw if isinstance(raw, list) else [])
                ]
                logging.info(f"Search returned {len(results)} results for: {query}")
                return results
            if slskd_client.interruptible_sleep(1):
                return []
        logging.warning(f"Search timed out for: {query}")
        return []
    except Exception as e:
        logging.error(f"Search failed for '{query}': {e}")
        return []
    finally:
        if search_id:
            try:
                client.searches.delete(search_id)
            except Exception:
                logging.debug(
                    f"Could not delete slskd search {search_id}",
                    exc_info=True,
                )


def interactive_search(
    artist: str,
    title: str,
    album: str | None = None,
    duration_ms: int | None = None,
    is_blocked: IsBlocked | None = None,
    timeout: int = 60,
) -> list[Candidate]:
    """Read-only search for the track page: walk the whole query waterfall,
    evaluate every file, and return them all — accepted first in auto-pick
    order, then rejected — de-duplicated by (user, filename).

    Runs on a web-UI thread, so it uses its own slskd client rather than
    sharing the daemon's requests session, and never touches the DB.
    """
    client = slskd_client.new_client()
    seen = set()
    evaluated = []
    for query in _build_search_queries(artist, title, album):
        results = _run_one_search(query, timeout, client=client)
        for c in evaluate_candidates(
            results or [],
            title,
            artist,
            expected_duration_ms=duration_ms,
            is_blocked=is_blocked,
        ):
            key = (c["user"], c["filename"])
            if key not in seen:
                seen.add(key)
                evaluated.append(c)
    accepted = sort_candidates([c for c in evaluated if c["accepted"]])
    return accepted + [c for c in evaluated if not c["accepted"]]


def search_and_download(
    artist: str,
    title: str,
    conn: sqlite3.Connection,
    playlist_name: str,
    track_id: str,
    album: str | None = None,
    max_version_tier: int | None = None,
    timeout: int = 60,
    duration_ms: int | None = None,
    lossless_only: bool = False,
    better_quality_than: str | None = None,
    current_quality: str | None = None,
) -> str | None:
    """Run the full query waterfall, attempting download from each.

    Falls through to the next query when a query yields no *usable* candidates
    (after format/quality/version filtering), not just zero raw responses. This
    fixes the case where slskd returns N junk responses for a long
    artist+title query but a shorter query would have surfaced good matches.
    """
    transfers.last_download = None
    queries = _build_search_queries(artist, title, album)
    if not queries:
        return None
    is_blocked = load_blocklist(conn) if conn is not None else None
    results_total = accepted_total = 0
    for query in queries:
        results = _run_one_search(query, timeout)
        evaluated = (
            evaluate_candidates(
                results,
                title,
                artist,
                max_version_tier=max_version_tier,
                expected_duration_ms=duration_ms,
                is_blocked=is_blocked,
                lossless_only=lossless_only,
                better_quality_than=better_quality_than,
                current_quality=current_quality,
            )
            if results
            else []
        )
        # Record every attempt — even empty ones — so the inspector shows
        # the full waterfall the daemon actually walked, and why each
        # file was turned down.
        runtime_state.record_search(
            track_id,
            query,
            result_count=len(results) if results else 0,
            **summarize_evaluation(evaluated),
        )
        results_total += len(results) if results else 0
        accepted_total += sum(1 for c in evaluated if c["accepted"])
        if not results:
            continue
        file_path = download_and_verify(
            search_results=results,
            expected_title=title,
            expected_artist=artist,
            conn=conn,
            playlist_name=playlist_name,
            track_id=track_id,
            max_version_tier=max_version_tier,
            evaluated=evaluated,
        )
        if file_path:
            return file_path
    # Failed grabs record their own events; this covers "nothing usable".
    # The upgrade pass (max_version_tier) finding nothing is not a failure.
    if accepted_total == 0 and max_version_tier is None:
        record_history(
            conn, "download_failed", table_name=playlist_name, track_id=track_id,
            reason=f"no acceptable candidate ({results_total} results)",
        )
    return None


def download_and_verify(
    search_results: list[SearchResponse],
    expected_title: str,
    expected_artist: str,
    conn: sqlite3.Connection,
    playlist_name: str,
    track_id: str,
    max_version_tier: int | None = None,
    evaluated: list[Candidate] | None = None,
    expected_duration_ms: int | None = None,
) -> str | None:
    """Filter, sort, and try to download the best candidate from one search.

    `max_version_tier`, when set, restricts candidates to *strictly better*
    version tiers (used by the upgrade pass — Original Mix already on disk
    means we'll only consider tier 0 = Extended Mix).

    `evaluated` lets a caller that already ran `evaluate_candidates` (to
    record rejection reasons) pass the result instead of recomputing it.
    """
    if evaluated is None:
        evaluated = evaluate_candidates(
            search_results,
            expected_title,
            expected_artist,
            max_version_tier=max_version_tier,
            expected_duration_ms=expected_duration_ms,
            is_blocked=load_blocklist(conn) if conn is not None else None,
        )
    candidates = [c for c in evaluated if c["accepted"]]
    if not candidates:
        logging.warning("No valid candidates found.")
        return None

    sorted_candidates = sort_candidates(candidates)
    tried_filenames = set(get_tried_files(conn, playlist_name, track_id))

    for candidate in sorted_candidates:
        basename = remote_basename(candidate["filename"])

        if basename in tried_filenames:
            logging.info(f"Skipping previously tried file: {basename}")
            continue

        file_path = transfers.grab_candidate(candidate, conn, playlist_name, track_id)
        if file_path:
            return file_path

    logging.warning("Exhausted all download attempts.")
    return None
