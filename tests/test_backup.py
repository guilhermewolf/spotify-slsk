"""Database backups (online backup API, verified, retention) and daily
housekeeping."""
import os
import sqlite3
import time

import pytest

from spotify_slsk import backup, config, daemon, db, tasks
from tests.test_webui import app_tmp, client, post  # noqa: F401  (fixtures)

DAY = 86400


@pytest.fixture
def conn(tmp_path):
    (tmp_path / "data").mkdir()
    c = db.create_connection(str(tmp_path / "data" / "playlist_tracks.db"))
    db.upsert_playlist_meta(c, "pid", "pl_x", "X", "s")
    yield c
    c.close()


class TestBackup:
    def test_backup_is_complete_verified_and_self_contained(self, conn, tmp_path):
        path = backup.create_backup(conn, now=time.mktime((2026, 3, 4, 5, 6, 7, 0, 0, -1)))
        assert path == str(tmp_path / "data" / "backups" / "playlist_tracks-20260304-050607.db")
        copy = sqlite3.connect(path)
        assert copy.execute("SELECT name FROM playlists_meta").fetchall() == [("X",)]
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        copy.close()
        assert not os.path.exists(path + ".partial") and not os.path.exists(path + "-wal")

    def test_backups_are_private(self, conn):
        import stat

        path = backup.create_backup(conn, now=1.0)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(backup.backup_dir(conn)).st_mode) == 0o700
        os.chmod(backup.backup_dir(conn), 0o755)  # an existing, looser dir is tightened
        backup.create_backup(conn, now=2.0)
        assert stat.S_IMODE(os.stat(backup.backup_dir(conn)).st_mode) == 0o700

    def test_retention_keeps_the_newest_and_ignores_other_files(self, conn, monkeypatch):
        for day in range(1, 6):
            backup.create_backup(conn, now=time.mktime((2026, 1, day, 0, 0, 0, 0, 0, -1)))
        other = os.path.join(backup.backup_dir(conn), "my-own-copy.db")
        open(other, "w").close()
        monkeypatch.setattr(config, "BACKUP_RETENTION", 2)
        result = backup.run_backup(conn)
        assert "removed 4 old" in result
        names = [b["name"] for b in backup.list_backups(conn)]
        assert len(names) == 2 and names == sorted(names, reverse=True)
        assert os.path.exists(other)

    def test_retention_zero_disables_the_schedule_and_deletes_nothing(self, conn, monkeypatch):
        monkeypatch.setattr(config, "BACKUP_RETENTION", 0)
        assert not tasks.is_due(conn, "db_backup")
        backup.create_backup(conn, now=1.0)
        backup.run_backup(conn)
        assert len(backup.list_backups(conn)) == 2

    def test_daily_task_and_run_now(self, conn, monkeypatch):
        monkeypatch.setattr(config, "BACKUP_RETENTION", 7)
        monkeypatch.setattr(daemon, "SCHEDULED", {"db_backup": backup.run_backup})
        daemon._run_scheduled(conn)
        daemon._run_scheduled(conn)  # not due again today
        assert len(backup.list_backups(conn)) == 1
        db.queue_task(conn, "db_backup")
        time.sleep(1.01)  # backups are named by the second
        daemon._run_scheduled(conn)
        assert len(backup.list_backups(conn)) == 2
        assert db.list_task_rows(conn)["db_backup"]["last_ok"] is True


class TestHousekeeping:
    def test_removes_only_our_expired_temp_files(self, conn, tmp_path, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        monkeypatch.setattr(config, "DOWNLOAD_DIR", str(downloads))
        art = tmp_path / "art"
        art.mkdir()
        monkeypatch.setenv("ARTWORK_CACHE_DIR", str(art))
        old = time.time() - 3 * DAY
        files = {name: art / name for name in ("a.fail", "b.fail", "c.img", "d.img.tmp")}
        for f in files.values():
            f.write_bytes(b"x")
        for name in ("a.fail", "c.img", "d.img.tmp"):
            os.utime(files[name], (old, old))
        os.makedirs(backup.backup_dir(conn))
        partial = os.path.join(backup.backup_dir(conn), "playlist_tracks-1.db.partial")
        open(partial, "w").close()
        os.utime(partial, (old, old))
        incomplete = downloads / "incomplete" / "peer"
        incomplete.mkdir(parents=True)
        stale = incomplete / "song.flac"
        stale.write_bytes(b"x")
        os.utime(stale, (time.time() - 10 * DAY,) * 2)

        result = backup.housekeeping(conn)

        assert not files["a.fail"].exists() and not files["d.img.tmp"].exists()
        assert files["b.fail"].exists() and files["c.img"].exists()  # fresh / not ours to expire
        assert not os.path.exists(partial)
        assert stale.exists()  # slskd's: report only
        assert "1 partial download(s)" in result and "report only" in result

    def test_vacuum_only_when_much_is_free(self, conn):
        assert backup.maybe_vacuum(conn) is False  # tiny DB
        conn.execute("CREATE TABLE junk (x BLOB)")
        with conn:
            conn.executemany("INSERT INTO junk VALUES (?)", [(b"x" * 4000,)] * 3000)
        with conn:
            conn.execute("DELETE FROM junk")
        assert backup.maybe_vacuum(conn) is True
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0

    def test_history_is_trimmed(self, conn, monkeypatch):
        monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 1)
        with conn:
            conn.execute("INSERT INTO history (ts, event) VALUES (datetime('now', '-5 days'), 'grabbed')")
        backup.housekeeping(conn)
        assert db.list_history(conn) == []


class TestStatusPage:
    def test_lists_backups_and_backup_now_queues(self, app_tmp, client):  # noqa: F811
        c = db.create_connection(app_tmp.config["DB_PATH"])
        backup.create_backup(c, now=time.mktime((2026, 2, 1, 0, 0, 0, 0, 0, -1)))
        c.close()
        html = client.get("/system/status").data
        assert b"playlist_tracks-20260201-000000.db" in html and b"Backup now" in html
        r = post(client, "/system/tasks/db_backup/run", {"next": "status"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/system/status")
        assert b"Backup queued" in client.get("/system/status").data
