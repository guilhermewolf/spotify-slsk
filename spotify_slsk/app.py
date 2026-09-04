import spotipy
import shutil
import os
import logging
import re
import difflib
import requests
import signal
import threading
import time
from .db import (
    create_connection,
    create_table,
    insert_track,
    fetch_all_tracks,
    update_download_status,
    clear_tried_entries,
    add_tried_file,
    get_pending_tracks,
    get_playlist_meta,
    upsert_playlist_meta,
    list_playlists,
    get_setting,
    get_upgrade_candidates,
    mark_upgrade_checked,
    record_cycle,
)
from .log_config import setup_logging
from . import runtime_state
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.flac import FLAC
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from .utils import sanitize_table_name, get_playlist_id
from spotipy.oauth2 import SpotifyClientCredentials
from .soulseek_api import (
    search_and_download,
    get_client as get_slskd_client,
    set_shutdown_event as _slsk_set_shutdown_event,
    refresh_from_db as _slsk_refresh_from_db,
    _version_tier,
)
from .models import Track


MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.62"))
CYCLE_INTERVAL_SECONDS = int(os.getenv("CYCLE_INTERVAL_SECONDS", "300"))
CYCLE_ERROR_BACKOFF_SECONDS = int(os.getenv("CYCLE_ERROR_BACKOFF_SECONDS", "60"))
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")


def _reload_settings(conn):
    """Pull live tunables from the DB so UI changes take effect next cycle."""
    global MIN_MATCH_SCORE
    try:
        MIN_MATCH_SCORE = float(get_setting(conn, "MIN_MATCH_SCORE", "0.62"))
    except (TypeError, ValueError) as e:
        logging.warning(f"Bad MIN_MATCH_SCORE in DB; keeping previous: {e}")
    _slsk_refresh_from_db(conn)

_shutdown = threading.Event()
# Set by wake_now() to break the inter-cycle sleep early. Cleared at the
# top of each cycle loop iteration after the wait returns.
_wake_event = threading.Event()


def _touch_heartbeat():
    """Update the heartbeat file so Docker HEALTHCHECK can detect a wedged loop."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
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
    enable/add) so the user doesn't wait up to CYCLE_INTERVAL_SECONDS for
    the change to be observable. Idempotent — repeated calls within the
    same sleep window collapse to one wake-up.
    """
    _wake_event.set()


PREFERRED_FORMATS = os.getenv("SLSKD_PREFERRED_FORMATS", "mp3,flac,aiff,wav,m4a,ogg")
AUDIO_EXTS = tuple(f".{ext.strip().lower()}" for ext in PREFERRED_FORMATS.split(","))
_STOP_PHRASES = [
    "original mix", "extended mix", "radio edit", "remastered", "remaster",
    "edit", "dub", "club mix", "mix", "version", "vip", "instrumental",
    "clean", "explicit"
]
# Splitters for artists like "Disclosure, AlunaGeorge", "Artist A & B", "feat.", "ft."
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&| and | feat\.? | ft\.? | featuring )\s*", re.IGNORECASE)

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

    stored_snapshot, _, _, _ = get_playlist_meta(conn, playlist_id)
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

    for item in items:
        track = item.get("track")
        if not track or not track.get("id"):
            continue
        artists_str = extract_artists_string(track)

        if track["id"] not in db_tracks:
            track_data = (
                track["id"],
                track["name"],
                artists_str,
                track["album"]["name"],
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
                )
            )

    # Only stamp the new snapshot after we've successfully inserted the diff,
    # so a crash mid-sync retries next cycle.
    upsert_playlist_meta(conn, playlist_id, table_name, playlist_title, snapshot_id)

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks, table_name

def find_closest_match(conn, table_name, title, artist):
    """
    Compatibility wrapper that delegates to the robust scorer.
    Returns (best_row, score) where best_row is (id, name, artists, album).
    """
    track_id, db_title, db_artist, db_album, score, reason = find_closest_db_match(
        conn, table_name, file_title=title, file_artist=artist
    )

    if track_id:
        logging.info(f"Best match: {db_title} by {db_artist} (score={score:.2f}, reason={reason})")
        return (track_id, db_title, db_artist, db_album), score

    logging.warning(f"No suitable match for: '{title}' by '{artist}'")
    return None, 0.0

def score_track_match(file_title: str, file_artist: str, db_title: str, db_artist: str) -> tuple[float, str]:
    """
    Returns (score, reason). Score in [0..1]. Reason is a short string for debugging.
    """
    if _titles_token_equivalent(file_title, db_title):
        if _artists_overlap(file_artist, db_artist):
            return 0.97, "title_tokens+artist_overlap"
        return 0.90, "title_tokens_only"

    if _remix_equivalent(file_title, db_title):
        if _artists_overlap(file_artist, db_artist):
            return 0.95, "remix_equivalent+artist_overlap"
        return 0.88, "remix_equivalent_title_only"

    title_sim = _similar(file_title, db_title)
    artist_sim = _similar(file_artist, db_artist) if (file_artist and db_artist) else 0.0

    score = max(
        title_sim,
        0.75 * title_sim + 0.25 * artist_sim,
    )

    if _artists_overlap(file_artist, db_artist):
        score = max(score, min(1.0, title_sim * 0.85 + 0.15))

    reason = f"fuzzy(title={title_sim:.2f}, artist={artist_sim:.2f})"
    return score, reason

def find_closest_db_match(conn, table_name: str, file_title: str, file_artist: str):
    """
    Scan the table and return (track_id, db_title, db_artist, db_album, score, reason).
    """
    cur = conn.cursor()
    cur.execute(f'SELECT id, name, artists, album FROM "{table_name}"')
    best_score = -1.0
    best_reason = ""
    best_row = (None, "", "", "")

    for track_id, db_title, db_artist, db_album in cur.fetchall():
        score, reason = score_track_match(file_title, file_artist, db_title, db_artist)
        logging.debug(f"[match] candidate: file='{file_title}'/{file_artist} vs db='{db_title}'/{db_artist} -> {score:.2f} ({reason})")
        if score > best_score:
            best_score = score
            best_reason = reason
            best_row = (track_id, db_title, db_artist, db_album)

    return (*best_row, best_score, best_reason)

def process_downloaded_file(file_path, playlist_name, conn, reconcile: bool = False):
    """
    Process a file that is either freshly downloaded or already on disk.

    When reconcile=True:
      - Never delete files on mismatch.
      - If the file already lives in /playlists/<playlist_name>, don't move it again.
    """
    title, artist, album = extract_metadata_from_file(file_path)

    if not title or not artist:
        logging.warning(f"❌ Missing metadata for {file_path}. Skipping tagging and DB update.")
        _reject_and_log(file_path, playlist_name, conn, reason="invalid metadata", destructive=not reconcile)
        return False, None

    # Route through the robust scorer (via wrapper)
    match, score = find_closest_match(conn, playlist_name, title, artist)
    if not match:
        _reject_and_log(file_path, playlist_name, conn, reason="no match", destructive=not reconcile)
        return False, None

    track_id, db_title, db_artist, db_album = match
    if score < MIN_MATCH_SCORE:
        logging.warning(f"⚠️ Low match score ({score:.2f}) for {title} by {artist}. Skipping update.")
        _reject_and_log(file_path, playlist_name, conn, track_id=track_id, reason="low score", destructive=not reconcile)
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
        return True, final_path

    logging.error(f"❌ Failed to move {file_path} to playlist folder.")
    return False, None


def _reject_and_log(file_path, playlist_name, conn, track_id=None, reason="unknown", destructive: bool = True):
    """
    On normal downloads we delete bad files; on reconcile we never delete.
    """
    filename = os.path.basename(file_path)

    if destructive:
        try:
            os.remove(file_path)
            logging.info(f"🧹 Deleted file due to {reason}: {file_path}")
        except Exception as e:
            logging.error(f"❌ Failed to delete file {file_path}: {e}")
    else:
        logging.info(f"ℹ️ (reconcile) Keeping unmatched file due to {reason}: {file_path}")

    if track_id:
        add_tried_file(conn, playlist_name, track_id, filename)
    else:
        logging.debug(f"Skipping add_tried_file() because track ID is unknown for: {filename}")

def _normalize_ext_list_env(var_name: str, default_csv: str) -> tuple:
    """
    Normalize env formats into a tuple of extensions like ('.flac', '.mp3', ...),
    accepting values with or without leading dots and stripping quotes & spaces.
    """
    raw = os.getenv(var_name, default_csv)
    items = []
    seen = set()
    for token in raw.split(","):
        fmt = token.strip().strip('"').strip("'").lower()
        if not fmt:
            continue
        if not fmt.startswith("."):
            fmt = "." + fmt
        if fmt not in seen:
            seen.add(fmt)
            items.append(fmt)
    return tuple(items)

def startup_check(conn, table_name: str):
    """
    Smart, scoped startup reconciliation for ONE playlist table (table_name).
    """

    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    playlist_dir = os.path.join(playlists_root, table_name)

    # Use preferred formats from ENV (e.g., "flac,mp3")
    PREFERRED_FORMATS = os.getenv("SLSKD_PREFERRED_FORMATS", "mp3,flac,aiff,wav,m4a,ogg")
    AUDIO_EXTS = _normalize_ext_list_env("SLSKD_PREFERRED_FORMATS", "mp3,flac,aiff,wav,m4a,ogg")

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

def _strip_brackets(s: str) -> str:
    # remove [stuff], (stuff), {stuff}
    return re.sub(r"[\[\(\{].*?[\]\)\}]", " ", s)

def _clean_title(s: str) -> str:
    s = (s or "")
    s = _strip_brackets(s).lower()
    s = re.sub(r"\b\d{3,4}\s?k?bps\b", " ", s)  # 320 kbps, etc.
    s = re.sub(r"[-_\.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for phrase in _STOP_PHRASES:
        s = re.sub(rf"\b{re.escape(phrase)}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[-_\.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _tokenize(s: str) -> set:
    s = _clean_title(s)
    tokens = re.split(r"\W+", s)
    return {t for t in tokens if t}

def _split_artists(artist_str: str) -> set:
    if not artist_str:
        return set()
    parts = _ARTIST_SPLIT_RE.split(artist_str)
    return {p.strip().lower() for p in parts if p.strip()}

def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=_norm(a), b=_norm(b)).ratio()

def _artists_overlap(a: str, b: str) -> bool:
    A = _split_artists(a)
    B = _split_artists(b)
    if not A or not B:
        return False
    return bool(A.intersection(B))

def _titles_token_equivalent(file_title: str, db_title: str) -> bool:
    ft = _tokenize(file_title)
    dt = _tokenize(db_title)
    if not ft or not dt:
        return False
    shorter, longer = (ft, dt) if len(ft) <= len(dt) else (dt, ft)
    return shorter.issubset(longer) or len(shorter.intersection(longer)) >= max(1, len(shorter) - 1)

def _remix_equivalent(file_title: str, db_title: str) -> bool:
    # Normalize "(Walker & Royce Remix)" vs "- Walker & Royce Remix"
    f = re.sub(r"[()\[\]{}\-–—]", " ", file_title or "", flags=re.IGNORECASE)
    d = re.sub(r"[()\[\]{}\-–—]", " ", db_title or "", flags=re.IGNORECASE)
    f = re.sub(r"\s+remix\b", " remix", f, flags=re.IGNORECASE)
    d = re.sub(r"\s+remix\b", " remix", d, flags=re.IGNORECASE)
    return _titles_token_equivalent(f, d)

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

def _looks_like_match(track_name: str, track_artist: str, file_title: str, file_artist: str, file_stem: str) -> bool:
    """
    Pragmatic matcher:
    - If (almost) all title tokens are present in file title OR filename -> accept.
    - If artist info is available, prefer intersection but don't require it when title is strong.
    - Tolerant to 'ft./feat./extended mix/[320 kbps]' noise.
    """
    tn_tokens = _tokenize(track_name)
    ta_set = _split_artists(track_artist)

    title_tokens = _tokenize(file_title)
    stem_tokens  = _tokenize(file_stem)
    fa_set       = _split_artists(file_artist)

    if not tn_tokens:
        return False

    # --- Strong title containment rules ---
    # require all tokens for short titles (<=2 tokens), allow one miss for longer titles
    needed_title = len(tn_tokens) if len(tn_tokens) <= 2 else len(tn_tokens) - 1

    title_hit = len(tn_tokens.intersection(title_tokens)) >= needed_title
    stem_hit  = len(tn_tokens.intersection(stem_tokens))  >= needed_title

    if title_hit or stem_hit:
        # If we know artists on either side, prefer seeing at least one overlap.
        # BUT: if title is a single clear word (len>=6) OR multi-word, accept even without artist.
        if ta_set and fa_set:
            if ta_set.intersection(fa_set) or any(a in " ".join(stem_tokens) for a in ta_set):
                return True
            # Accept strong title match even without artist overlap when the title is
            # multi-word or a long single word — prevents over-strict failures.
            if len(tn_tokens) >= 2 or (
                len(tn_tokens) == 1 and len(next(iter(tn_tokens))) >= 6
            ):
                return True
        else:
            # Missing artist info on one/both sides → accept strong title match
            return True

    # --- Fuzzy backup ---
    name_title = _similar(track_name, file_title)
    name_stem  = _similar(track_name, file_stem)
    artist_sim = _similar(track_artist, file_artist) if (track_artist and file_artist) else 0.0

    if name_title >= 0.78 and artist_sim >= 0.50:
        return True
    if name_stem  >= 0.83 and artist_sim >= 0.50:
        return True

    # Title-only last resort when artist data absent
    if (not track_artist or not file_artist) and max(name_title, name_stem) >= 0.90:
        return True

    return False

def _find_best_local_match(file_index, track_name: str, track_artist: str):
    """
    Return best candidate path or None based on combined evidence.
    Adds DEBUG logs for each candidate score for transparency.
    """
    best = None
    best_score = 0.0
    _norm(track_name)
    ta_set = _split_artists(track_artist)

    for item in file_index:
        # compute plausibility
        plausible = _looks_like_match(track_name, track_artist, item["title"], item["artist"], item["stem"])

        # score components
        title_score = _similar(track_name, item["title"])
        stem_score  = _similar(track_name, item["stem"])
        artist_hit  = 1.0 if (ta_set and (ta_set.intersection(item["artist_set"]) or
                                          any(a in " ".join(item["stem_tokens"]) for a in ta_set))) else 0.0

        score = max(title_score * 0.65 + artist_hit * 0.35,
                    stem_score  * 0.65 + artist_hit * 0.35,
                    max(title_score, stem_score))

        logging.debug(
            f"[startup] candidate score for '{track_name}' / '{track_artist}': "
            f"path='{item['path']}', title='{item['title']}', artist='{item['artist']}', "
            f"title_score={title_score:.2f}, stem_score={stem_score:.2f}, artist_hit={artist_hit:.0f}, "
            f"plausible={plausible}"
        )

        if plausible and score > best_score:
            best_score = score
            best = item["path"]

    # Lowered threshold to accept good real-world matches once plausible
    return best if (best and best_score >= 0.68) else None

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
        auth_manager = SpotifyClientCredentials()
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
    client = get_slskd_client()

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
    )

    if file_path:
        verified, _final_path = process_downloaded_file(file_path, playlist_name, conn)
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
    current_tier = _version_tier(current_basename)
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
    )
    if not new_file:
        logging.info(f"[upgrade] No better-tier candidate for '{track.name}'")
        return False

    verified, final_path = process_downloaded_file(new_file, playlist_name, conn)
    if not verified or not final_path:
        return False

    # Delete the old file iff it differs from the new one. Same-basename is
    # possible when the upgrade winner happens to share a filename — in that
    # case shutil.move already overwrote it.
    try:
        if (
            current_path
            and os.path.isfile(current_path)
            and os.path.abspath(current_path) != os.path.abspath(final_path)
        ):
            os.remove(current_path)
            logging.info(f"[upgrade] Replaced {current_path} -> {final_path}")
        else:
            logging.info(f"[upgrade] Wrote upgraded file: {final_path}")
    except OSError as e:
        logging.warning(f"[upgrade] Could not delete old file {current_path}: {e}")

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
    # Persist the Spotipy auth token across restarts on the data volume, and
    # silence the "Couldn't write token to cache at: .cache" warning that
    # spams otherwise (WORKDIR isn't writable by the non-root container user).
    os.environ.setdefault("SPOTIPY_CACHE_PATH", "/app/data/.spotipy-cache")

    setup_logging()
    _install_signal_handlers()
    _slsk_set_shutdown_event(_shutdown)
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
        _reload_settings(conn)

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
            _reload_settings(conn)
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
                for pl in playlists:
                    if _shutdown.is_set():
                        break
                    counters = process_playlist(
                        sp, conn, pl["playlist_id"], ntfy_url, ntfy_topic
                    )
                    if counters:
                        for k in cycle_totals:
                            cycle_totals[k] += counters.get(k, 0)
            except Exception:
                logging.exception("Cycle failed; backing off and retrying")
                if _shutdown.wait(CYCLE_ERROR_BACKOFF_SECONDS):
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
                detail=f"Sleeping up to {CYCLE_INTERVAL_SECONDS}s until next cycle",
            )
            if _wake_event.wait(CYCLE_INTERVAL_SECONDS):
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
