"""Structured health checks: local ones, remote ones (daemon only), the
integrity task, and how /healthz and /system/status show them."""
import os
import time

import pytest

from spotify_slsk import config, daemon, db, healthchecks
from spotify_slsk.slskd import client as slskd_client
from tests.test_webui import app_tmp, client  # noqa: F401  (fixtures)


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    yield c
    c.close()


def by_name(checks):
    return {c["name"]: c for c in checks}


class TestLocal:
    def test_healthy_sandbox(self, conn, sandbox_dirs, monkeypatch):
        downloads, _ = sandbox_dirs
        monkeypatch.setattr(config, "DOWNLOAD_DIR", str(downloads))
        monkeypatch.setattr(config, "MIN_FREE_SPACE_GB", 0)
        checks = by_name(healthchecks.local_checks(conn))
        for name in ("downloads_dir", "playlists_dir", "disk_space", "last_cycle"):
            assert checks[name]["status"] == "ok", checks[name]
        assert set(checks["downloads_dir"]) == {"name", "status", "message"}

    def test_missing_dir_is_an_error(self, conn, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "nope"))
        assert by_name(healthchecks.local_checks(conn))["downloads_dir"]["status"] == "error"

    def test_low_disk_space_warns(self, conn, monkeypatch):
        monkeypatch.setattr(config, "MIN_FREE_SPACE_GB", 10**9)
        check = by_name(healthchecks.local_checks(conn))["disk_space"]
        assert check["status"] == "warn" and "GB free" in check["message"]

    def test_stale_heartbeat_is_an_error(self, conn, tmp_path, monkeypatch):
        hb = tmp_path / "hb"
        hb.write_text("x")
        os.utime(hb, (time.time() - 5000, time.time() - 5000))
        monkeypatch.setenv("HEARTBEAT_FILE", str(hb))
        monkeypatch.setenv("HEARTBEAT_STALE_SECONDS", "900")
        assert by_name(healthchecks.local_checks(conn))["heartbeat"]["status"] == "error"

    def test_old_cycle_warns(self, conn):
        db.record_task_run(conn, "spotify_sync", started=time.time() - 72 * 3600, duration=60, result="")
        assert by_name(healthchecks.local_checks(conn))["last_cycle"]["status"] == "warn"

    def test_missing_flac_warns_only_when_the_check_is_on(self, conn, monkeypatch):
        monkeypatch.setattr(healthchecks.shutil, "which", lambda name: None)
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", True)
        assert by_name(healthchecks.local_checks(conn))["flac_binary"]["status"] == "warn"
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        assert "flac_binary" not in by_name(healthchecks.local_checks(conn))

    def test_integrity_verdict_comes_from_the_daily_task(self, conn, monkeypatch):
        assert "db_integrity" not in by_name(healthchecks.local_checks(conn))
        daemon._run_task(conn, "db_integrity", daemon._db_integrity)
        assert by_name(healthchecks.local_checks(conn))["db_integrity"]["status"] == "ok"
        monkeypatch.setattr(healthchecks, "integrity_check", lambda c: (False, "quick_check: page 7 bad"))
        daemon._run_task(conn, "db_integrity", daemon._db_integrity)
        check = by_name(healthchecks.local_checks(conn))["db_integrity"]
        assert check["status"] == "error" and "page 7 bad" in check["message"]


class _App:
    def __init__(self, state):
        self._state = state

    def state(self):
        if isinstance(self._state, Exception):
            raise self._state
        return self._state


class _Slskd:
    def __init__(self, state):
        self.application = _App(state)


class TestRemote:
    @pytest.mark.parametrize("state,status", [
        ({"server": {"isConnected": True, "isLoggedIn": True}}, "ok"),
        ({"server": {"isConnected": True, "isLoggedIn": False}}, "error"),
        ({"server": {}}, "error"),
        (ConnectionError("refused"), "error"),
    ])
    def test_slskd(self, monkeypatch, state, status):
        monkeypatch.setattr(slskd_client, "get_client", lambda: _Slskd(state))
        assert by_name(healthchecks.refresh_remote(None))["slskd"]["status"] == status

    def test_missing_api_key_is_named(self, monkeypatch):
        monkeypatch.delenv("SLSKD_API_KEY")
        check = by_name(healthchecks.refresh_remote(None))["slskd"]
        assert check == {"name": "slskd", "status": "error", "message": "SLSKD_API_KEY is not set"}

    def test_spotify(self, monkeypatch):
        monkeypatch.setattr(slskd_client, "get_client", lambda: _Slskd(ConnectionError()))

        class Auth:
            def __init__(self, error=None):
                self.error = error

            def get_access_token(self, as_dict=True):
                if self.error:
                    raise self.error
                return "token"

        class Sp:
            def __init__(self, auth):
                self.auth_manager = auth

        assert by_name(healthchecks.refresh_remote(None))["spotify"]["status"] == "error"
        assert by_name(healthchecks.refresh_remote(Sp(Auth())))["spotify"]["status"] == "ok"
        bad = by_name(healthchecks.refresh_remote(Sp(Auth(ValueError("invalid_client secret=abc")))))
        assert bad["spotify"]["status"] == "error"
        assert "abc" not in bad["spotify"]["message"]  # exception text never shown
        assert by_name(healthchecks.remote_checks())["spotify"]["status"] == "error"


class TestPages:
    def test_healthz_keeps_its_fields_and_adds_checks(self, client, monkeypatch):  # noqa: F811
        monkeypatch.setattr(config, "MIN_FREE_SPACE_GB", 10**9)  # a warn never fails the probe
        r = client.get("/healthz")
        body = r.get_json()
        assert r.status_code == 200 and body["ok"] is True
        assert {"ok", "schema_version", "heartbeat_age_seconds", "reason"} <= set(body)
        names = {c["name"]: c["status"] for c in body["checks"]}
        assert names["disk_space"] == "warn"
        assert all(c["status"] in ("ok", "warn", "error") for c in body["checks"])

    def test_status_page(self, client):  # noqa: F811
        r = client.get("/system/status")
        assert r.status_code == 200
        for text in (b"Health checks", b"Schema version", b"Disk space", b"playlists_dir"):
            assert text in r.data
        assert b"<script>" not in r.data
