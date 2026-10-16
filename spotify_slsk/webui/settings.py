"""Settings page (validated tunables) and the notification test buttons."""
from __future__ import annotations

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

from .. import config, db, notifications
from ..config import SETTINGS_SPEC, validate_setting
from ..library import naming
from ..matching.quality import FLOOR_BLOCKED, LADDER, LADDER_INDEX, make_profile
from .auth import hash_password
from .helpers import wake_daemon

bp = Blueprint("settings", __name__)

MASK = "••••"
AUTH_PARTIAL_WARNING = (
    "Only one of UI_AUTH_USERNAME / UI_AUTH_PASSWORD is set: the login is OFF and "
    "the web UI is open. Set both to require a login, or clear both."
)


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
            if kind == "password":
                # Hashed; the plain value is never stored. Blank keeps it.
                # Not stripped: the login form does not strip either.
                raw = request.form.get(key) or ""
                if request.form.get(f"clear_{key}") == "1":
                    cleaned[config.UI_AUTH_HASH_KEY] = None
                if raw:
                    cleaned[config.UI_AUTH_HASH_KEY] = hash_password(raw)
                continue
            if kind == "secret":
                # Never rendered back, so an empty field means "keep".
                if request.form.get(f"clear_{key}") == "1":
                    cleaned[key] = None
                if not val:
                    continue
            elif not val:
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
    # Secrets never reach the page: only whether one is set.
    stored = db.list_settings(g.conn)
    for key, _default, _help, kind in SETTINGS_SPEC:
        if kind == "secret":
            effective[key] = MASK if effective[key] else ""
            if key in overrides:
                overrides[key] = MASK
        elif kind == "password":
            in_db = bool(stored.get(config.UI_AUTH_HASH_KEY))
            effective[key] = MASK if in_db or effective[key] else ""
            overrides.pop(key, None)
            if in_db:
                overrides[key] = MASK
    profile = config.load_quality_profile(g.conn)
    # Allowed qualities in profile order, then the rest in ladder order.
    quality_rows = sorted(LADDER, key=lambda q: profile.rank(q.key))
    return render_template(
        "settings.html",
        auth_partial_warning=(
            AUTH_PARTIAL_WARNING if config.ui_auth_state(g.conn) == "partial" else None
        ),
        spec=SETTINGS_SPEC,
        overrides=overrides,
        effective=effective,
        profile=profile,
        quality_rows=quality_rows,
        floor_blocked=FLOOR_BLOCKED,
        name_example=naming.example(effective["FILE_NAME_TEMPLATE"]),
        name_example_source=(
            f"'{naming.EXAMPLE_ROW['name']}' by {naming.EXAMPLE_ROW['artists']}, "
            f"track {naming.EXAMPLE_ROW['track_number']}, playlist #{naming.EXAMPLE_ROW['position']}, "
            f"peer file '{naming.EXAMPLE_ORIGINAL}'"
        ),
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


# ---- notification test ---------------------------------------------

@bp.route("/settings/test-notification/<provider>", methods=["POST"])
def test_notification(provider: str) -> ResponseReturnValue:
    """Send one test message (or Subsonic scan) with the saved settings.
    Errors show the HTTP status or exception type only: a URL can carry a
    token, and requests' messages include it."""
    if provider not in (*notifications.PROVIDERS, "subsonic"):
        abort(404)
    try:
        flash(notifications.send_test(g.conn, provider) + ".", "success")
    except Exception as e:
        flash(f"Test failed: {notifications.safe_error(e)}", "error")
    return redirect(url_for("settings.settings") + "#notifications")
