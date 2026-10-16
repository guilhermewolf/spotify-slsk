"""Getting a track: search slskd, grab, import - for a pending track, or to
replace an existing file with a better version (the upgrade pass).
"""
from __future__ import annotations

import logging
import os
import sqlite3

from .. import config
from ..db import (
    add_tried_file,
    clear_tried_entries,
    get_file_check,
    get_track,
    record_history,
    set_track_quality,
    update_download_status,
)
from ..importer.audio_check import SUSPECT
from ..importer.process import process_downloaded_file
from ..matching.normalize import version_tier
from ..matching.quality import describe, effective_quality, quality_of_file
from ..models import Track
from ..slskd import transfers
from ..slskd.search import search_and_download
from .health import retire_replaced_file


def handle_track_download(track: Track, playlist_name: str, conn: sqlite3.Connection) -> bool:
    """Search slskd (with query fall-through) and download the best match."""
    file_path = search_and_download(
        artist=track.artist,
        title=track.name,
        conn=conn,
        playlist_name=playlist_name,
        track_id=track.id,
        album=track.album,
        duration_ms=track.duration_ms,
    )

    if file_path:
        verified, _final_path = process_downloaded_file(
            file_path, playlist_name, conn, expected_track=track,
            source=transfers.last_download,
        )
        if verified:
            clear_tried_entries(conn, playlist_name, track.id)
            return True

    update_download_status(conn, track.id, playlist_name, success=False)
    return False


def _current_quality(
    conn: sqlite3.Connection, playlist_name: str, track_id: str, current_path: str | None
) -> str | None:
    """The row's recorded quality, read from the file (and stored) when the
    row predates schema v7 or was reconciled."""
    row = get_track(conn, playlist_name, track_id) or {}
    if row.get("quality"):
        return row["quality"]
    if not current_path or not os.path.isfile(current_path):
        return None
    quality = quality_of_file(current_path)
    set_track_quality(conn, playlist_name, track_id, quality)
    return quality


def try_upgrade_track(
    track: Track, playlist_name: str, conn: sqlite3.Connection, current_path: str | None
) -> bool:
    """Replace the file when slskd has something better:

    * a *strictly better* version tier (Extended Mix > Original Mix > other)
      at the same or a better profile quality - a version upgrade never
      lowers quality (unknown quality of a lossless file: lossless only);
    * with quality upgrades on, a file below the profile cutoff may also be
      replaced by the same version tier in a *strictly better* quality
      (the lossless verdict counts: a suspect FLAC ranks as MP3 320);
    * a file the lossless check called "suspect" (cliff in the MP3-320 band)
      may be replaced by any lossless copy of the same or a better version,
      whatever the profile's upgrade switch says (Tier 3 behaviour); its
      name goes to `_tried` so it is not grabbed again.

    The replaced file is moved to .quarantine/<table>/replaced/, never
    deleted, and only when the import landed on *this* track.

    Failure semantics differ from a fresh download: we never call
    update_download_status(success=False) here because the track is already
    downloaded — incrementing attempts would eventually suspend a row that
    has nothing wrong with it.
    """
    current_basename = os.path.basename(current_path) if current_path else ""
    current_tier = version_tier(current_basename)
    check = get_file_check(conn, playlist_name, track.id)
    verdict = check["audio_check"] if check and check["path"] == current_path else None
    suspect = verdict == SUSPECT
    quality = effective_quality(
        _current_quality(conn, playlist_name, track.id, current_path), verdict
    )
    profile = config.QUALITY_PROFILE
    below_cutoff = (
        not suspect and profile.upgrades_allowed
        and quality is not None and profile.below_cutoff(quality)
    )
    max_tier = current_tier
    if suspect:
        max_tier = current_tier + 1  # same version is fine, if truly lossless
        add_tried_file(conn, playlist_name, track.id, current_basename)
    elif current_tier == 0 and not below_cutoff:
        logging.info(
            f"[upgrade] '{track.name}' already at top tier (Extended Mix) and "
            f"meets the quality cutoff; skipping"
        )
        return False

    logging.info(
        f"[upgrade] Checking '{track.name}' by '{track.artist}' "
        f"(current tier={current_tier}, quality={describe(quality)}, "
        f"file={current_basename or '∅'}"
        f"{', suspect lossless' if suspect else ''}"
        f"{', below cutoff ' + describe(profile.cutoff) if below_cutoff else ''})"
    )

    new_file = search_and_download(
        artist=track.artist,
        title=track.name,
        conn=conn,
        playlist_name=playlist_name,
        track_id=track.id,
        album=track.album,
        max_version_tier=max_tier,
        duration_ms=track.duration_ms,
        # Never trade quality for a version: a better tier must be at the
        # same or a better profile quality. Unknown quality of a lossless
        # file -> lossless candidates only. A suspect file is compared by
        # lossless_only alone (it counts as MP3 320).
        lossless_only=suspect or (
            quality is None
            and os.path.splitext(current_basename)[1].lower() in config.LOSSLESS_EXTS
        ),
        better_quality_than=quality if below_cutoff else None,
        current_quality=None if suspect else quality,
    )
    if not new_file:
        logging.info(f"[upgrade] No better candidate for '{track.name}'")
        return False

    verified, final_path = process_downloaded_file(
        new_file, playlist_name, conn, expected_track=track,
        source=transfers.last_download,
    )
    if not verified or not final_path:
        return False

    # Retire the old file only if the import landed on *this* track (re-read
    # the row, like _process_manual_grabs) and it differs from the new one.
    # Same-basename is possible when the upgrade winner happens to share a
    # filename — in that case shutil.move already overwrote it.
    imported = get_track(conn, playlist_name, track.id) or {}
    if (
        imported.get("path") == final_path
        and current_path
        and os.path.isfile(current_path)
        and os.path.abspath(current_path) != os.path.abspath(final_path)
    ):
        retire_replaced_file(
            conn, playlist_name, track.id, current_path, "replaced by upgrade"
        )
    else:
        logging.info(f"[upgrade] Wrote upgraded file: {final_path}")

    record_history(
        conn, "upgraded", table_name=playlist_name, track_id=track.id,
        old_path=current_path, path=final_path,
        old_quality=describe(quality) if quality else None,
        quality=describe(imported.get("quality")) if imported.get("quality") else None,
    )
    clear_tried_entries(conn, playlist_name, track.id)
    return True
