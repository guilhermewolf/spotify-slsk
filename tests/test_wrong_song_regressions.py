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
