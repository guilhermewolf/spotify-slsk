"""Audio preview file serving, live logs/activity, and /healthz."""
from __future__ import annotations

import os

from flask import (
    Blueprint,
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
from flask.typing import ResponseReturnValue

from .. import db, runtime_state, tasks
from ..library.health import SCAN_PENDING_KEY
from .helpers import health_snapshot, wake_daemon

bp = Blueprint("system", __name__)


# ---- file serving (audio preview) ---------------------------------

@bp.route("/file")
def serve_file() -> ResponseReturnValue:
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


# ---- live observability ------------------------------------------

@bp.route("/logs")
def logs_page() -> ResponseReturnValue:
    snapshot = runtime_state.get_logs(since=0, limit=500)
    return render_template(
        "logs.html",
        entries=snapshot["entries"],
        last_seq=snapshot["last_seq"],
    )


@bp.route("/logs.json")
def logs_json() -> ResponseReturnValue:
    try:
        since = int(request.args.get("since", "0"))
    except (TypeError, ValueError):
        since = 0
    try:
        limit = max(1, min(2000, int(request.args.get("limit", "500"))))
    except (TypeError, ValueError):
        limit = 500
    return jsonify(runtime_state.get_logs(since=since, limit=limit))


@bp.route("/activity.json")
def activity_json() -> ResponseReturnValue:
    return jsonify(runtime_state.get_activity())


@bp.route("/healthz")
def healthz() -> ResponseReturnValue:
    snapshot = health_snapshot()
    return jsonify(snapshot), 200 if snapshot["ok"] else 503


# ---- scheduled tasks ----------------------------------------------

@bp.route("/system/tasks")
def tasks_page() -> ResponseReturnValue:
    return render_template("system_tasks.html", tasks=tasks.describe(g.conn))


@bp.route("/system/tasks/<name>/run", methods=["POST"])
def task_run(name: str) -> ResponseReturnValue:
    """Queue a run for the daemon (deduplicated: a second click while the
    first is queued does nothing). The UI never runs a task itself."""
    task = tasks.BY_NAME.get(name)
    if task is None:
        abort(404)
    if name == "library_scan":
        # The scan's own request flag, shared with the Library health button.
        queued = db.get_setting(g.conn, SCAN_PENDING_KEY, "0") != "1"
        db.set_setting(g.conn, SCAN_PENDING_KEY, "1")
    else:
        queued = db.queue_task(g.conn, name)
    wake_daemon()
    if queued:
        flash(f"{task.label}: queued.", "success")
    else:
        flash(f"{task.label} is already queued.", "success")
    return redirect(url_for("system.tasks_page"))
