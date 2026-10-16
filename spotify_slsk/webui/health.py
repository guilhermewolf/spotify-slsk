"""Library health page: what the scan found, plus scan / quarantine / ignore.

Read-mostly. The only DB write here is the operator's "ignore" mark and the
scan request flag; quarantining moves files, so it is queued for the daemon
(runtime_state.enqueue_quarantine) like a manual grab.
"""
from __future__ import annotations

import time
from typing import Any

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from .. import db, runtime_state
from ..library.health import (
    ISRC_NOTE,
    PROBLEM_LABELS,
    PROBLEMS,
    QUARANTINE_DIR,
    SCAN_COMPLETED_KEY,
    SCAN_PENDING_KEY,
)
from .helpers import wake_daemon

bp = Blueprint("health", __name__)


def _flagged_rows() -> list[dict[str, Any]]:
    """Checks with problems (or an ISRC note) whose track still has that
    exact file."""
    tables = {
        p["table_name"]: p for p in db.list_playlists(g.conn)
    }
    rows = []
    for check in db.list_file_checks(g.conn):
        pl = tables.get(check["table_name"])
        if pl is None or not (check["problems"] or check["evidence"].get(ISRC_NOTE)):
            continue
        track = db.get_track(g.conn, check["table_name"], check["track_id"])
        if not track or not track["downloaded"] or track["path"] != check["path"]:
            continue  # stale: re-downloaded or quarantined since
        check.update(track=track, playlist_id=pl["playlist_id"], playlist_name=pl["name"])
        rows.append(check)
    return rows


@bp.route("/library-health")
def library_health() -> ResponseReturnValue:
    all_rows = _flagged_rows()
    problem = request.args.get("problem") or None
    if problem not in (*PROBLEMS, ISRC_NOTE):
        problem = None
    playlist_id = request.args.get("playlist") or None
    show_ignored = request.args.get("ignored") == "1"
    counts = {
        p: sum(1 for r in all_rows if p in r["problems"] and not r["ignored"])
        for p in PROBLEMS
    }
    info_count = sum(1 for r in all_rows if r["evidence"].get(ISRC_NOTE) and not r["ignored"])
    rows = [
        r for r in all_rows
        if (show_ignored or not r["ignored"])
        and (
            bool(r["evidence"].get(ISRC_NOTE)) if problem == ISRC_NOTE
            else (problem in r["problems"]) if problem
            else bool(r["problems"])
        )
        and (playlist_id is None or r["playlist_id"] == playlist_id)
    ]
    try:
        completed = float(db.get_setting(g.conn, SCAN_COMPLETED_KEY, "0") or 0)
    except ValueError:
        completed = 0.0
    return render_template(
        "library_health.html",
        active_page="health",
        rows=rows,
        counts=counts,
        info_count=info_count,
        isrc_note=ISRC_NOTE,
        labels=PROBLEM_LABELS,
        problem=problem,
        playlist_id=playlist_id,
        show_ignored=show_ignored,
        ignored_count=sum(1 for r in all_rows if r["ignored"]),
        playlists=db.list_playlists(g.conn),
        scan_pending=db.get_setting(g.conn, SCAN_PENDING_KEY, "0") == "1",
        scan_completed=time.strftime("%Y-%m-%d %H:%M", time.localtime(completed)) if completed else None,
        checked_count=len(db.list_file_checks(g.conn)),
        quarantine_dir=QUARANTINE_DIR,
    )


@bp.route("/library-health/scan", methods=["POST"])
def library_scan() -> ResponseReturnValue:
    db.set_setting(g.conn, SCAN_PENDING_KEY, "1")
    wake_daemon()
    flash("Library scan queued. The daemon checks a few hundred files per cycle.", "success")
    return redirect(url_for("health.library_health"))


def _targets() -> list[tuple[str, str]]:
    """(table_name, track_id) from the clicked row ("only") or the checked
    boxes, restricted to known playlists."""
    tables = {p["playlist_id"]: p["table_name"] for p in db.list_playlists(g.conn)}
    values = [request.form["only"]] if request.form.get("only") else request.form.getlist("tracks")
    out = []
    for value in values:
        pid, _, tid = value.partition(":")
        if pid in tables and tid:
            out.append((tables[pid], tid))
    return out


@bp.route("/library-health/quarantine", methods=["POST"])
def library_quarantine() -> ResponseReturnValue:
    targets = _targets()
    for table_name, track_id in targets:
        # The path the operator saw flagged: the daemon refuses to act if the
        # row points at another file by the time it runs.
        check = db.get_file_check(g.conn, table_name, track_id)
        track = db.get_track(g.conn, table_name, track_id) or {}
        runtime_state.enqueue_quarantine(
            table_name, track_id, check["path"] if check else track.get("path")
        )
    if targets:
        wake_daemon()
        flash(
            f"Queued {len(targets)} file(s) for quarantine. The daemon moves them to "
            f"{QUARANTINE_DIR}/ and downloads the tracks again.",
            "success",
        )
    else:
        flash("No tracks selected.", "error")
    return redirect(url_for("health.library_health"))


@bp.route("/library-health/ignore", methods=["POST"])
def library_ignore() -> ResponseReturnValue:
    undo = request.args.get("undo") == "1"
    changed = sum(
        db.set_check_ignored(g.conn, table_name, track_id, ignored=not undo)
        for table_name, track_id in _targets()
    )
    if changed:
        flash(
            f"{'Un-ignored' if undo else 'Ignored'} {changed} file(s).",
            "success",
        )
    else:
        flash("No tracks selected.", "error")
    return redirect(url_for("health.library_health"))
