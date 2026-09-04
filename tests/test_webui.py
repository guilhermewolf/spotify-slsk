"""Flask test-client smoke tests for the webui.

These stand up a fresh app against a throwaway SQLite DB, walk the main
routes, and verify that playlist CRUD + settings round-trip through the
templates without 500ing.
"""
import os
import tempfile

os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

import pytest

import db
from webui import create_app


@pytest.fixture
def app_tmp():
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    # Touch the schema by opening a connection once
    conn = db.create_connection(db_path)
    assert conn is not None
    conn.close()
    app = create_app(db_path, spotify_client=None)
    app.config.update(TESTING=True)
    yield app


@pytest.fixture
def client(app_tmp):
    return app_tmp.test_client()


class TestDashboard:
    def test_empty_dashboard_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"No playlists yet" in r.data

    def test_healthz_returns_json(self, client):
        r = client.get("/healthz")
        assert r.headers["Content-Type"].startswith("application/json")
        assert "schema_version" in r.get_json()


class TestPlaylistCrud:
    def test_add_invalid_url_flashes_error(self, client):
        r = client.post(
            "/playlists",
            data={"playlist_url": "not a url"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"doesn" in r.data  # "doesn't look like"

    def test_add_valid_url_without_spotify_client(self, client):
        # spotify_client=None means add_playlist uses the id as the name
        url = "https://open.spotify.com/playlist/abc123"
        r = client.post(
            "/playlists",
            data={"playlist_url": url},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Added" in r.data or b"abc123" in r.data

    def test_toggle_then_delete(self, app_tmp, client):
        with app_tmp.app_context():
            conn = db.create_connection(app_tmp.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "snap")
            db.create_table(conn, "pl_x")
            conn.close()

        r = client.post("/playlist/pid1/toggle", follow_redirects=True)
        assert r.status_code == 200

        r = client.post("/playlist/pid1/delete", follow_redirects=True)
        assert r.status_code == 200
        assert b"removed" in r.data.lower()

    def test_playlist_detail_404s_for_unknown(self, client):
        r = client.get("/playlist/does-not-exist")
        assert r.status_code == 404


class TestSettings:
    def test_settings_form_renders(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        assert b"MIN_MATCH_SCORE" in r.data

    def test_save_and_read_back(self, client):
        r = client.post(
            "/settings",
            data={"MIN_MATCH_SCORE": "0.75", "SLSKD_PREFERRED_FORMATS": "flac"},
            follow_redirects=True,
        )
        assert r.status_code == 200

        r = client.get("/settings")
        assert b'value="0.75"' in r.data
        assert b'value="flac"' in r.data

    def test_blank_field_deletes_override(self, app_tmp, client):
        # set, then clear
        client.post(
            "/settings",
            data={"MIN_MATCH_SCORE": "0.9"},
            follow_redirects=True,
        )
        client.post(
            "/settings",
            data={"MIN_MATCH_SCORE": ""},
            follow_redirects=True,
        )
        with app_tmp.app_context():
            conn = db.create_connection(app_tmp.config["DB_PATH"])
            assert "MIN_MATCH_SCORE" not in db.list_settings(conn)
            conn.close()


class TestTrackDetail:
    def test_track_detail_404s_for_unknown(self, client):
        r = client.get("/track/nope/nope")
        assert r.status_code == 404

    def test_retry_clears_attempts(self, app_tmp, client):
        with app_tmp.app_context():
            conn = db.create_connection(app_tmp.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
            db.create_table(conn, "pl_x")
            db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"))
            db.update_download_status(conn, "t1", "pl_x", success=False)
            db.update_download_status(conn, "t1", "pl_x", success=False)
            db.add_tried_file(conn, "pl_x", "t1", "bad.mp3")
            conn.close()

        client.post("/track/pid1/t1/retry", follow_redirects=True)

        with app_tmp.app_context():
            conn = db.create_connection(app_tmp.config["DB_PATH"])
            row = conn.execute(
                "SELECT attempts, suspended_until FROM pl_x WHERE id='t1'"
            ).fetchone()
            assert row == (0, None)
            assert db.get_tried_files(conn, "pl_x", "t1") == []
            conn.close()


class TestWakeCallback:
    """The webui invokes wake_callback after state-changing requests so the
    daemon's cycle sleep is interrupted and the change is observable in
    seconds rather than after the full cycle interval."""

    def _setup(self, tmpdir, calls):
        db_path = os.path.join(tmpdir, "test.db")
        conn = db.create_connection(db_path)
        conn.close()
        app = create_app(db_path, spotify_client=None, wake_callback=lambda: calls.append(1))
        app.config.update(TESTING=True)
        return app

    def test_retry_invokes_wake(self):
        tmpdir = tempfile.mkdtemp()
        calls = []
        app = self._setup(tmpdir, calls)
        with app.app_context():
            conn = db.create_connection(app.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
            db.create_table(conn, "pl_x")
            db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"))
            conn.close()
        client = app.test_client()
        client.post("/track/pid1/t1/retry", follow_redirects=True)
        assert calls == [1]

    def test_refresh_invokes_wake(self):
        tmpdir = tempfile.mkdtemp()
        calls = []
        app = self._setup(tmpdir, calls)
        with app.app_context():
            conn = db.create_connection(app.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
            db.create_table(conn, "pl_x")
            conn.close()
        client = app.test_client()
        client.post("/playlist/pid1/refresh", follow_redirects=True)
        assert calls == [1]

    def test_toggle_disable_does_not_wake(self):
        # Disabling a playlist shouldn't wake — there's nothing new to do.
        tmpdir = tempfile.mkdtemp()
        calls = []
        app = self._setup(tmpdir, calls)
        with app.app_context():
            conn = db.create_connection(app.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")  # enabled=1 by default
            conn.close()
        client = app.test_client()
        client.post("/playlist/pid1/toggle", follow_redirects=True)  # disables it
        assert calls == []

    def test_toggle_enable_wakes(self):
        tmpdir = tempfile.mkdtemp()
        calls = []
        app = self._setup(tmpdir, calls)
        with app.app_context():
            conn = db.create_connection(app.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
            db.set_playlist_enabled(conn, "pid1", False)  # start disabled
            conn.close()
        client = app.test_client()
        client.post("/playlist/pid1/toggle", follow_redirects=True)  # re-enables
        assert calls == [1]

    def test_missing_callback_is_noop(self):
        # Daemon may run with wake_callback=None (e.g. in tests) — must not crash.
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        db.create_connection(db_path).close()
        app = create_app(db_path, spotify_client=None, wake_callback=None)
        app.config.update(TESTING=True)
        with app.app_context():
            conn = db.create_connection(db_path)
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
            db.create_table(conn, "pl_x")
            db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"))
            conn.close()
        client = app.test_client()
        r = client.post("/track/pid1/t1/retry", follow_redirects=True)
        assert r.status_code == 200


class TestLiveObservability:
    def test_activity_json_returns_snapshot(self, client):
        import runtime_state
        runtime_state.set_activity("idle", detail="from test")
        r = client.get("/activity.json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["phase"] == "idle"
        assert data["detail"] == "from test"
        assert "age_seconds" in data

    def test_logs_json_increments_seq(self, client):
        import logging
        import runtime_state
        runtime_state._log_buffer.clear()
        runtime_state._log_seq = 0
        # Install handler so log calls land in the buffer regardless of root config.
        handler = runtime_state.RingBufferHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        try:
            logging.getLogger().warning("from test_logs_json")
            r = client.get("/logs.json?since=0")
            assert r.status_code == 200
            data = r.get_json()
            assert data["last_seq"] >= 1
            assert any("from test_logs_json" in e["message"] for e in data["entries"])
        finally:
            logging.getLogger().removeHandler(handler)

    def test_logs_page_renders(self, client):
        r = client.get("/logs")
        assert r.status_code == 200
        assert b"log-viewer" in r.data

    def test_dashboard_renders_activity_banner(self, client):
        import runtime_state
        runtime_state.set_activity("syncing", detail="testing")
        r = client.get("/")
        assert r.status_code == 200
        assert b"activity-banner" in r.data
        assert b"testing" in r.data

    def test_dashboard_renders_recent_cycles(self, app_tmp, client):
        import time as _time
        with app_tmp.app_context():
            conn = db.create_connection(app_tmp.config["DB_PATH"])
            db.record_cycle(
                conn,
                started_at=_time.time(),
                duration_seconds=1.0,
                playlists_synced=1,
                tracks_added=2,
                tracks_downloaded=3,
                tracks_failed=0,
            )
            conn.close()
        r = client.get("/")
        assert r.status_code == 200
        assert b"Recent cycles" in r.data


class TestExtraEndpoints:
    def test_test_ntfy_without_env_flashes_error(self, client, monkeypatch):
        monkeypatch.delenv("NTFY_URL", raising=False)
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        r = client.post("/settings/test-ntfy", follow_redirects=True)
        assert r.status_code == 200
        assert b"not set" in r.data

    def test_serve_file_rejects_traversal(self, client):
        r = client.get("/file?rel=../../etc/passwd")
        assert r.status_code == 404

    def test_serve_file_rejects_missing_rel(self, client):
        assert client.get("/file").status_code == 400

    def test_serve_file_returns_audio(self, client, tmp_path, monkeypatch):
        # Create a fake "downloaded" file under a tmp playlists root and verify
        # the route streams it back. We shadow SLSKD_PLAYLISTS_DIR so the
        # path-resolution logic accepts our temp tree.
        monkeypatch.setenv("SLSKD_PLAYLISTS_DIR", str(tmp_path))
        f = tmp_path / "pl_x" / "song.mp3"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"\xff\xfb\x90\x44")  # tiny MP3-ish header
        r = client.get("/file?rel=pl_x/song.mp3")
        assert r.status_code == 200
        assert r.data == b"\xff\xfb\x90\x44"

    def test_retag_404s_for_unknown_track(self, client):
        r = client.post("/track/nope/nope/retag", follow_redirects=False)
        assert r.status_code == 404

    def test_retag_flashes_when_file_missing(self, app_tmp, client):
        with app_tmp.app_context():
            conn = db.create_connection(app_tmp.config["DB_PATH"])
            db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
            db.create_table(conn, "pl_x")
            db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"))
            db.update_download_status(
                conn, "t1", "pl_x", success=True, file_path="/no/such/file.mp3"
            )
            conn.close()
        r = client.post("/track/pid1/t1/retag", follow_redirects=True)
        assert r.status_code == 200
        assert b"missing" in r.data.lower()

