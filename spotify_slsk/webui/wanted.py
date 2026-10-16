"""Wanted (every missing track), history, and blocklist pages."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from .. import config, db, runtime_state
from ..importer.audio_check import SUSPECT
from ..matching.quality import describe, effective_quality
from .helpers import wake_daemon

bp = Blueprint("wanted", __name__)


# ---- wanted -------------------------------------------------------


def _wanted_tracks():
    """Every not-downloaded track across enabled playlists."""
    rows = []
    for pl in db.list_playlists(g.conn, only_enabled=True):
        for t in db.list_tracks(g.conn, pl["table_name"]):
            if not t["downloaded"]:
                t["playlist_id"] = pl["playlist_id"]
                t["playlist_name"] = pl["name"]
                t["table_name"] = pl["table_name"]
                rows.append(t)
    return rows


@bp.route("/wanted")
def wanted_page() -> ResponseReturnValue:
    all_tracks = _wanted_tracks()
    playlist_id = request.args.get("playlist") or None
    query = (request.args.get("q") or "").strip()
    tracks = all_tracks
    if playlist_id:
        tracks = [t for t in tracks if t["playlist_id"] == playlist_id]
    if query:
        needle = query.lower()
        tracks = [
            t
            for t in tracks
            if needle in (t["name"] or "").lower()
            or needle in (t["artists"] or "").lower()
            or needle in (t["album"] or "").lower()
        ]
    failures = db.latest_failures(g.conn)
    for t in tracks:
        t["last_failure"] = failures.get(t["id"])
    return render_template(
        "wanted.html",
        tracks=tracks,
        total=len(all_tracks),
        playlists=db.list_playlists(g.conn, only_enabled=True),
        playlist_id=playlist_id,
        query=query,
    )


@bp.route("/wanted/search", methods=["POST"])
def wanted_search() -> ResponseReturnValue:
    if request.form.get("all") == "1":
        targets = [(t["playlist_id"], t["table_name"], t["id"]) for t in _wanted_tracks()]
    else:
        tables = {
            p["playlist_id"]: p["table_name"]
            for p in db.list_playlists(g.conn, only_enabled=True)
        }
        targets = []
        for value in request.form.getlist("tracks"):
            pid, _, tid = value.partition(":")
            if pid in tables and tid:
                targets.append((pid, tables[pid], tid))
    count = 0
    for pid, table_name, tid in targets:
        track = db.get_track(g.conn, table_name, tid)
        if not track or track["downloaded"]:
            continue
        db.retry_track(g.conn, table_name, tid)
        db.record_history(g.conn, "retried", playlist_id=pid, track_id=tid, bulk=True)
        count += 1
    if count:
        wake_daemon()
        flash(f"Cleared attempts for {count} track(s). Cycle starting now.", "success")
    else:
        flash("No tracks selected.", "error")
    return redirect(url_for("wanted.wanted_page"))


# ---- cutoff unmet ---------------------------------------------------


def _cutoff_unmet():
    """Downloaded tracks (enabled playlists) whose effective quality is
    below the profile cutoff, or whose lossless file is "suspect". Returns
    (rows, unmeasured_count, profile)."""
    profile = config.load_quality_profile(g.conn) or config.QUALITY_PROFILE
    checks = {(c["table_name"], c["track_id"]): c for c in db.list_file_checks(g.conn)}
    rows, unmeasured = [], 0
    for pl in db.list_playlists(g.conn, only_enabled=True):
        table = pl["table_name"]
        for t in db.list_tracks(g.conn, table):
            if not t["downloaded"]:
                continue
            check = checks.get((table, t["id"]))
            verdict = check["audio_check"] if check and check["path"] == t["path"] else None
            suspect = verdict == SUSPECT
            if t["quality"] is None and not suspect:
                unmeasured += 1
                continue
            effective = effective_quality(t["quality"], verdict)
            if not suspect and not profile.below_cutoff(effective):
                continue
            t.update(
                playlist_id=pl["playlist_id"], playlist_name=pl["name"], table_name=table,
                quality_label=describe(t["quality"]), effective_label=describe(effective),
                suspect=suspect,
            )
            rows.append(t)
    return rows, unmeasured, profile


@bp.route("/wanted/cutoff")
def cutoff_page() -> ResponseReturnValue:
    tracks, unmeasured, profile = _cutoff_unmet()
    playlist_id = request.args.get("playlist") or None
    shown = [t for t in tracks if not playlist_id or t["playlist_id"] == playlist_id]
    return render_template(
        "wanted_cutoff.html",
        tracks=shown,
        total=len(tracks),
        unmeasured=unmeasured,
        profile=profile,
        cutoff_label=describe(profile.cutoff),
        playlists=db.list_playlists(g.conn, only_enabled=True),
        playlist_id=playlist_id,
    )


@bp.route("/wanted/cutoff/search", methods=["POST"])
def cutoff_search() -> ResponseReturnValue:
    """Queue upgrade checks for the daemon (it alone downloads and moves)."""
    listed = {(t["playlist_id"], t["id"]): t["table_name"] for t in _cutoff_unmet()[0]}
    if request.form.get("all") == "1":
        targets = list(listed.items())
    else:
        targets = []
        for value in request.form.getlist("tracks"):
            pid, _, tid = value.partition(":")
            if (pid, tid) in listed:
                targets.append(((pid, tid), listed[(pid, tid)]))
    for (_pid, tid), table_name in targets:
        runtime_state.enqueue_upgrade_check(table_name, tid)
    if targets:
        wake_daemon()
        flash(f"Queued {len(targets)} upgrade check(s). The daemon searches them now.", "success")
    else:
        flash("No tracks selected.", "error")
    return redirect(url_for("wanted.cutoff_page"))


# ---- history ------------------------------------------------------

@bp.route("/history")
def history_page() -> ResponseReturnValue:
    event = request.args.get("event") or None
    if event not in db.HISTORY_EVENTS:
        event = None
    playlist_id = request.args.get("playlist") or None
    try:
        before_id = int(request.args.get("before_id", ""))
    except ValueError:
        before_id = None
    # SQLite integers are 64-bit: a larger bound raises OverflowError
    # (500). Ids are positive, so anything above the max is "no bound".
    if before_id is not None and not 0 < before_id < 2**63:
        before_id = None
    limit = 200
    rows = db.list_history(
        g.conn, event=event, playlist_id=playlist_id, before_id=before_id, limit=limit
    )
    playlists = db.list_playlists(g.conn)
    tables = {p["playlist_id"]: p["table_name"] for p in playlists}
    names = {}
    for row in rows:
        key = (row["playlist_id"], row["track_id"])
        if key not in names and key[0] in tables and key[1]:
            t = db.get_track(g.conn, tables[key[0]], key[1])
            names[key] = f"{t['name']} — {t['artists']}" if t else None
        row["track_label"] = names.get(key)
    return render_template(
        "history.html",
        rows=rows,
        events=db.HISTORY_EVENTS,
        playlists=playlists,
        event=event,
        playlist_id=playlist_id,
        older_id=rows[-1]["id"] if len(rows) == limit else None,
    )


# ---- blocklist ----------------------------------------------------

@bp.route("/blocklist")
def blocklist_page() -> ResponseReturnValue:
    return render_template(
        "blocklist.html", entries=db.list_blocklist(g.conn, limit=500)
    )


@bp.route("/blocklist/<int:entry_id>/delete", methods=["POST"])
def blocklist_delete(entry_id: int) -> ResponseReturnValue:
    if db.remove_blocklist(g.conn, entry_id):
        flash("Removed from the blocklist.", "success")
    else:
        flash("Blocklist entry not found.", "error")
    return redirect(url_for("wanted.blocklist_page"))


@bp.route("/blocklist/ban-user", methods=["POST"])
def blocklist_ban_user() -> ResponseReturnValue:
    username = (request.form.get("username") or "").strip()
    reason = (request.form.get("reason") or "").strip()[:200] or "banned manually"
    if not username or len(username) > 200:
        flash("Enter a Soulseek username.", "error")
    elif db.add_blocklist(g.conn, username, None, None, reason):
        flash(f'Banned "{username}". Their files are skipped from now on.', "success")
    else:
        flash(f'"{username}" is already banned.', "error")
    return redirect(url_for("wanted.blocklist_page"))
