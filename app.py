import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os
import logging
import re
import subprocess
import difflib
import random
import requests
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

    search_query = f'title="{track_name}",artist="{artist_name}"'
    
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
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info(f"Attempted download for track: {artist_name} - {track_name} into folder: {download_path}")
        logging.debug(f"Download command output: {result.stdout}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to download track: {artist_name} - {track_name} with error: {e}")
        logging.debug(f"Download command stderr: {e.stderr}")
        logging.debug(f"Download command stdout: {e.stdout}")

def fetch_and_compare_tracks(conn, playlist_id, table_name, sp):
    logging.info(f"Fetching tracks for playlist ID: {playlist_id} into table: {table_name}")
    results = sp.playlist_tracks(playlist_id)
    logging.info(f"Fetched {len(results['items'])} tracks from Spotify for playlist {table_name}")

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

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks

def find_closest_match(conn, playlist_name, title, artist):
    logging.info(f"Finding closest match for track: {title} by {artist} in playlist {playlist_name}")
    potential_matches = conn.execute(f"SELECT id, name, artists FROM {playlist_name}").fetchall()
    
    best_match = None
    best_score = 0.0
    
    for match in potential_matches:
        db_title, db_artist = match[1], match[2]
        title_similarity = difflib.SequenceMatcher(None, title.lower(), db_title.lower()).ratio()
        artist_similarity = difflib.SequenceMatcher(None, artist.lower(), db_artist.lower()).ratio()
        
        overall_similarity = (title_similarity + artist_similarity) / 2
        
        if overall_similarity > best_score:
            best_score = overall_similarity
            best_match = match
    
    if best_match:
        logging.info(f"Best match found: {best_match[1]} by {best_match[2]} with similarity score: {best_score}")
    else:
        logging.warning(f"No suitable match found for track: {title} by {artist}")

    return best_match, best_score

def process_downloaded_tracks(playlist_name, conn):
    download_path = f"/app/data/downloads/{playlist_name}"
    logging.info(f"Checking for downloaded tracks in {download_path}")

    all_downloaded = True

    for root, dirs, files in os.walk(download_path):
        for file in files:
            if file.endswith(".mp3"):
                file_path = os.path.join(root, file)
                logging.info(f"Processing file: {file_path}")

                title, artist, album = extract_metadata_from_file(file_path)
                if title and artist:
                    logging.info(f"Extracted Metadata - Track: {title}, Artist: {artist}, Album: {album}")

                    match, match_score = find_closest_match(conn, playlist_name, title, artist)

                    if match and match_score >= 0.6:
                        logging.info(f"Match found with similarity {match_score:.2f}. Updating database and marking as downloaded.")
                        update_download_status(conn, match[0], playlist_name, success=True, file_path=file_path)
                    else:
                        logging.warning(f"Could not find track {title} by {artist} in the database.")
                        all_downloaded = False
                else:
                    logging.warning(f"Metadata incomplete or missing for file: {file_path}")
                    all_downloaded = False
    if all_downloaded:
        notify_playlist_complete(playlist_name)

def update_download_status(conn, track_id, table_name, success=False, file_path=None):
    cursor = conn.cursor()
    if success:
        sql = f"UPDATE {table_name} SET downloaded = 1, attempts = 0, suspended_until = NULL, path = ? WHERE id = ?"
        params = (file_path, track_id)
        logging.info(f"Updating status of track ID: {track_id} to downloaded with path: {file_path}")
    else:
        sql = f"UPDATE {table_name} SET attempts = attempts + 1, last_attempt = CURRENT_TIMESTAMP WHERE id = ?"
        params = (track_id,)
        logging.info(f"Incrementing attempt count for track ID: {track_id}")
        cursor.execute(f"SELECT attempts FROM {table_name} WHERE id = ?", (track_id,))
        attempts = cursor.fetchone()[0]
        if attempts >= 3:
            sql = f"UPDATE {table_name} SET suspended_until = datetime('now', '+2 days') WHERE id = ?"
            logging.info(f"Track ID: {track_id} has reached max attempts, suspending for 2 days")

    try:
        cursor.execute(sql, params)
        conn.commit()
        logging.info(f"Updated track {track_id} status in {table_name}.")
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error updating track status for {track_id} in {table_name}: {e}")

def clean_up_untracked_files(conn, download_path, table_name):
    logging.info(f"Cleaning up untracked files in {download_path}")
    tracked_files = set()

    # Fetch paths from the database
    cursor = conn.cursor()
    cursor.execute(f"SELECT path FROM {table_name} WHERE downloaded = 1")
    db_files = {row[0] for row in cursor.fetchall() if row[0] is not None}

    # List files on the filesystem
    for root, dirs, files in os.walk(download_path):
        for file in files:
            if file.endswith(".mp3"):
                file_path = os.path.join(root, file)
                if file_path not in db_files:
                    os.remove(file_path)
                    logging.info(f"Deleted untracked file: {file_path}")
                else:
                    logging.info(f"Retaining tracked file: {file_path}")

def startup_check(conn, playlist_name):
    logging.info(f"Performing startup check for playlist: {playlist_name}")
    download_path = f"/app/data/downloads/{playlist_name}"
    
    # Process existing files on the filesystem
    logging.info(f"Starting to process existing downloaded tracks for playlist: {playlist_name}")
    process_downloaded_tracks(playlist_name, conn)

    # Clean up untracked files
    logging.info(f"Starting to clean up untracked files for playlist: {playlist_name}")
    clean_up_untracked_files(conn, download_path, playlist_name)

    logging.info(f"Startup check complete for playlist: {playlist_name}")

def retry_suspended_downloads(conn, table_name):
    logging.info(f"Retrying suspended downloads for table: {table_name}")
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, name, artists, attempts FROM {table_name} WHERE downloaded = 0 AND (suspended_until IS NULL OR suspended_until < datetime('now'))")
    tracks_to_retry = cursor.fetchall()

    for track in tracks_to_retry:
        track_id, name, artists, attempts = track
        backoff_time = random.uniform(1, 1.5) * (2 ** attempts)  # Random factor added
        logging.info(f"Retrying track {name} by {artists} after {backoff_time} seconds backoff")
        time.sleep(backoff_time)

    return tracks_to_retry

def extract_metadata_from_file(file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        title = audio.get("TIT2").text[0] if audio.get("TIT2") else None
        artist = audio.get("TPE1").text[0] if audio.get("TPE1") else None
        album = audio.get("TALB").text[0] if audio.get("TALB") else None
        logging.info(f"Extracted metadata from file: {file_path} - Title: {title}, Artist: {artist}, Album: {album}")
        return title, artist, album
    except Exception as e:
        logging.error(f"Error reading metadata from {file_path}: {e}")
        return None, None, None

def setup_spotify_client():
    logging.info("Setting up Spotify client")
    auth_manager = SpotifyClientCredentials()
    sp = spotipy.Spotify(auth_manager=auth_manager)
    logging.info("Spotify client setup complete")
    return sp

def notify_playlist_complete(playlist_name):
    ntfy_url = os.getenv('NTFY_URL')
    ntfy_topic = os.getenv('NTFY_TOPIC')

    if not ntfy_url or not ntfy_topic:
        logging.error("NTFY_URL or NTFY_TOPIC not set. Cannot send notifications.")
        return

    try:
        full_url = f"{ntfy_url}/{ntfy_topic}"
        message = f"🎉 Playlist '{playlist_name}' is fully downloaded! 🎶 All tracks are now available. ✅"
        response = requests.post(full_url, data=message.encode('utf-8'))

        if response.status_code == 200:
            logging.info(f"Notification sent successfully for playlist '{playlist_name}'.")
        else:
            logging.error(f"Failed to send notification. Status code: {response.status_code}")

    except Exception as e:
        logging.error(f"Error sending notification: {e}")

def main():
    setup_logging()

    logging.info("Starting main process")
    SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
    SLDL_USER = os.getenv('SLDL_USER')
    SLDL_PASS = os.getenv('SLDL_PASS')
    playlist_urls = os.getenv('SPOTIFY_PLAYLIST_URLS').split(',')

    sp = setup_spotify_client()

    database = "/app/data/playlist_tracks.db"
    conn = create_connection(database)

    if conn:
        for playlist_url in playlist_urls:
            logging.info(f"Processing playlist URL: {playlist_url}")
            playlist_id = get_playlist_id(playlist_url)
            if not playlist_id:
                logging.error(f"Skipping invalid playlist URL: {playlist_url}")
                continue

            playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
            create_table(conn, playlist_name)

            # Perform startup check
            startup_check(conn, playlist_name)

            new_tracks = fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)

            download_path = f"/app/data/downloads/{playlist_name}"

            for track in new_tracks:
                track_name, artist_name = track[1], track[2]
                logging.info(f"Attempting download for new track: {track_name} by {artist_name}")
                download_track(track_name, artist_name, SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SLDL_USER, SLDL_PASS, download_path)

            process_downloaded_tracks(playlist_name, conn)

            # Retry suspended downloads
            suspended_tracks = retry_suspended_downloads(conn, playlist_name)
            for track in suspended_tracks:
                logging.info(f"Retrying download for suspended track: {track[1]} by {track[2]}")
                download_track(track[1], track[2], SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SLDL_USER, SLDL_PASS, download_path)
                process_downloaded_tracks(playlist_name, conn)

        while True:
            logging.info("Starting new cycle of playlist checks")
            for playlist_url in playlist_urls:
                logging.info(f"Checking playlist: {playlist_url}")
                playlist_id = get_playlist_id(playlist_url)
                if not playlist_id:
                    logging.error(f"Skipping invalid playlist URL: {playlist_url}")
                    continue

                playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
                new_tracks = fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)

                download_path = f"/app/data/downloads/{playlist_name}"

                for track in new_tracks:
                    track_name, artist_name = track[1], track[2]
                    logging.info(f"Attempting download for new track: {track_name} by {artist_name}")
                    download_track(track_name, artist_name, SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SLDL_USER, SLDL_PASS, download_path)

                process_downloaded_tracks(playlist_name, conn)

                # Retry suspended downloads
                suspended_tracks = retry_suspended_downloads(conn, playlist_name)
                for track in suspended_tracks:
                    logging.info(f"Retrying download for suspended track: {track[1]} by {track[2]}")
                    download_track(track[1], track[2], SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SLDL_USER, SLDL_PASS, download_path)
                    process_downloaded_tracks(playlist_name, conn)
                    
            sleep_interval(5)
    else:
        logging.error("Failed to connect to the SQLite database.")

if __name__ == "__main__":
    main()
