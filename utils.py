import logging
import re


def sanitize_table_name(playlist_name):
    """Sanitize the playlist name to be used as a table name."""
    return 'pl_' + re.sub(r'\W+', '_', playlist_name.lower())


def get_playlist_id(playlist_url):
    """Extract a Spotify playlist id from a public playlist URL. Returns None
    if the URL doesn't contain a 'playlist/' segment or parsing fails."""
    try:
        if "playlist/" not in (playlist_url or ""):
            logging.error(
                f"Invalid playlist URL (no 'playlist/' segment): {playlist_url}"
            )
            return None
        return playlist_url.split("playlist/")[1].split("?")[0] or None
    except Exception as e:
        logging.error(
            f"Failed to extract playlist ID from URL {playlist_url!r}: {e}"
        )
        return None
