"""Per-playlist track tables (pl_<name>) and their _tried companions."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..models import Track
from .connection import _add_track_columns


def playlist_stats(conn: sqlite3.Connection, table_name: str) -> dict[str, int]:
    """Return counts the dashboard needs for one playlist.

    Missing tables (deleted playlist still listed in an older in-flight page)
    yield zeros rather than an exception.
    """
    try:
        row = conn.execute(
            f'SELECT '
            f'  COUNT(*), '
            f'  COALESCE(SUM(CASE WHEN downloaded = 1 THEN 1 ELSE 0 END), 0), '
            # The four statuses partition the table exactly like
            # _track_status (and so the playlist page's filter chips):
            # pending = never failed yet; retrying = failed at least once but
            # not suspended. "pending" used to include the retrying rows, so
            # the dashboard and the playlist page disagreed.
            f'  COALESCE(SUM(CASE WHEN downloaded = 0 AND COALESCE(attempts, 0) = 0 AND '
            f'    (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP) '
            f'    THEN 1 ELSE 0 END), 0), '
            f'  COALESCE(SUM(CASE WHEN downloaded = 0 AND suspended_until IS NOT NULL '
            f'    AND suspended_until >= CURRENT_TIMESTAMP THEN 1 ELSE 0 END), 0), '
            f'  COALESCE(SUM(CASE WHEN downloaded = 0 AND attempts > 0 AND '
            f'    (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP) '
            f'    THEN 1 ELSE 0 END), 0) '
            f'FROM "{table_name}"'
        ).fetchone()
        return {
            "total": row[0],
            "downloaded": row[1],
            "pending": row[2],
            "suspended": row[3],
            "retrying": row[4],
        }
    except sqlite3.Error as e:
        logging.warning(f"Could not get stats for {table_name}: {e}")
        return {
            "total": 0,
            "downloaded": 0,
            "pending": 0,
            "suspended": 0,
            "retrying": 0,
        }


def _track_status(downloaded, attempts, suspended_until, now):
    if downloaded:
        return "downloaded"
    if suspended_until and suspended_until >= now:
        return "suspended"
    if attempts and attempts > 0:
        return "retrying"
    return "pending"


def list_tracks(conn: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    """Return one dict per track in the playlist, with a derived status."""
    try:
        rows = conn.execute(
            f'SELECT id, name, artists, album, downloaded, path, attempts, '
            f'last_attempt, suspended_until, CURRENT_TIMESTAMP, duration_ms, isrc '
            f'FROM "{table_name}" ORDER BY name COLLATE NOCASE'
        ).fetchall()
        result = []
        for r in rows:
            d = {
                "id": r[0],
                "name": r[1],
                "artists": r[2],
                "album": r[3],
                "downloaded": bool(r[4]),
                "path": r[5],
                "attempts": r[6] or 0,
                "last_attempt": r[7],
                "suspended_until": r[8],
                "duration_ms": r[10],
                "isrc": r[11],
            }
            d["status"] = _track_status(d["downloaded"], d["attempts"], r[8], r[9])
            result.append(d)
        return result
    except sqlite3.Error as e:
        logging.error(f"Could not list tracks for {table_name}: {e}")
        return []


def get_track(conn: sqlite3.Connection, table_name: str, track_id: str) -> dict[str, Any] | None:
    """Return one track dict or None."""
    try:
        r = conn.execute(
            f'SELECT id, name, artists, album, downloaded, path, attempts, '
            f'last_attempt, suspended_until, CURRENT_TIMESTAMP, duration_ms, isrc '
            f'FROM "{table_name}" WHERE id = ?',
            (track_id,),
        ).fetchone()
        if not r:
            return None
        d = {
            "id": r[0],
            "name": r[1],
            "artists": r[2],
            "album": r[3],
            "downloaded": bool(r[4]),
            "path": r[5],
            "attempts": r[6] or 0,
            "last_attempt": r[7],
            "suspended_until": r[8],
            "duration_ms": r[10],
            "isrc": r[11],
        }
        d["status"] = _track_status(d["downloaded"], d["attempts"], r[8], r[9])
        return d
    except sqlite3.Error as e:
        logging.error(f"Could not read track {track_id} from {table_name}: {e}")
        return None


def retry_track(conn: sqlite3.Connection, table_name: str, track_id: str) -> None:
    """Clear suspended_until + attempts and wipe the tried-files history so
    the track is picked up fresh on the next cycle."""
    try:
        with conn:
            conn.execute(
                f'UPDATE "{table_name}" SET suspended_until = NULL, attempts = 0 '
                f'WHERE id = ?',
                (track_id,),
            )
            conn.execute(
                f'DELETE FROM "{table_name}_tried" WHERE track_id = ?',
                (track_id,),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not retry track {track_id} in {table_name}: {e}")

def create_table(conn: sqlite3.Connection, playlist_name: str) -> None:
    """Create a table dynamically based on the sanitized playlist name"""
    table_name = playlist_name
    try:
        sql_create_tracks_table = f"""CREATE TABLE IF NOT EXISTS {table_name} (
                                        id TEXT PRIMARY KEY,
                                        name TEXT NOT NULL,
                                        artists TEXT NOT NULL,
                                        album TEXT NOT NULL,
                                        downloaded INTEGER DEFAULT 0,
                                        path TEXT,
                                        attempts INTEGER DEFAULT 0,
                                        last_attempt TIMESTAMP,
                                        suspended_until TIMESTAMP,
                                        last_upgrade_check TIMESTAMP,
                                        tried_files TEXT DEFAULT '',
                                        duration_ms INTEGER,
                                        isrc TEXT
                                    );"""
        cursor = conn.cursor()
        cursor.execute(sql_create_tracks_table)
        logging.info(f"Table {table_name} created or already exists.")
        # Migrate older per-playlist tables that pre-date the upgrade-pass column.
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
        if "last_upgrade_check" not in cols:
            with conn:
                conn.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN last_upgrade_check TIMESTAMP'
                )
            logging.info(f"Migrated {table_name}: added last_upgrade_check column")
        with conn:
            _add_track_columns(conn, table_name)
        create_tried_table(conn, playlist_name)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_id ON {table_name} (id);")
        logging.info(f"Index on {table_name}(id) created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating table {table_name}: {e}")

def insert_track(conn: sqlite3.Connection, playlist_name: str, track: tuple[Any, ...]) -> None:
    """Insert (id, name, artists, album[, duration_ms[, isrc]]); existing ids are kept."""
    table_name = playlist_name
    track = tuple(track) + (None,) * (6 - len(track))
    sql = f'''INSERT OR IGNORE INTO {table_name}(id, name, artists, album, duration_ms, isrc) VALUES (?, ?, ?, ?, ?, ?)'''
    try:
        with conn:
            conn.execute(sql, track)
        logging.info(f"Inserted track into {table_name}: {track[1]} by {track[2]}")
    except sqlite3.Error as e:
        logging.error(f"Error inserting track into {table_name}: {e}")

def backfill_durations(
    conn: sqlite3.Connection,
    table_name: str,
    durations: list[tuple[Any, ...]],
) -> None:
    """Fill duration_ms (v5) and isrc (v6) for rows that don't have them
    yet. `durations` holds (track_id, duration_ms[, isrc]); a value already
    stored is never overwritten."""
    rows = [(tuple(d) + (None,))[:3] for d in durations]
    try:
        with conn:
            conn.executemany(
                f'UPDATE "{table_name}" SET duration_ms = COALESCE(duration_ms, ?), '
                f'isrc = COALESCE(isrc, ?) '
                f'WHERE id = ? AND (duration_ms IS NULL OR isrc IS NULL)',
                [(ms, isrc, tid) for tid, ms, isrc in rows if ms or isrc],
            )
    except sqlite3.Error as e:
        logging.error(f"Error backfilling durations in {table_name}: {e}")


def fetch_all_tracks(conn: sqlite3.Connection, playlist_name: str) -> list[tuple[Any, ...]]:
    table_name = playlist_name
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT id, name, artists, album FROM {table_name}")
        return cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(f"Error fetching tracks from {table_name}: {e}")
        return []

MAX_ATTEMPTS_BEFORE_SUSPEND = 2


def update_download_status(
    conn: sqlite3.Connection,
    track_id: str | None,
    table_name: str,
    success: bool = False,
    file_path: str | None = None,
) -> None:
    try:
        with conn:
            cursor = conn.cursor()
            if success:
                cursor.execute(
                    f'UPDATE "{table_name}" SET downloaded = 1, attempts = 0, '
                    f'suspended_until = NULL, path = ?, last_attempt = CURRENT_TIMESTAMP '
                    f'WHERE id = ?',
                    (file_path, track_id),
                )
                logging.info(f"Marked track {track_id} downloaded: {file_path}")
            else:
                cursor.execute(
                    f'UPDATE "{table_name}" SET attempts = attempts + 1, '
                    f'last_attempt = CURRENT_TIMESTAMP WHERE id = ?',
                    (track_id,),
                )
                cursor.execute(
                    f'SELECT attempts FROM "{table_name}" WHERE id = ?',
                    (track_id,),
                )
                row = cursor.fetchone()
                attempts = row[0] if row else 0
                if attempts >= MAX_ATTEMPTS_BEFORE_SUSPEND:
                    cursor.execute(
                        f'UPDATE "{table_name}" SET suspended_until = datetime("now", "+2 days") '
                        f'WHERE id = ?',
                        (track_id,),
                    )
                    logging.info(
                        f"Track {track_id} hit {attempts} attempts, suspended for 2 days"
                    )
                else:
                    logging.info(f"Track {track_id} attempts now {attempts}")
    except sqlite3.Error as e:
        logging.error(f"Error updating track status for {track_id} in {table_name}: {e}")

def create_tried_table(conn: sqlite3.Connection, playlist_name: str) -> None:
    tried_table_name = f"{playlist_name}_tried"
    try:
        with conn:
            conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{tried_table_name}" ('
                f'track_id TEXT NOT NULL, '
                f'file_path TEXT NOT NULL, '
                f'PRIMARY KEY (track_id, file_path))'
            )
        logging.info(f"Tried table {tried_table_name} created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating tried table {tried_table_name}: {e}")

def get_tried_files(conn: sqlite3.Connection, table_name: str, track_id: str) -> list[str]:
    tried_table_name = f"{table_name}_tried"
    cursor = conn.cursor()
    cursor.execute(
        f'SELECT file_path FROM "{tried_table_name}" WHERE track_id = ?',
        (track_id,)
    )
    rows = cursor.fetchall()
    return [row[0] for row in rows] if rows else []


def add_tried_file(conn: sqlite3.Connection, table_name: str, track_id: str, file_path: str) -> None:
    tried_table_name = f"{table_name}_tried"
    try:
        with conn:
            conn.execute(
                f'INSERT OR IGNORE INTO "{tried_table_name}" (track_id, file_path) VALUES (?, ?)',
                (track_id, file_path),
            )
        logging.info(f"Added attempted file for {track_id}: {file_path}")
    except sqlite3.Error as e:
        logging.error(f"Failed to insert into {tried_table_name} for {track_id}: {e}")


def clear_tried_entries(conn: sqlite3.Connection, playlist_name: str, track_id: str) -> None:
    """Wipe the rejected-filename history for one track.

    The history lives in the `_tried` companion table — the same place
    get_tried_files reads and add_tried_file writes. This used to blank a
    `tried_files` column on the main table instead, which nothing reads, so
    every filename ever rejected for a track stayed blacklisted forever and
    the upgrade pass could never reconsider it.
    """
    try:
        with conn:
            conn.execute(
                f'DELETE FROM "{playlist_name}_tried" WHERE track_id = ?',
                (track_id,),
            )
        logging.info(f"Cleared tried entries for track {track_id} in {playlist_name}")
    except sqlite3.Error as e:
        logging.error(
            f"Failed to clear tried entries for {track_id} in {playlist_name}: {e}"
        )


def get_pending_tracks(conn: sqlite3.Connection, playlist_name: str) -> list[Track]:
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT id, name, artists, album, duration_ms, isrc
            FROM "{playlist_name}"
            WHERE downloaded = 0
              AND (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP)
        """)
        rows = cursor.fetchall()
        return [
            Track(row[0], row[1], row[2], row[3], playlist_name, duration_ms=row[4], isrc=row[5])
            for row in rows
        ]
    except sqlite3.Error as e:
        logging.error(f"Failed to fetch pending tracks from {playlist_name}: {e}")
        return []


def get_upgrade_candidates(
    conn: sqlite3.Connection, playlist_name: str, interval_hours: int
) -> list[tuple[Track, str]]:
    """Return downloaded tracks whose last upgrade-check is stale or absent.

    Throttling lives at the row level so a 200-track library doesn't fire
    200 slskd searches every cycle. Returns (Track, current_path) tuples.
    `interval_hours <= 0` disables the feature entirely.
    """
    if interval_hours <= 0:
        return []
    try:
        cursor = conn.cursor()
        cursor.execute(
            f'SELECT id, name, artists, album, path, duration_ms, isrc FROM "{playlist_name}" '
            f'WHERE downloaded = 1 '
            f"AND (last_upgrade_check IS NULL "
            f"     OR last_upgrade_check < datetime('now', ?))",
            (f"-{interval_hours} hours",),
        )
        rows = cursor.fetchall()
        return [
            (Track(r[0], r[1], r[2], r[3], playlist_name, duration_ms=r[5], isrc=r[6]), r[4])
            for r in rows
        ]
    except sqlite3.Error as e:
        logging.error(f"Failed to fetch upgrade candidates from {playlist_name}: {e}")
        return []


def mark_upgrade_checked(conn: sqlite3.Connection, playlist_name: str, track_id: str) -> None:
    """Stamp last_upgrade_check so the throttle window starts now.

    Called whether or not an upgrade was found — a fruitless check still
    consumed a search, so it counts toward the interval.
    """
    try:
        with conn:
            conn.execute(
                f'UPDATE "{playlist_name}" SET last_upgrade_check = CURRENT_TIMESTAMP '
                f'WHERE id = ?',
                (track_id,),
            )
    except sqlite3.Error as e:
        logging.error(
            f"Failed to update last_upgrade_check for {track_id} in {playlist_name}: {e}"
        )


def reset_track_for_redownload(conn: sqlite3.Connection, table_name: str, track_id: str) -> None:
    """Back to pending: downloaded=0, no path, no attempts, no pause. Used
    after a library file is quarantined so the normal cycle fetches it again."""
    try:
        with conn:
            conn.execute(
                f'UPDATE "{table_name}" SET downloaded = 0, path = NULL, attempts = 0, '
                f'suspended_until = NULL WHERE id = ?',
                (track_id,),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not reset track {track_id} in {table_name}: {e}")


def mark_upgrade_due(conn: sqlite3.Connection, table_name: str, track_id: str) -> None:
    """Make the track eligible for the next upgrade pass (clears its throttle)."""
    try:
        with conn:
            conn.execute(
                f'UPDATE "{table_name}" SET last_upgrade_check = NULL WHERE id = ?',
                (track_id,),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not mark {track_id} in {table_name} for upgrade: {e}")
