"""Settings page (validated tunables) and the ntfy probe."""
from __future__ import annotations

import os

import requests
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import db
from ..config import SETTINGS_SPEC, validate_setting
from .helpers import wake_daemon

bp = Blueprint("settings", __name__)


# ---- settings -----------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        # Validate everything before writing anything, so a form with one
        # bad field doesn't half-apply.
        cleaned, errors = {}, []
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
    return render_template(
        "settings.html",
        spec=SETTINGS_SPEC,
        overrides=overrides,
        effective=effective,
    )


# ---- ntfy probe ---------------------------------------------------

@bp.route("/settings/test-ntfy", methods=["POST"])
def test_ntfy():
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
