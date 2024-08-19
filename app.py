from flask import Flask, render_template
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os
import logging
import re
import subprocess
from db import create_connection, create_table, insert_track, fetch_all_tracks
from log_config import setup_logging
from timing_utils import sleep_interval
from threading import Thread
import sqlite3

app = Flask(__name__)

def get_db_connection():
    conn = sqlite3.connect('/app/data/playlist_tracks.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    conn = get_db_connection()
    playlists = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    data = {}

    for playlist in playlists:
        table_name = playlist['name']
        tracks = conn.execute(f"SELECT * FROM {table_name};").fetchall()
        data[table_name] = tracks

    conn.close()
    return render_template('index.html', data=data)

def run_flask_app():
    app.run(host='0.0.0.0', port=5110)

def get_playlist_id(playlist_url):
    return playlist_url.split("playlist/")[1].split("?")[0]

def sanitize_table_name(name):
    return re.sub(r'\W+', '_', name)

def download_playlist(playlist_url, playlist_name, client_id, client_secret, sldl_user, sldl_pass):
    download_path = f"/app/data/downloads/"

    os.makedirs(download_path, exist_ok=True)

    command = [
        "sldl", playlist_url,
        "--username", sldl_user,
        "--password", sldl_pass,
        "--spotify-id", client_id,
        "--spotify-secret", client_secret,
        "--format", "mp3",
        "--min-bitrate", "320",
        "--path", download_path,
        "--skip-existing",
    ]

    try:
        subprocess.run(command, check=True)
        logging.info(f"Started download for playlist: {playlist_url} into folder: {download_path}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to start download for playlist: {playlist_url} with error: {e}")

def fetch_and_compare_tracks(conn, playlist_id, table_name, sp):
    results = sp.playlist_tracks(playlist_id)
    current_track_ids = set()
    db_tracks = {track[0]: track for track in fetch_all_tracks(conn, table_name)}

    for item in results['items']:
        track = item['track']
        current_track_ids.add(track['id'])

        if track['id'] not in db_tracks:
            track_data = (track['id'],
                          track['name'],
                          ', '.join([artist['name'] for artist in track['artists']]),
                          track['album']['name'])  # Only provide 4 values
            insert_track(conn, table_name, track_data)
            logging.info(f"New Song found in {table_name}: {track['name']} by {', '.join([artist['name'] for artist in track['artists']])} from the album {track['album']['name']}")
            playlist_url = sp.playlist(playlist_id)['external_urls']['spotify']
            download_playlist(playlist_url, table_name, os.getenv('SPOTIPY_CLIENT_ID'), os.getenv('SPOTIPY_CLIENT_SECRET'), os.getenv('SLDL_USER'), os.getenv('SLDL_PASS'))
            process_downloaded_tracks(table_name)

def process_downloaded_tracks(playlist_name):
    download_path = f"/app/data/downloads/{playlist_name}"
    logging.info(f"Checking for downloaded tracks in {download_path}")

    for root, dirs, files in os.walk(download_path):
        for file in files:
            if file.endswith(".mp3"):
                file_path = os.path.join(root, file)
                logging.info(f"Processing file: {file_path}")

                # Verify file and mark as downloaded in the database
                conn = create_connection("/app/data/playlist_tracks.db")
                try:
                    track_name, artist_name = extract_basic_metadata(file)
                    logging.info(f"Extracted Metadata - Track: {track_name}, Artist: {artist_name}")

                    track_id = conn.execute(f"SELECT id FROM {playlist_name} WHERE name = ? AND artists = ?",
                                            (track_name, artist_name)).fetchone()
                    
                    if track_id:
                        update_download_status(conn, file_path, track_id[0], playlist_name)
                    else:
                        logging.warning(f"Could not find track {track_name} by {artist_name} in the database.")
                except Exception as e:
                    logging.error(f"Error processing file {file}: {e}")
                finally:
                    conn.close()


def update_download_status(conn, file_path, track_id, table_name):
    if os.path.exists(file_path) and os.path.isfile(file_path):
        try:
            sql = f"UPDATE {table_name} SET downloaded = 1 WHERE id = ?"
            cursor = conn.cursor()
            cursor.execute(sql, (track_id,))
            conn.commit()
            logging.info(f"Marked as downloaded in {table_name} for Track ID: {track_id}")
        except Exception as e:
            logging.error(f"Failed to update download status for {file_path}: {e}")
    else:
        logging.warning(f"File not found or not complete: {file_path}")

def extract_basic_metadata(filename):
    parts = filename.rsplit("-", 1)
    if len(parts) == 2:
        artist = parts[0].strip()
        track = parts[1].replace(".mp3", "").strip()
        return track, artist
    return None, None

def main():
    setup_logging()

    # Start the Flask web server in a separate thread
    flask_thread = Thread(target=run_flask_app)
    flask_thread.start()

    SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
    playlist_urls = os.getenv('SPOTIFY_PLAYLIST_URLS').split(',')

    auth_manager = SpotifyClientCredentials()
    sp = spotipy.Spotify(auth_manager=auth_manager)

    database = "/app/data/playlist_tracks.db"
    conn = create_connection(database)

    if conn:
        for playlist_url in playlist_urls:
            playlist_id = get_playlist_id(playlist_url)
            playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
            create_table(conn, playlist_name)
            fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)
        while True:
            for playlist_url in playlist_urls:
                playlist_id = get_playlist_id(playlist_url)
                playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
                fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)
            sleep_interval(5)
    else:
        logging.error("Failed to connect to the SQLite database.")

if __name__ == "__main__":
    main()
