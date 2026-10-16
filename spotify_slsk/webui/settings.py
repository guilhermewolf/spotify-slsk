"""Settings page (validated tunables) and the ntfy probe."""
from __future__ import annotations

import os

import requests
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from .. import config, db
from ..config import SETTINGS_SPEC, validate_setting
from ..matching.quality import FLOOR_BLOCKED, LADDER, LADDER_INDEX, make_profile
from .helpers import wake_daemon

bp = Blueprint("settings", __name__)


# ---- settings -----------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings() -> ResponseReturnValue:
    if request.method == "POST":
        # Validate everything before writing anything, so a form with one
        # bad field doesn't half-apply.
        cleaned: dict[str, str | None] = {}
        errors: list[str] = []
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
            return redirect(url_for("settings.settings"))

        for key, value in cleaned.items():
            if value is None:
                db.delete_setting(g.conn, key)
            else:
                db.set_setting(g.conn, key, value)
        wake_daemon()
        flash("Saved. Takes effect on the next cycle.", "success")
        return redirect(url_for("settings.settings"))

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
    profile = config.load_quality_profile(g.conn) or config.QUALITY_PROFILE
    # Allowed qualities in profile order, then the rest in ladder order.
    quality_rows = sorted(LADDER, key=lambda q: profile.rank(q.key))
    return render_template(
        "settings.html",
        spec=SETTINGS_SPEC,
        overrides=overrides,
        effective=effective,
        profile=profile,
        quality_rows=quality_rows,
        floor_blocked=FLOOR_BLOCKED,
    )


# ---- quality profile ----------------------------------------------

@bp.route("/settings/quality", methods=["POST"])
def save_quality_profile() -> ResponseReturnValue:
    """Plain form controls, no JS: a checkbox per quality, a numeric order
    field (ties keep ladder order), a cutoff select and an upgrades box.
    Validated as a whole (make_profile) before anything is written."""
    allowed = request.form.getlist("allowed")
    order: dict[str, int] = {}
    for key in allowed:
        raw = (request.form.get(f"order_{key}") or "").strip()
        try:
            order[key] = int(raw) if raw else 1000
        except ValueError:
            flash(f"Quality profile: the order of {key} must be a whole number", "error")
            return redirect(url_for("settings.settings") + "#quality")
    ordered = sorted(allowed, key=lambda k: (order.get(k, 1000), LADDER_INDEX.get(k, 99)))
    profile, error = make_profile(
        ordered,
        request.form.get("cutoff") or "",
        request.form.get("upgrades_allowed") == "1",
    )
    if profile is None:
        flash(f"Quality profile: {error}", "error")
        return redirect(url_for("settings.settings") + "#quality")
    db.set_setting(g.conn, config.QUALITY_PROFILE_KEY, profile.to_json())
    wake_daemon()
    flash("Quality profile saved. Takes effect on the next cycle.", "success")
    return redirect(url_for("settings.settings") + "#quality")


# ---- ntfy probe ---------------------------------------------------

@bp.route("/settings/test-ntfy", methods=["POST"])
def test_ntfy() -> ResponseReturnValue:
    """Fire one ntfy notification using the configured URL+topic."""
    ntfy_url = os.getenv("NTFY_URL", "").strip()
    ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
    if not ntfy_url or not ntfy_topic:
        flash("NTFY_URL or NTFY_TOPIC is not set in the environment.", "error")
        return redirect(url_for("settings.settings"))
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
    return redirect(url_for("settings.settings"))
