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
    url_for,
)

import db
from utils import get_playlist_id, sanitize_table_name


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


def create_app(db_path: str, spotify_client=None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )
    app.config["DB_PATH"] = db_path
    app.config["SPOTIFY"] = spotify_client
    app.secret_key = os.getenv("UI_SECRET_KEY", os.urandom(32).hex())

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
        for pl in playlists:
            pl["stats"] = db.playlist_stats(g.conn, pl["table_name"])
        return render_template("dashboard.html", playlists=playlists)

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
        return render_template(
            "track.html",
            playlist_id=playlist_id,
            playlist_name=playlist_name,
            track=track,
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
        flash(f'Added "{name}".', "success")
        return redirect(url_for("dashboard"))

    @app.route("/playlist/<playlist_id>/toggle", methods=["POST"])
    def toggle_playlist(playlist_id):
        row = g.conn.execute(
            "SELECT enabled FROM playlists_meta WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if not row:
            abort(404)
        db.set_playlist_enabled(g.conn, playlist_id, not bool(row[0]))
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
            flash("Will re-fetch on the next cycle.", "success")
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
        flash("Cleared attempts and rejected-file history.", "success")
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
    host: str = "0.0.0.0",
    port: int = 8000,
) -> threading.Thread:
    """Start the Flask app on a daemon thread. Uses the stdlib WSGI server;
    fine for a single-user dashboard — no need for gunicorn here."""
    app = create_app(db_path, spotify_client=spotify_client)

    def _run():
        try:
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except Exception:
            logging.exception("Web UI thread crashed")

    t = threading.Thread(target=_run, daemon=True, name="webui")
    t.start()
    logging.info(f"Web UI listening on http://{host}:{port}")
    return t
