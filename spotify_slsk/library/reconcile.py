"""Startup reconciliation: match files already on disk to DB rows.

Non-destructive by design: it only ever *records* a path for a row; it
never moves or deletes a file. See matching.tracks._looks_like_match.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3

from mutagen import File as MutagenFile

from .. import config
from ..db import update_download_status
from ..matching.tracks import (
    _find_best_local_match,
    _looks_like_match,
    _split_artists,
    _tokenize,
)


def startup_check(conn: sqlite3.Connection, table_name: str) -> None:
    """
    Smart, scoped startup reconciliation for ONE playlist table (table_name).
    """

    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    playlist_dir = os.path.join(playlists_root, table_name)

    # Use preferred formats from ENV (e.g., "flac,mp3")
    PREFERRED_FORMATS = os.getenv("SLSKD_PREFERRED_FORMATS", "mp3,flac,aiff,wav,m4a,ogg")
    AUDIO_EXTS = tuple(config.normalize_ext_list(PREFERRED_FORMATS))

    if not os.path.isdir(playlist_dir):
        logging.info(f"[startup] Skipping {table_name}: no folder at {playlist_dir}")
        # Extra hint if path missing
        try:
            parent = os.path.dirname(playlist_dir)
            if os.path.isdir(parent):
                logging.info(f"[startup] Parent exists, contents of {parent}: {os.listdir(parent)}")
            else:
                logging.info(f"[startup] Parent does not exist either: {parent}")
        except Exception as e:
            logging.debug(f"[startup] Could not list parent: {e}")
        return

    logging.info(f"[startup] Building local index for {table_name} in {playlist_dir} (exts={AUDIO_EXTS})")
    file_index = _index_playlist_files(playlist_dir, AUDIO_EXTS)

    # NEW: visibility into what we actually found
    try:
        indexed_count = len(file_index)
        logging.info(f"[startup] Indexed {indexed_count} audio file(s) under {playlist_dir}")

        if indexed_count == 0:
            # Dump raw directory listing once (non-recursive) to catch mount or filter issues
            try:
                top_level = os.listdir(playlist_dir)
                logging.info(f"[startup] Directory is accessible but no matching files were indexed. "
                             f"Top-level entries in {playlist_dir}: {top_level}")
                # Show a few recursive entries as a hint
                sample = []
                for r, _, files in os.walk(playlist_dir):
                    for f in files:
                        sample.append(os.path.join(r, f))
                        if len(sample) >= 10:
                            break
                    if len(sample) >= 10:
                        break
                logging.info(f"[startup] Sample of discovered files (unfiltered): {sample}")
                logging.info(f"[startup] If you see your .mp3 files above but index is 0, check SLSKD_PREFERRED_FORMATS={PREFERRED_FORMATS}")
            except Exception as e:
                logging.info(f"[startup] Could not list contents of {playlist_dir}: {e}")
    except Exception as e:
        logging.debug(f"[startup] Could not compute index diagnostics: {e}")

    # Read DB rows: expect tuples -> (id, name, artists, album, path, downloaded)
    cursor = conn.cursor()
    try:
        cursor.execute(f'SELECT id, name, artists, album, path, downloaded FROM "{table_name}"')
        rows = cursor.fetchall()
    except Exception as e:
        logging.exception(f"[startup] Failed to fetch rows for {table_name}: {e}")
        return

    verified = 0
    updated = 0
    missing = 0

    for row in rows:
        # tuple indices aligned with the SELECT above
        track_id = row[0]
        track_name = row[1] or ""
        track_artist = row[2] or ""
        # album = row[3]  # not needed for matching here
        file_path = (row[4] or "").strip() if row[4] else ""
        # downloaded = row[5]  # informational

        # 1) If we have a stored path, verify it and do nothing if correct
        if file_path:
            if os.path.isfile(file_path) and _file_matches_track(file_path, track_name, track_artist):
                verified += 1
                continue  # NO DB write when already correct
            # else: either file missing or mismatch -> try to re-locate below

        logging.debug(f"[startup] Trying to reconcile DB row: id={track_id} name='{track_name}' artist='{track_artist}'")
        # 2) Try to find a local match for this track
        match_path = _find_best_local_match(file_index, track_name, track_artist)
        if match_path:
            logging.info(f"[startup] Reconciled locally: [{track_artist}] {track_name} -> {match_path}")
            try:
                # Your signature: update_download_status(conn, track_id, table_name, success=False, file_path=None)
                update_download_status(conn, track_id, table_name, success=True, file_path=match_path)
                updated += 1
                logging.info(f"[startup] Reconciled locally: [{track_artist}] {track_name} -> {match_path}")
            except Exception as e:
                logging.exception(f"[startup] Failed to update DB for track {track_id} in {table_name}: {e}")
        else:
            logging.debug(f"[startup] No local match found for: [{track_artist}] {track_name}")
            missing += 1  # leave for normal download flow later

    logging.info(f"[startup] {table_name}: verified(no-op)={verified}, updated(from local)={updated}, still-missing={missing}")


def _read_audio_tags_safe(path: str):
    """Return (title, artist) using mutagen; fall back to filename for title."""
    title = ""
    artist = ""
    try:
        mf = MutagenFile(path, easy=True)
        if mf is not None:
            t = mf.get("title", [])
            a = mf.get("artist", [])
            title = t[0] if t else ""
            artist = a[0] if a else ""
    except Exception as e:
        logging.debug(f"Could not read tags from {path}: {e}")
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]
    return title, artist

def _derive_artist_title_from_stem(stem: str):
    """
    Parse from filename pattern '<artist> - <title>'.
    Returns (artist_guess, title_guess). If pattern not present, title_guess=stem.
    """
    parts = re.split(r"\s+-\s+", stem, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", stem.strip()

def _index_playlist_files(playlist_dir: str, audio_exts: tuple):
    """Index local audio files with robust metadata and precomputed tokens."""
    index = []
    for root, _, files in os.walk(playlist_dir):
        for f in files:
            if not f.lower().endswith(audio_exts):
                continue
            path = os.path.join(root, f)
            stem = os.path.splitext(f)[0]
            title_tag, artist_tag = _read_audio_tags_safe(path)
            artist_guess, title_guess = _derive_artist_title_from_stem(stem)
            artist = artist_tag or artist_guess
            title = title_tag or title_guess or stem
            index.append({
                "path": path,
                "title": title,
                "artist": artist,
                "stem": stem,
                "title_tokens": _tokenize(title),
                "stem_tokens": _tokenize(stem),
                "artist_set": _split_artists(artist),
            })
    return index


def _file_matches_track(file_path: str, track_name: str, track_artist: str) -> bool:
    """Validate that an existing DB path still corresponds to the intended track."""
    title, artist = _read_audio_tags_safe(file_path)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    return _looks_like_match(track_name, track_artist, title, artist, stem)
