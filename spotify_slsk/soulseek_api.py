import os
import time
import logging
import re

import slskd_api

from .db import (
    get_tried_files,
    add_tried_file,
    add_blocklist,
    load_blocklist,
    record_history,
)
from . import config, runtime_state
from .matching.candidates import (
    evaluate_candidates,
    sort_candidates,
    summarize_evaluation,
)
from .slskd.models import remote_basename

_client = None
_shutdown_event = None
# The candidate dict behind the most recent successful download_and_verify,
# so the caller can attribute a post-download rejection to the peer file
# (blocklist) and record where the file came from (history). Daemon-thread
# only.
last_download = None


def set_shutdown_event(event) -> None:
    """Wire the caller's shutdown Event so our polling loops can exit promptly."""
    global _shutdown_event
    _shutdown_event = event


def _interruptible_sleep(seconds: float) -> bool:
    """Return True if shutdown was requested during the sleep, False on timeout."""
    if _shutdown_event is None:
        time.sleep(seconds)
        return False
    return _shutdown_event.wait(seconds)


def new_client():
    """Build a fresh SlskdClient from env."""
    return slskd_api.SlskdClient(
        host=os.getenv("SLSKD_HOST_URL", "http://slskd:5030"),
        api_key=os.getenv("SLSKD_API_KEY"),
        url_base=os.getenv("SLSKD_URL_BASE", ""),
    )


def get_client():
    """Lazily build and cache the daemon's SlskdClient. Reads env at first call."""
    global _client
    if _client is None:
        _client = new_client()
    return _client


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
    client = client or get_client()
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
        search_id = search.get("id")
        if not search_id:
            return []

        start = time.time()
        while time.time() - start < timeout:
            state = client.searches.state(search_id).get("state", "")
            if state != "InProgress":
                results = client.searches.search_responses(search_id)
                logging.info(f"Search returned {len(results)} results for: {query}")
                return results or []
            if _interruptible_sleep(1):
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


def perform_search(artist, title, album=None, timeout=60):
    """
    Run a waterfall of slskd searches for one track. Returns the first query's
    responses that yields at least one result, else an empty list.

    Kept for backward compatibility with tests and external callers; the daemon
    itself now uses `search_and_download`, which falls through on empty *usable*
    candidates rather than just empty *responses*.
    """
    queries = _build_search_queries(artist, title, album)
    for query in queries:
        results = _run_one_search(query, timeout)
        if results:
            return results
    return []


def interactive_search(
    artist, title, album=None, duration_ms=None, is_blocked=None, timeout=60
):
    """Read-only search for the track page: walk the whole query waterfall,
    evaluate every file, and return them all — accepted first in auto-pick
    order, then rejected — de-duplicated by (user, filename).

    Runs on a web-UI thread, so it uses its own slskd client rather than
    sharing the daemon's requests session, and never touches the DB.
    """
    client = new_client()
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


# slskd can report "Completed, Succeeded" while the file still sits in its
# incomplete directory; the move to the final location lands moments later.
COMPLETION_GRACE_SECONDS = 30


def find_file_in_downloads(filename, base_dir=None):
    """Path of a *finished* download named `filename`, or None. slskd's
    incomplete directory is never searched: a hit there is still being
    written."""
    if base_dir is None:
        base_dir = config.DOWNLOAD_DIR
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d != "incomplete"]
        if filename in files:
            return os.path.join(root, filename)
    return None


def _await_completed_file(filename, grace=None):
    """Poll for a finished download for up to `grace` seconds (1 s steps,
    interruptible by shutdown). Returns the path or None."""
    deadline = time.time() + (COMPLETION_GRACE_SECONDS if grace is None else grace)
    while True:
        path = find_file_in_downloads(filename)
        if path or time.time() >= deadline:
            return path
        if _interruptible_sleep(1):
            return None


def download_and_verify(
    search_results,
    expected_title,
    expected_artist,
    conn,
    playlist_name,
    track_id,
    max_version_tier=None,
    evaluated=None,
    expected_duration_ms=None,
):
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

        file_path = grab_candidate(candidate, conn, playlist_name, track_id)
        if file_path:
            return file_path

    logging.warning("Exhausted all download attempts.")
    return None


def _shutting_down() -> bool:
    return _shutdown_event is not None and _shutdown_event.is_set()


def _candidate_history(candidate) -> dict:
    return {
        "username": candidate.get("user"),
        "filename": candidate.get("filename"),
        "format": (candidate.get("ext") or "").lstrip(".") or None,
        "bitrate": candidate.get("bitrate"),
        "size": candidate.get("size"),
    }


def grab_candidate(candidate, conn, playlist_name, track_id, source="auto"):
    """Enqueue one candidate, wait for it, and return the local path or None.

    Any failure is remembered in the track's `_tried` table and history.
    Only a definitive peer-file fault — slskd reporting the transfer
    failed/errored/rejected/aborted (`candidate["peer_fault"]`, set by
    wait_for_completion) — also blocklists the file for every track. Our
    own wait timeout, a "Queued, Remotely" stall, slskd not listing the
    transfer yet, a file we could not find afterwards, or a shutdown say
    nothing certain about the peer, and a global ban would outlive the
    track's retry. On success the candidate is stashed in `last_download`.
    `source` ("auto"/"manual") is recorded in history.
    """
    global last_download
    candidate.pop("failure", None)
    candidate.pop("peer_fault", None)
    basename = remote_basename(candidate["filename"])
    logging.info(
        f"Attempting download: {candidate['filename']} from {candidate['user']}"
    )
    try:
        get_client().transfers.enqueue(
            username=candidate["user"],
            files=[
                {
                    "filename": candidate["filename"],
                    "size": candidate["size"],
                }
            ],
        )

        file_path = wait_for_completion(candidate)
        if file_path:
            logging.info(f"Downloaded and verified: {file_path}")
            if _wait_for_external_processing(file_path):
                last_download = candidate
                record_history(
                    conn, "grabbed", table_name=playlist_name, track_id=track_id,
                    source=source, **_candidate_history(candidate),
                )
                return file_path
            logging.warning(f"Post-download verification failed for: {basename}")
            failure = "file did not appear after download"
        else:
            logging.warning(f"Download failed or was not confirmed: {basename}")
            failure = candidate.get("failure") or "transfer failed"
        add_tried_file(conn, playlist_name, track_id, basename)
        # A shutdown mid-transfer says nothing about the peer.
        if not _shutting_down():
            record_history(
                conn, "download_failed", table_name=playlist_name,
                track_id=track_id, reason=failure, source=source,
                **_candidate_history(candidate),
            )
            if candidate.get("peer_fault"):
                add_blocklist(
                    conn,
                    candidate["user"],
                    candidate["filename"],
                    candidate.get("size"),
                    failure,
                    track_id,
                )
    except Exception as e:
        # Don't blacklist on this path: an exception here is usually a
        # transient slskd-API hiccup (JSON decode against an empty body,
        # connection reset, etc.), not a "this peer rejected the file"
        # signal. add_tried_file is permanent until the track succeeds,
        # so a single bad response would permanently ban a perfectly
        # good candidate. Let the next cycle retry naturally — bounded
        # by MAX_ATTEMPTS_BEFORE_SUSPEND on the track itself.
        logging.error(
            f"Error downloading {basename} from {candidate['user']}: {e}"
        )
    return None


def search_and_download(
    artist,
    title,
    conn,
    playlist_name,
    track_id,
    album=None,
    max_version_tier=None,
    timeout=60,
    duration_ms=None,
):
    """Run the full query waterfall, attempting download from each.

    Falls through to the next query when a query yields no *usable* candidates
    (after format/quality/version filtering), not just zero raw responses. This
    fixes the case where slskd returns N junk responses for a long
    artist+title query but a shorter query would have surfaced good matches.
    """
    global last_download
    last_download = None
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


def _transfer_outcome(state):
    """Classify a slskd transfer state: "succeeded", "failed", or None while
    it is still queued/in progress.

    slskd's terminal states are all "Completed, <reason>". Only Succeeded is
    a success; Rejected, TimedOut, Errored, Aborted, Cancelled... are
    failures. Matching only failed/aborted/errored let "Completed, Rejected"
    and "Completed, TimedOut" run into the 300 s timeout.
    """
    s = (state or "").lower()
    if "completed" in s:
        return "succeeded" if "succeeded" in s else "failed"
    if any(w in s for w in ("failed", "aborted", "errored", "rejected", "timedout")):
        return "failed"
    return None


# Terminal slskd states that are the peer's (or its file's) fault. Other
# failures — "Completed, TimedOut", "Completed, Cancelled" — may be ours or
# the network's, so they never blocklist.
_PEER_FAULT_STATES = ("failed", "errored", "rejected", "aborted")


def wait_for_completion(candidate, timeout=300):
    client = get_client()
    logging.debug(f"Waiting for transfer of {candidate['filename']} to complete...")
    transfer_id = None
    start = time.time()

    while time.time() - start < 10:
        downloads = client.transfers.get_downloads(candidate["user"])
        for directory in downloads.get("directories", []):
            for file in directory.get("files", []):
                if (
                    remote_basename(file["filename"])
                    == remote_basename(candidate["filename"])
                    and file["size"] == candidate["size"]
                ):
                    transfer_id = file["id"]
                    break
            if transfer_id:
                break
        if transfer_id:
            break
        if _interruptible_sleep(1):
            return None

    if not transfer_id:
        logging.error(f"Transfer ID not found for {candidate['filename']}")
        candidate["failure"] = "transfer not found in slskd"
        return None

    start = time.time()
    while True:
        downloads = client.transfers.get_downloads(candidate["user"])
        for directory in downloads.get("directories", []):
            for file in directory.get("files", []):
                if file["id"] == transfer_id:
                    state = file.get("state", "").lower()
                    logging.debug(f"State for {file['filename']}: {state}")
                    outcome = _transfer_outcome(state)
                    if outcome == "succeeded":
                        filename = remote_basename(candidate["filename"])
                        real_path = _await_completed_file(filename)
                        logging.info(f"File found: {real_path}")
                        if real_path and _wait_for_external_processing(real_path):
                            return real_path
                        logging.warning(
                            f"File not confirmed after download: {filename}"
                        )
                        candidate["failure"] = "file not found after download"
                        return None
                    if outcome == "failed":
                        logging.warning(
                            f"Transfer failed: {file['filename']} — state: {state}"
                        )
                        candidate["failure"] = f"transfer {state}"
                        candidate["peer_fault"] = any(
                            w in state for w in _PEER_FAULT_STATES
                        )
                        return None
        if time.time() - start > timeout:
            logging.warning(f"Transfer timeout for {candidate['filename']}")
            candidate["failure"] = "transfer timed out"
            return None
        if _interruptible_sleep(2):
            return None


def _wait_for_external_processing(file_path):
    if "/incomplete/" in file_path:
        logging.warning(f"Rejected incomplete path: {file_path}")
        return False

    start = time.time()
    while time.time() - start < config.EXTERNAL_PROCESS_WAIT_TIMEOUT:
        if os.path.exists(file_path):
            logging.info(f"File confirmed at: {file_path}")
            return True
        if _interruptible_sleep(2):
            return False

    logging.warning(f"File did not appear within timeout: {file_path}")
    return False
