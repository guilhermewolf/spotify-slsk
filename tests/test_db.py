"""Unit tests for db.py — uses an on-disk temp SQLite file (not :memory:).

Using a file is necessary because WAL mode on :memory: is silently demoted.
"""
import os
import tempfile
import time

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
