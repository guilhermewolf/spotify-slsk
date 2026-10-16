"""Tunables: the single source of truth for runtime configuration.

Two kinds of value live here:

* **Deploy-time** (env only, read once at import): paths, cycle timing,
  heartbeat. Changing them means restarting the container.
* **UI-editable** (env is the default, a ``settings`` row overrides it): the
  matching and slskd knobs below. ``reload(conn)`` re-reads them at the start
  of every cycle, so a save on /settings applies to the next cycle.

Readers must use ``config.NAME`` at call time, never ``from .config import
NAME``: ``reload`` rebinds the module attributes, and an imported copy would
keep the boot-time value forever.

Every UI-editable key is declared in ``SETTINGS_SPEC`` and validated on save
by ``validate_setting``. That matters because ``reload`` silently keeps the
previous value when a stored value won't parse — an unvalidated save would
report success for a setting that never applies.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3

from .db import delete_setting, get_setting, list_settings, set_setting
from .library import naming
from .matching.quality import (
    QualityProfile,
    parse_profile,
    profile_from_formats,
)

DEFAULT_FORMATS = "flac,mp3,aiff,wav"
# Formats that claim to be lossless.
LOSSLESS_EXTS = (".flac", ".wav", ".aiff")


# A comma-separated term list; `/.../` is a regex and may contain commas.
_TERM_RE = re.compile(r"\s*(/(?:\\.|[^\\/])+/|[^,]*)\s*(?:,|$)")
Terms = list[tuple[str, re.Pattern[str]]]


def parse_terms(value: str | None) -> tuple[Terms | None, str | None]:
    """Release-profile terms (Lidarr's required/ignored): plain terms match
    as case-insensitive substrings, `/regex/` as a case-insensitive search.
    Returns ([(label, pattern)], error); exactly one is None."""
    terms: Terms = []
    text, pos = value or "", 0
    while pos < len(text):
        m = _TERM_RE.match(text, pos)
        if m is None or m.end() == pos:
            break
        pos = m.end()
        token = m.group(1).strip()
        if not token:
            continue
        if len(token) > 2 and token.startswith("/") and token.endswith("/"):
            try:
                pattern = re.compile(token[1:-1], re.IGNORECASE)
            except re.error as e:
                return None, f"invalid regex {token}: {e}"
        else:
            pattern = re.compile(re.escape(token), re.IGNORECASE)
        terms.append((token, pattern))
    return terms, None


def first_term(terms: Terms, text: str) -> str | None:
    """Label of the first term found in a peer path, or None. Matched
    against the path with `\\` normalised to `/` (so "Artist/Album" works on
    slskd's backslash paths); a /regex/ also sees the raw path."""
    normalised = text.replace("\\", "/")
    for label, pattern in terms:
        if pattern.search(normalised) or (label.startswith("/") and pattern.search(text)):
            return label
    return None


def _env_terms(key: str) -> Terms:
    terms, error = parse_terms(os.getenv(key, ""))
    if terms is None:
        logging.error(f"Invalid {key} in the environment ({error}); ignoring it")
        return []
    return terms


def normalize_ext_list(env_val: str) -> list[str]:
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


# ---- deploy-time (env only) -------------------------------------------------

DOWNLOAD_DIR = os.getenv("SLSKD_DOWNLOADS_DIR", "/downloads")
CYCLE_INTERVAL_SECONDS = int(os.getenv("CYCLE_INTERVAL_SECONDS", "300"))
CYCLE_ERROR_BACKOFF_SECONDS = int(os.getenv("CYCLE_ERROR_BACKOFF_SECONDS", "60"))
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")

# ---- UI-editable (refreshed by reload) --------------------------------------

MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.62"))
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", "90"))
# The quality profile (matching/quality.py). Stored as JSON under
# QUALITY_PROFILE, edited on /settings (not in SETTINGS_SPEC: it has its own
# form). SLSKD_PREFERRED_FORMATS is deprecated: it is read only to build the
# first profile (ensure_quality_profile).
QUALITY_PROFILE: QualityProfile = profile_from_formats(
    normalize_ext_list(os.getenv("SLSKD_PREFERRED_FORMATS", DEFAULT_FORMATS))
)
EXTERNAL_PROCESS_WAIT_TIMEOUT = int(os.getenv("SLSKD_WAIT_TIMEOUT", "60"))
MIN_PEER_UPLOAD_SPEED = int(os.getenv("SLSKD_MIN_PEER_UPLOAD_SPEED", "0"))
# MP3s whose effective bitrate (size*8/duration/1000) falls below this are
# rejected even if their reported bitrate claims 320 — catches upsampled fakes.
# Independent of the fixed rule in evaluate_candidates: an MP3 whose reported
# or filename-inferred bitrate is known and below 320 is always rejected.
MIN_EFFECTIVE_MP3_KBPS = int(os.getenv("SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280"))
# Cut the slskd search short once we have this many responses. slskd will
# keep collecting responses for the full searchTimeout window otherwise.
EARLY_STOP_RESPONSES = int(os.getenv("SLSKD_EARLY_STOP_RESPONSES", "20"))
# Reject a candidate whose slskd-reported length differs from Spotify's
# duration by more than this many seconds. 0 disables the check.
MAX_DURATION_DIFF_SECONDS = int(os.getenv("SLSKD_MAX_DURATION_DIFF_SECONDS", "15"))
# Fake-lossless detection (importer/audio_check.py). Measured margins between
# real masters and lossy transcodes are only ~3-5 dB, hence tunable.
LOSSLESS_CHECK_ENABLED = os.getenv("LOSSLESS_CHECK_ENABLED", "1") == "1"
LOSSLESS_FAKE_CUTOFF_HZ = int(os.getenv("LOSSLESS_FAKE_CUTOFF_HZ", "19500"))
LOSSLESS_MIN_DROP_DB = int(os.getenv("LOSSLESS_MIN_DROP_DB", "15"))
# Release terms (matching.candidates), applied to the full peer path before
# any scoring. Empty = no filtering.
REQUIRED_TERMS: Terms = _env_terms("REQUIRED_TERMS")
IGNORED_TERMS: Terms = _env_terms("IGNORED_TERMS")
# Re-run the library health scan this often; 0 = only from the UI button.
LIBRARY_SCAN_INTERVAL_HOURS = int(os.getenv("LIBRARY_SCAN_INTERVAL_HOURS", "0"))
# Embed the Spotify album cover when tagging (importer.tags).
EMBED_COVER_ART = os.getenv("EMBED_COVER_ART", "1") == "1"
# Purge files in .quarantine/*/replaced/ older than this many days; 0 = never.
RECYCLE_BIN_RETENTION_DAYS = int(os.getenv("RECYCLE_BIN_RETENTION_DAYS", "0"))
# A track already downloaded in another playlist: hardlink | copy | off.
LINK_MODES = ("hardlink", "copy", "off")
LINK_MODE = os.getenv("LINK_MODE", "hardlink")
# Basename template for newly imported files (library.naming).
FILE_NAME_TEMPLATE = os.getenv("FILE_NAME_TEMPLATE", naming.DEFAULT_TEMPLATE)


def reload(conn: sqlite3.Connection) -> None:
    """Pull live tunables from the DB so UI changes take effect next cycle.

    Infrastructure values (SLSKD_HOST_URL, DOWNLOAD_DIR, SLSKD_URL_BASE)
    stay env-only because they're deploy-time concerns. A value that won't
    parse keeps the previous one (and logs a warning).
    """
    global MIN_MATCH_SCORE, HISTORY_RETENTION_DAYS
    global QUALITY_PROFILE, MIN_PEER_UPLOAD_SPEED, MIN_EFFECTIVE_MP3_KBPS
    global EARLY_STOP_RESPONSES, EXTERNAL_PROCESS_WAIT_TIMEOUT
    global MAX_DURATION_DIFF_SECONDS
    global LOSSLESS_CHECK_ENABLED, LOSSLESS_FAKE_CUTOFF_HZ, LOSSLESS_MIN_DROP_DB
    global LIBRARY_SCAN_INTERVAL_HOURS, REQUIRED_TERMS, IGNORED_TERMS
    global EMBED_COVER_ART, FILE_NAME_TEMPLATE, LINK_MODE, RECYCLE_BIN_RETENTION_DAYS
    try:
        MIN_MATCH_SCORE = float(get_setting(conn, "MIN_MATCH_SCORE", "0.62"))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad MIN_MATCH_SCORE in DB; keeping previous: {e}")
    try:
        HISTORY_RETENTION_DAYS = max(1, int(get_setting(conn, "HISTORY_RETENTION_DAYS", "90")))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad HISTORY_RETENTION_DAYS in DB; keeping previous: {e}")

    for key in ("REQUIRED_TERMS", "IGNORED_TERMS"):
        terms, error = parse_terms(get_setting(conn, key, ""))
        if terms is None:
            logging.warning(f"Bad {key} in DB; keeping previous: {error}")
        elif key == "REQUIRED_TERMS":
            REQUIRED_TERMS = terms
        else:
            IGNORED_TERMS = terms
    QUALITY_PROFILE = load_quality_profile(conn)
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
    LOSSLESS_CHECK_ENABLED = get_setting(conn, "LOSSLESS_CHECK_ENABLED", "1") == "1"
    EMBED_COVER_ART = get_setting(conn, "EMBED_COVER_ART", "1") == "1"
    link_mode = get_setting(conn, "LINK_MODE", "hardlink")
    if link_mode in LINK_MODES:
        LINK_MODE = link_mode
    else:
        logging.warning(f"Bad LINK_MODE in DB ({link_mode!r}); keeping previous")
    template = get_setting(conn, "FILE_NAME_TEMPLATE", naming.DEFAULT_TEMPLATE) or naming.DEFAULT_TEMPLATE
    error = naming.validate_template(template)
    if error:
        logging.warning(f"Bad FILE_NAME_TEMPLATE in DB ({error}); keeping previous")
    else:
        FILE_NAME_TEMPLATE = template
    try:
        LOSSLESS_FAKE_CUTOFF_HZ = int(get_setting(conn, "LOSSLESS_FAKE_CUTOFF_HZ", "19500"))
        LOSSLESS_MIN_DROP_DB = int(get_setting(conn, "LOSSLESS_MIN_DROP_DB", "15"))
        LIBRARY_SCAN_INTERVAL_HOURS = int(get_setting(conn, "LIBRARY_SCAN_INTERVAL_HOURS", "0"))
        RECYCLE_BIN_RETENTION_DAYS = int(get_setting(conn, "RECYCLE_BIN_RETENTION_DAYS", "0"))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad lossless-check setting during refresh; using previous values: {e}")


# ---- quality profile ---------------------------------------------------------

QUALITY_PROFILE_KEY = "QUALITY_PROFILE"


def load_quality_profile(conn: sqlite3.Connection) -> QualityProfile:
    """The stored profile; before one is stored - or when the stored value
    is invalid (logged as an error) - the profile equivalent to
    SLSKD_PREFERRED_FORMATS. Never raises: a bad row must not fail every
    cycle."""
    raw = get_setting(conn, QUALITY_PROFILE_KEY, None)
    if raw:
        profile, error = parse_profile(raw)
        if profile is not None:
            return profile
        logging.error(
            f"Invalid {QUALITY_PROFILE_KEY} in the settings table ({error}); "
            f"using the profile derived from SLSKD_PREFERRED_FORMATS until it is re-saved"
        )
    return profile_from_formats(
        normalize_ext_list(get_setting(conn, "SLSKD_PREFERRED_FORMATS", DEFAULT_FORMATS))
    )


def ensure_quality_profile(conn: sqlite3.Connection) -> None:
    """First boot after Tier 4: freeze the profile derived from
    SLSKD_PREFERRED_FORMATS (DB override, else env, else the default) into
    the settings table and drop the deprecated DB override, so editing the
    profile is the only way to change what is accepted from now on."""
    stored = list_settings(conn)
    if os.getenv("SLSKD_PREFERRED_FORMATS"):
        logging.warning(
            "SLSKD_PREFERRED_FORMATS is deprecated and only seeded the quality "
            "profile on first boot; edit the profile on /settings instead"
        )
    if QUALITY_PROFILE_KEY in stored:
        return
    profile = load_quality_profile(conn)
    set_setting(conn, QUALITY_PROFILE_KEY, profile.to_json())
    if "SLSKD_PREFERRED_FORMATS" in stored:
        delete_setting(conn, "SLSKD_PREFERRED_FORMATS")
    logging.info(
        f"Quality profile created from SLSKD_PREFERRED_FORMATS: "
        f"{', '.join(profile.allowed)} (cutoff {profile.cutoff})"
    )


# ---- settings page spec + validation ----------------------------------------

# (key, default, help text, kind). `kind` drives validation on save — an
# unvalidated setting is worse than no setting: reload() silently keeps the
# previous value on a bad parse, so the UI would report "Saved" for a value
# that never takes effect.
SETTINGS_SPEC = [
    ("MIN_MATCH_SCORE", "0.62", "Accept threshold for downloaded-file -> DB match (0.0-1.0)", "ratio"),
    ("SLSKD_MIN_PEER_UPLOAD_SPEED", "0", "Passed to slskd; bytes/sec", "int0"),
    ("SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280", "Floor (kbps) for an MP3's effective bitrate, computed from file size and duration; catches 128 kbps files retagged as 320. Separate from the fixed 320 rule: an MP3 whose reported (or filename-inferred) bitrate is below 320 is always rejected, whatever this is set to", "int0"),
    ("SLSKD_EARLY_STOP_RESPONSES", "20", "Stop the slskd search once this many peers replied", "int1"),
    ("SLSKD_MAX_DURATION_DIFF_SECONDS", "15", "Reject candidates whose length differs from Spotify's by more than this many seconds; also the tolerance for accepting an untagged download by filename + duration. 0 disables both", "int0"),
    ("SLSKD_WAIT_TIMEOUT", "60", "Seconds to wait for a downloaded file to appear on disk", "int1"),
    ("HISTORY_RETENTION_DAYS", "90", "Delete history events older than this many days", "int1"),
    ("UPGRADE_CHECK_INTERVAL_HOURS", "168", "Re-search downloaded tracks for a better version_tier (Extended Mix > Original Mix); 0 disables", "int0"),
    ("LOSSLESS_CHECK_ENABLED", "1", "Spectrum-check downloaded FLAC/WAV files and reject lossy transcodes (1 = on, 0 = off)", "bool"),
    ("LOSSLESS_FAKE_CUTOFF_HZ", "19500", "A steep spectral drop below this frequency (Hz) marks a FLAC as a lossy transcode. 128-192 kbps MP3s cut at 16-19 kHz; MP3-320 at ~20 kHz is only flagged 'suspect'", "int1"),
    ("LOSSLESS_MIN_DROP_DB", "15", "How steep (dB over ~1 kHz) that drop must be. Lower catches more fakes but risks flagging dark-sounding real masters; measured margins are ~3-5 dB", "int1"),
    ("REQUIRED_TERMS", "", "A file's full peer path must contain at least one of these (comma-separated, case-insensitive; /regex/ allowed). Empty = no requirement", "terms"),
    ("IGNORED_TERMS", "", "Reject a file whose full peer path contains any of these, even when the title matches (comma-separated, case-insensitive; /regex/ allowed). Common: karaoke, instrumental, nightcore, sped up, 8d audio, /\\bcover\\b/. Plain terms match inside words ('live' matches 'Oliver'): use a /\\b...\\b/ regex for whole words", "terms"),
    ("EMBED_COVER_ART", "1", "Embed the Spotify album cover (largest size, cached under data/artwork) when tagging imported or re-tagged files (1 = on, 0 = off). A failed fetch never fails the import", "bool"),
    ("FILE_NAME_TEMPLATE", naming.DEFAULT_TEMPLATE, "Name for newly imported files (existing files keep their names until you apply a rename on Library -> Rename). Tokens: " + " ".join(f"{{{t}}}" for t in naming.TOKENS) + "; numbers take a zero-pad width, e.g. {track:02} {playlist_index:03}. The extension is the file's own. {original_filename} (the default) keeps the peer's file name. Unsafe characters are replaced, Windows-reserved names avoided and names capped at 200 bytes; an existing file is never overwritten (' (2)' is added)", "template"),
    ("LINK_MODE", "hardlink", "A track already downloaded in another playlist (same Spotify id or ISRC) is linked into this playlist's folder instead of being searched and downloaded again. hardlink: no extra disk space, falls back to a copy across filesystems; copy: an independent file; off: always download. Removing one playlist's copy never affects the other", "choice:hardlink|copy|off"),
    ("RECYCLE_BIN_RETENTION_DAYS", "0", "Files replaced by an upgrade or a manual grab go to the recycle bin, .quarantine/<playlist>/replaced/. When this is above 0, a daily step deletes those older than this many days (counted from when they were replaced). 0 = keep them forever. Files you quarantined yourself (.quarantine/<playlist>/) are never deleted automatically", "int0"),
    ("LIBRARY_SCAN_INTERVAL_HOURS", "0", "Re-verify every library file (tags, duration, ISRC, lossless check) this often; report-only, a few hundred files per cycle. 0 = only when you press Scan library", "int0"),
]


def validate_setting(kind: str, value: str) -> tuple[str | None, str | None]:
    """Return (normalised_value, error). Exactly one is non-None."""
    if kind == "ratio":
        try:
            parsed = float(value)
        except ValueError:
            return None, "must be a number between 0 and 1"
        if not 0.0 <= parsed <= 1.0:
            return None, "must be between 0 and 1"
        return str(parsed), None

    if kind in ("int0", "int1"):
        minimum = 0 if kind == "int0" else 1
        try:
            parsed_int = int(value)
        except ValueError:
            return None, f"must be a whole number >= {minimum}"
        if parsed_int < minimum:
            return None, f"must be >= {minimum}"
        return str(parsed_int), None

    if kind == "bool":
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return "1", None
        if lowered in ("0", "false", "no", "off"):
            return "0", None
        return None, "must be 1 (on) or 0 (off)"

    if kind.startswith("choice:"):
        choices = kind[len("choice:"):].split("|")
        lowered = value.strip().lower()
        return (lowered, None) if lowered in choices else (None, f"must be one of {', '.join(choices)}")

    if kind == "template":
        error = naming.validate_template(value)
        return (None, error) if error else (value, None)

    if kind == "terms":
        terms, error = parse_terms(value)
        if terms is None:
            return None, error
        return ", ".join(label for label, _ in terms), None

    return value, None
