import spotipy
import slskd_api
import shutil
import os
import logging
import re
import difflib
import random
import requests
import time
import sqlite3
from db import (
    create_connection,
    create_table,
    insert_track,
    fetch_all_tracks,
    update_download_status,
    clear_tried_entries,
    add_tried_file,
    get_pending_tracks,
)
from log_config import setup_logging
from utils import sleep_interval
from mutagen import File
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TALB
from mutagen.flac import FLAC
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from utils import sanitize_table_name
from spotipy.oauth2 import SpotifyClientCredentials
from soulseek_api import perform_search, download_and_verify
from models import Track


MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.62"))
PREFERRED_FORMATS = os.getenv("SLSKD_PREFERRED_FORMATS", "mp3,flac,aiff,wav,m4a,ogg")
AUDIO_EXTS = tuple(f".{ext.strip().lower()}" for ext in PREFERRED_FORMATS.split(","))
_STOP_PHRASES = [
    "original mix", "extended mix", "radio edit", "remastered", "remaster",
    "edit", "dub", "club mix", "mix", "version", "vip", "instrumental",
    "clean", "explicit"
]
# Splitters for artists like "Disclosure, AlunaGeorge", "Artist A & B", "feat.", "ft."
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&| and | feat\.? | ft\.? | featuring )\s*", re.IGNORECASE)

def get_playlist_id(playlist_url):
    try:
        if "playlist/" in playlist_url:
            return playlist_url.split("playlist/")[1].split("?")[0]
        else:
            raise ValueError(f"Invalid playlist URL: {playlist_url}")
    except IndexError:
        logging.error(f"Failed to extract playlist ID from URL: {playlist_url}")
        return None

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
    playlist_info = sp.playlist(playlist_id)
    playlist_title = playlist_info['name']
    table_name = f"{sanitize_table_name(playlist_title)}"

    create_table(conn, table_name)

    logging.info(f"Fetching tracks for playlist ID: {playlist_id} into table: {table_name}")
    items = fetch_all_playlist_tracks(sp, playlist_id)
    logging.info(f"Fetched {len(items)} tracks from Spotify for playlist {table_name}")

    db_tracks = {track[0]: track for track in fetch_all_tracks(conn, table_name)}
    new_tracks = []

    for item in items:
        track = item['track']
        artists_str = extract_artists_string(track)

        logging.debug(f"Fetched track: {track['name']} by {artists_str}")

        if track['id'] not in db_tracks:
            track_data = (
                track['id'],
                track['name'],
                artists_str,
                track['album']['name']
            )
            insert_track(conn, table_name, track_data)
            logging.info(
                f"New Song found in {table_name}: {track['name']} by {artists_str} from album {track['album']['name']}"
            )
            new_tracks.append(
                Track(track['id'], track['name'], artists_str, track['album']['name'], playlist_id)
            )

    logging.info(f"Found {len(new_tracks)} new tracks to download in playlist {table_name}")
    return new_tracks, table_name

def _mm_strip_brackets(s: str) -> str:
    return re.sub(r"[\[\(\{].*?[\]\)\}]", " ", s or "")

def _mm_clean_title(s: str) -> str:
    s = _mm_strip_brackets(s).lower()
    s = re.sub(r"\b\d{3,4}\s?k?bps\b", " ", s)  # 320 kbps, etc.
    s = re.sub(r"[-_\.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for phrase in _STOP_PHRASES:
        s = re.sub(rf"\b{re.escape(phrase)}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _mm_norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[-_\.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _mm_tokenize_title(s: str) -> set:
    return {t for t in re.split(r"\W+", _mm_clean_title(s)) if t}

def _mm_split_artists(s: str) -> set:
    if not s:
        return set()
    parts = _ARTIST_SPLIT_RE.split(s)
    return {p.strip().lower() for p in parts if p.strip()}

def _mm_similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=_mm_norm(a), b=_mm_norm(b)).ratio()

def _mm_artists_overlap(a: str, b: str) -> bool:
    A = _mm_split_artists(a)
    B = _mm_split_artists(b)
    if not A or not B:
        return False
    return bool(A.intersection(B))

def _mm_titles_token_equivalent(file_title: str, db_title: str) -> bool:
    ft = _mm_tokenize_title(file_title)
    dt = _mm_tokenize_title(db_title)
    if not ft or not dt:
        return False
    # Accept if all tokens from the shorter set are present in the longer one
    shorter, longer = (ft, dt) if len(ft) <= len(dt) else (dt, ft)
    return shorter.issubset(longer) or len(shorter.intersection(longer)) >= max(1, len(shorter) - 1)

def _mm_remix_equivalent(file_title: str, db_title: str) -> bool:
    # Normalize "(Walker & Royce Remix)" vs "- Walker & Royce Remix"
    f = re.sub(r"[()\[\]{}\-–—]", " ", file_title or "", flags=re.IGNORECASE)
    d = re.sub(r"[()\[\]{}\-–—]", " ", db_title or "", flags=re.IGNORECASE)
    f = re.sub(r"\s+remix\b", " remix", f, flags=re.IGNORECASE)
    d = re.sub(r"\s+remix\b", " remix", d, flags=re.IGNORECASE)
    return _mm_titles_token_equivalent(f, d)


def find_closest_match(conn, table_name, title, artist):
    """
    Compatibility wrapper that delegates to the robust scorer.
    Returns (best_row, score) where best_row is (id, name, artists).
    """
    track_id, db_title, db_artist, score, reason = find_closest_db_match(
        conn, table_name, file_title=title, file_artist=artist
    )

    if track_id:
        logging.info(f"Best match: {db_title} by {db_artist} (score={score:.2f}, reason={reason})")
        return (track_id, db_title, db_artist), score

    logging.warning(f"No suitable match for: '{title}' by '{artist}'")
    return None, 0.0

def score_track_match(file_title: str, file_artist: str, db_title: str, db_artist: str) -> tuple[float, str]:
    """
    Returns (score, reason). Score in [0..1]. Reason is a short string for debugging.
    """
    # Fast path: token equivalence (ignores mix labels/brackets)
    if _mm_titles_token_equivalent(file_title, db_title):
        if _mm_artists_overlap(file_artist, db_artist):
            return 0.97, "title_tokens+artist_overlap"
        # title tokens match but artist missing/mismatched — still very strong
        return 0.90, "title_tokens_only"

    # Remix-aware equivalence (hyphen vs parentheses)
    if _mm_remix_equivalent(file_title, db_title):
        if _mm_artists_overlap(file_artist, db_artist):
            return 0.95, "remix_equivalent+artist_overlap"
        return 0.88, "remix_equivalent_title_only"

    # Fuzzy fallback (weighted)
    title_sim = _mm_similar(file_title, db_title)      # handles punctuation differences
    artist_sim = _mm_similar(file_artist, db_artist) if (file_artist and db_artist) else 0.0

    # Blend title and artist; title is main signal
    score = max(
        title_sim,                                      # pure title similarity
        0.75 * title_sim + 0.25 * artist_sim            # weighted blend when artist present
    )

    # Boost a bit if any artist overlap exists
    if _mm_artists_overlap(file_artist, db_artist):
        score = max(score, min(1.0, title_sim * 0.85 + 0.15))  # light boost

    reason = f"fuzzy(title={title_sim:.2f}, artist={artist_sim:.2f})"
    return score, reason

def find_closest_db_match(conn, table_name: str, file_title: str, file_artist: str):
    """
    Scan the table and return (track_id, db_title, db_artist, score, reason).
    """
    cur = conn.cursor()
    cur.execute(f'SELECT id, name, artists FROM "{table_name}"')
    best = None
    best_score = -1.0
    best_reason = ""
    best_row = (None, "", "")

    for track_id, db_title, db_artist in cur.fetchall():
        score, reason = score_track_match(file_title, file_artist, db_title, db_artist)
        logging.debug(f"[match] candidate: file='{file_title}'/{file_artist} vs db='{db_title}'/{db_artist} -> {score:.2f} ({reason})")
        if score > best_score:
            best_score = score
            best_reason = reason
            best_row = (track_id, db_title, db_artist)

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

    track_id, db_title, db_artist = match
    if score < MIN_MATCH_SCORE:
        logging.warning(f"⚠️ Low match score ({score:.2f}) for {title} by {artist}. Skipping update.")
        _reject_and_log(file_path, playlist_name, conn, track_id=track_id, reason="low score", destructive=not reconcile)
        return False, None

    # Tag before placement
    tag_audio_file(file_path, title, artist, album)

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

def clean_up_untracked_files(conn, download_path, table_name, delete: bool = False):
    """
    Compare files on disk vs DB. By default, do NOT delete (safe).
    If delete=True, remove files not present in DB.
    """
    logging.info(f"Cleaning up untracked files in {download_path} (delete={delete})")
    cursor = conn.cursor()
    cursor.execute(f'SELECT path FROM "{table_name}" WHERE downloaded = 1')
    db_files = {row[0] for row in cursor.fetchall() if row[0]}

    for root, _, files in os.walk(download_path):
        for file in files:
            if file.lower().endswith(('.mp3', '.flac', '.aiff', '.wav', '.m4a', '.ogg')):
                file_path = os.path.join(root, file)
                if file_path not in db_files:
                    if delete:
                        try:
                            os.remove(file_path)
                            logging.info(f"Deleted untracked file: {file_path}")
                        except Exception as e:
                            logging.error(f"Failed to delete {file_path}: {e}")
                    else:
                        logging.info(f"(dry-run) Would delete untracked file: {file_path}")

def startup_check(conn, table_name: str):
    """
    Smart, scoped startup reconciliation for ONE playlist table (table_name).
    """

    playlists_root = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    playlist_dir = os.path.join(playlists_root, table_name)

    # Use preferred formats from ENV (e.g., "flac,mp3")
    PREFERRED_FORMATS = os.getenv("SLSKD_PREFERRED_FORMATS", "mp3,flac,aiff,wav,m4a,ogg")
    AUDIO_EXTS = tuple(f".{ext.strip().lower()}" for ext in PREFERRED_FORMATS.split(",") if ext.strip())

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
    except Exception:
        pass
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
            # Title is multi-word or a long single word? Accept to avoid over-strict failures.
            if len(tn_tokens) >= 2 or len(next(iter(tn_tokens))).__int__ if False else len(list(tn_tokens)[0]) >= 6:
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
    tn_norm = _norm(track_name)
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

def apply_exponential_backoff(attempts, base=1.0, jitter=0.5):
    delay = base * (2 ** attempts)
    jittered_delay = delay * random.uniform(1.0, 1.0 + jitter)
    logging.debug(f"Sleeping for {jittered_delay:.2f} seconds (attempts: {attempts})")
    time.sleep(jittered_delay)

def retry_suspended_downloads(conn, table_name):
    logging.info(f"Retrying suspended downloads for table: {table_name}")
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, name, artists, attempts FROM {table_name} WHERE downloaded = 0 AND (suspended_until IS NULL OR suspended_until < datetime('now'))")
    tracks_to_retry = cursor.fetchall()

    for track in tracks_to_retry:
        track_id, name, artists, attempts = track
        logging.info(f"Retrying track {name} by {artists}")
        apply_exponential_backoff(attempts)

    #return tracks_to_retry
    return [Track(track[0], track[1], track[2], "") for track in tracks_to_retry]

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
    logging.info("Setting up Spotify client")
    auth_manager = SpotifyClientCredentials()
    sp = spotipy.Spotify(auth_manager=auth_manager)
    logging.info("Spotify client setup complete")
    return sp

def send_ntfy_notification(url, topic, message):
    try:
        response = requests.post(f"{url}/{topic}", data=message)
        if response.status_code == 200:
            logging.info(f"Notification sent successfully: {message}")
        else:
            logging.error(f"Failed to send notification: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Error while sending notification: {e}")

def all_tracks_downloaded(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE downloaded = 0")
    remaining_tracks = cursor.fetchone()[0]
    
    if remaining_tracks == 0:
        logging.info(f"All tracks in playlist {table_name} have been downloaded.")
        return True
    else:
        logging.info(f"{remaining_tracks} tracks in playlist {table_name} are still not downloaded.")
        return False
    


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
    logging.info(f"Waiting for slskd at {host} (timeout: {timeout}s)...")
    client = slskd_api.SlskdClient(
        host=os.getenv("SLSKD_HOST_URL", "http://slskd:5030"),
        api_key=os.getenv("SLSKD_API_KEY"),
        url_base=os.getenv("SLSKD_URL_BASE", "")
    )

    start = time.time()
    while time.time() - start < timeout:
        try:
            state = client.application.state()
            if state['server'].get('isConnected') and state['server'].get('isLoggedIn'):
                logging.info("✅ slskd is healthy and connected.")
                return
        except Exception as e:
            pass  # silence repeated logs

        if int(time.time() - start) % 5 == 0:
            logging.debug("Still waiting for slskd...")

        time.sleep(check_interval)

    raise RuntimeError("❌ slskd did not become healthy in time.")


def process_playlist(sp, conn, playlist_id, ntfy_url, ntfy_topic):
    logging.info(f"🎧 Processing playlist ID: {playlist_id}")

    new_tracks, playlist_name = fetch_and_compare_tracks(conn, playlist_id, sp)

    startup_check(conn, playlist_name)

    if new_tracks:
        msg = f"🔄 Playlist updated: {len(new_tracks)} new track(s) added to {playlist_name}"
        send_ntfy_notification(ntfy_url, ntfy_topic, msg)

    tracks = get_pending_tracks(conn, playlist_name)
    if not tracks:
        logging.info(f"No pending tracks for {playlist_name}")
        return

    for track in tracks:
        logging.info(f"🎶 Downloading: {track.name} by {track.artist}")
        search_results = perform_search(track.artist, track.name)

        success = handle_track_download(
            track=track,
            playlist_name=playlist_name,
            conn=conn,
            search_results=search_results,
            max_attempts=2
        )

        if success:
            logging.info(f"✅ Downloaded: {track.name} by {track.artist}")
        else:
            logging.warning(f"❌ Failed: {track.name} by {track.artist}")

    send_ntfy_notification(ntfy_url, ntfy_topic, f"✅ Finished processing playlist: {playlist_name}")


def safe_get(tag):
    if isinstance(tag, list):
        return tag[0]
    return tag

def handle_track_download(track, playlist_name, conn, search_results, max_attempts=2):
    if not search_results:
        logging.warning(f"No search results for: {track.name} by {track.artist}")
        return False

    file_path = download_and_verify(
        search_results=search_results,
        expected_title=track.name,
        expected_artist=track.artist,
        conn=conn,
        playlist_name=playlist_name,
        track_id=track.id,
        max_attempts=max_attempts,
    )

    if file_path:
        verified, final_path = process_downloaded_file(file_path, playlist_name, conn)
        if verified:
            clear_tried_entries(conn, playlist_name, track.id)
            return True

    update_download_status(conn, track.id, playlist_name, success=False)
    return False


def main():
    setup_logging()
    logging.info("Starting main process")

    SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
    SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
    SLSKD_API_KEY = os.getenv("SLSKD_API_KEY")
    SLSKD_HOST_URL = os.getenv("SLSKD_HOST_URL", "http://slskd:5030")
    NTFY_URL = os.getenv('NTFY_URL')
    NTFY_TOPIC = os.getenv('NTFY_TOPIC')
    DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", "/downloads")
    DATA_ROOT = os.getenv("DATA_ROOT", "/data") 
    playlist_urls = os.getenv('SPOTIFY_PLAYLIST_URLS').split(',')

    wait_for_slskd_healthy(SLSKD_HOST_URL, SLSKD_API_KEY)

    send_ntfy_notification(NTFY_URL, NTFY_TOPIC, "Starting Spotify Playlist Downloader 🚀")
    sp = setup_spotify_client()

    database = "./data/playlist_tracks.db"
    conn = create_connection(database)


    if not conn:
        logging.error("Failed to connect to the SQLite database.")
        return

    

    for playlist_url in playlist_urls:
        playlist_id = get_playlist_id(playlist_url)
        playlist_name = sanitize_table_name(playlist_id)
        process_playlist(sp, conn, playlist_id, NTFY_URL, NTFY_TOPIC)

    while True:
        logging.info("Starting new cycle of playlist checks")
        for playlist_url in playlist_urls:
            playlist_id = get_playlist_id(playlist_url)
            playlist_name = sanitize_table_name(playlist_id)
            process_playlist(sp, conn, playlist_id, NTFY_URL, NTFY_TOPIC)
        sleep_interval(5)


if __name__ == "__main__":
    main()
