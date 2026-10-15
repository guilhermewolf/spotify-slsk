"""cycle_history: one row per completed daemon cycle."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC
from typing import Any


def record_cycle(
    conn: sqlite3.Connection,
    *,
    started_at: float,
    duration_seconds: float,
    playlists_synced: int,
    tracks_added: int,
    tracks_downloaded: int,
    tracks_failed: int,
    keep_last: int = 200,
) -> None:
    """Persist one cycle's stats and prune anything older than ``keep_last``.

    Capping in the same transaction keeps the table from growing without
    bound on long-running deployments.
    """
    try:
        from datetime import datetime
        ts = datetime.fromtimestamp(started_at, tz=UTC).isoformat()
        with conn:
            conn.execute(
                "INSERT INTO cycle_history "
                "(started_at, duration_seconds, playlists_synced, "
                "tracks_added, tracks_downloaded, tracks_failed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    duration_seconds,
                    playlists_synced,
                    tracks_added,
                    tracks_downloaded,
                    tracks_failed,
                ),
            )
            conn.execute(
                "DELETE FROM cycle_history WHERE id NOT IN ("
                "SELECT id FROM cycle_history ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
    except sqlite3.Error as e:
        logging.error(f"Error recording cycle history: {e}")


def list_cycles(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent cycles, newest first."""
    try:
        rows = conn.execute(
            "SELECT started_at, duration_seconds, playlists_synced, "
            "tracks_added, tracks_downloaded, tracks_failed "
            "FROM cycle_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "started_at": r[0],
                "duration_seconds": r[1],
                "playlists_synced": r[2],
                "tracks_added": r[3],
                "tracks_downloaded": r[4],
                "tracks_failed": r[5],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logging.warning(f"Could not list cycle history: {e}")
        return []
