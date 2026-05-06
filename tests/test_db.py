"""Unit tests for db.py — uses an on-disk temp SQLite file (not :memory:).

Using a file is necessary because WAL mode on :memory: is silently demoted.
"""
import os
import tempfile

os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

import pytest

import db


@pytest.fixture
def conn():
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "test.db")
    c = db.create_connection(path)
    assert c is not None
    yield c
    c.close()


class TestConnectionPragmas:
    def test_wal_enabled(self, conn):
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_busy_timeout_set(self, conn):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    def test_user_version_initialized(self, conn):
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


class TestPlaylistsMeta:
    def test_initial_read_returns_none_tuple(self, conn):
        assert db.get_playlist_meta(conn, "nope") == (None, None, None, None)

    def test_upsert_then_read(self, conn):
        db.upsert_playlist_meta(conn, "pid1", "pl_my_list", "My List", "snap-abc")
        snap, table, name, last = db.get_playlist_meta(conn, "pid1")
        assert snap == "snap-abc"
        assert table == "pl_my_list"
        assert name == "My List"
        assert last is not None

    def test_upsert_updates_existing(self, conn):
        db.upsert_playlist_meta(conn, "pid1", "pl_my_list", "My List", "snap-v1")
        db.upsert_playlist_meta(conn, "pid1", "pl_my_list", "My List", "snap-v2")
        snap, _, _, _ = db.get_playlist_meta(conn, "pid1")
        assert snap == "snap-v2"

    def test_added_at_preserved_on_update(self, conn):
        db.upsert_playlist_meta(conn, "pid1", "pl_a", "A", "snap-v1")
        first = conn.execute(
            "SELECT added_at FROM playlists_meta WHERE playlist_id=?", ("pid1",)
        ).fetchone()[0]
        db.upsert_playlist_meta(conn, "pid1", "pl_a", "A", "snap-v2")
        later = conn.execute(
            "SELECT added_at FROM playlists_meta WHERE playlist_id=?", ("pid1",)
        ).fetchone()[0]
        assert first == later

    def test_list_playlists_with_enabled_filter(self, conn):
        db.upsert_playlist_meta(conn, "p1", "pl_a", "A", "s1")
        db.upsert_playlist_meta(conn, "p2", "pl_b", "B", "s2")
        db.set_playlist_enabled(conn, "p1", False)
        all_pls = db.list_playlists(conn)
        enabled_only = db.list_playlists(conn, only_enabled=True)
        assert len(all_pls) == 2
        assert [p["playlist_id"] for p in enabled_only] == ["p2"]

    def test_remove_playlist_drops_tables(self, conn):
        db.upsert_playlist_meta(conn, "p1", "pl_a", "A", "s1")
        db.create_table(conn, "pl_a")
        db.insert_track(conn, "pl_a", ("t1", "S", "A", "Alb"))
        assert db.remove_playlist(conn, "p1") is True
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "pl_a" not in tables
        assert "pl_a_tried" not in tables

    def test_remove_missing_playlist_returns_false(self, conn):
        assert db.remove_playlist(conn, "nope") is False


class TestSettings:
    def test_default_when_unset(self, conn):
        assert db.get_setting(conn, "UNSET_KEY", "fallback") == "fallback"
        assert db.get_setting(conn, "UNSET_KEY") is None

    def test_roundtrip(self, conn):
        db.set_setting(conn, "FOO", "bar")
        assert db.get_setting(conn, "FOO") == "bar"

    def test_db_overrides_env(self, conn, monkeypatch):
        monkeypatch.setenv("SOME_ENV_KEY", "from-env")
        assert db.get_setting(conn, "SOME_ENV_KEY") == "from-env"
        db.set_setting(conn, "SOME_ENV_KEY", "from-db")
        assert db.get_setting(conn, "SOME_ENV_KEY") == "from-db"

    def test_set_none_deletes(self, conn):
        db.set_setting(conn, "FOO", "bar")
        db.set_setting(conn, "FOO", None)
        assert db.get_setting(conn, "FOO") is None

    def test_delete_setting(self, conn):
        db.set_setting(conn, "FOO", "bar")
        db.delete_setting(conn, "FOO")
        assert db.get_setting(conn, "FOO") is None

    def test_list_settings(self, conn):
        db.set_setting(conn, "A", "1")
        db.set_setting(conn, "B", "2")
        assert db.list_settings(conn) == {"A": "1", "B": "2"}

    def test_values_stored_as_strings(self, conn):
        db.set_setting(conn, "N", 42)
        assert db.get_setting(conn, "N") == "42"


class TestTrackLifecycle:
    def test_insert_fetch_roundtrip(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "Song", "Artist", "Album"))
        rows = db.fetch_all_tracks(conn, "pl_test")
        assert ("abc", "Song", "Artist", "Album") in rows

    def test_insert_or_ignore_on_duplicate(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "Song", "A", "Alb"))
        db.insert_track(conn, "pl_test", ("abc", "Changed", "Changed", "Changed"))
        rows = db.fetch_all_tracks(conn, "pl_test")
        assert rows == [("abc", "Song", "A", "Alb")]


class TestUpdateDownloadStatusOffByOne:
    """Regression: tracks must suspend after exactly MAX_ATTEMPTS_BEFORE_SUSPEND
    failures. Before the fix they suspended one attempt too late."""

    def test_first_failure_does_not_suspend(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.update_download_status(conn, "abc", "pl_test", success=False)
        r = conn.execute(
            "SELECT attempts, suspended_until FROM pl_test WHERE id=?", ("abc",)
        ).fetchone()
        assert r[0] == 1
        assert r[1] is None

    def test_suspension_on_second_failure(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.update_download_status(conn, "abc", "pl_test", success=False)
        db.update_download_status(conn, "abc", "pl_test", success=False)
        r = conn.execute(
            "SELECT attempts, suspended_until FROM pl_test WHERE id=?", ("abc",)
        ).fetchone()
        assert r[0] == db.MAX_ATTEMPTS_BEFORE_SUSPEND
        assert r[1] is not None, "should be suspended after reaching max attempts"

    def test_success_clears_attempts_and_suspension(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.update_download_status(conn, "abc", "pl_test", success=False)
        db.update_download_status(conn, "abc", "pl_test", success=False)
        db.update_download_status(conn, "abc", "pl_test", success=True, file_path="/p")
        r = conn.execute(
            "SELECT attempts, suspended_until, downloaded, path FROM pl_test WHERE id=?",
            ("abc",),
        ).fetchone()
        assert r == (0, None, 1, "/p")


class TestPendingTracks:
    def test_only_returns_undownloaded_unsuspended(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("a", "Sa", "Art", "Alb"))
        db.insert_track(conn, "pl_test", ("b", "Sb", "Art", "Alb"))
        db.insert_track(conn, "pl_test", ("c", "Sc", "Art", "Alb"))

        db.update_download_status(conn, "a", "pl_test", success=True, file_path="/x")
        # Force b into suspended state
        db.update_download_status(conn, "b", "pl_test", success=False)
        db.update_download_status(conn, "b", "pl_test", success=False)

        pending = db.get_pending_tracks(conn, "pl_test")
        assert [t.id for t in pending] == ["c"]


class TestTriedFiles:
    def test_add_and_read_back(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.add_tried_file(conn, "pl_test", "abc", "bad1.mp3")
        db.add_tried_file(conn, "pl_test", "abc", "bad2.mp3")
        tried = db.get_tried_files(conn, "pl_test", "abc")
        assert set(tried) == {"bad1.mp3", "bad2.mp3"}

    def test_duplicate_insert_is_idempotent(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.add_tried_file(conn, "pl_test", "abc", "bad.mp3")
        db.add_tried_file(conn, "pl_test", "abc", "bad.mp3")
        assert db.get_tried_files(conn, "pl_test", "abc") == ["bad.mp3"]


class TestUpgradeChecks:
    def _seed(self, conn, table="pl_up"):
        db.create_table(conn, table)
        db.insert_track(conn, table, ("a", "Sa", "Art", "Alb"))
        db.insert_track(conn, table, ("b", "Sb", "Art", "Alb"))
        db.update_download_status(conn, "a", table, success=True, file_path="/p/a.flac")
        db.update_download_status(conn, "b", table, success=True, file_path="/p/b.flac")
        return table

    def test_disabled_with_zero_interval(self, conn):
        table = self._seed(conn)
        assert db.get_upgrade_candidates(conn, table, 0) == []

    def test_returns_only_downloaded(self, conn):
        table = self._seed(conn)
        # Add an undownloaded row that must NOT show up.
        db.insert_track(conn, table, ("c", "Sc", "Art", "Alb"))
        out = db.get_upgrade_candidates(conn, table, 168)
        assert sorted(t.id for t, _ in out) == ["a", "b"]

    def test_path_returned(self, conn):
        table = self._seed(conn)
        out = dict((t.id, p) for t, p in db.get_upgrade_candidates(conn, table, 168))
        assert out == {"a": "/p/a.flac", "b": "/p/b.flac"}

    def test_mark_checked_excludes_from_window(self, conn):
        table = self._seed(conn)
        db.mark_upgrade_checked(conn, table, "a")
        out = db.get_upgrade_candidates(conn, table, 168)
        assert [t.id for t, _ in out] == ["b"]

    def test_short_interval_re_includes_recently_checked(self, conn):
        table = self._seed(conn)
        db.mark_upgrade_checked(conn, table, "a")
        # interval=0 hours selects everything regardless of last check (the
        # row's last_upgrade_check is "now", and "now < now" is false — so
        # we use a tiny positive interval that still catches it).
        # Force the timestamp to the past:
        with conn:
            conn.execute(
                f'UPDATE "{table}" SET last_upgrade_check = datetime("now", "-200 hours") '
                f'WHERE id = ?', ("a",),
            )
        out = db.get_upgrade_candidates(conn, table, 168)
        assert sorted(t.id for t, _ in out) == ["a", "b"]


class TestCycleHistory:
    def test_record_then_list(self, conn):
        import time as _time
        db.record_cycle(
            conn,
            started_at=_time.time(),
            duration_seconds=12.5,
            playlists_synced=3,
            tracks_added=2,
            tracks_downloaded=5,
            tracks_failed=1,
        )
        rows = db.list_cycles(conn, limit=10)
        assert len(rows) == 1
        assert rows[0]["playlists_synced"] == 3
        assert rows[0]["tracks_downloaded"] == 5
        assert rows[0]["tracks_failed"] == 1
        assert rows[0]["duration_seconds"] == 12.5

    def test_list_orders_newest_first(self, conn):
        import time as _time
        for i in range(3):
            db.record_cycle(
                conn,
                started_at=_time.time() + i,
                duration_seconds=float(i),
                playlists_synced=1,
                tracks_added=0,
                tracks_downloaded=0,
                tracks_failed=0,
            )
        rows = db.list_cycles(conn, limit=10)
        assert [r["duration_seconds"] for r in rows] == [2.0, 1.0, 0.0]

    def test_keep_last_caps_history(self, conn):
        import time as _time
        for i in range(8):
            db.record_cycle(
                conn,
                started_at=_time.time() + i,
                duration_seconds=float(i),
                playlists_synced=1,
                tracks_added=0,
                tracks_downloaded=0,
                tracks_failed=0,
                keep_last=5,
            )
        rows = db.list_cycles(conn, limit=10)
        # Newest 5 of the 8 inserted survive
        assert [r["duration_seconds"] for r in rows] == [7.0, 6.0, 5.0, 4.0, 3.0]


