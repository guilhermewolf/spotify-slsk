import sqlite3
import logging
import json
from models import Track

SCHEMA_VERSION = 1


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
        if current == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            logging.info(f"Initialized schema user_version to {SCHEMA_VERSION}")
        elif current != SCHEMA_VERSION:
            logging.info(
                f"DB user_version is {current}; code expects {SCHEMA_VERSION}"
            )
    except sqlite3.Error as e:
        logging.warning(f"Failed to read/set schema user_version: {e}")


def create_connection(db_file):
    try:
        conn = sqlite3.connect(db_file, timeout=30)
        _apply_pragmas(conn)
        _ensure_schema_version(conn)
        logging.info(f"Connected to SQLite database: {db_file}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to SQLite: {e}")
        return None

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
