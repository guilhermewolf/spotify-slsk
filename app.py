import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os
import logging
import re
import subprocess
from db import create_connection, create_table, insert_track, fetch_all_tracks
from log_config import setup_logging
from timing_utils import sleep_interval

def get_playlist_id(playlist_url):
    return playlist_url.split("playlist/")[1].split("?")[0]

def sanitize_table_name(name):
    return re.sub(r'\W+', '_', name)

def download_playlist(playlist_url, playlist_name, client_id, client_secret, sldl_user, sldl_pass):
    download_path = f"/app/data/downloads/{playlist_name}"
    
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
                          track['album']['name'])
            insert_track(conn, table_name, track_data)
            logging.info(f"New Song found in {table_name}: {track['name']} by {', '.join([artist['name'] for artist in track['artists']])} from the album {track['album']['name']}")
            playlist_url = sp.playlist(playlist_id)['external_urls']['spotify']
            download_playlist(playlist_url, table_name, os.getenv('SPOTIFY_CLIENT_ID'), os.getenv('SPOTIFY_CLIENT_SECRET'), os.getenv('SLDL_USER'), os.getenv('SLDL_PASS'))

def main():
    setup_logging()

    SPOTIPY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
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
