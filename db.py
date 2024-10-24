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
                                        suspended_until TEXT
                                    );"""
        cursor = conn.cursor()
        cursor.execute(sql_create_tracks_table)
        logging.info(f"Table {table_name} created or already exists.")
        
        # Optionally, create an index on the 'id' column for faster lookups
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

def update_download_status(conn, track_id, playlist_name):
    table_name = playlist_name
    sql = f"UPDATE {table_name} SET downloaded = 1 WHERE id = ?"
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (track_id,))
        conn.commit()
        logging.info(f"Updated track {track_id} as downloaded in {table_name}.")
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error updating download status for {track_id} in {table_name}: {e}")
