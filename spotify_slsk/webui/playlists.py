"""Dashboard, playlist page, and playlist add/toggle/delete/refresh."""
from __future__ import annotations

import logging
import os

import requests
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import db, runtime_state
from ..spotify.client import fetch_playlist_name
from ..utils import get_playlist_id, sanitize_table_name
from .helpers import dir_size, wake_daemon

bp = Blueprint("playlists", __name__)


@bp.route("/")
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
        pl["disk_bytes"] = dir_size(os.path.join(playlists_root, pl["table_name"]))
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


@bp.route("/playlist/<playlist_id>")
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


# ---- playlist mutations -------------------------------------------

@bp.route("/playlists", methods=["POST"])
def add_playlist():
    url = (request.form.get("playlist_url") or "").strip()
    pid = get_playlist_id(url)
    if not pid:
        flash("That doesn't look like a Spotify playlist URL.", "error")
        return redirect(url_for("playlists.dashboard"))

    sp = current_app.config.get("SPOTIFY")
    if sp is not None:
        try:
            name = fetch_playlist_name(sp, pid)
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
            return redirect(url_for("playlists.dashboard"))
    else:
        # Daemon's Spotify client isn't wired in — store the id as
        # placeholder name; the first sync fills in the real name.
        name = pid

    # snapshot_id intentionally NULL so the first cycle fetches tracks.
    table = sanitize_table_name(name)
    db.upsert_playlist_meta(g.conn, pid, table, name, None)
    db.create_table(g.conn, table)
    wake_daemon()
    flash(f'Added "{name}". Cycle starting now.', "success")
    return redirect(url_for("playlists.dashboard"))


@bp.route("/playlist/<playlist_id>/toggle", methods=["POST"])
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
        wake_daemon()
    return redirect(url_for("playlists.dashboard"))


@bp.route("/playlist/<playlist_id>/delete", methods=["POST"])
def delete_playlist(playlist_id):
    # Dropping the rows is irreversible: require the explicit second
    # step from the dashboard's confirm disclosure.
    if request.form.get("confirm") != "1":
        flash("Removal not confirmed; nothing was deleted.", "error")
        return redirect(url_for("playlists.dashboard"))
    if db.remove_playlist(g.conn, playlist_id):
        flash("Playlist removed. Files on disk were kept.", "success")
    else:
        flash("Playlist not found.", "error")
    return redirect(url_for("playlists.dashboard"))


@bp.route("/playlist/<playlist_id>/refresh", methods=["POST"])
def refresh_playlist(playlist_id):
    # Wipe the stored snapshot so the next cycle re-fetches from Spotify
    # even if the playlist hasn't changed upstream.
    try:
        with g.conn:
            g.conn.execute(
                "UPDATE playlists_meta SET snapshot_id = NULL WHERE playlist_id = ?",
                (playlist_id,),
            )
        wake_daemon()
        flash("Re-fetching from Spotify now.", "success")
    except Exception as e:
        flash(f"Could not queue refresh: {e}", "error")
    return redirect(url_for("playlists.playlist_detail", playlist_id=playlist_id))
