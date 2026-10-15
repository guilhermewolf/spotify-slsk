"""Getting a track: search slskd, grab, import - for a pending track, or to
replace an existing file with a better version (the upgrade pass).
"""
import logging
import os

from ..db import clear_tried_entries, get_track, record_history, update_download_status
from ..importer.process import process_downloaded_file
from ..matching.normalize import version_tier
from ..slskd import transfers
from ..slskd.search import search_and_download

def handle_track_download(track, playlist_name, conn):
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


def try_upgrade_track(track, playlist_name, conn, current_path):
    """If slskd has a *strictly better* version_tier, swap the file in place.

    Failure semantics differ from a fresh download: we never call
    update_download_status(success=False) here because the track is already
    downloaded — incrementing attempts would eventually suspend a row that
    has nothing wrong with it.
    """
    current_basename = os.path.basename(current_path) if current_path else ""
    current_tier = version_tier(current_basename)
    if current_tier == 0:
        logging.info(
            f"[upgrade] '{track.name}' already at top tier (Extended Mix); skipping"
        )
        return False

    logging.info(
        f"[upgrade] Checking '{track.name}' by '{track.artist}' "
        f"(current tier={current_tier}, file={current_basename or '∅'})"
    )

    new_file = search_and_download(
        artist=track.artist,
        title=track.name,
        conn=conn,
        playlist_name=playlist_name,
        track_id=track.id,
        album=track.album,
        max_version_tier=current_tier,
        duration_ms=track.duration_ms,
    )
    if not new_file:
        logging.info(f"[upgrade] No better-tier candidate for '{track.name}'")
        return False

    verified, final_path = process_downloaded_file(
        new_file, playlist_name, conn, expected_track=track,
        source=transfers.last_download,
    )
    if not verified or not final_path:
        return False

    # Delete the old file only if the import landed on *this* track (re-read
    # the row, like _process_manual_grabs) and it differs from the new one.
    # Same-basename is possible when the upgrade winner happens to share a
    # filename — in that case shutil.move already overwrote it.
    imported = get_track(conn, playlist_name, track.id) or {}
    try:
        if (
            imported.get("path") == final_path
            and current_path
            and os.path.isfile(current_path)
            and os.path.abspath(current_path) != os.path.abspath(final_path)
        ):
            os.remove(current_path)
            logging.info(f"[upgrade] Replaced {current_path} -> {final_path}")
            record_history(
                conn, "deleted_file", table_name=playlist_name, track_id=track.id,
                path=current_path, reason="replaced by upgrade",
            )
        else:
            logging.info(f"[upgrade] Wrote upgraded file: {final_path}")
    except OSError as e:
        logging.warning(f"[upgrade] Could not delete old file {current_path}: {e}")

    record_history(
        conn, "upgraded", table_name=playlist_name, track_id=track.id,
        old_path=current_path, path=final_path,
    )
    clear_tried_entries(conn, playlist_name, track.id)
    return True
