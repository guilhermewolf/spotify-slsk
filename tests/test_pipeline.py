"""The download-to-library pipeline and the Spotify sync diff.

These are the paths that delete and move real files and decide what gets
downloaded, and they had no coverage at all. The metadata boundary
(mutagen) is stubbed so the tests exercise the *decisions* — accept, reject,
delete, move — against real files on a real temp filesystem.
"""
import os

import pytest

import app
import db


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    assert c is not None
    db.create_table(c, "pl_x")
    yield c
    c.close()


def _seed(conn, track_id="t1", name="Heart To Find", artists="Mat Joe", album="Alb"):
    db.insert_track(conn, "pl_x", (track_id, name, artists, album))


def _stub_metadata(monkeypatch, title, artist, album="Alb"):
    monkeypatch.setattr(
        app, "extract_metadata_from_file", lambda p: (title, artist, album)
    )
    monkeypatch.setattr(app, "tag_audio_file", lambda *a, **k: True)


class TestProcessDownloadedFileAccepts:
    def test_good_match_is_tagged_moved_and_marked_downloaded(
        self, conn, monkeypatch, sandbox_dirs
    ):
        downloads, playlists = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, "Heart To Find", "Mat Joe")
        src = downloads / "Mat Joe - Heart To Find.flac"
        src.write_bytes(b"audio")

        ok, final = app.process_downloaded_file(str(src), "pl_x", conn)

        assert ok is True
        assert final == str(playlists / "pl_x" / "Mat Joe - Heart To Find.flac")
        assert os.path.exists(final), "file was not moved into the library"
        assert not src.exists(), "source file left behind in downloads"

        row = db.get_track(conn, "pl_x", "t1")
        assert row["downloaded"] is True
        assert row["path"] == final


class TestProcessDownloadedFileRejects:
    """Rejection deletes the file on a normal download. That is destructive
    and must be exercised deliberately."""

    def test_missing_metadata_is_rejected_and_deleted(
        self, conn, monkeypatch, sandbox_dirs
    ):
        downloads, _ = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, None, None)
        bad = downloads / "junk.flac"
        bad.write_bytes(b"audio")

        ok, final = app.process_downloaded_file(str(bad), "pl_x", conn)

        assert ok is False and final is None
        assert not bad.exists(), "unusable download was not cleaned up"

    def test_low_score_is_rejected_deleted_and_remembered(
        self, conn, monkeypatch, sandbox_dirs
    ):
        downloads, _ = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, "Something Totally Different", "Other Artist")
        monkeypatch.setattr(app, "MIN_MATCH_SCORE", 0.62)
        bad = downloads / "Other Artist - Something Totally Different.flac"
        bad.write_bytes(b"audio")

        ok, _ = app.process_downloaded_file(str(bad), "pl_x", conn)

        assert ok is False
        assert not bad.exists()
        # Remembering the filename is what stops it being re-downloaded.
        assert "Other Artist - Something Totally Different.flac" in db.get_tried_files(
            conn, "pl_x", "t1"
        )

    def test_track_stays_pending_after_rejection(
        self, conn, monkeypatch, sandbox_dirs
    ):
        downloads, _ = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, None, None)
        bad = downloads / "junk.flac"
        bad.write_bytes(b"audio")

        app.process_downloaded_file(str(bad), "pl_x", conn)

        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is False


class TestReconcileModeIsNonDestructive:
    """CLAUDE.md documents that startup reconciliation never deletes.

    This is the guard rail that keeps a bad match from eating the operator's
    existing library on boot.
    """

    def test_reconcile_keeps_a_file_it_cannot_match(
        self, conn, monkeypatch, sandbox_dirs
    ):
        _, playlists = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, "Unrelated Song", "Unrelated Artist")
        existing = playlists / "pl_x" / "Unrelated Artist - Unrelated Song.flac"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"audio")

        ok, _ = app.process_downloaded_file(
            str(existing), "pl_x", conn, reconcile=True
        )

        assert ok is False
        assert existing.exists(), "reconcile deleted a file it should have kept"

    def test_reconcile_keeps_a_file_with_no_metadata(
        self, conn, monkeypatch, sandbox_dirs
    ):
        _, playlists = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, None, None)
        existing = playlists / "pl_x" / "mystery.flac"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"audio")

        app.process_downloaded_file(str(existing), "pl_x", conn, reconcile=True)

        assert existing.exists()

    def test_file_already_in_library_is_not_moved_again(
        self, conn, monkeypatch, sandbox_dirs
    ):
        _, playlists = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, "Heart To Find", "Mat Joe")
        existing = playlists / "pl_x" / "Mat Joe - Heart To Find.flac"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"audio")

        ok, final = app.process_downloaded_file(
            str(existing), "pl_x", conn, reconcile=True
        )

        assert ok is True
        assert final == str(existing)
        assert existing.exists()


class TestMoveTrackToPlaylistFolder:
    def test_creates_destination_and_moves(self, sandbox_dirs):
        downloads, playlists = sandbox_dirs
        src = downloads / "song.flac"
        src.write_bytes(b"audio")

        dest = app.move_track_to_playlist_folder(str(src), "pl_new")

        assert dest == str(playlists / "pl_new" / "song.flac")
        assert os.path.exists(dest) and not src.exists()

    def test_returns_none_when_source_missing(self, sandbox_dirs):
        downloads, _ = sandbox_dirs
        assert app.move_track_to_playlist_folder(
            str(downloads / "nope.flac"), "pl_x"
        ) is None

    def test_falls_back_to_copy_across_devices(self, monkeypatch, sandbox_dirs):
        """shutil.move raises OSError across filesystems; a copy must save it."""
        import shutil

        downloads, playlists = sandbox_dirs
        src = downloads / "song.flac"
        src.write_bytes(b"audio")

        def _cross_device(*a, **k):
            raise OSError("Invalid cross-device link")

        monkeypatch.setattr(shutil, "move", _cross_device)

        dest = app.move_track_to_playlist_folder(str(src), "pl_x")

        assert dest and os.path.exists(dest)


class _FakeSpotify:
    """Minimal stand-in for the spotipy client used by the sync path."""

    def __init__(self, name, snapshot, items):
        self._name = name
        self._snapshot = snapshot
        self._items = items
        self.track_fetches = 0

    def playlist(self, pid, fields=None):
        return {"name": self._name, "snapshot_id": self._snapshot}

    def playlist_tracks(self, pid, offset=0, limit=100):
        self.track_fetches += 1
        return {"items": self._items[offset:offset + limit]}


def _item(tid, name="Song", artist="Artist", album="Album"):
    return {
        "track": {
            "id": tid,
            "name": name,
            "album": {"name": album},
            "artists": [{"name": artist}],
        }
    }


class TestSpotifySyncDiff:
    def test_inserts_only_genuinely_new_tracks(self, conn):
        _seed(conn, "existing", name="Old")
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", "snap-old")
        sp = _FakeSpotify("X", "snap-new", [_item("existing"), _item("brand-new")])

        new_tracks, table = app.fetch_and_compare_tracks(conn, "pid", sp)

        assert table == "pl_x"
        assert [t.id for t in new_tracks] == ["brand-new"]
        assert {r[0] for r in db.fetch_all_tracks(conn, "pl_x")} == {
            "existing",
            "brand-new",
        }

    def test_unchanged_snapshot_skips_the_expensive_track_fetch(self, conn):
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", "same-snap")
        sp = _FakeSpotify("X", "same-snap", [_item("a")])

        new_tracks, _ = app.fetch_and_compare_tracks(conn, "pid", sp)

        assert new_tracks == []
        assert sp.track_fetches == 0, "paginated fetch ran despite an unchanged snapshot"

    def test_first_sync_fetches_even_though_stored_snapshot_is_null(self, conn):
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", None)
        sp = _FakeSpotify("X", "snap", [_item("a")])

        new_tracks, _ = app.fetch_and_compare_tracks(conn, "pid", sp)

        assert [t.id for t in new_tracks] == ["a"]
        # Pagination stops on the first empty page, so one item costs two calls.
        assert sp.track_fetches >= 1

    def test_snapshot_is_stamped_after_a_successful_sync(self, conn):
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", None)
        sp = _FakeSpotify("X", "snap-123", [_item("a")])

        app.fetch_and_compare_tracks(conn, "pid", sp)

        assert db.get_playlist_meta(conn, "pid")[0] == "snap-123"

    def test_items_without_a_track_id_are_skipped(self, conn):
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", None)
        # Local files and removed tracks arrive as null/idless entries.
        sp = _FakeSpotify(
            "X", "snap", [{"track": None}, {"track": {"id": None}}, _item("ok")]
        )

        new_tracks, _ = app.fetch_and_compare_tracks(conn, "pid", sp)

        assert [t.id for t in new_tracks] == ["ok"]

    def test_a_track_removed_upstream_is_retained_locally(self, conn):
        """Documents current behaviour: the local library is append-only.

        Removing a track from the Spotify playlist does not delete the row or
        the file. If that ever becomes undesirable, this test should be the
        thing that fails.
        """
        _seed(conn, "gone", name="Removed Upstream")
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", "old")
        sp = _FakeSpotify("X", "new", [_item("still-here")])

        app.fetch_and_compare_tracks(conn, "pid", sp)

        assert "gone" in {r[0] for r in db.fetch_all_tracks(conn, "pl_x")}


class TestStartupReconciliation:
    def test_matches_a_file_on_disk_and_marks_it_downloaded(
        self, conn, monkeypatch, sandbox_dirs
    ):
        _, playlists = sandbox_dirs
        _seed(conn, "t1", name="Heart To Find", artists="Mat Joe")
        target = playlists / "pl_x" / "Mat Joe - Heart To Find.flac"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"audio")
        # Filenames carry the metadata; no real tags in a stub file.
        monkeypatch.setattr(app, "_read_audio_tags_safe", lambda p: ("", ""))

        app.startup_check(conn, "pl_x")

        row = db.get_track(conn, "pl_x", "t1")
        assert row["downloaded"] is True
        assert row["path"] == str(target)

    def test_no_folder_is_a_no_op(self, conn, sandbox_dirs):
        _seed(conn)
        app.startup_check(conn, "pl_x")  # must not raise
        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is False

    def test_verified_existing_path_is_left_alone(
        self, conn, monkeypatch, sandbox_dirs
    ):
        _, playlists = sandbox_dirs
        _seed(conn, "t1", name="Heart To Find", artists="Mat Joe")
        target = playlists / "pl_x" / "Mat Joe - Heart To Find.flac"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"audio")
        db.update_download_status(
            conn, "t1", "pl_x", success=True, file_path=str(target)
        )
        monkeypatch.setattr(app, "_read_audio_tags_safe", lambda p: ("", ""))

        writes = []
        monkeypatch.setattr(
            app, "update_download_status", lambda *a, **k: writes.append(1)
        )

        app.startup_check(conn, "pl_x")

        assert writes == [], "re-wrote a row whose stored path was already correct"
