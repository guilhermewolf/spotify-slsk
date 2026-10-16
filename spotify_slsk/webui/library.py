"""Library -> Rename: preview old -> new names for the current template,
then queue the selected renames for the daemon (runtime_state.enqueue_rename).

POST without `confirm` renders a confirmation page (no JS); POST with it
queues. The new names are recomputed here from the DB and the current
template - the form only carries which tracks, never a name or a path.
"""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from .. import db, runtime_state
from ..library import naming
from ..library.rename import rename_plan
from .helpers import wake_daemon

bp = Blueprint("library", __name__)


def _template() -> str:
    return db.get_setting(g.conn, "FILE_NAME_TEMPLATE", naming.DEFAULT_TEMPLATE) or naming.DEFAULT_TEMPLATE


def _plans(template: str, playlist_id: str | None = None) -> list[dict]:
    plans = []
    for pl in db.list_playlists(g.conn):
        if playlist_id and pl["playlist_id"] != playlist_id:
            continue
        items, skipped = rename_plan(g.conn, pl["table_name"], template)
        plans.append({**pl, "renames": items, "skipped": skipped})
    return plans


@bp.route("/library/rename")
def rename_page() -> ResponseReturnValue:
    template = _template()
    playlist_id = request.args.get("playlist") or None
    return render_template(
        "rename.html",
        template=template,
        template_error=naming.validate_template(template),
        is_default=template == naming.DEFAULT_TEMPLATE,
        plans=_plans(template, playlist_id),
        playlists=db.list_playlists(g.conn),
        playlist_id=playlist_id,
        queued=runtime_state.pending_renames(),
    )


@bp.route("/library/rename", methods=["POST"])
def rename_apply() -> ResponseReturnValue:
    template = _template()
    if naming.validate_template(template):
        flash("The file name template is invalid; fix it on Settings first.", "error")
        return redirect(url_for("library.rename_page"))
    selected = set(request.form.getlist("tracks"))
    chosen = [
        (pl, item)
        for pl in _plans(template)
        for item in pl["renames"]
        if f"{pl['playlist_id']}:{item['id']}" in selected
    ]
    if not chosen:
        flash("No files selected (or they no longer need renaming).", "error")
        return redirect(url_for("library.rename_page"))
    if request.form.get("confirm") != "1":
        return render_template("rename_confirm.html", chosen=chosen, template=template)
    for pl, item in chosen:
        runtime_state.enqueue_rename(pl["table_name"], item["id"], item["path"], item["new_name"])
    wake_daemon()
    flash(
        f"Queued {len(chosen)} rename(s). The daemon applies them at the start of the next cycle.",
        "success",
    )
    return redirect(url_for("library.rename_page"))
