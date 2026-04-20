"""Unit tests for the slskd search pipeline in soulseek_api.py."""
import os

os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

from soulseek_api import (
    _build_search_queries,
    _effective_mp3_kbps,
    _has_version_marker,
    _infer_bitrate_from_name,
    _is_ascii,
    _normalize_ext_list,
    _version_mismatch,
    clean_filename,
    extract_candidates,
    sort_candidates,
)


class TestNormalizeExtList:
    def test_basic(self):
        assert _normalize_ext_list("flac,mp3") == [".flac", ".mp3"]

    def test_with_dots_and_whitespace(self):
        assert _normalize_ext_list(" .FLAC , mp3 ") == [".flac", ".mp3"]

    def test_dedupes_preserving_order(self):
        assert _normalize_ext_list("mp3,flac,mp3") == [".mp3", ".flac"]


class TestIsAscii:
    def test_ascii(self):
        assert _is_ascii("Hello World")

    def test_cjk(self):
        assert not _is_ascii("群青")

    def test_accented(self):
        assert not _is_ascii("Beyoncé")


class TestBuildSearchQueries:
    def test_title_and_artist(self):
        q = _build_search_queries("Eminem", "Lose Yourself")
        assert q[0] == "lose yourself eminem"
        assert "lose yourself" in q

    def test_album_cleaned_variant(self):
        # Album tokens shared with title produce an album-cleaned variant.
        q = _build_search_queries("Daft Punk", "Giorgio by Moroder", "Random Access Memories")
        assert any("giorgio" in x for x in q)
        # Primary remains
        assert q[0] == "giorgio by moroder daft punk"

    def test_cjk_bypasses_punctuation_strip(self):
        q = _build_search_queries("YOASOBI", "群青", None)
        # Non-ASCII title must appear in raw form (not stripped to empty)
        assert any("群青" in x for x in q)

    def test_empty_inputs(self):
        assert _build_search_queries("", "", None) == []

    def test_dedupes(self):
        # artist == title leads to what could be duplicate; must dedupe
        q = _build_search_queries("abc", "abc", None)
        assert len(q) == len(set(x.lower() for x in q))


class TestVersionMarker:
    def test_detects_remix(self):
        assert _has_version_marker("Song (Walker Remix)") == "remix"

    def test_detects_live(self):
        assert _has_version_marker("Song - Live at the BBC") == "live"

    def test_none_for_plain(self):
        assert _has_version_marker("Song Name") is None

    def test_remix_marker_takes_priority_over_suffix(self):
        assert _has_version_marker("Song Remix Edit") == "remix"


class TestVersionMismatch:
    def test_one_side_remix_other_plain(self):
        assert _version_mismatch("Song", "Song (Remix).mp3")

    def test_both_plain(self):
        assert not _version_mismatch("Song", "Song.mp3")

    def test_both_same_marker(self):
        assert not _version_mismatch("Song (Remix)", "Song (Remix).mp3")

    def test_different_markers(self):
        assert _version_mismatch("Song (Remix)", "Song (Live).mp3")


class TestEffectiveMp3Kbps:
    def test_standard_calc(self):
        # 5 MB / 180 s = ~222 kbps
        assert _effective_mp3_kbps(5_000_000, 180) == 222

    def test_none_on_missing_inputs(self):
        assert _effective_mp3_kbps(None, 180) is None
        assert _effective_mp3_kbps(5_000_000, None) is None
        assert _effective_mp3_kbps(5_000_000, 0) is None


class TestInferBitrateFromName:
    def test_standard_patterns(self):
        assert _infer_bitrate_from_name("Song [320 kbps].mp3") == 320
        assert _infer_bitrate_from_name("Song - 192k.mp3") == 192

    def test_no_match(self):
        assert _infer_bitrate_from_name("Song.mp3") is None

    def test_ignores_year_like_numbers(self):
        # "2020" shouldn't be interpreted as a bitrate.
        assert _infer_bitrate_from_name("Song (2020).mp3") is None


class TestCleanFilename:
    def test_strips_brackets_and_metadata(self):
        assert "flac" not in clean_filename("Song Name [FLAC] (2013).flac")

    def test_normalizes_separators(self):
        out = clean_filename("Artist_Song-Name.mp3")
        assert out == "artist song name"


class TestExtractCandidates:
    def _search_result(self, filename, bitrate=None, size=5_000_000, length=180, upload_speed=10000):
        return [{
            "username": "user1",
            "uploadSpeed": upload_speed,
            "files": [{
                "filename": filename,
                "size": size,
                "bitrate": bitrate,
                "length": length,
            }],
        }]

    def test_accepts_high_quality_match(self):
        results = self._search_result("Eminem - Lose Yourself.flac", bitrate=1411)
        out = extract_candidates(results, "Lose Yourself", "Eminem")
        assert len(out) == 1
        assert out[0]["ext"] == ".flac"

    def test_rejects_low_bitrate_mp3(self):
        results = self._search_result("Eminem - Lose Yourself.mp3", bitrate=192)
        out = extract_candidates(results, "Lose Yourself", "Eminem")
        assert out == []

    def test_rejects_effective_bitrate_too_low(self):
        # size * 8 / length / 1000 = 50 kbps → way below 280
        results = self._search_result(
            "Eminem - Lose Yourself.mp3", bitrate=320, size=1_000_000, length=160
        )
        out = extract_candidates(results, "Lose Yourself", "Eminem")
        assert out == []

    def test_unsupported_extension_rejected(self):
        results = self._search_result("Eminem - Lose Yourself.wma", bitrate=320)
        out = extract_candidates(results, "Lose Yourself", "Eminem")
        assert out == []

    def test_version_mismatch_penalty(self):
        # Searching for the original; candidate is a remix → title score is penalized
        # enough that it falls below the 80 threshold.
        results = self._search_result(
            "Eminem - Lose Yourself (Walker Remix).flac", bitrate=1411
        )
        out = extract_candidates(results, "Lose Yourself", "Eminem")
        assert out == [], "Remix should not match when original is expected"


class TestSortCandidates:
    def test_prefers_flac_over_mp3(self):
        cands = [
            {"ext": ".mp3", "bitrate": 320, "user_upload_speed": 10000},
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 10000},
        ]
        assert sort_candidates(cands)[0]["ext"] == ".flac"

    def test_prefers_higher_bitrate_within_same_format(self):
        cands = [
            {"ext": ".mp3", "bitrate": 320, "user_upload_speed": 10000},
            {"ext": ".mp3", "bitrate": None, "user_upload_speed": 10000},
        ]
        assert sort_candidates(cands)[0]["bitrate"] == 320

    def test_tiebreaks_on_upload_speed(self):
        cands = [
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 1000},
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 50000},
        ]
        assert sort_candidates(cands)[0]["user_upload_speed"] == 50000
