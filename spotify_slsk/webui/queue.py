"""Live queue: slskd's download transfers, joined to our tracks, plus what
the UI has queued for the daemon (manual grabs, quarantines, renames,
upgrade checks).

slskd is read with a client built per request on this (web) thread -
``new_client``, never the daemon's shared one, whose requests.Session is not
thread-safe - and a short timeout, since the page polls every 3 s.

Cancelling is the one slskd write the UI makes. The daemon, if it is
waiting on that transfer, sees "Completed, Cancelled": a failure that is
not the peer's fault, so the file goes to the track's tried list and is
never blocklisted automatically. "Cancel + blocklist" adds that one file to
the blocklist, as an operator decision.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from flask import (
    Blueprint,
    abort,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from .. import db, runtime_state
from ..slskd import client as slskd_client
from ..slskd.models import Transfer, remote_basename
from .helpers import is_htmx, notify_user

bp = Blueprint("queue", __name__)

QUEUE_HTTP_TIMEOUT = 5.0


def _track_index() -> dict[tuple[str, str], tuple[str, str]]:
    """(username, peer filename) -> (playlist_id, track_id), from the
    daemon's current transfer and recent grab/failure history."""
    index: dict[tuple[str, str], tuple[str, str]] = {}
    for event in ("download_failed", "grabbed"):  # grabbed wins
        for h in reversed(db.list_history(g.conn, event=event, limit=500)):
            user, filename = h["data"].get("username"), h["data"].get("filename")
            if user and filename and h["playlist_id"] and h["track_id"]:
                index[(user, filename)] = (h["playlist_id"], h["track_id"])
    current = runtime_state.get_current_transfer()
    if current:
        pid = {p["table_name"]: p["playlist_id"] for p in db.list_playlists(g.conn)}.get(
            current["table_name"]
        )
        if pid:
            index[(current["username"], current["filename"])] = (pid, current["track_id"])
    return index


def _transfers() -> tuple[list[dict[str, Any]], str | None]:
    try:
        raw = slskd_client.new_client(timeout=QUEUE_HTTP_TIMEOUT).transfers.get_all_downloads()
    except Exception as e:
        logging.debug("Queue page could not read slskd", exc_info=True)
        return [], f"slskd unavailable ({type(e).__name__})"
    playlists = {p["playlist_id"]: p for p in db.list_playlists(g.conn)}
    index = _track_index()
    rows = []
    for t in Transfer.list_from_all_json(raw):
        row: dict[str, Any] = {
            "transfer": t,
            "basename": remote_basename(t.filename),
            "done": t.outcome is not None,
            "percent": t.percent_complete or 0,
            "eta": t.eta_seconds,
        }
        link = index.get((t.username or "", t.filename))
        if link and link[0] in playlists:
            pl = playlists[link[0]]
            track = db.get_track(g.conn, pl["table_name"], link[1])
            row.update(playlist_id=link[0], playlist_name=pl["name"], track_id=link[1],
                       track_name=f"{track['name']} — {track['artists']}" if track else link[1])
        rows.append(row)
    # Active first, then by state (InProgress before Queued), newest-looking last.
    rows.sort(key=lambda r: (r["done"], "progress" not in r["transfer"].state.lower()))
    return rows, None


def _pending_requests() -> dict[str, list[dict[str, Any]]]:
    """What the UI queued and the daemon has not picked up yet."""
    def name(table: str, track_id: str) -> str:
        track = db.get_track(g.conn, table, track_id)
        return f"{track['name']} — {track['artists']}" if track else track_id

    return {
        "Manual grabs": [
            {"track": name(r["table_name"], r["track_id"]),
             "detail": f"{remote_basename(r['candidate'].get('filename'))} from {r['candidate'].get('user')}"}
            for r in runtime_state.peek_manual_grabs()
        ],
        "Quarantines": [
            {"track": name(t, tid), "detail": path or ""}
            for t, tid, path in runtime_state.peek_quarantines()
        ],
        "Renames": [
            {"track": name(t, tid), "detail": new}
            for t, tid, _old, new in runtime_state.peek_renames()
        ],
        "Upgrade checks": [
            {"track": name(t, tid), "detail": ""}
            for t, tid in runtime_state.peek_upgrade_checks()
        ],
    }


@bp.route("/queue")
def queue_page() -> ResponseReturnValue:
    rows, error = _transfers()
    return render_template(
        "queue.html", active_page="queue", rows=rows, error=error, pending=_pending_requests()
    )


def _after_action() -> ResponseReturnValue:
    """htmx: the refreshed queue (swapped in) plus a toast; else redirect."""
    return queue_page() if is_htmx() else redirect(url_for("queue.queue_page"))


# slskd transfer ids are GUIDs. slskd_api interpolates the id into the URL
# path unquoted, so anything else could walk to another slskd endpoint
# with our API key.
_TRANSFER_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@bp.route("/queue/cancel", methods=["POST"])
def queue_cancel() -> ResponseReturnValue:
    username = request.form.get("username") or ""
    transfer_id = request.form.get("id") or ""
    if not username or not _TRANSFER_ID_RE.fullmatch(transfer_id):
        abort(400)
    blocklist = request.form.get("blocklist") == "1"
    try:
        client = slskd_client.new_client(timeout=QUEUE_HTTP_TIMEOUT)
        # Only a transfer slskd lists right now, for that user, is cancelled;
        # and the blocklist gets what slskd says it is, not what the form says.
        transfer = next(
            (t for t in Transfer.list_from_all_json(client.transfers.get_all_downloads())
             if t.id == transfer_id and t.username == username),
            None,
        )
        if transfer is None:
            notify_user("That transfer is no longer in slskd's queue.", "error")
            return _after_action()
        ok = client.transfers.cancel_download(username, transfer_id, remove=False)
    except Exception as e:
        notify_user(f"Could not reach slskd ({type(e).__name__}).", "error")
        return _after_action()
    if not ok:
        notify_user("slskd refused to cancel that transfer (already finished?).", "error")
        return _after_action()
    name = remote_basename(transfer.filename) or transfer_id
    if blocklist and transfer.filename:
        link = _track_index().get((username, transfer.filename))
        db.add_blocklist(
            g.conn, username, transfer.filename, transfer.size,
            "cancelled and blocklisted from the queue page", link[1] if link else None,
        )
        notify_user(f"Cancelled and blocklisted {name} from {username}.", "success")
    else:
        notify_user(f"Cancelled {name} from {username}.", "success")
    return _after_action()
