"""Unit tests for the local-file <-> DB matcher in app.py."""
import os

# Avoid requiring real slskd env at import time — the lazy client never runs.
os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

from app import (
    _artists_overlap,
    _looks_like_match,
    _remix_equivalent,
    _similar,
    _split_artists,
    _titles_token_equivalent,
    _tokenize,
    get_playlist_id,
    score_track_match,
)
from utils import sanitize_table_name


class TestTokenize:
    def test_splits_on_punctuation(self):
        assert _tokenize("Song - Name") == {"song", "name"}

    def test_strips_stop_phrases(self):
        assert "mix" not in _tokenize("Song Name (Extended Mix)")

    def test_empty_input(self):
        assert _tokenize("") == set()
        assert _tokenize(None) == set()


class TestSplitArtists:
    def test_comma_split(self):
        assert _split_artists("Disclosure, AlunaGeorge") == {"disclosure", "alunageorge"}

    def test_feat_ft_ampersand_and(self):
        assert _split_artists("A feat. B") == {"a", "b"}
        assert _split_artists("A ft B") == {"a", "b"}
        assert _split_artists("A & B") == {"a", "b"}
        assert _split_artists("A and B") == {"a", "b"}

    def test_empty(self):
        assert _split_artists("") == set()
        assert _split_artists(None) == set()


class TestArtistsOverlap:
    def test_exact(self):
        assert _artists_overlap("Daft Punk", "daft punk")

    def test_subset(self):
        assert _artists_overlap("Disclosure, AlunaGeorge", "Disclosure")

    def test_none(self):
        assert not _artists_overlap("Aphex Twin", "Boards of Canada")

    def test_empty_side(self):
        assert not _artists_overlap("", "Daft Punk")
        assert not _artists_overlap("Daft Punk", None)


class TestTitlesTokenEquivalent:
    def test_ignores_mix_tag(self):
        assert _titles_token_equivalent("Song Name (Extended Mix)", "Song Name")

    def test_one_missing_token_in_longer(self):
        assert _titles_token_equivalent("one two three", "one two three four")

    def test_fails_on_unrelated(self):
        assert not _titles_token_equivalent("apple pie", "banana split")


class TestRemixEquivalent:
    def test_paren_vs_dash(self):
        assert _remix_equivalent("Song (Walker Remix)", "Song - Walker Remix")

    def test_different_remixers(self):
        # Different remixers should still match as "remix-style equivalent"
        # since the helper only checks the normalized token form, not identity.
        # Cross-check: title-token equivalence fails, so the scorer falls
        # through to fuzzy matching, which is the intended behavior.
        assert _remix_equivalent("Song (X Remix)", "Song (Y Remix)")


class TestScoreTrackMatch:
    def test_exact_title_and_artist(self):
        score, reason = score_track_match("Song Name", "Artist", "Song Name", "Artist")
        assert score >= 0.95
        assert "artist_overlap" in reason

    def test_title_only_when_artist_missing(self):
        score, reason = score_track_match("Song Name", "", "Song Name", "Artist")
        assert 0.85 <= score < 0.95

    def test_fuzzy_fallback(self):
        score, reason = score_track_match(
            "Completely Different", "A", "Song Name", "B"
        )
        assert score < 0.6
        assert "fuzzy" in reason


class TestLooksLikeMatch:
    def test_multi_word_title_matches_without_artist_overlap(self):
        # Regression: the old `if False` bug prevented multi-word titles
        # from being accepted when artist info existed on both sides but
        # didn't overlap. This must work.
        assert _looks_like_match(
            track_name="Lose Yourself",
            track_artist="Eminem",
            file_title="Lose Yourself",
            file_artist="Some Other Artist",
            file_stem="lose yourself",
        )

    def test_long_single_word_title_matches(self):
        assert _looks_like_match(
            track_name="Supernova",
            track_artist="Artist",
            file_title="Supernova",
            file_artist="Different",
            file_stem="supernova",
        )

    def test_short_single_word_title_does_not_match_without_artist(self):
        # "Run" is 3 chars; with distinct artists on both sides, the short
        # title shouldn't be enough to claim a match.
        assert not _looks_like_match(
            track_name="Run",
            track_artist="Aphex Twin",
            file_title="Run",
            file_artist="Completely Unrelated Artist",
            file_stem="run",
        )


class TestGetPlaylistId:
    def test_standard_url(self):
        assert (
            get_playlist_id(
                "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc"
            )
            == "37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_no_query_string(self):
        assert (
            get_playlist_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
            == "37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_invalid_url_returns_none(self):
        # get_playlist_id swallows ValueError/IndexError and returns None
        assert get_playlist_id("not a url") is None


class TestSanitizeTableName:
    def test_lowercases_and_replaces_non_word(self):
        assert sanitize_table_name("My Playlist!") == "pl_my_playlist_"

    def test_unicode_collapsed(self):
        # Documented gotcha: Unicode collapses to "_" under \W+.
        assert sanitize_table_name("Favorites ♥") == "pl_favorites_"


class TestSimilar:
    def test_identical(self):
        assert _similar("abc", "abc") == 1.0

    def test_case_insensitive(self):
        assert _similar("ABC", "abc") == 1.0

    def test_totally_different(self):
        assert _similar("abc", "xyz") < 0.3
