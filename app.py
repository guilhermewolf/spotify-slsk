import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os
import logging
import re
import subprocess
from db import create_connection, create_table, insert_track, fetch_all_tracks, update_download_status
from log_config import setup_logging
from timing_utils import sleep_interval
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC

def get_playlist_id(playlist_url):
    try:
        if "playlist/" in playlist_url:
            return playlist_url.split("playlist/")[1].split("?")[0]
        else:
            raise ValueError(f"Invalid playlist URL: {playlist_url}")
    except IndexError:
        logging.error(f"Failed to extract playlist ID from URL: {playlist_url}")
        return None

def sanitize_table_name(name):
    return re.sub(r'\W+', '_', name)

def download_track(track_name, artist_name, client_id, client_secret, sldl_user, sldl_pass, download_path):
    os.makedirs(download_path, exist_ok=True)

    search_query = f"{artist_name} - {track_name}"
    command = [
        "sldl", search_query,
        "--username", sldl_user,
        "--password", sldl_pass,
        "--format", "mp3",
        "--min-bitrate", "320",
        "--path", download_path,
        "--skip-existing",
    ]

    try:
        subprocess.run(command, check=True)
        logging.info(f"Attempted download for track: {search_query} into folder: {download_path}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to download track: {search_query} with error: {e}")

def fetch_and_compare_tracks(conn, playlist_id, table_name, sp):
    results = sp.playlist_tracks(playlist_id)
    current_track_ids = set()
    db_tracks = {track[0]: track for track in fetch_all_tracks(conn, table_name)}

    new_tracks = []

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
            new_tracks.append(track['id'])  # Collect new tracks to trigger download later

    return new_tracks

def process_downloaded_tracks(playlist_name, conn):
    download_path = f"/app/data/downloads/"
    logging.info(f"Checking for downloaded tracks in {download_path}")

    for root, dirs, files in os.walk(download_path):
        for file in files:
            if file.endswith(".mp3"):
                file_path = os.path.join(root, file)
                logging.info(f"Processing file: {file_path}")

                title, artist, album = extract_metadata_from_file(file_path)
                if title and artist:
                    logging.info(f"Extracted Metadata - Track: {title}, Artist: {artist}, Album: {album}")

                    track_id = conn.execute(f"SELECT id FROM {playlist_name} WHERE name = ? AND artists = ?",
                                            (title, artist)).fetchone()

                    if track_id:
                        update_download_status(conn, track_id[0], playlist_name, success=True)
                    else:
                        logging.warning(f"Could not find track {title} by {artist} in the database.")
                else:
                    logging.warning(f"Metadata incomplete or missing for file: {file_path}")

def update_download_status(conn, track_id, table_name, success=False):
    cursor = conn.cursor()
    if success:
        sql = f"UPDATE {table_name} SET downloaded = 1, attempts = 0, suspended_until = NULL WHERE id = ?"
    else:
        sql = f"UPDATE {table_name} SET attempts = attempts + 1, last_attempt = CURRENT_TIMESTAMP WHERE id = ?"
        cursor.execute(f"SELECT attempts FROM {table_name} WHERE id = ?", (track_id,))
        attempts = cursor.fetchone()[0]
        if attempts >= 3:
            sql = f"UPDATE {table_name} SET suspended_until = datetime('now', '+2 days') WHERE id = ?"

    try:
        cursor.execute(sql, (track_id,))
        conn.commit()
        logging.info(f"Updated track {track_id} status in {table_name}.")
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error updating track status for {track_id} in {table_name}: {e}")

def retry_suspended_downloads(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, name, artists FROM {table_name} WHERE downloaded = 0 AND (suspended_until IS NULL OR suspended_until < datetime('now'))")
    tracks_to_retry = cursor.fetchall()
    return tracks_to_retry

def extract_metadata_from_file(file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        title = audio.get("TIT2").text[0] if audio.get("TIT2") else None
        artist = audio.get("TPE1").text[0] if audio.get("TPE1") else None
        album = audio.get("TALB").text[0] if audio.get("TALB") else None
        return title, artist, album
    except Exception as e:
        logging.error(f"Error reading metadata from {file_path}: {e}")
        return None, None, None
def setup_spotify_client():
    auth_manager = SpotifyClientCredentials()
    sp = spotipy.Spotify(auth_manager=auth_manager)
    return sp

def main():
    setup_logging()

    SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
    playlist_urls = os.getenv('SPOTIFY_PLAYLIST_URLS').split(',')

    sp = setup_spotify_client()

    database = "/app/data/playlist_tracks.db"
    conn = create_connection(database)

    if conn:
        for playlist_url in playlist_urls:
            playlist_id = get_playlist_id(playlist_url)
            if not playlist_id:
                continue

            playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
            create_table(conn, playlist_name)

            new_tracks = fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)

            download_path = f"/app/data/downloads/{playlist_name}"

            for track in new_tracks:
                track_name, artist_name = track[1], track[2]
                download_track(track_name, artist_name, SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, os.getenv('SLDL_USER'), os.getenv('SLDL_PASS'), download_path)

            process_downloaded_tracks(playlist_name, conn)

            # Retry suspended downloads
            suspended_tracks = retry_suspended_downloads(conn, playlist_name)
            for track in suspended_tracks:
                download_track(track[1], track[2], SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, os.getenv('SLDL_USER'), os.getenv('SLDL_PASS'), download_path)
                process_downloaded_tracks(playlist_name, conn)

        while True:
            for playlist_url in playlist_urls:
                playlist_id = get_playlist_id(playlist_url)
                if not playlist_id:
                    continue

                playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
                new_tracks = fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)

                download_path = f"/app/data/downloads/{playlist_name}"

                for track in new_tracks:
                    track_name, artist_name = track[1], track[2]
                    download_track(track_name, artist_name, SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, os.getenv('SLDL_USER'), os.getenv('SLDL_PASS'), download_path)

                process_downloaded_tracks(playlist_name, conn)

                # Retry suspended downloads
                suspended_tracks = retry_suspended_downloads(conn, playlist_name)
                for track in suspended_tracks:
                    download_track(track[1], track[2], SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, os.getenv('SLDL_USER'), os.getenv('SLDL_PASS'), download_path)
                    process_downloaded_tracks(playlist_name, conn)
                    
            sleep_interval(5)
    else:
        logging.error("Failed to connect to the SQLite database.")

if __name__ == "__main__":
    main()