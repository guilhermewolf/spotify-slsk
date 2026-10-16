"""Library health scan: re-verify files already in the library (report-only).

For every downloaded row of every enabled playlist, the file is checked
against the DB row with the current strict rules - tags (score_track_match,
version keywords), real duration vs Spotify's, ISRC - and lossless files get
the spectrum check. Results go to `file_checks`; nothing is moved or
deleted here (quarantine is a separate, operator-triggered action).

The scan is chunked: `scan_step` does bounded work (files and seconds) per
daemon cycle, stops on shutdown, and is resumable for free - a file whose
check is current (same path, size, mtime and RULES_VERSION) is skipped, so
the next step continues where the last one stopped.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .. import config
from ..db import (
    add_tried_file,
    delete_file_check,
    get_file_check,
    get_track,
    list_playlists,
    list_tracks,
    record_history,
    reset_track_for_redownload,
    save_file_check,
    set_track_quality,
    tracks_missing_quality,
)
from ..importer.audio_check import (
    CHECKABLE_EXTS,
    REJECT_VERDICTS,
    SUSPECT,
    check_file,
    rules_key,
)
from ..importer.tags import (
    audio_length_seconds,
    extract_metadata_from_file,
    normalize_isrc,
    read_isrc,
)
from ..matching.candidates import _duration_off, filename_match
from ..matching.normalize import version_mismatch, version_tier
from ..matching.quality import quality_of_file
from ..matching.tracks import score_track_match
from ..utils import fmt_duration

# Bump when the identity rules below change: every file is re-verified once.
RULES_VERSION = 1

# A differing ISRC alone is not a problem (singles, albums, remasters and
# compilations carry different ISRCs for the same song): it is stored as
# evidence["isrc_note"] and listed separately on the page, never counted.
ISRC_NOTE = "isrc_note"

# Problem codes, in display priority order.
PROBLEMS = (
    "missing_file",
    "version_mismatch",
    "title_artist_mismatch",
    "duration_off",
    "fake_lossless",
    "suspect_lossless",
)
PROBLEM_LABELS = {
    "missing_file": "Missing file",
    "version_mismatch": "Wrong song: version mismatch",
    "title_artist_mismatch": "Wrong song: title/artist mismatch",
    "duration_off": "Duration off",
    "fake_lossless": "Fake lossless",
    "suspect_lossless": "Suspect lossless",
}

# Internal settings keys (underscore: never rendered on /settings).
SCAN_PENDING_KEY = "_library_scan_pending"
SCAN_COMPLETED_KEY = "_library_scan_completed_at"

# Under the playlists root, beside the pl_<name> folders. Nothing walks it:
# the scan iterates DB rows, startup_check only <root>/<table>.
QUARANTINE_DIR = ".quarantine"

MAX_FILES_PER_STEP = 200
MAX_SECONDS_PER_STEP = 120.0


def _stat(path: str | None) -> tuple[int | None, float | None]:
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    return (st.st_size, st.st_mtime) if st else (None, None)


def is_current(check: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    """True when `check` already describes the row's file under the current
    rules - identity (RULES_VERSION) and lossless thresholds (rules_key), so
    changing a LOSSLESS_* setting and pressing Scan re-evaluates."""
    return bool(
        check
        and check["rules_version"] == RULES_VERSION
        and check["evidence"].get("lossless_rules") == rules_key()
        and check["path"] == row["path"]
        and (check["size"], check["mtime"]) == _stat(row["path"])
    )


def verify_file(row: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check one downloaded row's file. Returns save_file_check kwargs.

    `previous` lets an unchanged file reuse its spectrum verdict (the
    expensive part) when only the identity rules need re-running.
    """
    path = row["path"]
    size, mtime = _stat(path)
    expected_isrc = normalize_isrc(row.get("isrc"))
    evidence: dict[str, Any] = {
        "expected_title": row["name"],
        "expected_artist": row["artists"],
        "expected_duration_s": round(row["duration_ms"] / 1000) if row.get("duration_ms") else None,
        "expected_isrc": expected_isrc,
        "lossless_rules": rules_key(),
        "reasons": {},
    }
    result: dict[str, Any] = {
        "path": path, "size": size, "mtime": mtime, "rules_version": RULES_VERSION,
        "evidence": evidence,
    }
    reasons: dict[str, str] = evidence["reasons"]
    if size is None:
        reasons["missing_file"] = f"no file at {path}" if path else "no path recorded"
        result["problems"] = ["missing_file"]
        return result

    title, artist, _album = extract_metadata_from_file(path)
    length = audio_length_seconds(path)
    file_isrc = read_isrc(path)
    evidence.update(
        file_title=title, file_artist=artist, file_isrc=file_isrc,
        file_duration_s=round(length) if length else None,
    )

    isrc_equal = bool(expected_isrc and file_isrc == expected_isrc)
    if expected_isrc and file_isrc and not isrc_equal:
        evidence[ISRC_NOTE] = f"ISRC differs: file {file_isrc}, expected {expected_isrc}"
    if title and artist:
        if version_mismatch(row["name"], title, row["artists"]):
            reasons["version_mismatch"] = f"tags say '{title}'"
        elif not isrc_equal:
            score, scored = score_track_match(title, artist, row["name"], row["artists"])
            if score < config.MIN_MATCH_SCORE:
                reasons["title_artist_mismatch"] = f"tags '{title}' by '{artist}' (score {score:.2f}, {scored})"
    else:
        name = os.path.join(os.path.basename(os.path.dirname(path)), os.path.basename(path))
        _, why = filename_match(name, row["name"], row["artists"])
        if why:
            key = "version_mismatch" if why.startswith("version mismatch") else "title_artist_mismatch"
            reasons[key] = f"no tags; filename: {why}"

    if config.MAX_DURATION_DIFF_SECONDS > 0 and row.get("duration_ms") and length:
        expected = row["duration_ms"] / 1000
        longer_ok = version_tier(os.path.basename(path)) < version_tier(row["name"])
        if _duration_off(length - expected, longer_ok=longer_ok):
            reasons["duration_off"] = f"{fmt_duration(length)} vs {fmt_duration(expected)}"

    if config.LOSSLESS_CHECK_ENABLED and os.path.splitext(path)[1].lower() in CHECKABLE_EXTS:
        verdict: str
        cutoff: int | None
        drop: float | None
        if (
            previous
            and previous["audio_check"]
            and previous["evidence"].get("lossless_rules") == rules_key()
            and (previous["path"], previous["size"], previous["mtime"]) == (path, size, mtime)
        ):
            verdict = previous["audio_check"]
            cutoff, drop = previous["audio_cutoff_hz"], previous["audio_drop_db"]
        else:
            audio = check_file(path)
            verdict, cutoff, drop = audio.verdict, audio.cutoff_hz, audio.drop_db
        result.update(audio_check=verdict, audio_cutoff_hz=cutoff, audio_drop_db=drop)
        if cutoff is not None:
            detail = f"{verdict}: steepest drop {drop:.0f} dB at {cutoff / 1000:.1f} kHz"
        else:
            detail = verdict
        if verdict in REJECT_VERDICTS:
            reasons["fake_lossless"] = detail
        elif verdict == SUSPECT:
            reasons["suspect_lossless"] = detail

    result["problems"] = [p for p in PROBLEMS if p in reasons]
    return result


def scan_step(
    conn: sqlite3.Connection,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    heartbeat: Callable[[], None] = lambda: None,
    max_files: int = MAX_FILES_PER_STEP,
    max_seconds: float = MAX_SECONDS_PER_STEP,
) -> tuple[int, bool]:
    """Check up to `max_files` stale files (or `max_seconds`). Returns
    (files_checked, finished); finished means every file is current."""
    started = time.monotonic()
    checked = 0
    for pl in list_playlists(conn, only_enabled=True):
        table = pl["table_name"]
        for row in list_tracks(conn, table):
            if not row["downloaded"]:
                continue
            previous = get_file_check(conn, table, row["id"])
            if is_current(previous, row):
                continue
            if (
                should_stop()
                or checked >= max_files
                or time.monotonic() - started >= max_seconds
            ):
                return checked, False
            heartbeat()
            try:
                save_file_check(conn, table, row["id"], **verify_file(row, previous))
            except Exception:
                logging.exception(f"[health] Could not check {row['path']}")
                continue
            checked += 1
    return checked, True


def _free_path(directory: str, basename: str) -> str:
    """`directory/basename`, or "name (2).ext" ... if that is taken."""
    stem, ext = os.path.splitext(basename)
    candidate, n = os.path.join(directory, basename), 1
    while os.path.lexists(candidate):
        n += 1
        candidate = os.path.join(directory, f"{stem} ({n}){ext}")
    return candidate


def _inside(path: str, root: str) -> bool:
    try:
        real, real_root = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([real, real_root]) == real_root
    except ValueError:  # different drives / relative vs absolute
        return False


def _skip_quarantine(conn, table_name, track_id, path, reason) -> bool:
    logging.warning(f"[quarantine] Skipped {track_id} ({path}): {reason}")
    record_history(
        conn, "quarantine_skipped", table_name=table_name, track_id=track_id,
        path=path, reason=reason,
    )
    return False


def quarantine_track(
    conn: sqlite3.Connection, table_name: str, track_id: str, expected_path: str | None
) -> bool:
    """Move a library file to <playlists_root>/.quarantine/<table>/ (never
    delete it) and put the row back to pending so the normal cycle
    downloads it again. The file's name goes to the track's _tried list so
    the same peer file is not simply fetched again. A missing file only
    resets the row. Daemon thread only. Returns False when nothing was done.

    `expected_path` is the file the operator saw flagged. If the row points
    elsewhere by now (re-downloaded, upgraded), or the file is outside the
    playlists root, nothing happens and history records quarantine_skipped.

    To purge: inspect and delete <playlists_root>/.quarantine by hand.
    """
    row = get_track(conn, table_name, track_id)
    if not row or not row["downloaded"]:
        return False
    path = row["path"]
    if path != expected_path:
        return _skip_quarantine(
            conn, table_name, track_id, path,
            f"file changed since it was flagged (was {expected_path})",
        )
    root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    check = get_file_check(conn, table_name, track_id) or {}
    dest = None
    if path and os.path.isfile(path):
        if not _inside(path, root):
            return _skip_quarantine(
                conn, table_name, track_id, path, "file is outside the playlists folder"
            )
        qdir = os.path.join(root, QUARANTINE_DIR, table_name)
        try:
            os.makedirs(qdir, exist_ok=True)
            dest = _free_path(qdir, os.path.basename(path))
            shutil.move(path, dest)
        except OSError as e:
            logging.error(f"[quarantine] Could not move {path}: {e}; row left as is")
            return False
        add_tried_file(conn, table_name, track_id, os.path.basename(path))
    reset_track_for_redownload(conn, table_name, track_id)
    delete_file_check(conn, table_name, track_id)
    record_history(
        conn, "quarantined", table_name=table_name, track_id=track_id,
        path=path, quarantine_path=dest,
        problems=", ".join(check.get("problems") or []) or None,
    )
    logging.info(f"[quarantine] {path} -> {dest or '(file missing)'}; track back to pending")
    return True


REPLACED_SUBDIR = "replaced"


def retire_replaced_file(
    conn: sqlite3.Connection, table_name: str, track_id: str, old_path: str, reason: str
) -> str | None:
    """Move a library file that an upgrade or manual grab replaced to
    <playlists_root>/.quarantine/<table>/replaced/ (" (2)" on a collision).
    Never deletes: until a recycle bin exists, the operator purges by hand.
    A file outside the playlists root is left where it is. Daemon only.
    Returns where the file went, or None when it was not moved."""
    root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    if not _inside(old_path, root):
        logging.warning(f"[replace] {old_path} is outside {root}; left in place")
        return None
    qdir = os.path.join(root, QUARANTINE_DIR, table_name, REPLACED_SUBDIR)
    try:
        os.makedirs(qdir, exist_ok=True)
        dest = _free_path(qdir, os.path.basename(old_path))
        shutil.move(old_path, dest)
    except OSError as e:
        logging.warning(f"[replace] Could not move replaced file {old_path}: {e}")
        return None
    logging.info(f"[replace] {old_path} -> {dest} ({reason})")
    record_history(
        conn, "replaced_file", table_name=table_name, track_id=track_id,
        path=old_path, replaced_path=dest, reason=reason,
    )
    return dest


def backfill_qualities(
    conn: sqlite3.Connection,
    max_files: int = 500,
    heartbeat: Callable[[], None] = lambda: None,
) -> int:
    """Read the quality of up to `max_files` downloaded files that have
    none recorded (pre-v7 rows, reconciled files). Header reads only, so
    cheap; runs a bounded batch per cycle instead of a blocking migration.
    Returns how many rows were looked at (missing files included)."""
    done = 0
    for pl in list_playlists(conn, only_enabled=True):
        table = pl["table_name"]
        for track_id, path in tracks_missing_quality(conn, table, max_files - done):
            if done % 50 == 0:
                heartbeat()
            done += 1
            # A missing file (unmounted volume, moved by hand) is retried
            # next cycle; '' is only for a file mutagen cannot classify.
            if not os.path.isfile(path):
                continue
            set_track_quality(conn, table, track_id, quality_of_file(path))
        if done >= max_files:
            break
    return done
