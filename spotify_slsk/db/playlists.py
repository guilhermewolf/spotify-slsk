"""The playlists_meta catalogue: one row per Spotify playlist."""
import logging
import sqlite3




def get_playlist_meta(conn, playlist_id):
    """Return (snapshot_id, table_name, name, last_synced) or a 4-tuple of None."""
    try:
        row = conn.execute(
            "SELECT snapshot_id, table_name, name, last_synced "
            "FROM playlists_meta WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        return row if row else (None, None, None, None)
    except sqlite3.Error as e:
        logging.error(f"Error reading playlists_meta for {playlist_id}: {e}")
        return (None, None, None, None)


def upsert_playlist_meta(conn, playlist_id, table_name, name, snapshot_id):
    """Insert a playlist or update its syncable fields.

    `added_at` is set only on first insert (via COALESCE below); subsequent
    calls don't bump it so the dashboard can sort by insertion order.
    `enabled` is left alone on update — the UI toggles it explicitly.
    """
    try:
        with conn:
            conn.execute(
                "INSERT INTO playlists_meta "
                "(playlist_id, table_name, name, snapshot_id, last_synced, "
                "enabled, added_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP) "
                "ON CONFLICT(playlist_id) DO UPDATE SET "
                "table_name = excluded.table_name, "
                "name = excluded.name, "
                "snapshot_id = excluded.snapshot_id, "
                "last_synced = excluded.last_synced",
                (playlist_id, table_name, name, snapshot_id),
            )
    except sqlite3.Error as e:
        logging.error(f"Error upserting playlists_meta for {playlist_id}: {e}")


def rename_playlist_meta(conn, playlist_id, table_name, name) -> None:
    """Point an existing catalogue row at its real table/name without
    touching snapshot_id or last_synced."""
    try:
        with conn:
            conn.execute(
                "UPDATE playlists_meta SET table_name = ?, name = ? WHERE playlist_id = ?",
                (table_name, name, playlist_id),
            )
    except sqlite3.Error as e:
        logging.error(f"Error renaming playlists_meta for {playlist_id}: {e}")


def list_playlists(conn, only_enabled: bool = False):
    """Return a list of dicts describing every known playlist."""
    try:
        sql = (
            "SELECT playlist_id, name, table_name, snapshot_id, last_synced, "
            "enabled, added_at FROM playlists_meta"
        )
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY added_at DESC NULLS LAST, name COLLATE NOCASE"
        rows = conn.execute(sql).fetchall()
        return [
            {
                "playlist_id": r[0],
                "name": r[1],
                "table_name": r[2],
                "snapshot_id": r[3],
                "last_synced": r[4],
                "enabled": bool(r[5]) if r[5] is not None else True,
                "added_at": r[6],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logging.error(f"Could not list playlists: {e}")
        return []


def set_playlist_enabled(conn, playlist_id: str, enabled: bool) -> None:
    try:
        with conn:
            conn.execute(
                "UPDATE playlists_meta SET enabled = ? WHERE playlist_id = ?",
                (1 if enabled else 0, playlist_id),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not toggle playlist {playlist_id}: {e}")


def remove_playlist(conn, playlist_id: str) -> bool:
    """Drop the playlist from the catalogue AND drop its per-playlist tables.

    Returns True if a row was removed. Leaves files on disk — removing them
    is the caller's choice (via the UI dialog) to avoid accidental mass
    deletion.
    """
    try:
        row = conn.execute(
            "SELECT table_name FROM playlists_meta WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if not row:
            return False
        table_name = row[0]
        with conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}_tried"')
            conn.execute(
                "DELETE FROM playlists_meta WHERE playlist_id = ?",
                (playlist_id,),
            )
        logging.info(f"Removed playlist {playlist_id} (dropped table {table_name})")
        return True
    except sqlite3.Error as e:
        logging.error(f"Could not remove playlist {playlist_id}: {e}")
        return False
