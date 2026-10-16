"""Daemon orchestration behaviour: notifications, startup, liveness, shutdown.

These paths had no coverage at all, yet they decide whether the process
survives a dependency outage and whether Docker considers it healthy.
"""
import os
import threading

import pytest

from spotify_slsk import config, daemon, notify
from spotify_slsk.slskd import client as slskd_client
from spotify_slsk.spotify import client as spotify_client
from spotify_slsk.spotify import sync as spotify_sync


@pytest.fixture(autouse=True)
def _reset_shutdown():
    """Keep module-level daemon state from leaking between tests."""
    daemon._shutdown.clear()
    daemon._wake_event.clear()
    yield
    daemon._shutdown.clear()
    daemon._wake_event.clear()


class TestNtfyNotifications:
    """ntfy is optional; an unconfigured deployment must stay silent."""

    def test_unconfigured_sends_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(a))

        notify.send_ntfy_notification(None, None, "hello")
        notify.send_ntfy_notification("", "", "hello")
        notify.send_ntfy_notification("https://ntfy.sh", None, "hello")
        notify.send_ntfy_notification(None, "topic", "hello")

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

        monkeypatch.setattr(notify.requests, "post", _post)
        notify.send_ntfy_notification("https://ntfy.example", "mytopic", "hi")

        assert sent["url"] == "https://ntfy.example/mytopic"
        assert sent["data"] == "hi"


class TestSpotifyClientSetup:
    def test_returns_none_when_credentials_missing(self, monkeypatch):
        def _raise():
            raise Exception("You need to set your Spotify API credentials")

        monkeypatch.setattr(spotify_client, "SpotifyClientCredentials", _raise)

        # Must not raise: the dashboard has to come up so the operator can
        # see why nothing is syncing.
        assert spotify_client.setup_spotify_client() is None


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
            slskd_client, "get_client", lambda: self._client_always_failing()
        )
        # Previously this raised RuntimeError, which propagated out of main()
        # and killed the process — including the web UI.
        assert slskd_client.wait_for_slskd_healthy("http://x", "k", timeout=0) is False

    def test_returns_true_when_connected_and_logged_in(self, monkeypatch):
        class _App:
            def state(self):
                return {"server": {"isConnected": True, "isLoggedIn": True}}

        class _Client:
            application = _App()

        monkeypatch.setattr(
            slskd_client, "get_client", lambda: _Client())
        assert slskd_client.wait_for_slskd_healthy("http://x", "k", timeout=5) is True

    def test_not_logged_in_is_not_healthy(self, monkeypatch):
        class _App:
            def state(self):
                return {"server": {"isConnected": True, "isLoggedIn": False}}

        class _Client:
            application = _App()

        monkeypatch.setattr(
            slskd_client, "get_client", lambda: _Client())
        assert slskd_client.wait_for_slskd_healthy("http://x", "k", timeout=0) is False

    def test_block_until_healthy_exits_on_shutdown(self, monkeypatch):
        monkeypatch.setattr(daemon, "wait_for_slskd_healthy", lambda *a, **k: False)

        # Simulate SIGTERM arriving while we wait for the dependency.
        threading.Timer(0.05, daemon._shutdown.set).start()
        assert (
            daemon.block_until_slskd_healthy("http://x", "k", retry_seconds=0.01)
            is False
        )

    def test_block_until_healthy_keeps_heartbeat_fresh(self, monkeypatch, tmp_path):
        """A dependency outage is not our outage — don't let Docker restart us."""
        heartbeat = tmp_path / "heartbeat"
        monkeypatch.setattr(config, "HEARTBEAT_FILE", str(heartbeat))
        monkeypatch.setattr(daemon, "wait_for_slskd_healthy", lambda *a, **k: False)

        threading.Timer(0.05, daemon._shutdown.set).start()
        daemon.block_until_slskd_healthy("http://x", "k", retry_seconds=0.01)

        assert heartbeat.exists(), "heartbeat went stale while waiting for slskd"


class TestHeartbeatDuringWork:
    def test_touched_per_track_not_just_per_cycle(self, monkeypatch, tmp_path):
        """A slow cycle must stay distinguishable from a wedged one.

        One track can occupy minutes; HEARTBEAT_STALE_SECONDS defaults to 900.
        Touching only once per full cycle let a busy daemon be declared
        unhealthy and restarted mid-download.
        """
        heartbeat = tmp_path / "heartbeat"
        monkeypatch.setattr(config, "HEARTBEAT_FILE", str(heartbeat))

        touches = []
        real_touch = daemon._touch_heartbeat

        def _counting_touch():
            touches.append(1)
            real_touch()

        monkeypatch.setattr(daemon, "_touch_heartbeat", _counting_touch)
        monkeypatch.setattr(
            daemon, "fetch_and_compare_tracks", lambda conn, pid, sp: ([], "pl_x")
        )
        # process_playlist reads the stored name for the activity badge.
        monkeypatch.setattr(
            daemon, "get_playlist_meta", lambda conn, pid: (None, "pl_x", "X", None)
        )
        monkeypatch.setattr(
            daemon,
            "get_pending_tracks",
            lambda conn, name: [
                daemon.Track(f"t{i}", f"Song {i}", "Artist", "Album", "pl_x")
                for i in range(3)
            ],
        )
        monkeypatch.setattr(daemon, "handle_track_download", lambda *a, **k: True)
        monkeypatch.setattr(daemon, "_run_upgrade_pass", lambda conn, name: None)

        daemon.process_playlist(object(), object(), "pid", None, None)

        # once for the playlist + once per pending track
        assert len(touches) >= 4


class TestShutdownResponsiveness:
    def test_process_playlist_stops_between_tracks(self, monkeypatch):
        monkeypatch.setattr(
            daemon, "fetch_and_compare_tracks", lambda conn, pid, sp: ([], "pl_x")
        )
        # process_playlist reads the stored name for the activity badge.
        monkeypatch.setattr(
            daemon, "get_playlist_meta", lambda conn, pid: (None, "pl_x", "X", None)
        )
        monkeypatch.setattr(
            daemon,
            "get_pending_tracks",
            lambda conn, name: [
                daemon.Track(f"t{i}", f"Song {i}", "Artist", "Album", "pl_x")
                for i in range(5)
            ],
        )
        monkeypatch.setattr(daemon, "_run_upgrade_pass", lambda conn, name: None)

        attempted = []

        def _download(track, *a, **k):
            attempted.append(track.id)
            daemon._shutdown.set()  # SIGTERM arrives during the first download
            return True

        monkeypatch.setattr(daemon, "handle_track_download", _download)

        daemon.process_playlist(object(), object(), "pid", None, None)

        assert attempted == ["t0"], "daemon kept downloading after shutdown request"


class TestHeartbeatFile:
    def test_touch_writes_a_timestamp(self, monkeypatch, tmp_path):
        heartbeat = tmp_path / "hb"
        monkeypatch.setattr(config, "HEARTBEAT_FILE", str(heartbeat))
        daemon._touch_heartbeat()
        assert float(heartbeat.read_text()) > 0

    def test_touch_never_raises_on_unwritable_path(self, monkeypatch, tmp_path):
        # A wedged heartbeat must not itself crash the cycle.
        monkeypatch.setattr(config, "HEARTBEAT_FILE", str(tmp_path / "no-such-dir" / "hb")
        )
        daemon._touch_heartbeat()  # must not raise

    def test_uses_configured_path(self, monkeypatch, tmp_path):
        target = tmp_path / "custom-heartbeat"
        monkeypatch.setattr(config, "HEARTBEAT_FILE", str(target))
        daemon._touch_heartbeat()
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
        spotify_sync.migrate_env_playlists(conn, None)  # must not raise
        assert db.list_playlists(conn) == [], "imported a playlist with no client"
        conn.close()

    def test_startup_reconciliation_does_not_raise_without_a_client(self, tmp_path):
        from spotify_slsk import db

        conn = db.create_connection(str(tmp_path / "t.db"))
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", None)
        db.create_table(conn, "pl_x")
        daemon._run_startup_reconciliation(None, conn)  # must not raise
        conn.close()



def test_spotify_token_cache_goes_to_the_configured_path(monkeypatch, tmp_path):
    from spotify_slsk.spotify import client as spotify_client
    monkeypatch.setenv("SPOTIPY_CLIENT_ID", "id")
    monkeypatch.setenv("SPOTIPY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPOTIPY_CACHE_PATH", str(tmp_path / "tok"))
    sp = spotify_client.setup_spotify_client()
    assert sp.auth_manager.cache_handler.cache_path == str(tmp_path / "tok")


def test_sqlite_connect_is_not_logged_at_info(tmp_path, caplog):
    import logging

    from spotify_slsk import db
    with caplog.at_level(logging.INFO):
        db.create_connection(str(tmp_path / "x.db")).close()
    assert not [r for r in caplog.records if "Connected to SQLite" in r.getMessage()]


def test_missing_slskd_api_key_does_not_crash_the_wait(monkeypatch):
    """SlskdClient raises ValueError without an API key. That used to escape
    main() before the cycle loop - killing the web UI with it."""
    def _no_key():
        raise ValueError("Please provide an API-Key, a valid token or username/password.")

    monkeypatch.setattr(slskd_client, "get_client", _no_key)
    assert slskd_client.wait_for_slskd_healthy("h", None, timeout=0.05, check_interval=0.01) is False


def test_requested_upgrade_checks_bypass_the_interval(tmp_path, monkeypatch):
    from spotify_slsk import daemon, db, runtime_state

    conn = db.create_connection(str(tmp_path / "u.db"))
    db.set_setting(conn, "UPGRADE_CHECK_INTERVAL_HOURS", "0")  # pass disabled
    db.create_table(conn, "pl_x")
    db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Alb"))
    db.insert_track(conn, "pl_x", ("t2", "Other", "Artist", "Alb"))
    db.update_download_status(conn, "t1", "pl_x", success=True, file_path="/m/t1.flac")
    seen = []
    monkeypatch.setattr(
        daemon, "try_upgrade_track",
        lambda track, table, c, path: seen.append((track.id, table, path)),
    )
    runtime_state.pop_upgrade_checks()
    runtime_state.enqueue_upgrade_check("pl_x", "t1")
    runtime_state.enqueue_upgrade_check("pl_x", "t1")  # de-duplicated
    runtime_state.enqueue_upgrade_check("pl_x", "t2")  # not downloaded: skipped
    daemon._process_upgrade_requests(conn)
    assert seen == [("t1", "pl_x", "/m/t1.flac")]
    stamped = conn.execute("SELECT last_upgrade_check FROM pl_x WHERE id = 't1'").fetchone()[0]
    assert stamped is not None
    assert runtime_state.pop_upgrade_checks() == []
    conn.close()


class TestBetweenPlaylists:
    """A long download cycle must not starve the library scan, the quality
    backfill or the UI's queued requests (prod: a scan requested hours ago
    had checked one file)."""

    def test_scan_and_backfill_get_a_small_step_after_each_playlist(self, monkeypatch, tmp_path):
        from spotify_slsk import db
        from spotify_slsk.library import health

        conn = db.create_connection(str(tmp_path / "t.db"))
        db.set_setting(conn, health.SCAN_PENDING_KEY, "1")
        calls = []
        monkeypatch.setattr(
            daemon, "process_playlist",
            lambda sp, c, pid, *a: calls.append(("playlist", pid)) or {"tracks_downloaded": 1},
        )
        monkeypatch.setattr(
            health, "scan_step",
            lambda c, **k: calls.append(("scan", k["max_files"], k["max_seconds"])) or (0, False),
        )
        monkeypatch.setattr(
            health, "backfill_qualities",
            lambda c, max_files=500, **k: calls.append(("backfill", max_files)) or 0,
        )
        monkeypatch.setattr(daemon, "_drain_queues", lambda c: calls.append(("queues",)))
        monkeypatch.setattr(daemon, "_run_scheduled", lambda c: None)

        totals = daemon._run_cycle(None, conn, [{"playlist_id": "a"}, {"playlist_id": "b"}], None, None)

        small = ("scan", daemon.BETWEEN_PLAYLISTS_SCAN_FILES, daemon.BETWEEN_PLAYLISTS_SCAN_SECONDS)
        assert calls == [
            ("queues",),
            ("playlist", "a"), ("queues",), small, ("backfill", daemon.BETWEEN_PLAYLISTS_BACKFILL_FILES),
            ("playlist", "b"), ("queues",), small, ("backfill", daemon.BETWEEN_PLAYLISTS_BACKFILL_FILES),
            ("scan", health.MAX_FILES_PER_STEP, health.MAX_SECONDS_PER_STEP), ("backfill", 500),
        ]
        assert totals["tracks_downloaded"] == 2
        conn.close()

    def test_drain_queues_runs_every_ui_queue(self, monkeypatch):
        ran = []
        for name in ("_process_quarantines", "_process_renames",
                     "_process_manual_grabs", "_process_upgrade_requests"):
            monkeypatch.setattr(daemon, name, lambda c, n=name: ran.append(n))
        daemon._drain_queues(object())
        assert ran == ["_process_quarantines", "_process_renames",
                       "_process_manual_grabs", "_process_upgrade_requests"]
