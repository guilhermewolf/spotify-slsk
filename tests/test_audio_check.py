"""Fake-lossless detector (importer/audio_check.py).

Signals are synthesised with numpy: white noise is the worst case for a
spectral check (content everywhere), and a steep FFT-domain lowpass at 16 kHz
is what a 128 kbps MP3 leaves behind. The array-level tests always run; the
decode tests write real FLACs with the `flac` binary (CI installs it) and
skip with a reason when it is missing.
"""
import shutil
import subprocess
import wave

import numpy as np
import pytest

from spotify_slsk import config
from spotify_slsk.importer import audio_check as ac

RNG = np.random.default_rng(1234)


def _noise(sr, seconds=20.0, channels=2):
    return RNG.uniform(-0.5, 0.5, size=(int(sr * seconds), channels))


def _shape(x, sr, gain):
    """Apply a per-frequency amplitude gain(freqs) in the FFT domain."""
    spec = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    return np.fft.irfft(spec * gain(freqs)[:, None], n=len(x), axis=0)


def _lowpass(x, sr, cutoff):
    return _shape(x, sr, lambda f: (f <= cutoff).astype(float))


def _to_int(x, bits):
    peak = float(np.max(np.abs(x))) or 1.0
    return np.round(x / peak * 0.8 * (2 ** (bits - 1) - 1)).astype(np.int32)


def _verdict(x, sr=44100, bits=16):
    return ac.analyse(_to_int(x, bits), sr, bits)


class TestAnalyse:
    def test_full_band_noise_is_ok(self):
        result = _verdict(_noise(44100))
        assert result.verdict == ac.OK
        assert result.describe() == "lossless ok"

    def test_lossy_lowpass_at_16k_is_fake(self):
        result = _verdict(_lowpass(_noise(44100), 44100, 16000))
        assert result.verdict == ac.FAKE
        assert abs(result.cutoff_hz - 16000) < 600
        assert result.drop_db >= 15
        assert result.rejected
        assert result.describe().startswith("fake lossless (cutoff 16.")

    def test_cliff_near_20k_is_only_suspect(self):
        # MP3-320 territory: ambiguous with brickwalled masters, never rejected.
        result = _verdict(_lowpass(_noise(44100), 44100, 20000))
        assert result.verdict == ac.SUSPECT
        assert not result.rejected

    def test_gentle_rolloff_is_ok(self):
        # A dark master: high frequencies fall off smoothly, no cliff.
        dark = _shape(_noise(44100), 44100, lambda f: 1 / (1 + f / 3000))
        assert _verdict(dark).verdict == ac.OK

    def test_hi_res_with_content_cut_at_22k_is_upsampled(self):
        x = _lowpass(_noise(96000, seconds=10), 96000, 22000)
        result = _verdict(x, sr=96000, bits=24)
        assert result.verdict == ac.UPSAMPLED
        assert result.rejected

    def test_genuine_hi_res_is_ok(self):
        assert _verdict(_noise(96000, seconds=10), sr=96000, bits=24).verdict == ac.OK

    def test_24_bit_with_zero_low_byte_is_padded_16(self):
        ints = _to_int(_noise(44100), 16) << 8
        result = ac.analyse(ints, 44100, 24)
        assert result.verdict == ac.PADDED_16
        assert result.rejected

    def test_real_24_bit_is_not_padded(self):
        assert ac.analyse(_to_int(_noise(44100), 24), 44100, 24).verdict == ac.OK

    def test_silence_is_unknown_never_rejected(self):
        result = ac.analyse(np.zeros((44100 * 5, 2), np.int32), 44100, 16)
        assert result.verdict == ac.UNKNOWN
        assert not result.rejected

    def test_thresholds_come_from_config(self, monkeypatch):
        fake_at_16k = _lowpass(_noise(44100), 44100, 16000)
        monkeypatch.setattr(config, "LOSSLESS_FAKE_CUTOFF_HZ", 15000)
        assert _verdict(fake_at_16k).verdict == ac.SUSPECT
        monkeypatch.setattr(config, "LOSSLESS_FAKE_CUTOFF_HZ", 19500)
        monkeypatch.setattr(config, "LOSSLESS_MIN_DROP_DB", 500)
        assert _verdict(fake_at_16k).verdict == ac.OK


class TestWindows:
    def test_long_file_samples_three_segments_in_the_middle(self):
        sr, total = 44100, 44100 * 300
        wins = ac._windows(total, sr)
        assert len(wins) == ac.SEGMENTS
        assert wins[0][0] >= total * 0.1 and wins[-1][1] <= total * 0.9 + 1
        assert all(e - s == int(ac.SEGMENT_SECONDS * sr) for s, e in wins)

    def test_short_file_is_read_whole(self):
        assert ac._windows(44100 * 20, 44100) == [(0, 44100 * 20)]


def _write_wav(path, ints, sr, width):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ints.shape[1])
        w.setsampwidth(width)
        w.setframerate(sr)
        w.writeframes(ints.astype("<i2" if width == 2 else "<i4").tobytes())


class TestDecodeWav:
    def test_fake_wav_is_detected(self, tmp_path):
        path = tmp_path / "fake.wav"
        _write_wav(path, _to_int(_lowpass(_noise(44100), 44100, 16000), 16), 44100, 2)
        assert ac.check_file(str(path)).verdict == ac.FAKE

    def test_unsupported_and_corrupt_files_are_unknown(self, tmp_path):
        bad = tmp_path / "broken.flac"
        bad.write_bytes(b"not audio")
        assert ac.check_file(str(bad)).verdict == ac.UNKNOWN
        assert ac.check_file(str(tmp_path / "x.mp3")).verdict == ac.UNKNOWN


needs_flac = pytest.mark.skipif(
    shutil.which("flac") is None,
    reason="flac binary not installed (CI installs it; brew/apt install flac)",
)


def _write_flac(path, ints, sr, bits):
    width = (bits + 7) // 8
    if width == 3:
        b = ints.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
        raw = b.tobytes()
    else:
        raw = ints.astype("<i2").tobytes()
    subprocess.run(
        ["flac", "-s", "-f", "--force-raw-format", "--endian=little", "--sign=signed",
         f"--channels={ints.shape[1]}", f"--bps={bits}", f"--sample-rate={sr}",
         "-o", str(path), "-"],
        input=raw, check=True, capture_output=True,
    )


@needs_flac
class TestDecodeFlac:
    def test_transcode_is_fake_and_real_is_ok(self, tmp_path):
        fake, real = tmp_path / "fake.flac", tmp_path / "real.flac"
        # 60 s, so the segmented --skip/--until path is exercised.
        _write_flac(fake, _to_int(_lowpass(_noise(44100, 60), 44100, 16000), 16), 44100, 16)
        _write_flac(real, _to_int(_noise(44100, 60), 16), 44100, 16)
        result = ac.check_file(str(fake))
        assert result.verdict == ac.FAKE
        assert (result.sample_rate, result.bit_depth) == (44100, 16)
        assert ac.check_file(str(real)).verdict == ac.OK

    def test_24_bit_padded_flac(self, tmp_path):
        path = tmp_path / "padded.flac"
        _write_flac(path, _to_int(_noise(44100), 16) << 8, 44100, 24)
        assert ac.check_file(str(path)).verdict == ac.PADDED_16

    def test_missing_binary_is_unknown(self, tmp_path, monkeypatch):
        path = tmp_path / "real.flac"
        _write_flac(path, _to_int(_noise(44100, 5), 16), 44100, 16)
        monkeypatch.setattr(ac.shutil, "which", lambda name: None)
        assert ac.check_file(str(path)).verdict == ac.UNKNOWN
