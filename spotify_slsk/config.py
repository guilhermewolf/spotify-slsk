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
import sqlite3

from .db import get_setting

DEFAULT_FORMATS = "flac,mp3,aiff,wav"
# Formats that claim to be lossless.
LOSSLESS_EXTS = (".flac", ".wav", ".aiff")


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
PREFERRED_FORMATS = normalize_ext_list(os.getenv("SLSKD_PREFERRED_FORMATS", DEFAULT_FORMATS))
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
# Re-run the library health scan this often; 0 = only from the UI button.
LIBRARY_SCAN_INTERVAL_HOURS = int(os.getenv("LIBRARY_SCAN_INTERVAL_HOURS", "0"))


def reload(conn: sqlite3.Connection) -> None:
    """Pull live tunables from the DB so UI changes take effect next cycle.

    Infrastructure values (SLSKD_HOST_URL, DOWNLOAD_DIR, SLSKD_URL_BASE)
    stay env-only because they're deploy-time concerns. A value that won't
    parse keeps the previous one (and logs a warning).
    """
    global MIN_MATCH_SCORE, HISTORY_RETENTION_DAYS
    global PREFERRED_FORMATS, MIN_PEER_UPLOAD_SPEED, MIN_EFFECTIVE_MP3_KBPS
    global EARLY_STOP_RESPONSES, EXTERNAL_PROCESS_WAIT_TIMEOUT
    global MAX_DURATION_DIFF_SECONDS
    global LOSSLESS_CHECK_ENABLED, LOSSLESS_FAKE_CUTOFF_HZ, LOSSLESS_MIN_DROP_DB
    global LIBRARY_SCAN_INTERVAL_HOURS
    try:
        MIN_MATCH_SCORE = float(get_setting(conn, "MIN_MATCH_SCORE", "0.62"))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad MIN_MATCH_SCORE in DB; keeping previous: {e}")
    try:
        HISTORY_RETENTION_DAYS = max(1, int(get_setting(conn, "HISTORY_RETENTION_DAYS", "90")))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad HISTORY_RETENTION_DAYS in DB; keeping previous: {e}")

    PREFERRED_FORMATS = normalize_ext_list(
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
    LOSSLESS_CHECK_ENABLED = get_setting(conn, "LOSSLESS_CHECK_ENABLED", "1") == "1"
    try:
        LOSSLESS_FAKE_CUTOFF_HZ = int(get_setting(conn, "LOSSLESS_FAKE_CUTOFF_HZ", "19500"))
        LOSSLESS_MIN_DROP_DB = int(get_setting(conn, "LOSSLESS_MIN_DROP_DB", "15"))
        LIBRARY_SCAN_INTERVAL_HOURS = int(get_setting(conn, "LIBRARY_SCAN_INTERVAL_HOURS", "0"))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad lossless-check setting during refresh; using previous values: {e}")


# ---- settings page spec + validation ----------------------------------------

# (key, default, help text, kind). `kind` drives validation on save — an
# unvalidated setting is worse than no setting: reload() silently keeps the
# previous value on a bad parse, so the UI would report "Saved" for a value
# that never takes effect.
SETTINGS_SPEC = [
    ("MIN_MATCH_SCORE", "0.62", "Accept threshold for downloaded-file -> DB match (0.0-1.0)", "ratio"),
    ("SLSKD_PREFERRED_FORMATS", "flac,mp3,aiff,wav", "Comma-separated, ordered — first format wins ties", "formats"),
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
    ("LIBRARY_SCAN_INTERVAL_HOURS", "0", "Re-verify every library file (tags, duration, ISRC, lossless check) this often; report-only, a few hundred files per cycle. 0 = only when you press Scan library", "int0"),
]

# Formats the tagging / metadata-extraction code can actually handle.
_KNOWN_FORMATS = {"flac", "mp3", "aiff", "wav", "m4a", "ogg"}


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

    if kind == "formats":
        items = [f.strip().lstrip(".").lower() for f in value.split(",")]
        items = [f for f in items if f]
        if not items:
            return None, "needs at least one format"
        unknown = [f for f in items if f not in _KNOWN_FORMATS]
        if unknown:
            return None, (
                f"unsupported format(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(_KNOWN_FORMATS))}"
            )
        return ",".join(items), None

    return value, None
