"""Fake-lossless detection: is this FLAC/WAV really lossless?

A lossy encoder (MP3, AAC, Vorbis) throws away the top of the spectrum with a
steep lowpass: ~16 kHz at 128 kbps, ~19 kHz at 192, ~20-20.5 kHz at 320.
Transcoding that back to FLAC keeps the hole. A genuine CD master has content
(or at least noise) up to its anti-alias filter near Nyquist.

Method: decode a bounded window (``SEGMENTS`` x ``SEGMENT_SECONDS`` spread over
the middle 80% of the track, so RAM stays at a few MB whatever the length),
mono-mix, Welch-average Hann frames of ``FRAME`` samples (near-silent frames
dropped), smooth the dB spectrum to ~170 Hz bands, then find the steepest
drop between 11 kHz and Nyquist: the mean level 0.2-1 kHz below f minus the
mean level 0.2-1 kHz above f.

Verdicts (thresholds in ``config``; the measured margins are thin, ~3-5 dB):

* ``fake``      drop >= LOSSLESS_MIN_DROP_DB below LOSSLESS_FAKE_CUTOFF_HZ
* ``upsampled`` sample rate > 48 kHz and a >= 20 dB drop at <= 26 kHz
                (a CD master resampled to "hi-res")
* ``padded_16`` a 24-bit file whose low 8 bits are always zero
* ``suspect``   the drop sits between the cutoff and 20.5 kHz: MP3-320 and
                AAC-256 land here, but so do brickwalled masters, so it is
                never rejected
* ``ok``        none of the above
* ``unknown``   could not decode or too little non-silent audio; never rejected

Known blind spot: a high-bitrate AAC (>= 256 kbps) transcode keeps content to
~20 kHz and is indistinguishable from a real master by this method.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass

import numpy as np

from .. import config

FAKE, UPSAMPLED, PADDED_16, SUSPECT, OK, UNKNOWN = (
    "fake", "upsampled", "padded_16", "suspect", "ok", "unknown",
)
# The verdicts that reject a download (and flag a library file).
REJECT_VERDICTS = frozenset({FAKE, UPSAMPLED, PADDED_16})
# Formats this module can decode: FLAC via the `flac` CLI, WAV via stdlib.
CHECKABLE_EXTS = (".flac", ".wav")

FRAME = 4096
SILENCE_RMS = 1e-3
MIN_FRAMES = 8
SEGMENTS = 3
SEGMENT_SECONDS = 10.0
SMOOTH_HZ = 170.0
SEARCH_FROM_HZ = 11000.0
BAND_NEAR_HZ, BAND_FAR_HZ = 200.0, 1000.0
SUSPECT_MAX_HZ = 20500.0
UPSAMPLED_MAX_HZ = 26000.0
UPSAMPLED_MIN_DROP_DB = 20.0
DECODE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class AudioCheck:
    verdict: str
    cutoff_hz: int | None = None
    drop_db: float | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    error: str | None = None

    @property
    def rejected(self) -> bool:
        return self.verdict in REJECT_VERDICTS

    def describe(self) -> str:
        """Human reason, e.g. "fake lossless (cutoff 16.2 kHz, drop 21 dB)"."""
        label = {
            FAKE: "fake lossless",
            UPSAMPLED: "upsampled hi-res",
            PADDED_16: "16-bit audio padded to 24-bit",
            SUSPECT: "suspect lossless",
            OK: "lossless ok",
            UNKNOWN: "lossless check inconclusive",
        }.get(self.verdict, self.verdict)
        if self.verdict == PADDED_16:
            return label
        if self.verdict == UNKNOWN:
            return f"{label} ({self.error})" if self.error else label
        if self.cutoff_hz is None or self.drop_db is None:
            return label
        return f"{label} (cutoff {self.cutoff_hz / 1000:.1f} kHz, drop {self.drop_db:.0f} dB)"


def steepest_drop(mono: np.ndarray, sample_rate: int) -> tuple[float, float] | None:
    """(frequency Hz, drop dB) of the steepest spectral drop above 11 kHz, or
    None when there is too little non-silent audio to judge."""
    n = len(mono) // FRAME
    if n == 0:
        return None
    frames = mono[: n * FRAME].reshape(n, FRAME)
    frames = frames[np.sqrt((frames**2).mean(axis=1)) > SILENCE_RMS]
    if len(frames) < MIN_FRAMES:
        return None
    power = (np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(power + 1e-20)
    step = sample_rate / FRAME
    k = max(1, round(SMOOTH_HZ / step))
    # "valid" so the edges are not averaged against zero padding.
    smooth = np.convolve(db, np.ones(k) / k, mode="valid")
    freqs = (np.arange(len(smooth)) + (k - 1) / 2) * step
    near, far = max(1, int(BAND_NEAR_HZ / step)), int(BAND_FAR_HZ / step)
    start = max(far, int(np.searchsorted(freqs, SEARCH_FROM_HZ)))
    idx = np.arange(start, len(smooth) - far)
    if len(idx) == 0:
        return None
    csum = np.concatenate([[0.0], np.cumsum(smooth)])
    below = (csum[idx - near] - csum[idx - far]) / (far - near)
    above = (csum[idx + far] - csum[idx + near]) / (far - near)
    drop = below - above
    best = int(np.argmax(drop))
    return float(freqs[idx[best]]), float(drop[best])


def is_padded_16(samples: np.ndarray, bit_depth: int) -> bool:
    """A >16-bit file whose extra low bits are zero in every sample: 16-bit
    audio in a bigger container. All-zero (silent) input proves nothing."""
    if bit_depth <= 16 or not np.any(samples):
        return False
    return not np.any(samples & ((1 << (bit_depth - 16)) - 1))


def classify(
    sample_rate: int, cutoff_hz: float, drop_db: float, padded: bool = False
) -> str:
    """Map a measurement to a verdict using the config thresholds."""
    lossy_cliff = drop_db >= config.LOSSLESS_MIN_DROP_DB
    if lossy_cliff and cutoff_hz < config.LOSSLESS_FAKE_CUTOFF_HZ:
        return FAKE
    if sample_rate > 48000 and drop_db >= UPSAMPLED_MIN_DROP_DB and cutoff_hz <= UPSAMPLED_MAX_HZ:
        return UPSAMPLED
    if padded:
        return PADDED_16
    if lossy_cliff and cutoff_hz < SUSPECT_MAX_HZ:
        return SUSPECT
    return OK


def analyse(samples: np.ndarray, sample_rate: int, bit_depth: int) -> AudioCheck:
    """Judge integer PCM `samples` shaped (n, channels)."""
    mono = samples.mean(axis=1) / float(1 << (bit_depth - 1))
    measured = steepest_drop(mono, sample_rate)
    if measured is None:
        return AudioCheck(UNKNOWN, sample_rate=sample_rate, bit_depth=bit_depth,
                          error="too little non-silent audio")
    cutoff, drop = measured
    verdict = classify(sample_rate, cutoff, drop, is_padded_16(samples, bit_depth))
    return AudioCheck(verdict, round(cutoff), round(drop, 1), sample_rate, bit_depth)


def _windows(total: int, sample_rate: int) -> list[tuple[int, int]]:
    """(start, end) sample ranges: SEGMENTS windows across the middle 80%,
    or the whole file when it is too short for that."""
    seg = int(SEGMENT_SECONDS * sample_rate)
    if total <= seg * SEGMENTS * 1.25:
        return [(0, total)]
    starts = np.linspace(total * 0.1, total * 0.9 - seg, SEGMENTS).astype(int)
    return [(int(s), int(s) + seg) for s in starts]


def _unpack(raw: bytes, width: int, channels: int) -> np.ndarray:
    raw = raw[: len(raw) - len(raw) % (width * channels)]
    if width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = (ints << 8) >> 8  # sign-extend 24 -> 32 bits
    else:
        dtype = {1: "<i1", 2: "<i2", 4: "<i4"}[width]
        ints = np.frombuffer(raw, dtype).astype(np.int32)
    return ints.reshape(-1, channels)


def _decode_flac(path: str) -> tuple[np.ndarray, int, int]:
    from mutagen.flac import FLAC

    info = FLAC(path).info
    sr, bits, ch = info.sample_rate, info.bits_per_sample, info.channels
    flac_bin = shutil.which("flac")
    if not flac_bin:
        raise RuntimeError("flac binary not found")
    width = (bits + 7) // 8
    chunks = []
    for start, end in _windows(info.total_samples, sr):
        proc = subprocess.run(
            [flac_bin, "-d", "-c", "-s", "--force-raw-format", "--endian=little",
             "--sign=signed", f"--skip={start}", f"--until={end}",
             os.path.abspath(path)],
            capture_output=True, timeout=DECODE_TIMEOUT_SECONDS, check=True,
        )
        chunks.append(_unpack(proc.stdout, width, ch))
    return np.concatenate(chunks), sr, bits


def _decode_wav(path: str) -> tuple[np.ndarray, int, int]:
    with wave.open(path, "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        chunks = []
        for start, end in _windows(w.getnframes(), sr):
            w.setpos(start)
            raw = w.readframes(end - start)
            if width == 1:  # 8-bit WAV is unsigned
                ints = (np.frombuffer(raw, np.uint8).astype(np.int32) - 128).reshape(-1, ch)
            else:
                ints = _unpack(raw, width, ch)
            chunks.append(ints)
    return np.concatenate(chunks), sr, width * 8


def check_file(path: str) -> AudioCheck:
    """Decode a bounded window of `path` and judge it. Never raises: any
    decode problem is an ``unknown`` verdict, which never rejects."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".flac":
            samples, sr, bits = _decode_flac(path)
        elif ext == ".wav":
            samples, sr, bits = _decode_wav(path)
        else:
            return AudioCheck(UNKNOWN, error=f"cannot decode {ext or 'this format'}")
        return analyse(samples, sr, bits)
    except Exception as e:  # corrupt file, missing binary, timeout ...
        logging.warning(f"Lossless check could not decode {path}: {e}")
        return AudioCheck(UNKNOWN, error=type(e).__name__)
