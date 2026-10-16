"""Flask dashboard for spotify-slsk.

Runs in a thread alongside the daemon. Uses per-request SQLite connections
against the same DB file the daemon writes to (WAL mode makes this safe).

``create_app`` owns everything cross-cutting - secret key, session cookie
flags, CSRF, security headers/CSP, the per-request DB connection, the
header health badge - and registers one blueprint per area:

* ``playlists`` - dashboard, playlist page, playlist mutations
* ``tracks``    - track page, retry/re-tag, interactive search and grab
* ``wanted``    - wanted, history, blocklist
* ``health``    - library health report, scan / quarantine / ignore
* ``settings``  - validated tunables, ntfy probe
* ``system``    - audio preview file, logs, activity, /healthz
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading

from flask import Flask, abort, g, request, session

from .. import db
from ..utils import fmt_duration
from . import health, library, playlists, settings, system, tracks, wanted
from .helpers import fmt_age, fmt_interval, fmt_ts, health_snapshot, human_bytes

# Reserved settings key holding the generated Flask secret. It is not in
# SETTINGS_SPEC, so it never appears on the settings page.
_SECRET_KEY_SETTING = "_ui_secret_key"

# Methods that cannot change state, so they need no CSRF token.
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _resolve_secret_key(db_path: str) -> str:
    """Return a secret key that survives a restart.

    This used to be `os.urandom(32).hex()` whenever UI_SECRET_KEY was unset,
    i.e. a fresh key on every boot. That was tolerable when the session only
    carried flash messages, but CSRF tokens live in the session too: a
    restart would invalidate the token embedded in any open page and turn the
    next click into a confusing 403. Persisting a generated key in the
    existing settings table keeps deployments that set no env var working.
    """
    env_key = os.getenv("UI_SECRET_KEY")
    if env_key:
        return env_key

    conn = db.create_connection(db_path)
    if conn is None:
        # Can't persist one; fall back to ephemeral so the UI still starts.
        logging.warning("Could not open DB for secret key; using an ephemeral one")
        return secrets.token_hex(32)
    try:
        stored = db.get_setting(conn, _SECRET_KEY_SETTING, default=None)
        if stored:
            return stored
        generated = secrets.token_hex(32)
        db.set_setting(conn, _SECRET_KEY_SETTING, generated)
        logging.info(
            "Generated a persistent UI secret key. Set UI_SECRET_KEY to "
            "manage it yourself."
        )
        return generated
    finally:
        conn.close()


def create_app(db_path: str, spotify_client=None, wake_callback=None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config["DB_PATH"] = db_path
    app.config["SPOTIFY"] = spotify_client
    # Optional callable invoked after state-changing requests so the daemon's
    # cycle sleep is interrupted and the change is observable in seconds
    # rather than after the full CYCLE_INTERVAL_SECONDS window.
    app.config["WAKE_DAEMON"] = wake_callback
    app.secret_key = _resolve_secret_key(db_path)
    # The dashboard is normally loopback-bound and has no auth, so the
    # session cookie is the only thing standing between a random page in
    # another tab and the destructive routes below. Lax still allows the
    # normal top-level navigations this UI relies on.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Only set Secure when the operator terminates TLS in front of us —
        # forcing it on plain http:// loopback would drop the cookie entirely.
        SESSION_COOKIE_SECURE=os.getenv("UI_COOKIE_SECURE", "0") == "1",
    )

    # ---- CSRF ----------------------------------------------------------
    # No auth means a browser is a confused deputy: any page the operator
    # visits can POST to 127.0.0.1:8000 and delete a playlist or rewrite
    # settings. Loopback binding does not help — the *browser* is inside the
    # trust boundary. A session-backed token is proxy-safe, unlike an
    # Origin/Referer check which breaks behind a misconfigured reverse proxy.

    def _csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    @app.before_request
    def _require_csrf_token():
        if request.method in _CSRF_SAFE_METHODS:
            return None
        expected = session.get("_csrf_token")
        submitted = request.form.get("csrf_token") or request.headers.get(
            "X-CSRF-Token", ""
        )
        if not expected or not hmac.compare_digest(expected, submitted):
            logging.warning(
                f"Rejected {request.method} {request.path}: bad or missing CSRF token"
            )
            abort(400, "invalid or missing CSRF token")
        return None

    # Templates call csrf_token() to render the hidden field.
    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.jinja_env.filters["human_bytes"] = human_bytes
    app.jinja_env.filters["ts"] = fmt_ts
    app.jinja_env.filters["interval"] = fmt_interval
    app.jinja_env.filters["age"] = fmt_age
    app.jinja_env.filters["duration_ms"] = (
        lambda ms: fmt_duration(ms / 1000) if ms else "—"
    )

    @app.after_request
    def _security_headers(response):
        # Conservative headers for a single-user dashboard. The CSP matches
        # what the templates actually use: self-hosted CSS/JS, no inline
        # scripts, no third-party origins.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'",
        )
        return response

    @app.before_request
    def _open_conn():
        g.conn = db.create_connection(app.config["DB_PATH"])
        if g.conn is None:
            abort(503, "database unavailable")

    @app.teardown_appcontext
    def _close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logging.debug("Error closing webui conn", exc_info=True)

    for blueprint in (playlists.bp, tracks.bp, wanted.bp, health.bp, library.bp, settings.bp, system.bp):
        app.register_blueprint(blueprint)

    @app.context_processor
    def _inject_health():
        # Every page shows the daemon's status in the header, so it must not
        # depend on a single route remembering to pass it.
        return {"health": health_snapshot()}

    return app


def run_in_thread(
    db_path: str,
    spotify_client=None,
    wake_callback=None,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> threading.Thread:
    """Start the Flask app on a daemon thread. Uses the stdlib WSGI server;
    fine for a single-user dashboard — no need for gunicorn here."""
    app = create_app(db_path, spotify_client=spotify_client, wake_callback=wake_callback)

    def _run():
        try:
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except Exception:
            logging.exception("Web UI thread crashed")

    t = threading.Thread(target=_run, daemon=True, name="webui")
    t.start()
    logging.info(f"Web UI listening on http://{host}:{port}")
    return t
