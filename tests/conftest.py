"""Test isolation guarantees.

Every test module calls ``os.environ.setdefault(...)``, which is a no-op when
the variable is already exported. A developer who had sourced the real
``.env`` into their shell before running pytest therefore ran the suite
against their production configuration: ``SLSKD_HOST_URL`` pointing at the
real slskd daemon and ``SLSKD_PLAYLISTS_DIR`` at their actual music library,
which ``app._reject_and_log`` is willing to ``os.remove`` from.

The autouse fixtures below *override* rather than default, so the suite is
isolated regardless of the ambient environment, and they are autouse so no
test can opt out by forgetting to request them.
"""
from __future__ import annotations

import os
import sys

import pytest

# Import the app modules from the repo root regardless of how pytest was invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Values that must never reach a real service. The host uses the reserved
# .invalid TLD so a bug that actually opens a socket fails fast and loudly
# instead of reaching something real.
_ISOLATED_ENV = {
    "SLSKD_HOST_URL": "http://slskd.invalid:5030",
    "SLSKD_API_KEY": "test-api-key-not-real",
    "SLSKD_URL_BASE": "",
    "SPOTIPY_CLIENT_ID": "test-client-id-not-real",
    "SPOTIPY_CLIENT_SECRET": "test-client-secret-not-real",
    "SPOTIFY_PLAYLIST_URLS": "",
    # Empty so send_ntfy_notification short-circuits instead of posting.
    "NTFY_URL": "",
    "NTFY_TOPIC": "",
    "UI_SECRET_KEY": "test-secret-key-not-real",
    "LOGLEVEL": "CRITICAL",
    "LIB_LOGLEVEL": "CRITICAL",
}

# Directory-valued settings are pointed at a per-session temp tree, so that
# even the destructive paths (os.remove, shutil.move) cannot escape it.
_ISOLATED_DIR_KEYS = (
    "SLSKD_DOWNLOADS_DIR",
    "SLSKD_PLAYLISTS_DIR",
    "HEARTBEAT_FILE",
    "SPOTIPY_CACHE_PATH",
)


@pytest.fixture(autouse=True, scope="session")
def _isolate_environment(tmp_path_factory):
    """Force every externally-configurable value to a sandboxed one.

    Session-scoped and autouse: isolation is not something an individual test
    should be able to decline.
    """
    sandbox = tmp_path_factory.mktemp("isolated-env")
    saved = {k: os.environ.get(k) for k in (*_ISOLATED_ENV, *_ISOLATED_DIR_KEYS)}

    os.environ.update(_ISOLATED_ENV)
    os.environ["SLSKD_DOWNLOADS_DIR"] = str(sandbox / "downloads")
    os.environ["SLSKD_PLAYLISTS_DIR"] = str(sandbox / "playlists")
    os.environ["HEARTBEAT_FILE"] = str(sandbox / "heartbeat")
    os.environ["SPOTIPY_CACHE_PATH"] = str(sandbox / "spotipy-cache")
    os.makedirs(os.environ["SLSKD_DOWNLOADS_DIR"], exist_ok=True)
    os.makedirs(os.environ["SLSKD_PLAYLISTS_DIR"], exist_ok=True)

    yield sandbox

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _no_real_slskd_client(monkeypatch):
    """Fail loudly if a test builds a real slskd client.

    Today every test that reaches the slskd boundary monkeypatches
    ``get_client``; nothing structurally prevents a future test from calling
    ``search_and_download`` unmocked and hitting the operator's real daemon
    and download queue. This turns that latent risk into an immediate error.
    """
    import slskd_api

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "A test tried to construct a real slskd client. Monkeypatch "
            "soulseek_api.get_client instead."
        )

    monkeypatch.setattr(slskd_api, "SlskdClient", _blocked)
    # get_client() caches its client in a module global; make sure a client
    # built by an earlier test can never leak into this one.
    from spotify_slsk import soulseek_api

    monkeypatch.setattr(soulseek_api, "_client", None)


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch):
    """Fail loudly if any test performs a real HTTP request.

    Nothing in the suite is supposed to reach the network. Tests that want to
    exercise HTTP behaviour monkeypatch the boundary themselves, which
    overrides this for their duration.
    """
    import requests

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "A test attempted a real outbound HTTP request. Mock the boundary "
            "(requests.post / the slskd client / the spotipy client) instead."
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked)


@pytest.fixture
def sandbox_dirs(tmp_path, monkeypatch):
    """Per-test download/playlist roots for code that touches the filesystem.

    Returns (downloads_dir, playlists_dir). Use this for anything exercising
    move_track_to_playlist_folder, startup_check, or the reject/delete path.
    """
    downloads = tmp_path / "downloads"
    playlists = tmp_path / "playlists"
    downloads.mkdir()
    playlists.mkdir()
    monkeypatch.setenv("SLSKD_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setenv("SLSKD_PLAYLISTS_DIR", str(playlists))
    return downloads, playlists
