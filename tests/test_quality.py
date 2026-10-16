"""Tier 4 quality model: the ladder, classification from slskd metadata and
from local files, and the lossless-verdict demotion."""
import struct
import types
import wave

import mutagen
import pytest

from spotify_slsk.matching import quality as q


class TestLadder:
    def test_order_best_to_worst(self):
        keys = [x.key for x in q.LADDER]
        assert keys.index("FLAC_24") < keys.index("FLAC_16") < keys.index("ALAC")
        assert keys.index("WAV") < keys.index("MP3_320") < keys.index("MP3_V0")
        assert keys.index("MP3_V0") < keys.index("AAC_256") < keys.index("OGG")
        assert keys[-1] == "MP3_LOW"

    def test_floor_blocked_rungs_are_lossy_mp3(self):
        assert {q.BY_KEY[k].ext for k in q.FLOOR_BLOCKED} == {".mp3"}


class TestClassify:
    @pytest.mark.parametrize("args,expected", [
        ((".flac", None, 24), "FLAC_24"),
        ((".flac", None, 16), "FLAC_16"),
        ((".flac", None, None), "FLAC_16"),
        ((".mp3", 320), "MP3_320"),
        ((".mp3", None), "MP3_320"),  # unknown bitrate: allowed, like before
        ((".mp3", 245, None, True), "MP3_V0"),
        ((".mp3", 256, None, False), "MP3_LOW"),
        ((".mp3", 192, None, True), "MP3_LOW"),
        ((".m4a", 256), "AAC_256"),
        ((".m4a", 128), "AAC_LOW"),
        ((".m4a", None, 16), "ALAC"),
        ((".m4a", None, None, None, 900), "ALAC"),
        ((".aiff",), "AIFF"),
        ((".wav",), "WAV"),
        ((".ogg",), "OGG"),
        ((".wma", 320), None),
        ((".jpg",), None),
    ])
    def test_classify(self, args, expected):
        assert q.classify(*args) == expected

    def test_describe_names_the_real_bitrate(self):
        assert q.describe("MP3_LOW", 256) == "MP3 256"
        assert q.describe("MP3_V0", 245) == "MP3 245 (V0)"
        assert q.describe("FLAC_24") == "FLAC 24-bit"
        assert q.describe(None) == "unknown"

    def test_candidate_quality_uses_stamp_or_fields(self):
        assert q.candidate_quality({"quality": "OGG", "ext": ".flac"}) == "OGG"
        assert q.candidate_quality({"ext": ".flac", "bit_depth": 24}) == "FLAC_24"


class TestQualityOfFile:
    def _fake(self, monkeypatch, **info):
        monkeypatch.setattr(
            mutagen, "File", lambda path: types.SimpleNamespace(info=types.SimpleNamespace(**info))
        )

    def test_real_wav(self, tmp_path):
        path = tmp_path / "a.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(struct.pack("<hh", 0, 0) * 4410)
        assert q.quality_of_file(str(path)) == "WAV"

    def test_flac_24(self, monkeypatch):
        self._fake(monkeypatch, bits_per_sample=24, bitrate=2_500_000)
        assert q.quality_of_file("x.flac") == "FLAC_24"

    def test_mp3_cbr_320(self, monkeypatch):
        from mutagen.mp3 import BitrateMode
        self._fake(monkeypatch, bitrate=320_000, bitrate_mode=BitrateMode.CBR)
        assert q.quality_of_file("x.mp3") == "MP3_320"

    def test_mp3_vbr_v0(self, monkeypatch):
        from mutagen.mp3 import BitrateMode
        self._fake(monkeypatch, bitrate=245_000, bitrate_mode=BitrateMode.VBR)
        assert q.quality_of_file("x.mp3") == "MP3_V0"

    def test_m4a_alac_vs_aac(self, monkeypatch):
        self._fake(monkeypatch, codec="alac", bitrate=900_000)
        assert q.quality_of_file("x.m4a") == "ALAC"
        self._fake(monkeypatch, codec="mp4a.40.2", bitrate=256_000)
        assert q.quality_of_file("x.m4a") == "AAC_256"

    def test_unreadable_is_none(self, tmp_path):
        bad = tmp_path / "bad.flac"
        bad.write_bytes(b"not audio")
        assert q.quality_of_file(str(bad)) is None
        assert q.quality_of_file(str(tmp_path / "missing.flac")) is None


class TestEffectiveQuality:
    def test_suspect_lossless_ranks_as_mp3_320(self):
        assert q.effective_quality("FLAC_16", "suspect") == "MP3_320"
        assert q.effective_quality("WAV", "suspect") == "MP3_320"

    def test_fake_ranks_below_320(self):
        assert q.effective_quality("FLAC_24", "fake") == "MP3_LOW"

    def test_upsampled_24_bit_is_16_bit(self):
        assert q.effective_quality("FLAC_24", "upsampled") == "FLAC_16"
        assert q.effective_quality("FLAC_16", "padded_16") == "FLAC_16"

    def test_ok_and_lossy_untouched(self):
        assert q.effective_quality("FLAC_24", "ok") == "FLAC_24"
        assert q.effective_quality("FLAC_24", None) == "FLAC_24"
        assert q.effective_quality("MP3_320", "suspect") == "MP3_320"
        assert q.effective_quality(None, "suspect") is None
