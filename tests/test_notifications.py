"""Notifications: batching, providers, backoff, secrets, health transitions,
the Subsonic rescan, and the settings page's secret handling and tests.

Every HTTP call is mocked (conftest blocks real ones)."""
import json

import pytest

from spotify_slsk import daemon, db, notifications
from tests.test_webui import app_tmp, client, post  # noqa: F401  (fixtures)


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    db.upsert_playlist_meta(c, "pid1", "pl_chill", "Chill", "s")
    db.create_table(c, "pl_chill")
    for i in range(3):
        db.insert_track(c, "pl_chill", (f"t{i}", f"Song {i}", "Artist", "Album"))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_state():
    notifications._queue.clear()
    notifications._backoff.clear()
    notifications._rescan_pending = False
    yield
    notifications._queue.clear()
    notifications._backoff.clear()
    notifications._rescan_pending = False


class _Resp:
    """A stream=True requests.Response: a context manager whose body is read
    through iter_content (json() would read an unbounded body)."""

    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self.ok = status < 400
        self._raw = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.read = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        for i in range(0, len(self._raw), size):
            self.read += size
            yield self._raw[i:i + size]

    def json(self):
        raise AssertionError("json() reads an unbounded body")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def _post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return _Resp()

    def _get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return _Resp(body={"subsonic-response": {"status": "ok"}})

    monkeypatch.setattr(notifications.requests, "post", _post)
    monkeypatch.setattr(notifications.requests, "get", _get)
    return calls


class TestBatching:
    def test_history_events_are_batched_per_playlist(self, conn):
        notifications.install()
        for i in range(3):
            db.record_history(conn, "imported", table_name="pl_chill", track_id=f"t{i}", path=f"/p/{i}.flac")
        db.record_history(conn, "renamed", table_name="pl_chill", track_id="t0")  # not an event
        notifications.emit("cycle_summary", message="1 playlist(s)")
        messages = notifications.batch(conn, list(notifications._queue))
        assert [m["event"] for m in messages] == ["imported", "cycle_summary"]
        imported = messages[0]
        assert imported["title"] == "Imported: 3 track(s) in Chill"
        assert imported["data"]["count"] == 3 and imported["data"]["playlist"] == "Chill"
        assert "Song 1 — Artist" in imported["text"]

    def test_long_batches_are_capped(self, conn):
        for _ in range(notifications.MAX_LISTED + 5):
            notifications.emit("download_failed", playlist_id="pid1", track_id="t0", reason="timeout")
        (msg,) = notifications.batch(conn, list(notifications._queue))
        assert msg["data"]["count"] == notifications.MAX_LISTED + 5
        assert len(msg["data"]["tracks"]) == notifications.MAX_LISTED
        assert "… and 5 more" in msg["text"]


class TestProviders:
    def test_nothing_is_sent_when_unconfigured(self, conn, sent, monkeypatch):
        for key in ("NTFY_URL", "NTFY_TOPIC", "WEBHOOK_URL"):
            monkeypatch.delenv(key, raising=False)
        notifications.emit("cycle_summary", message="x")
        assert notifications.flush(conn) == 1
        assert sent == []

    def test_ntfy_gets_only_its_events(self, conn, sent):
        db.set_setting(conn, "NTFY_URL", "https://ntfy.example/")
        db.set_setting(conn, "NTFY_TOPIC", "music")
        notifications.emit("cycle_summary", message="2 downloaded")
        notifications.emit("imported", playlist_id="pid1", track_id="t0")  # not a default ntfy event
        notifications.flush(conn)
        assert len(sent) == 1
        method, url, kwargs = sent[0]
        assert url == "https://ntfy.example/music"
        assert kwargs["data"] == b"2 downloaded"
        assert kwargs["headers"]["Title"] == "Cycle summary"
        assert kwargs["timeout"] == 10

    def test_webhook_posts_json_with_the_auth_header(self, conn, sent):
        db.set_setting(conn, "WEBHOOK_URL", "https://hooks.example/x")
        db.set_setting(conn, "WEBHOOK_HEADER_NAME", "X-Api-Key")
        db.set_setting(conn, "WEBHOOK_HEADER_VALUE", "s3cret")
        db.set_setting(conn, "WEBHOOK_EVENTS", "imported")
        notifications.emit("imported", playlist_id="pid1", track_id="t1")
        notifications.emit("cycle_summary", message="filtered out")
        notifications.flush(conn)
        assert len(sent) == 1
        _, url, kwargs = sent[0]
        assert kwargs["headers"]["X-Api-Key"] == "s3cret"
        assert kwargs["allow_redirects"] is False
        body = kwargs["json"]
        assert body["event"] == "imported" and body["ts"] and body["data"]["count"] == 1
        json.dumps(body)  # serialisable

    def test_failure_backs_off_and_never_logs_the_url_or_secret(self, conn, monkeypatch, caplog):
        import requests

        db.set_setting(conn, "WEBHOOK_URL", "https://hooks.example/token-in-path")
        db.set_setting(conn, "WEBHOOK_HEADER_NAME", "Authorization")
        db.set_setting(conn, "WEBHOOK_HEADER_VALUE", "Bearer s3cret")
        calls = []

        def _boom(url, **kwargs):
            calls.append(url)
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

        monkeypatch.setattr(notifications.requests, "post", _boom)
        caplog.set_level("WARNING")
        notifications.emit("cycle_summary", message="a")
        notifications.flush(conn, now=1000.0)
        notifications.emit("cycle_summary", message="b")
        notifications.flush(conn, now=1030.0)  # paused: not attempted
        assert len(calls) == 1
        notifications.emit("cycle_summary", message="c")
        notifications.flush(conn, now=1061.0)  # 60 s later: retried, now paused 120 s
        assert len(calls) == 2
        assert notifications._backoff["webhook"][1] == 120
        assert "token-in-path" not in caplog.text and "s3cret" not in caplog.text
        assert "ConnectionError" in caplog.text


class TestSubsonic:
    def _configure(self, conn):
        db.set_setting(conn, "SUBSONIC_URL", "http://navidrome:4533/")
        db.set_setting(conn, "SUBSONIC_USERNAME", "admin")
        db.set_setting(conn, "SUBSONIC_PASSWORD", "pw")

    def test_rescan_once_per_cycle_after_imports_only(self, conn, sent):
        self._configure(conn)
        notifications.emit("cycle_summary", message="nothing imported")
        notifications.flush(conn)
        assert notifications.rescan_library(conn) is False
        notifications.emit("imported", playlist_id="pid1", track_id="t0")
        notifications.emit("upgraded", playlist_id="pid1", track_id="t1")
        notifications.flush(conn)
        assert notifications.rescan_library(conn) is True
        assert notifications.rescan_library(conn) is False  # debounced
        (method, url, kwargs) = sent[-1]
        assert method == "GET" and url == "http://navidrome:4533/rest/startScan"
        params = kwargs["params"]
        assert params["u"] == "admin" and "p" not in params and "pw" not in params.values()
        import hashlib
        assert params["t"] == hashlib.md5(("pw" + params["s"]).encode()).hexdigest()

    def test_subsonic_error_is_reported(self, conn, monkeypatch):
        self._configure(conn)
        monkeypatch.setattr(notifications.requests, "get", lambda url, **k: _Resp(body={
            "subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}
        }))
        with pytest.raises(notifications.DeliveryError, match="Wrong username"):
            notifications.send_test(conn, "subsonic")


class TestBoundedResponses:
    def test_senders_stream_and_subsonic_reads_at_most_64_kib(self, conn, monkeypatch):
        seen = []
        huge = _Resp(raw=b"x" * (10 * 1024 * 1024))
        monkeypatch.setattr(notifications.requests, "get",
                            lambda url, **k: seen.append(k) or huge)
        db.set_setting(conn, "SUBSONIC_URL", "http://navidrome:4533")
        db.set_setting(conn, "SUBSONIC_USERNAME", "admin")
        db.set_setting(conn, "SUBSONIC_PASSWORD", "pw")
        with pytest.raises(notifications.DeliveryError, match="larger than 64 KiB"):
            notifications.send_test(conn, "subsonic")
        assert seen[0]["stream"] is True
        assert huge.read <= 64 * 1024 + 8192

    def test_ntfy_and_webhook_stream_without_reading(self, conn, sent):
        db.set_setting(conn, "NTFY_URL", "https://ntfy.example")
        db.set_setting(conn, "NTFY_TOPIC", "t")
        db.set_setting(conn, "WEBHOOK_URL", "https://hooks.example/x")
        notifications.emit("cycle_summary", message="m")
        notifications.flush(conn)
        assert [k["stream"] for _, _, k in sent] == [True, True]


class TestHealthTransitions:
    def test_only_changes_notify(self):
        notifications.health_transitions({}, [{"name": "slskd", "status": "ok", "message": "fine"}])
        assert notifications.pending() == 0
        notifications.health_transitions({"slskd": "ok"}, [{"name": "slskd", "status": "error", "message": "down"}])
        notifications.health_transitions({"slskd": "error"}, [{"name": "slskd", "status": "error", "message": "down"}])
        notifications.health_transitions({"slskd": "error"}, [{"name": "slskd", "status": "ok", "message": "up"}])
        assert [i["event"] for i in notifications._queue] == ["health_issue", "health_restored"]

    def test_daemon_persists_state_across_restarts(self, conn, monkeypatch):
        monkeypatch.setattr(daemon.healthchecks, "refresh_remote", lambda sp: [])
        monkeypatch.setattr(daemon.healthchecks, "collect",
                            lambda c: [{"name": "disk_space", "status": "warn", "message": "low"}])
        monkeypatch.setattr(notifications, "flush", lambda c: 0)
        daemon._health_checks(conn)
        assert [i["event"] for i in notifications._queue] == ["health_issue"]
        notifications._queue.clear()
        daemon._health_checks(conn)  # same status after a "restart": silent
        assert notifications.pending() == 0


class TestSettingsPage:
    def test_secret_is_never_rendered_and_blank_keeps_it(self, app_tmp, client):  # noqa: F811
        post(client, "/settings", {"WEBHOOK_URL": "https://hooks.example/x",
                                   "WEBHOOK_HEADER_VALUE": "hunter2"})
        html = client.get("/settings").data
        assert b"hunter2" not in html
        assert "••••".encode() in html and b"leave blank to keep" in html
        post(client, "/settings", {"WEBHOOK_URL": "https://hooks.example/x"})  # field left blank
        c = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.get_setting(c, "WEBHOOK_HEADER_VALUE") == "hunter2"
        c.close()
        post(client, "/settings", {"clear_WEBHOOK_HEADER_VALUE": "1"})
        c = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.list_settings(c).get("WEBHOOK_HEADER_VALUE") is None
        c.close()

    def test_validation(self, client):  # noqa: F811
        r = post(client, "/settings", {"WEBHOOK_URL": "ftp://x"}, follow_redirects=True)
        assert b"must be an http" in r.data
        r = post(client, "/settings", {"NTFY_EVENTS": "imported,bogus"}, follow_redirects=True)
        assert b"unknown event(s) bogus" in r.data
        r = post(client, "/settings", {"WEBHOOK_HEADER_NAME": "Bad Header:"}, follow_redirects=True)
        assert b"HTTP header name" in r.data

    def test_test_button_shows_a_safe_error(self, app_tmp, client, monkeypatch):  # noqa: F811
        import requests

        post(client, "/settings", {"WEBHOOK_URL": "https://hooks.example/abc-token"})
        monkeypatch.setattr(notifications.requests, "post",
                            lambda url, **k: (_ for _ in ()).throw(requests.ConnectionError(url)))
        r = post(client, "/settings/test-notification/webhook", follow_redirects=True)
        flash = r.data.split(b'class="flash flash-error">')[1].split(b"</p>")[0]
        assert flash == b"Test failed: ConnectionError"
        assert post(client, "/settings/test-notification/nope").status_code == 404
