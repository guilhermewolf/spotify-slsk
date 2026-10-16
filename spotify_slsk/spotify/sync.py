"""Spotify -> DB: fetch a playlist, diff it against its table, insert new rows."""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

import spotipy

from ..db import (
    backfill_durations,
    create_table,
    fetch_all_tracks,
    get_playlist_meta,
    insert_track,
    list_playlists,
    rename_playlist_meta,
    upsert_playlist_meta,
)
from ..models import Track
from ..utils import get_playlist_id, sanitize_table_name


def fetch_all_playlist_tracks(sp: spotipy.Spotify, playlist_id: str) -> list[dict[str, Any]]:
    tracks = []
    offset = 0
    limit = 100

    while True:
        results = sp.playlist_tracks(playlist_id, offset=offset, limit=limit)
        items = results.get('items', [])
        if not items:
            break
        tracks.extend(items)
        offset += len(items)

    return tracks

def fetch_and_compare_tracks(
    conn: sqlite3.Connection, playlist_id: str, sp: spotipy.Spotify
) -> tuple[list[Track], str]:
    # Pull the playlist root (cheap one-page call). snapshot_id lets us skip
    # the expensive paginated track fetch when nothing has changed.
    playlist_info = sp.playlist(playlist_id, fields="name,snapshot_id")
    playlist_title = playlist_info["name"]
    snapshot_id = playlist_info.get("snapshot_id")
    table_name = sanitize_table_name(playlist_title)

    create_table(conn, table_name)

    stored_snapshot, stored_table, stored_name, _ = get_playlist_meta(conn, playlist_id)
    # A playlist added while Spotify was unreachable is catalogued under a
    # placeholder (its id) and table. Point the row at the real table now,
    # not only when the snapshot is stamped at the end: rows are inserted
    # into `table_name` below, and until then the dashboard was reading the
    # empty placeholder table ("0 / 0 downloaded" mid-sync).
    if stored_table is not None and (stored_table, stored_name) != (table_name, playlist_title):
        rename_playlist_meta(conn, playlist_id, table_name, playlist_title)
    if stored_snapshot and snapshot_id and stored_snapshot == snapshot_id:
        logging.info(
            f"Playlist {playlist_title} unchanged (snapshot {snapshot_id[:8]}); "
            f"skipping Spotify track fetch"
        )
        return [], table_name

    logging.info(
        f"Fetching tracks for playlist ID: {playlist_id} into table: {table_name}"
    )
    items = fetch_all_playlist_tracks(sp, playlist_id)
    logging.info(f"Fetched {len(items)} tracks from Spotify for playlist {table_name}")

    db_tracks = {track[0]: track for track in fetch_all_tracks(conn, table_name)}
    new_tracks = []
    known_durations = []

    for item in items:
        track = item.get("track")
        if not track or not track.get("id"):
            continue
        artists_str = extract_artists_string(track)
        duration_ms = track.get("duration_ms")
        isrc = (track.get("external_ids") or {}).get("isrc")

        if track["id"] in db_tracks:
            known_durations.append((track["id"], duration_ms, isrc))
        else:
            track_data = (
                track["id"],
                track["name"],
                artists_str,
                track["album"]["name"],
                duration_ms,
                isrc,
            )
            insert_track(conn, table_name, track_data)
            logging.info(
                f"New Song found in {table_name}: {track['name']} by {artists_str} "
                f"from album {track['album']['name']}"
            )
            new_tracks.append(
                Track(
                    track["id"],
                    track["name"],
                    artists_str,
                    track["album"]["name"],
                    playlist_id,
                    duration_ms=duration_ms,
                    isrc=isrc,
                )
            )

    # Rows inserted before schema v5/v6 have no duration/ISRC; fill them in.
    if known_durations:
        backfill_durations(conn, table_name, known_durations)

    # Only stamp the new snapshot after we've successfully inserted the diff,
    # so a crash mid-sync retries next cycle.
    upsert_playlist_meta(conn, playlist_id, table_name, playlist_title, snapshot_id)

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks, table_name


def extract_artists_string(track: dict[str, Any]) -> str:
    return ', '.join(artist['name'] for artist in track['artists'])


def migrate_env_playlists(conn: sqlite3.Connection, sp: spotipy.Spotify) -> None:
    """First-boot import: seed the empty catalogue from SPOTIFY_PLAYLIST_URLS env.

    Runs only when `playlists_meta` has no rows AND the env var is set.
    After this, the DB is the source of truth; the UI manages add/remove.
    """
    if list_playlists(conn):
        return
    env_urls = os.getenv("SPOTIFY_PLAYLIST_URLS", "")
    urls = [u.strip() for u in env_urls.split(",") if u.strip()]
    if not urls:
        return
    logging.info(
        f"First-boot: importing {len(urls)} playlist(s) from SPOTIFY_PLAYLIST_URLS"
    )
    for url in urls:
        pid = get_playlist_id(url)
        if not pid:
            continue
        try:
            info = sp.playlist(pid, fields="name")
            table_name = sanitize_table_name(info["name"])
            # Intentionally leave snapshot_id NULL — the first cycle's
            # fetch_and_compare_tracks will populate tracks and stamp
            # the snapshot itself.
            upsert_playlist_meta(conn, pid, table_name, info["name"], None)
            logging.info(f"Imported: {info['name']} ({pid})")
        except Exception as e:
            logging.error(f"Could not import env playlist {url}: {e}")
