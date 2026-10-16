"""Optional login for the web UI.

Off unless both UI_AUTH_USERNAME and a password are configured - in the
environment (UI_AUTH_USERNAME / UI_AUTH_PASSWORD) or on /settings. The
password is only ever kept as a salted werkzeug hash: the env value is
hashed in memory when the app starts, and a password saved on /settings is
stored as its hash under ``_ui_auth_password_hash`` (underscore: never
rendered). A stored hash wins over the env password; the username follows
the usual settings rule (DB, else env).

When on, every route except /healthz, /login and static assets needs a
session cookie set by a successful login. The session remembers a
fingerprint of the password hash, so changing the password logs every
other session out. Failed logins are rate-limited per client IP, in memory.
Neither the password nor the submitted username is ever logged.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from collections import deque

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from werkzeug.security import check_password_hash, generate_password_hash

from .. import config, db

bp = Blueprint("auth", __name__)

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
# Reachable without a login.
PUBLIC_ENDPOINTS = frozenset({"static", "system.healthz", "auth.login"})

_failures_lock = threading.Lock()
_failures: dict[str, deque[float]] = {}


def env_password_hash() -> str | None:
    """Hash UI_AUTH_PASSWORD once, at app creation."""
    password = os.getenv("UI_AUTH_PASSWORD")
    return generate_password_hash(password) if password else None


def credentials() -> tuple[str, str] | None:
    """(username, password hash) when auth is on, else None."""
    username = db.get_setting(g.conn, "UI_AUTH_USERNAME", "") or ""
    stored = db.list_settings(g.conn).get(config.UI_AUTH_HASH_KEY)
    password_hash = stored or current_app.config.get("AUTH_ENV_HASH")
    return (username, password_hash) if username and password_hash else None


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:32]


def current_user() -> str | None:
    """The logged-in username, or None (also None when auth is off)."""
    creds = credentials()
    if creds is None:
        return None
    if session.get("auth_user") == creds[0] and session.get("auth_fp") == _fingerprint(creds[1]):
        return creds[0]
    return None


def require_login() -> ResponseReturnValue | None:
    """before_request guard (registered by create_app after the DB opens)."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    creds = credentials()
    if creds is None or current_user() is not None:
        return None
    login = url_for("auth.login", next=request.full_path.rstrip("?"))
    if request.headers.get("HX-Request"):
        # htmx would swap the login page into a fragment; make it navigate.
        resp = Response("login required", status=401)
        resp.headers["HX-Redirect"] = login
        return resp
    return redirect(login)


def _safe_next(target: str | None) -> str:
    """Only same-site relative paths: no open redirect via ?next=."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("playlists.dashboard")


def _too_many(ip: str, now: float) -> int:
    """Seconds until `ip` may try again, or 0."""
    with _failures_lock:
        attempts = _failures.get(ip)
        if not attempts:
            return 0
        while attempts and now - attempts[0] > FAILURE_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) < MAX_FAILURES:
            return 0
        return int(FAILURE_WINDOW_SECONDS - (now - attempts[0])) + 1


def _record_failure(ip: str, now: float) -> None:
    with _failures_lock:
        _failures.setdefault(ip, deque(maxlen=MAX_FAILURES)).append(now)


@bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    creds = credentials()
    if creds is None:  # auth is off: there is nothing to log in to
        return redirect(url_for("playlists.dashboard"))
    target = _safe_next(request.values.get("next"))
    if request.method == "GET":
        return render_template("login.html", next=target)

    ip = request.remote_addr or "?"
    now = time.time()
    wait = _too_many(ip, now)
    if wait:
        flash(f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).", "error")
        return render_template("login.html", next=target), 429
    username = request.form.get("username") or ""
    password = request.form.get("password") or ""
    # Always run the hash check, so a wrong username costs the same time.
    password_ok = check_password_hash(creds[1], password)
    if hmac.compare_digest(username.encode(), creds[0].encode()) and password_ok:
        session.clear()  # no session fixation; a fresh CSRF token follows
        session["auth_user"] = creds[0]
        session["auth_fp"] = _fingerprint(creds[1])
        logging.info(f"UI login from {ip}")
        return redirect(target)
    _record_failure(ip, now)
    logging.warning(f"Failed UI login from {ip}")
    flash("Wrong username or password.", "error")
    return render_template("login.html", next=target), 401


@bp.route("/logout", methods=["POST"])
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("auth.login"))


def hash_password(password: str) -> str:
    return generate_password_hash(password)
