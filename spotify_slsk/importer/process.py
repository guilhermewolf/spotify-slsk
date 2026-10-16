"""Import one downloaded file: verify it is the requested track, tag it,
move it into the library, and record the outcome.

This is the only place that deletes a downloaded file (_reject_and_log
with destructive=True); the startup reconcile passes reconcile=True
so it never does.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3

from .. import config, runtime_state
from ..db import (
    add_blocklist,
    add_tried_file,
    get_track,
    mark_upgrade_due,
    record_history,
    save_file_check,
    set_track_quality,
    update_download_status,
)
from ..library.naming import file_name
from ..matching.candidates import _duration_off, filename_match
from ..matching.normalize import version_mismatch, version_tier
from ..matching.quality import quality_of_file
from ..matching.tracks import find_closest_match, score_track_match
from ..models import Candidate, Track
from ..utils import fmt_duration, free_path
from . import artwork
from .audio_check import (
    BLOCKLIST_VERDICTS,
    CHECKABLE_EXTS,
    SUSPECT,
    AudioCheck,
    check_file,
    rules_key,
)
from .tags import (
    audio_length_seconds,
    extract_metadata_from_file,
    normalize_isrc,
    read_isrc,
    spotify_tags,
    tag_audio_file,
)


def _fallback_match_untagged(file_path, expected_track, source=None):
    """Decide whether an untagged download is the track we asked slskd for.

    Used only when the file has no title/artist tags, and only against the
    requested track. Both must hold: the file's name passes the same strict
    rules as a search candidate (filename_match: every title
    word in the basename + coverage, whole-name artist in the name or
    folder, same version), AND the file's real duration is within
    SLSKD_MAX_DURATION_DIFF_SECONDS of Spotify's. The peer path from
    `source` is preferred (it carries the share's folders); otherwise the
    local basename plus its parent folder. Unknown durations, or a disabled
    duration check (0), mean no. Returns (ok, reason).
    """
    tolerance = config.MAX_DURATION_DIFF_SECONDS
    if tolerance <= 0:
        return False, "missing tags (duration check disabled)"
    if not expected_track.duration_ms:
        return False, "missing tags (no Spotify duration)"
    name = (source or {}).get("filename") or os.path.join(
        os.path.basename(os.path.dirname(file_path)), os.path.basename(file_path)
    )
    _, why = filename_match(name, expected_track.name, expected_track.artist)
    if why:
        return False, f"missing tags; filename: {why}"
    length = audio_length_seconds(file_path)
    if length is None:
        return False, "missing tags; unreadable duration"
    expected = expected_track.duration_ms / 1000.0
    if abs(length - expected) > tolerance:
        return False, (
            f"missing tags; duration {fmt_duration(length)} "
            f"vs {fmt_duration(expected)}"
        )
    return True, None


def _match_requested_track(conn, table_name, track_id, title, artist):
    """Score the file's tags against the one track that was requested.

    Returns ((id, name, artists, album), score) or (None, 0.0) if the row is
    gone. Never falls back to another row: picking the best row of the whole
    playlist assigned wrong files to *other* tracks while the requested one
    stayed pending and was downloaded again.
    """
    row = get_track(conn, table_name, track_id)
    if not row:
        logging.warning(f"Requested track {track_id} not found in {table_name}")
        return None, 0.0
    score, reason = score_track_match(title, artist, row["name"], row["artists"])
    logging.info(
        f"Verify '{title}' by '{artist}' against requested '{row['name']}' by "
        f"'{row['artists']}': score={score:.2f} ({reason})"
    )
    return (row["id"], row["name"], row["artists"], row["album"]), score


def process_downloaded_file(
    file_path: str,
    playlist_name: str,
    conn: sqlite3.Connection,
    reconcile: bool = False,
    track_id: str | None = None,
    expected_track: Track | None = None,
    source: Candidate | None = None,
) -> tuple[bool, str | None]:
    """
    Process a file that is either freshly downloaded or already on disk.

    track_id: the track this file was downloaded for. When given, the file is
      verified against that track only. Without it (reconciling an unknown
      file) the best-scoring row of the playlist is used.

    When reconcile=True:
      - Never delete files on mismatch.
      - If the file already lives in /playlists/<playlist_name>, don't move it again.

    `expected_track` is the Track the download was for (implies its
    track_id). When given and the file has no title/artist tags, the
    filename + real duration are checked against it (see
    _fallback_match_untagged) instead of rejecting outright.

    `source` is the slskd candidate the file came from. A rejection only
    blocklists that peer file when the fault is the file's own, whatever
    track it is for: no usable tags, unreadable audio, a real length
    far from what the peer advertised (truncated/corrupt), or a 24-bit file
    that is padded 16-bit audio. A fake/upsampled verdict (see
    importer.audio_check) rejects for this track only; "suspect" and
    "unknown" are accepted. A mismatch
    against the *requested* track ("no match", "low score", a filename or
    Spotify-duration miss) is recorded in the track's _tried list and
    history only — the same file can be exactly right for another track,
    and a global ban would hide it there.
    """
    if expected_track is not None and track_id is None:
        track_id = expected_track.id

    advertised = (source or {}).get("length")
    if advertised and not reconcile and config.MAX_DURATION_DIFF_SECONDS > 0:
        actual = audio_length_seconds(file_path)
        if actual is not None and abs(actual - advertised) > config.MAX_DURATION_DIFF_SECONDS:
            reason = (
                f"duration {fmt_duration(actual)} vs advertised "
                f"{fmt_duration(advertised)} (truncated or corrupt)"
            )
            logging.warning(f"❌ Rejecting {file_path}: {reason}")
            _reject_and_log(
                file_path, playlist_name, conn, track_id=track_id, reason=reason,
                source=source, peer_fault=True,
            )
            return False, None

    title, artist, album = extract_metadata_from_file(file_path)

    # ISRC. Equal -> strong accept evidence: it may override a low
    # title/artist score below, never a version mismatch. Different -> NOT a
    # rejection on its own: singles, albums, remasters and compilations carry
    # different ISRCs for the same song. It is logged and recorded, and only
    # rejects together with another failure (low score, version mismatch, or
    # a real duration off from Spotify's).
    expected_isrc = file_isrc = isrc_conflict = None
    expected_ms = None
    expected_name = ""
    if not reconcile and track_id is not None:
        row = get_track(conn, playlist_name, track_id) or {}
        expected_isrc, expected_ms = normalize_isrc(row.get("isrc")), row.get("duration_ms")
        expected_name = row.get("name") or ""
        file_isrc = read_isrc(file_path)
        if expected_isrc and file_isrc and expected_isrc != file_isrc:
            isrc_conflict = f"ISRC mismatch (file {file_isrc}, expected {expected_isrc})"
            logging.warning(f"{isrc_conflict} for {file_path}; checking other evidence")
    isrc_match = bool(expected_isrc and file_isrc == expected_isrc)

    def _with_isrc(reason: str) -> str:
        return f"{reason} + {isrc_conflict}" if isrc_conflict else reason

    if not title or not artist:
        ok, reason = (
            _fallback_match_untagged(file_path, expected_track, source)
            if expected_track is not None and not reconcile
            else (False, "invalid metadata")
        )
        if not ok:
            assert reason is not None
            logging.warning(f"❌ Rejecting {file_path}: {reason}")
            _reject_and_log(
                file_path, playlist_name, conn, track_id=track_id,
                reason=_with_isrc(reason), destructive=not reconcile, source=source,
                peer_fault=reason in _PEER_FAULT_REASONS,
            )
            return False, None
        logging.info(
            f"Untagged file accepted by filename + duration: {file_path}"
        )
        assert expected_track is not None  # ok is only True on the expected_track path
        title, artist, album = expected_track.name, expected_track.artist, None
        db_album = expected_track.album
        match_method = "filename+duration"
    else:
        match_method = "tags"
        if track_id is not None:
            match, score = _match_requested_track(conn, playlist_name, track_id, title, artist)
        else:
            match, score = find_closest_match(conn, playlist_name, title, artist)
        if not match:
            _reject_and_log(file_path, playlist_name, conn, reason="no match", destructive=not reconcile, source=source)
            return False, None

        track_id, db_title, db_artist, db_album = match
        if isrc_match and score < config.MIN_MATCH_SCORE and not version_mismatch(db_title, title, db_artist):
            logging.info(
                f"Low tag score ({score:.2f}) for '{title}' by '{artist}' overridden "
                f"by matching ISRC {file_isrc}"
            )
            match_method = "isrc"
        elif score < config.MIN_MATCH_SCORE:
            logging.warning(f"⚠️ Low match score ({score:.2f}) for {title} by {artist}. Skipping update.")
            _reject_and_log(file_path, playlist_name, conn, track_id=track_id, reason=_with_isrc("low score"), destructive=not reconcile, source=source)
            return False, None

    if isrc_conflict and expected_ms and config.MAX_DURATION_DIFF_SECONDS > 0:
        length = audio_length_seconds(file_path)
        longer_ok = version_tier(os.path.basename(file_path)) < version_tier(expected_name)
        if length is not None and _duration_off(length - expected_ms / 1000, longer_ok=longer_ok):
            reason = _with_isrc(
                f"duration {fmt_duration(length)} vs {fmt_duration(expected_ms / 1000)}"
            )
            logging.warning(f"❌ Rejecting {file_path}: {reason}")
            _reject_and_log(file_path, playlist_name, conn, track_id=track_id, reason=reason, source=source)
            return False, None

    audio = None if reconcile else _lossless_check(file_path)
    if audio is not None and audio.rejected:
        # Deleted and tried for this track. Only an exact verdict (padded_16)
        # blocklists the peer file globally: fake/upsampled rest on thin dB
        # margins, and a false positive must not ban a good file everywhere.
        reason = audio.describe()
        logging.warning(f"❌ Rejecting {file_path}: {reason}")
        _reject_and_log(
            file_path, playlist_name, conn, track_id=track_id, reason=reason,
            source=source, peer_fault=audio.verdict in BLOCKLIST_VERDICTS,
        )
        return False, None

    # Tag from Spotify (the verified row), not from the peer's tags. On an
    # ISRC conflict the file's own ISRC is kept ("" = leave it alone).
    assert track_id is not None  # set by the match above
    row = get_track(conn, playlist_name, track_id) or {
        "id": track_id, "name": title, "artists": artist, "album": album or db_album,
    }
    tag_audio_file(
        file_path,
        spotify_tags(row, isrc="" if isrc_conflict else (expected_isrc or file_isrc)),
        cover=artwork.album_cover(row) if config.EMBED_COVER_ART else None,
    )

    # If the file is already inside the playlists dir, don't move it.
    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    try:
        already_in_library = os.path.commonpath(
            [os.path.abspath(file_path), os.path.abspath(playlists_root)]
        ) == os.path.abspath(playlists_root)
    except Exception:
        already_in_library = False

    final_path: str | None
    quality = quality_of_file(file_path)
    if already_in_library:
        final_path = file_path
    else:
        # FILE_NAME_TEMPLATE applies to newly imported files only.
        name = file_name(config.FILE_NAME_TEMPLATE, row, os.path.basename(file_path), quality)
        final_path = move_track_to_playlist_folder(file_path, playlist_name, name)

    if final_path:
        update_download_status(conn, track_id, playlist_name, success=True, file_path=final_path)
        set_track_quality(conn, playlist_name, track_id, quality)
        if not reconcile:
            record_history(
                conn, "imported", table_name=playlist_name, track_id=track_id,
                path=final_path,
                match=match_method,
                isrc=file_isrc if isrc_match else None,
                isrc_mismatch=isrc_conflict,
                audio=audio.describe() if audio else None,
                username=(source or {}).get("user"),
                filename=(source or {}).get("filename"),
            )
            _record_import_check(conn, playlist_name, track_id, final_path, audio)
        return True, final_path

    logging.error(f"❌ Failed to move {file_path} to playlist folder.")
    return False, None


def _lossless_check(file_path: str) -> AudioCheck | None:
    """Spectrum-check a lossless download (None when not applicable).
    ~1-2 s on a low-power NAS; the heartbeat is touched around it."""
    if (
        not config.LOSSLESS_CHECK_ENABLED
        or os.path.splitext(file_path)[1].lower() not in CHECKABLE_EXTS
    ):
        return None
    runtime_state.touch_heartbeat()
    try:
        audio = check_file(file_path)
    finally:
        runtime_state.touch_heartbeat()
    logging.info(f"Lossless check {file_path}: {audio.describe()}")
    return audio


def _record_import_check(conn, table_name, track_id, path, audio):
    """Persist the import's verdict for the track page and the health scan.

    rules_version 0: the scan still re-verifies identity with its own rules,
    but reuses this audio verdict while the file is unchanged. A suspect
    file is made due for the upgrade pass, which then looks for a lossless
    replacement (library.acquire.try_upgrade_track).
    """
    size: int | None = None
    mtime: float | None = None
    try:
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        pass
    suspect = audio is not None and audio.verdict == SUSPECT
    save_file_check(
        conn, table_name, track_id, path=path, size=size, mtime=mtime,
        rules_version=0,
        audio_check=audio.verdict if audio else None,
        audio_cutoff_hz=audio.cutoff_hz if audio else None,
        audio_drop_db=audio.drop_db if audio else None,
        problems=["suspect_lossless"] if suspect else [],
        evidence={"lossless_rules": rules_key()},
    )
    if suspect:
        mark_upgrade_due(conn, table_name, track_id)


# Rejections that condemn the peer file for every track (see
# process_downloaded_file). Everything else is a per-track mismatch.
_PEER_FAULT_REASONS = ("invalid metadata", "missing tags; unreadable duration")


def _reject_and_log(file_path, playlist_name, conn, track_id=None, reason="unknown", destructive: bool = True, source=None, peer_fault: bool = False):
    """
    On normal downloads we delete bad files; on reconcile we never delete.
    The file always goes to the track's _tried list; a known slskd `source`
    is blocklisted globally only when `peer_fault` (the file itself is bad).
    """
    filename = os.path.basename(file_path)

    if destructive:
        record_history(
            conn, "import_rejected", table_name=playlist_name, track_id=track_id,
            reason=reason, path=file_path,
            username=(source or {}).get("user"),
            filename=(source or {}).get("filename"),
        )
        try:
            os.remove(file_path)
            logging.info(f"🧹 Deleted file due to {reason}: {file_path}")
            record_history(
                conn, "deleted_file", table_name=playlist_name, track_id=track_id,
                path=file_path, reason=reason,
            )
        except Exception as e:
            logging.error(f"❌ Failed to delete file {file_path}: {e}")
    else:
        logging.info(f"ℹ️ (reconcile) Keeping unmatched file due to {reason}: {file_path}")

    if track_id:
        add_tried_file(conn, playlist_name, track_id, filename)
    else:
        logging.debug(f"Skipping add_tried_file() because track ID is unknown for: {filename}")

    if destructive and source and peer_fault:
        add_blocklist(
            conn,
            source.get("user"),
            source.get("filename"),
            source.get("size"),
            f"rejected after download: {reason}",
            track_id,
        )


def move_track_to_playlist_folder(
    track_path: str, playlist_name: str, name: str | None = None
) -> str | None:
    """Move a download to <playlists_root>/<table>/<name> (default: its own
    basename). Never overwrites: a taken name gets " (2)", " (3)"..."""
    try:
        base_dir = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
        dest_dir = os.path.join(base_dir, playlist_name)
        os.makedirs(dest_dir, exist_ok=True)

        if not os.path.exists(track_path):
            logging.error(f"Source file does not exist: {track_path}")
            return None
        destination = free_path(dest_dir, name or os.path.basename(track_path))

        # Attempt to move, fallback to copy
        try:
            shutil.move(track_path, destination)
        except OSError as e:
            logging.error(f"Failed to move file across devices: {e}. Trying to copy instead.")
            shutil.copy2(track_path, destination)

        logging.info(f"Moved file to playlist folder: {destination}")
        return destination

    except Exception as e:
        logging.error(f"Failed to move file: {e}")
        return None
