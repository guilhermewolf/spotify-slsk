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
