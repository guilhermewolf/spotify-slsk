import sqlite3
import logging

def create_connection(db_file):
    try:
        conn = sqlite3.connect(db_file)
        logging.info(f"Connected to SQLite database: {db_file}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to SQLite: {e}")
        return None

def create_table(conn, table_name):
    try:
        sql_create_tracks_table = f"""CREATE TABLE IF NOT EXISTS {table_name} (
                                        id TEXT PRIMARY KEY,
                                        name TEXT NOT NULL,
                                        artists TEXT NOT NULL,
                                        album TEXT NOT NULL
                                    );"""
        cursor = conn.cursor()
        cursor.execute(sql_create_tracks_table)
        logging.info(f"Table {table_name} created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating table {table_name}: {e}")

def insert_track(conn, table_name, track):
    sql = f''' INSERT OR IGNORE INTO {table_name}(id, name, artists, album)
              VALUES(?,?,?,?) '''
    cursor = conn.cursor()
    try:
        cursor.execute(sql, track)
        conn.commit()
        logging.info(f"Inserted track into {table_name}: {track[1]} by {track[2]}")
    except sqlite3.Error as e:
        logging.error(f"Error inserting track into {table_name}: {e}")

def fetch_all_tracks(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, name, artists, album FROM {table_name}")
    return cursor.fetchall()
