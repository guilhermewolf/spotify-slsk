"""When each recycle-bin file was retired (.quarantine/<table>/replaced/).

The purge (library.health.purge_recycle_bin) counts retention from here, not
from the file's mtime: a moved file keeps its download-time mtime, and
stamping it can fail on some shares.
"""
from __future__ import annotations

import logging
import sqlite3


def record_retired(conn: sqlite3.Connection, path: str, retired_at: float) -> bool:
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO recycle_bin (path, retired_at) VALUES (?, ?)",
                (path, retired_at),
            )
        return True
    except sqlite3.Error as e:
        logging.error(f"Could not record retirement of {path}: {e}")
        return False


def retired_times(conn: sqlite3.Connection) -> dict[str, float]:
    try:
        return dict(conn.execute("SELECT path, retired_at FROM recycle_bin").fetchall())
    except sqlite3.Error as e:
        logging.error(f"Could not read the recycle bin: {e}")
        return {}


def forget_retired(conn: sqlite3.Connection, paths: list[str]) -> None:
    try:
        with conn:
            conn.executemany("DELETE FROM recycle_bin WHERE path = ?", [(p,) for p in paths])
    except sqlite3.Error as e:
        logging.error(f"Could not prune the recycle bin table: {e}")
