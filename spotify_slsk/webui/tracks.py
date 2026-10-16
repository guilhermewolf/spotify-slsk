"""Track page, retry/re-tag, and the interactive slskd search + grab."""
from __future__ import annotations

import logging
import os
import threading

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from .. import db, runtime_state
from ..importer.tags import write_tags
from ..slskd import search as slskd_search
from .helpers import relative_to_playlists_root, wake_daemon

bp = Blueprint("tracks", __name__)


@bp.route("/track/<playlist_id>/<track_id>")
def track_detail(playlist_id: str, track_id: str) -> ResponseReturnValue:
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
    track["rel_path"] = relative_to_playlists_root(track.get("path"))
    return render_template(
        "track.html",
        playlist_id=playlist_id,
        playlist_name=playlist_name,
        track=track,
        search_history=runtime_state.get_search_history(track_id),
        history=db.list_history(g.conn, track_id=track_id, limit=50),
        check=db.get_file_check(g.conn, table_name, track_id),
        search=runtime_state.get_interactive_search((playlist_id, track_id)),
    )


# ---- track mutations ----------------------------------------------

@bp.route("/track/<playlist_id>/<track_id>/retry", methods=["POST"])
def retry_track(playlist_id: str, track_id: str) -> ResponseReturnValue:
    _, table_name, _, _ = db.get_playlist_meta(g.conn, playlist_id)
    if not table_name:
        abort(404)
    db.retry_track(g.conn, table_name, track_id)
    db.record_history(g.conn, "retried", playlist_id=playlist_id, track_id=track_id)
    wake_daemon()
    flash(
        "Cleared attempts and rejected-file history. Cycle starting now.",
        "success",
    )
    return redirect(
        url_for("tracks.track_detail", playlist_id=playlist_id, track_id=track_id)
    )


# ---- interactive search -------------------------------------------


def _track_or_404(playlist_id, track_id):
    _, table_name, _, _ = db.get_playlist_meta(g.conn, playlist_id)
    if not table_name:
        abort(404)
    track = db.get_track(g.conn, table_name, track_id)
    if not track:
        abort(404)
    return table_name, track


@bp.route("/track/<playlist_id>/<track_id>/search", methods=["POST"])
def interactive_search_start(playlist_id: str, track_id: str) -> ResponseReturnValue:
    _, track = _track_or_404(playlist_id, track_id)
    key = (playlist_id, track_id)
    token = runtime_state.start_interactive_search(key)
    if token is None:
        flash("A search for this track is already running.", "error")
    else:
        # Snapshot the blocklist here: the search thread gets no DB.
        is_blocked = db.load_blocklist(g.conn)

        def _run():
            try:
                results = slskd_search.interactive_search(
                    track["artists"],
                    track["name"],
                    album=track["album"],
                    duration_ms=track["duration_ms"],
                    is_blocked=is_blocked,
                )
                runtime_state.finish_interactive_search(key, token, results=results)
            except Exception as e:
                logging.exception("Interactive search failed")
                runtime_state.finish_interactive_search(key, token, error=str(e))

        threading.Thread(target=_run, daemon=True, name="interactive-search").start()
        flash("Searching slskd. Results appear below as soon as it finishes.", "success")
    return redirect(
        url_for("tracks.track_detail", playlist_id=playlist_id, track_id=track_id)
        + "#interactive"
    )


@bp.route("/track/<playlist_id>/<track_id>/search")
def interactive_search_results(playlist_id: str, track_id: str) -> ResponseReturnValue:
    _track_or_404(playlist_id, track_id)
    return render_template(
        "_interactive.html",
        playlist_id=playlist_id,
        track_id=track_id,
        search=runtime_state.get_interactive_search((playlist_id, track_id)),
    )


@bp.route("/track/<playlist_id>/<track_id>/grab", methods=["POST"])
def interactive_grab(playlist_id: str, track_id: str) -> ResponseReturnValue:
    table_name, track = _track_or_404(playlist_id, track_id)
    back = url_for("tracks.track_detail", playlist_id=playlist_id, track_id=track_id)
    search = runtime_state.get_interactive_search((playlist_id, track_id))
    username = request.form.get("username", "")
    filename = request.form.get("filename", "")
    # Only results of this track's own search can be grabbed — the form
    # is not a way to make the daemon fetch arbitrary peer paths.
    candidate = next(
        (
            c
            for c in (search or {}).get("results", [])
            if c["user"] == username
            and c["filename"] == filename
            and str(c["size"]) == request.form.get("size", "")
        ),
        None,
    )
    if candidate is None:
        flash("That result has expired. Run the search again.", "error")
        return redirect(back)
    if not candidate["accepted"] and request.form.get("force") != "1":
        return render_template(
            "grab_confirm.html",
            playlist_id=playlist_id,
            track=track,
            candidate=candidate,
        )
    runtime_state.enqueue_manual_grab(
        {
            "playlist_id": playlist_id,
            "table_name": table_name,
            "track_id": track_id,
            "candidate": candidate,
        }
    )
    wake_daemon()
    flash("Grab queued. The daemon will download it next.", "success")
    return redirect(back)


# ---- track maintenance --------------------------------------------

@bp.route("/track/<playlist_id>/<track_id>/retag", methods=["POST"])
def retag_track(playlist_id: str, track_id: str) -> ResponseReturnValue:
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
            write_tags(track["path"], track["name"], track["artists"], track["album"])
            flash("Re-tagged from DB metadata.", "success")
        except Exception as e:
            flash(f"Re-tag failed: {e}", "error")
    return redirect(
        url_for("tracks.track_detail", playlist_id=playlist_id, track_id=track_id)
    )
