"""Building the Spotify client."""
import logging
import os

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyClientCredentials

def setup_spotify_client():
    """Build the Spotify client, or return None if credentials are missing.

    Returning None rather than raising keeps the dashboard reachable so the
    operator can see *why* nothing is syncing. Every caller already tolerates
    a missing client: the webui falls back to a placeholder playlist name,
    and the cycle loop skips syncing (see main()).
    """
    logging.info("Setting up Spotify client")
    try:
        # SpotifyClientCredentials ignores SPOTIPY_CACHE_PATH and defaults to
        # ./.cache, which the non-root container user can't write: spotipy
        # warned "Couldn't write token to cache" on every call. Point the
        # cache at the data dir (the tests point it into their sandbox).
        auth_manager = SpotifyClientCredentials(
            cache_handler=CacheFileHandler(
                cache_path=os.getenv("SPOTIPY_CACHE_PATH", "./data/.spotipy-cache")
            )
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)
    except Exception as e:
        logging.error(
            f"Spotify client unavailable — check SPOTIPY_CLIENT_ID / "
            f"SPOTIPY_CLIENT_SECRET: {e}"
        )
        return None
    logging.info("Spotify client setup complete")
    return sp
