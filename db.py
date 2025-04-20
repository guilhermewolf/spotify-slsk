import sqlite3
import logging
import json
from utils import sanitize_table_name
from models import Track

def create_connection(db_file):
    try:
        conn = sqlite3.connect(db_file)
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
    cursor = conn.cursor()
    try:
        cursor.execute(sql, track)
        conn.commit()
        logging.info(f"Inserted track into {table_name}: {track[1]} by {track[2]}")
    except sqlite3.Error as e:
        conn.rollback()
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

def update_download_status(conn, track_id, table_name, success=False, file_path=None):
    cursor = conn.cursor()
    if success:
        sql = f'UPDATE "{table_name}" SET downloaded = 1, attempts = 0, suspended_until = NULL, path = ?, last_attempt = CURRENT_TIMESTAMP WHERE id = ?'
        params = (file_path, track_id)
        logging.info(f"Updating status of track ID: {track_id} to downloaded with path: {file_path}")
    else:
        cursor.execute(f'SELECT attempts FROM "{table_name}" WHERE id = ?', (track_id,))
        row = cursor.fetchone()
        attempts = row[0] if row else 0
        params = (track_id,)
        sql = f'UPDATE "{table_name}" SET attempts = attempts + 1, last_attempt = CURRENT_TIMESTAMP WHERE id = ?'
        logging.info(f"Incrementing attempt count for track ID: {track_id}")
        
        if attempts >= 2:
            suspend_sql = f'UPDATE "{table_name}" SET suspended_until = datetime("now", "+2 days") WHERE id = ?'
            cursor.execute(suspend_sql, (track_id,))
            logging.info(f"Track ID: {track_id} has reached max attempts, suspending for 2 days")

    try:
        cursor.execute(sql, params)
        conn.commit()
        logging.info(f"Updated track {track_id} status in {table_name}.")
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error updating track status for {track_id} in {table_name}: {e}")

def create_tried_table(conn, playlist_name):
    tried_table_name = f"{playlist_name}_tried"
    try:
        sql_create_tried_table = f"""
        CREATE TABLE IF NOT EXISTS "{tried_table_name}" (
            track_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            PRIMARY KEY (track_id, file_path)
        );
        """
        cursor = conn.cursor()
        cursor.execute(sql_create_tried_table)
        conn.commit()
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
    cursor = conn.cursor()
    try:
        cursor.execute(
            f'INSERT OR IGNORE INTO "{tried_table_name}" (track_id, file_path) VALUES (?, ?)',
            (track_id, file_path)
        )
        conn.commit()
        logging.info(f"✅ Added attempted file for {track_id}: {file_path}")
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"❌ Failed to insert into {tried_table_name} for {track_id}: {e}")

def clear_tried_entries(conn, playlist_name, track_id):
    cursor = conn.cursor()
    try:
        cursor.execute(
            f'UPDATE "{playlist_name}" SET tried_files = ? WHERE id = ?',
            (json.dumps([]), track_id)
        )
        conn.commit()
        logging.info(f"✅ Cleared tried entries for track {track_id} in {playlist_name}")
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"❌ Failed to clear tried entries for {track_id} in {playlist_name}: {e}")


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
