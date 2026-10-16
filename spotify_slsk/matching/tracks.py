"""Local-phase matching: a file's tags (or name) against DB rows.

Two matchers live here and are deliberately *not* merged:

* `score_track_match` gates a destructive decision (accept the download, or
  delete it). Artist overlap and an identical version are hard requirements.
* `_looks_like_match` is a lenient plausibility check used only by the
  non-destructive startup reconcile, where a false negative costs a
  re-download and a false positive costs nothing on disk.

Both use difflib similarity over `_norm`/`_tokenize`, not the strict
search-phase rules in `matching.candidates`.
"""
from __future__ import annotations

import difflib
import logging
import re
import sqlite3
from typing import Any

from .normalize import (
    ARTIST_SPLIT_RE,
    credited_names,
    expected_title_tokens,
    fold,
    version_keywords,
    version_mismatch,
)

# Neutral release words only. Version words (dub, vip, club mix, instrumental,
# a named mix) are kept: stripping them collapsed different recordings into
# one. score_track_match compares versions explicitly via _version_matches.
_STOP_PHRASES = [
    "original mix", "extended mix", "radio edit", "remastered", "remaster",
    "edit", "version", "clean", "explicit"
]


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
    # Folded like the slskd matcher: accents, punctuation and a "The " prefix
    # don't stop "Beyoncé"/"Beyonce" or "The Weeknd"/"Weeknd" overlapping.
    return {n for p in ARTIST_SPLIT_RE.split(artist_str) for n in credited_names(p)}

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
    if shorter <= longer:
        # "Dream" is not "A Real Bad Dream": the shorter title must be at
        # least half of the longer one.
        return len(shorter) * 2 >= len(longer)
    # One missing word is tolerated only for titles of 3+ words; on a
    # 2-word title it meant one shared word ("you", "the", "eyes") passed.
    return len(shorter) >= 3 and len(shorter & longer) >= len(shorter) - 1

def _remix_equivalent(file_title: str, db_title: str) -> bool:
    # Normalize "(Walker & Royce Remix)" vs "- Walker & Royce Remix"
    f = re.sub(r"[()\[\]{}\-–—]", " ", file_title or "", flags=re.IGNORECASE)
    d = re.sub(r"[()\[\]{}\-–—]", " ", db_title or "", flags=re.IGNORECASE)
    f = re.sub(r"\s+remix\b", " remix", f, flags=re.IGNORECASE)
    d = re.sub(r"\s+remix\b", " remix", d, flags=re.IGNORECASE)
    return _titles_token_equivalent(f, d)


def find_closest_match(
    conn: sqlite3.Connection, table_name: str, title: str, artist: str
) -> tuple[tuple[Any, ...] | None, float]:
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

    An artist overlap and the same version are hard requirements: a title-only
    match used to score 0.90 (0.88 for remixes), far above config.MIN_MATCH_SCORE,
    which accepted "Your Eyes" for "Eyes On Fire" by someone else.
    """
    title_sim = _similar(file_title, db_title)
    if not _artists_overlap(file_artist, db_artist):
        return 0.0, f"no_artist_overlap fuzzy(title={title_sim:.2f})"
    if not _version_matches(file_title, db_title, db_artist):
        return 0.0, "version_mismatch"

    if _titles_token_equivalent(file_title, db_title):
        return 0.97, "title_tokens+artist_overlap"

    if _remix_equivalent(file_title, db_title):
        return 0.95, "remix_equivalent+artist_overlap"

    score = min(1.0, title_sim * 0.85 + 0.15)
    return score, f"fuzzy(title={title_sim:.2f})+artist_overlap"


def _version_matches(file_title: str, db_title: str, artist: str = "") -> bool:
    """Same version keywords on both sides, and for a remix/named edit the
    expected remixer's words must be in the file title too ("(X Remix)" is
    not "(Y Remix)")."""
    if version_mismatch(db_title, file_title, artist):
        return False
    if not version_keywords(db_title, artist):
        return True
    return expected_title_tokens(db_title) <= set(fold(file_title).split())

def find_closest_db_match(
    conn: sqlite3.Connection, table_name: str, file_title: str, file_artist: str
) -> tuple[Any, ...]:
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
