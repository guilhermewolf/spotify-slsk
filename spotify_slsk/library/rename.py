"""Rename existing library files to the current FILE_NAME_TEMPLATE.

The template only names new imports on its own; existing files change only
through Library -> Rename: `rename_plan` is the preview (read-only, any
thread), `apply_rename` the daemon-side action the UI queues. A rename never
leaves the file's folder, never touches anything outside the playlists
root, skips missing files, and never overwrites (" (2)" on a collision).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any

from ..db import get_track, list_tracks, record_history, set_track_path
from ..utils import free_path
from .health import _inside
from .naming import file_name


def _root() -> str:
    return os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")


def rename_plan(
    conn: sqlite3.Connection, table_name: str, template: str
) -> tuple[list[dict[str, Any]], int]:
    """(renames, skipped) for one playlist table. Each rename is the track
    row plus old_name/new_name; `skipped` counts missing files and files
    outside the playlists root. Rows whose name already fits are left out."""
    root, plan, skipped = _root(), [], 0
    for row in list_tracks(conn, table_name):
        path = row["path"]
        if not row["downloaded"] or not path:
            continue
        if not os.path.isfile(path) or not _inside(path, root):
            skipped += 1
            continue
        old_name = os.path.basename(path)
        new_name = file_name(template, row, old_name, row["quality"])
        if not already_named(old_name, new_name):
            plan.append({**row, "old_name": old_name, "new_name": new_name, "collision": False})
    # Two planned targets with one name, or a target taken by another file:
    # the later one(s) will get " (2)" - say so in the preview.
    seen: dict[str, int] = {}
    for item in plan:
        folder = os.path.dirname(item["path"])
        key = os.path.join(folder, item["new_name"]).casefold()
        seen[key] = seen.get(key, 0) + 1
        target = os.path.join(folder, item["new_name"])
        taken = os.path.lexists(target) and not _same_file(item["path"], target)
        item["collision"] = seen[key] > 1 or taken
    return plan, skipped


def already_named(old_name: str, new_name: str) -> bool:
    """True when the file already carries the target name, or the target
    name with a collision suffix (" (2)"): renaming it again would only
    churn between suffixes."""
    old_stem, old_ext = os.path.splitext(old_name)
    new_stem, new_ext = os.path.splitext(new_name)
    if old_ext != new_ext:
        return False
    return old_stem == new_stem or re.fullmatch(re.escape(new_stem) + r" \(\d+\)", old_stem) is not None


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def apply_rename(
    conn: sqlite3.Connection, table_name: str, track_id: str, old_path: str, new_name: str
) -> str | None:
    """Rename one file in place (daemon thread only). Returns the new path,
    or None when skipped: the row moved on since the preview, the file is
    gone or outside the playlists root, or `new_name` is not a bare name."""
    row = get_track(conn, table_name, track_id)
    if not row or not row["downloaded"] or row["path"] != old_path:
        logging.info(f"[rename] Skipped {track_id}: file changed since the preview")
        return None
    if (
        not new_name or os.path.basename(new_name) != new_name or new_name in (".", "..")
        or not os.path.isfile(old_path) or not _inside(old_path, _root())
    ):
        logging.warning(f"[rename] Skipped {old_path} -> {new_name!r}: not safe to rename")
        return None
    if already_named(os.path.basename(old_path), new_name):
        logging.debug(f"[rename] {old_path} already named {new_name!r}; nothing to do")
        return None
    directory = os.path.dirname(old_path)
    target = os.path.join(directory, new_name)
    # A case-only change on a case-insensitive share "exists" as itself.
    if os.path.lexists(target) and not _same_file(old_path, target):
        target = free_path(directory, new_name)
    try:
        os.rename(old_path, target)
    except OSError as e:
        logging.error(f"[rename] Could not rename {old_path}: {e}")
        return None
    set_track_path(conn, table_name, track_id, target)
    record_history(
        conn, "renamed", table_name=table_name, track_id=track_id,
        old_path=old_path, path=target,
    )
    logging.info(f"[rename] {old_path} -> {target}")
    return target
