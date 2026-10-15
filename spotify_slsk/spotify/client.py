"""Building the Spotify client."""
import logging
import os
import time

import requests
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


# The daemon and this web thread share one spotipy client, and spotipy's
# underlying requests.Session is not thread-safe; a stale pooled connection
# also shows up here as RemoteDisconnected. Retry a couple of times before
# giving up — a retry gets a fresh socket.
SPOTIFY_TRANSPORT_RETRIES = 3


def fetch_playlist_name(sp: spotipy.Spotify, pid: str) -> str:
    """Name of playlist `pid`, retrying transport errors (used by the web UI)."""
    last_exc = None
    for attempt in range(SPOTIFY_TRANSPORT_RETRIES):
        try:
            return sp.playlist(pid, fields="name")["name"]
        except requests.exceptions.RequestException as e:
            last_exc = e
            logging.warning(
                f"Spotify transport error for {pid} "
                f"(attempt {attempt + 1}/{SPOTIFY_TRANSPORT_RETRIES}): {e}"
            )
            time.sleep(0.5 * (attempt + 1))
    raise last_exc
