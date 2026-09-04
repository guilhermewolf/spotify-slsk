import os
import time
import logging
import re

import slskd_api
from rapidfuzz import fuzz

from .db import get_tried_files, add_tried_file, get_setting

DEFAULT_FORMATS = "flac,mp3,aiff,wav"


def _normalize_ext_list(env_val: str):
    """
    Return a normalized, ordered list of extensions like ['.flac', '.mp3', ...],
    accepting inputs with or without leading dots and removing duplicates.
    """
    items = []
    seen = set()
    for raw in env_val.split(","):
        fmt = raw.strip().strip('"').strip("'").lower()
        if not fmt:
            continue
        if not fmt.startswith("."):
            fmt = "." + fmt
        if fmt not in seen:
            seen.add(fmt)
            items.append(fmt)
    return items


PREFERRED_FORMATS = _normalize_ext_list(os.getenv("SLSKD_PREFERRED_FORMATS", DEFAULT_FORMATS))
DOWNLOAD_DIR = os.getenv("SLSKD_DOWNLOADS_DIR", "/downloads")
EXTERNAL_PROCESS_WAIT_TIMEOUT = int(os.getenv("SLSKD_WAIT_TIMEOUT", "60"))
MIN_PEER_UPLOAD_SPEED = int(os.getenv("SLSKD_MIN_PEER_UPLOAD_SPEED", "0"))
# MP3s whose effective bitrate (size*8/duration/1000) falls below this are
# rejected even if their reported bitrate claims 320 — catches upsampled fakes.
MIN_EFFECTIVE_MP3_KBPS = int(os.getenv("SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280"))
# Cut the slskd search short once we have this many responses. slskd will
# keep collecting responses for the full searchTimeout window otherwise.
EARLY_STOP_RESPONSES = int(os.getenv("SLSKD_EARLY_STOP_RESPONSES", "20"))

_client = None
_shutdown_event = None


def refresh_from_db(conn) -> None:
    """Reload UI-editable tunables from the DB. Called at each cycle start.

    Infrastructure values (SLSKD_HOST_URL, DOWNLOAD_DIR, SLSKD_URL_BASE)
    stay env-only because they're deploy-time concerns.
    """
    global PREFERRED_FORMATS, MIN_PEER_UPLOAD_SPEED, MIN_EFFECTIVE_MP3_KBPS
    global EARLY_STOP_RESPONSES, EXTERNAL_PROCESS_WAIT_TIMEOUT

    PREFERRED_FORMATS = _normalize_ext_list(
        get_setting(conn, "SLSKD_PREFERRED_FORMATS", DEFAULT_FORMATS)
    )
    try:
        MIN_PEER_UPLOAD_SPEED = int(
            get_setting(conn, "SLSKD_MIN_PEER_UPLOAD_SPEED", "0")
        )
        MIN_EFFECTIVE_MP3_KBPS = int(
            get_setting(conn, "SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280")
        )
        EARLY_STOP_RESPONSES = int(
            get_setting(conn, "SLSKD_EARLY_STOP_RESPONSES", "20")
        )
        EXTERNAL_PROCESS_WAIT_TIMEOUT = int(
            get_setting(conn, "SLSKD_WAIT_TIMEOUT", "60")
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad numeric setting during refresh; using previous values: {e}")


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


def get_client():
    """Lazily build and cache the SlskdClient. Reads env at first call."""
    global _client
    if _client is None:
        _client = slskd_api.SlskdClient(
            host=os.getenv("SLSKD_HOST_URL", "http://slskd:5030"),
            api_key=os.getenv("SLSKD_API_KEY"),
            url_base=os.getenv("SLSKD_URL_BASE", ""),
        )
    return _client


def _is_ascii(s: str) -> bool:
    return s.isascii() if isinstance(s, str) else True


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

    clean_title = re.sub(r"[^\w\s]", "", raw_title).lower().strip()
    clean_artist = re.sub(r"[^\w\s]", "", raw_artist).lower().strip()
    clean_album = re.sub(r"[^\w\s]", "", raw_album).lower().strip()

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
        add(raw_title)

    return queries


def _run_one_search(query: str, timeout: int = 60) -> list:
    """Run a single slskd search to completion and return its responses.

    Returns [] on timeout, error, or shutdown. Always cleans up the slskd-side
    search record in the finally block so we don't leak ids on the daemon.
    """
    client = get_client()
    logging.info(f"Searching for: {query}")
    search_id = None
    try:
        # `responseLimit` caps the search at N responses on the slskd side —
        # slskd then marks the search Completed and the polling loop picks it
        # up. Slskd does not expose in-flight responses, so server-side
        # capping is the only sane way to cut per-search latency.
        search = client.searches.search_text(
            searchText=query,
            minimumPeerUploadSpeed=MIN_PEER_UPLOAD_SPEED,
            responseLimit=EARLY_STOP_RESPONSES,
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


def clean_filename(filename):
    """
    Clean a filename for fuzzy matching: replace brackets/parens with spaces
    (keep their content), normalize separators, drop the extension and
    obvious tag noise.

    We *don't* strip parenthesized content because real filenames embed
    version info there ("(Walker & Royce Remix)", "(Extended Mix)") that the
    title scorer needs to see. token_set_ratio is robust to extra tokens
    like a leftover "[FLAC]", so leaving them in costs us nothing.
    """
    filename = re.sub(r"[\[\]\(\)\{\}]", " ", filename)
    filename = re.sub(
        r"\b\d{1,2}bit\b|\b\d{1,3}\.\d{1,2}kHz\b|\b\d{4}\b",
        "",
        filename,
        flags=re.IGNORECASE,
    )
    filename = os.path.splitext(filename)[0]
    filename = filename.replace("_", " ").replace("-", " ")
    filename = " ".join(filename.split())
    return filename.lower().strip()


def _infer_bitrate_from_name(name: str):
    """
    Try to infer bitrate from the filename text. Returns kbps or None.
    """
    text = name.lower()
    m = re.search(
        r"(?<!\d)(320|256|224|192|160|128)\s*(k|kbps)?(?!\d)",
        text,
    )
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


_VERSION_MARKERS = (
    "remix", "remake", "rework", "bootleg", "mashup",
    "live", "acoustic", "unplugged", "instrumental", "karaoke",
    "radio edit", "extended edit", "club edit",
)


def _has_version_marker(text: str):
    t = (text or "").lower()
    for marker in _VERSION_MARKERS:
        if re.search(rf"\b{re.escape(marker)}\b", t):
            return marker
    return None


def _version_mismatch(expected_title: str, file_title: str) -> bool:
    """True when exactly one side declares a version marker, or both declare different ones."""
    e = _has_version_marker(expected_title)
    f = _has_version_marker(file_title)
    if e is None and f is None:
        return False
    return e != f


# DJ-oriented version preference. Lower = more preferred. Extended Mix is the
# DJ-friendly long cut; Original Mix is the producer's studio version; everything
# else (radio edits, plain titles) is the fallback. Note: this is intentionally
# orthogonal to `_VERSION_MARKERS` / `_version_mismatch` — those reject *wrong*
# versions (e.g. Live when expecting Studio); this only ranks compatible ones.
def _version_tier(text: str) -> int:
    t = (text or "").lower()
    if re.search(r"\bextended\s+(mix|version|edit)\b", t):
        return 0
    if re.search(r"\boriginal\s+(mix|version)\b", t):
        return 1
    return 2


def _effective_mp3_kbps(size_bytes, length_sec):
    if not size_bytes or not length_sec or length_sec <= 0:
        return None
    try:
        return int((size_bytes * 8) / length_sec / 1000)
    except Exception:
        return None


def extract_candidates(
    search_results,
    expected_title,
    expected_artist,
    min_title_score=80,
    min_artist_score=70,
):
    """
    Extract valid file candidates from slskd search results based on title and
    artist matching. Unknown MP3 bitrates are allowed (slskd often reports None);
    files are hard-rejected only when we *know* quality is too low.
    """
    candidates = []
    expected_title_norm = " ".join(expected_title.lower().replace("-", " ").split())
    expected_artists = [a.strip().lower() for a in expected_artist.split(",")]

    for result in search_results:
        user = result.get("username", "unknown")
        user_upload_speed = result.get("uploadSpeed", 0) or 0
        files = result.get("files", [])

        for file in files:
            filename = file.get("filename")
            if not filename:
                continue

            ext = os.path.splitext(filename)[1].lower()
            if ext not in PREFERRED_FORMATS:
                continue

            reported_bitrate = file.get("bitrate")
            length_sec = file.get("length")
            size_bytes = file.get("size")
            inferred_bitrate = _infer_bitrate_from_name(os.path.basename(filename))
            effective_bitrate = (
                reported_bitrate if reported_bitrate is not None else inferred_bitrate
            )
            effective_mp3 = (
                _effective_mp3_kbps(size_bytes, length_sec) if ext == ".mp3" else None
            )

            # Reject MP3s whose reported/inferred bitrate is known-low.
            if ext == ".mp3" and effective_bitrate is not None and effective_bitrate < 320:
                logging.debug(
                    f"Skipped {filename}: reported bitrate {effective_bitrate} < 320"
                )
                continue

            # Reject MP3s whose size/duration shows they're effectively below the
            # configured floor — catches 128 kbps files retagged as 320.
            if (
                ext == ".mp3"
                and effective_mp3 is not None
                and effective_mp3 < MIN_EFFECTIVE_MP3_KBPS
            ):
                logging.debug(
                    f"Skipped {filename}: effective bitrate {effective_mp3} < {MIN_EFFECTIVE_MP3_KBPS}"
                )
                continue

            base = os.path.basename(filename)
            clean_base = clean_filename(base)

            title_score = fuzz.token_set_ratio(expected_title_norm, clean_base)
            artist_scores = [
                fuzz.token_set_ratio(a, clean_base) for a in expected_artists
            ]
            max_artist_score = max(artist_scores) if artist_scores else 0

            # Version-gate penalty: if expected track doesn't mention a version
            # (remix/live/...) but the file does (or vice versa), or they mention
            # different versions, penalize the title score.
            if _version_mismatch(expected_title, base):
                title_score = max(0, title_score - 25)

            if title_score >= min_title_score and max_artist_score >= min_artist_score:
                candidates.append(
                    {
                        "user": user,
                        "user_upload_speed": user_upload_speed,
                        "filename": base,
                        "size": size_bytes,
                        "bitrate": effective_bitrate,
                        "effective_mp3": effective_mp3,
                        "ext": ext,
                        "title_score": title_score,
                        "artist_score": max_artist_score,
                        "version_tier": _version_tier(base),
                    }
                )

    return candidates


def sort_candidates(candidates):
    """
    Sort by DJ-version tier (Extended Mix > Original Mix > other), then by
    preferred extension, then bitrate desc (unknown last), then peer upload
    speed desc.
    """
    def fmt_rank(ext: str) -> int:
        return (
            PREFERRED_FORMATS.index(ext)
            if ext in PREFERRED_FORMATS
            else len(PREFERRED_FORMATS)
        )

    def bitrate_rank(bps_k) -> int:
        return bps_k or 0

    return sorted(
        candidates,
        key=lambda c: (
            c.get("version_tier", 2),
            fmt_rank(c["ext"]),
            -bitrate_rank(c.get("bitrate")),
            -(c.get("user_upload_speed") or 0),
        ),
    )


def find_file_in_downloads(filename, base_dir=None):
    if base_dir is None:
        base_dir = DOWNLOAD_DIR
    for root, _, files in os.walk(base_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None


def download_and_verify(
    search_results,
    expected_title,
    expected_artist,
    conn,
    playlist_name,
    track_id,
    max_version_tier=None,
):
    """Filter, sort, and try to download the best candidate from one search.

    `max_version_tier`, when set, restricts candidates to *strictly better*
    version tiers (used by the upgrade pass — Original Mix already on disk
    means we'll only consider tier 0 = Extended Mix).
    """
    client = get_client()
    candidates = extract_candidates(search_results, expected_title, expected_artist)
    if max_version_tier is not None:
        candidates = [
            c for c in candidates if c.get("version_tier", 2) < max_version_tier
        ]
    if not candidates:
        logging.warning("No valid candidates found.")
        return None

    sorted_candidates = sort_candidates(candidates)
    tried_filenames = set(get_tried_files(conn, playlist_name, track_id))

    for candidate in sorted_candidates:
        basename = os.path.basename(candidate["filename"])

        if basename in tried_filenames:
            logging.info(f"Skipping previously tried file: {basename}")
            continue

        logging.info(
            f"Attempting download: {candidate['filename']} from {candidate['user']}"
        )
        try:
            client.transfers.enqueue(
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
                if not _wait_for_external_processing(file_path):
                    logging.warning(
                        f"Post-download verification failed for: {basename}"
                    )
                    add_tried_file(conn, playlist_name, track_id, basename)
                    continue
                return file_path
            logging.warning(f"Download failed or was not confirmed: {basename}")
            add_tried_file(conn, playlist_name, track_id, basename)
        except Exception as e:
            logging.error(
                f"Error downloading {basename} from {candidate['user']}: {e}"
            )
            add_tried_file(conn, playlist_name, track_id, basename)

    logging.warning("Exhausted all download attempts.")
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
):
    """Run the full query waterfall, attempting download from each.

    Falls through to the next query when a query yields no *usable* candidates
    (after format/quality/version filtering), not just zero raw responses. This
    fixes the case where slskd returns N junk responses for a long
    artist+title query but a shorter query would have surfaced good matches.
    """
    queries = _build_search_queries(artist, title, album)
    if not queries:
        return None
    for query in queries:
        results = _run_one_search(query, timeout)
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
        )
        if file_path:
            return file_path
    return None


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
                    os.path.basename(file["filename"])
                    == os.path.basename(candidate["filename"])
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
        return None

    start = time.time()
    while True:
        downloads = client.transfers.get_downloads(candidate["user"])
        for directory in downloads.get("directories", []):
            for file in directory.get("files", []):
                if file["id"] == transfer_id:
                    state = file.get("state", "").lower()
                    logging.debug(f"State for {file['filename']}: {state}")
                    if "completed" in state and "succeeded" in state:
                        filename = os.path.basename(
                            candidate["filename"].replace("\\", "/")
                        )
                        real_path = find_file_in_downloads(filename)
                        logging.info(f"File found: {real_path}")
                        if real_path and _wait_for_external_processing(real_path):
                            return real_path
                        logging.warning(
                            f"File not confirmed after download: {filename}"
                        )
                        return None
                    if any(w in state for w in ("failed", "aborted", "errored")):
                        logging.warning(
                            f"Transfer failed: {file['filename']} — state: {state}"
                        )
                        return None
        if time.time() - start > timeout:
            logging.warning(f"Transfer timeout for {candidate['filename']}")
            return None
        if _interruptible_sleep(2):
            return None


def _wait_for_external_processing(file_path):
    if "/incomplete/" in file_path:
        logging.warning(f"Rejected incomplete path: {file_path}")
        return False

    start = time.time()
    while time.time() - start < EXTERNAL_PROCESS_WAIT_TIMEOUT:
        if os.path.exists(file_path):
            logging.info(f"File confirmed at: {file_path}")
            return True
        if _interruptible_sleep(2):
            return False

    logging.warning(f"File did not appear within timeout: {file_path}")
    return False
