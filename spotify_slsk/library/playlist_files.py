"""Files beside the music in each playlist folder: `<playlist name>.m3u8`
(WRITE_M3U) and `cover.jpg` (WRITE_PLAYLIST_COVER). Daemon thread only.

The m3u8 is UTF-8 extended M3U: downloaded tracks in Spotify playlist order
with `#EXTINF:<seconds>,Artist - Title` and paths relative to the folder.
It carries a `#SPOTIFY-SLSK:<playlist id>` marker, so a playlist renamed on
Spotify replaces its old m3u8 instead of leaving a stale one; m3u8 files
without our marker are never touched. Both files are rewritten only when
their content changed. The matchers and scans never look at them: startup
reconciliation indexes audio extensions only, and the health scan and the
renamer work from DB rows.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from .. import config
from ..db import list_playlists, list_tracks
from ..importer.artwork import fetch_image
from .naming import safe_name

MARKER = "#SPOTIFY-SLSK:"


def m3u8_name(playlist_name: str) -> str:
    return safe_name(playlist_name, ".m3u8") or "playlist.m3u8"


def build_m3u8(playlist: dict[str, Any], tracks: list[dict[str, Any]], folder: str) -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist['name']}", f"{MARKER}{playlist['playlist_id']}"]
    ordered = sorted(
        (t for t in tracks if t["downloaded"] and t["path"] and os.path.isfile(t["path"])),
        key=lambda t: (t["position"] is None, t["position"] or 0, t["name"].lower()),
    )
    for t in ordered:
        seconds = round(t["duration_ms"] / 1000) if t.get("duration_ms") else -1
        title = f"{t['artists']} - {t['name']}".replace("\n", " ")
        lines += [f"#EXTINF:{seconds},{title}", os.path.relpath(t["path"], folder).replace(os.sep, "/")]
    return "\n".join(lines) + "\n"


def _write_if_changed(path: str, data: bytes) -> bool:
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def _remove_stale_m3u8(folder: str, keep: str, playlist_id: str) -> None:
    marker = f"{MARKER}{playlist_id}".encode()
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if name == keep or not name.lower().endswith(".m3u8") or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                if marker in f.read(4096).splitlines():
                    os.remove(path)
                    logging.info(f"[playlist] Removed stale {path}")
        except OSError as e:
            logging.warning(f"[playlist] Could not check {path}: {e}")


def write_playlist_files(conn: sqlite3.Connection, playlist_id: str) -> None:
    """Refresh the m3u8 and cover.jpg of one playlist's folder. Never
    raises (a playlist file is never worth failing a cycle over); a folder
    that doesn't exist yet (nothing downloaded) is left alone."""
    if not (config.WRITE_M3U or config.WRITE_PLAYLIST_COVER):
        return
    try:
        _write(conn, playlist_id)
    except Exception:
        logging.exception(f"[playlist] Could not write playlist files for {playlist_id}")


def _write(conn: sqlite3.Connection, playlist_id: str) -> None:
    playlist = next((p for p in list_playlists(conn) if p["playlist_id"] == playlist_id), None)
    if playlist is None:
        return
    folder = os.path.join(os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists"), playlist["table_name"])
    if not os.path.isdir(folder):
        return
    if config.WRITE_M3U:
        name = m3u8_name(playlist["name"])
        text = build_m3u8(playlist, list_tracks(conn, playlist["table_name"]), folder)
        if _write_if_changed(os.path.join(folder, name), text.encode("utf-8")):
            logging.info(f"[playlist] Wrote {os.path.join(folder, name)}")
        _remove_stale_m3u8(folder, name, playlist_id)
    if config.WRITE_PLAYLIST_COVER:
        image = fetch_image(playlist.get("cover_url"), f"playlist_{playlist_id}")
        if image and _write_if_changed(os.path.join(folder, "cover.jpg"), image):
            logging.info(f"[playlist] Wrote {os.path.join(folder, 'cover.jpg')}")
