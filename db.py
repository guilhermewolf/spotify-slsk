import os
import sqlite3
import logging
import json
from models import Track

SCHEMA_VERSION = 3


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply reliability-oriented pragmas to a fresh connection.

    WAL avoids writers blocking readers; busy_timeout lets SQLite wait for a
    lock instead of raising OperationalError immediately.
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error as e:
        logging.warning(f"Failed to apply sqlite pragmas: {e}")


def _ensure_schema_version(conn: sqlite3.Connection) -> None:
    """Bootstrap PRAGMA user_version so future migrations can diff cleanly."""
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            if current == 0:
                logging.info(f"Initialized schema user_version to {SCHEMA_VERSION}")
            else:
                logging.info(
                    f"Migrated schema user_version {current} -> {SCHEMA_VERSION}"
                )
        elif current > SCHEMA_VERSION:
            logging.info(
                f"DB user_version is {current}; code expects {SCHEMA_VERSION}"
            )
    except sqlite3.Error as e:
        logging.warning(f"Failed to read/set schema user_version: {e}")


def _ensure_playlists_meta_table(conn: sqlite3.Connection) -> None:
    """Catalogue table keyed by Spotify playlist id — source of truth for
    which playlists the daemon syncs. `enabled` and `added_at` arrived in
    schema v3; the ALTER TABLE guards handle v2 DBs on upgrade.
    """
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS playlists_meta ("
                "playlist_id TEXT PRIMARY KEY, "
                "table_name TEXT NOT NULL, "
                "name TEXT NOT NULL, "
                "snapshot_id TEXT, "
                "last_synced TIMESTAMP, "
                "enabled INTEGER NOT NULL DEFAULT 1, "
                "added_at TIMESTAMP)"
            )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(playlists_meta)")}
        with conn:
            if "enabled" not in cols:
                conn.execute(
                    "ALTER TABLE playlists_meta ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "added_at" not in cols:
                conn.execute("ALTER TABLE playlists_meta ADD COLUMN added_at TIMESTAMP")
    except sqlite3.Error as e:
        logging.error(f"Error ensuring playlists_meta: {e}")


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    """Key-value bag for UI-editable tunables. DB value overrides env default."""
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, "
                "value TEXT, "
                "updated_at TIMESTAMP)"
            )
    except sqlite3.Error as e:
        logging.error(f"Error ensuring settings: {e}")


def create_connection(db_file):
    try:
        conn = sqlite3.connect(db_file, timeout=30)
        _apply_pragmas(conn)
        _ensure_playlists_meta_table(conn)
        _ensure_settings_table(conn)
        _ensure_schema_version(conn)
        logging.info(f"Connected to SQLite database: {db_file}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to SQLite: {e}")
        return None


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


def get_setting(conn, key: str, default=None):
    """Look up a setting: DB value wins, else env var by the same key,
    else `default`. Always returns a str (or None if no value anywhere).
    """
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row[0] is not None:
            return row[0]
    except sqlite3.Error as e:
        logging.warning(f"Could not read setting {key}: {e}")
    env_value = os.getenv(key)
    if env_value is not None:
        return env_value
    return default


def set_setting(conn, key: str, value) -> None:
    """Write a DB override for a setting. Pass None to delete the override."""
    if value is None:
        delete_setting(conn, key)
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, str(value)),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not write setting {key}: {e}")


def delete_setting(conn, key: str) -> None:
    """Drop the DB override so env/code default takes over again."""
    try:
        with conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    except sqlite3.Error as e:
        logging.error(f"Could not delete setting {key}: {e}")


def list_settings(conn) -> dict:
    """Return all DB-stored settings as a plain dict."""
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows}
    except sqlite3.Error as e:
        logging.warning(f"Could not list settings: {e}")
        return {}

def create_table(conn, playlist_name):
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
                                        tried_files TEXT DEFAULT ''
                                    );"""
        cursor = conn.cursor()
        cursor.execute(sql_create_tracks_table)
        logging.info(f"Table {table_name} created or already exists.")
        create_tried_table(conn, playlist_name)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_id ON {table_name} (id);")
        logging.info(f"Index on {table_name}(id) created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating table {table_name}: {e}")

def insert_track(conn, playlist_name, track):
    table_name = playlist_name
    sql = f'''INSERT OR IGNORE INTO {table_name}(id, name, artists, album) VALUES (?, ?, ?, ?)'''
    try:
        with conn:
            conn.execute(sql, track)
        logging.info(f"Inserted track into {table_name}: {track[1]} by {track[2]}")
    except sqlite3.Error as e:
        logging.error(f"Error inserting track into {table_name}: {e}")

def fetch_all_tracks(conn, playlist_name):
    table_name = playlist_name
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT id, name, artists, album FROM {table_name}")
        return cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(f"Error fetching tracks from {table_name}: {e}")
        return []

MAX_ATTEMPTS_BEFORE_SUSPEND = 2


def update_download_status(conn, track_id, table_name, success=False, file_path=None):
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

def create_tried_table(conn, playlist_name):
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

def get_tried_files(conn, table_name, track_id):
    tried_table_name = f"{table_name}_tried"
    cursor = conn.cursor()
    cursor.execute(
        f'SELECT file_path FROM "{tried_table_name}" WHERE track_id = ?',
        (track_id,)
    )
    rows = cursor.fetchall()
    return [row[0] for row in rows] if rows else []


def add_tried_file(conn, table_name, track_id, file_path):
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


def clear_tried_entries(conn, playlist_name, track_id):
    try:
        with conn:
            conn.execute(
                f'UPDATE "{playlist_name}" SET tried_files = ? WHERE id = ?',
                (json.dumps([]), track_id),
            )
        logging.info(f"Cleared tried entries for track {track_id} in {playlist_name}")
    except sqlite3.Error as e:
        logging.error(
            f"Failed to clear tried entries for {track_id} in {playlist_name}: {e}"
        )


def get_pending_tracks(conn, playlist_name: str) -> list:
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT id, name, artists, album
            FROM "{playlist_name}"
            WHERE downloaded = 0
              AND (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP)
        """)
        rows = cursor.fetchall()
        return [Track(row[0], row[1], row[2], row[3], playlist_name) for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Failed to fetch pending tracks from {playlist_name}: {e}")
        return []
