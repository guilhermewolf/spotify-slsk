import re
import time

def sleep_interval(seconds):
    time.sleep(seconds)

def sanitize_table_name(playlist_name):
    playlist_name = re.sub(r'[^A-Za-z0-9]', '_', playlist_name).lower()

    # Check if 'pl_' already exists at the start to avoid double prefix
    if not playlist_name.startswith('pl_'):
        playlist_name = f'pl_{playlist_name}'

    return playlist_name