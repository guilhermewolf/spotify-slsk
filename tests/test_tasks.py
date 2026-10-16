"""Scheduled tasks: the registry, the tasks table (schema v9), the daemon's
runner, and /system/tasks."""
import sqlite3
import time

import pytest

from spotify_slsk import config, daemon, db, tasks
from tests.test_webui import app_tmp, client, post  # noqa: F401  (fixtures)


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    yield c
    c.close()


class TestTable:
    def test_run_now_is_deduplicated(self, conn):
        assert db.queue_task(conn, "db_backup") is True
        assert db.queue_task(conn, "db_backup") is False
        assert db.pop_queued_tasks(conn, ["db_backup", "other"]) == {"db_backup"}
        assert db.pop_queued_tasks(conn, ["db_backup"]) == set()
        assert db.queue_task(conn, "db_backup") is True

    def test_pop_only_touches_the_named_tasks(self, conn):
        db.queue_task(conn, "a")
        db.queue_task(conn, "b")
        assert db.pop_queued_tasks(conn, ["a"]) == {"a"}
        assert db.list_task_rows(conn)["b"]["queued_at"] is not None

    def test_a_run_keeps_a_pending_request(self, conn):
        db.queue_task(conn, "a")
        db.record_task_run(conn, "a", started=1.0, duration=2.0, result="ok")
        row = db.list_task_rows(conn)["a"]
        assert (row["last_run"], row["last_duration"], row["last_result"], row["last_ok"]) == (1.0, 2.0, "ok", True)
        assert row["queued_at"] is not None

    def test_v8_database_gains_the_table_without_a_resync(self, tmp_path):
        path = str(tmp_path / "v8.db")
        c = db.create_connection(path)
        db.upsert_playlist_meta(c, "pid", "pl_x", "X", "snap")
        c.execute("DROP TABLE tasks")
        c.execute("PRAGMA user_version = 8")
        c.commit()
        c.close()
        c = db.create_connection(path)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 9 == db.SCHEMA_VERSION
        assert db.queue_task(c, "x")
        assert db.get_playlist_meta(c, "pid")[0] == "snap"  # v8 -> v9 needs no resync
        c.close()


class TestRegistry:
    def test_due_follows_the_interval(self, conn, monkeypatch):
        monkeypatch.setattr(config, "RECYCLE_BIN_RETENTION_DAYS", 0)
        assert not tasks.is_due(conn, "recycle_purge")  # disabled
        monkeypatch.setattr(config, "RECYCLE_BIN_RETENTION_DAYS", 7)
        assert tasks.is_due(conn, "recycle_purge")  # never ran
        db.record_task_run(conn, "recycle_purge", started=time.time(), duration=0, result="")
        assert not tasks.is_due(conn, "recycle_purge")
        assert tasks.is_due(conn, "recycle_purge", now=time.time() + tasks.DAY + 1)

    def test_describe_lists_every_task(self, conn):
        rows = {r["name"]: r for r in tasks.describe(conn)}
        assert set(rows) == {t.name for t in tasks.TASKS}
        assert rows["spotify_sync"]["cadence"] == "every cycle"
        assert rows["spotify_sync"]["last_run"] is None


class TestRunner:
    def test_queued_task_runs_even_when_not_due(self, conn, monkeypatch):
        ran = []
        monkeypatch.setitem(daemon.SCHEDULED, "recycle_purge", lambda c: ran.append(1) or "done")
        monkeypatch.setattr(config, "RECYCLE_BIN_RETENTION_DAYS", 0)
        daemon._run_scheduled(conn)
        assert ran == []
        db.queue_task(conn, "recycle_purge")
        daemon._run_scheduled(conn)
        daemon._run_scheduled(conn)
        assert ran == [1]
        assert db.list_task_rows(conn)["recycle_purge"]["last_result"] == "done"

    def test_a_failing_task_is_recorded_not_raised(self, conn, monkeypatch):
        def boom(c):
            raise RuntimeError("disk full")

        monkeypatch.setitem(daemon.SCHEDULED, "recycle_purge", boom)
        daemon._run_task(conn, "recycle_purge", boom)
        row = db.list_task_rows(conn)["recycle_purge"]
        assert row["last_ok"] is False and "disk full" in row["last_result"]


class TestPage:
    def test_lists_tasks_and_queues_run_now(self, app_tmp, client):  # noqa: F811
        woke = []
        app_tmp.config["WAKE_DAEMON"] = lambda: woke.append(1)
        r = client.get("/system/tasks")
        assert r.status_code == 200
        assert b"Recycle bin purge" in r.data and b'name="csrf_token"' in r.data
        assert b"<script>" not in r.data and b"onclick" not in r.data

        assert client.post("/system/tasks/recycle_purge/run").status_code == 400  # CSRF
        r = post(client, "/system/tasks/recycle_purge/run", follow_redirects=True)
        assert b"queued" in r.data
        r = post(client, "/system/tasks/recycle_purge/run", follow_redirects=True)
        assert b"already queued" in r.data
        assert woke == [1, 1]
        c = sqlite3.connect(app_tmp.config["DB_PATH"])
        assert c.execute("SELECT queued_at FROM tasks WHERE name='recycle_purge'").fetchone()[0]
        c.close()

    def test_library_scan_uses_the_scan_request_flag(self, app_tmp, client):  # noqa: F811
        from spotify_slsk.library.health import SCAN_PENDING_KEY

        post(client, "/system/tasks/library_scan/run")
        c = db.create_connection(app_tmp.config["DB_PATH"])
        assert db.get_setting(c, SCAN_PENDING_KEY) == "1"
        assert {r["name"]: r for r in tasks.describe(c)}["library_scan"]["queued"]
        c.close()

    def test_unknown_task_is_404(self, client):  # noqa: F811
        assert post(client, "/system/tasks/nope/run").status_code == 404
