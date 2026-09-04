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
    _version_tier,
    clean_filename,
    extract_candidates,
    sort_candidates,
)
import soulseek_api


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
    def test_year_dropped_from_brackets(self):
        # Year-like 4-digit number is dropped, but the [FLAC] tag is kept
        # as an extra token. token_set_ratio tolerates extra tokens, and
        # *not* keeping parens means we'd lose remix/version info elsewhere.
        out = clean_filename("Song Name [FLAC] (2013).flac")
        assert "2013" not in out
        assert "song" in out
        assert "name" in out

    def test_keeps_remix_info_in_parens(self):
        # The original bug: paren-stripping erased "(Walker & Royce Remix)"
        # so fuzzy matching never saw the remix tokens.
        out = clean_filename("Channel Tres - Controller (Walker & Royce Remix).flac")
        assert "walker" in out
        assert "royce" in out
        assert "remix" in out

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


class TestVersionTier:
    def test_extended_mix(self):
        assert _version_tier("Heart To Find (Extended Mix).flac") == 0

    def test_extended_version(self):
        assert _version_tier("Track - Extended Version.mp3") == 0

    def test_original_mix(self):
        assert _version_tier("Heart To Find - Original Mix.flac") == 1

    def test_original_version(self):
        assert _version_tier("Track (Original Version).flac") == 1

    def test_plain_title(self):
        assert _version_tier("Heart To Find.flac") == 2

    def test_remix_is_not_extended(self):
        # A remix is a different track rewrite, not the DJ extended cut.
        assert _version_tier("Track (Walker Remix).flac") == 2


class TestSortCandidatesVersionTier:
    def test_extended_beats_original(self):
        cands = [
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 0, "version_tier": 1},
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 0, "version_tier": 0},
        ]
        assert sort_candidates(cands)[0]["version_tier"] == 0

    def test_original_beats_normal(self):
        cands = [
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 0, "version_tier": 2},
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 0, "version_tier": 1},
        ]
        assert sort_candidates(cands)[0]["version_tier"] == 1

    def test_extended_outranks_format(self):
        # An MP3 Extended Mix beats a FLAC plain version — version tier wins.
        cands = [
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 0, "version_tier": 2},
            {"ext": ".mp3", "bitrate": 320, "user_upload_speed": 0, "version_tier": 0},
        ]
        out = sort_candidates(cands)
        assert out[0]["version_tier"] == 0
        assert out[0]["ext"] == ".mp3"

    def test_format_tiebreaks_within_same_tier(self):
        cands = [
            {"ext": ".mp3", "bitrate": 320, "user_upload_speed": 0, "version_tier": 0},
            {"ext": ".flac", "bitrate": 1411, "user_upload_speed": 0, "version_tier": 0},
        ]
        assert sort_candidates(cands)[0]["ext"] == ".flac"

    def test_extract_stamps_version_tier(self):
        results = [{
            "username": "u",
            "uploadSpeed": 1000,
            "files": [{
                "filename": "Mat Joe - Heart To Find (Extended Mix).flac",
                "size": 30_000_000,
                "bitrate": 1411,
                "length": 360,
            }],
        }]
        out = extract_candidates(results, "Heart To Find", "Mat Joe")
        assert len(out) == 1
        assert out[0]["version_tier"] == 0


class TestMaxVersionTierFilter:
    """Upgrade-pass uses max_version_tier to demand strictly better versions."""

    def _two_versions_search_result(self):
        """Returns search results containing both an Original Mix and Extended Mix."""
        return [{
            "username": "u",
            "uploadSpeed": 1000,
            "files": [
                {
                    "filename": "Mat Joe - Heart To Find (Original Mix).flac",
                    "size": 30_000_000,
                    "bitrate": 1411,
                    "length": 360,
                },
                {
                    "filename": "Mat Joe - Heart To Find (Extended Mix).flac",
                    "size": 50_000_000,
                    "bitrate": 1411,
                    "length": 480,
                },
            ],
        }]

    def _attempted_filenames(self, monkeypatch, tmp_path, max_version_tier):
        """Run the real download_and_verify and report what it tried to fetch.

        These tests used to re-implement the tier predicate inline, so they
        passed whether or not download_and_verify actually filtered. Driving
        the real function means deleting the filter fails the test.
        """
        import db as _db

        enqueued = []

        class _Transfers:
            def enqueue(self, username, files):
                enqueued.append(files[0]["filename"])

        class _Client:
            transfers = _Transfers()

        monkeypatch.setattr(soulseek_api, "get_client", lambda: _Client())
        # Every attempt "fails" so the loop walks all permitted candidates.
        monkeypatch.setattr(soulseek_api, "wait_for_completion", lambda c: None)

        conn = _db.create_connection(str(tmp_path / "t.db"))
        _db.create_table(conn, "pl_x")
        _db.insert_track(conn, "pl_x", ("t1", "Heart To Find", "Mat Joe", "Alb"))
        try:
            soulseek_api.download_and_verify(
                search_results=self._two_versions_search_result(),
                expected_title="Heart To Find",
                expected_artist="Mat Joe",
                conn=conn,
                playlist_name="pl_x",
                track_id="t1",
                max_version_tier=max_version_tier,
            )
        finally:
            conn.close()
        return enqueued

    def test_both_tiers_are_recognised(self):
        cands = extract_candidates(
            self._two_versions_search_result(), "Heart To Find", "Mat Joe"
        )
        assert {c["version_tier"] for c in cands} == {0, 1}

    def test_only_strictly_better_tier_is_downloaded(self, monkeypatch, tmp_path):
        # Currently holding Original Mix (tier 1): only Extended Mix qualifies.
        attempted = self._attempted_filenames(monkeypatch, tmp_path, max_version_tier=1)
        assert len(attempted) == 1
        assert "Extended Mix" in attempted[0]

    def test_equal_tier_is_not_downloaded(self, monkeypatch, tmp_path):
        # Already on Extended Mix (tier 0): nothing is strictly better.
        assert self._attempted_filenames(monkeypatch, tmp_path, max_version_tier=0) == []

    def test_without_the_cap_both_versions_are_eligible(self, monkeypatch, tmp_path):
        attempted = self._attempted_filenames(
            monkeypatch, tmp_path, max_version_tier=None
        )
        assert len(attempted) == 2, "a normal download should consider both versions"
        # Extended Mix outranks Original Mix, so it is tried first.
        assert "Extended Mix" in attempted[0]


class TestRunOneSearch:
    """_run_one_search is the per-query primitive used by the waterfall."""

    def test_returns_responses_when_search_completes(self, monkeypatch):
        fake = type("F", (), {})()
        fake.searches = type("S", (), {})()
        fake.searches.search_text = lambda **kw: {"id": "sid"}
        fake.searches.state = lambda sid: {"state": "Completed"}
        fake.searches.search_responses = lambda sid: [{"username": "u", "files": []}]
        fake.searches.delete = lambda sid: None
        monkeypatch.setattr(soulseek_api, "get_client", lambda: fake)
        out = soulseek_api._run_one_search("hello", timeout=2)
        assert out and out[0]["username"] == "u"

    def test_returns_empty_on_no_search_id(self, monkeypatch):
        fake = type("F", (), {})()
        fake.searches = type("S", (), {})()
        fake.searches.search_text = lambda **kw: {}
        fake.searches.delete = lambda sid: None
        monkeypatch.setattr(soulseek_api, "get_client", lambda: fake)
        assert soulseek_api._run_one_search("hello", timeout=2) == []

    def test_returns_empty_on_timeout(self, monkeypatch):
        fake = type("F", (), {})()
        fake.searches = type("S", (), {})()
        fake.searches.search_text = lambda **kw: {"id": "sid"}
        fake.searches.state = lambda sid: {"state": "InProgress"}
        fake.searches.delete = lambda sid: None
        monkeypatch.setattr(soulseek_api, "get_client", lambda: fake)
        # Skip the real sleep so the test runs fast.
        monkeypatch.setattr(soulseek_api, "_interruptible_sleep", lambda s: False)
        assert soulseek_api._run_one_search("hello", timeout=0) == []


class TestSearchAndDownloadFallthrough:
    """search_and_download falls through to the next query when extract_candidates
    yields zero usable candidates — not just on zero raw responses."""

    def test_falls_through_to_simpler_query_when_filtering_empty(self, monkeypatch):
        # First query: 1 raw response, but the candidate is unsupported (.wma) so
        # extract_candidates returns []. Must fall through to next query.
        # Second query: a clean .flac match.
        calls = []

        def fake_run_one_search(query, timeout=60):
            calls.append(query)
            if len(calls) == 1:
                return [{
                    "username": "u1",
                    "uploadSpeed": 1000,
                    "files": [{
                        "filename": "junk.wma",
                        "size": 1_000_000,
                        "bitrate": 320,
                        "length": 180,
                    }],
                }]
            return [{
                "username": "u2",
                "uploadSpeed": 1000,
                "files": [{
                    "filename": "Mat Joe - Heart To Find.flac",
                    "size": 30_000_000,
                    "bitrate": 1411,
                    "length": 360,
                }],
            }]

        captured = {}

        def fake_download_and_verify(**kw):
            captured.update(kw)
            cands = extract_candidates(
                kw["search_results"], kw["expected_title"], kw["expected_artist"]
            )
            return "/downloads/Mat Joe - Heart To Find.flac" if cands else None

        monkeypatch.setattr(soulseek_api, "_run_one_search", fake_run_one_search)
        monkeypatch.setattr(soulseek_api, "download_and_verify", fake_download_and_verify)

        out = soulseek_api.search_and_download(
            artist="Mat Joe",
            title="Heart To Find",
            conn=None,
            playlist_name="pl_x",
            track_id="t1",
        )
        assert out == "/downloads/Mat Joe - Heart To Find.flac"
        # Confirm the waterfall actually advanced past the first query.
        assert len(calls) >= 2

    def test_returns_none_when_all_queries_exhausted(self, monkeypatch):
        monkeypatch.setattr(soulseek_api, "_run_one_search", lambda q, timeout=60: [])
        monkeypatch.setattr(
            soulseek_api, "download_and_verify", lambda **kw: None
        )
        out = soulseek_api.search_and_download(
            artist="X", title="Y", conn=None, playlist_name="pl", track_id="t",
        )
        assert out is None
