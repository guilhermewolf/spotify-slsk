"""Optional web UI login: off by default, guards every route but /healthz,
/login and static files, hashes the password, rate-limits failures."""
import pytest

from spotify_slsk import config, db, healthchecks
from spotify_slsk.webui import auth, create_app
from tests.test_webui import app_tmp, client, post  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _reset_failures():
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture
def env_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("UI_AUTH_USERNAME", "admin")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "correct horse")
    db_path = str(tmp_path / "a.db")
    db.create_connection(db_path).close()
    app = create_app(db_path)
    app.config.update(TESTING=True)
    return app


def login(c, username="admin", password="correct horse", next_=None):
    data = {"username": username, "password": password}
    if next_:
        data["next"] = next_
    return post(c, "/login", data)


def test_off_by_default(client):  # noqa: F811
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 302  # nothing to log in to
    assert b"Log out" not in client.get("/").data


def test_every_page_needs_a_login_except_health_and_static(env_auth):
    c = env_auth.test_client()
    r = c.get("/settings")
    assert r.status_code == 302 and "/login?next=/settings" in r.headers["Location"]
    for path in ("/", "/queue", "/logs.json", "/activity.json", "/system/status", "/file?rel=x"):
        assert c.get(path).status_code == 302, path
    assert c.get("/healthz").status_code == 200
    assert c.get("/static/app.css").status_code == 200
    r = c.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/login")
    assert post(c, "/playlists", {"playlist_url": "x"}).status_code == 302  # POSTs too


def test_login_flow_and_logout(env_auth):
    c = env_auth.test_client()
    assert c.post("/login", data={"username": "admin", "password": "correct horse"}).status_code == 400  # CSRF
    r = login(c, next_="/settings")
    assert r.status_code == 302 and r.headers["Location"] == "/settings"
    page = c.get("/settings")
    assert page.status_code == 200 and b"Log out" in page.data
    post(c, "/logout")
    assert c.get("/").status_code == 302


def test_wrong_credentials_and_open_redirect(env_auth, caplog):
    c = env_auth.test_client()
    caplog.set_level("INFO")
    r = login(c, password="nope")
    assert r.status_code == 401 and b"Wrong username or password" in r.data
    assert login(c, username="mallory").status_code == 401
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "correct horse" not in logged and "nope" not in logged and "mallory" not in logged
    r = login(c, next_="//evil.example/x")
    assert r.headers["Location"] == "/"


def test_failed_attempts_are_rate_limited(env_auth):
    c = env_auth.test_client()
    for _ in range(auth.MAX_FAILURES):
        assert login(c, password="bad").status_code == 401
    r = login(c)  # even the right password, while locked
    assert r.status_code == 429 and b"Too many failed attempts" in r.data
    auth._failures.clear()
    assert login(c).status_code == 302


def test_password_saved_on_settings_is_hashed_and_logs_others_out(app_tmp, client):  # noqa: F811
    post(client, "/settings", {"UI_AUTH_USERNAME": "me", "UI_AUTH_PASSWORD": " s3cret pass "})
    conn = db.create_connection(app_tmp.config["DB_PATH"])
    stored = db.list_settings(conn)
    conn.close()
    assert "UI_AUTH_PASSWORD" not in stored
    assert stored[config.UI_AUTH_HASH_KEY].startswith(("scrypt:", "pbkdf2:"))
    assert " s3cret pass " not in str(stored)
    assert client.get("/").status_code == 302  # the saving session is not logged in
    assert login(client, "me", " s3cret pass ").status_code == 302
    assert b"s3cret" not in client.get("/settings").data

    other = app_tmp.test_client()
    assert login(other, "me", " s3cret pass ").status_code == 302
    post(client, "/settings", {"UI_AUTH_USERNAME": "me", "UI_AUTH_PASSWORD": "new one"})
    assert other.get("/").status_code == 302  # password changed: logged out


def test_health_check_warns_when_exposed_without_login(tmp_path, monkeypatch):
    conn = db.create_connection(str(tmp_path / "h.db"))
    monkeypatch.delenv("UI_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("UI_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("UI_BIND_ADDR", "0.0.0.0")

    def status():
        return {c["name"]: c["status"] for c in healthchecks.local_checks(conn)}["ui_auth"]

    assert status() == "warn"
    monkeypatch.setenv("UI_BIND_ADDR", "127.0.0.1")
    assert status() == "ok"
    monkeypatch.setenv("UI_BIND_ADDR", "0.0.0.0")
    monkeypatch.setenv("UI_AUTH_USERNAME", "admin")
    monkeypatch.setenv("UI_AUTH_PASSWORD", "pw")
    assert status() == "ok"
    conn.close()
