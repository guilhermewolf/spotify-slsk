"""Validation verdicts per library file (the `file_checks` table, schema v6).

One global table rather than columns on every pl_<name> table: the library
health page reads all of them in one query, and the scan bookkeeping
(size/mtime/rules_version/ignored) stays out of the track rows.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

_COLUMNS = (
    "table_name", "track_id", "path", "size", "mtime", "rules_version",
    "audio_check", "audio_cutoff_hz", "audio_drop_db", "problems",
    "evidence_json", "ignored", "checked_at",
)


def _row(r: tuple) -> dict[str, Any]:
    d = dict(zip(_COLUMNS, r, strict=True))
    d["problems"] = [p for p in (d["problems"] or "").split(",") if p]
    try:
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
    except ValueError:
        d["evidence"] = {}
    d["ignored"] = bool(d["ignored"])
    return d


def save_file_check(
    conn: sqlite3.Connection,
    table_name: str,
    track_id: str,
    *,
    path: str | None,
    size: int | None,
    mtime: float | None,
    rules_version: int,
    audio_check: str | None = None,
    audio_cutoff_hz: int | None = None,
    audio_drop_db: float | None = None,
    problems: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Insert or replace the check for one track. An "ignored" mark survives
    only while the file is the same one (path, size, mtime) it was set on."""
    try:
        with conn:
            prev = conn.execute(
                "SELECT path, size, mtime, ignored FROM file_checks "
                "WHERE table_name = ? AND track_id = ?",
                (table_name, track_id),
            ).fetchone()
            ignored = bool(prev and prev[3] and tuple(prev[:3]) == (path, size, mtime))
            conn.execute(
                "INSERT OR REPLACE INTO file_checks (table_name, track_id, path, size, "
                "mtime, rules_version, audio_check, audio_cutoff_hz, audio_drop_db, "
                "problems, evidence_json, ignored, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    table_name, track_id, path, size, mtime, rules_version,
                    audio_check, audio_cutoff_hz, audio_drop_db,
                    ",".join(problems or []),
                    json.dumps(evidence) if evidence else None,
                    1 if ignored else 0,
                ),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not save file check for {track_id} in {table_name}: {e}")


def get_file_check(
    conn: sqlite3.Connection, table_name: str, track_id: str
) -> dict[str, Any] | None:
    try:
        r = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM file_checks "
            "WHERE table_name = ? AND track_id = ?",
            (table_name, track_id),
        ).fetchone()
    except sqlite3.Error as e:
        logging.warning(f"Could not read file check for {track_id}: {e}")
        return None
    return _row(r) if r else None


def list_file_checks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every check, newest first."""
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM file_checks ORDER BY checked_at DESC"
        ).fetchall()
    except sqlite3.Error as e:
        logging.warning(f"Could not list file checks: {e}")
        return []
    return [_row(r) for r in rows]


def set_check_ignored(
    conn: sqlite3.Connection, table_name: str, track_id: str, ignored: bool = True
) -> bool:
    try:
        with conn:
            cur = conn.execute(
                "UPDATE file_checks SET ignored = ? WHERE table_name = ? AND track_id = ?",
                (1 if ignored else 0, table_name, track_id),
            )
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Could not update ignore mark for {track_id}: {e}")
        return False


def delete_file_check(conn: sqlite3.Connection, table_name: str, track_id: str) -> None:
    try:
        with conn:
            conn.execute(
                "DELETE FROM file_checks WHERE table_name = ? AND track_id = ?",
                (table_name, track_id),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not delete file check for {track_id}: {e}")
