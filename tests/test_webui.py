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

        # Without the confirm step nothing is deleted.
        r = post(client, "/playlist/pid1/delete", follow_redirects=True)
        assert b"not confirmed" in r.data
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.get_playlist_meta(conn, "pid1")[1] == "pl_x"
        conn.close()

        r = post(client, "/playlist/pid1/delete", {"confirm": "1"}, follow_redirects=True)
        assert r.status_code == 200
        assert b"removed" in r.data.lower()

    def test_dashboard_remove_uses_no_inline_handler(self, app_tmp, client):
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "snap")
        conn.close()
        html = client.get("/").data
        assert b"onsubmit" not in html and b"onclick" not in html
        assert b'name="confirm" value="1"' in html

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
        ("/blocklist/1/delete", {}),
        ("/blocklist/ban-user", {"username": "x"}),
        ("/track/pid1/t1/search", {}),
        ("/track/pid1/t1/grab", {"username": "u", "filename": "f", "size": "1"}),
        ("/wanted/search", {"all": "1"}),
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

    config.reload keeps the previous value when a stored value won't
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

    def test_every_setting_is_rendered(self, client):
        # The form clears every key it does not send, so an unrendered key
        # had its override silently deleted on the next save.
        from spotify_slsk.config import SETTINGS_SPEC

        r = client.get("/settings")
        for key, *_ in SETTINGS_SPEC:
            assert f'name="{key}"'.encode() in r.data, key

    def test_bool_setting_is_normalised(self, app_tmp, client):
        post(client, "/settings", data={"LOSSLESS_CHECK_ENABLED": "off"})
        r = post(client, "/settings", data={"LOSSLESS_CHECK_ENABLED": "maybe"},
                 follow_redirects=True)
        assert b"must be 1 (on) or 0 (off)" in r.data
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.list_settings(conn).get("LOSSLESS_CHECK_ENABLED") == "0"
        conn.close()

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
class TestLiveObservability:
    def test_activity_json_returns_snapshot(self, client):
        from spotify_slsk import runtime_state
        runtime_state.set_activity("idle", detail="from test")
        r = client.get("/activity.json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["phase"] == "idle"
        assert data["detail"] == "from test"
        assert "age_seconds" in data

    def test_logs_json_increments_seq(self, client):
        import logging

        from spotify_slsk import runtime_state
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
        from spotify_slsk import runtime_state
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
        r = post(client, "/settings/test-ntfy", follow_redirects=True)
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
        r = post(client, "/track/nope/nope/retag", follow_redirects=False)
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
        r = post(client, "/track/pid1/t1/retag", follow_redirects=True)
        assert r.status_code == 200
        assert b"missing" in r.data.lower()



class TestTrackSearchHistoryReasons:
    def test_track_page_shows_rejection_reasons(self, app_tmp, client):
        from spotify_slsk import runtime_state

        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("t-why", "Song", "Artist", "Album"))
        conn.close()
        runtime_state.record_search(
            "t-why", "song artist", result_count=3, candidate_count=1,
            rejected_count=2,
            rejections=[{"user": "peer", "filename": "x.wma", "reason": "format not allowed: .wma"}],
        )
        r = client.get("/track/pid1/t-why")
        assert r.status_code == 200
        assert b"format not allowed: .wma" in r.data
        assert b"and 1 more" in r.data


class TestBlocklistPage:
    def test_ban_list_and_remove(self, app_tmp, client):
        r = post(client, "/blocklist/ban-user", {"username": "villain", "reason": "fakes"},
                 follow_redirects=True)
        assert r.status_code == 200
        assert b"villain" in r.data and b"whole user" in r.data
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        entry_id = db.list_blocklist(conn)[0]["id"]
        conn.close()
        post(client, f"/blocklist/{entry_id}/delete")
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.list_blocklist(conn) == []
        conn.close()

    def test_empty_username_is_refused(self, app_tmp, client):
        post(client, "/blocklist/ban-user", {"username": "  "})
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.list_blocklist(conn) == []
        conn.close()

    def test_entries_are_autoescaped(self, app_tmp, client):
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.add_blocklist(conn, "<script>x</script>", "f", None, "r")
        conn.close()
        r = client.get("/blocklist")
        assert b"<script>x</script>" not in r.data


class TestHistoryPage:
    def _seed(self, app_tmp):
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"))
        db.record_history(conn, "grabbed", playlist_id="pid1", track_id="t1", username="peer", filename="f.flac")
        db.record_history(conn, "download_failed", playlist_id="pid1", track_id="t1", reason="transfer timed out")
        conn.close()

    def test_lists_and_filters(self, app_tmp, client):
        self._seed(app_tmp)
        r = client.get("/history")
        assert r.status_code == 200
        assert b"transfer timed out" in r.data and b"Song" in r.data
        r = client.get("/history?event=grabbed")
        assert b"transfer timed out" not in r.data and b"f.flac" in r.data
        assert client.get("/history?event=bogus&before_id=x").status_code == 200
        for bad in ("99999999999999999999999", "-5", "0", str(2**63)):
            assert client.get(f"/history?before_id={bad}").status_code == 200, bad

    def test_retry_records_history_and_track_page_shows_it(self, app_tmp, client):
        self._seed(app_tmp)
        post(client, "/track/pid1/t1/retry")
        r = client.get("/track/pid1/t1")
        assert b"retried" in r.data and b"transfer timed out" in r.data


class TestInteractiveSearchUI:
    CAND_OK = {
        "user": "peer", "filename": "Artist - Song.flac", "size": 123, "ext": ".flac",
        "basename": "Artist - Song.flac",
        "bitrate": 1411, "bit_depth": 16, "sample_rate": 44100, "length": 200,
        "free_slot": True, "user_upload_speed": 1000, "queue_length": 0,
        "accepted": True, "reason": None,
    }
    CAND_BAD = dict(CAND_OK, filename="Artist - Song.wma", basename="Artist - Song.wma", ext=".wma",
                    accepted=False, reason="format not allowed: .wma")

    def _seed(self, app_tmp):
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album", 200_000))
        conn.close()

    def _search(self, app_tmp, client, monkeypatch):
        import time as _time

        from spotify_slsk import runtime_state
        from spotify_slsk.slskd import search as slskd_search

        self._seed(app_tmp)
        captured = {}

        def fake(artist, title, album=None, duration_ms=None, is_blocked=None):
            captured.update(artist=artist, title=title, duration_ms=duration_ms)
            return [dict(self.CAND_OK), dict(self.CAND_BAD)]

        monkeypatch.setattr(slskd_search, "interactive_search", fake)
        runtime_state._interactive.pop(("pid1", "t1"), None)
        runtime_state.pop_manual_grabs()
        r = post(client, "/track/pid1/t1/search")
        assert r.status_code == 302
        for _ in range(200):
            snap = runtime_state.get_interactive_search(("pid1", "t1"))
            if snap and snap["status"] != "running":
                break
            _time.sleep(0.01)
        assert captured == {"artist": "Artist", "title": "Song", "duration_ms": 200_000}

    def test_results_partial_lists_accepted_and_rejected(self, app_tmp, client, monkeypatch):
        self._search(app_tmp, client, monkeypatch)
        r = client.get("/track/pid1/t1/search")
        assert r.status_code == 200
        assert b"Artist - Song.flac" in r.data
        assert b"format not allowed: .wma" in r.data
        assert b"hx-trigger" not in r.data, "finished search must stop polling"
        # The filename cell wraps (full path in title); Grab column is pinned.
        assert b'class="file-cell" title="Artist - Song.flac"' in r.data
        assert r.data.count(b'class="actions-col"') == 3
        assert b"Artist - Song.flac" in client.get("/track/pid1/t1").data

    def test_grab_accepted_queues_for_daemon_and_wakes(self, app_tmp, client, monkeypatch):
        from spotify_slsk import runtime_state
        self._search(app_tmp, client, monkeypatch)
        woke = []
        app_tmp.config["WAKE_DAEMON"] = lambda: woke.append(1)
        post(client, "/track/pid1/t1/grab",
             {"username": "peer", "filename": "Artist - Song.flac", "size": "123"})
        grabs = runtime_state.pop_manual_grabs()
        assert [(g["table_name"], g["track_id"], g["candidate"]["filename"]) for g in grabs] == [
            ("pl_x", "t1", "Artist - Song.flac")]
        assert woke == [1]

    def test_grab_rejected_needs_confirmation(self, app_tmp, client, monkeypatch):
        from spotify_slsk import runtime_state
        self._search(app_tmp, client, monkeypatch)
        form = {"username": "peer", "filename": "Artist - Song.wma", "size": "123"}
        r = post(client, "/track/pid1/t1/grab", form)
        assert r.status_code == 200 and b"Grab anyway" in r.data
        assert runtime_state.pop_manual_grabs() == []
        post(client, "/track/pid1/t1/grab", dict(form, force="1"))
        assert len(runtime_state.pop_manual_grabs()) == 1

    def test_grab_of_unknown_result_is_refused(self, app_tmp, client, monkeypatch):
        from spotify_slsk import runtime_state
        self._search(app_tmp, client, monkeypatch)
        post(client, "/track/pid1/t1/grab",
             {"username": "evil", "filename": "/etc/passwd", "size": "1", "force": "1"})
        assert runtime_state.pop_manual_grabs() == []


class TestWantedPage:
    def _seed(self, app_tmp):
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        db.upsert_playlist_meta(conn, "pid1", "pl_x", "X", "s")
        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("t1", "Missing One", "Artist", "Album"))
        db.insert_track(conn, "pl_x", ("t2", "Missing Two", "Artist", "Album"))
        db.insert_track(conn, "pl_x", ("t3", "Have It", "Artist", "Album"))
        db.update_download_status(conn, "t3", "pl_x", success=True, file_path="/p/x.flac")
        db.update_download_status(conn, "t1", "pl_x", success=False)
        db.update_download_status(conn, "t1", "pl_x", success=False)
        db.record_history(conn, "download_failed", playlist_id="pid1", track_id="t1",
                          reason="transfer timed out")
        db.upsert_playlist_meta(conn, "pid2", "pl_off", "Off", "s")
        db.create_table(conn, "pl_off")
        db.insert_track(conn, "pl_off", ("t9", "Disabled Track", "A", "B"))
        db.set_playlist_enabled(conn, "pid2", False)
        conn.close()

    def test_lists_missing_from_enabled_playlists(self, app_tmp, client):
        self._seed(app_tmp)
        r = client.get("/wanted")
        assert r.status_code == 200
        assert b"Missing One" in r.data and b"Missing Two" in r.data
        assert b"Have It" not in r.data and b"Disabled Track" not in r.data
        assert b"transfer timed out" in r.data and b"suspended" in r.data
        assert b"does not clear" in r.data and b'href="/blocklist"' in r.data
        r = client.get("/wanted?q=two")
        assert b"Missing Two" in r.data and b"Missing One" not in r.data

    def test_search_selected_clears_suspension_and_records_history(self, app_tmp, client):
        self._seed(app_tmp)
        woke = []
        app_tmp.config["WAKE_DAEMON"] = lambda: woke.append(1)
        post(client, "/wanted/search", {"tracks": ["pid1:t1", "pid2:t9", "bogus"]})
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        t1 = db.get_track(conn, "pl_x", "t1")
        assert (t1["attempts"], t1["suspended_until"]) == (0, None)
        retried = db.list_history(conn, event="retried")
        assert [r["track_id"] for r in retried] == ["t1"], "disabled playlist not touched"
        conn.close()
        assert woke == [1]

    def test_search_all_missing(self, app_tmp, client):
        self._seed(app_tmp)
        post(client, "/wanted/search", {"all": "1"})
        conn = db.create_connection(app_tmp.config["DB_PATH"])
        assert sorted(r["track_id"] for r in db.list_history(conn, event="retried")) == ["t1", "t2"]
        conn.close()


def test_activity_banner_keeps_polling(client):
    # outerHTML swapped the polling wrapper away after the first refresh.
    html = client.get("/").data.decode()
    wrapper = html[html.index('hx-select="#activity-banner"') - 200:html.index('hx-select="#activity-banner"')]
    assert 'hx-swap="innerHTML"' in wrapper


def test_url_map_is_unchanged_by_the_blueprint_split(app_tmp):
    """Blueprints rename endpoints, never URLs: bookmarks, the htmx polling
    paths in templates/JS and any reverse proxy rules depend on these."""
    rules = {
        (r.rule, m)
        for r in app_tmp.url_map.iter_rules()
        for m in r.methods - {"HEAD", "OPTIONS"}
        if r.endpoint != "static"
    }
    assert rules == {
        ("/", "GET"),
        ("/playlist/<playlist_id>", "GET"),
        ("/track/<playlist_id>/<track_id>", "GET"),
        ("/playlists", "POST"),
        ("/playlist/<playlist_id>/toggle", "POST"),
        ("/playlist/<playlist_id>/delete", "POST"),
        ("/playlist/<playlist_id>/refresh", "POST"),
        ("/track/<playlist_id>/<track_id>/retry", "POST"),
        ("/track/<playlist_id>/<track_id>/search", "POST"),
        ("/track/<playlist_id>/<track_id>/search", "GET"),
        ("/track/<playlist_id>/<track_id>/grab", "POST"),
        ("/track/<playlist_id>/<track_id>/retag", "POST"),
        ("/wanted", "GET"),
        ("/wanted/search", "POST"),
        ("/history", "GET"),
        ("/blocklist", "GET"),
        ("/blocklist/<int:entry_id>/delete", "POST"),
        ("/blocklist/ban-user", "POST"),
        ("/settings", "GET"),
        ("/settings", "POST"),
        ("/settings/test-ntfy", "POST"),
        ("/file", "GET"),
        ("/logs", "GET"),
        ("/logs.json", "GET"),
        ("/activity.json", "GET"),
        ("/healthz", "GET"),
    }
