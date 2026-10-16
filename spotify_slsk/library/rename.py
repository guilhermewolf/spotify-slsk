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
        if new_name != old_name:
            plan.append({**row, "old_name": old_name, "new_name": new_name})
    return plan, skipped


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
