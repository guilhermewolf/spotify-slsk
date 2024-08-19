import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os
import logging
import re
from db import create_connection, create_table, insert_track, fetch_all_tracks
from log_config import setup_logging
from timing_utils import sleep_interval

def get_playlist_id(playlist_url):
    return playlist_url.split("playlist/")[1].split("?")[0]

def sanitize_table_name(name):
    return re.sub(r'\W+', '_', name)

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

def main():
    setup_logging()

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
