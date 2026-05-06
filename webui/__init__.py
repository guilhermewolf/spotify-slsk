"""Flask dashboard for spotify-slsk.

Runs in a thread alongside the daemon. Uses per-request SQLite connections
against the same DB file the daemon writes to (WAL mode makes this safe).
"""
from __future__ import annotations

import logging
import os
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
    url_for,
)

import requests

import db
import runtime_state
from utils import get_playlist_id, sanitize_table_name


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


SETTINGS_SPEC = [
    ("MIN_MATCH_SCORE", "0.62", "Accept threshold for downloaded-file -> DB match (0.0-1.0)"),
    ("SLSKD_PREFERRED_FORMATS", "flac,mp3,aiff,wav", "Comma-separated, ordered — first format wins ties"),
    ("SLSKD_MIN_PEER_UPLOAD_SPEED", "0", "Passed to slskd; bytes/sec"),
    ("SLSKD_MIN_EFFECTIVE_MP3_KBPS", "280", "Reject MP3s whose size/duration yields below this"),
    ("SLSKD_EARLY_STOP_RESPONSES", "20", "Stop the slskd search once this many peers replied"),
    ("SLSKD_MAX_RETRIES", "2", "Per-search download attempt cap"),
    ("SLSKD_WAIT_TIMEOUT", "60", "Seconds to wait for a downloaded file to appear on disk"),
    ("UPGRADE_CHECK_INTERVAL_HOURS", "168", "Re-search downloaded tracks for a better version_tier (Extended Mix > Original Mix); 0 disables"),
]


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
    app.secret_key = os.getenv("UI_SECRET_KEY", os.urandom(32).hex())
    app.jinja_env.filters["human_bytes"] = _human_bytes

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
        for pl in playlists:
            pl["stats"] = db.playlist_stats(g.conn, pl["table_name"])
            pl["disk_bytes"] = _dir_size(os.path.join(playlists_root, pl["table_name"]))
        activity = runtime_state.get_activity()
        cycles = db.list_cycles(g.conn, limit=10)
        return render_template(
            "dashboard.html",
            playlists=playlists,
            activity=activity,
            cycles=cycles,
        )

    @app.route("/playlist/<playlist_id>")
    def playlist_detail(playlist_id):
        _, table_name, name, last_synced = db.get_playlist_meta(g.conn, playlist_id)
        if not table_name:
            abort(404)
        tracks = db.list_tracks(g.conn, table_name)
        status_filter = request.args.get("status")
        if status_filter:
            tracks = [t for t in tracks if t["status"] == status_filter]
        return render_template(
            "playlist.html",
            playlist_id=playlist_id,
            name=name,
            last_synced=last_synced,
            tracks=tracks,
            status_filter=status_filter,
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
        # Compute the playlists-root-relative path for the audio preview link.
        # If the file lives outside the root (legacy data, manual move) we
        # just don't render the preview — serve_file would 404 anyway.
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
                info = sp.playlist(pid, fields="name")
                name = info["name"]
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
            for key, _default, _help in SETTINGS_SPEC:
                val = (request.form.get(key) or "").strip()
                if val:
                    db.set_setting(g.conn, key, val)
                else:
                    db.delete_setting(g.conn, key)
            flash("Saved. Takes effect on the next cycle.", "success")
            return redirect(url_for("settings"))

        overrides = db.list_settings(g.conn)
        effective = {
            key: db.get_setting(g.conn, key, default=default)
            for key, default, _help in SETTINGS_SPEC
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

    @app.route("/healthz")
    def healthz():
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
            return (
                jsonify(
                    {
                        "ok": ok,
                        "schema_version": version,
                        "heartbeat_age_seconds": heartbeat_age,
                    }
                ),
                200 if ok else 503,
            )
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 503

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
