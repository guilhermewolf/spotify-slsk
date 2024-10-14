import re
import time

def sleep_interval(seconds):
    time.sleep(seconds)

def sanitize_table_name(playlist_name):
    """Sanitize the playlist name to be used as a table name."""
    return 'pl_' + re.sub(r'\W+', '_', playlist_name.lower())
