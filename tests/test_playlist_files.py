"""Playlist folder files (Tier 5): <name>.m3u8 and cover.jpg."""
from __future__ import annotations

import pathlib

import pytest

from spotify_slsk import config, db
from spotify_slsk.library import playlist_files, reconcile

COVER = (pathlib.Path(__file__).parent / "fixtures" / "audio" / "cover.jpg").read_bytes()


@pytest.fixture
def pl(tmp_path, sandbox_dirs, monkeypatch):
    _, playlists = sandbox_dirs
    conn = db.create_connection(str(tmp_path / "t.db"))
    db.upsert_playlist_meta(conn, "pid1", "pl_road_trip", "Road Trip: 2024/25", "s")
    db.create_table(conn, "pl_road_trip")
    folder = playlists / "pl_road_trip"
    folder.mkdir()
    for tid, name, pos, ms, fname in (
        ("t1", "Ünïcode Song", 2, 201_400, "b.flac"),
        ("t2", "First", 1, None, "a.mp3"),
        ("t3", "Pending", 3, 100_000, None),
        ("t4", "Gone", 4, 100_000, "gone.mp3"),
    ):
        db.insert_track(conn, "pl_road_trip", (tid, name, "Artist", "Album", ms), {"position": pos})
        if fname:
            if tid != "t4":
                (folder / fname).write_bytes(b"x")
            db.update_download_status(conn, tid, "pl_road_trip", success=True, file_path=str(folder / fname))
    monkeypatch.setattr(config, "WRITE_M3U", True)
    monkeypatch.setattr(config, "WRITE_PLAYLIST_COVER", True)
    yield conn, folder
    conn.close()


class TestM3u8:
    def test_written_in_spotify_order_relative_utf8(self, pl):
        conn, folder = pl
        playlist_files.write_playlist_files(conn, "pid1")
        m3u = folder / "Road Trip - 2024+25.m3u8"
        assert m3u.read_text(encoding="utf-8") == (
            "#EXTM3U\n"
            "#PLAYLIST:Road Trip: 2024/25\n"
            "#SPOTIFY-SLSK:pid1\n"
            "#EXTINF:-1,Artist - First\n"
            "a.mp3\n"
            "#EXTINF:201,Artist - Ünïcode Song\n"
            "b.flac\n"
        )

    def test_unchanged_content_is_not_rewritten(self, pl):
        conn, folder = pl
        playlist_files.write_playlist_files(conn, "pid1")
        m3u = folder / "Road Trip - 2024+25.m3u8"
        import os
        os.utime(m3u, (0, 0))
        playlist_files.write_playlist_files(conn, "pid1")
        assert m3u.stat().st_mtime == 0

    def test_renamed_playlist_replaces_only_its_own_old_m3u8(self, pl):
        conn, folder = pl
        playlist_files.write_playlist_files(conn, "pid1")
        (folder / "mine.m3u8").write_text("#EXTM3U\nuser playlist\n")
        db.rename_playlist_meta(conn, "pid1", "pl_road_trip", "Summer")
        playlist_files.write_playlist_files(conn, "pid1")
        assert sorted(p.name for p in folder.glob("*.m3u8")) == ["Summer.m3u8", "mine.m3u8"]

    def test_disabled_writes_nothing(self, pl, monkeypatch):
        conn, folder = pl
        monkeypatch.setattr(config, "WRITE_M3U", False)
        monkeypatch.setattr(config, "WRITE_PLAYLIST_COVER", False)
        playlist_files.write_playlist_files(conn, "pid1")
        assert not list(folder.glob("*.m3u8")) and not (folder / "cover.jpg").exists()

    def test_reserved_and_empty_names(self):
        assert playlist_files.m3u8_name("CON") == "CON_.m3u8"
        assert playlist_files.m3u8_name("...") == "playlist.m3u8"


class TestCover:
    def test_playlist_image_written_as_cover_jpg(self, pl, monkeypatch):
        conn, folder = pl
        db.set_playlist_cover_url(conn, "pid1", "https://mosaic.scdn.co/640/abc")
        seen = []
        monkeypatch.setattr(
            playlist_files, "fetch_image", lambda url, key: seen.append((url, key)) or COVER
        )
        playlist_files.write_playlist_files(conn, "pid1")
        assert (folder / "cover.jpg").read_bytes() == COVER
        assert seen == [("https://mosaic.scdn.co/640/abc", "playlist_pid1")]

    def test_failed_fetch_leaves_no_cover_and_does_not_raise(self, pl, monkeypatch):
        conn, folder = pl
        db.set_playlist_cover_url(conn, "pid1", "https://mosaic.scdn.co/640/abc")
        monkeypatch.setattr(playlist_files, "fetch_image", lambda *a: None)
        playlist_files.write_playlist_files(conn, "pid1")
        assert not (folder / "cover.jpg").exists()
        assert (folder / "Road Trip - 2024+25.m3u8").exists()

    def test_sync_stores_the_largest_playlist_image(self, pl):
        from spotify_slsk.spotify import sync
        from tests.test_pipeline import _FakeSpotify

        conn, _ = pl

        class Sp(_FakeSpotify):
            def playlist(self, pid, fields=None):
                assert "images" in fields
                return {"name": "Road Trip: 2024/25", "snapshot_id": "s", "images": [
                    {"url": "https://mosaic.scdn.co/60/x", "width": 60, "height": 60},
                    {"url": "https://mosaic.scdn.co/640/x", "width": 640, "height": 640},
                ]}

        sync.fetch_and_compare_tracks(conn, "pid1", Sp("Road Trip: 2024/25", "s", []))
        assert db.list_playlists(conn)[0]["cover_url"] == "https://mosaic.scdn.co/640/x"


class TestIgnoredByTheMatchers:
    def test_startup_check_never_indexes_playlist_files(self, pl):
        conn, folder = pl
        db.set_playlist_cover_url(conn, "pid1", None)
        playlist_files.write_playlist_files(conn, "pid1")
        (folder / "cover.jpg").write_bytes(COVER)
        index = reconcile._index_playlist_files(str(folder), (".mp3", ".flac", ".m4a", ".aiff", ".wav", ".ogg"))
        assert sorted(pathlib.Path(i["path"]).name for i in index) == ["a.mp3", "b.flac"]
