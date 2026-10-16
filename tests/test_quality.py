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


class TestProfile:
    def test_rank_is_the_allowed_order_then_disallowed(self):
        p, err = q.make_profile(["FLAC_16", "MP3_320"], "MP3_320", True)
        assert err is None
        assert p.rank("FLAC_16") == 0 and p.rank("MP3_320") == 1
        assert p.rank("FLAC_24") > p.rank("MP3_320")  # disallowed: after
        assert p.below_cutoff("FLAC_24")  # a disallowed file never meets it
        assert not p.below_cutoff("FLAC_16") and not p.below_cutoff("MP3_320")

    @pytest.mark.parametrize("allowed,cutoff,error", [
        ([], "FLAC_16", "allow at least one"),
        (["FLAC_16"], "MP3_320", "cutoff must be one of the allowed"),
        (["FLAC_16", "NOPE"], "FLAC_16", "unknown quality: NOPE"),
        (["FLAC_16", "FLAC_16"], "FLAC_16", "listed twice"),
    ])
    def test_make_profile_validates(self, allowed, cutoff, error):
        p, err = q.make_profile(allowed, cutoff, False)
        assert p is None and error in err

    def test_json_round_trip(self):
        p = q.QualityProfile(("FLAC_24", "FLAC_16"), "FLAC_16", True)
        assert q.parse_profile(p.to_json()) == (p, None)
        assert q.parse_profile("{nope")[0] is None
        assert q.parse_profile('{"allowed": "FLAC_16", "cutoff": "FLAC_16"}')[0] is None


class TestProfileFromFormats:
    def test_prod_flac_only(self):
        # Production runs SLSKD_PREFERRED_FORMATS=flac.
        p = q.profile_from_formats([".flac"])
        assert p == q.QualityProfile(("FLAC_24", "FLAC_16"), "FLAC_16", False)
        # Every FLAC already meets the cutoff: no upgrade storm.
        assert not p.below_cutoff("FLAC_16") and not p.below_cutoff("FLAC_24")

    def test_default_formats_keep_their_order(self):
        p = q.profile_from_formats([".flac", ".mp3", ".aiff", ".wav"])
        assert p.allowed == ("FLAC_24", "FLAC_16", "MP3_320", "AIFF", "WAV")
        assert p.cutoff == "WAV" and p.upgrades_allowed is False

    def test_m4a_and_ogg(self):
        p = q.profile_from_formats([".m4a", ".ogg"])
        assert p.allowed == ("ALAC", "AAC_256", "AAC_LOW", "OGG")

    @pytest.mark.parametrize("formats", [
        [".flac"], [".flac", ".mp3", ".aiff", ".wav"], [".mp3"], [".wav", ".flac"],
    ])
    def test_accepts_exactly_what_the_format_allowlist_accepted(self, formats, monkeypatch):
        """The migrated profile + the fixed floor accept the same files as
        the old `ext in PREFERRED_FORMATS` + floor did."""
        from spotify_slsk import config
        from spotify_slsk.matching.candidates import evaluate_candidates

        files = [
            {"filename": "Eminem - Lose Yourself.flac", "size": 30_000_000, "length": 300,
             "bitDepth": 24, "sampleRate": 96000},
            {"filename": "Eminem - Lose Yourself.flac", "size": 30_000_000, "length": 300,
             "bitDepth": 16, "sampleRate": 44100},
            {"filename": "Eminem - Lose Yourself.mp3", "size": 12_000_000, "length": 300,
             "bitRate": 320},
            {"filename": "Eminem - Lose Yourself.mp3", "size": 12_000_000, "length": 300},
            {"filename": "Eminem - Lose Yourself.mp3", "size": 9_000_000, "length": 300,
             "bitRate": 245, "isVariableBitRate": True},
            {"filename": "Eminem - Lose Yourself.mp3", "size": 9_000_000, "length": 300,
             "bitRate": 256},
            {"filename": "Eminem - Lose Yourself.wav", "size": 52_000_000, "length": 300},
            {"filename": "Eminem - Lose Yourself.aiff", "size": 52_000_000, "length": 300},
            {"filename": "Eminem - Lose Yourself.wma", "size": 9_000_000, "length": 300},
        ]
        monkeypatch.setattr(config, "QUALITY_PROFILE", q.profile_from_formats(formats))
        out = evaluate_candidates([{"username": "u", "files": files}], "Lose Yourself", "Eminem")
        old_floor_ok = [
            not (f["filename"].endswith(".mp3") and (f.get("bitRate") or 320) < 320)
            for f in files
        ]
        expected = [
            ("." + f["filename"].rsplit(".", 1)[1]) in formats and ok
            for f, ok in zip(files, old_floor_ok, strict=True)
        ]
        assert [c["accepted"] for c in out] == expected


@pytest.fixture
def restore_config(monkeypatch):
    """config.reload rebinds every tunable; put them all back afterwards."""
    from spotify_slsk import config

    for name in dir(config):
        if name.isupper():
            monkeypatch.setattr(config, name, getattr(config, name))


@pytest.mark.usefixtures("restore_config")
class TestEnsureQualityProfile:
    def _v6_db(self, path, formats=None):
        import sqlite3

        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)")
        if formats is not None:
            raw.execute("INSERT INTO settings VALUES ('SLSKD_PREFERRED_FORMATS', ?, NULL)", (formats,))
        raw.execute("PRAGMA user_version = 6")
        raw.commit()
        raw.close()

    def test_v6_db_with_flac_override_is_migrated(self, tmp_path, monkeypatch):
        from spotify_slsk import config, db

        monkeypatch.delenv("SLSKD_PREFERRED_FORMATS", raising=False)
        path = str(tmp_path / "v6.db")
        self._v6_db(path, "flac")
        conn = db.create_connection(path)
        config.ensure_quality_profile(conn)
        stored = db.list_settings(conn)
        assert "SLSKD_PREFERRED_FORMATS" not in stored
        profile, _ = q.parse_profile(stored["QUALITY_PROFILE"])
        assert profile == q.QualityProfile(("FLAC_24", "FLAC_16"), "FLAC_16", False)
        config.reload(conn)
        assert config.QUALITY_PROFILE == profile
        conn.close()

    def test_env_formats_seed_the_profile(self, tmp_path, monkeypatch):
        from spotify_slsk import config, db

        monkeypatch.setenv("SLSKD_PREFERRED_FORMATS", "mp3,flac")
        path = str(tmp_path / "v6.db")
        self._v6_db(path)
        conn = db.create_connection(path)
        config.ensure_quality_profile(conn)
        profile, _ = q.parse_profile(db.get_setting(conn, "QUALITY_PROFILE"))
        assert profile.allowed == ("MP3_320", "FLAC_24", "FLAC_16")
        conn.close()

    def test_an_existing_profile_is_never_overwritten(self, tmp_path, monkeypatch):
        from spotify_slsk import config, db

        monkeypatch.setenv("SLSKD_PREFERRED_FORMATS", "mp3")
        conn = db.create_connection(str(tmp_path / "x.db"))
        mine = q.QualityProfile(("FLAC_24",), "FLAC_24", True)
        db.set_setting(conn, "QUALITY_PROFILE", mine.to_json())
        config.ensure_quality_profile(conn)
        assert db.get_setting(conn, "QUALITY_PROFILE") == mine.to_json()
        conn.close()

    def test_bad_stored_profile_keeps_the_previous(self, tmp_path, monkeypatch):
        from spotify_slsk import config, db

        before = q.QualityProfile(("FLAC_16",), "FLAC_16", False)
        monkeypatch.setattr(config, "QUALITY_PROFILE", before)
        conn = db.create_connection(str(tmp_path / "x.db"))
        db.set_setting(conn, "QUALITY_PROFILE", '{"allowed": [], "cutoff": "x"}')
        config.reload(conn)
        assert config.QUALITY_PROFILE == before
        conn.close()
