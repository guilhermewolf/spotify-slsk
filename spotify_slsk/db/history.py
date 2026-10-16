"""Append-only event history (grabs, imports, failures, upgrades)."""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

# ---- history --------------------------------------------------------------

HISTORY_EVENTS = (
    "grabbed",
    "imported",
    "download_failed",
    "import_rejected",
    "upgraded",
    "retried",
    "deleted_file",
)
FAILURE_EVENTS = ("download_failed", "import_rejected")


def record_history(
    conn: sqlite3.Connection | None,
    event: str,
    *,
    playlist_id: str | None = None,
    table_name: str | None = None,
    track_id: str | None = None,
    **data: Any,
) -> None:
    """Append one history event. `table_name` resolves to its playlist_id
    when the id isn't given. None-valued data keys are dropped. Never
    raises — history is an audit aid, not something to fail a download on.
    """
    if conn is None:
        return
    try:
        if playlist_id is None and table_name:
            row = conn.execute(
                "SELECT playlist_id FROM playlists_meta WHERE table_name = ? LIMIT 1",
                (table_name,),
            ).fetchone()
            playlist_id = row[0] if row else None
        payload = {k: v for k, v in data.items() if v is not None}
        with conn:
            conn.execute(
                "INSERT INTO history (event, playlist_id, track_id, data_json) "
                "VALUES (?, ?, ?, ?)",
                (event, playlist_id, track_id, json.dumps(payload) if payload else None),
            )
    except (sqlite3.Error, TypeError, ValueError) as e:
        logging.error(f"Could not record history event {event}: {e}")


def list_history(
    conn: sqlite3.Connection,
    *,
    event: str | None = None,
    playlist_id: str | None = None,
    track_id: str | None = None,
    before_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest first. Each row has `data` parsed from data_json."""
    sql = "SELECT id, ts, event, playlist_id, track_id, data_json FROM history"
    where: list[str] = []
    args: list[Any] = []
    for col, val in (("event", event), ("playlist_id", playlist_id), ("track_id", track_id)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if before_id:
        where.append("id < ?")
        args.append(before_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        logging.warning(f"Could not list history: {e}")
        return []
    out = []
    for r in rows:
        try:
            data = json.loads(r[5]) if r[5] else {}
        except ValueError:
            data = {}
        out.append({
            "id": r[0], "ts": r[1], "event": r[2],
            "playlist_id": r[3], "track_id": r[4], "data": data,
        })
    return out


def latest_failures(conn: sqlite3.Connection) -> dict[str, str | None]:
    """{track_id: reason} from each track's most recent failure event."""
    try:
        rows = conn.execute(
            "SELECT track_id, data_json FROM history WHERE id IN ("
            "SELECT MAX(id) FROM history WHERE event IN (?, ?) "
            "AND track_id IS NOT NULL GROUP BY track_id)",
            FAILURE_EVENTS,
        ).fetchall()
    except sqlite3.Error as e:
        logging.warning(f"Could not read latest failures: {e}")
        return {}
    out: dict[str, str | None] = {}
    for tid, data_json in rows:
        try:
            out[tid] = (json.loads(data_json) if data_json else {}).get("reason")
        except ValueError:
            out[tid] = None
    return out


def prune_history(conn: sqlite3.Connection, retention_days: int) -> None:
    """Delete events older than `retention_days` (at least 1, whatever the
    env says: 0 or a negative value would wipe the whole log every cycle).
    Indexed on ts, so cheap."""
    retention_days = max(1, int(retention_days))
    try:
        with conn:
            conn.execute(
                "DELETE FROM history WHERE ts < datetime('now', ?)",
                (f"-{int(retention_days)} days",),
            )
    except sqlite3.Error as e:
        logging.warning(f"Could not prune history: {e}")
