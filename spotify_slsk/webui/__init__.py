"""Flask dashboard for spotify-slsk.

Runs in a thread alongside the daemon. Uses per-request SQLite connections
against the same DB file the daemon writes to (WAL mode makes this safe).
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import requests

from .. import db
from .. import runtime_state
from ..utils import get_playlist_id, sanitize_table_name


# The daemon and this web thread share one spotipy client, and spotipy's
# underlying requests.Session is not thread-safe; a stale pooled connection
# also shows up here as RemoteDisconnected. Retry a couple of times before
# giving up — a retry gets a fresh socket.
SPOTIFY_TRANSPORT_RETRIES = 3


def _fetch_playlist_name(sp, pid: str) -> str:
    last_exc = None
    for attempt in range(SPOTIFY_TRANSPORT_RETRIES):
        try:
            return sp.playlist(pid, fields="name")["name"]
        except requests.exceptions.RequestException as e:
            last_exc = e
            logging.warning(
                f"Spotify transport error for {pid} "
                f"(attempt {attempt + 1}/{SPOTIFY_TRANSPORT_RETRIES}): {e}"
            )
            time.sleep(0.5 * (attempt + 1))
    raise last_exc


def _dir_size(path: str) -> int:
    """Sum of file sizes (bytes) under ``path``. Returns 0 for missing dirs.

    Uses os.scandir + recursion (faster than os.walk for many small files).
    Permission errors on individual entries are swallowed — we'd rather
    show an underestimate than 500 the dashboard.
    """
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _relative_to_playlists_root(path):
    """Return ``path`` relative to the playlists root, or None if outside.

    Used by the track view to build a safe ``?rel=`` query for /file.
    """
    if not path:
        return None
    root = os.path.realpath(os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists"))
    try:
        full = os.path.realpath(path)
        if os.path.commonpath([root, full]) != root:
            return None
        return os.path.relpath(full, root)
    except (ValueError, OSError):
        return None


def _retag_file(path: str, title: str, artist: str, album: str) -> None:
    """Re-write tags using the values the DB has for this track.

    Mirrors app.tag_audio_file but is local to the webui to avoid an import
    cycle (app.py imports from webui at runtime). Supported formats match
    the daemon's path: mp3, flac, aiff. WAV is silently skipped.
    """
    from mutagen.id3 import ID3, TALB, TIT2, TPE1
    from mutagen.flac import FLAC
    from mutagen.aiff import AIFF
    from mutagen.mp3 import MP3

    title = title or ""
    artist = artist or ""
    album = album or ""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".mp3":
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.save()
    elif ext == ".flac":
        audio = FLAC(path)
        audio["title"] = title
        audio["artist"] = artist
        audio["album"] = album
        audio.save()
    elif ext == ".aiff":
        audio = AIFF(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.save()
    else:
        raise ValueError(f"Unsupported extension for retag: {ext}")


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


# (key, default, help text, kind). `kind` drives validation on save — an
# unvalidated setting is worse than no setting: the daemon's _reload_settings
# silently keeps the previous value on a bad parse, so the UI would report
# "Saved" for a value that never takes effect.
SETTINGS_SPEC = [
    ("MIN_MATCH_SCORE", "0.62", "Accept threshold for downloaded-file -> DB match (0.0-1.0)", "ratio"),
    ("SLSKD_PREFERRED_FORMATS", "flac,mp3,aiff,wav", "Comma-separated, ordered — first format wins ties", "formats"),
    ("SLSKD_MIN_PEER_UPLOAD_SPEED", "0", "Passed to slskd; bytes/sec", "int0"),
    ("SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280", "Reject MP3s whose size/duration yields below this", "int0"),
    ("SLSKD_EARLY_STOP_RESPONSES", "20", "Stop the slskd search once this many peers replied", "int1"),
    ("SLSKD_WAIT_TIMEOUT", "60", "Seconds to wait for a downloaded file to appear on disk", "int1"),
    ("UPGRADE_CHECK_INTERVAL_HOURS", "168", "Re-search downloaded tracks for a better version_tier (Extended Mix > Original Mix); 0 disables", "int0"),
]

# Formats the tagging/'extract metadata' code in app.py can actually handle.
_KNOWN_FORMATS = {"flac", "mp3", "aiff", "wav", "m4a", "ogg"}


def validate_setting(kind: str, value: str):
    """Return (normalised_value, error). Exactly one is non-None."""
    if kind == "ratio":
        try:
            parsed = float(value)
        except ValueError:
            return None, "must be a number between 0 and 1"
        if not 0.0 <= parsed <= 1.0:
            return None, "must be between 0 and 1"
        return str(parsed), None

    if kind in ("int0", "int1"):
        minimum = 0 if kind == "int0" else 1
        try:
            parsed = int(value)
        except ValueError:
            return None, f"must be a whole number >= {minimum}"
        if parsed < minimum:
            return None, f"must be >= {minimum}"
        return str(parsed), None

    if kind == "formats":
        items = [f.strip().lstrip(".").lower() for f in value.split(",")]
        items = [f for f in items if f]
        if not items:
            return None, "needs at least one format"
        unknown = [f for f in items if f not in _KNOWN_FORMATS]
        if unknown:
            return None, (
                f"unsupported format(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(_KNOWN_FORMATS))}"
            )
        return ",".join(items), None

    return value, None



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
    app.jinja_env.filters["human_bytes"] = _human_bytes

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

    def _wake():
        cb = app.config.get("WAKE_DAEMON")
        if cb is not None:
            try:
                cb()
            except Exception:
                logging.debug("wake_callback raised", exc_info=True)

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

    # ---- views ---------------------------------------------------------

    @app.route("/")
    def dashboard():
        playlists = db.list_playlists(g.conn)
        playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
        totals = {
            "total": 0,
            "downloaded": 0,
            "pending": 0,
            "retrying": 0,
            "suspended": 0,
        }
        for pl in playlists:
            pl["stats"] = db.playlist_stats(g.conn, pl["table_name"])
            pl["disk_bytes"] = _dir_size(os.path.join(playlists_root, pl["table_name"]))
            # Disabled playlists still hold tracks, but counting them in the
            # library totals would misrepresent what the daemon is working on.
            if pl["enabled"]:
                for key in totals:
                    totals[key] += pl["stats"][key]
        return render_template(
            "dashboard.html",
            playlists=playlists,
            totals=totals,
            activity=runtime_state.get_activity(),
            cycles=db.list_cycles(g.conn, limit=10),
        )

    @app.route("/playlist/<playlist_id>")
    def playlist_detail(playlist_id):
        _, table_name, name, last_synced = db.get_playlist_meta(g.conn, playlist_id)
        if not table_name:
            abort(404)
        all_tracks = db.list_tracks(g.conn, table_name)
        # Counts come from the unfiltered list so every filter chip can show
        # its size — the previous page only knew the total.
        counts = {"all": len(all_tracks)}
        for status in ("downloaded", "pending", "retrying", "suspended"):
            counts[status] = sum(1 for t in all_tracks if t["status"] == status)

        status_filter = request.args.get("status")
        tracks = (
            [t for t in all_tracks if t["status"] == status_filter]
            if status_filter
            else all_tracks
        )
        query = (request.args.get("q") or "").strip()
        if query:
            needle = query.lower()
            tracks = [
                t
                for t in tracks
                if needle in (t["name"] or "").lower()
                or needle in (t["artists"] or "").lower()
                or needle in (t["album"] or "").lower()
            ]
        return render_template(
            "playlist.html",
            playlist_id=playlist_id,
            name=name,
            last_synced=last_synced,
            tracks=tracks,
            counts=counts,
            status_filter=status_filter,
            query=query,
            activity=runtime_state.get_activity(),
        )

    @app.route("/track/<playlist_id>/<track_id>")
    def track_detail(playlist_id, track_id):
        _, table_name, playlist_name, _ = db.get_playlist_meta(g.conn, playlist_id)
        if not table_name:
            abort(404)
        track = db.get_track(g.conn, table_name, track_id)
        if not track:
            abort(404)
        track["tried"] = db.get_tried_files(g.conn, table_name, track_id)
        # Playlists-root-relative path for the <audio> preview. Files outside
        # the root (legacy rows, manual moves) get no preview — /file would
        # refuse them anyway.
        track["rel_path"] = _relative_to_playlists_root(track.get("path"))
        return render_template(
            "track.html",
            playlist_id=playlist_id,
            playlist_name=playlist_name,
            track=track,
            search_history=runtime_state.get_search_history(track_id),
        )

    # ---- playlist mutations -------------------------------------------

    @app.route("/playlists", methods=["POST"])
    def add_playlist():
        url = (request.form.get("playlist_url") or "").strip()
        pid = get_playlist_id(url)
        if not pid:
            flash("That doesn't look like a Spotify playlist URL.", "error")
            return redirect(url_for("dashboard"))

        sp = app.config.get("SPOTIFY")
        if sp is not None:
            try:
                name = _fetch_playlist_name(sp, pid)
            except requests.exceptions.RequestException as e:
                # Transport-level failure: a stale keep-alive socket, or the
                # daemon thread using the same requests.Session at the same
                # time. The playlist itself is probably fine, so add it under
                # a placeholder name and let the first sync correct it.
                logging.warning(
                    f"Spotify unreachable while validating {pid}; "
                    f"adding unvalidated: {e}"
                )
                name = pid
            except Exception as e:
                logging.warning(f"Spotify validation failed for {pid}: {e}")
                flash(f"Spotify rejected that playlist: {e}", "error")
                return redirect(url_for("dashboard"))
        else:
            # Daemon's Spotify client isn't wired in — store the id as
            # placeholder name; the first sync fills in the real name.
            name = pid

        # snapshot_id intentionally NULL so the first cycle fetches tracks.
        table = sanitize_table_name(name)
        db.upsert_playlist_meta(g.conn, pid, table, name, None)
        db.create_table(g.conn, table)
        _wake()
        flash(f'Added "{name}". Cycle starting now.', "success")
        return redirect(url_for("dashboard"))

    @app.route("/playlist/<playlist_id>/toggle", methods=["POST"])
    def toggle_playlist(playlist_id):
        row = g.conn.execute(
            "SELECT enabled FROM playlists_meta WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if not row:
            abort(404)
        new_enabled = not bool(row[0])
        db.set_playlist_enabled(g.conn, playlist_id, new_enabled)
        if new_enabled:
            _wake()
        return redirect(url_for("dashboard"))

    @app.route("/playlist/<playlist_id>/delete", methods=["POST"])
    def delete_playlist(playlist_id):
        if db.remove_playlist(g.conn, playlist_id):
            flash("Playlist removed. Files on disk were kept.", "success")
        else:
            flash("Playlist not found.", "error")
        return redirect(url_for("dashboard"))

    @app.route("/playlist/<playlist_id>/refresh", methods=["POST"])
    def refresh_playlist(playlist_id):
        # Wipe the stored snapshot so the next cycle re-fetches from Spotify
        # even if the playlist hasn't changed upstream.
        try:
            with g.conn:
                g.conn.execute(
                    "UPDATE playlists_meta SET snapshot_id = NULL WHERE playlist_id = ?",
                    (playlist_id,),
                )
            _wake()
            flash("Re-fetching from Spotify now.", "success")
        except Exception as e:
            flash(f"Could not queue refresh: {e}", "error")
        return redirect(url_for("playlist_detail", playlist_id=playlist_id))

    # ---- track mutations ----------------------------------------------

    @app.route("/track/<playlist_id>/<track_id>/retry", methods=["POST"])
    def retry_track(playlist_id, track_id):
        _, table_name, _, _ = db.get_playlist_meta(g.conn, playlist_id)
        if not table_name:
            abort(404)
        db.retry_track(g.conn, table_name, track_id)
        _wake()
        flash(
            "Cleared attempts and rejected-file history. Cycle starting now.",
            "success",
        )
        return redirect(
            url_for("track_detail", playlist_id=playlist_id, track_id=track_id)
        )

    # ---- settings -----------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            # Validate everything before writing anything, so a form with one
            # bad field doesn't half-apply.
            cleaned, errors = {}, []
            for key, _default, _help, kind in SETTINGS_SPEC:
                val = (request.form.get(key) or "").strip()
                if not val:
                    cleaned[key] = None  # clear the override
                    continue
                normalised, error = validate_setting(kind, val)
                if error:
                    errors.append(f"{key}: {error}")
                else:
                    cleaned[key] = normalised

            if errors:
                for message in errors:
                    flash(message, "error")
                return redirect(url_for("settings"))

            for key, value in cleaned.items():
                if value is None:
                    db.delete_setting(g.conn, key)
                else:
                    db.set_setting(g.conn, key, value)
            _wake()
            flash("Saved. Takes effect on the next cycle.", "success")
            return redirect(url_for("settings"))

        # Underscore-prefixed keys are internal (currently _ui_secret_key).
        # The template only renders SETTINGS_SPEC keys today, so this is
        # belt-and-braces — but a secret should never be one careless
        # template loop away from being rendered.
        overrides = {
            key: value
            for key, value in db.list_settings(g.conn).items()
            if not key.startswith("_")
        }
        effective = {
            key: db.get_setting(g.conn, key, default=default)
            for key, default, _help, _kind in SETTINGS_SPEC
        }
        return render_template(
            "settings.html",
            spec=SETTINGS_SPEC,
            overrides=overrides,
            effective=effective,
        )

    # ---- file serving (audio preview) ---------------------------------

    @app.route("/file")
    def serve_file():
        """Serve a downloaded audio file for the in-browser <audio> element.

        ``rel`` is the path *relative to the playlists root* — that's what
        we expect callers to construct from ``track.path``. We resolve it,
        then verify it still lives inside the root after symlink-aware
        normalization to block traversal attempts.
        """
        rel = request.args.get("rel", "")
        if not rel:
            abort(400)
        playlists_root = os.path.realpath(
            os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
        )
        target = os.path.realpath(os.path.join(playlists_root, rel))
        try:
            common = os.path.commonpath([playlists_root, target])
        except ValueError:
            abort(404)
        if common != playlists_root or not os.path.isfile(target):
            abort(404)
        directory, filename = os.path.split(target)
        # send_from_directory handles Range requests for HTML5 audio seeking.
        return send_from_directory(directory, filename, conditional=True)

    # ---- track maintenance --------------------------------------------

    @app.route("/track/<playlist_id>/<track_id>/retag", methods=["POST"])
    def retag_track(playlist_id, track_id):
        _, table_name, _, _ = db.get_playlist_meta(g.conn, playlist_id)
        if not table_name:
            abort(404)
        track = db.get_track(g.conn, table_name, track_id)
        if not track:
            abort(404)
        if not track["path"]:
            flash("This track has no file on disk yet.", "error")
        elif not os.path.isfile(track["path"]):
            flash(f"File missing: {track['path']}", "error")
        else:
            try:
                _retag_file(track["path"], track["name"], track["artists"], track["album"])
                flash("Re-tagged from DB metadata.", "success")
            except Exception as e:
                flash(f"Re-tag failed: {e}", "error")
        return redirect(
            url_for("track_detail", playlist_id=playlist_id, track_id=track_id)
        )

    # ---- ntfy probe ---------------------------------------------------

    @app.route("/settings/test-ntfy", methods=["POST"])
    def test_ntfy():
        """Fire one ntfy notification using the configured URL+topic."""
        ntfy_url = os.getenv("NTFY_URL", "").strip()
        ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        if not ntfy_url or not ntfy_topic:
            flash("NTFY_URL or NTFY_TOPIC is not set in the environment.", "error")
            return redirect(url_for("settings"))
        try:
            resp = requests.post(
                f"{ntfy_url}/{ntfy_topic}",
                data="spotify-slsk: test notification from /settings",
                timeout=5,
            )
            if resp.ok:
                flash(f"Sent! ntfy responded {resp.status_code}.", "success")
            else:
                flash(f"ntfy responded {resp.status_code}: {resp.text[:200]}", "error")
        except Exception as e:
            flash(f"ntfy request failed: {e}", "error")
        return redirect(url_for("settings"))

    # ---- live observability ------------------------------------------

    @app.route("/logs")
    def logs_page():
        snapshot = runtime_state.get_logs(since=0, limit=500)
        return render_template(
            "logs.html",
            entries=snapshot["entries"],
            last_seq=snapshot["last_seq"],
        )

    @app.route("/logs.json")
    def logs_json():
        try:
            since = int(request.args.get("since", "0"))
        except (TypeError, ValueError):
            since = 0
        try:
            limit = max(1, min(2000, int(request.args.get("limit", "500"))))
        except (TypeError, ValueError):
            limit = 500
        return jsonify(runtime_state.get_logs(since=since, limit=limit))

    @app.route("/activity.json")
    def activity_json():
        return jsonify(runtime_state.get_activity())

    # ---- ops ----------------------------------------------------------

    def _health_snapshot() -> dict:
        """Shared by /healthz and the header badge, so they cannot disagree.

        Deliberately cheap: a PRAGMA and a stat() call. No slskd round-trip —
        a health endpoint that depends on a remote service fails when that
        service is slow, which is exactly when you need it to answer.
        """
        try:
            version = g.conn.execute("PRAGMA user_version").fetchone()[0]
            heartbeat_file = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")
            heartbeat_age = None
            if os.path.exists(heartbeat_file):
                heartbeat_age = time.time() - os.path.getmtime(heartbeat_file)
            stale = int(os.getenv("HEARTBEAT_STALE_SECONDS", "900"))
            ok = version == db.SCHEMA_VERSION and (
                heartbeat_age is None or heartbeat_age < stale
            )
            if not ok:
                reason = (
                    f"schema version {version}, expected {db.SCHEMA_VERSION}"
                    if version != db.SCHEMA_VERSION
                    else f"no daemon heartbeat for {int(heartbeat_age)}s"
                )
            elif heartbeat_age is None:
                reason = "waiting for the daemon's first heartbeat"
            else:
                reason = f"daemon active {int(heartbeat_age)}s ago"
            return {
                "ok": ok,
                "schema_version": version,
                "heartbeat_age_seconds": heartbeat_age,
                "reason": reason,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "reason": str(e)}

    @app.context_processor
    def _inject_health():
        # Every page shows the daemon's status in the header, so it must not
        # depend on a single route remembering to pass it.
        return {"health": _health_snapshot()}

    @app.route("/healthz")
    def healthz():
        snapshot = _health_snapshot()
        return jsonify(snapshot), 200 if snapshot["ok"] else 503

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
