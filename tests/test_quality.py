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

    @pytest.mark.parametrize("raw", [
        '{"allowed": [], "cutoff": "x"}',
        '{"allowed": [["FLAC_16"]], "cutoff": "FLAC_16"}',
        '{"allowed": [{"a": 1}], "cutoff": {"b": 2}}',
        '{"allowed": ["FLAC_16"], "cutoff": ["FLAC_16"]}',
        '{"allowed": [1, null], "cutoff": "FLAC_16"}',
        '[1, 2]',
    ])
    def test_bad_stored_profile_falls_back_to_the_formats(self, tmp_path, monkeypatch, raw, caplog):
        from spotify_slsk import config, db

        monkeypatch.delenv("SLSKD_PREFERRED_FORMATS", raising=False)
        conn = db.create_connection(str(tmp_path / "x.db"))
        db.set_setting(conn, "SLSKD_PREFERRED_FORMATS", "flac")
        db.set_setting(conn, "QUALITY_PROFILE", raw)
        config.reload(conn)  # must not raise
        assert config.QUALITY_PROFILE == q.profile_from_formats([".flac"])
        assert "Invalid QUALITY_PROFILE" in caplog.text
        conn.close()


class TestCandidatesCarryQuality:
    def test_evaluated_candidates_are_stamped(self, monkeypatch):
        from spotify_slsk import config
        from spotify_slsk.matching.candidates import evaluate_candidates

        monkeypatch.setattr(config, "QUALITY_PROFILE", q.profile_from_formats([".flac"]))
        files = [
            {"filename": "Eminem - Lose Yourself.flac", "size": 75_000_000, "length": 300,
             "bitDepth": 24, "sampleRate": 96000},
            {"filename": "Eminem - Lose Yourself.mp3", "size": 9_600_000, "length": 300,
             "bitRate": 256},
        ]
        hi, mp3 = evaluate_candidates([{"username": "u", "files": files}], "Lose Yourself", "Eminem")
        assert (hi["quality"], hi["quality_label"], hi["accepted"]) == ("FLAC_24", "FLAC 24-bit", True)
        assert mp3["quality"] == "MP3_LOW"
        assert mp3["reason"] == "quality not allowed by profile: MP3 256"

    def test_a_flac_only_profile_names_mp3_320(self, monkeypatch):
        from spotify_slsk import config
        from spotify_slsk.matching.candidates import evaluate_candidates

        monkeypatch.setattr(config, "QUALITY_PROFILE", q.profile_from_formats([".flac"]))
        files = [{"filename": "Eminem - Lose Yourself.mp3", "size": 12_000_000, "length": 300,
                  "bitRate": 320}]
        (c,) = evaluate_candidates([{"username": "u", "files": files}], "Lose Yourself", "Eminem")
        assert c["reason"] == "quality not allowed by profile: MP3 320"


class TestRanking:
    """sort_candidates over slskd-shaped responses (camelCase, per-peer
    slot/queue/speed), as the daemon sees them."""

    MB = 1024 * 1024

    def _peer(self, user, filename, *, speed=MB, slot=True, queue=0, **f):
        return {
            "username": user, "uploadSpeed": speed, "hasFreeUploadSlot": slot,
            "queueLength": queue,
            "files": [{"filename": filename, "length": 300, **f}],
        }

    def _flac(self, user, depth=16, size=33_750_000, name="Daft Punk - Get Lucky.flac", **peer):
        # 33.75 MB / 300 s = 900 kbps: FLAC 16-bit's preferred bitrate.
        return self._peer(user, f"Music\\Daft Punk\\{name}", size=size,
                          bitDepth=depth, sampleRate=44100 if depth == 16 else 96000, **peer)

    def _mp3(self, user, name="Daft Punk - Get Lucky.mp3", **peer):
        return self._peer(user, f"@@x\\{name}", size=12_000_000, bitRate=320, **peer)

    def _order(self, monkeypatch, *responses, allowed=("FLAC_24", "FLAC_16", "MP3_320")):
        from spotify_slsk import config
        from spotify_slsk.matching.candidates import (
            evaluate_candidates,
            sort_candidates,
        )

        monkeypatch.setattr(config, "QUALITY_PROFILE",
                            q.make_profile(list(allowed), allowed[-1], False)[0])
        out = evaluate_candidates(list(responses), "Get Lucky", "Daft Punk")
        assert all(c["accepted"] for c in out), [c["reason"] for c in out]
        return [c["user"] for c in sort_candidates(out)]

    def test_version_tier_beats_quality(self, monkeypatch):
        assert self._order(
            monkeypatch,
            self._flac("flac24", depth=24, size=90_000_000),
            self._mp3("extended_mp3", name="Daft Punk - Get Lucky (Extended Mix).mp3"),
        ) == ["extended_mp3", "flac24"]

    def test_profile_order_is_the_quality_ranking(self, monkeypatch):
        args = (self._mp3("mp3"), self._flac("flac16"))
        assert self._order(monkeypatch, *args) == ["flac16", "mp3"]
        # A profile that prefers MP3 ranks it first.
        assert self._order(monkeypatch, *args, allowed=("MP3_320", "FLAC_16")) == ["mp3", "flac16"]

    def test_quality_beats_availability(self, monkeypatch):
        assert self._order(
            monkeypatch,
            self._flac("cd_fast", speed=50 * self.MB),
            self._flac("hires_slow", depth=24, size=90_000_000, slot=False, queue=40,
                       speed=100_000),
        ) == ["hires_slow", "cd_fast"]

    def test_free_slot_beats_queue_and_speed(self, monkeypatch):
        assert self._order(
            monkeypatch,
            self._flac("busy_fast", slot=False, queue=0, speed=50 * self.MB),
            self._flac("free_slow", slot=True, queue=3, speed=200_000),
        ) == ["free_slow", "busy_fast"]

    def test_shorter_queue_beats_speed(self, monkeypatch):
        assert self._order(
            monkeypatch,
            self._flac("long_queue_fast", slot=False, queue=30, speed=50 * self.MB),
            self._flac("short_queue_slow", slot=False, queue=2, speed=200_000),
        ) == ["short_queue_slow", "long_queue_fast"]

    def test_faster_peer_wins_when_queues_tie(self, monkeypatch):
        # Queues 5 and 6 share a log2 bucket; speed decides.
        assert self._order(
            monkeypatch,
            self._flac("slow", slot=False, queue=5, speed=300_000),
            self._flac("fast", slot=False, queue=6, speed=8 * self.MB),
        ) == ["fast", "slow"]

    def test_size_closest_to_preferred_breaks_a_speed_tie(self, monkeypatch):
        # 10.1 vs 10.4 MB/s share a bucket; 900 kbps beats 1400 kbps for FLAC 16.
        assert self._order(
            monkeypatch,
            self._flac("fat", size=52_500_000, speed=int(10.4 * self.MB)),
            self._flac("typical", speed=int(10.1 * self.MB)),
        ) == ["typical", "fat"]

    def test_multi_peer_fixture_order(self, monkeypatch):
        import json
        import pathlib

        from spotify_slsk import config
        from spotify_slsk.matching.candidates import (
            evaluate_candidates,
            sort_candidates,
        )

        fx = json.loads((pathlib.Path(__file__).parent / "fixtures/slskd/"
                         "search_responses_multi_peer.json").read_text())
        monkeypatch.setattr(config, "QUALITY_PROFILE",
                            q.profile_from_formats([".flac", ".mp3", ".aiff", ".wav"]))
        t = fx["track"]
        out = evaluate_candidates(fx["responses"], t["title"], t["artist"],
                                  expected_duration_ms=t["duration_ms"])
        ranked = sort_candidates([c for c in out if c["accepted"]])
        users = [c["user"] for c in ranked]
        # lossless_peer's "24-bit/88.2 kHz" is 1022 kbps: likely upsampled, so
        # it ranks as 16-bit and after the plain 16-bit FLAC; MP3 320 last.
        assert users.index("sparse_peer") < users.index("lossless_peer") < users.index("cbr_peer")
        assert [c["quality"] for c in ranked] == sorted(
            (c["quality"] for c in ranked), key=config.QUALITY_PROFILE.rank)


class TestReleaseTerms:
    def test_parse_plain_and_regex(self):
        from spotify_slsk.config import parse_terms

        terms, err = parse_terms(" Karaoke , /\\bremix{1,2}\\b/ ,, sped up")
        assert err is None
        assert [label for label, _ in terms] == ["Karaoke", "/\\bremix{1,2}\\b/", "sped up"]
        assert terms[0][1].search("Song (KARAOKE Version)")
        assert terms[1][1].search("song remix") and not terms[1][1].search("remixes")

    def test_invalid_regex_is_an_error(self):
        from spotify_slsk.config import parse_terms, validate_setting

        assert parse_terms("/(unclosed/")[0] is None
        value, err = validate_setting("terms", "karaoke, /(unclosed/")
        assert value is None and "invalid regex /(unclosed/" in err

    def test_validate_normalises(self):
        from spotify_slsk.config import validate_setting

        assert validate_setting("terms", " karaoke ,  /a,b/ ") == ("karaoke, /a,b/", None)

    def _eval(self, monkeypatch, filename, required="", ignored=""):
        from spotify_slsk import config
        from spotify_slsk.matching.candidates import evaluate_candidates

        monkeypatch.setattr(config, "QUALITY_PROFILE", q.profile_from_formats([".flac"]))
        monkeypatch.setattr(config, "REQUIRED_TERMS", config.parse_terms(required)[0])
        monkeypatch.setattr(config, "IGNORED_TERMS", config.parse_terms(ignored)[0])
        files = [{"filename": filename, "size": 33_000_000, "length": 300, "bitDepth": 16}]
        (c,) = evaluate_candidates([{"username": "u", "files": files}], "Lose Yourself", "Eminem")
        return c

    def test_no_terms_change_nothing(self, monkeypatch):
        assert self._eval(monkeypatch, "Eminem - Lose Yourself.flac")["accepted"] is True

    def test_ignored_term_on_the_full_peer_path(self, monkeypatch):
        c = self._eval(monkeypatch, "Karaoke Hits\\Eminem - Lose Yourself.flac",
                       ignored="nightcore, karaoke")
        assert c["reason"] == "ignored term: karaoke"

    def test_ignored_regex(self, monkeypatch):
        c = self._eval(monkeypatch, "@@x\\Eminem - Lose Yourself [WEB].flac",
                       ignored="/\\[web\\]/")
        assert c["reason"] == "ignored term: /\\[web\\]/"

    def test_required_terms(self, monkeypatch):
        c = self._eval(monkeypatch, "Eminem - Lose Yourself.flac", required="vinyl, /\\bcd\\b/")
        assert c["reason"] == "missing required term (one of: vinyl, /\\bcd\\b/)"
        ok = self._eval(monkeypatch, "Eminem\\8 Mile (CD)\\Eminem - Lose Yourself.flac",
                        required="vinyl, /\\bcd\\b/")
        assert ok["accepted"] is True


def test_make_profile_never_raises_on_odd_types():
    for allowed, cutoff in ([["x"]], "x"), ([{}], "x"), (["FLAC_16"], ["FLAC_16"]), ("FLAC_16", "FLAC_16"):
        p, err = q.make_profile(allowed, cutoff, False)
        assert p is None and err


class TestTermPaths:
    def test_plain_term_with_a_slash_matches_backslash_paths(self):
        from spotify_slsk.config import first_term, parse_terms

        terms = parse_terms("karaoke hits/")[0]
        assert first_term(terms, "Music\\Karaoke Hits\\Eminem - Lose Yourself.flac") == "karaoke hits/"

    def test_regex_sees_both_forms(self):
        from spotify_slsk.config import first_term, parse_terms

        path = "Music\\Bootlegs\\Song.flac"
        assert first_term(parse_terms("/bootlegs\\/song/")[0], path)  # normalised
        assert first_term(parse_terms("/bootlegs\\\\song/")[0], path)  # raw backslash

    def test_invalid_env_regex_is_logged(self, monkeypatch, caplog):
        from spotify_slsk import config

        monkeypatch.setenv("IGNORED_TERMS", "karaoke, /(oops/")
        assert config._env_terms("IGNORED_TERMS") == []
        assert "Invalid IGNORED_TERMS in the environment" in caplog.text


def test_unknown_queue_and_slot_rank_after_known(monkeypatch):
    from spotify_slsk import config
    from spotify_slsk.matching.candidates import sort_candidates

    monkeypatch.setattr(config, "QUALITY_PROFILE", q.profile_from_formats([".flac"]))
    base = {"ext": ".flac", "bit_depth": 16, "version_tier": 2, "user_upload_speed": 1_000_000}
    cands = [
        dict(base, user="unknown_queue", free_slot=False, queue_length=None),
        dict(base, user="long_queue", free_slot=False, queue_length=500),
        dict(base, user="unknown_slot", free_slot=None, queue_length=0),
        dict(base, user="free", free_slot=True, queue_length=None),
    ]
    assert [c["user"] for c in sort_candidates(cands)] == [
        "free", "unknown_slot", "long_queue", "unknown_queue",
    ]
