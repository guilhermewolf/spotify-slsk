"""Search-phase matching: judge every file in an slskd search response.

`evaluate_candidates` applies the quality gates (format allowlist, 320 kbps
MP3 rule, effective-bitrate floor, duration, blocklist) and then the strict
wrong-song rules in `filename_match`; `sort_candidates` orders the accepted
ones (version tier > format > bitrate > peer upload speed).
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from typing import Any

from .. import config
from ..models import Candidate, IsBlocked
from ..slskd.models import SearchResponse, remote_basename
from ..utils import fmt_duration
from .normalize import (
    BRACKETS_RE,
    FEAT_RE,
    NEUTRAL_TITLE_WORDS,
    artist_in,
    credited_names,
    expected_title_tokens,
    fold,
    version_keywords,
    version_mismatch,
    version_tier,
)
from .quality import candidate_quality, classify, describe, effective_quality


def clean_filename(filename: str) -> str:
    """
    Clean a filename for fuzzy matching: replace brackets/parens with spaces
    (keep their content), normalize separators, drop the extension and
    obvious tag noise.

    We *don't* strip parenthesized content because real filenames embed
    version info there ("(Walker & Royce Remix)", "(Extended Mix)") that the
    title scorer needs to see. token_set_ratio is robust to extra tokens
    like a leftover "[FLAC]", so leaving them in costs us nothing.
    """
    filename = re.sub(r"[\[\]\(\)\{\}]", " ", filename)
    filename = re.sub(
        r"\b\d{1,2}bit\b|\b\d{1,3}\.\d{1,2}kHz\b|\b\d{4}\b",
        "",
        filename,
        flags=re.IGNORECASE,
    )
    filename = os.path.splitext(filename)[0]
    filename = filename.replace("_", " ").replace("-", " ")
    filename = " ".join(filename.split())
    return filename.lower().strip()


def _infer_bitrate_from_name(name: str):
    """
    Try to infer bitrate from the filename text. Returns kbps or None.
    """
    text = name.lower()
    m = re.search(
        r"(?<!\d)(320|256|224|192|160|128)\s*(k|kbps)?(?!\d)",
        text,
    )
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _duration_off(diff_seconds, longer_ok=False) -> bool:
    """True when a file's length is outside config.MAX_DURATION_DIFF_SECONDS of the
    expected one. A longer-cut version may run long but never short."""
    if longer_ok and diff_seconds > 0:
        return False
    return abs(diff_seconds) > config.MAX_DURATION_DIFF_SECONDS


# A 16-bit/44.1 kHz stereo FLAC compresses to roughly 600-1100 kbps; below
# this it is almost certainly a lossy file re-encoded (and padded with
# nothing). Mono recordings can legitimately sit near it - a known edge.
MIN_FLAC_KBPS = 400
# 24-bit >= 88.2 kHz FLAC of real hi-res content is usually 2000+ kbps; a
# CD master upsampled into that container compresses far better.
MIN_HIRES_FLAC_KBPS = 1200


def _flac_plausibility(kbps, bit_depth, sample_rate) -> tuple[str | None, str | None]:
    """(reject_reason, demote_note) for a FLAC from its size-derived bitrate."""
    if kbps is None:
        return None, None
    if (bit_depth or 16) <= 16 and (sample_rate or 44100) <= 48000 and kbps < MIN_FLAC_KBPS:
        return (
            f"FLAC at {kbps} kbps is implausibly low for 16-bit/44.1 kHz "
            f"(< {MIN_FLAC_KBPS}; likely a lossy transcode)"
        ), None
    if (bit_depth or 0) >= 24 and (sample_rate or 0) >= 88200 and kbps < MIN_HIRES_FLAC_KBPS:
        return None, (
            f"likely upsampled: {kbps} kbps for {bit_depth}-bit/"
            f"{sample_rate / 1000:g} kHz (< {MIN_HIRES_FLAC_KBPS}); ranked lower"
        )
    return None, None


def _effective_mp3_kbps(size_bytes, length_sec):
    if not size_bytes or not length_sec or length_sec <= 0:
        return None
    try:
        return int((size_bytes * 8) / length_sec / 1000)
    except Exception:
        return None


# --- Candidate title/artist matching ------------------------------------------
# The old scorer ran token_set_ratio over the whole name, which is 100 whenever
# every expected title word appears *anywhere*, and let one shared word clear
# the artist bar ("super flu" vs "...Super Supergirl" = 71). Title and artist
# are now judged separately against the basename.


# Filename noise that is neither title nor artist.
_FILENAME_NOISE = frozenset({
    "flac", "mp3", "wav", "aiff", "m4a", "ogg", "kbps", "web", "cd",
    "vinyl", "lossless", "hq", "bit", "khz",
})
# Share of the basename's title-ish words that must be the expected title.
# Rejects "Dream" -> "A Real Bad Dream" while tolerating an album name in the
# filename ("Daft Punk - Random Access Memories - 08 - Get Lucky" = 0.4).
MIN_TITLE_COVERAGE = 0.4


def _title_coverage(expected_tokens, stem, artist_names, folder_tokens) -> float:
    """0.0 unless every expected title word is in the basename; otherwise the
    share of the basename's title-ish words (outside brackets, minus artists,
    featured credits, numbers, noise and folder words) that are the title.
    A " - " segment naming an expected artist is the artist credit and is
    dropped whole, co-credits included ("Daft Punk, Pharrell Williams - ...")."""
    if not expected_tokens or not expected_tokens <= set(fold(stem).split()):
        return 0.0
    segments = re.split(r"\s+[-–—]\s+", BRACKETS_RE.sub(" ", FEAT_RE.sub(" ", stem)))
    outside = " "
    for seg in segments:
        folded = " " + fold(seg) + " "
        credit = any(artist_in(n, folded.strip()) for n in artist_names)
        if credit and not expected_tokens <= set(folded.split()):
            continue  # pure artist credit
        for name in artist_names:
            folded = folded.replace(f" {name} ", " ")
        outside += folded
    extras = [
        t for t in outside.split()
        if t not in expected_tokens
        and t not in NEUTRAL_TITLE_WORDS
        and t not in _FILENAME_NOISE
        and t not in folder_tokens
        and not t.isdigit()
    ]
    return len(expected_tokens) / (len(expected_tokens) + len(extras))


def filename_match(path: str, expected_title: str, expected_artist: str) -> tuple[float, str | None]:
    """Judge a peer path's name against the expected track with the strict
    wrong-song rules. Returns (title_coverage, reason); reason is None when
    the name is this track.

    Title evidence comes from the basename only; artist evidence may also
    come from the folders (shares are laid out Artist\\Album\\NN - Title).
    Folder text never supplies title words - that is how a "Vespers
    (All-Night Vigil)" folder matched "All Night".
    """
    base = remote_basename(path)
    stem = os.path.splitext(base)[0]
    folder_folded = fold(" ".join(re.split(r"[\\/]", path or "")[:-1]))
    artist_names = credited_names(expected_artist)
    title_tokens = expected_title_tokens(expected_title)
    # A basename that repeats its album folder ("Artist_Album Name_01_Song")
    # must still name the title outside that phrase: the folder-word exclusion
    # below would otherwise let "If Love Is a Skill & Georgia_01_If Love Is a
    # Skill" pass for "Georgia".
    padded = f" {fold(stem)} "
    for folder in re.split(r"[\\/]", path or "")[:-1]:
        phrase = fold(folder)
        if phrase and set(phrase.split()) != title_tokens and f" {phrase} " in padded:
            padded = padded.replace(f" {phrase} ", " ")
            if not title_tokens <= set(padded.split()):
                return 0.0, "title words only in the album/folder name"
    coverage = _title_coverage(
        title_tokens, stem, artist_names,
        set(folder_folded.split()),
    )
    base_folded = fold(stem)
    if coverage == 0.0:
        return coverage, "title words missing from filename"
    if coverage < MIN_TITLE_COVERAGE:
        return coverage, f"title coverage {coverage:.2f} < {MIN_TITLE_COVERAGE}"
    if not any(
        artist_in(n, base_folded) or artist_in(n, folder_folded)
        for n in artist_names
    ):
        return coverage, "artist not in filename or folder"
    # A different version (remix/live/... on one side only) is never usable.
    if version_mismatch(expected_title, stem, expected_artist):
        got = ", ".join(sorted(version_keywords(stem, expected_artist))) or "none"
        want = ", ".join(sorted(version_keywords(expected_title, expected_artist))) or "none"
        return coverage, f"version mismatch ({got} vs expected {want})"
    return coverage, None


def evaluate_candidates(
    search_results: Iterable[SearchResponse | dict[str, Any]],
    expected_title: str,
    expected_artist: str,
    max_version_tier: int | None = None,
    expected_duration_ms: int | None = None,
    is_blocked: IsBlocked | None = None,
    lossless_only: bool = False,
) -> list[Candidate]:
    """
    Judge every file in slskd search results against the expected track.

    Returns one dict per file (in response order) carrying the candidate
    fields plus ``accepted`` and, when rejected, a short human ``reason``
    ("format not allowed: .wma", "bitrate 192 < 320", ...). Unknown MP3
    bitrates are allowed (slskd often reports None); files are hard-rejected
    only when we *know* quality is too low.

    `max_version_tier`, when set, rejects anything not *strictly better*
    (used by the upgrade pass). `expected_duration_ms` (Spotify) rejects
    files whose reported length is more than config.MAX_DURATION_DIFF_SECONDS off.
    `is_blocked(username, filename)` (see db.load_blocklist) rejects
    blocklisted peer files and banned peers. `lossless_only` rejects every
    lossy format (replacing a "suspect" lossless file). Title/artist/version
    use the strict rules in `filename_match`.
    """
    evaluated = []

    for raw in search_results:
        result = raw if isinstance(raw, SearchResponse) else SearchResponse.from_json(raw)
        user = result.username
        user_upload_speed = result.upload_speed

        for file in result.files:
            filename = file.filename
            if not filename:
                continue

            ext = file.ext
            # slskd sends camelCase "bitRate" (only for lossy files); the old
            # lowercase read was always None, so the floor below never applied.
            reported_bitrate = file.bit_rate
            length_sec = file.length
            size_bytes = file.size
            base = file.basename
            inferred_bitrate = _infer_bitrate_from_name(base)
            effective_bitrate = (
                reported_bitrate if reported_bitrate is not None else inferred_bitrate
            )
            effective_mp3 = (
                _effective_mp3_kbps(size_bytes, length_sec) if ext == ".mp3" else None
            )
            flac_reject, flac_note = (
                _flac_plausibility(
                    _effective_mp3_kbps(size_bytes, length_sec),
                    file.bit_depth, file.sample_rate,
                )
                if ext == ".flac" else (None, None)
            )
            quality = classify(
                ext, effective_bitrate, file.bit_depth, file.is_variable_bit_rate,
                _effective_mp3_kbps(size_bytes, length_sec),
            )
            if flac_note:
                # Likely CD audio in a hi-res container: rank it as 16-bit.
                quality = effective_quality(quality, "upsampled")
            entry: Candidate = {
                "user": user,
                "user_upload_speed": user_upload_speed,
                # Full peer path: slskd needs it to enqueue the transfer, and
                # it is the blocklist key. `basename` is what `_tried` stores.
                "filename": filename,
                "basename": base,
                "size": size_bytes,
                "bitrate": effective_bitrate,
                "effective_mp3": effective_mp3,
                "ext": ext,
                "length": length_sec,
                "sample_rate": file.sample_rate,
                "bit_depth": file.bit_depth,
                "vbr": file.is_variable_bit_rate,
                "quality": quality,
                "quality_label": describe(quality, effective_bitrate),
                "free_slot": result.has_free_upload_slot,
                "queue_length": result.queue_length,
                "title_coverage": None,
                "version_tier": version_tier(base),
                "accepted": False,
                "reason": None,
                # Accepted but ranked below its peers (shown in the UI).
                "note": flac_note,
            }
            evaluated.append(entry)

            if is_blocked is not None and is_blocked(user, filename):
                entry["reason"] = "blocklisted"
            elif quality is None:
                entry["reason"] = f"format not allowed: {ext or '(none)'}"
            elif not config.QUALITY_PROFILE.allows(quality):
                entry["reason"] = f"quality not allowed by profile: {entry['quality_label']}"
            elif lossless_only and ext not in config.LOSSLESS_EXTS:
                entry["reason"] = "not lossless (replacing a suspect lossless file)"
            # Reject MP3s whose reported/inferred bitrate is known-low.
            elif ext == ".mp3" and effective_bitrate is not None and effective_bitrate < 320:
                entry["reason"] = f"bitrate {effective_bitrate} < 320"
            # Reject MP3s whose size/duration shows they're effectively below the
            # configured floor — catches 128 kbps files retagged as 320.
            elif (
                ext == ".mp3"
                and effective_mp3 is not None
                and effective_mp3 < config.MIN_EFFECTIVE_MP3_KBPS
            ):
                entry["reason"] = (
                    f"effective bitrate {effective_mp3} < {config.MIN_EFFECTIVE_MP3_KBPS}"
                )
            elif flac_reject:
                entry["reason"] = flac_reject
            elif (
                config.MAX_DURATION_DIFF_SECONDS > 0
                and expected_duration_ms
                and length_sec
                and _duration_off(
                    length_sec - expected_duration_ms / 1000,
                    # Extended/Original Mixes are the long cuts the version
                    # tiers prefer; Spotify often lists the radio edit.
                    longer_ok=version_tier(base) < version_tier(expected_title),
                )
            ):
                entry["reason"] = (
                    f"duration {fmt_duration(length_sec)} vs "
                    f"{fmt_duration(expected_duration_ms / 1000)}"
                )
            if entry["reason"]:
                logging.debug(f"Skipped {filename}: {entry['reason']}")
                continue

            title_coverage, reason = filename_match(filename, expected_title, expected_artist)
            entry["title_coverage"] = round(title_coverage, 2)
            if reason:
                entry["reason"] = reason
            elif max_version_tier is not None and entry["version_tier"] >= max_version_tier:
                entry["reason"] = "not a better version than the current file"
            else:
                entry["accepted"] = True
            if entry["reason"]:
                logging.debug(f"Skipped {filename}: {entry['reason']}")

    return evaluated


def extract_candidates(
    search_results: Iterable[SearchResponse | dict[str, Any]],
    expected_title: str,
    expected_artist: str,
) -> list[Candidate]:
    """Return only the accepted candidates from `evaluate_candidates`."""
    return [
        c
        for c in evaluate_candidates(search_results, expected_title, expected_artist)
        if c["accepted"]
    ]


def summarize_evaluation(evaluated: list[Candidate], limit: int = 50) -> dict[str, Any]:
    """Compact form of an evaluation for the per-track search history."""
    rejected = [c for c in evaluated if not c["accepted"]]
    return {
        "candidate_count": len(evaluated) - len(rejected),
        "rejected_count": len(rejected),
        "rejections": [
            {"user": c["user"], "filename": c["filename"], "reason": c["reason"]}
            for c in rejected[:limit]
        ],
    }


def sort_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    """
    Sort by DJ-version tier (Extended Mix > Original Mix > other), then by
    preferred extension, then demoted (likely-upsampled hi-res) last, then
    bitrate desc (unknown last), then peer upload
    speed desc.
    """
    profile = config.QUALITY_PROFILE

    def bitrate_rank(bps_k) -> int:
        return bps_k or 0

    return sorted(
        candidates,
        key=lambda c: (
            c.get("version_tier", 2),
            profile.rank(candidate_quality(c)),
            bool(c.get("note")),
            -bitrate_rank(c.get("bitrate")),
            -(c.get("user_upload_speed") or 0),
        ),
    )
