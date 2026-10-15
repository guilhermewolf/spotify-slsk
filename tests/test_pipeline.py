"""The download-to-library pipeline and the Spotify sync diff.

These are the paths that delete and move real files and decide what gets
downloaded, and they had no coverage at all. The metadata boundary
(mutagen) is stubbed so the tests exercise the *decisions* — accept, reject,
delete, move — against real files on a real temp filesystem.
"""
import os

import pytest

from spotify_slsk import app
from spotify_slsk import db


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


class TestDurationSync:
    def test_new_tracks_store_duration_and_old_rows_are_backfilled(self, conn):
        _seed(conn, "old")  # pre-v5 row: no duration
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", None)
        old, new = _item("old"), _item("new")
        old["track"]["duration_ms"] = 200_000
        new["track"]["duration_ms"] = 242_000
        sp = _FakeSpotify("X", "snap", [old, new])

        new_tracks, _ = app.fetch_and_compare_tracks(conn, "pid", sp)

        assert new_tracks[0].duration_ms == 242_000
        assert db.get_track(conn, "pl_x", "new")["duration_ms"] == 242_000
        assert db.get_track(conn, "pl_x", "old")["duration_ms"] == 200_000
        assert db.get_pending_tracks(conn, "pl_x")[0].duration_ms is not None


class TestUntaggedFallback:
    """An untagged download is accepted only if the slskd filename AND the
    real audio duration both match the track that was requested."""

    def _track(self, duration_ms=242_000):
        from spotify_slsk.models import Track
        return Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x", duration_ms=duration_ms)

    def _setup(self, conn, monkeypatch, sandbox_dirs, filename, length):
        from spotify_slsk import soulseek_api
        downloads, playlists = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, None, None)
        monkeypatch.setattr(app, "_audio_length_seconds", lambda p: length)
        monkeypatch.setattr(soulseek_api, "MAX_DURATION_DIFF_SECONDS", 15)
        monkeypatch.setattr(app, "MIN_MATCH_SCORE", 0.62)
        src = downloads / filename
        src.write_bytes(b"audio")
        return src, playlists

    def test_accepts_when_filename_and_duration_match(self, conn, monkeypatch, sandbox_dirs):
        src, playlists = self._setup(
            conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.flac", 245.0
        )
        ok, final = app.process_downloaded_file(
            str(src), "pl_x", conn, expected_track=self._track()
        )
        assert ok is True
        assert final == str(playlists / "pl_x" / "Mat Joe - Heart To Find.flac")
        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is True

    def test_rejects_and_deletes_on_duration_mismatch(self, conn, monkeypatch, sandbox_dirs):
        src, _ = self._setup(
            conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.flac", 120.0
        )
        ok, _ = app.process_downloaded_file(
            str(src), "pl_x", conn, expected_track=self._track()
        )
        assert ok is False
        assert not src.exists()
        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is False
        assert "Mat Joe - Heart To Find.flac" in db.get_tried_files(conn, "pl_x", "t1")

    def test_rejects_on_filename_mismatch(self, conn, monkeypatch, sandbox_dirs):
        src, _ = self._setup(
            conn, monkeypatch, sandbox_dirs, "Other Guy - Something Else.flac", 242.0
        )
        ok, _ = app.process_downloaded_file(
            str(src), "pl_x", conn, expected_track=self._track()
        )
        assert ok is False and not src.exists()

    def test_rejects_without_spotify_duration(self, conn, monkeypatch, sandbox_dirs):
        src, _ = self._setup(
            conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.flac", 242.0
        )
        ok, _ = app.process_downloaded_file(
            str(src), "pl_x", conn, expected_track=self._track(duration_ms=None)
        )
        assert ok is False

    def test_filename_uses_strict_rules(self, conn, monkeypatch, sandbox_dirs):
        # A different version passes a fuzzy filename score but not the
        # wrong-song rules (symmetric version keywords).
        src, _ = self._setup(
            conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find (Dub Mix).flac", 242.0
        )
        ok, _ = app.process_downloaded_file(
            str(src), "pl_x", conn, expected_track=self._track()
        )
        assert ok is False

    def test_peer_folder_may_supply_the_artist(self, conn, monkeypatch, sandbox_dirs):
        src, _ = self._setup(
            conn, monkeypatch, sandbox_dirs, "01 - Heart To Find.flac", 242.0
        )
        ok, _ = app.process_downloaded_file(
            str(src), "pl_x", conn, expected_track=self._track(),
            source={"user": "p", "filename": "@@m\\Mat Joe\\Single\\01 - Heart To Find.flac"},
        )
        assert ok is True

    def test_reconcile_never_uses_fallback_or_deletes(self, conn, monkeypatch, sandbox_dirs):
        src, _ = self._setup(
            conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.flac", 242.0
        )
        ok, _ = app.process_downloaded_file(
            str(src), "pl_x", conn, reconcile=True, expected_track=self._track()
        )
        assert ok is False and src.exists()


class TestRejectionBlocklists:
    def test_rejected_download_blocklists_its_source(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, "Something Totally Different", "Other Artist")
        bad = downloads / "x.flac"
        bad.write_bytes(b"audio")
        source = {"user": "peer", "filename": "@@a\\x.flac", "size": 5}

        app.process_downloaded_file(str(bad), "pl_x", conn, source=source)

        assert db.is_blocklisted(conn, "peer", "@@a\\x.flac")

    def test_reconcile_never_blocklists(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, None, None)
        f = downloads / "x.flac"
        f.write_bytes(b"audio")
        app.process_downloaded_file(
            str(f), "pl_x", conn, reconcile=True, source={"user": "peer", "filename": "x"}
        )
        assert db.list_blocklist(conn) == []


class TestHistoryEvents:
    def test_import_and_rejection_are_recorded(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _seed(conn)
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", "s")
        _stub_metadata(monkeypatch, "Something Totally Different", "Other Artist")
        bad = downloads / "bad.flac"
        bad.write_bytes(b"audio")
        app.process_downloaded_file(str(bad), "pl_x", conn, source={"user": "peer", "filename": "bad.flac"})

        _stub_metadata(monkeypatch, "Heart To Find", "Mat Joe")
        good = downloads / "good.flac"
        good.write_bytes(b"audio")
        app.process_downloaded_file(str(good), "pl_x", conn, source={"user": "peer2", "filename": "good.flac"})

        events = [(r["event"], r["playlist_id"]) for r in db.list_history(conn)]
        assert events == [
            ("imported", "pid"), ("deleted_file", "pid"), ("import_rejected", "pid"),
        ]
        assert db.list_history(conn, event="imported")[0]["data"]["username"] == "peer2"

    def test_reconcile_records_nothing(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, None, None)
        f = downloads / "x.flac"
        f.write_bytes(b"audio")
        app.process_downloaded_file(str(f), "pl_x", conn, reconcile=True)
        assert db.list_history(conn) == []

    def test_nothing_acceptable_records_download_failed(self, conn, monkeypatch):
        from spotify_slsk import soulseek_api
        monkeypatch.setattr(soulseek_api, "_run_one_search", lambda q, timeout=60: [])
        soulseek_api.search_and_download("Mat Joe", "Heart To Find", conn, "pl_x", "t1")
        rows = db.list_history(conn)
        assert [r["event"] for r in rows] == ["download_failed"]
        assert rows[0]["data"]["reason"].startswith("no acceptable candidate")

    def test_upgrade_pass_finding_nothing_is_not_a_failure(self, conn, monkeypatch):
        from spotify_slsk import soulseek_api
        monkeypatch.setattr(soulseek_api, "_run_one_search", lambda q, timeout=60: [])
        soulseek_api.search_and_download(
            "Mat Joe", "Heart To Find", conn, "pl_x", "t1", max_version_tier=1
        )
        assert db.list_history(conn) == []


class TestManualGrabs:
    def test_daemon_downloads_and_imports_a_queued_grab(self, conn, monkeypatch, sandbox_dirs):
        from spotify_slsk import runtime_state, soulseek_api
        downloads, playlists = sandbox_dirs
        _seed(conn)
        _stub_metadata(monkeypatch, "Heart To Find", "Mat Joe")
        src = downloads / "Mat Joe - Heart To Find.flac"
        src.write_bytes(b"audio")
        seen = {}

        def fake_grab(candidate, c, table, tid, source="auto"):
            seen.update(table=table, tid=tid, source=source)
            return str(src)

        monkeypatch.setattr(soulseek_api, "grab_candidate", fake_grab)
        runtime_state.pop_manual_grabs()
        runtime_state.enqueue_manual_grab({
            "playlist_id": "pid", "table_name": "pl_x", "track_id": "t1",
            "candidate": {"user": "peer", "filename": "Mat Joe - Heart To Find.flac", "size": 5},
        })

        app._process_manual_grabs(conn)

        assert seen == {"table": "pl_x", "tid": "t1", "source": "manual"}
        row = db.get_track(conn, "pl_x", "t1")
        assert row["downloaded"] is True
        assert row["path"] == str(playlists / "pl_x" / "Mat Joe - Heart To Find.flac")
        assert runtime_state.pop_manual_grabs() == []

    def test_failed_manual_grab_does_not_count_an_attempt(self, conn, monkeypatch):
        from spotify_slsk import runtime_state, soulseek_api
        _seed(conn)
        monkeypatch.setattr(soulseek_api, "grab_candidate", lambda *a, **k: None)
        runtime_state.enqueue_manual_grab({
            "playlist_id": "pid", "table_name": "pl_x", "track_id": "t1",
            "candidate": {"user": "peer", "filename": "x.flac", "size": 5},
        })
        app._process_manual_grabs(conn)
        assert db.get_track(conn, "pl_x", "t1")["attempts"] == 0

    def test_manual_grab_replaces_old_file_only_for_its_own_track(self, conn, monkeypatch, sandbox_dirs):
        from spotify_slsk import runtime_state, soulseek_api
        downloads, playlists = sandbox_dirs
        _seed(conn)
        _seed(conn, "t2", name="Another Song", artists="Someone")
        old = playlists / "old.flac"
        old.write_bytes(b"old")
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(old))
        # The grabbed file's tags match t2, not t1.
        _stub_metadata(monkeypatch, "Another Song", "Someone")
        src = downloads / "Someone - Another Song.flac"
        src.write_bytes(b"audio")
        monkeypatch.setattr(soulseek_api, "grab_candidate", lambda *a, **k: str(src))
        runtime_state.enqueue_manual_grab({
            "playlist_id": "pid", "table_name": "pl_x", "track_id": "t1",
            "candidate": {"user": "peer", "filename": "x.flac", "size": 5},
        })
        app._process_manual_grabs(conn)
        assert old.exists(), "t1's file must survive a grab that imported as t2"


class TestVerifiesTheRequestedTrackOnly:
    """A download is verified against the track it was requested for.

    Scoring the file against every row and marking the best one assigned
    wrong files to *other* tracks (306 paths shared by 635 rows in
    production) while the requested track stayed pending.
    """

    def test_file_for_another_row_is_rejected_not_reassigned(
        self, conn, monkeypatch, sandbox_dirs
    ):
        downloads, _ = sandbox_dirs
        _seed(conn, "t1", "Heart To Find", "Mat Joe")
        _seed(conn, "t2", "Lose Yourself", "Eminem")
        _stub_metadata(monkeypatch, "Lose Yourself", "Eminem")
        f = downloads / "Eminem - Lose Yourself.flac"
        f.write_bytes(b"audio")

        ok, _ = app.process_downloaded_file(str(f), "pl_x", conn, track_id="t1")

        assert ok is False
        assert not f.exists()
        assert db.get_track(conn, "pl_x", "t2")["downloaded"] is False
        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is False
        assert "Eminem - Lose Yourself.flac" in db.get_tried_files(conn, "pl_x", "t1")

    def test_matching_file_marks_the_requested_row(
        self, conn, monkeypatch, sandbox_dirs
    ):
        downloads, _ = sandbox_dirs
        _seed(conn, "t1", "Heart To Find", "Mat Joe")
        _seed(conn, "t2", "Heart To Find", "Mat Joe")  # duplicate entry
        _stub_metadata(monkeypatch, "Heart To Find", "Mat Joe")
        f = downloads / "Mat Joe - Heart To Find.flac"
        f.write_bytes(b"audio")

        ok, _ = app.process_downloaded_file(str(f), "pl_x", conn, track_id="t2")

        assert ok is True
        assert db.get_track(conn, "pl_x", "t2")["downloaded"] is True
        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is False

    def test_handle_track_download_passes_the_track_id(self, conn, monkeypatch):
        from spotify_slsk.models import Track

        seen = {}
        monkeypatch.setattr(app, "search_and_download", lambda **k: "/x.flac")

        def _process(path, pl, c, reconcile=False, track_id=None, expected_track=None, source=None):
            # Tier 1 passes the Track itself (its id is the requested track).
            seen["track_id"] = track_id or expected_track.id
            return False, None

        monkeypatch.setattr(app, "process_downloaded_file", _process)
        _seed(conn, "t1")
        app.handle_track_download(
            Track("t1", "Heart To Find", "Mat Joe", "Alb", "p"), "pl_x", conn
        )
        assert seen["track_id"] == "t1"


class TestUpgradeReplacesOnlyItsOwnFile:
    def _setup(self, conn, monkeypatch, sandbox_dirs):
        from spotify_slsk.models import Track
        _, playlists = sandbox_dirs
        _seed(conn)
        old = playlists / "Mat Joe - Heart To Find.flac"
        old.write_bytes(b"old")
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(old))
        new = playlists / "Mat Joe - Heart To Find (Extended Mix).flac"
        new.write_bytes(b"new")
        monkeypatch.setattr(app, "search_and_download", lambda **k: str(new))
        return Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"), old, new

    def test_old_file_kept_when_import_did_not_land_on_this_track(self, conn, monkeypatch, sandbox_dirs):
        track, old, new = self._setup(conn, monkeypatch, sandbox_dirs)
        # Verified, but the row still points at the old file.
        monkeypatch.setattr(app, "process_downloaded_file", lambda *a, **k: (True, str(new)))
        app.try_upgrade_track(track, "pl_x", conn, str(old))
        assert old.exists()

    def test_old_file_replaced_when_import_landed_here(self, conn, monkeypatch, sandbox_dirs):
        track, old, new = self._setup(conn, monkeypatch, sandbox_dirs)

        def _process(*a, **k):
            db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(new))
            return True, str(new)

        monkeypatch.setattr(app, "process_downloaded_file", _process)
        assert app.try_upgrade_track(track, "pl_x", conn, str(old)) is True
        assert not old.exists() and new.exists()

    def test_same_path_is_never_deleted(self, conn, monkeypatch, sandbox_dirs):
        track, old, _ = self._setup(conn, monkeypatch, sandbox_dirs)
        monkeypatch.setattr(app, "process_downloaded_file", lambda *a, **k: (True, str(old)))
        app.try_upgrade_track(track, "pl_x", conn, str(old))
        assert old.exists()
