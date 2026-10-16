"""Full Spotify tagging (Tier 5): one tagger for import, re-tag and preview."""
from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from spotify_slsk import config, db
from spotify_slsk.importer import artwork, tags
from spotify_slsk.importer import process as importer_process
from spotify_slsk.spotify import sync as spotify_sync

AUDIO = pathlib.Path(__file__).parent / "fixtures" / "audio"
COVER = (AUDIO / "cover.jpg").read_bytes()

ROW = {
    "id": "4uLU6hMCjMI75M1A2tKUQC",
    "name": "Desires Ignite - Radio Edit",
    "artists": "Reber, Kris M",
    "artist_list": ["Reber", "Kris M"],
    "album": "Desires Ignite",
    "album_artist": "Reber",
    "track_number": 2,
    "track_total": 5,
    "disc_number": 1,
    "release_date": "2021-03-05",
    "isrc": "GB-AYE-06-01498",
    "album_id": "alb1",
    "cover_url": "https://i.scdn.co/image/abc",
}


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    assert c is not None
    yield c
    c.close()


def _copy(tmp_path, ext):
    dest = tmp_path / f"song.{ext}"
    shutil.copy(AUDIO / f"silence.{ext}", dest)
    return str(dest)


class TestSpotifyTags:
    def test_builds_every_field(self):
        t = tags.spotify_tags(ROW)
        assert t == {
            "title": ["Desires Ignite - Radio Edit"],
            "artist": ["Reber", "Kris M"],
            "album": ["Desires Ignite"],
            "album_artist": ["Reber"],
            "track": ["2/5"],
            "disc": ["1"],
            "date": ["2021-03-05"],
            "isrc": ["GBAYE0601498"],
            "spotify_track_id": ["4uLU6hMCjMI75M1A2tKUQC"],
        }

    def test_unknown_fields_are_left_out_never_blanked(self):
        # A pre-v8 row (not resynced yet): only what the DB knows.
        t = tags.spotify_tags({"id": "x", "name": "T", "artists": "A, B", "album": "Al"})
        assert t == {
            "title": ["T"], "artist": ["A, B"], "album": ["Al"], "spotify_track_id": ["x"],
        }

    def test_isrc_override_empty_keeps_the_files(self):
        assert "isrc" not in tags.spotify_tags(ROW, isrc="")
        assert tags.spotify_tags(ROW, isrc="USUM71703861")["isrc"] == ["USUM71703861"]


@pytest.mark.parametrize("ext", ["mp3", "flac", "m4a", "aiff"])
class TestRoundTrip:
    def test_writes_and_reads_back_with_cover(self, tmp_path, ext):
        path = _copy(tmp_path, ext)
        before, cover_before = tags.read_tags(path)
        assert before == {} and cover_before is False

        wanted = tags.spotify_tags(ROW)
        tags.write_tags(path, wanted, COVER)

        got, has_cover = tags.read_tags(path)
        assert got == wanted
        assert has_cover is True
        assert tags.read_isrc(path) == "GBAYE0601498"
        assert all(not r["changed"] for r in tags.tag_diff(got, wanted))

    def test_rewrite_replaces_not_appends(self, tmp_path, ext):
        path = _copy(tmp_path, ext)
        tags.write_tags(path, tags.spotify_tags(ROW), COVER)
        tags.write_tags(path, {"title": ["New"], "artist": ["Solo"]}, COVER)
        got, _ = tags.read_tags(path)
        assert got["title"] == ["New"] and got["artist"] == ["Solo"]
        assert got["album"] == ["Desires Ignite"]  # absent key: left alone


class TestMp3Specifics:
    def test_id3v24_multi_artist_and_spotify_id_frame(self, tmp_path):
        from mutagen.id3 import ID3

        path = _copy(tmp_path, "mp3")
        tags.write_tags(path, tags.spotify_tags(ROW), COVER)
        id3 = ID3(path)
        assert id3.version[:2] == (2, 4)
        assert id3.getall("TPE1")[0].text == ["Reber", "Kris M"]
        assert id3.getall("TXXX:SPOTIFY_TRACK_ID")[0].text == [ROW["id"]]
        apic = id3.getall("APIC")
        assert len(apic) == 1 and apic[0].type == 3 and apic[0].mime == "image/jpeg"


class TestTagDiff:
    def test_marks_changes_and_keeps_unknowns(self):
        rows = tags.tag_diff(
            {"title": ["Old"], "artist": ["Reber"], "date": ["1999"]},
            {"title": ["New"], "artist": ["Reber"]},
        )
        by_key = {r["key"]: r for r in rows}
        assert by_key["title"]["changed"] and by_key["title"]["new"] == "New"
        assert not by_key["artist"]["changed"]
        assert not by_key["date"]["changed"] and by_key["date"]["new"] == "1999"


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]


class TestArtwork:
    @pytest.fixture(autouse=True)
    def _cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARTWORK_CACHE_DIR", str(tmp_path))

    def test_fetches_once_then_serves_from_cache(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            artwork.requests, "get",
            lambda url, timeout, stream, allow_redirects: calls.append(
                (url, timeout, allow_redirects)) or _Resp(COVER),
        )
        assert artwork.album_cover(ROW) == COVER
        assert artwork.album_cover(ROW) == COVER
        assert calls == [(ROW["cover_url"], artwork.TIMEOUT_SECONDS, False)]
        assert [p.name.startswith("album_alb1_") for p in tmp_path.iterdir()] == [True]

    @pytest.mark.parametrize("body,status", [
        (b"<html>not an image</html>", 200),
        (COVER, 404),
        (COVER, 302),  # redirects are not followed
        (b"\xff\xd8\xff" + b"0" * (artwork.MAX_BYTES + 1), 200),
    ])
    def test_bad_responses_are_none_and_not_cached(self, monkeypatch, tmp_path, body, status):
        monkeypatch.setattr(artwork.requests, "get", lambda *a, **k: _Resp(body, status))
        assert artwork.album_cover(ROW) is None
        assert not list(tmp_path.glob("*.img"))

    def test_failure_is_remembered_for_a_day(self, monkeypatch, tmp_path):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise TimeoutError("slow")

        monkeypatch.setattr(artwork.requests, "get", boom)
        assert artwork.album_cover(ROW) is None
        assert artwork.album_cover(ROW) is None
        assert len(calls) == 1
        fail = next(tmp_path.glob("*.fail"))
        os.utime(fail, (0, 0))  # a day later
        monkeypatch.setattr(artwork.requests, "get", lambda *a, **k: _Resp(COVER))
        assert artwork.album_cover(ROW) == COVER
        assert not list(tmp_path.glob("*.fail"))

    @pytest.mark.parametrize("url,ok", [
        ("https://i.scdn.co/image/ab67616d0000b273", True),
        ("https://mosaic.scdn.co/640/ab67616d", True),
        ("https://image-cdn-ak.spotifycdn.com/image/ab67706c", True),
        ("https://image-cdn-fa.spotifycdn.com/image/ab67706c", True),
        ("http://i.scdn.co/image/x", False),
        ("https://evil.example/i.scdn.co/x", False),
        ("https://i.scdn.co.evil.example/x", False),
        ("https://notscdn.co/x", False),
        (None, False),
    ])
    def test_only_spotify_image_hosts(self, url, ok, monkeypatch):
        monkeypatch.setattr(artwork.requests, "get", lambda *a, **k: _Resp(COVER))
        assert artwork.allowed_url(url) is ok
        assert (artwork.fetch_image(url, "k") == COVER) is ok

    def test_cache_is_capped_least_recently_used_first(self, monkeypatch, tmp_path):
        monkeypatch.setattr(artwork, "CACHE_MAX_BYTES", 2 * len(COVER))
        monkeypatch.setattr(artwork.requests, "get", lambda *a, **k: _Resp(COVER))
        artwork.fetch_image("https://i.scdn.co/image/0", "k0")
        artwork.fetch_image("https://i.scdn.co/image/1", "k1")
        os.utime(next(tmp_path.glob("k1_*.img")), (1, 1))
        artwork.fetch_image("https://i.scdn.co/image/0", "k0")  # cache hit refreshes k0
        artwork.fetch_image("https://i.scdn.co/image/2", "k2")
        names = sorted(p.name.split("_")[0] for p in tmp_path.glob("*.img"))
        assert names == ["k0", "k2"]


class TestImportTagsFromSpotify:
    def test_import_writes_spotify_tags_and_cover(self, conn, monkeypatch, sandbox_dirs):
        downloads, playlists = sandbox_dirs
        db.create_table(conn, "pl_x")
        fields = {k: v for k, v in ROW.items() if k in db.SPOTIFY_COLUMNS}
        fields["artist_names"] = '["Reber", "Kris M"]'
        db.insert_track(conn, "pl_x", (ROW["id"], ROW["name"], ROW["artists"], ROW["album"], None, ROW["isrc"]), fields)
        f = downloads / "Reber - Desires Ignite (Radio Edit).mp3"
        shutil.copy(AUDIO / "silence.mp3", f)
        # The peer's tags: right song, sloppy metadata.
        monkeypatch.setattr(
            importer_process, "extract_metadata_from_file",
            lambda p: ("Desires Ignite (Radio Edit)", "Reber", "Some Promo"),
        )
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        monkeypatch.setattr(config, "EMBED_COVER_ART", True)
        monkeypatch.setattr(artwork, "fetch_image", lambda url, key: COVER)

        ok, final = importer_process.process_downloaded_file(str(f), "pl_x", conn, track_id=ROW["id"])

        assert ok and final and os.path.isfile(final)
        got, has_cover = tags.read_tags(final)
        assert got["album"] == ["Desires Ignite"]
        assert got["artist"] == ["Reber", "Kris M"]
        assert got["track"] == ["2/5"] and got["spotify_track_id"] == [ROW["id"]]
        assert has_cover

    def test_cover_disabled_is_not_fetched(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"), {"cover_url": "https://x/y"})
        f = downloads / "Artist - Song.mp3"
        shutil.copy(AUDIO / "silence.mp3", f)
        monkeypatch.setattr(importer_process, "extract_metadata_from_file", lambda p: ("Song", "Artist", None))
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        monkeypatch.setattr(config, "EMBED_COVER_ART", False)
        monkeypatch.setattr(artwork, "fetch_image", lambda *a: pytest.fail("fetched"))
        ok, final = importer_process.process_downloaded_file(str(f), "pl_x", conn, track_id="t1")
        assert ok and tags.read_tags(final) == (
            {"title": ["Song"], "artist": ["Artist"], "album": ["Album"], "spotify_track_id": ["t1"]},
            False,
        )


class TestSyncStoresTaggingFields:
    def test_new_rows_get_fields_and_old_rows_are_backfilled(self, conn):
        from tests.test_pipeline import _FakeSpotify, _item

        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("old", "Old", "A", "Alb"))
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", None)
        new = _item("new", artist="Reber")
        new["track"]["artists"].append({"name": "Kris M"})
        new["track"].update(track_number=2, disc_number=1)
        new["track"]["album"].update(
            id="alb1", total_tracks=5, release_date="2021-03-05",
            artists=[{"name": "Reber"}],
            images=[
                {"url": "https://i.scdn.co/small", "width": 64, "height": 64},
                {"url": "https://i.scdn.co/big", "width": 640, "height": 640},
            ],
        )
        old = _item("old")
        old["track"]["album"]["artists"] = [{"name": "Various Artists"}]

        spotify_sync.fetch_and_compare_tracks(conn, "pid", _FakeSpotify("X", "s", [old, new]))

        row = db.get_track(conn, "pl_x", "new")
        assert row["artist_list"] == ["Reber", "Kris M"]
        assert (row["album_artist"], row["track_number"], row["track_total"]) == ("Reber", 2, 5)
        assert (row["disc_number"], row["release_date"], row["album_id"]) == (1, "2021-03-05", "alb1")
        assert row["cover_url"] == "https://i.scdn.co/big"
        assert row["position"] == 2
        old_row = db.get_track(conn, "pl_x", "old")
        assert (old_row["album_artist"], old_row["position"]) == ("Various Artists", 1)


class TestRetagAndPreview:
    @pytest.fixture
    def app_with_track(self, tmp_path, monkeypatch):
        from spotify_slsk.webui import create_app

        path = _copy(tmp_path, "flac")
        tags.write_tags(path, {"title": ["desires ignite radio edit"], "artist": ["REBER"]})
        db_path = str(tmp_path / "ui.db")
        c = db.create_connection(db_path)
        db.upsert_playlist_meta(c, "pid1", "pl_x", "X", "s")
        db.create_table(c, "pl_x")
        fields = {k: v for k, v in ROW.items() if k in db.SPOTIFY_COLUMNS}
        fields["artist_names"] = '["Reber", "Kris M"]'
        db.insert_track(c, "pl_x", (ROW["id"], ROW["name"], ROW["artists"], ROW["album"], None, ROW["isrc"]), fields)
        db.update_download_status(c, ROW["id"], "pl_x", success=True, file_path=path)
        c.close()
        monkeypatch.setattr(artwork, "fetch_image", lambda url, key: COVER)
        app = create_app(db_path, spotify_client=None)
        app.config.update(TESTING=True)
        return app.test_client(), path

    def test_preview_shows_current_vs_new_without_writing(self, app_with_track):
        client, path = app_with_track
        before = pathlib.Path(path).read_bytes()
        html = client.get(f"/track/pid1/{ROW['id']}").get_data(as_text=True)
        assert "would change on re-tag" in html
        assert "desires ignite radio edit" in html  # current
        assert "Reber; Kris M" in html and "2/5" in html  # what re-tag writes
        assert "Spotify album cover" in html
        assert pathlib.Path(path).read_bytes() == before

    def test_retag_uses_the_same_tagger(self, app_with_track):
        from tests.test_webui import post

        client, path = app_with_track
        r = post(client, f"/track/pid1/{ROW['id']}/retag", follow_redirects=True)
        assert b"with cover art" in r.data
        got, has_cover = tags.read_tags(path)
        assert got == tags.spotify_tags(ROW) and has_cover
        html = client.get(f"/track/pid1/{ROW['id']}").get_data(as_text=True)
        assert "1 field(s) would change" in html  # only the cover row (always rewritten)
