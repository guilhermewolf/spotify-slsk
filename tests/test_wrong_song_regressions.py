"""Regression tests for the wrong-song production bug (~15% of downloaded
rows pointed at the wrong song). Each failing example is a real case
observed on the NAS: expected track -> wrong file that was accepted.
"""
import os

import pytest

os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

from spotify_slsk import soulseek_api
from spotify_slsk.soulseek_api import _remote_basename, extract_candidates


def _results(*filenames, user="user1"):
    return [{
        "username": user,
        "uploadSpeed": 10000,
        "files": [
            {"filename": f, "size": 30_000_000, "bitDepth": 16,
             "sampleRate": 44100, "length": 300}
            for f in filenames
        ],
    }]


class TestRemotePathSplitting:
    def test_backslash_path(self):
        assert _remote_basename(
            "@@abc\\music\\Рахманинов\\Vespers (All-Night Vigil)\\06 Ave Maria.flac"
        ) == "06 Ave Maria.flac"

    def test_forward_slash_and_bare(self):
        assert _remote_basename("a/b/c.mp3") == "c.mp3"
        assert _remote_basename("c.mp3") == "c.mp3"

    def test_folder_names_are_not_scored_as_title(self):
        # Expected "AVE - All Night": the folder "All-Night Vigil" used to
        # supply the title tokens for a Rachmaninov "Ave Maria".
        path = "@@abc\\music\\Рахманинов\\Vespers (All-Night Vigil)\\06 Ave Maria.flac"
        assert extract_candidates(_results(path), "All Night", "AVE") == []

    def test_candidate_keeps_full_path_for_enqueue(self):
        path = "music\\Mat Joe\\Mat Joe - Heart To Find.flac"
        out = extract_candidates(_results(path), "Heart To Find", "Mat Joe")
        assert len(out) == 1
        assert out[0]["filename"] == path, "slskd needs the full peer path"
        assert out[0]["basename"] == "Mat Joe - Heart To Find.flac"

    def test_tried_files_store_the_real_basename(self, monkeypatch):
        added = []

        class _Transfers:
            def enqueue(self, username, files):
                pass

        class _Client:
            transfers = _Transfers()

        monkeypatch.setattr(soulseek_api, "get_client", lambda: _Client())
        monkeypatch.setattr(soulseek_api, "wait_for_completion", lambda c: None)
        monkeypatch.setattr(soulseek_api, "get_tried_files", lambda *a: [])
        monkeypatch.setattr(
            soulseek_api, "add_tried_file", lambda c, p, t, f: added.append(f)
        )
        soulseek_api.download_and_verify(
            _results("music\\Mat Joe\\Mat Joe - Heart To Find.flac"),
            "Heart To Find", "Mat Joe", None, "pl_x", "t1",
        )
        assert added == ["Mat Joe - Heart To Find.flac"]


# (expected title, expected artist, wrong file that production accepted)
WRONG_CANDIDATES = [
    ("All Night", "InsertFX", "HOYO-MiX - We Shall Sing All Night.flac"),
    ("The City", "Jake Dile", "Tina Turner - Nutbush City Limits.flac"),
    ("Really Really", "Super Flu", "Andy Partridge - That's Really Super Supergirl.flac"),
    ("Dream", "Kris m", "Kris Kross - A Real Bad Dream.flac"),
    ("Your Eyes", "Eudi",
     "Blue Foundation; Michael Bibi - Eyes On Fire (Bibi Remix).flac"),
    ("The Scoop", "Format:B", "Robbie Doherty - Pour the Milk.flac"),
    ("Together", "Hollaphonic", "Öwnboss - Come Together.flac"),
    ("Odyssey - Kyle Watson Remix", "Justin Martin",
     "War - Low Rider (Kyle Watson Remix).flac"),
    ("Dustup - Mefjus Remix", "Noisia", "Noisia - Dustup (Original Mix).flac"),
    ("Cold Outside - Kris m Remix", "Toxic Wraith",
     "Toxic Wraith - Cold Outside (Original Mix).flac"),
    ("All Night", "AVE",
     "@@abc\\music\\Рахманинов\\Vespers (All-Night Vigil)\\06 Ave Maria.flac"),
    ("Dream", "Kris m", "Kris M A Real Bad Dream.flac"),
    ("Get Lucky", "Daft Punk", "Daft Punk - Get Lucky (DMC Hotmix).mp3"),
]


class TestCandidateRejectsWrongSongs:
    @pytest.mark.parametrize("title,artist,filename", WRONG_CANDIDATES)
    def test_rejected(self, title, artist, filename):
        assert extract_candidates(_results(filename), title, artist) == []


# (expected title, expected artist, correct file that must still match)
RIGHT_CANDIDATES = [
    ("Get Lucky (feat. Pharrell Williams)", "Daft Punk", "Daft Punk - Get Lucky.flac"),
    ("Get Lucky", "Daft Punk, Pharrell Williams",
     "Daft Punk feat. Pharrell Williams - Get Lucky.flac"),
    ("Get Lucky", "Daft Punk",
     "music\\Daft Punk\\Random Access Memories\\08 - Get Lucky "
     "(feat. Pharrell Williams and Nile Rodgers).flac"),
    ("Get Lucky", "Daft Punk", "Daft Punk - Random Access Memories - 08 - Get Lucky.flac"),
    ("Song", "Artist1, Artist2", "Artist1 & Artist2 - Song.flac"),
    ("Song", "Artist1 & Artist2", "Artist2 - Song.flac"),
    ("Heart To Find", "Mat Joe", "Mat Joe - Heart To Find (Extended Mix).flac"),
    ("Heart To Find", "Mat Joe", "Mat Joe - Heart To Find (Original Mix).flac"),
    ("Heart To Find", "Mat Joe", "Mat Joe - Heart To Find - Extended Mix.flac"),
    ("Heart To Find", "Mat Joe", "02. mat_joe-heart_to_find [320].flac"),
    ("Déjà Vu", "Beyoncé", "Beyonce - Deja Vu.flac"),
    ("Don't Stop Me Now", "Queen", "QUEEN - Don’t Stop Me Now!.flac"),
    ("Blinding Lights", "The Weeknd", "Weeknd - Blinding Lights.flac"),
    ("Blinding Lights", "The Weeknd", "The Weeknd - Blinding Lights.flac"),
    ("Here Comes The Sun - Remastered 2019", "The Beatles",
     "The Beatles - Here Comes The Sun.flac"),
    ("Here Comes The Sun - 2009 Remaster", "The Beatles",
     "The Beatles - Here Comes The Sun (Remastered).flac"),
    ("群青", "YOASOBI", "YOASOBI - 群青.flac"),
    ("First Love", "宇多田ヒカル", "Music\\宇多田ヒカル Utada Hikaru\\First Love\\04 First Love.mp3"),
    ("Odyssey - Kyle Watson Remix", "Justin Martin",
     "Justin Martin - Odyssey (Kyle Watson Remix).flac"),
    ("The Scoop", "Format:B", "FormatB - The Scoop (Original Mix).flac"),
    ("Get Lucky", "Daft Punk",
     "Daft Punk, Pharrell Williams, Nile Rodgers - Get Lucky - USQX91300108.flac"),
]


class TestCandidateKeepsRightSongs:
    @pytest.mark.parametrize("title,artist,filename", RIGHT_CANDIDATES)
    def test_accepted(self, title, artist, filename):
        assert len(extract_candidates(_results(filename), title, artist)) == 1

    def test_extended_mix_still_ranks_first(self):
        out = soulseek_api.sort_candidates(extract_candidates(_results(
            "Mat Joe - Heart To Find (Original Mix).flac",
            "Mat Joe - Heart To Find.flac",
            "Mat Joe - Heart To Find (Extended Mix).flac",
        ), "Heart To Find", "Mat Joe"))
        assert len(out) == 3
        assert "Extended Mix" in out[0]["basename"]


class TestVersionSymmetry:
    @pytest.mark.parametrize("expected,filename", [
        ("Heart To Find", "Mat Joe - Heart To Find (Dub).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (VIP).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Club Mix).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Instrumental).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Sped Up).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Slowed + Reverb).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Nightcore).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (8D Audio).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Karaoke Version).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Acoustic).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Live).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Piano Cover).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Kris M Edit).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Strings Mix).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find RMX.flac"),
        ("Heart To Find - Dub", "Mat Joe - Heart To Find.flac"),
        ("Heart To Find - Kris M Remix", "Mat Joe - Heart To Find (Other Remix).flac"),
    ])
    def test_different_version_rejected(self, expected, filename):
        assert extract_candidates(_results(filename), expected, "Mat Joe") == []

    @pytest.mark.parametrize("expected,filename", [
        ("Heart To Find", "Mat Joe - Heart To Find (Radio Edit).flac"),
        ("Heart To Find", "Mat Joe - Heart To Find (Album Version).flac"),
        ("Heart To Find - Dub", "Mat Joe - Heart To Find (Dub).flac"),
        ("Heart To Find - Kris M Remix", "Mat Joe - Heart To Find (Kris M Remix).flac"),
        ("Heart To Find - Kris M Remix", "Mat Joe - Heart To Find (Kris M RMX).flac"),
        ("Heart To Find - Kris M Edit", "Mat Joe - Heart To Find (Kris M Edit).flac"),
    ])
    def test_same_or_neutral_version_accepted(self, expected, filename):
        assert len(extract_candidates(_results(filename), expected, "Mat Joe")) == 1

    def test_artist_name_is_not_a_version_keyword(self):
        assert len(extract_candidates(
            _results("Dub FX - Love Someone.flac"), "Love Someone", "Dub FX"
        )) == 1


from spotify_slsk.app import _titles_token_equivalent, score_track_match  # noqa: E402

MIN_MATCH = 0.62  # app default

# (file title tag, file artist tag, expected title, expected artist)
WRONG_TAGS = [
    ("We Shall Sing All Night", "HOYO-MiX", "All Night", "InsertFX"),
    ("Nutbush City Limits", "Tina Turner", "The City", "Jake Dile"),
    ("That's Really Super Supergirl", "Andy Partridge", "Really Really", "Super Flu"),
    ("A Real Bad Dream", "Kris Kross", "Dream", "Kris m"),
    ("Eyes On Fire (Bibi Remix)", "Blue Foundation; Michael Bibi", "Your Eyes", "Eudi"),
    ("Pour the Milk", "Robbie Doherty", "The Scoop", "Format:B"),
    ("Come Together", "Öwnboss", "Together", "Hollaphonic"),
    ("Low Rider (Kyle Watson Remix)", "War",
     "Odyssey - Kyle Watson Remix", "Justin Martin"),
    ("Dustup (Original Mix)", "Noisia", "Dustup - Mefjus Remix", "Noisia"),
    ("Cold Outside", "Toxic Wraith", "Cold Outside - Kris m Remix", "Toxic Wraith"),
    ("Cold Outside (Other Remix)", "Toxic Wraith",
     "Cold Outside - Kris m Remix", "Toxic Wraith"),
    ("Heart To Find (Dub)", "Mat Joe", "Heart To Find", "Mat Joe"),
    ("Heart To Find (Instrumental)", "Mat Joe", "Heart To Find", "Mat Joe"),
    ("A Real Bad Dream", "Kris m", "Dream", "Kris m"),
]

RIGHT_TAGS = [
    ("Get Lucky", "Daft Punk feat. Pharrell Williams",
     "Get Lucky (feat. Pharrell Williams)", "Daft Punk, Pharrell Williams"),
    ("Get Lucky", "Daft Punk ft. Pharrell Williams", "Get Lucky", "Daft Punk"),
    ("Song", "Artist1 & Artist2", "Song", "Artist1, Artist2"),
    ("Heart To Find (Extended Mix)", "Mat Joe", "Heart To Find", "Mat Joe"),
    ("Heart To Find (Original Mix)", "Mat Joe", "Heart To Find", "Mat Joe"),
    ("Heart To Find (Radio Edit)", "Mat Joe", "Heart To Find", "Mat Joe"),
    ("Heart To Find - Extended Mix", "Mat Joe", "Heart To Find", "Mat Joe"),
    ("HEART TO FIND!", "MAT JOE", "Heart To Find", "Mat Joe"),
    ("群青", "YOASOBI", "群青", "YOASOBI"),
    ("Here Comes The Sun", "The Beatles", "Here Comes The Sun - Remastered 2019",
     "The Beatles"),
    ("Cold Outside (Kris M Remix)", "Toxic Wraith",
     "Cold Outside - Kris m Remix", "Toxic Wraith"),
]


class TestScoreTrackMatchRegressions:
    @pytest.mark.parametrize("ft,fa,dt,da", WRONG_TAGS)
    def test_wrong_song_below_threshold(self, ft, fa, dt, da):
        score, reason = score_track_match(ft, fa, dt, da)
        assert score < MIN_MATCH, reason

    @pytest.mark.parametrize("ft,fa,dt,da", RIGHT_TAGS)
    def test_right_song_above_threshold(self, ft, fa, dt, da):
        score, reason = score_track_match(ft, fa, dt, da)
        assert score >= MIN_MATCH, reason

    def test_two_word_titles_need_more_than_one_shared_word(self):
        assert not _titles_token_equivalent("Your Eyes", "Eyes On Fire")
        assert not _titles_token_equivalent("I Love You", "You")
        assert not _titles_token_equivalent("Dream", "A Real Bad Dream")


# Matches that need the extended artist splitting and folding.
RIGHT_ARTIST_TAGS = [
    ("Déjà Vu", "Beyonce", "Déjà Vu", "Beyoncé"),
    ("Blinding Lights", "Weeknd", "Blinding Lights", "The Weeknd"),
    ("Eyes On Fire (Bibi Remix)", "Blue Foundation; Michael Bibi",
     "Eyes On Fire - Bibi Remix", "Blue Foundation, Michael Bibi"),
    ("Song", "Artist1 x Artist2", "Song", "Artist2"),
    ("Song", "Artist1 vs Artist2", "Song", "Artist2"),
    ("Song", "Artist1 vs. Artist2", "Song", "Artist2"),
    ("Song", "Artist1 with Artist2", "Song", "Artist2"),
    ("Song", "Format B", "Song", "Format:B"),
]


class TestArtistSplitting:
    @pytest.mark.parametrize("ft,fa,dt,da", RIGHT_ARTIST_TAGS)
    def test_right_song_above_threshold(self, ft, fa, dt, da):
        score, reason = score_track_match(ft, fa, dt, da)
        assert score >= MIN_MATCH, reason

    def test_separators(self):
        from spotify_slsk.app import _split_artists

        assert _split_artists("A; B") == {"a", "b"}
        assert _split_artists("A x B") == {"a", "b"}
        assert _split_artists("A vs B") == {"a", "b"}
        assert _split_artists("A with B") == {"a", "b"}
        assert _split_artists("AC/DC") == {"ac dc"}
        assert _split_artists("Lil Nas X") == {"lil nas x"}
        assert _split_artists("Daft Punk") == {"daft punk"}
