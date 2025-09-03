import spotipy
import slskd_api
import shutil
import os
import logging
import re
import difflib
import random
import requests
import time
import sqlite3
from db import (
    create_connection,
    create_table,
    insert_track,
    fetch_all_tracks,
    update_download_status,
    clear_tried_entries,
    add_tried_file,
    get_pending_tracks,
)
from log_config import setup_logging
from utils import sleep_interval
from mutagen import File
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.flac import FLAC
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from utils import sanitize_table_name
from spotipy.oauth2 import SpotifyClientCredentials
from soulseek_api import perform_search, download_and_verify
from models import Track

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

def fetch_all_playlist_tracks(sp, playlist_id):
    tracks = []
    offset = 0
    limit = 100

    while True:
        results = sp.playlist_tracks(playlist_id, offset=offset, limit=limit)
        items = results.get('items', [])
        if not items:
            break
        tracks.extend(items)
        offset += len(items)

    return tracks

def fetch_and_compare_tracks(conn, playlist_id, sp):
    playlist_info = sp.playlist(playlist_id)
    playlist_title = playlist_info['name']
    table_name = f"{sanitize_table_name(playlist_title)}"

    create_table(conn, table_name)

    logging.info(f"Fetching tracks for playlist ID: {playlist_id} into table: {table_name}")
    items = fetch_all_playlist_tracks(sp, playlist_id)
    logging.info(f"Fetched {len(items)} tracks from Spotify for playlist {table_name}")

    db_tracks = {track[0]: track for track in fetch_all_tracks(conn, table_name)}
    new_tracks = []

    for item in items:
        track = item['track']
        artists_str = extract_artists_string(track)

        logging.debug(f"Fetched track: {track['name']} by {artists_str}")

        if track['id'] not in db_tracks:
            track_data = (
                track['id'],
                track['name'],
                artists_str,
                track['album']['name']
            )
            insert_track(conn, table_name, track_data)
            logging.info(
                f"New Song found in {table_name}: {track['name']} by {artists_str} from album {track['album']['name']}"
            )
            new_tracks.append(
                Track(track['id'], track['name'], artists_str, track['album']['name'], playlist_id)
            )

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks, table_name


def find_closest_match(conn, table_name, title, artist):
    """
    Return (best_row, score) where best_row is (id, name, artists).
    More tolerant matching:
      - normalizes punctuation/case
      - strips bracketed parts in titles (e.g., remixes)
      - supports multi-artist strings ("A, B" vs "A feat. B")
      - weights title (0.6) higher than artist (0.4)
    """
    import re
    import difflib

    def norm(s: str) -> str:
        if not s:
            return ""
        s = s.lower()
        s = re.sub(r"\s*\([^)]*\)|\s*\[[^\]]*\]", " ", s)     # drop (...) and [...]
        s = re.sub(r"(feat\.|featuring|ft\.)", "", s)          # drop feat variants
        s = re.sub(r"[^\w\s]+", " ", s)                        # punctuation -> space
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def artist_tokens(s: str) -> set:
        # split on commas, ampersand, " x ", " and ", etc.
        s = norm(s)
        parts = re.split(r"\s*(?:,|&| x | and )\s*", s)
        return {p for p in parts if p}

    title_q = norm(title)
    artist_q = norm(artist)
    artist_q_tokens = artist_tokens(artist)

    logging.info(f"Finding closest match for: title='{title}' artist='{artist}' in table {table_name}")
    cursor = conn.cursor()
    cursor.execute(f'SELECT id, name, artists FROM "{table_name}";')
    rows = cursor.fetchall()

    best = None
    best_score = 0.0

    for row in rows:
        tid, db_title, db_artists = row
        db_title_n = norm(db_title)
        db_artist_n = norm(db_artists)
        db_artist_tokens = artist_tokens(db_artists)

        # Title similarity
        s_title = difflib.SequenceMatcher(None, title_q, db_title_n).ratio()

        # Artist similarity: max of token-to-token sims + token overlap bonus
        token_sims = []
        for tq in artist_q_tokens or {artist_q}:
            for tk in db_artist_tokens or {db_artist_n}:
                token_sims.append(difflib.SequenceMatcher(None, tq, tk).ratio())
        s_artist = max(token_sims) if token_sims else 0.0

        # Bonus for token overlap (handles lists like "A, B")
        if artist_q_tokens and db_artist_tokens:
            inter = len(artist_q_tokens & db_artist_tokens)
            union = len(artist_q_tokens | db_artist_tokens)
            jacc = inter / union if union else 0.0
            s_artist = max(s_artist, jacc)

        score = 0.6 * s_title + 0.4 * s_artist
        if score > best_score:
            best_score = score
            best = row

    if best:
        logging.info(f"Best match: {best[1]} by {best[2]} (score={best_score:.2f})")
    else:
        logging.warning(f"No suitable match for: '{title}' by '{artist}'")

    return best, best_score

def process_downloaded_file(file_path, playlist_name, conn, reconcile: bool = False):
    """
    Process a file that is either freshly downloaded or already on disk.

    When reconcile=True:
      - Never delete files on mismatch.
      - If the file already lives in /playlists/<playlist_name>, don't move it again.
    """
    title, artist, album = extract_metadata_from_file(file_path)

    if not title or not artist:
        logging.warning(f"❌ Missing metadata for {file_path}. Skipping tagging and DB update.")
        _reject_and_log(file_path, playlist_name, conn, reason="invalid metadata", destructive=not reconcile)
        return False, None

    match, score = find_closest_match(conn, playlist_name, title, artist)
    if not match:
        logging.warning(f"❌ No match found in DB for {title} by {artist}.")
        _reject_and_log(file_path, playlist_name, conn, reason="no match", destructive=not reconcile)
        return False, None

    if score < 0.6:
        logging.warning(f"⚠️ Low match score ({score:.2f}) for {title} by {artist}. Skipping update.")
        _reject_and_log(file_path, playlist_name, conn, track_id=match[0], reason="low score", destructive=not reconcile)
        return False, None

    # Tag before placement
    tag_audio_file(file_path, title, artist, album)

    # If the file is already inside the playlists dir, don't move it.
    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    try:
        already_in_library = os.path.commonpath([os.path.abspath(file_path), os.path.abspath(playlists_root)]) == os.path.abspath(playlists_root)
    except Exception:
        already_in_library = False

    if already_in_library:
        final_path = file_path
    else:
        final_path = move_track_to_playlist_folder(file_path, playlist_name)

    if final_path:
        update_download_status(conn, match[0], playlist_name, success=True, file_path=final_path)
        return True, final_path

    logging.error(f"❌ Failed to move {file_path} to playlist folder.")
    return False, None


def _reject_and_log(file_path, playlist_name, conn, track_id=None, reason="unknown", destructive: bool = True):
    """
    On normal downloads we delete bad files; on reconcile we never delete.
    """
    filename = os.path.basename(file_path)

    if destructive:
        try:
            os.remove(file_path)
            logging.info(f"🧹 Deleted file due to {reason}: {file_path}")
        except Exception as e:
            logging.error(f"❌ Failed to delete file {file_path}: {e}")
    else:
        logging.info(f"ℹ️ (reconcile) Keeping unmatched file due to {reason}: {file_path}")

    if track_id:
        add_tried_file(conn, playlist_name, track_id, filename)
    else:
        logging.debug(f"Skipping add_tried_file() because track ID is unknown for: {filename}")

# def update_download_status(conn, track_id, table_name, success=False, file_path=None):
#     cursor = conn.cursor()
#     if success:
#         sql = f'UPDATE "{table_name}" SET downloaded = 1, attempts = 0, suspended_until = NULL, path = ? WHERE id = ?'
#         params = (file_path, track_id)
#         logging.info(f"Updating status of track ID: {track_id} to downloaded with path: {file_path}")
#     else:
#         sql = f'UPDATE "{table_name}" SET attempts = attempts + 1, last_attempt = CURRENT_TIMESTAMP WHERE id = ?'
#         params = (track_id,)
#         logging.info(f"Incrementing attempt count for track ID: {track_id}")
#         cursor.execute(f'SELECT attempts FROM "{table_name}" WHERE id = ?', (track_id,))
#         attempts = cursor.fetchone()[0]
#         if attempts >= 3:
#             sql = f'UPDATE "{table_name}" SET suspended_until = datetime("now", "+2 days") WHERE id = ?'
#             logging.info(f"Track ID: {track_id} has reached max attempts, suspending for 2 days")

#     try:
#         cursor.execute(sql, params)
#         conn.commit()
#         logging.info(f"Updated track {track_id} status in {table_name}.")
#     except sqlite3.Error as e:
#         conn.rollback()
#         logging.error(f"Error updating track status for {track_id} in {table_name}: {e}")


def clean_up_untracked_files(conn, download_path, table_name, delete: bool = False):
    """
    Compare files on disk vs DB. By default, do NOT delete (safe).
    If delete=True, remove files not present in DB.
    """
    logging.info(f"Cleaning up untracked files in {download_path} (delete={delete})")
    cursor = conn.cursor()
    cursor.execute(f'SELECT path FROM "{table_name}" WHERE downloaded = 1')
    db_files = {row[0] for row in cursor.fetchall() if row[0]}

    for root, _, files in os.walk(download_path):
        for file in files:
            if file.lower().endswith(('.mp3', '.flac', '.aiff', '.wav', '.m4a', '.ogg')):
                file_path = os.path.join(root, file)
                if file_path not in db_files:
                    if delete:
                        try:
                            os.remove(file_path)
                            logging.info(f"Deleted untracked file: {file_path}")
                        except Exception as e:
                            logging.error(f"Failed to delete {file_path}: {e}")
                    else:
                        logging.info(f"(dry-run) Would delete untracked file: {file_path}")

def startup_check(conn, table_name):
    """
    Reconcile DB with files already on disk for a given playlist table.
    - Scans the library folder: /playlists/<table_name>
    - Scans ONLY completed downloads: /downloads/complete (ignores /downloads/incomplete)
    - Never deletes files during reconciliation.
    """
    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    downloads_root = os.getenv("SLSKD_DOWNLOADS_DIR", "/downloads")
    downloads_complete = os.path.join(downloads_root, "complete")

    valid_exts = ('.mp3', '.flac', '.aiff', '.wav', '.m4a', '.ogg')

    processed_count = 0

    # 1) Library folder for this playlist
    playlist_dir = os.path.join(playlists_root, table_name)
    if os.path.isdir(playlist_dir):
        logging.info(f"[startup] Reconciling library folder: {playlist_dir}")
        for root, _, files in os.walk(playlist_dir):
            for f in files:
                lf = f.lower()
                if not lf.endswith(valid_exts):
                    continue
                # Skip obvious temp/incomplete artifacts
                if lf.endswith(('.part', '.tmp', '.partial')) or lf.startswith(('._', '~$')):
                    continue
                file_path = os.path.join(root, f)
                try:
                    process_downloaded_file(file_path, table_name, conn, reconcile=True)
                    processed_count += 1
                except Exception as e:
                    logging.exception(f"[startup] Failed to reconcile {file_path}: {e}")
    else:
        logging.info(f"[startup] No library folder found at {playlist_dir}")

    # 2) Only completed downloads (ignore /downloads/incomplete)
    if os.path.isdir(downloads_complete):
        logging.info(f"[startup] Reconciling completed downloads: {downloads_complete}")
        for root, _, files in os.walk(downloads_complete):
            for f in files:
                lf = f.lower()
                if not lf.endswith(valid_exts):
                    continue
                # Skip obvious temp/incomplete artifacts (defensive)
                if lf.endswith(('.part', '.tmp', '.partial')) or lf.startswith(('._', '~$')):
                    continue
                file_path = os.path.join(root, f)
                try:
                    process_downloaded_file(file_path, table_name, conn, reconcile=True)
                    processed_count += 1
                except Exception as e:
                    logging.exception(f"[startup] Failed to reconcile {file_path}: {e}")
    else:
        logging.info(f"[startup] No completed downloads folder at {downloads_complete}")

    # 3) Never delete during reconcile; if you really want cleanup later, run with delete=True
    clean_up_untracked_files(conn, playlists_root, table_name, delete=False)
    logging.info(f"[startup] Reconciliation complete for: {table_name} (files processed: {processed_count})")

def extract_artists_string(track):
    return ', '.join(artist['name'] for artist in track['artists'])

def apply_exponential_backoff(attempts, base=1.0, jitter=0.5):
    delay = base * (2 ** attempts)
    jittered_delay = delay * random.uniform(1.0, 1.0 + jitter)
    logging.debug(f"Sleeping for {jittered_delay:.2f} seconds (attempts: {attempts})")
    time.sleep(jittered_delay)

def retry_suspended_downloads(conn, table_name):
    logging.info(f"Retrying suspended downloads for table: {table_name}")
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, name, artists, attempts FROM {table_name} WHERE downloaded = 0 AND (suspended_until IS NULL OR suspended_until < datetime('now'))")
    tracks_to_retry = cursor.fetchall()

    for track in tracks_to_retry:
        track_id, name, artists, attempts = track
        logging.info(f"Retrying track {name} by {artists}")
        apply_exponential_backoff(attempts)

    #return tracks_to_retry
    return [Track(track[0], track[1], track[2], "") for track in tracks_to_retry]

def extract_metadata_from_file(file_path):
    try:
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == ".mp3":
            audio = MP3(file_path, ID3=ID3)
            title = audio.tags.get("TIT2")
            artist = audio.tags.get("TPE1")
            album = audio.tags.get("TALB")

            title = title.text[0] if title else None
            artist = artist.text[0] if artist else None
            album = album.text[0] if album else None

        elif ext == ".flac":
            audio = FLAC(file_path)
            title = audio.get("title", [None])[0]
            artist = audio.get("artist", [None])[0]
            album = audio.get("album", [None])[0]

        elif ext == ".aiff":
            audio = AIFF(file_path)
            title = audio.get("TIT2", [None])[0]
            artist = audio.get("TPE1", [None])[0]
            album = audio.get("TALB", [None])[0]

        elif ext == ".wav":
            logging.warning("WAV format may not have embedded metadata.")
            return None, None, None

        else:
            logging.warning(f"Unsupported audio format for metadata: {ext}")
            return None, None, None

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
        # Use the correct base path
        base_dir = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
        dest_dir = os.path.join(base_dir, playlist_name)
        os.makedirs(dest_dir, exist_ok=True)

        # Final destination path
        filename = os.path.basename(track_path)
        destination = os.path.join(dest_dir, filename)

        if not os.path.exists(track_path):
            logging.error(f"Source file does not exist: {track_path}")
            return None

        # Attempt to move, fallback to copy
        try:
            shutil.move(track_path, destination)
        except OSError as e:
            logging.error(f"Failed to move file across devices: {e}. Trying to copy instead.")
            shutil.copy2(track_path, destination)

        logging.info(f"Moved file to playlist folder: {destination}")
        return destination

    except Exception as e:
        logging.error(f"Failed to move file: {e}")
        return None

def tag_audio_file(file_path, title, artist, album):
    ext = os.path.splitext(file_path)[1].lower()

    # Ensure all metadata fields are non-None strings
    title = title or ""
    artist = artist or ""
    album = album or ""

    try:
        if ext == '.mp3':
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
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
            if audio.tags is None:
                audio.add_tags()
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

    
def wait_for_slskd_healthy(host, api_key, timeout=90, check_interval=1):
    logging.info(f"Waiting for slskd at {host} (timeout: {timeout}s)...")
    client = slskd_api.SlskdClient(
        host=os.getenv("SLSKD_HOST_URL", "http://slskd:5030"),
        api_key=os.getenv("SLSKD_API_KEY"),
        url_base=os.getenv("SLSKD_URL_BASE", "")
    )

    start = time.time()
    while time.time() - start < timeout:
        try:
            state = client.application.state()
            if state['server'].get('isConnected') and state['server'].get('isLoggedIn'):
                logging.info("✅ slskd is healthy and connected.")
                return
        except Exception as e:
            pass  # silence repeated logs

        if int(time.time() - start) % 5 == 0:
            logging.debug("Still waiting for slskd...")

        time.sleep(check_interval)

    raise RuntimeError("❌ slskd did not become healthy in time.")


def process_playlist(sp, conn, playlist_id, ntfy_url, ntfy_topic):
    logging.info(f"🎧 Processing playlist ID: {playlist_id}")

    new_tracks, playlist_name = fetch_and_compare_tracks(conn, playlist_id, sp)

    startup_check(conn, playlist_name)

    if new_tracks:
        msg = f"🔄 Playlist updated: {len(new_tracks)} new track(s) added to {playlist_name}"
        send_ntfy_notification(ntfy_url, ntfy_topic, msg)

    tracks = get_pending_tracks(conn, playlist_name)
    if not tracks:
        logging.info(f"No pending tracks for {playlist_name}")
        return

    for track in tracks:
        logging.info(f"🎶 Downloading: {track.name} by {track.artist}")
        search_results = perform_search(track.artist, track.name)

        success = handle_track_download(
            track=track,
            playlist_name=playlist_name,
            conn=conn,
            search_results=search_results,
            max_attempts=2
        )

        if success:
            logging.info(f"✅ Downloaded: {track.name} by {track.artist}")
        else:
            logging.warning(f"❌ Failed: {track.name} by {track.artist}")

    send_ntfy_notification(ntfy_url, ntfy_topic, f"✅ Finished processing playlist: {playlist_name}")


def safe_get(tag):
    if isinstance(tag, list):
        return tag[0]
    return tag

def handle_track_download(track, playlist_name, conn, search_results, max_attempts=2):
    if not search_results:
        logging.warning(f"No search results for: {track.name} by {track.artist}")
        return False

    file_path = download_and_verify(
        search_results=search_results,
        expected_title=track.name,
        expected_artist=track.artist,
        conn=conn,
        playlist_name=playlist_name,
        track_id=track.id,
        max_attempts=max_attempts,
    )

    if file_path:
        verified, final_path = process_downloaded_file(file_path, playlist_name, conn)
        if verified:
            clear_tried_entries(conn, playlist_name, track.id)
            return True

    update_download_status(conn, track.id, playlist_name, success=False)
    return False


def main():
    setup_logging()
    logging.info("Starting main process")

    SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
    SLSKD_API_KEY = os.getenv("SLSKD_API_KEY")
    SLSKD_HOST_URL = os.getenv("SLSKD_HOST_URL", "http://slskd:5030")
    NTFY_URL = os.getenv('NTFY_URL')
    NTFY_TOPIC = os.getenv('NTFY_TOPIC')
    DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", "/downloads")
    DATA_ROOT = os.getenv("DATA_ROOT", "/data") 
    playlist_urls = os.getenv('SPOTIFY_PLAYLIST_URLS').split(',')

    wait_for_slskd_healthy(SLSKD_HOST_URL, SLSKD_API_KEY)

    send_ntfy_notification(NTFY_URL, NTFY_TOPIC, "Starting Spotify Playlist Downloader 🚀")
    sp = setup_spotify_client()

    database = "./data/playlist_tracks.db"
    conn = create_connection(database)


    if not conn:
        logging.error("Failed to connect to the SQLite database.")
        return

    

    for playlist_url in playlist_urls:
        playlist_id = get_playlist_id(playlist_url)
        playlist_name = sanitize_table_name(playlist_id)
        process_playlist(sp, conn, playlist_id, NTFY_URL, NTFY_TOPIC)

    while True:
        logging.info("Starting new cycle of playlist checks")
        for playlist_url in playlist_urls:
            playlist_id = get_playlist_id(playlist_url)
            playlist_name = sanitize_table_name(playlist_id)
            process_playlist(sp, conn, playlist_id, NTFY_URL, NTFY_TOPIC)
        sleep_interval(5)


if __name__ == "__main__":
    main()
