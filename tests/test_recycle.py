"""Recycle bin (Tier 5): RECYCLE_BIN_RETENTION_DAYS purges only
.quarantine/<table>/replaced/, never operator-quarantined files."""
from __future__ import annotations

import os
import time

import pytest

from spotify_slsk import config, daemon, db
from spotify_slsk.library import health

DAY = 86400


@pytest.fixture
def setup(tmp_path, sandbox_dirs):
    _, playlists = sandbox_dirs
    conn = db.create_connection(str(tmp_path / "t.db"))
    q = playlists / ".quarantine"
    files = {
        "old_replaced": q / "pl_a" / "replaced" / "old.flac",
        "new_replaced": q / "pl_a" / "replaced" / "new.flac",
        "old_user_quarantined": q / "pl_a" / "flagged.flac",
        "old_nested_replaced": q / "pl_b" / "replaced" / "sub" / "deep.mp3",
        "old_library": playlists / "pl_a" / "keep.flac",
    }
    now = time.time()
    for key, f in files.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        age = 1 * DAY if key == "new_replaced" else 40 * DAY
        os.utime(f, (now - age, now - age))
    yield conn, files, playlists
    conn.close()


class TestPurge:
    def test_zero_days_keeps_everything(self, setup):
        conn, files, _ = setup
        assert health.purge_recycle_bin(conn, 0) == 0
        assert all(f.exists() for f in files.values())

    def test_purges_only_old_replaced_files(self, setup):
        conn, files, _ = setup
        assert health.purge_recycle_bin(conn, 30) == 2
        assert not files["old_replaced"].exists()
        assert not files["old_nested_replaced"].exists()
        assert files["new_replaced"].exists()
        assert files["old_user_quarantined"].exists()
        assert files["old_library"].exists()
        paths = {e["data"]["path"] for e in db.list_history(conn, event="recycle_purged")}
        assert paths == {str(files["old_replaced"]), str(files["old_nested_replaced"])}

    def test_symlinked_bin_is_not_followed(self, setup, tmp_path):
        conn, files, playlists = setup
        outside = tmp_path / "outside"
        outside.mkdir()
        precious = outside / "precious.flac"
        precious.write_bytes(b"x")
        os.utime(precious, (0, 0))
        (playlists / ".quarantine" / "pl_c").mkdir()
        os.symlink(outside, playlists / ".quarantine" / "pl_c" / "replaced")
        health.purge_recycle_bin(conn, 30)
        assert precious.exists()

    def test_retired_file_counts_from_when_it_was_replaced(self, setup):
        conn, _, playlists = setup
        f = playlists / "pl_a" / "ancient.flac"
        f.write_bytes(b"x")
        os.utime(f, (0, 0))  # downloaded long ago
        dest = health.retire_replaced_file(conn, "pl_a", "t1", str(f), "replaced by upgrade")
        assert dest and time.time() - os.path.getmtime(dest) < 60
        health.purge_recycle_bin(conn, 30)
        assert os.path.exists(dest)


class TestHousekeeping:
    def test_runs_at_most_daily_and_only_when_enabled(self, setup, monkeypatch):
        conn, files, _ = setup
        monkeypatch.setattr(config, "RECYCLE_BIN_RETENTION_DAYS", 0)
        daemon._run_housekeeping(conn)
        assert files["old_replaced"].exists()

        monkeypatch.setattr(config, "RECYCLE_BIN_RETENTION_DAYS", 30)
        daemon._run_housekeeping(conn)
        assert not files["old_replaced"].exists()

        files["old_replaced"].write_bytes(b"x")
        os.utime(files["old_replaced"], (0, 0))
        daemon._run_housekeeping(conn)  # same day: not again
        assert files["old_replaced"].exists()
        db.set_setting(conn, health.RECYCLE_PURGED_KEY, str(time.time() - DAY - 1))
        daemon._run_housekeeping(conn)
        assert not files["old_replaced"].exists()
