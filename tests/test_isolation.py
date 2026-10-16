"""Guards on the test environment itself.

These assert that the isolation in conftest.py is actually in force. Without
them, a future change that weakens conftest would go unnoticed: the rest of
the suite would keep passing while quietly running against production
configuration.
"""
import os

import pytest

from spotify_slsk.slskd import client as slskd_client


class TestEnvironmentIsolation:
    """The sandbox must win over whatever the developer's shell exports."""

    def test_slskd_host_is_not_a_real_host(self):
        assert os.environ["SLSKD_HOST_URL"] == "http://slskd.invalid:5030"

    def test_credentials_are_obviously_fake(self):
        for key in (
            "SLSKD_API_KEY",
            "SPOTIPY_CLIENT_ID",
            "SPOTIPY_CLIENT_SECRET",
        ):
            assert "not-real" in os.environ[key], f"{key} is not a sandbox value"

    def test_playlists_dir_is_a_temp_dir_not_the_music_library(self):
        # importer.process._reject_and_log will os.remove() from under this root, so it
        # must never point at a real library.
        playlists = os.environ["SLSKD_PLAYLISTS_DIR"]
        assert "isolated-env" in playlists
        assert os.path.isdir(playlists)

    def test_downloads_dir_is_a_temp_dir(self):
        downloads = os.environ["SLSKD_DOWNLOADS_DIR"]
        assert "isolated-env" in downloads
        assert os.path.isdir(downloads)

    def test_artwork_cache_is_a_temp_dir_not_the_data_volume(self):
        from spotify_slsk.importer import artwork

        assert "isolated-env" in artwork.cache_dir()

    def test_ntfy_disabled_so_no_notifications_are_sent(self):
        assert os.environ["NTFY_URL"] == ""
        assert os.environ["NTFY_TOPIC"] == ""

    def test_webhook_subsonic_and_ui_login_are_off(self):
        for key in ("WEBHOOK_URL", "SUBSONIC_URL", "SUBSONIC_PASSWORD",
                    "UI_AUTH_USERNAME", "UI_AUTH_PASSWORD"):
            assert os.environ[key] == "", key


class TestOutboundBoundariesAreBlocked:
    """Reaching a real external system must fail loudly, not silently work."""

    def test_building_a_real_slskd_client_raises(self):
        with pytest.raises(AssertionError, match="real slskd client"):
            slskd_client.get_client()

    def test_real_http_request_raises(self):
        import requests

        with pytest.raises(AssertionError, match="real outbound HTTP request"):
            requests.get("http://example.invalid")


class TestSandboxDirsFixture:
    def test_provides_isolated_roots(self, sandbox_dirs, tmp_path):
        downloads, playlists = sandbox_dirs
        assert downloads.is_dir() and playlists.is_dir()
        assert os.environ["SLSKD_DOWNLOADS_DIR"] == str(downloads)
        assert os.environ["SLSKD_PLAYLISTS_DIR"] == str(playlists)
        # Both live under pytest's per-test tmp_path, so nothing can escape.
        assert str(downloads).startswith(str(tmp_path))
