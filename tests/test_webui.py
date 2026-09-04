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

from spotify_slsk import db
from spotify_slsk.webui import create_app


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


def post(client, url, data=None, **kwargs):
    """POST the way a real browser form does: carrying a valid CSRF token.

    The mutating routes reject tokenless POSTs (see TestCsrfProtection). These
    tests are legitimate clients, so they present a token rather than having
    the protection relaxed for them.
    """
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
        if not token:
            token = "test-csrf-token"
            sess["_csrf_token"] = token
    payload = dict(data or {})
    payload.setdefault("csrf_token", token)
    return client.post(url, data=payload, **kwargs)



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
        r = post(client,
            "/playlists",
            data={"playlist_url": "not a url"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"doesn" in r.data  # "doesn't look like"

    def test_add_valid_url_without_spotify_client(self, client):
        # spotify_client=None means add_playlist uses the id as the name
        url = "https://open.spotify.com/playlist/abc123"
        r = post(client,
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

        r = post(client, "/playlist/pid1/toggle", follow_redirects=True)
        assert r.status_code == 200

        r = post(client, "/playlist/pid1/delete", follow_redirects=True)
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
        r = post(client,
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
        post(client,
            "/settings",
            data={"MIN_MATCH_SCORE": "0.9"},
            follow_redirects=True,
        )
        post(client,
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

        post(client, "/track/pid1/t1/retry", follow_redirects=True)

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
        post(client, "/track/pid1/t1/retry", follow_redirects=True)
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
        post(client, "/playlist/pid1/refresh", follow_redirects=True)
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
        post(client, "/playlist/pid1/toggle", follow_redirects=True)  # disables it
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
        post(client, "/playlist/pid1/toggle", follow_redirects=True)  # re-enables
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
        r = post(client, "/track/pid1/t1/retry", follow_redirects=True)
        assert r.status_code == 200


class TestCsrfProtection:
    """The UI has no auth, so the browser is a confused deputy.

    Any page the operator has open in another tab can POST to the dashboard's
    origin. Loopback binding does not help — the browser is inside the trust
    boundary — so every state-changing route requires a session-backed token.
    """

    MUTATING_ROUTES = [
        ("/playlists", {"playlist_url": "https://open.spotify.com/playlist/abc"}),
        ("/playlist/pid1/toggle", {}),
        ("/playlist/pid1/delete", {}),
        ("/playlist/pid1/refresh", {}),
        ("/track/pid1/t1/retry", {}),
        ("/settings", {"MIN_MATCH_SCORE": "0.9"}),
    ]

    @pytest.mark.parametrize("route,data", MUTATING_ROUTES)
    def test_post_without_token_is_rejected(self, client, route, data):
        r = client.post(route, data=data)
        assert r.status_code == 400, f"{route} accepted a tokenless POST"

    @pytest.mark.parametrize("route,data", MUTATING_ROUTES)
    def test_post_with_wrong_token_is_rejected(self, client, route, data):
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "the-real-token"
        payload = dict(data)
        payload["csrf_token"] = "attacker-guess"
        r = client.post(route, data=payload)
        assert r.status_code == 400, f"{route} accepted a forged token"

    def test_delete_playlist_is_not_reachable_without_a_token(self, app_tmp, client):
        """The concrete attack: a cross-site POST silently dropping a playlist."""
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", None)
        conn.close()

        r = client.post("/playlist/pid1/delete")
        assert r.status_code == 400

        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert len(db.list_playlists(conn)) == 1, "playlist was deleted by a forged POST"
        conn.close()

    def test_get_requests_need_no_token(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/settings").status_code == 200

    def test_token_is_rendered_into_forms(self, client):
        r = client.get("/settings")
        assert b'name="csrf_token"' in r.data


class TestSecurityHeaders:
    def test_headers_present(self, client):
        r = client.get("/")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]

    def test_csp_allows_no_third_party_script_origin(self, client):
        """Assets are self-hosted, so the CSP must not need a CDN exemption."""
        csp = client.get("/").headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "unpkg" not in csp and "jsdelivr" not in csp


class TestSecretKeyPersistence:
    def test_generated_key_survives_a_restart(self, app_tmp, monkeypatch):
        """CSRF tokens live in the session; a new key per boot would 403 users."""
        monkeypatch.delenv("UI_SECRET_KEY", raising=False)
        db_path = app_tmp.config["DB_PATH"]
        first = create_app(db_path, spotify_client=None).secret_key
        second = create_app(db_path, spotify_client=None).secret_key
        assert first == second and first

    def test_env_var_wins_over_stored_key(self, app_tmp, monkeypatch):
        monkeypatch.setenv("UI_SECRET_KEY", "operator-managed-key")
        assert (
            create_app(app_tmp.config["DB_PATH"], spotify_client=None).secret_key
            == "operator-managed-key"
        )

    def test_secret_key_is_not_exposed_on_the_settings_page(self, app_tmp, client):
        """It is stored in the settings table; it must not render there."""
        create_app(app_tmp.config["DB_PATH"], spotify_client=None)
        body = client.get("/settings").data
        assert b"_ui_secret_key" not in body


class TestSettingsValidation:
    """A setting that silently fails to apply is worse than no setting.

    _reload_settings keeps the previous value when a stored value won't
    parse, so an unvalidated save reported success for a value the daemon
    then ignored forever.
    """

    def test_non_numeric_score_is_rejected(self, app_tmp, client):
        r = post(client, "/settings", data={"MIN_MATCH_SCORE": "garbage"},
                 follow_redirects=True)
        assert b"must be a number" in r.data

        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.list_settings(conn).get("MIN_MATCH_SCORE") is None
        conn.close()

    def test_out_of_range_score_is_rejected(self, client):
        r = post(client, "/settings", data={"MIN_MATCH_SCORE": "1.5"},
                 follow_redirects=True)
        assert b"between 0 and 1" in r.data

    def test_negative_integer_is_rejected(self, client):
        r = post(client, "/settings", data={"SLSKD_MIN_EFFECTIVE_MP3_KBPS": "-5"},
                 follow_redirects=True)
        assert b"must be &gt;= 0" in r.data or b"must be >= 0" in r.data

    def test_zero_rejected_where_minimum_is_one(self, client):
        r = post(client, "/settings", data={"SLSKD_WAIT_TIMEOUT": "0"},
                 follow_redirects=True)
        assert b"must be" in r.data

    def test_unknown_audio_format_is_rejected(self, client):
        r = post(client, "/settings", data={"SLSKD_PREFERRED_FORMATS": "flac,exe"},
                 follow_redirects=True)
        assert b"unsupported format" in r.data

    def test_formats_are_normalised(self, app_tmp, client):
        post(client, "/settings",
             data={"SLSKD_PREFERRED_FORMATS": " .FLAC , MP3 "},
             follow_redirects=True)
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.list_settings(conn)["SLSKD_PREFERRED_FORMATS"] == "flac,mp3"
        conn.close()

    def test_one_bad_field_does_not_half_apply_the_form(self, app_tmp, client):
        r = post(client, "/settings",
                 data={"MIN_MATCH_SCORE": "0.8", "SLSKD_WAIT_TIMEOUT": "nope"},
                 follow_redirects=True)
        assert r.status_code == 200
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        stored = db.list_settings(conn)
        conn.close()
        assert stored.get("MIN_MATCH_SCORE") is None, "valid field applied despite an invalid one"

    def test_valid_values_still_save(self, app_tmp, client):
        post(client, "/settings",
             data={"MIN_MATCH_SCORE": "0.75", "SLSKD_WAIT_TIMEOUT": "90"},
             follow_redirects=True)
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        stored = db.list_settings(conn)
        conn.close()
        assert stored["MIN_MATCH_SCORE"] == "0.75"
        assert stored["SLSKD_WAIT_TIMEOUT"] == "90"

    def test_removed_dead_knob_is_not_offered(self, client):
        """SLSKD_MAX_RETRIES was configurable but wired to nothing."""
        assert b"SLSKD_MAX_RETRIES" not in client.get("/settings").data


class TestHealthz:
    """The endpoint Docker and the operator rely on to spot a wedged daemon."""

    def test_ok_when_schema_current_and_no_heartbeat_yet(self, client):
        body = client.get("/healthz").get_json()
        assert body["ok"] is True
        assert body["schema_version"] == db.SCHEMA_VERSION

    def test_fresh_heartbeat_is_healthy(self, client, tmp_path, monkeypatch):
        hb = tmp_path / "hb"
        hb.write_text("now")
        monkeypatch.setenv("HEARTBEAT_FILE", str(hb))
        monkeypatch.setenv("HEARTBEAT_STALE_SECONDS", "900")

        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert r.get_json()["heartbeat_age_seconds"] < 900

    def test_stale_heartbeat_reports_503(self, client, tmp_path, monkeypatch):
        hb = tmp_path / "hb"
        hb.write_text("old")
        os.utime(hb, (0, 0))  # epoch: definitively stale
        monkeypatch.setenv("HEARTBEAT_FILE", str(hb))
        monkeypatch.setenv("HEARTBEAT_STALE_SECONDS", "900")

        r = client.get("/healthz")
        assert r.status_code == 503
        assert r.get_json()["ok"] is False

    def test_healthz_needs_no_csrf_token(self, client):
        assert client.get("/healthz").status_code in (200, 503)


class TestDatabaseUnavailable:
    def test_corrupt_database_returns_503_not_an_empty_page(self, tmp_path):
        """A connection that only *looks* usable would render empty pages."""
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"not a sqlite file" * 50)
        app = create_app(str(bad), spotify_client=None)
        app.config.update(TESTING=True)

        r = app.test_client().get("/")
        assert r.status_code == 503


class TestInternalSettingsAreNotExposed:
    def test_overrides_passed_to_the_template_exclude_private_keys(self, app_tmp, client):
        """The secret key lives in the settings table; keep it out of the view."""
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.set_setting(conn, "_ui_secret_key", "super-secret-value")
        db.set_setting(conn, "MIN_MATCH_SCORE", "0.8")
        conn.close()

        body = client.get("/settings").data
        assert b"super-secret-value" not in body
        assert b"_ui_secret_key" not in body
        assert b"0.8" in body, "ordinary overrides must still render"
