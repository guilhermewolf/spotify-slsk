"""Cross-playlist dedupe via hardlinks (Tier 5, LINK_MODE)."""
from __future__ import annotations

import errno
import os
import pathlib
import shutil

import pytest

from spotify_slsk import config, db
from spotify_slsk.config import validate_setting
from spotify_slsk.importer.tags import write_tags
from spotify_slsk.library import acquire, health
from spotify_slsk.models import Track

AUDIO = pathlib.Path(__file__).parent / "fixtures" / "audio"


@pytest.fixture
def lib(tmp_path, sandbox_dirs, monkeypatch):
    """pl_a has 'Song' downloaded; pl_b wants the same track (pending)."""
    _, playlists = sandbox_dirs
    conn = db.create_connection(str(tmp_path / "t.db"))
    for pid, table in (("pa", "pl_a"), ("pb", "pl_b")):
        db.upsert_playlist_meta(conn, pid, table, table, "s")
        db.create_table(conn, table)
    src = playlists / "pl_a" / "Artist - Song.mp3"
    src.parent.mkdir()
    shutil.copy(AUDIO / "silence.mp3", src)
    write_tags(str(src), {"title": ["Song"], "artist": ["Artist"]})
    db.insert_track(conn, "pl_a", ("t1", "Song", "Artist", "Album", None, "GBAYE0601498"))
    db.update_download_status(conn, "t1", "pl_a", success=True, file_path=str(src))
    db.set_track_quality(conn, "pl_a", "t1", "MP3_320")
    db.insert_track(conn, "pl_b", ("t1", "Song", "Artist", "Album", None, "GBAYE0601498"))
    monkeypatch.setattr(config, "LINK_MODE", "hardlink")
    monkeypatch.setattr(config, "FILE_NAME_TEMPLATE", "{original_filename}")
    searched = []
    monkeypatch.setattr(acquire, "search_and_download", lambda **k: searched.append(k) or None)
    yield conn, src, playlists, searched
    conn.close()


def _track(tid="t1", isrc="GBAYE0601498"):
    return Track(tid, "Song", "Artist", "Album", "pl_b", isrc=isrc)


class TestLink:
    def test_same_spotify_id_is_hardlinked_not_downloaded(self, lib):
        conn, src, playlists, searched = lib
        assert acquire.handle_track_download(_track(), "pl_b", conn) is True
        dest = playlists / "pl_b" / "Artist - Song.mp3"
        assert os.path.samefile(src, dest)
        row = db.get_track(conn, "pl_b", "t1")
        assert row["downloaded"] and row["path"] == str(dest) and row["quality"] == "MP3_320"
        event = db.list_history(conn, event="linked")[0]["data"]
        assert (event["mode"], event["match"], event["source_path"]) == ("hardlink", "spotify id", str(src))
        assert searched == []

    def test_same_isrc_under_another_id(self, lib):
        conn, src, playlists, searched = lib
        db.insert_track(conn, "pl_b", ("t-other", "Song", "Artist", "Album", None, "gbaye0601498"))
        assert acquire.link_existing_copy(_track("t-other"), "pl_b", conn)
        assert db.list_history(conn, event="linked")[0]["data"]["match"] == "isrc"

    def test_name_follows_the_template_and_never_overwrites(self, lib, monkeypatch):
        conn, src, playlists, _ = lib
        monkeypatch.setattr(config, "FILE_NAME_TEMPLATE", "{title}")
        (playlists / "pl_b").mkdir()
        (playlists / "pl_b" / "Song.mp3").write_bytes(b"someone else")
        acquire.link_existing_copy(_track(), "pl_b", conn)
        assert db.get_track(conn, "pl_b", "t1")["path"] == str(playlists / "pl_b" / "Song (2).mp3")
        assert (playlists / "pl_b" / "Song.mp3").read_bytes() == b"someone else"

    def test_copy_mode_makes_an_independent_file(self, lib, monkeypatch):
        conn, src, playlists, _ = lib
        monkeypatch.setattr(config, "LINK_MODE", "copy")
        assert acquire.link_existing_copy(_track(), "pl_b", conn)
        dest = playlists / "pl_b" / "Artist - Song.mp3"
        assert not os.path.samefile(src, dest) and dest.read_bytes() == src.read_bytes()

    def test_cross_device_hardlink_falls_back_to_copy(self, lib, monkeypatch):
        conn, src, playlists, _ = lib

        def exdev(a, b):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(acquire.os, "link", exdev)
        assert acquire.link_existing_copy(_track(), "pl_b", conn)
        assert db.list_history(conn, event="linked")[0]["data"]["mode"] == "copy"
        assert (playlists / "pl_b" / "Artist - Song.mp3").exists()

    def test_off_downloads_as_before(self, lib, monkeypatch):
        conn, _, _, searched = lib
        monkeypatch.setattr(config, "LINK_MODE", "off")
        assert acquire.handle_track_download(_track(), "pl_b", conn) is False
        assert len(searched) == 1

    def test_missing_source_file_falls_through_to_search(self, lib):
        conn, src, _, searched = lib
        src.unlink()
        assert acquire.handle_track_download(_track(), "pl_b", conn) is False
        assert len(searched) == 1

    def test_unrelated_track_is_not_linked(self, lib):
        conn, _, _, _ = lib
        db.insert_track(conn, "pl_b", ("t9", "Other", "X", "Y"))
        assert not acquire.link_existing_copy(_track("t9", isrc=None), "pl_b", conn)


class TestHardlinksStayIndependent:
    def test_quarantining_one_keeps_the_other(self, lib):
        conn, src, playlists, _ = lib
        acquire.link_existing_copy(_track(), "pl_b", conn)
        dest = str(playlists / "pl_b" / "Artist - Song.mp3")
        assert health.quarantine_track(conn, "pl_b", "t1", dest)
        assert src.exists() and db.get_track(conn, "pl_a", "t1")["downloaded"]
        assert not os.path.exists(dest)

    def test_replacing_one_keeps_the_other(self, lib):
        conn, src, playlists, _ = lib
        acquire.link_existing_copy(_track(), "pl_b", conn)
        retired = health.retire_replaced_file(conn, "pl_a", "t1", str(src), "test")
        assert retired and os.path.exists(retired)
        assert (playlists / "pl_b" / "Artist - Song.mp3").read_bytes() == pathlib.Path(retired).read_bytes()

    def test_health_scan_does_not_flag_hardlinks(self, lib, monkeypatch):
        conn, _, _, _ = lib
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        acquire.link_existing_copy(_track(), "pl_b", conn)
        checked, finished = health.scan_step(conn)
        assert (checked, finished) == (2, True)
        for table in ("pl_a", "pl_b"):
            assert db.get_file_check(conn, table, "t1")["problems"] == []


def test_link_mode_setting_is_validated():
    assert validate_setting("choice:hardlink|copy|off", " Copy ") == ("copy", None)
    assert validate_setting("choice:hardlink|copy|off", "symlink")[0] is None
