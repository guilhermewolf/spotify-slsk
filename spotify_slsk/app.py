import spotipy
import shutil
import os
import logging
import re
import requests
import signal
import threading
import time
from .db import (
    create_connection,
    create_table,
    insert_track,
    fetch_all_tracks,
    backfill_durations,
    update_download_status,
    clear_tried_entries,
    add_tried_file,
    add_blocklist,
    record_history,
    prune_history,
    get_pending_tracks,
    get_track,
    get_playlist_meta,
    upsert_playlist_meta,
    rename_playlist_meta,
    list_playlists,
    get_setting,
    get_upgrade_candidates,
    mark_upgrade_checked,
    record_cycle,
)
from .log_config import setup_logging
from . import config, runtime_state
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.flac import FLAC
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from .utils import fmt_duration, get_playlist_id, sanitize_table_name
from spotipy.oauth2 import SpotifyClientCredentials
from spotipy.cache_handler import CacheFileHandler
from .slskd import client as slskd_client
from .slskd import transfers
from .slskd.search import search_and_download
from .matching.candidates import filename_match
from .matching.normalize import version_tier
from .matching.tracks import (
    _find_best_local_match,
    _looks_like_match,
    _split_artists,
    _tokenize,
    find_closest_match,
    score_track_match,
)
from .models import Track


_shutdown = threading.Event()
# Set by wake_now() to break the inter-cycle sleep early. Cleared at the
# top of each cycle loop iteration after the wait returns.
_wake_event = threading.Event()


def _touch_heartbeat():
    """Update the heartbeat file so Docker HEALTHCHECK can detect a wedged loop."""
    try:
        with open(config.HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        logging.debug("Could not write heartbeat file", exc_info=True)


def _install_signal_handlers():
    def _handle(signum, _frame):
        logging.info(f"Received signal {signum}, initiating graceful shutdown")
        _shutdown.set()
        # Also break the cycle waiter so shutdown is prompt.
        _wake_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle)


def wake_now() -> None:
    """Break the cycle sleep early so the next cycle starts immediately.

    Called by the webui after state-changing actions (retry, refresh,
    enable/add) so the user doesn't wait up to config.CYCLE_INTERVAL_SECONDS for
    the change to be observable. Idempotent — repeated calls within the
    same sleep window collapse to one wake-up.
    """
    _wake_event.set()


def sanitize_input(text):
    return re.sub(r'[^A-Za-z0-9 ]+', '', text)

def fetch_all_playlist_tracks(sp, playlist_id):
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

def fetch_and_compare_tracks(conn, playlist_id, sp):
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

        if track["id"] in db_tracks:
            known_durations.append((track["id"], duration_ms))
        else:
            track_data = (
                track["id"],
                track["name"],
                artists_str,
                track["album"]["name"],
                duration_ms,
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
                )
            )

    # Rows inserted before schema v5 have no duration; fill them in.
    if known_durations:
        backfill_durations(conn, table_name, known_durations)

    # Only stamp the new snapshot after we've successfully inserted the diff,
    # so a crash mid-sync retries next cycle.
    upsert_playlist_meta(conn, playlist_id, table_name, playlist_title, snapshot_id)

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks, table_name


def _audio_length_seconds(file_path):
    """Actual audio duration via mutagen, or None if unreadable."""
    try:
        mf = MutagenFile(file_path)
        if mf is not None and mf.info is not None and mf.info.length:
            return float(mf.info.length)
    except Exception as e:
        logging.debug(f"Could not read duration of {file_path}: {e}")
    return None


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
    length = _audio_length_seconds(file_path)
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
    file_path,
    playlist_name,
    conn,
    reconcile: bool = False,
    track_id=None,
    expected_track=None,
    source=None,
):
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
    track it is for: no usable tags, unreadable audio, or a real length
    far from what the peer advertised (truncated/corrupt). A mismatch
    against the *requested* track ("no match", "low score", a filename or
    Spotify-duration miss) is recorded in the track's _tried list and
    history only — the same file can be exactly right for another track,
    and a global ban would hide it there.
    """
    if expected_track is not None and track_id is None:
        track_id = expected_track.id

    advertised = (source or {}).get("length")
    if advertised and not reconcile and config.MAX_DURATION_DIFF_SECONDS > 0:
        actual = _audio_length_seconds(file_path)
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

    if not title or not artist:
        ok, reason = (
            _fallback_match_untagged(file_path, expected_track, source)
            if expected_track is not None and not reconcile
            else (False, "invalid metadata")
        )
        if not ok:
            logging.warning(f"❌ Rejecting {file_path}: {reason}")
            _reject_and_log(
                file_path, playlist_name, conn, track_id=track_id,
                reason=reason, destructive=not reconcile, source=source,
                peer_fault=reason in _PEER_FAULT_REASONS,
            )
            return False, None
        logging.info(
            f"Untagged file accepted by filename + duration: {file_path}"
        )
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
        if score < config.MIN_MATCH_SCORE:
            logging.warning(f"⚠️ Low match score ({score:.2f}) for {title} by {artist}. Skipping update.")
            _reject_and_log(file_path, playlist_name, conn, track_id=track_id, reason="low score", destructive=not reconcile, source=source)
            return False, None

    # Fall back to the Spotify-known album when the downloaded file has no
    # album tag (observed during Phase B testing: downloaded MP3 had no TALB).
    effective_album = album or db_album
    tag_audio_file(file_path, title, artist, effective_album)

    # If the file is already inside the playlists dir, don't move it.
    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    try:
        already_in_library = os.path.commonpath(
            [os.path.abspath(file_path), os.path.abspath(playlists_root)]
        ) == os.path.abspath(playlists_root)
    except Exception:
        already_in_library = False

    if already_in_library:
        final_path = file_path
    else:
        final_path = move_track_to_playlist_folder(file_path, playlist_name)

    if final_path:
        update_download_status(conn, track_id, playlist_name, success=True, file_path=final_path)
        if not reconcile:
            record_history(
                conn, "imported", table_name=playlist_name, track_id=track_id,
                path=final_path,
                match=match_method,
                username=(source or {}).get("user"),
                filename=(source or {}).get("filename"),
            )
        return True, final_path

    logging.error(f"❌ Failed to move {file_path} to playlist folder.")
    return False, None


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

def startup_check(conn, table_name: str):
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

def extract_artists_string(track):
    return ', '.join(artist['name'] for artist in track['artists'])

def extract_metadata_from_file(file_path):
    try:
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".mp3":
            audio = MP3(file_path, ID3=ID3)
            title = audio.tags.get("TIT2")
            artist = audio.tags.get("TPE1")
            album = audio.tags.get("TALB")

            title = title.text[0] if title else None
            artist = artist.text[0] if artist else None
            album = album.text[0] if album else None

        elif ext == ".flac":
            audio = FLAC(file_path)
            title = audio.get("title", [None])[0]
            artist = audio.get("artist", [None])[0]
            album = audio.get("album", [None])[0]

        elif ext == ".aiff":
            audio = AIFF(file_path)
            title = audio.get("TIT2", [None])[0]
            artist = audio.get("TPE1", [None])[0]
            album = audio.get("TALB", [None])[0]

        elif ext == ".wav":
            logging.warning("WAV format may not have embedded metadata.")
            return None, None, None

        else:
            logging.warning(f"Unsupported audio format for metadata: {ext}")
            return None, None, None

        logging.info(f"Extracted metadata from file: {file_path} - Title: {title}, Artist: {artist}, Album: {album}")
        return title, artist, album

    except Exception as e:
        logging.error(f"Error reading metadata from {file_path}: {e}")
        return None, None, None

def setup_spotify_client():
    """Build the Spotify client, or return None if credentials are missing.

    Returning None rather than raising keeps the dashboard reachable so the
    operator can see *why* nothing is syncing. Every caller already tolerates
    a missing client: the webui falls back to a placeholder playlist name,
    and the cycle loop skips syncing (see main()).
    """
    logging.info("Setting up Spotify client")
    try:
        # SpotifyClientCredentials ignores SPOTIPY_CACHE_PATH and defaults to
        # ./.cache, which the non-root container user can't write: spotipy
        # warned "Couldn't write token to cache" on every call. Point the
        # cache at the data dir (the tests point it into their sandbox).
        auth_manager = SpotifyClientCredentials(
            cache_handler=CacheFileHandler(
                cache_path=os.getenv("SPOTIPY_CACHE_PATH", "./data/.spotipy-cache")
            )
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)
    except Exception as e:
        logging.error(
            f"Spotify client unavailable — check SPOTIPY_CLIENT_ID / "
            f"SPOTIPY_CLIENT_SECRET: {e}"
        )
        return None
    logging.info("Spotify client setup complete")
    return sp

def send_ntfy_notification(url, topic, message):
    # ntfy is optional. Without this guard an unconfigured deployment POSTs to
    # the literal URL "None/None" on every playlist of every cycle, which
    # raises, gets swallowed, and logs an ERROR — forever.
    if not url or not topic:
        logging.debug(f"ntfy not configured; skipping notification: {message}")
        return

    try:
        response = requests.post(f"{url}/{topic}", data=message)
        if response.status_code == 200:
            logging.info(f"Notification sent successfully: {message}")
        else:
            logging.error(f"Failed to send notification: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Error while sending notification: {e}")

def move_track_to_playlist_folder(track_path: str, playlist_name: str) -> str:
    try:
        # Use the correct base path
        base_dir = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
        dest_dir = os.path.join(base_dir, playlist_name)
        os.makedirs(dest_dir, exist_ok=True)

        # Final destination path
        filename = os.path.basename(track_path)
        destination = os.path.join(dest_dir, filename)

        if not os.path.exists(track_path):
            logging.error(f"Source file does not exist: {track_path}")
            return None

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

def tag_audio_file(file_path, title, artist, album):
    ext = os.path.splitext(file_path)[1].lower()

    # Ensure all metadata fields are non-None strings
    title = title or ""
    artist = artist or ""
    album = album or ""

    try:
        if ext == '.mp3':
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
            audio.save()

        elif ext == '.flac':
            audio = FLAC(file_path)
            audio['title'] = title
            audio['artist'] = artist
            audio['album'] = album
            audio.save()

        elif ext == '.aiff':
            audio = AIFF(file_path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(TIT2(encoding=3, text=title))
            audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=album))
            audio.save()

        elif ext == '.wav':
            logging.warning("WAV tagging is not fully supported; skipping tags.")

        else:
            logging.warning(f"Unsupported format for tagging: {ext}")
            return False

        logging.info(f"Tagged {file_path} successfully.")
        return True

    except Exception as e:
        logging.error(f"Failed to tag {file_path}: {e}")
        return False


def wait_for_slskd_healthy(host, api_key, timeout=90, check_interval=1):
    """Poll slskd until it reports connected+logged-in. Returns True on success.

    Returns False on timeout rather than raising: an unreachable slskd is an
    expected transient condition (compose start order, a restart upstream),
    not a reason to kill a daemon whose dashboard the operator needs most
    during exactly that outage. `block_until_slskd_healthy` owns the retrying.
    """
    logging.info(f"Waiting for slskd at {host} (timeout: {timeout}s)...")
    client = slskd_client.get_client()

    start = time.time()
    last_err = None
    while time.time() - start < timeout:
        if _shutdown.is_set():
            return False
        try:
            state = client.application.state()
            if state['server'].get('isConnected') and state['server'].get('isLoggedIn'):
                logging.info("slskd is healthy and connected.")
                return True
        except Exception as e:
            last_err = e

        if int(time.time() - start) % 5 == 0:
            logging.debug(f"Still waiting for slskd... (last error: {last_err})")

        if _shutdown.wait(check_interval):
            return False

    logging.warning(f"slskd did not become healthy within {timeout}s: {last_err}")
    return False


def block_until_slskd_healthy(host, api_key, retry_seconds=60):
    """Keep waiting for slskd, staying alive and heartbeating while we do.

    Cycles must not start before slskd is reachable: every download would
    fail, and two failures suspend a track for two days (see
    db.MAX_ATTEMPTS_BEFORE_SUSPEND). So a dependency outage would otherwise
    convert into a self-inflicted two-day backlog.

    Returns True when healthy, False if shutdown was requested first.
    """
    while not _shutdown.is_set():
        # Keep the heartbeat fresh: the process is alive and doing its job,
        # it is a dependency that is down. Letting the file go stale would
        # have Docker restart us repeatedly for someone else's outage.
        _touch_heartbeat()
        if wait_for_slskd_healthy(host, api_key):
            return True
        logging.warning(
            f"slskd still unreachable; retrying in {retry_seconds}s. "
            f"The dashboard stays available at /healthz."
        )
        if _shutdown.wait(retry_seconds):
            break
    return False


def process_playlist(sp, conn, playlist_id, ntfy_url, ntfy_topic):
    """Sync + download for one playlist. Returns per-playlist counters
    (tracks_added/downloaded/failed) so the caller can aggregate them
    into a cycle_history row.
    """
    logging.info(f"Processing playlist ID: {playlist_id}")
    _touch_heartbeat()

    # Best-effort name lookup so the activity badge has something readable
    # before the Spotify fetch returns.
    _, _stored_table, stored_name, _ = get_playlist_meta(conn, playlist_id)
    runtime_state.set_activity(
        "syncing",
        detail="Fetching playlist from Spotify",
        playlist_id=playlist_id,
        playlist_name=stored_name,
        table_name=_stored_table,
    )

    new_tracks, playlist_name = fetch_and_compare_tracks(conn, playlist_id, sp)
    counters = {
        "tracks_added": len(new_tracks),
        "tracks_downloaded": 0,
        "tracks_failed": 0,
    }

    if new_tracks:
        msg = f"Playlist updated: {len(new_tracks)} new track(s) added to {playlist_name}"
        send_ntfy_notification(ntfy_url, ntfy_topic, msg)

    tracks = get_pending_tracks(conn, playlist_name)
    if tracks:
        for track in tracks:
            if _shutdown.is_set():
                logging.info("Shutdown requested, stopping track processing")
                return counters
            # One track can occupy several minutes (60s search + up to 300s
            # transfer + 60s post-processing, per candidate). Touching the
            # heartbeat per track is what lets the Docker HEALTHCHECK tell a
            # slow-but-working daemon from a wedged one.
            _touch_heartbeat()
            runtime_state.set_activity(
                "downloading",
                detail=f"{track.name} — {track.artist}",
                playlist_id=playlist_id,
                playlist_name=playlist_name,
                table_name=playlist_name,
                track_id=track.id,
                track_name=track.name,
                track_artist=track.artist,
            )
            logging.info(f"Downloading: {track.name} by {track.artist}")
            success = handle_track_download(track, playlist_name, conn)
            if success:
                counters["tracks_downloaded"] += 1
                logging.info(f"Downloaded: {track.name} by {track.artist}")
            else:
                counters["tracks_failed"] += 1
                logging.warning(f"Failed: {track.name} by {track.artist}")
    else:
        logging.info(f"No pending tracks for {playlist_name}")

    runtime_state.set_activity(
        "upgrading",
        detail="Checking for higher-tier versions",
        playlist_id=playlist_id,
        playlist_name=playlist_name,
        table_name=playlist_name,
    )
    _run_upgrade_pass(conn, playlist_name)

    send_ntfy_notification(ntfy_url, ntfy_topic, f"Finished processing playlist: {playlist_name}")
    return counters


def safe_get(tag):
    if isinstance(tag, list):
        return tag[0]
    return tag

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


def _run_upgrade_pass(conn, playlist_name):
    """Per-playlist upgrade pass. Throttled by UPGRADE_CHECK_INTERVAL_HOURS."""
    try:
        interval = int(get_setting(conn, "UPGRADE_CHECK_INTERVAL_HOURS", "168"))
    except (TypeError, ValueError):
        interval = 168
    if interval <= 0:
        return

    candidates = get_upgrade_candidates(conn, playlist_name, interval)
    if not candidates:
        return

    logging.info(
        f"[upgrade] {playlist_name}: {len(candidates)} track(s) eligible for upgrade check "
        f"(interval={interval}h)"
    )
    for track, current_path in candidates:
        if _shutdown.is_set():
            logging.info("[upgrade] Shutdown requested, stopping upgrade pass")
            return
        _touch_heartbeat()
        try:
            try_upgrade_track(track, playlist_name, conn, current_path)
        except Exception:
            logging.exception(
                f"[upgrade] Failed to check track {track.id} in {playlist_name}"
            )
        finally:
            mark_upgrade_checked(conn, playlist_name, track.id)


def _process_manual_grabs(conn):
    """Download grabs queued from the track page's interactive search.

    Runs on the daemon thread so only it writes files and the DB. Reuses
    the automatic path: grab_candidate (tried/blocklist/history on
    failure) then process_downloaded_file (tag check, move, import). A
    manual failure does not count toward the track's attempts.
    """
    for req in runtime_state.pop_manual_grabs():
        if _shutdown.is_set():
            return
        table_name, candidate = req["table_name"], req["candidate"]
        row = get_track(conn, table_name, req["track_id"])
        if not row:
            continue
        track = Track(
            row["id"], row["name"], row["artists"], row["album"], table_name,
            duration_ms=row["duration_ms"],
        )
        _touch_heartbeat()
        runtime_state.set_activity(
            "downloading",
            detail=f"Manual grab: {track.name} — {track.artist}",
            playlist_id=req["playlist_id"],
            table_name=table_name,
            track_id=track.id,
            track_name=track.name,
            track_artist=track.artist,
        )
        try:
            file_path = transfers.grab_candidate(
                candidate, conn, table_name, track.id, source="manual"
            )
            if not file_path:
                continue
            verified, final_path = process_downloaded_file(
                file_path, table_name, conn, expected_track=track, source=candidate
            )
            if not verified:
                continue
            clear_tried_entries(conn, table_name, track.id)
            # The import is verified against this track only, but re-read the
            # row anyway: replace the old file only if the import landed here.
            imported = get_track(conn, table_name, track.id) or {}
            old = row["path"]
            if (
                imported.get("path") == final_path
                and old
                and os.path.isfile(old)
                and os.path.abspath(old) != os.path.abspath(final_path)
            ):
                os.remove(old)
                record_history(
                    conn, "deleted_file", table_name=table_name, track_id=track.id,
                    path=old, reason="replaced by manual grab",
                )
        except Exception:
            logging.exception(f"Manual grab failed for {track.id} in {table_name}")


def _run_startup_reconciliation(sp, conn):
    """Run the one-shot on-disk reconciliation once per daemon start."""
    for pl in list_playlists(conn, only_enabled=True):
        if _shutdown.is_set():
            return
        try:
            _, playlist_name = fetch_and_compare_tracks(conn, pl["playlist_id"], sp)
            startup_check(conn, playlist_name)
        except Exception:
            logging.exception(
                f"Startup reconciliation failed for {pl['playlist_id']} ({pl['name']})"
            )


def _migrate_env_playlists(conn, sp):
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


def main():
    # Persist the Spotipy auth token across restarts on the data volume
    # (read by setup_spotify_client's cache handler).
    os.environ.setdefault("SPOTIPY_CACHE_PATH", "/app/data/.spotipy-cache")

    setup_logging()
    _install_signal_handlers()
    slskd_client.set_shutdown_event(_shutdown)
    logging.info("Starting main process")
    runtime_state.set_activity("starting", detail="Daemon booting")

    slskd_api_key = os.getenv("SLSKD_API_KEY")
    slskd_host_url = os.getenv("SLSKD_HOST_URL", "http://slskd:5030")
    ntfy_url = os.getenv("NTFY_URL")
    ntfy_topic = os.getenv("NTFY_TOPIC")


    conn = create_connection("./data/playlist_tracks.db")
    if not conn:
        logging.error("Failed to connect to the SQLite database.")
        return

    try:
        sp = setup_spotify_client()
        if sp is not None:
            _migrate_env_playlists(conn, sp)
        config.reload(conn)

        # Bring the dashboard up before waiting on slskd. Previously the
        # slskd wait ran first and raised on timeout, so an slskd outage took
        # the whole process down — and with it the UI and /healthz the
        # operator needed to diagnose the outage — leaving Docker to
        # crash-loop the container every ~90s.
        if os.getenv("UI_ENABLED", "1") == "1":
            from .webui import run_in_thread as _run_webui
            _run_webui(
                db_path="./data/playlist_tracks.db",
                spotify_client=sp,
                wake_callback=wake_now,
                host=os.getenv("UI_BIND_ADDR", "0.0.0.0"),
                port=int(os.getenv("UI_PORT", "8000")),
            )

        runtime_state.set_activity(
            "waiting_slskd", detail=f"Waiting for slskd at {slskd_host_url}"
        )
        if not block_until_slskd_healthy(slskd_host_url, slskd_api_key):
            return  # shutdown requested while waiting

        send_ntfy_notification(
            ntfy_url, ntfy_topic, "Spotify Playlist Downloader starting"
        )
        # Both of these need Spotify. Their own try/except would swallow the
        # resulting AttributeError, but the operator would then get a
        # 'NoneType' has no attribute 'playlist' traceback per playlist per
        # boot instead of being told the actual problem.
        if sp is not None:
            runtime_state.set_activity(
                "reconciling", detail="Matching local files to DB"
            )
            _run_startup_reconciliation(sp, conn)
        else:
            logging.error(
                "Skipping startup reconciliation: no Spotify client. Set "
                "SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET and restart."
            )

        while not _shutdown.is_set():
            _touch_heartbeat()
            config.reload(conn)
            prune_history(conn, config.HISTORY_RETENTION_DAYS)
            playlists = list_playlists(conn, only_enabled=True)
            if sp is None:
                # No Spotify credentials. Idle rather than spin: syncing is
                # impossible and attempting downloads would burn attempts and
                # suspend tracks for two days over a config error.
                logging.error(
                    "Spotify client unavailable; skipping cycle. Set "
                    "SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET and restart."
                )
                playlists = []
            elif not playlists:
                logging.info(
                    "No enabled playlists; waiting for the UI to add some"
                )
            else:
                logging.info(
                    f"Starting new cycle ({len(playlists)} enabled playlist(s))"
                )
            cycle_started_at = time.time()
            cycle_totals = {
                "tracks_added": 0,
                "tracks_downloaded": 0,
                "tracks_failed": 0,
            }
            try:
                _process_manual_grabs(conn)
                for pl in playlists:
                    if _shutdown.is_set():
                        break
                    counters = process_playlist(
                        sp, conn, pl["playlist_id"], ntfy_url, ntfy_topic
                    )
                    # Between playlists too: a grab shouldn't wait a full cycle.
                    _process_manual_grabs(conn)
                    if counters:
                        for k in cycle_totals:
                            cycle_totals[k] += counters.get(k, 0)
            except Exception:
                logging.exception("Cycle failed; backing off and retrying")
                if _shutdown.wait(config.CYCLE_ERROR_BACKOFF_SECONDS):
                    break
                continue
            # Only record cycles that did real work — empty-playlist ticks
            # would dilute the dashboard view of recent activity.
            if playlists:
                record_cycle(
                    conn,
                    started_at=cycle_started_at,
                    duration_seconds=time.time() - cycle_started_at,
                    playlists_synced=len(playlists),
                    tracks_added=cycle_totals["tracks_added"],
                    tracks_downloaded=cycle_totals["tracks_downloaded"],
                    tracks_failed=cycle_totals["tracks_failed"],
                )
            # Sleep until the next cycle, but allow the webui (or a signal
            # handler) to interrupt us via _wake_event.set(). Clearing
            # *after* the wait means a wake_now() that arrives during the
            # cycle is preserved and shortcuts the next sleep.
            runtime_state.set_activity(
                "idle",
                detail=f"Sleeping up to {config.CYCLE_INTERVAL_SECONDS}s until next cycle",
            )
            if _wake_event.wait(config.CYCLE_INTERVAL_SECONDS):
                _wake_event.clear()
                if _shutdown.is_set():
                    break
                logging.info("Cycle interrupted by wake request; starting next cycle")
    finally:
        runtime_state.set_activity("shutting_down", detail="Closing connections")
        logging.info("Shutting down, closing database connection")
        try:
            conn.close()
        except Exception:
            logging.debug("Error closing DB connection", exc_info=True)


if __name__ == "__main__":
    main()
