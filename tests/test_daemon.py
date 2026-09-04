"""Daemon orchestration behaviour: notifications, startup, liveness, shutdown.

These paths had no coverage at all, yet they decide whether the process
survives a dependency outage and whether Docker considers it healthy.
"""
import os
import threading

import pytest

from spotify_slsk import app


@pytest.fixture(autouse=True)
def _reset_shutdown():
    """Keep module-level daemon state from leaking between tests."""
    app._shutdown.clear()
    app._wake_event.clear()
    yield
    app._shutdown.clear()
    app._wake_event.clear()


class TestNtfyNotifications:
    """ntfy is optional; an unconfigured deployment must stay silent."""

    def test_unconfigured_sends_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app.requests, "post", lambda *a, **k: calls.append(a))

        app.send_ntfy_notification(None, None, "hello")
        app.send_ntfy_notification("", "", "hello")
        app.send_ntfy_notification("https://ntfy.sh", None, "hello")
        app.send_ntfy_notification(None, "topic", "hello")

        # Previously this POSTed to the literal URL "None/None", raised, and
        # logged an ERROR on every playlist of every cycle.
        assert calls == []

    def test_configured_posts_to_url_and_topic(self, monkeypatch):
        sent = {}

        class _Resp:
            status_code = 200
            text = "ok"

        def _post(url, data=None, **kwargs):
            sent["url"] = url
            sent["data"] = data
            return _Resp()

        monkeypatch.setattr(app.requests, "post", _post)
        app.send_ntfy_notification("https://ntfy.example", "mytopic", "hi")

        assert sent["url"] == "https://ntfy.example/mytopic"
        assert sent["data"] == "hi"


class TestSpotifyClientSetup:
    def test_returns_none_when_credentials_missing(self, monkeypatch):
        def _raise():
            raise Exception("You need to set your Spotify API credentials")

        monkeypatch.setattr(app, "SpotifyClientCredentials", _raise)

        # Must not raise: the dashboard has to come up so the operator can
        # see why nothing is syncing.
        assert app.setup_spotify_client() is None


class TestSlskdHealthWait:
    """An slskd outage must not take the daemon down with it."""

    def _client_always_failing(self):
        class _App:
            def state(self):
                raise ConnectionError("slskd unreachable")

        class _Client:
            application = _App()

        return _Client()

    def test_returns_false_on_timeout_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(
            app, "get_slskd_client", lambda: self._client_always_failing()
        )
        # Previously this raised RuntimeError, which propagated out of main()
        # and killed the process — including the web UI.
        assert app.wait_for_slskd_healthy("http://x", "k", timeout=0) is False

    def test_returns_true_when_connected_and_logged_in(self, monkeypatch):
        class _App:
            def state(self):
                return {"server": {"isConnected": True, "isLoggedIn": True}}

        class _Client:
            application = _App()

        monkeypatch.setattr(app, "get_slskd_client", lambda: _Client())
        assert app.wait_for_slskd_healthy("http://x", "k", timeout=5) is True

    def test_not_logged_in_is_not_healthy(self, monkeypatch):
        class _App:
            def state(self):
                return {"server": {"isConnected": True, "isLoggedIn": False}}

        class _Client:
            application = _App()

        monkeypatch.setattr(app, "get_slskd_client", lambda: _Client())
        assert app.wait_for_slskd_healthy("http://x", "k", timeout=0) is False

    def test_block_until_healthy_exits_on_shutdown(self, monkeypatch):
        monkeypatch.setattr(app, "wait_for_slskd_healthy", lambda *a, **k: False)

        # Simulate SIGTERM arriving while we wait for the dependency.
        threading.Timer(0.05, app._shutdown.set).start()
        assert (
            app.block_until_slskd_healthy("http://x", "k", retry_seconds=0.01)
            is False
        )

    def test_block_until_healthy_keeps_heartbeat_fresh(self, monkeypatch, tmp_path):
        """A dependency outage is not our outage — don't let Docker restart us."""
        heartbeat = tmp_path / "heartbeat"
        monkeypatch.setattr(app, "HEARTBEAT_FILE", str(heartbeat))
        monkeypatch.setattr(app, "wait_for_slskd_healthy", lambda *a, **k: False)

        threading.Timer(0.05, app._shutdown.set).start()
        app.block_until_slskd_healthy("http://x", "k", retry_seconds=0.01)

        assert heartbeat.exists(), "heartbeat went stale while waiting for slskd"


class TestHeartbeatDuringWork:
    def test_touched_per_track_not_just_per_cycle(self, monkeypatch, tmp_path):
        """A slow cycle must stay distinguishable from a wedged one.

        One track can occupy minutes; HEARTBEAT_STALE_SECONDS defaults to 900.
        Touching only once per full cycle let a busy daemon be declared
        unhealthy and restarted mid-download.
        """
        heartbeat = tmp_path / "heartbeat"
        monkeypatch.setattr(app, "HEARTBEAT_FILE", str(heartbeat))

        touches = []
        real_touch = app._touch_heartbeat

        def _counting_touch():
            touches.append(1)
            real_touch()

        monkeypatch.setattr(app, "_touch_heartbeat", _counting_touch)
        monkeypatch.setattr(
            app, "fetch_and_compare_tracks", lambda conn, pid, sp: ([], "pl_x")
        )
        monkeypatch.setattr(
            app,
            "get_pending_tracks",
            lambda conn, name: [
                app.Track(f"t{i}", f"Song {i}", "Artist", "Album", "pl_x")
                for i in range(3)
            ],
        )
        monkeypatch.setattr(app, "handle_track_download", lambda *a, **k: True)
        monkeypatch.setattr(app, "_run_upgrade_pass", lambda conn, name: None)

        app.process_playlist(object(), object(), "pid", None, None)

        # once for the playlist + once per pending track
        assert len(touches) >= 4


class TestShutdownResponsiveness:
    def test_process_playlist_stops_between_tracks(self, monkeypatch):
        monkeypatch.setattr(
            app, "fetch_and_compare_tracks", lambda conn, pid, sp: ([], "pl_x")
        )
        monkeypatch.setattr(
            app,
            "get_pending_tracks",
            lambda conn, name: [
                app.Track(f"t{i}", f"Song {i}", "Artist", "Album", "pl_x")
                for i in range(5)
            ],
        )
        monkeypatch.setattr(app, "_run_upgrade_pass", lambda conn, name: None)

        attempted = []

        def _download(track, *a, **k):
            attempted.append(track.id)
            app._shutdown.set()  # SIGTERM arrives during the first download
            return True

        monkeypatch.setattr(app, "handle_track_download", _download)

        app.process_playlist(object(), object(), "pid", None, None)

        assert attempted == ["t0"], "daemon kept downloading after shutdown request"


class TestHeartbeatFile:
    def test_touch_writes_a_timestamp(self, monkeypatch, tmp_path):
        heartbeat = tmp_path / "hb"
        monkeypatch.setattr(app, "HEARTBEAT_FILE", str(heartbeat))
        app._touch_heartbeat()
        assert float(heartbeat.read_text()) > 0

    def test_touch_never_raises_on_unwritable_path(self, monkeypatch, tmp_path):
        # A wedged heartbeat must not itself crash the cycle.
        monkeypatch.setattr(
            app, "HEARTBEAT_FILE", str(tmp_path / "no-such-dir" / "hb")
        )
        app._touch_heartbeat()  # must not raise

    def test_uses_configured_path(self, monkeypatch, tmp_path):
        target = tmp_path / "custom-heartbeat"
        monkeypatch.setattr(app, "HEARTBEAT_FILE", str(target))
        app._touch_heartbeat()
        assert os.path.exists(target)


class TestMissingSpotifyCredentials:
    """Losing credentials must degrade, not crash.

    setup_spotify_client returns None so the dashboard still boots. These pin
    the behaviour of the startup paths that run before the cycle loop's own
    guard — a reviewer flagged them as an uncaught crash, so the no-raise
    contract is worth asserting rather than re-deriving.
    """

    def test_env_playlist_import_does_not_raise_without_a_client(
        self, tmp_path, monkeypatch
    ):
        from spotify_slsk import db

        monkeypatch.setenv(
            "SPOTIFY_PLAYLIST_URLS", "https://open.spotify.com/playlist/abc"
        )
        conn = db.create_connection(str(tmp_path / "t.db"))
        app._migrate_env_playlists(conn, None)  # must not raise
        assert db.list_playlists(conn) == [], "imported a playlist with no client"
        conn.close()

    def test_startup_reconciliation_does_not_raise_without_a_client(self, tmp_path):
        from spotify_slsk import db

        conn = db.create_connection(str(tmp_path / "t.db"))
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", None)
        db.create_table(conn, "pl_x")
        app._run_startup_reconciliation(None, conn)  # must not raise
        conn.close()

