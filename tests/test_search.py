"""Unit tests for the slskd search pipeline (slskd/ and matching/candidates)."""
import os

import pytest

os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

from spotify_slsk import config
from spotify_slsk.config import normalize_ext_list
from spotify_slsk.matching import candidates as matching_candidates
from spotify_slsk.matching.candidates import (
    _effective_mp3_kbps,
    _infer_bitrate_from_name,
    clean_filename,
    extract_candidates,
    sort_candidates,
)
from spotify_slsk.matching.normalize import (
    has_version_marker,
    version_mismatch,
    version_tier,
)
from spotify_slsk.slskd import client as slskd_client
from spotify_slsk.slskd import models as slskd_models
from spotify_slsk.slskd import search as slskd_search
from spotify_slsk.slskd import transfers as slskd_transfers
from spotify_slsk.slskd.search import (
    _build_search_queries,
    _is_ascii,
    _normalize_query_text,
    download_and_verify,
)


class TestNormalizeExtList:
    def test_basic(self):
        assert normalize_ext_list("flac,mp3") == [".flac", ".mp3"]

    def test_with_dots_and_whitespace(self):
        assert normalize_ext_list(" .FLAC , mp3 ") == [".flac", ".mp3"]

    def test_dedupes_preserving_order(self):
        assert normalize_ext_list("mp3,flac,mp3") == [".mp3", ".flac"]


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
        assert has_version_marker("Song (Walker Remix)") == "remix"

    def test_detects_live(self):
        assert has_version_marker("Song - Live at the BBC") == "live"

    def test_none_for_plain(self):
        assert has_version_marker("Song Name") is None

    def test_remix_marker_takes_priority_over_suffix(self):
        assert has_version_marker("Song Remix Edit") == "remix"


class TestVersionMismatch:
    def test_one_side_remix_other_plain(self):
        assert version_mismatch("Song", "Song (Remix).mp3")

    def test_both_plain(self):
        assert not version_mismatch("Song", "Song.mp3")

    def test_both_same_marker(self):
        assert not version_mismatch("Song (Remix)", "Song (Remix).mp3")

    def test_different_markers(self):
        assert version_mismatch("Song (Remix)", "Song (Live).mp3")


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
                "bitRate": bitrate,
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
        assert version_tier("Heart To Find (Extended Mix).flac") == 0

    def test_extended_version(self):
        assert version_tier("Track - Extended Version.mp3") == 0

    def test_original_mix(self):
        assert version_tier("Heart To Find - Original Mix.flac") == 1

    def test_original_version(self):
        assert version_tier("Track (Original Version).flac") == 1

    def test_plain_title(self):
        assert version_tier("Heart To Find.flac") == 2

    def test_remix_is_not_extended(self):
        # A remix is a different track rewrite, not the DJ extended cut.
        assert version_tier("Track (Walker Remix).flac") == 2


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
                "bitDepth": 16, "sampleRate": 44100,
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
                    "bitDepth": 16, "sampleRate": 44100,
                    "length": 360,
                },
                {
                    "filename": "Mat Joe - Heart To Find (Extended Mix).flac",
                    "size": 50_000_000,
                    "bitDepth": 16, "sampleRate": 44100,
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
        from spotify_slsk import db as _db

        enqueued = []

        class _Transfers:
            def enqueue(self, username, files):
                enqueued.append(files[0]["filename"])

        class _Client:
            transfers = _Transfers()

        monkeypatch.setattr(slskd_client, "get_client", lambda: _Client())
        # Every attempt "fails" so the loop walks all permitted candidates.
        monkeypatch.setattr(slskd_transfers, "wait_for_completion", lambda c: None)

        conn = _db.create_connection(str(tmp_path / "t.db"))
        _db.create_table(conn, "pl_x")
        _db.insert_track(conn, "pl_x", ("t1", "Heart To Find", "Mat Joe", "Alb"))
        try:
            slskd_search.download_and_verify(
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
        monkeypatch.setattr(slskd_client, "get_client", lambda: fake)
        out = slskd_search._run_one_search("hello", timeout=2)
        assert out and out[0].username == "u"

    def test_returns_empty_on_no_search_id(self, monkeypatch):
        fake = type("F", (), {})()
        fake.searches = type("S", (), {})()
        fake.searches.search_text = lambda **kw: {}
        fake.searches.delete = lambda sid: None
        monkeypatch.setattr(slskd_client, "get_client", lambda: fake)
        assert slskd_search._run_one_search("hello", timeout=2) == []

    def test_returns_empty_on_timeout(self, monkeypatch):
        fake = type("F", (), {})()
        fake.searches = type("S", (), {})()
        fake.searches.search_text = lambda **kw: {"id": "sid"}
        fake.searches.state = lambda sid: {"state": "InProgress"}
        fake.searches.delete = lambda sid: None
        monkeypatch.setattr(slskd_client, "get_client", lambda: fake)
        # Skip the real sleep so the test runs fast.
        monkeypatch.setattr(slskd_client, "interruptible_sleep", lambda s: False)
        assert slskd_search._run_one_search("hello", timeout=0) == []


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
                        "bitRate": 320,
                        "length": 180,
                    }],
                }]
            return [{
                "username": "u2",
                "uploadSpeed": 1000,
                "files": [{
                    "filename": "Mat Joe - Heart To Find.flac",
                    "size": 30_000_000,
                    "bitDepth": 16, "sampleRate": 44100,
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

        monkeypatch.setattr(slskd_search, "_run_one_search", fake_run_one_search)
        monkeypatch.setattr(slskd_search, "download_and_verify", fake_download_and_verify)

        out = slskd_search.search_and_download(
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
        monkeypatch.setattr(slskd_search, "_run_one_search", lambda q, timeout=60: [])
        monkeypatch.setattr(
            slskd_search, "download_and_verify", lambda **kw: None
        )
        out = slskd_search.search_and_download(
            artist="X", title="Y", conn=None, playlist_name="pl", track_id="t",
        )
        assert out is None


class TestNormalizeQueryText:
    """Hyphens, underscores, and slashes must become spaces — not be deleted."""

    def test_hyphenated_compound_keeps_word_boundary(self):
        # "Radio-Edit" must become "radio edit", NOT "radioedit", because
        # slskd peers tag files as "(Radio Edit)" with a space.
        assert _normalize_query_text("Radio-Edit") == "radio edit"

    def test_full_title_with_hyphen_separator(self):
        # Reproduces the prod failure: "Mind Twister - Radio-Edit" used to
        # collapse to "mind twister  radioedit" (double space, joined word).
        out = _normalize_query_text("Mind Twister - Radio-Edit")
        assert out == "mind twister radio edit"

    def test_strips_apostrophes_keeps_letters(self):
        assert _normalize_query_text("Don't Stop") == "dont stop"

    def test_underscores_become_spaces(self):
        assert _normalize_query_text("foo_bar_baz") == "foo bar baz"

    def test_collapses_runs_of_whitespace(self):
        assert _normalize_query_text("  a   b  ") == "a b"

    def test_empty_input(self):
        assert _normalize_query_text("") == ""
        assert _normalize_query_text(None) == ""


class TestBuildSearchQueriesHyphens:
    """Regression: hyphenated titles used to lose the word boundary."""

    def test_radio_edit_hyphen_produces_spaced_tokens(self):
        queries = _build_search_queries("Kolter", "Mind Twister - Radio-Edit")
        # No query may contain "radioedit" as a single token.
        for q in queries:
            tokens = q.split()
            assert "radioedit" not in tokens, f"hyphen collapse in query: {q!r}"
        # The shorter title-only fallback should be cleanly tokenized.
        assert "mind twister radio edit" in queries

    def test_no_double_spaces_in_any_query(self):
        queries = _build_search_queries("Kolter", "Mind Twister - Radio-Edit")
        for q in queries:
            assert "  " not in q, f"double space leaked: {q!r}"


class TestDownloadAndVerifyTransientErrors:
    """Exceptions from slskd_api shouldn't permanently blacklist a candidate.

    add_tried_file is permanent until the track succeeds. A single JSON
    decode error against an empty slskd response would otherwise ban a
    perfectly good peer/file forever.
    """

    def _setup(self, monkeypatch):
        # One usable candidate; everything passes filtering.
        results = [{
            "username": "u",
            "uploadSpeed": 1000,
            "files": [{
                "filename": "Artist - Track.flac",
                "size": 30_000_000,
                "bitDepth": 16, "sampleRate": 44100,
                "length": 360,
            }],
        }]
        # Stub get_client so transfers.enqueue raises (simulating the prod
        # JSON decode error we saw against slskd).
        class FakeClient:
            class transfers:
                @staticmethod
                def enqueue(**kw):
                    raise ValueError("Expecting value: line 1 column 1 (char 0)")
        monkeypatch.setattr(slskd_client, "get_client", lambda: FakeClient())

        added = []
        captured_tried = {"v": []}

        def fake_get_tried_files(conn, pl, tid):
            return list(captured_tried["v"])

        def fake_add_tried_file(conn, pl, tid, fname):
            added.append(fname)
            captured_tried["v"].append(fname)

        monkeypatch.setattr(slskd_search, "get_tried_files", fake_get_tried_files)
        monkeypatch.setattr(slskd_transfers, "add_tried_file", fake_add_tried_file)
        return results, added

    def test_transient_api_error_does_not_blacklist(self, monkeypatch):
        results, added = self._setup(monkeypatch)
        out = download_and_verify(
            search_results=results,
            expected_title="Track",
            expected_artist="Artist",
            conn=None,
            playlist_name="pl",
            track_id="t1",
        )
        assert out is None  # download didn't succeed
        assert added == [], (
            "transient slskd error should NOT add candidate to tried_files; "
            "got: " + repr(added)
        )


class TestRejectionReasons:
    """evaluate_candidates explains every rejected file; accepted ones are
    exactly what extract_candidates returns."""

    def _results(self, *files, user="u1"):
        return [{"username": user, "uploadSpeed": 1000, "files": list(files)}]

    def _file(self, filename, bitrate=None, size=30_000_000, length=360):
        # slskd's real field is camelCase "bitRate" (see TestRealSlskdShape).
        return {"filename": filename, "size": size, "bitRate": bitrate, "length": length}

    def _reason(self, file, title="Lose Yourself", artist="Eminem"):
        out = matching_candidates.evaluate_candidates(self._results(file), title, artist)
        assert len(out) == 1
        return out[0]["reason"]

    def test_format_reason(self):
        assert self._reason(self._file("Eminem - Lose Yourself.wma")) == "format not allowed: .wma"

    def test_reported_bitrate_reason(self):
        assert self._reason(
            self._file("Eminem - Lose Yourself.mp3", bitrate=192, size=5_000_000, length=200)
        ) == "bitrate 192 < 320"

    def test_effective_bitrate_reason(self):
        reason = self._reason(
            self._file("Eminem - Lose Yourself.mp3", bitrate=320, size=1_000_000, length=160)
        )
        assert reason.startswith("effective bitrate 50 <")

    # The wrong-song hotfix replaced the fuzzy title/artist scores with hard
    # rules; their rejections surface as reasons too.
    def test_version_mismatch_reason(self):
        reason = self._reason(self._file("Eminem - Lose Yourself (Walker Remix).flac", bitrate=1411))
        assert reason == "version mismatch (remix vs expected none)"

    def test_artist_reason(self):
        reason = self._reason(self._file("Somebody Else - Lose Yourself.flac", bitrate=1411))
        assert reason == "artist not in filename or folder"

    def test_missing_title_words_reason(self):
        reason = self._reason(self._file("Eminem - Lose Control.flac", bitrate=1411))
        assert reason == "title words missing from filename"

    def test_low_title_coverage_reason(self):
        reason = self._reason(
            self._file("Eminem - Lose Yourself In The Endless Night Of Music.flac", bitrate=1411)
        )
        assert reason.startswith("title coverage ")

    def test_backslash_peer_path_keeps_full_filename_and_basename(self):
        out = matching_candidates.evaluate_candidates(
            self._results(self._file("@@x\\Eminem\\8 Mile\\01 - Lose Yourself.flac", bitrate=1411)),
            "Lose Yourself", "Eminem",
        )
        assert out[0]["accepted"], out[0]["reason"]
        assert out[0]["filename"] == "@@x\\Eminem\\8 Mile\\01 - Lose Yourself.flac"
        assert out[0]["basename"] == "01 - Lose Yourself.flac"

    def test_accepted_has_no_reason_and_matches_extract(self):
        results = self._results(
            self._file("Eminem - Lose Yourself.flac", bitrate=1411),
            self._file("Eminem - Lose Yourself.wma"),
        )
        evaluated = matching_candidates.evaluate_candidates(results, "Lose Yourself", "Eminem")
        accepted = [c for c in evaluated if c["accepted"]]
        assert len(accepted) == 1 and accepted[0]["reason"] is None
        assert accepted == extract_candidates(results, "Lose Yourself", "Eminem")

    def test_upgrade_cap_reason(self):
        out = matching_candidates.evaluate_candidates(
            self._results(self._file("Eminem - Lose Yourself (Original Mix).flac", bitrate=1411)),
            "Lose Yourself", "Eminem", max_version_tier=1,
        )
        assert out[0]["reason"] == "not a better version than the current file"

    def test_search_history_records_counts_and_reasons(self, monkeypatch):
        from spotify_slsk import runtime_state

        results = self._results(
            self._file("Eminem - Lose Yourself.flac", bitrate=1411),
            self._file("Eminem - Lose Yourself.wma"),
        )
        monkeypatch.setattr(slskd_search, "_run_one_search", lambda q, timeout=60: results)
        monkeypatch.setattr(slskd_search, "download_and_verify", lambda **kw: "/x.flac")
        slskd_search.search_and_download(
            artist="Eminem", title="Lose Yourself", conn=None,
            playlist_name="pl", track_id="t-reasons",
        )
        entry = runtime_state.get_search_history("t-reasons")[-1]
        assert entry["candidate_count"] == 1
        assert entry["rejected_count"] == 1
        assert entry["rejections"][0]["reason"] == "format not allowed: .wma"


class TestDurationFilter:
    def _results(self, length):
        return [{"username": "u", "uploadSpeed": 1000, "files": [{
            "filename": "Eminem - Lose Yourself.flac", "size": 30_000_000,
            "bitrate": 1411, "length": length,
        }]}]

    def test_rejects_when_too_far_off(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DURATION_DIFF_SECONDS", 15)
        out = matching_candidates.evaluate_candidates(
            self._results(190), "Lose Yourself", "Eminem", expected_duration_ms=242_000
        )
        assert out[0]["accepted"] is False
        assert out[0]["reason"] == "duration 3:10 vs 4:02"

    def test_accepts_within_tolerance(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DURATION_DIFF_SECONDS", 15)
        out = matching_candidates.evaluate_candidates(
            self._results(250), "Lose Yourself", "Eminem", expected_duration_ms=242_000
        )
        assert out[0]["accepted"] is True

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DURATION_DIFF_SECONDS", 0)
        out = matching_candidates.evaluate_candidates(
            self._results(100), "Lose Yourself", "Eminem", expected_duration_ms=242_000
        )
        assert out[0]["accepted"] is True

    def test_extended_mix_may_run_longer_but_not_shorter(self, monkeypatch):
        # Spotify often lists the radio edit; the version tiers prefer the
        # Extended Mix, so a duration check must not reject the long cut.
        monkeypatch.setattr(config, "MAX_DURATION_DIFF_SECONDS", 15)

        def judge(length):
            return matching_candidates.evaluate_candidates(
                [{"username": "u", "uploadSpeed": 1, "files": [{
                    "filename": "Sem Jacobs - Blue Berries (Extended Mix).flac",
                    "size": 40_000_000, "length": length}]}],
                "Blue Berries", "Sem Jacobs", expected_duration_ms=180_000,
            )[0]
        assert judge(330)["accepted"] is True
        assert judge(120)["reason"] == "duration 2:00 vs 3:00"

    def test_unknown_durations_are_not_rejected(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DURATION_DIFF_SECONDS", 15)
        assert matching_candidates.evaluate_candidates(
            self._results(None), "Lose Yourself", "Eminem", expected_duration_ms=242_000
        )[0]["accepted"]
        assert matching_candidates.evaluate_candidates(
            self._results(100), "Lose Yourself", "Eminem", expected_duration_ms=None
        )[0]["accepted"]


class TestBlocklistFiltering:
    def _results(self):
        return [
            {"username": "bad", "uploadSpeed": 1, "files": [{
                "filename": "Eminem - Lose Yourself.flac", "size": 1, "bitrate": 1411, "length": 300}]},
            {"username": "good", "uploadSpeed": 1, "files": [{
                "filename": "Eminem - Lose Yourself.flac", "size": 1, "bitrate": 1411, "length": 300}]},
        ]

    def test_blocklisted_file_is_rejected_with_reason(self):
        out = matching_candidates.evaluate_candidates(
            self._results(), "Lose Yourself", "Eminem",
            is_blocked=lambda u, f: u == "bad",
        )
        assert [(c["user"], c["accepted"], c["reason"]) for c in out] == [
            ("bad", False, "blocklisted"), ("good", True, None),
        ]

    def test_failed_transfer_blocklists_and_is_skipped_next_time(self, monkeypatch, tmp_path):
        from spotify_slsk import db as _db

        enqueued = []

        class _Client:
            class transfers:
                @staticmethod
                def enqueue(username, files):
                    enqueued.append(username)

        monkeypatch.setattr(slskd_client, "get_client", lambda: _Client())

        def _fail(candidate):
            candidate["failure"] = "transfer completed, errored"
            candidate["peer_fault"] = True
            return None

        monkeypatch.setattr(slskd_transfers, "wait_for_completion", _fail)
        conn = _db.create_connection(str(tmp_path / "t.db"))
        _db.create_table(conn, "pl_x")
        _db.insert_track(conn, "pl_x", ("t1", "Lose Yourself", "Eminem", "Alb"))
        results = self._results()[:1]
        slskd_search.download_and_verify(results, "Lose Yourself", "Eminem", conn, "pl_x", "t1")
        assert _db.is_blocklisted(conn, "bad", "Eminem - Lose Yourself.flac")

        # Retry clears _tried; the blocklist still keeps the peer file out.
        _db.retry_track(conn, "pl_x", "t1")
        enqueued.clear()
        slskd_search.download_and_verify(results, "Lose Yourself", "Eminem", conn, "pl_x", "t1")
        assert enqueued == []
        conn.close()


class TestBlocklistPolicy:
    """Only a definitive peer-file fault blocklists globally; everything
    else goes to the track's _tried list only."""

    def _run(self, monkeypatch, tmp_path, outcome):
        from spotify_slsk import db as _db

        class _Client:
            class transfers:
                @staticmethod
                def enqueue(username, files):
                    pass

        monkeypatch.setattr(slskd_client, "get_client", lambda: _Client())
        monkeypatch.setattr(slskd_transfers, "wait_for_completion", outcome)
        conn = _db.create_connection(str(tmp_path / "t.db"))
        _db.create_table(conn, "pl_x")
        _db.insert_track(conn, "pl_x", ("t1", "Lose Yourself", "Eminem", "Alb"))
        cand = {"user": "peer", "filename": "@@a\\Eminem - Lose Yourself.flac", "size": 1}
        assert slskd_transfers.grab_candidate(cand, conn, "pl_x", "t1") is None
        out = (
            _db.list_blocklist(conn),
            _db.get_tried_files(conn, "pl_x", "t1"),
            [r["event"] for r in _db.list_history(conn)],
        )
        conn.close()
        return out

    @pytest.mark.parametrize("state", [
        "completed, errored", "completed, rejected", "completed, failed", "completed, aborted",
    ])
    def test_explicit_peer_failure_blocklists(self, monkeypatch, tmp_path, state):
        def outcome(c):
            c["failure"] = f"transfer {state}"
            c["peer_fault"] = any(w in state for w in slskd_models.PEER_FAULT_STATES)
            return None

        blocked, tried, _ = self._run(monkeypatch, tmp_path, outcome)
        assert len(blocked) == 1 and tried == ["Eminem - Lose Yourself.flac"]

    @pytest.mark.parametrize("failure", [
        "transfer timed out", "transfer not found in slskd",
        "file not found after download", None,
    ])
    def test_soft_failures_only_go_to_tried(self, monkeypatch, tmp_path, failure):
        def outcome(c):
            if failure:
                c["failure"] = failure
            return None

        blocked, tried, events = self._run(monkeypatch, tmp_path, outcome)
        assert blocked == []
        assert tried == ["Eminem - Lose Yourself.flac"]
        assert events == ["download_failed"]

    def test_wait_for_completion_flags_only_peer_fault_states(self, monkeypatch):
        for state, fault in [("Completed, Errored", True), ("Completed, TimedOut", False)]:
            payload = {"directories": [{"files": [
                {"id": "x", "filename": "a\\b.flac", "size": 1, "state": state},
            ]}]}

            class _Client:
                class transfers:
                    @staticmethod
                    def get_downloads(user, payload=payload):
                        return payload

            monkeypatch.setattr(slskd_client, "get_client", lambda: _Client())
            cand = {"user": "u", "filename": "a\\b.flac", "size": 1}
            assert slskd_transfers.wait_for_completion(cand) is None
            assert cand["peer_fault"] is fault, state


class TestInteractiveSearch:
    def test_collects_all_queries_accepted_first_and_deduped(self, monkeypatch):
        monkeypatch.setattr(slskd_client, "new_client", lambda: object())
        good = {"filename": "Eminem - Lose Yourself.flac", "size": 1, "bitrate": 1411, "length": 300}
        bad = {"filename": "Eminem - Lose Yourself.wma", "size": 1, "bitrate": 320, "length": 300}
        mp3 = {"filename": "Eminem - Lose Yourself.mp3", "size": 12_000_000, "bitrate": 320, "length": 300}
        calls = []

        def fake(query, timeout=60, client=None):
            calls.append(query)
            # Same files on every query: must be de-duplicated.
            return [{"username": "u", "uploadSpeed": 1, "files": [bad, mp3, good]}]

        monkeypatch.setattr(slskd_search, "_run_one_search", fake)
        out = slskd_search.interactive_search("Eminem", "Lose Yourself")
        assert len(calls) == len(slskd_search._build_search_queries("Eminem", "Lose Yourself"))
        assert [(c["ext"], c["accepted"]) for c in out] == [
            (".flac", True), (".mp3", True), (".wma", False),
        ]


class TestRealSlskdShape:
    """slskd sends camelCase fields; "bitrate" (lowercase) never exists, so
    the reported-bitrate floor used to be dead code."""

    def _fixture(self):
        import json
        import pathlib

        path = pathlib.Path(__file__).parent / "fixtures" / "slskd" / "search_responses.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_bitrate_is_parsed_and_enforced(self):
        data = self._fixture()
        out = extract_candidates(
            data["responses"], data["expected"]["title"], data["expected"]["artist"]
        )
        by_user = {c["user"]: c for c in out}
        assert set(by_user) == {"user1", "user2"}, by_user.keys()
        assert by_user["user2"]["bitrate"] == 320
        # user3's MP3 reports bitRate 128 but its size/length look like 320:
        # only the reported bitRate can reject it.
        assert "user3" not in by_user


def _transfer_fixture():
    import json
    import pathlib

    path = pathlib.Path(__file__).parent / "fixtures" / "slskd" / "transfer_states.json"
    return json.loads(path.read_text(encoding="utf-8"))


_TRANSFERS = _transfer_fixture()


class TestTransferStates:
    """Every terminal "Completed, X" state except Succeeded is a failure, and
    must end the wait immediately instead of running into the timeout."""

    @pytest.mark.parametrize(
        "case", _TRANSFERS["cases"], ids=[c["state"] for c in _TRANSFERS["cases"]]
    )
    def test_classification(self, case):
        assert slskd_models.Transfer(id="x", filename="f", state=case["state"]).outcome == case["expected"]

    @pytest.mark.parametrize(
        "case",
        [c for c in _TRANSFERS["cases"] if c["expected"]],
        ids=[c["state"] for c in _TRANSFERS["cases"] if c["expected"]],
    )
    def test_wait_for_completion_returns_without_timing_out(self, case, monkeypatch):
        import copy

        payload = copy.deepcopy(_TRANSFERS["template"])
        f = payload["directories"][0]["files"][0]
        f["state"] = case["state"]
        polls = []

        class _Transfers:
            def get_downloads(self, user):
                polls.append(user)
                return payload

        class _Client:
            transfers = _Transfers()

        monkeypatch.setattr(slskd_client, "get_client", lambda: _Client())
        monkeypatch.setattr(slskd_transfers, "_await_completed_file", lambda n: "/dl/" + n)
        monkeypatch.setattr(slskd_transfers, "_wait_for_external_processing", lambda p: True)
        sleeps = []
        # Returning True ("shutdown") turns a would-be 300 s hang into a fast,
        # visible failure.
        monkeypatch.setattr(
            slskd_client, "interruptible_sleep", lambda s: sleeps.append(s) or True
        )

        out = slskd_transfers.wait_for_completion(
            {"user": "user1", "filename": f["filename"], "size": f["size"]}, timeout=300
        )

        assert sleeps == [], "a terminal state must not wait for another poll"
        if case["expected"] == "succeeded":
            assert out == "/dl/Artist - Song (Original Mix).flac"
        else:
            assert out is None


class TestCompletedFileGrace:
    """slskd 0.23 reported "Completed, Succeeded" while the file was still
    under /downloads/incomplete; the move landed moments later. That was
    treated as "file not found" and the good peer was blocklisted."""

    def test_incomplete_dir_is_never_a_hit(self, tmp_path):
        (tmp_path / "incomplete" / "Album").mkdir(parents=True)
        (tmp_path / "incomplete" / "Album" / "a.flac").write_bytes(b"x")
        assert slskd_transfers.find_file_in_downloads("a.flac", str(tmp_path)) is None
        (tmp_path / "Album").mkdir()
        (tmp_path / "Album" / "a.flac").write_bytes(b"x")
        assert slskd_transfers.find_file_in_downloads("a.flac", str(tmp_path)) == str(
            tmp_path / "Album" / "a.flac"
        )

    def test_waits_for_the_file_to_leave_incomplete(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
        (tmp_path / "incomplete").mkdir()
        (tmp_path / "incomplete" / "a.flac").write_bytes(b"x")
        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) == 3:  # slskd moves the file during the grace period
                (tmp_path / "done").mkdir()
                (tmp_path / "incomplete" / "a.flac").rename(tmp_path / "done" / "a.flac")
            return False

        monkeypatch.setattr(slskd_client, "interruptible_sleep", fake_sleep)
        assert slskd_transfers._await_completed_file("a.flac", grace=30) == str(
            tmp_path / "done" / "a.flac"
        )
        assert len(sleeps) == 3

    def test_gives_up_after_grace_and_on_shutdown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path))
        clock = [1000.0]
        monkeypatch.setattr(slskd_transfers.time, "time", lambda: clock[0])

        def fake_sleep(s):
            clock[0] += s
            return False

        monkeypatch.setattr(slskd_client, "interruptible_sleep", fake_sleep)
        assert slskd_transfers._await_completed_file("a.flac", grace=5) is None
        assert clock[0] == 1005.0
        monkeypatch.setattr(slskd_client, "interruptible_sleep", lambda s: True)
        assert slskd_transfers._await_completed_file("a.flac", grace=30) is None
