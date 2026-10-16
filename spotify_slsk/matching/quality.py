"""The quality model: what a file *is*, independent of whether it is wanted.

A quality is one rung of ``LADDER`` (best first). It is derived from slskd's
search metadata before a download (``classify``) and from the file itself
afterwards (``quality_of_file``, mutagen headers only - no decode).

FLAC 24-bit is a single rung whatever its sample rate. 24/44.1, 24/48 and
24/96 are not reliably distinguishable from the peer's metadata (peers
mislabel sample rates, and an upsampled CD rip *claims* 96 kHz), and
nobody can hear the difference between them; the real defect - a hi-res
container holding CD audio - is caught by the size plausibility demotion in
``candidates`` and the ``upsampled`` spectrum verdict instead.

The fixed 320 kbps floor for MP3s with a known bitrate lives in
``candidates.evaluate_candidates`` and is not a quality decision: MP3 V0 and
lower MP3 are on the ladder so they can be *named*, but they are always
rejected, whatever a profile says.

This module imports nothing from the package so ``config`` can use it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Quality:
    key: str
    label: str
    ext: str
    lossless: bool
    # Typical bitrate, for "size closest to the preferred size" ranking.
    preferred_kbps: int


LADDER: tuple[Quality, ...] = (
    Quality("FLAC_24", "FLAC 24-bit", ".flac", True, 2000),
    Quality("FLAC_16", "FLAC 16-bit", ".flac", True, 900),
    Quality("ALAC", "ALAC", ".m4a", True, 900),
    Quality("AIFF", "AIFF", ".aiff", True, 1411),
    Quality("WAV", "WAV", ".wav", True, 1411),
    Quality("MP3_320", "MP3 320", ".mp3", False, 320),
    Quality("MP3_V0", "MP3 V0", ".mp3", False, 245),
    Quality("AAC_256", "AAC 256+", ".m4a", False, 256),
    Quality("OGG", "OGG/Opus", ".ogg", False, 256),
    Quality("AAC_LOW", "AAC <256", ".m4a", False, 192),
    Quality("MP3_LOW", "MP3 <320", ".mp3", False, 192),
)
BY_KEY: dict[str, Quality] = {q.key: q for q in LADDER}
LADDER_INDEX: dict[str, int] = {q.key: i for i, q in enumerate(LADDER)}
# Rejected by the fixed MP3 floor whatever the profile says.
FLOOR_BLOCKED = frozenset({"MP3_V0", "MP3_LOW"})


def classify(
    ext: str,
    bitrate: int | None = None,
    bit_depth: int | None = None,
    vbr: bool | None = None,
    kbps_from_size: int | None = None,
) -> str | None:
    """Quality key from slskd-style metadata, or None for a format with no
    rung (.wma, .jpg, ...). An MP3 of unknown bitrate counts as 320 - slskd
    often omits it, and the effective-bitrate floor catches the fakes."""
    ext = (ext or "").lower()
    if ext == ".flac":
        return "FLAC_24" if (bit_depth or 0) >= 24 else "FLAC_16"
    if ext == ".aiff":
        return "AIFF"
    if ext == ".wav":
        return "WAV"
    if ext == ".ogg":
        return "OGG"
    if ext == ".mp3":
        if bitrate is None or bitrate >= 320:
            return "MP3_320"
        if vbr and bitrate >= 220:
            return "MP3_V0"
        return "MP3_LOW"
    if ext == ".m4a":
        # slskd reports bitRate for lossy files and bitDepth for lossless.
        if bit_depth or (bitrate is None and (kbps_from_size or 0) > 500):
            return "ALAC"
        return "AAC_256" if bitrate is None or bitrate >= 256 else "AAC_LOW"
    return None


def describe(key: str | None, bitrate: int | None = None) -> str:
    """Human label; below-floor lossy rungs carry their real bitrate
    ("MP3 256") because the rung name alone ("MP3 <320") says less."""
    if key is None:
        return "unknown"
    if key in ("MP3_LOW", "AAC_LOW", "MP3_V0") and bitrate:
        return f"{BY_KEY[key].label.split()[0]} {bitrate}" + (" (V0)" if key == "MP3_V0" else "")
    return BY_KEY[key].label


def candidate_quality(c: dict[str, Any]) -> str | None:
    """Quality of an evaluated candidate (computed if it was not stamped)."""
    if "quality" in c:
        return c["quality"]
    kbps = None
    if c.get("size") and c.get("length"):
        kbps = int(c["size"] * 8 / c["length"] / 1000)
    return classify(c.get("ext", ""), c.get("bitrate"), c.get("bit_depth"), c.get("vbr"), kbps)


def quality_of_file(path: str) -> str | None:
    """Quality of a local file from its headers (mutagen), or None when
    unreadable or not an audio format on the ladder."""
    ext = os.path.splitext(path)[1].lower()
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
    except Exception as e:
        logging.debug(f"quality_of_file: cannot read {path}: {e}")
        return None
    if audio is None or getattr(audio, "info", None) is None:
        return None
    info = audio.info
    bitrate = getattr(info, "bitrate", 0) or 0
    kbps = round(bitrate / 1000) if bitrate else None
    bit_depth = getattr(info, "bits_per_sample", None)
    if ext == ".mp3":
        from mutagen.mp3 import BitrateMode

        vbr = getattr(info, "bitrate_mode", None) in (BitrateMode.VBR, BitrateMode.ABR)
        # A 320 CBR frame header reads 320000; tolerate rounding.
        return classify(ext, 320 if kbps and kbps >= 318 else kbps, None, vbr)
    if ext == ".m4a":
        codec = (getattr(info, "codec", "") or "").lower()
        if codec == "alac":
            return "ALAC"
        return classify(ext, kbps or None, None, None)
    return classify(ext, kbps, bit_depth)


# What a lossless file whose spectrum check failed most likely really is.
# suspect = a cliff in the MP3-320 band; fake = a lower lossy cutoff;
# upsampled / padded_16 = CD audio in a 24-bit container. Strings rather
# than importer.audio_check constants: matching sits below importer.
_VERDICT_DEMOTION = {
    "suspect": "MP3_320",
    "fake": "MP3_LOW",
    "upsampled": "FLAC_16",
    "padded_16": "FLAC_16",
}


def effective_quality(key: str | None, audio_check: str | None) -> str | None:
    """A file's quality after its lossless verdict: a SUSPECT/FAKE "FLAC"
    is not true lossless and ranks as the lossy source it came from."""
    if key is None or not BY_KEY[key].lossless:
        return key
    demoted = _VERDICT_DEMOTION.get(audio_check or "")
    if demoted is None:
        return key
    # Never *promote* (a 16-bit file flagged padded_16 stays 16-bit).
    return demoted if LADDER_INDEX[demoted] > LADDER_INDEX[key] else key
