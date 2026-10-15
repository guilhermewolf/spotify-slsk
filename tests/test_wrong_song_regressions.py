"""Regression tests for the wrong-song production bug (~15% of downloaded
rows pointed at the wrong song). Each failing example is a real case
observed on the NAS: expected track -> wrong file that was accepted.
"""
import os

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
