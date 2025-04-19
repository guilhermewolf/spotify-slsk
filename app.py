import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
#import music_tag
from slskd_api.client import SlskdClient
import shutil
import os
import logging
import re
import difflib
import random
import requests
import time
import shlex
import sqlite3
from db import create_connection, create_table, insert_track, fetch_all_tracks, update_download_status
from log_config import setup_logging
from utils import sleep_interval
from mutagen import File
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.flac import FLAC
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from utils import sanitize_table_name
from slskd_client import SlskdClient
from slskd_api.client import SlskdClient as BaseSlskdClient

slskd = None
class Track:
    def __init__(self, id, name, artists, album):
        self.id = id
        self.name = name
        self.artists = artists
        self.album = album

    def __repr__(self):
        return f"Track(id={self.id}, name={self.name}, artists={self.artists}, album={self.album})"


def get_playlist_id(playlist_url):
    try:
        if "playlist/" in playlist_url:
            return playlist_url.split("playlist/")[1].split("?")[0]
        else:
            raise ValueError(f"Invalid playlist URL: {playlist_url}")
    except IndexError:
        logging.error(f"Failed to extract playlist ID from URL: {playlist_url}")
        return None

def sanitize_input(text):
    return re.sub(r'[^A-Za-z0-9 ]+', '', text)

def fetch_and_compare_tracks(conn, playlist_id, table_name, sp):
    logging.info(f"Fetching tracks for playlist ID: {playlist_id} into table: {table_name}")
    results = sp.playlist_tracks(playlist_id)
    logging.info(f"Fetched {len(results['items'])} tracks from Spotify for playlist {table_name}")

    current_track_ids = set()
    db_tracks = {track[0]: track for track in fetch_all_tracks(conn, table_name)}

    new_tracks = []

    for item in results['items']:
        track = item['track']
        # Add debug log to check the extracted track details
        logging.debug(f"Fetched track: {track['name']} by {', '.join([artist['name'] for artist in track['artists']])}")
        
        current_track_ids.add(track['id'])

        if track['id'] not in db_tracks:
            track_data = (track['id'],
                          track['name'],
                          ', '.join([artist['name'] for artist in track['artists']]),
                          track['album']['name'])  # Only provide 4 values
            insert_track(conn, table_name, track_data)
            logging.info(f"New Song found in {table_name}: {track['name']} by {', '.join([artist['name'] for artist in track['artists']])} from the album {track['album']['name']}")
            t = Track(track['id'], track['name'], ', '.join([artist['name'] for artist in track['artists']]), track['album']['name'])
            new_tracks.append(t)  # Collect new tracks to trigger download later

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks

def find_closest_match(conn, playlist_name, title, artist):
    logging.info(f"Finding closest match for track: {title} by {artist} in playlist {playlist_name}")
    cursor = conn.cursor()
    cursor.execute(f"""SELECT id, name, artists FROM "{playlist_name}";""")
    potential_matches = cursor.fetchall()
    
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
    download_path = os.getenv("SLSKD_DOWNLOADS_DIR", "/downloads")
    logging.info(f"Checking for downloaded tracks in {download_path}")

    supported_extensions = (".mp3", ".flac", ".aiff", ".wav")

    for root, dirs, files in os.walk(download_path):
        for file in files:
            if file.lower().endswith(supported_extensions) and "incomplete" not in root.lower():
                file_path = os.path.join(root, file)
                logging.info(f"Processing file: {file_path}")

                title, artist, album = extract_metadata_from_file(file_path)
                if title and artist:
                    logging.info(f"Extracted Metadata - Track: {title}, Artist: {artist}, Album: {album}")

                    match, match_score = find_closest_match(conn, playlist_name, title, artist)

                    if match and match_score >= 0.6:
                        logging.info(f"Match found with similarity {match_score:.2f}. Tagging, moving, and updating database.")
                        
                        tagged = tag_audio_file(file_path, title, artist, album)
                        if not tagged:
                            logging.warning(f"Failed to tag {file_path}, continuing anyway.")

                        final_path = move_track_to_playlist_folder(file_path, playlist_name)
                        update_download_status(conn, match[0], playlist_name, success=True, file_path=final_path)
                    else:
                        logging.warning(f"Could not find track {title} by {artist} in the database.")
                else:
                    logging.warning(f"Metadata incomplete or missing for file: {file_path}")


def update_download_status(conn, track_id, table_name, success=False, file_path=None):
    cursor = conn.cursor()
    if success:
        sql = f'UPDATE "{table_name}" SET downloaded = 1, attempts = 0, suspended_until = NULL, path = ? WHERE id = ?'
        params = (file_path, track_id)
        logging.info(f"Updating status of track ID: {track_id} to downloaded with path: {file_path}")
    else:
        sql = f'UPDATE "{table_name}" SET attempts = attempts + 1, last_attempt = CURRENT_TIMESTAMP WHERE id = ?'
        params = (track_id,)
        logging.info(f"Incrementing attempt count for track ID: {track_id}")
        cursor.execute(f'SELECT attempts FROM "{table_name}" WHERE id = ?', (track_id,))
        attempts = cursor.fetchone()[0]
        if attempts >= 3:
            sql = f'UPDATE "{table_name}" SET suspended_until = datetime("now", "+2 days") WHERE id = ?'
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

    #return tracks_to_retry
    return [Track(track[0], track[1], track[2], "") for track in tracks_to_retry]

def extract_metadata_from_file(file_path):
    try:
        audio = File(file_path)
        if not audio:
            return None, None, None

        title = audio.get("title", [None])[0]
        artist = audio.get("artist", [None])[0]
        album = audio.get("album", [None])[0]

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

def send_ntfy_notification(url, topic, message):
    try:
        response = requests.post(f"{url}/{topic}", data=message)
        if response.status_code == 200:
            logging.info(f"Notification sent successfully: {message}")
        else:
            logging.error(f"Failed to send notification: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Error while sending notification: {e}")

def all_tracks_downloaded(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE downloaded = 0")
    remaining_tracks = cursor.fetchone()[0]
    
    if remaining_tracks == 0:
        logging.info(f"All tracks in playlist {table_name} have been downloaded.")
        return True
    else:
        logging.info(f"{remaining_tracks} tracks in playlist {table_name} are still not downloaded.")
        return False
    


def move_track_to_playlist_folder(track_path: str, playlist_name: str) -> str:
    try:
        # Extract only the filename
        filename = os.path.basename(track_path)

        # Find the actual file inside the playlist folder under /downloads
        real_path = os.path.join("/downloads", playlist_name, filename)

        # Define destination folder inside local data/ for organized storage
        dest_dir = os.path.join("data", playlist_name)
        os.makedirs(dest_dir, exist_ok=True)

        destination = os.path.join(dest_dir, filename)

        try:
            shutil.move(real_path, destination)
        except OSError as e:
            logging.error(f"Failed to move file across devices: {e}")
            shutil.copy2(real_path, destination)

        logging.info(f"Moved file to playlist folder: {destination}")
        return destination

    except Exception as e:
        logging.error(f"Failed to move file: {e}")
        return None

def tag_audio_file(file_path, title, artist, album):
    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext == '.mp3':
            audio = MP3(file_path, ID3=ID3)
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
            audio.save()

        elif ext == '.flac':
            audio = FLAC(file_path)
            audio['title'] = title
            audio['artist'] = artist
            audio['album'] = album
            audio.save()

        elif ext == '.aiff':
            audio = AIFF(file_path)
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
            audio.save()

        elif ext == '.wav':
            logging.warning("WAV tagging is not fully supported; skipping tags.")

        else:
            logging.warning(f"Unsupported format for tagging: {ext}")
            return False

        logging.info(f"Tagged {file_path} successfully.")
        return True

    except Exception as e:
        logging.error(f"Failed to tag {file_path}: {e}")
        return False
    
def wait_for_slskd(host, api_key, timeout=60):
    logging.info(f"Waiting for slskd at {host}...")

    client = BaseSlskdClient(host=host, api_key=api_key)

    start = time.time()
    while time.time() - start < timeout:
        try:
            version_info = client.misc.version()
            if version_info and "version" in version_info:
                logging.info(f"slskd is ready. Version: {version_info['version']}")
                return
        except Exception as e:
            logging.debug(f"slskd not ready yet: {e}")
        time.sleep(1)

    raise RuntimeError("slskd did not become ready in time.")

def main():
    setup_logging()
    logging.info("Starting main process")

    SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
    SLSKD_API_KEY = os.getenv("SLSKD_API_KEY")
    SLSKD_HOST_URL = os.getenv("SLSKD_HOST_URL", "http://slskd:5030")
    NTFY_URL = os.getenv('NTFY_URL')
    NTFY_TOPIC = os.getenv('NTFY_TOPIC')
    playlist_urls = os.getenv('SPOTIFY_PLAYLIST_URLS').split(',')

    wait_for_slskd(SLSKD_HOST_URL, SLSKD_API_KEY)
    global slskd
    slskd = SlskdClient(host=SLSKD_HOST_URL, api_key=SLSKD_API_KEY)

    send_ntfy_notification(NTFY_URL, NTFY_TOPIC, "Starting Spotify Playlist Downloader 🚀")
    sp = setup_spotify_client()

    database = "./data/playlist_tracks.db"
    conn = create_connection(database)


    if not conn:
        logging.error("Failed to connect to the SQLite database.")
        return

    

    for playlist_url in playlist_urls:
        playlist_id = get_playlist_id(playlist_url)
        if not playlist_id:
            logging.error(f"Skipping invalid playlist URL: {playlist_url}")
            continue

        playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
        create_table(conn, playlist_name)
        startup_check(conn, playlist_name)

        fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)

        cursor = conn.cursor()
        cursor.execute(f"SELECT id, name, artists, album FROM \"{playlist_name}\" WHERE downloaded = 0 AND (suspended_until IS NULL OR suspended_until < datetime('now'))")
        rows = cursor.fetchall()
        pending_tracks = [Track(row[0], row[1], row[2], row[3]) for row in rows]
        logging.info(f"{len(pending_tracks)} tracks in playlist {playlist_name} are still not downloaded.")

        for track in pending_tracks:
            logging.info(f"Attempting download for: {track.name} by {track.artists}")
            search_result = slskd.perform_search(track.artists, track.name)

            if search_result:
                file_path = slskd.download_best_candidate(search_result)
                if file_path:
                    verified = process_downloaded_tracks(playlist_name, conn)
                    if verified:
                        file_path = os.path.normpath(file_path.replace("\\", "/"))
                        final_path = move_track_to_playlist_folder(file_path, playlist_name)
                        update_download_status(conn, track.id, playlist_name, success=True, file_path=final_path)
                        continue
                file_path = slskd.download_best_candidate(search_result, exclude_first=True)
                if file_path:
                    verified = process_downloaded_tracks(playlist_name, conn)
                    if verified:
                        file_path = os.path.normpath(file_path.replace("\\", "/"))
                        final_path = move_track_to_playlist_folder(file_path, playlist_name)
                        update_download_status(conn, track.id, playlist_name, success=True, file_path=final_path)
                        continue

            logging.warning(f"All download attempts failed for: {track.name}")
            update_download_status(conn, track.id, playlist_name, success=False)

        if all_tracks_downloaded(conn, playlist_name):
            send_ntfy_notification(NTFY_URL, NTFY_TOPIC, f"All tracks in {playlist_name} have been downloaded! 🎉")

    while True:
        logging.info("Starting new cycle of playlist checks")
        for playlist_url in playlist_urls:
            playlist_id = get_playlist_id(playlist_url)
            if not playlist_id:
                logging.error(f"Skipping invalid playlist URL: {playlist_url}")
                continue

            playlist_name = sanitize_table_name(sp.playlist(playlist_id)['name'])
            fetch_and_compare_tracks(conn, playlist_id, playlist_name, sp)

            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT id, name, artists, album FROM "{playlist_name}"
                WHERE downloaded = 0 AND (suspended_until IS NULL OR suspended_until < datetime('now'))
            """)
            rows = cursor.fetchall()
            pending_tracks = [Track(row[0], row[1], row[2], row[3]) for row in rows]
            logging.info(f"{len(pending_tracks)} tracks in playlist {playlist_name} are still not downloaded.")

            for track in pending_tracks:
                logging.info(f"Retrying track: {track.name} by {track.artists}")
                search_result = slskd.perform_search(track.artists, track.name)

                if search_result:
                    file_path = slskd.download_best_candidate(search_result)
                    if file_path and process_downloaded_tracks(playlist_name, conn):
                        final_path = move_track_to_playlist_folder(file_path, playlist_name)
                        update_download_status(conn, track.id, playlist_name, success=True, file_path=final_path)
                        continue

                    file_path = slskd.download_best_candidate(search_result, exclude_first=True)
                    if file_path and process_downloaded_tracks(playlist_name, conn):
                        final_path = move_track_to_playlist_folder(file_path, playlist_name)
                        update_download_status(conn, track.id, playlist_name, success=True, file_path=final_path)
                        continue

                update_download_status(conn, track.id, playlist_name, success=False)

            if all_tracks_downloaded(conn, playlist_name):
                send_ntfy_notification(NTFY_URL, NTFY_TOPIC, f"All tracks in {playlist_name} have been downloaded! 🎉")

        sleep_interval(5)

if __name__ == "__main__":
    main()
