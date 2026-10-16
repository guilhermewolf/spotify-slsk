"""Unit tests for db.py — uses an on-disk temp SQLite file (not :memory:).

Using a file is necessary because WAL mode on :memory: is silently demoted.
"""
import os
import sqlite3
import tempfile

os.environ.setdefault("SLSKD_HOST_URL", "http://localhost")
os.environ.setdefault("SLSKD_API_KEY", "x")

import pytest

from spotify_slsk import db


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

    def test_clear_tried_entries_actually_clears_the_history(self, conn):
        """The rejected-filename history must be readable-then-empty.

        acquire.handle_track_download calls clear_tried_entries after a verified
        download so a filename rejected once isn't blacklisted forever. It
        previously wrote to a dead `tried_files` column while the history
        lived in the `_tried` table, making the call a silent no-op.
        """
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.add_tried_file(conn, "pl_test", "abc", "rejected.mp3")
        assert db.get_tried_files(conn, "pl_test", "abc") == ["rejected.mp3"]

        db.clear_tried_entries(conn, "pl_test", "abc")

        assert db.get_tried_files(conn, "pl_test", "abc") == []

    def test_clear_tried_entries_only_affects_the_named_track(self, conn):
        db.create_table(conn, "pl_test")
        db.insert_track(conn, "pl_test", ("abc", "S", "A", "Alb"))
        db.insert_track(conn, "pl_test", ("xyz", "S2", "A2", "Alb2"))
        db.add_tried_file(conn, "pl_test", "abc", "one.mp3")
        db.add_tried_file(conn, "pl_test", "xyz", "two.mp3")

        db.clear_tried_entries(conn, "pl_test", "abc")

        assert db.get_tried_files(conn, "pl_test", "abc") == []
        assert db.get_tried_files(conn, "pl_test", "xyz") == ["two.mp3"]


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


class TestSchemaMigrations:
    """CLAUDE.md promises older DBs upgrade automatically. Nothing tested it.

    Every other fixture starts from create_connection, which always produces
    the current schema — so the ALTER TABLE guards were never exercised
    against an actually-old database.
    """

    def test_v2_playlists_meta_gains_enabled_and_added_at(self, tmp_path):
        path = str(tmp_path / "old.db")
        raw = sqlite3.connect(path)
        # The pre-v3 shape: no `enabled`, no `added_at`.
        raw.execute(
            "CREATE TABLE playlists_meta ("
            "playlist_id TEXT PRIMARY KEY, table_name TEXT NOT NULL, "
            "name TEXT NOT NULL, snapshot_id TEXT, last_synced TIMESTAMP)"
        )
        raw.execute(
            "INSERT INTO playlists_meta VALUES ('pid-old','pl_old','Old',"
            "'snap',NULL)"
        )
        raw.commit()
        raw.close()

        conn = db.create_connection(path)
        assert conn is not None

        cols = {r[1] for r in conn.execute("PRAGMA table_info(playlists_meta)")}
        assert {"enabled", "added_at"} <= cols

        rows = db.list_playlists(conn)
        assert len(rows) == 1, "pre-existing playlist lost during migration"
        assert rows[0]["playlist_id"] == "pid-old"
        assert rows[0]["enabled"] is True, "migrated playlist should default to enabled"
        conn.close()

    def test_playlist_table_gains_last_upgrade_check(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = db.create_connection(path)
        # A per-playlist table created before the upgrade-pass feature.
        conn.execute(
            'CREATE TABLE "pl_legacy" ('
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, artists TEXT NOT NULL, "
            "album TEXT NOT NULL, downloaded INTEGER DEFAULT 0, path TEXT, "
            "attempts INTEGER DEFAULT 0, last_attempt TIMESTAMP, "
            "suspended_until TIMESTAMP, tried_files TEXT DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO pl_legacy (id, name, artists, album, downloaded) "
            "VALUES ('t1','S','A','Alb',1)"
        )
        conn.commit()

        db.create_table(conn, "pl_legacy")

        cols = {r[1] for r in conn.execute('PRAGMA table_info("pl_legacy")')}
        assert "last_upgrade_check" in cols
        # And the feature that needs the column works against the old row.
        assert db.get_upgrade_candidates(conn, "pl_legacy", 1)[0][0].id == "t1"
        conn.close()

    def test_v4_db_upgrades_to_v5(self, tmp_path):
        """A real v4 DB: per-playlist table without duration_ms."""
        path = str(tmp_path / "v4.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE playlists_meta (playlist_id TEXT PRIMARY KEY, "
            "table_name TEXT NOT NULL, name TEXT NOT NULL, snapshot_id TEXT, "
            "last_synced TIMESTAMP, enabled INTEGER NOT NULL DEFAULT 1, "
            "added_at TIMESTAMP)"
        )
        raw.execute("INSERT INTO playlists_meta VALUES ('pid','pl_v4','V4','snap',NULL,0,NULL)")
        raw.execute(
            'CREATE TABLE "pl_v4" (id TEXT PRIMARY KEY, name TEXT NOT NULL, '
            "artists TEXT NOT NULL, album TEXT NOT NULL, downloaded INTEGER DEFAULT 0, "
            "path TEXT, attempts INTEGER DEFAULT 0, last_attempt TIMESTAMP, "
            "suspended_until TIMESTAMP, last_upgrade_check TIMESTAMP, "
            "tried_files TEXT DEFAULT '')"
        )
        raw.execute("INSERT INTO pl_v4 (id, name, artists, album) VALUES ('t1','S','A','Alb')")
        raw.execute("PRAGMA user_version = 4")
        raw.commit()
        raw.close()

        conn = db.create_connection(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        # Migrated without a sync (the playlist is disabled), so the webui
        # can read it straight away.
        track = db.get_track(conn, "pl_v4", "t1")
        assert track is not None and track["duration_ms"] is None
        # Snapshot cleared so the next sync backfills durations.
        assert db.get_playlist_meta(conn, "pid")[0] is None
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"blocklist", "history"} <= tables
        conn.close()

    def test_v5_db_upgrades_to_v6(self, tmp_path):
        """A real v5 DB: per-playlist table with duration_ms, no isrc, no
        file_checks table."""
        path = str(tmp_path / "v5.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE playlists_meta (playlist_id TEXT PRIMARY KEY, "
            "table_name TEXT NOT NULL, name TEXT NOT NULL, snapshot_id TEXT, "
            "last_synced TIMESTAMP, enabled INTEGER NOT NULL DEFAULT 1, "
            "added_at TIMESTAMP)"
        )
        raw.execute("INSERT INTO playlists_meta VALUES ('pid','pl_v5','V5','snap',NULL,0,NULL)")
        raw.execute(
            'CREATE TABLE "pl_v5" (id TEXT PRIMARY KEY, name TEXT NOT NULL, '
            "artists TEXT NOT NULL, album TEXT NOT NULL, downloaded INTEGER DEFAULT 0, "
            "path TEXT, attempts INTEGER DEFAULT 0, last_attempt TIMESTAMP, "
            "suspended_until TIMESTAMP, last_upgrade_check TIMESTAMP, "
            "tried_files TEXT DEFAULT '', duration_ms INTEGER)"
        )
        raw.execute(
            "INSERT INTO pl_v5 (id, name, artists, album, downloaded, path, duration_ms) "
            "VALUES ('t1','S','A','Alb',1,'/p/s.flac',200000)"
        )
        raw.execute("PRAGMA user_version = 5")
        raw.commit()
        raw.close()

        conn = db.create_connection(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        track = db.get_track(conn, "pl_v5", "t1")
        assert track["isrc"] is None
        assert (track["downloaded"], track["path"], track["duration_ms"]) == (True, "/p/s.flac", 200000)
        # Snapshot cleared so the next sync backfills ISRCs.
        assert db.get_playlist_meta(conn, "pid")[0] is None
        db.save_file_check(conn, "pl_v5", "t1", path="/p/s.flac", size=1, mtime=2.0,
                           rules_version=1, audio_check="ok")
        assert db.get_file_check(conn, "pl_v5", "t1")["audio_check"] == "ok"
        conn.close()

    def test_v6_db_upgrades_to_v7(self, tmp_path):
        """A real v6 DB: per-playlist table with isrc but no quality column,
        file_checks present. v7 adds `quality` without forcing a resync."""
        path = str(tmp_path / "v6.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE playlists_meta (playlist_id TEXT PRIMARY KEY, "
            "table_name TEXT NOT NULL, name TEXT NOT NULL, snapshot_id TEXT, "
            "last_synced TIMESTAMP, enabled INTEGER NOT NULL DEFAULT 1, "
            "added_at TIMESTAMP)"
        )
        raw.execute("INSERT INTO playlists_meta VALUES ('pid','pl_v6','V6','snap',NULL,1,NULL)")
        raw.execute(
            'CREATE TABLE "pl_v6" (id TEXT PRIMARY KEY, name TEXT NOT NULL, '
            "artists TEXT NOT NULL, album TEXT NOT NULL, downloaded INTEGER DEFAULT 0, "
            "path TEXT, attempts INTEGER DEFAULT 0, last_attempt TIMESTAMP, "
            "suspended_until TIMESTAMP, last_upgrade_check TIMESTAMP, "
            "tried_files TEXT DEFAULT '', duration_ms INTEGER, isrc TEXT)"
        )
        raw.execute(
            "INSERT INTO pl_v6 (id, name, artists, album, downloaded, path, duration_ms, isrc) "
            "VALUES ('t1','S','A','Alb',1,'/p/s.flac',200000,'GBAYE0601498')"
        )
        raw.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)"
        )
        raw.execute("INSERT INTO settings VALUES ('SLSKD_PREFERRED_FORMATS','flac',NULL)")
        raw.execute("PRAGMA user_version = 6")
        raw.commit()
        raw.close()

        conn = db.create_connection(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7 == db.SCHEMA_VERSION
        track = db.get_track(conn, "pl_v6", "t1")
        assert track["quality"] is None  # backfilled lazily from the file
        assert (track["path"], track["isrc"]) == ("/p/s.flac", "GBAYE0601498")
        # No resync: quality comes from the file, not Spotify.
        assert db.get_playlist_meta(conn, "pid")[0] == "snap"
        assert db.tracks_missing_quality(conn, "pl_v6", 10) == [("t1", "/p/s.flac")]
        db.set_track_quality(conn, "pl_v6", "t1", "FLAC_16")
        assert db.get_track(conn, "pl_v6", "t1")["quality"] == "FLAC_16"
        # A new file at that row resets it.
        db.update_download_status(conn, "t1", "pl_v6", success=True, file_path="/p/n.flac")
        assert db.get_track(conn, "pl_v6", "t1")["quality"] is None
        db.set_track_quality(conn, "pl_v6", "t1", None)  # unreadable: '' = don't retry
        assert db.tracks_missing_quality(conn, "pl_v6", 10) == []
        conn.close()

    def test_existing_rows_survive_reopening(self, tmp_path):
        path = str(tmp_path / "reopen.db")
        conn = db.create_connection(path)
        db.create_table(conn, "pl_x")
        db.insert_track(conn, "pl_x", ("t1", "S", "A", "Alb"))
        conn.close()

        conn = db.create_connection(path)
        assert [r[0] for r in db.fetch_all_tracks(conn, "pl_x")] == ["t1"]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        conn.close()

    def test_corrupted_database_returns_none_rather_than_raising(self, tmp_path):
        path = tmp_path / "corrupt.db"
        path.write_bytes(b"this is definitely not a sqlite database" * 10)
        assert db.create_connection(str(path)) is None
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




class TestBlocklist:
    def test_add_list_remove_and_predicate(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "b.db"))
        assert db.add_blocklist(conn, "peer", "a.flac", 10, "transfer failed", "t1")
        assert not db.add_blocklist(conn, "peer", "a.flac"), "duplicate must be ignored"
        assert db.add_blocklist(conn, "villain", None, None, "banned")
        assert db.is_blocklisted(conn, "peer", "a.flac")
        assert not db.is_blocklisted(conn, "peer", "b.flac")
        assert db.is_blocklisted(conn, "villain", "anything.flac"), "banned user covers all files"
        rows = db.list_blocklist(conn)
        assert [r["username"] for r in rows] == ["villain", "peer"], "newest first"
        assert db.remove_blocklist(conn, rows[1]["id"])
        assert not db.is_blocklisted(conn, "peer", "a.flac")
        conn.close()


class TestHistory:
    def test_record_list_filter_and_paginate(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "h.db"))
        db.upsert_playlist_meta(conn, "pid", "pl_x", "X", None)
        db.record_history(conn, "grabbed", table_name="pl_x", track_id="t1", username="u", bitrate=None)
        db.record_history(conn, "download_failed", table_name="pl_x", track_id="t1", reason="transfer failed")
        db.record_history(conn, "retried", playlist_id="other", track_id="t2")

        rows = db.list_history(conn)
        assert [r["event"] for r in rows] == ["retried", "download_failed", "grabbed"]
        assert rows[2]["playlist_id"] == "pid", "table_name resolves to playlist_id"
        assert rows[2]["data"] == {"username": "u"}, "None values are dropped"
        assert [r["event"] for r in db.list_history(conn, playlist_id="pid")] == [
            "download_failed", "grabbed"]
        assert [r["event"] for r in db.list_history(conn, event="grabbed")] == ["grabbed"]
        assert [r["event"] for r in db.list_history(conn, before_id=rows[1]["id"])] == ["grabbed"]
        assert db.latest_failures(conn) == {"t1": "transfer failed"}
        conn.close()

    def test_prune_keeps_recent(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "h.db"))
        db.record_history(conn, "grabbed", track_id="new")
        with conn:
            conn.execute(
                "INSERT INTO history (ts, event, track_id) VALUES (datetime('now','-100 days'), 'grabbed', 'old')"
            )
        db.prune_history(conn, 90)
        assert [r["track_id"] for r in db.list_history(conn)] == ["new"]
        conn.close()

    @pytest.mark.parametrize("days", [0, -5])
    def test_retention_below_one_day_is_clamped(self, tmp_path, days):
        conn = db.create_connection(str(tmp_path / "h.db"))
        db.record_history(conn, "grabbed", track_id="recent")
        with conn:
            conn.execute(
                "INSERT INTO history (ts, event, track_id) VALUES (datetime('now','-2 days'), 'grabbed', 'old')"
            )
        db.prune_history(conn, days)
        assert [r["track_id"] for r in db.list_history(conn)] == ["recent"]
        conn.close()

    def test_reload_settings_clamps_retention(self, tmp_path, monkeypatch):
        from spotify_slsk import config
        conn = db.create_connection(str(tmp_path / "h.db"))
        monkeypatch.setenv("HISTORY_RETENTION_DAYS", "0")
        # reload() rebinds every tunable; snapshot them so they are restored.
        for name in ("HISTORY_RETENTION_DAYS", "MIN_MATCH_SCORE", "QUALITY_PROFILE",
                     "MIN_PEER_UPLOAD_SPEED", "MIN_EFFECTIVE_MP3_KBPS",
                     "EARLY_STOP_RESPONSES", "EXTERNAL_PROCESS_WAIT_TIMEOUT",
                     "MAX_DURATION_DIFF_SECONDS"):
            monkeypatch.setattr(config, name, getattr(config, name))
        config.reload(conn)
        assert config.HISTORY_RETENTION_DAYS == 1
        conn.close()

    def test_none_conn_is_a_noop(self):
        db.record_history(None, "grabbed")


class TestStatusCountsAgree:
    """Dashboard (playlist_stats) and the playlist page's filter chips
    (list_tracks statuses) count the same partition of the table."""

    def test_stats_partition_matches_track_statuses(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "s.db"))
        db.create_table(conn, "pl_s")
        for tid in ("new", "failed_once", "suspended", "done1", "done2"):
            db.insert_track(conn, "pl_s", (tid, tid, "A", "B"))
        db.update_download_status(conn, "failed_once", "pl_s", success=False)
        for _ in range(db.MAX_ATTEMPTS_BEFORE_SUSPEND):
            db.update_download_status(conn, "suspended", "pl_s", success=False)
        for tid in ("done1", "done2"):
            db.update_download_status(conn, tid, "pl_s", success=True, file_path=f"/p/{tid}")

        stats = db.playlist_stats(conn, "pl_s")
        statuses = [t["status"] for t in db.list_tracks(conn, "pl_s")]
        for key in ("downloaded", "pending", "retrying", "suspended"):
            assert stats[key] == statuses.count(key), key
        assert stats == {"total": 5, "downloaded": 2, "pending": 1, "retrying": 1, "suspended": 1}
        conn.close()

    def test_all_downloaded_means_nothing_pending(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "s.db"))
        db.create_table(conn, "pl_s")
        for i in range(3):
            db.insert_track(conn, "pl_s", (f"t{i}", "x", "A", "B"))
            db.update_download_status(conn, f"t{i}", "pl_s", success=False)
            db.update_download_status(conn, f"t{i}", "pl_s", success=True, file_path="/p")
        assert db.playlist_stats(conn, "pl_s")["pending"] == 0
        conn.close()


class TestFileChecks:
    def test_round_trip_and_ignore_survives_only_for_the_same_file(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "c.db"))
        kw = dict(path="/p/a.flac", size=10, mtime=1.0, rules_version=1)
        db.save_file_check(conn, "pl_a", "t1", **kw, audio_check="fake",
                           audio_cutoff_hz=16000, audio_drop_db=30.0,
                           problems=["fake_lossless"], evidence={"x": 1})
        check = db.get_file_check(conn, "pl_a", "t1")
        assert check["problems"] == ["fake_lossless"] and check["evidence"] == {"x": 1}
        assert db.set_check_ignored(conn, "pl_a", "t1")
        db.save_file_check(conn, "pl_a", "t1", **kw, problems=["fake_lossless"])
        assert db.get_file_check(conn, "pl_a", "t1")["ignored"] is True
        db.save_file_check(conn, "pl_a", "t1", **{**kw, "size": 11}, problems=["fake_lossless"])
        assert db.get_file_check(conn, "pl_a", "t1")["ignored"] is False
        assert [c["track_id"] for c in db.list_file_checks(conn)] == ["t1"]
        db.delete_file_check(conn, "pl_a", "t1")
        assert db.get_file_check(conn, "pl_a", "t1") is None
        conn.close()

    def test_removing_a_playlist_drops_its_checks(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "c.db"))
        db.upsert_playlist_meta(conn, "pid", "pl_a", "A", None)
        db.create_table(conn, "pl_a")
        db.save_file_check(conn, "pl_a", "t1", path=None, size=None, mtime=None,
                           rules_version=1)
        db.remove_playlist(conn, "pid")
        assert db.list_file_checks(conn) == []
        conn.close()

    def test_reset_track_for_redownload(self, tmp_path):
        conn = db.create_connection(str(tmp_path / "c.db"))
        db.create_table(conn, "pl_a")
        db.insert_track(conn, "pl_a", ("t1", "S", "A", "Alb"))
        db.update_download_status(conn, "t1", "pl_a", success=True, file_path="/p/x.flac")
        db.reset_track_for_redownload(conn, "pl_a", "t1")
        track = db.get_track(conn, "pl_a", "t1")
        assert (track["downloaded"], track["path"], track["status"]) == (False, None, "pending")
        conn.close()
