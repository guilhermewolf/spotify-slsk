import os
import time
import logging
import re
import unicodedata

import slskd_api

from .db import (
    get_tried_files,
    add_tried_file,
    get_setting,
    add_blocklist,
    load_blocklist,
    record_history,
)
from . import runtime_state

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
# Independent of the fixed rule in extract_candidates: an MP3 whose reported
# or filename-inferred bitrate is known and below 320 is always rejected.
MIN_EFFECTIVE_MP3_KBPS = int(os.getenv("SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280"))
# Cut the slskd search short once we have this many responses. slskd will
# keep collecting responses for the full searchTimeout window otherwise.
EARLY_STOP_RESPONSES = int(os.getenv("SLSKD_EARLY_STOP_RESPONSES", "20"))
# Reject a candidate whose slskd-reported length differs from Spotify's
# duration by more than this many seconds. 0 disables the check.
MAX_DURATION_DIFF_SECONDS = int(os.getenv("SLSKD_MAX_DURATION_DIFF_SECONDS", "15"))

_client = None
_shutdown_event = None
# The candidate dict behind the most recent successful download_and_verify,
# so the caller can attribute a post-download rejection to the peer file
# (blocklist) and record where the file came from (history). Daemon-thread
# only.
last_download = None


def refresh_from_db(conn) -> None:
    """Reload UI-editable tunables from the DB. Called at each cycle start.

    Infrastructure values (SLSKD_HOST_URL, DOWNLOAD_DIR, SLSKD_URL_BASE)
    stay env-only because they're deploy-time concerns.
    """
    global PREFERRED_FORMATS, MIN_PEER_UPLOAD_SPEED, MIN_EFFECTIVE_MP3_KBPS
    global EARLY_STOP_RESPONSES, EXTERNAL_PROCESS_WAIT_TIMEOUT
    global MAX_DURATION_DIFF_SECONDS

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
        MAX_DURATION_DIFF_SECONDS = int(
            get_setting(conn, "SLSKD_MAX_DURATION_DIFF_SECONDS", "15")
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


def _remote_basename(path):
    """Basename of a peer path. slskd paths use backslashes, which
    os.path.basename does not split on Linux — so the whole share path,
    folder names included, used to be scored and blacklisted."""
    return re.split(r"[\\/]", path or "")[-1]


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


# Words that make a recording a *different* version. Compared as a set on both
# sides, so "Dustup (Original Mix)" can't stand in for "Dustup (Mefjus Remix)"
# and vice versa. Neutral variants (original/extended mix, radio edit,
# remastered, album version) are deliberately absent — `_version_tier` ranks
# those instead.
_VERSION_KEYWORDS = (
    "remix", "dub", "vip", "club mix", "live", "acoustic", "unplugged",
    "instrumental", "karaoke", "cover", "sped up", "slowed", "nightcore", "8d",
    "bootleg", "rework", "remake", "reedit", "flip", "mashup", "acapella",
    "hotmix", "megamix",
)
_VERSION_WORDS = frozenset(w for kw in _VERSION_KEYWORDS for w in kw.split())
# "<word> Mix" / "<word> Edit" is a named version ("Kris M Edit", "Strings
# Mix") unless the word is one of these neutral qualifiers.
_NEUTRAL_QUALIFIERS = frozenset({
    "original", "extended", "radio", "album", "single", "short", "clean",
    "explicit", "main", "full", "long",
})


def _version_keywords(text, artist="") -> frozenset:
    """Version keywords in `text`, ignoring words of the credited artists
    (so an artist called "Dub FX" doesn't make every file a dub)."""
    t = " " + _fold(text) + " "
    for name in _artist_names(artist):
        t = t.replace(f" {name} ", " ")
    found = {kw for kw in _VERSION_KEYWORDS if f" {kw} " in t}
    for word, kind in re.findall(r"(?<= )(\w+) (mix|edit)(?= )", t):
        if word not in _NEUTRAL_QUALIFIERS and word not in _VERSION_WORDS:
            found.add(f"named {kind}")
    return frozenset(found)


def _has_version_marker(text: str):
    found = _version_keywords(text)
    return next((kw for kw in _VERSION_KEYWORDS if kw in found), None)


def _version_mismatch(expected_title: str, file_title: str, artist: str = "") -> bool:
    """True when the two sides declare different sets of version keywords."""
    return _version_keywords(expected_title, artist) != _version_keywords(file_title, artist)


# DJ-oriented version preference. Lower = more preferred. Extended Mix is the
# DJ-friendly long cut; Original Mix is the producer's studio version; everything
# else (radio edits, plain titles) is the fallback. Note: this is intentionally
# orthogonal to `_VERSION_KEYWORDS` / `_version_mismatch` — those reject *wrong*
# versions (e.g. Live when expecting Studio); this only ranks compatible ones.
def _version_tier(text: str) -> int:
    t = (text or "").lower()
    if re.search(r"\bextended\s+(mix|version|edit)\b", t):
        return 0
    if re.search(r"\boriginal\s+(mix|version)\b", t):
        return 1
    return 2


def fmt_duration(seconds) -> str:
    """Seconds -> "m:ss"."""
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _effective_mp3_kbps(size_bytes, length_sec):
    if not size_bytes or not length_sec or length_sec <= 0:
        return None
    try:
        return int((size_bytes * 8) / length_sec / 1000)
    except Exception:
        return None


# --- Candidate title/artist matching ------------------------------------------
# The old scorer ran token_set_ratio over the whole name, which is 100 whenever
# every expected title word appears *anywhere*, and let one shared word clear
# the artist bar ("super flu" vs "...Super Supergirl" = 71). Title and artist
# are now judged separately against the basename.

# Neutral release words: not required when matching the expected title.
_NEUTRAL_TITLE_WORDS = frozenset({
    "original", "extended", "radio", "album", "single", "mix", "edit",
    "version", "remastered", "remaster", "mono", "stereo", "and",
})
# Filename noise that is neither title nor artist.
_FILENAME_NOISE = frozenset({
    "flac", "mp3", "wav", "aiff", "m4a", "ogg", "kbps", "web", "cd",
    "vinyl", "lossless", "hq", "bit", "khz",
})
# Share of the basename's title-ish words that must be the expected title.
# Rejects "Dream" -> "A Real Bad Dream" while tolerating an album name in the
# filename ("Daft Punk - Random Access Memories - 08 - Get Lucky" = 0.4).
MIN_TITLE_COVERAGE = 0.4
# An artist matched with spaces removed ("FormatB" for "Format:B") must be at
# least this long, so short names can't hide inside other words.
MIN_COMPACT_ARTIST_LEN = 6

_FEAT_RE = re.compile(
    r"[\(\[]\s*(?:feat|ft|featuring)\b[^\)\]]*[\)\]]"
    r"|\b(?:feat|ft|featuring)\b.*?(?=\s[-–—]\s|[\(\[]|$)",
    re.IGNORECASE,
)
_BRACKETS_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# Credit separators. "/" is deliberately absent: it is part of names (AC/DC).
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b"
    r"|\s+x\s+|\s+vs\b\.?|\s+with\s+|\s+and\s+)\s*",
    re.IGNORECASE,
)


def _fold(text) -> str:
    """Lowercase, strip accents, collapse every non-alphanumeric run to a
    space, and spell "rmx" as "remix"."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join("remix" if w == "rmx" else w for w in re.findall(r"[^\W_]+", t))


def _expected_title_tokens(title) -> set:
    """Words a file must contain to be this title. Featured artists, neutral
    release words and bare numbers (years) are not required."""
    folded = _fold(_FEAT_RE.sub(" ", title or "")).split()
    core = {t for t in folded if t not in _NEUTRAL_TITLE_WORDS and not t.isdigit()}
    return core or {t for t in folded if not t.isdigit()} or set(folded)


def _artist_names(artist_str) -> list:
    """The full credit plus each credited artist, folded, "The " dropped."""
    names = []
    for part in [artist_str or ""] + _ARTIST_SPLIT_RE.split(artist_str or ""):
        name = _fold(part)
        if name.startswith("the "):
            name = name[4:]
        if name and name not in names:
            names.append(name)
    return names


def _artist_in(name, folded_text) -> bool:
    """Whole-name match: the artist's words as a contiguous run, never a
    single shared word ("kris m" is not in "kris kross")."""
    if f" {name} " in f" {folded_text} ":
        return True
    compact = name.replace(" ", "")
    return len(compact) >= MIN_COMPACT_ARTIST_LEN and compact in folded_text.replace(" ", "")


def _title_coverage(expected_tokens, stem, artist_names, folder_tokens) -> float:
    """0.0 unless every expected title word is in the basename; otherwise the
    share of the basename's title-ish words (outside brackets, minus artists,
    featured credits, numbers, noise and folder words) that are the title.
    A " - " segment naming an expected artist is the artist credit and is
    dropped whole, co-credits included ("Daft Punk, Pharrell Williams - ...")."""
    if not expected_tokens or not expected_tokens <= set(_fold(stem).split()):
        return 0.0
    segments = re.split(r"\s+[-–—]\s+", _BRACKETS_RE.sub(" ", _FEAT_RE.sub(" ", stem)))
    outside = " "
    for seg in segments:
        folded = " " + _fold(seg) + " "
        credit = any(_artist_in(n, folded.strip()) for n in artist_names)
        if credit and not expected_tokens <= set(folded.split()):
            continue  # pure artist credit
        for name in artist_names:
            folded = folded.replace(f" {name} ", " ")
        outside += folded
    extras = [
        t for t in outside.split()
        if t not in expected_tokens
        and t not in _NEUTRAL_TITLE_WORDS
        and t not in _FILENAME_NOISE
        and t not in folder_tokens
        and not t.isdigit()
    ]
    return len(expected_tokens) / (len(expected_tokens) + len(extras))


def filename_match(path, expected_title, expected_artist):
    """Judge a peer path's name against the expected track with the strict
    wrong-song rules. Returns (title_coverage, reason); reason is None when
    the name is this track.

    Title evidence comes from the basename only; artist evidence may also
    come from the folders (shares are laid out Artist\\Album\\NN - Title).
    Folder text never supplies title words - that is how a "Vespers
    (All-Night Vigil)" folder matched "All Night".
    """
    base = _remote_basename(path)
    stem = os.path.splitext(base)[0]
    folder_folded = _fold(" ".join(re.split(r"[\\/]", path or "")[:-1]))
    artist_names = _artist_names(expected_artist)
    coverage = _title_coverage(
        _expected_title_tokens(expected_title), stem, artist_names,
        set(folder_folded.split()),
    )
    base_folded = _fold(stem)
    if coverage == 0.0:
        return coverage, "title words missing from filename"
    if coverage < MIN_TITLE_COVERAGE:
        return coverage, f"title coverage {coverage:.2f} < {MIN_TITLE_COVERAGE}"
    if not any(
        _artist_in(n, base_folded) or _artist_in(n, folder_folded)
        for n in artist_names
    ):
        return coverage, "artist not in filename or folder"
    # A different version (remix/live/... on one side only) is never usable.
    if _version_mismatch(expected_title, stem, expected_artist):
        got = ", ".join(sorted(_version_keywords(stem, expected_artist))) or "none"
        want = ", ".join(sorted(_version_keywords(expected_title, expected_artist))) or "none"
        return coverage, f"version mismatch ({got} vs expected {want})"
    return coverage, None


def evaluate_candidates(
    search_results,
    expected_title,
    expected_artist,
    max_version_tier=None,
    expected_duration_ms=None,
    is_blocked=None,
):
    """
    Judge every file in slskd search results against the expected track.

    Returns one dict per file (in response order) carrying the candidate
    fields plus ``accepted`` and, when rejected, a short human ``reason``
    ("format not allowed: .wma", "bitrate 192 < 320", ...). Unknown MP3
    bitrates are allowed (slskd often reports None); files are hard-rejected
    only when we *know* quality is too low.

    `max_version_tier`, when set, rejects anything not *strictly better*
    (used by the upgrade pass). `expected_duration_ms` (Spotify) rejects
    files whose reported length is more than MAX_DURATION_DIFF_SECONDS off.
    `is_blocked(username, filename)` (see db.load_blocklist) rejects
    blocklisted peer files and banned peers. Title/artist/version use the
    strict rules in `filename_match`.
    """
    evaluated = []

    for result in search_results:
        user = result.get("username", "unknown")
        user_upload_speed = result.get("uploadSpeed", 0) or 0

        for file in result.get("files", []):
            filename = file.get("filename")
            if not filename:
                continue

            ext = os.path.splitext(filename)[1].lower()
            # slskd sends camelCase "bitRate" (only for lossy files); the old
            # lowercase read was always None, so the floor below never applied.
            reported_bitrate = file.get("bitRate")
            length_sec = file.get("length")
            size_bytes = file.get("size")
            base = _remote_basename(filename)
            inferred_bitrate = _infer_bitrate_from_name(base)
            effective_bitrate = (
                reported_bitrate if reported_bitrate is not None else inferred_bitrate
            )
            effective_mp3 = (
                _effective_mp3_kbps(size_bytes, length_sec) if ext == ".mp3" else None
            )
            entry = {
                "user": user,
                "user_upload_speed": user_upload_speed,
                # Full peer path: slskd needs it to enqueue the transfer, and
                # it is the blocklist key. `basename` is what `_tried` stores.
                "filename": filename,
                "basename": base,
                "size": size_bytes,
                "bitrate": effective_bitrate,
                "effective_mp3": effective_mp3,
                "ext": ext,
                "length": length_sec,
                "sample_rate": file.get("sampleRate"),
                "bit_depth": file.get("bitDepth"),
                "free_slot": result.get("hasFreeUploadSlot"),
                "queue_length": result.get("queueLength"),
                "title_coverage": None,
                "version_tier": _version_tier(base),
                "accepted": False,
                "reason": None,
            }
            evaluated.append(entry)

            if is_blocked is not None and is_blocked(user, filename):
                entry["reason"] = "blocklisted"
            elif ext not in PREFERRED_FORMATS:
                entry["reason"] = f"format not allowed: {ext or '(none)'}"
            # Reject MP3s whose reported/inferred bitrate is known-low.
            elif ext == ".mp3" and effective_bitrate is not None and effective_bitrate < 320:
                entry["reason"] = f"bitrate {effective_bitrate} < 320"
            # Reject MP3s whose size/duration shows they're effectively below the
            # configured floor — catches 128 kbps files retagged as 320.
            elif (
                ext == ".mp3"
                and effective_mp3 is not None
                and effective_mp3 < MIN_EFFECTIVE_MP3_KBPS
            ):
                entry["reason"] = (
                    f"effective bitrate {effective_mp3} < {MIN_EFFECTIVE_MP3_KBPS}"
                )
            elif (
                MAX_DURATION_DIFF_SECONDS > 0
                and expected_duration_ms
                and length_sec
                and abs(length_sec - expected_duration_ms / 1000)
                > MAX_DURATION_DIFF_SECONDS
            ):
                entry["reason"] = (
                    f"duration {fmt_duration(length_sec)} vs "
                    f"{fmt_duration(expected_duration_ms / 1000)}"
                )
            if entry["reason"]:
                logging.debug(f"Skipped {filename}: {entry['reason']}")
                continue

            title_coverage, reason = filename_match(filename, expected_title, expected_artist)
            entry["title_coverage"] = round(title_coverage, 2)
            if reason:
                entry["reason"] = reason
            elif max_version_tier is not None and entry["version_tier"] >= max_version_tier:
                entry["reason"] = "not a better version than the current file"
            else:
                entry["accepted"] = True
            if entry["reason"]:
                logging.debug(f"Skipped {filename}: {entry['reason']}")

    return evaluated


def extract_candidates(search_results, expected_title, expected_artist):
    """Return only the accepted candidates from `evaluate_candidates`."""
    return [
        c
        for c in evaluate_candidates(search_results, expected_title, expected_artist)
        if c["accepted"]
    ]


def summarize_evaluation(evaluated, limit=50):
    """Compact form of an evaluation for the per-track search history."""
    rejected = [c for c in evaluated if not c["accepted"]]
    return {
        "candidate_count": len(evaluated) - len(rejected),
        "rejected_count": len(rejected),
        "rejections": [
            {"user": c["user"], "filename": c["filename"], "reason": c["reason"]}
            for c in rejected[:limit]
        ],
    }


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
        base_dir = DOWNLOAD_DIR
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
        basename = _remote_basename(candidate["filename"])

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
    basename = _remote_basename(candidate["filename"])
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
                    _remote_basename(file["filename"])
                    == _remote_basename(candidate["filename"])
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
                        filename = _remote_basename(candidate["filename"])
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
    while time.time() - start < EXTERNAL_PROCESS_WAIT_TIMEOUT:
        if os.path.exists(file_path):
            logging.info(f"File confirmed at: {file_path}")
            return True
        if _interruptible_sleep(2):
            return False

    logging.warning(f"File did not appear within timeout: {file_path}")
    return False
