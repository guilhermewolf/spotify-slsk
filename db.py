import sqlite3
import logging
from utils import sanitize_table_name

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
                                        suspended_until TIMESTAMP
                                    );"""
        cursor = conn.cursor()
        cursor.execute(sql_create_tracks_table)
        logging.info(f"Table {table_name} created or already exists.")
        
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_id ON {table_name} (id);")
        logging.info(f"Index on {table_name}(id) created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating table {table_name}: {e}")

def insert_track(conn, playlist_name, track):
    table_name = playlist_name
    sql = f''' INSERT OR IGNORE INTO {table_name}(id, name, artists, album) VALUES(?,?,?,?) '''
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
        # Increment attempts and update last_attempt
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
