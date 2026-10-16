"""Library health scan (library/health.py) and its daemon trigger.

mutagen and the spectrum check are stubbed; files are real so stat-based
change detection is exercised.
"""
import os

import pytest

from spotify_slsk import config, daemon, db
from spotify_slsk.importer import audio_check as ac
from spotify_slsk.library import health


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    db.upsert_playlist_meta(c, "pid", "pl_x", "X", "snap")
    db.create_table(c, "pl_x")
    yield c
    c.close()


@pytest.fixture
def tags(monkeypatch):
    """Per-path tags: {basename: (title, artist, length, isrc)}."""
    table = {}
    monkeypatch.setattr(health, "extract_metadata_from_file",
                        lambda p: (*table[os.path.basename(p)][:2], None))
    monkeypatch.setattr(health, "audio_length_seconds", lambda p: table[os.path.basename(p)][2])
    monkeypatch.setattr(health, "read_isrc", lambda p: table[os.path.basename(p)][3])
    monkeypatch.setattr(health, "check_file", lambda p: ac.AudioCheck(ac.OK, 21500, 2.0))
    return table


def _add(conn, playlists, tid, name, artists, fname, duration_ms=200_000, isrc=None):
    f = playlists / fname
    f.write_bytes(b"audio")
    db.insert_track(conn, "pl_x", (tid, name, artists, "Alb", duration_ms, isrc))
    db.update_download_status(conn, tid, "pl_x", success=True, file_path=str(f))
    return f


def _problems(conn, playlists, tags, tid, name, artists, fname, file_tags, **kw):
    _add(conn, playlists, tid, name, artists, fname, **kw)
    tags[fname] = file_tags
    row = db.get_track(conn, "pl_x", tid)
    return health.verify_file(row)


class TestVerifyFile:
    def test_matching_file_has_no_problems(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        r = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe", "a.flac",
                      ("Heart To Find", "Mat Joe", 201.0, "GBAYE0601498"), isrc="GBAYE0601498")
        assert r["problems"] == []
        assert r["audio_check"] == "ok" and r["rules_version"] == health.RULES_VERSION

    def test_wrong_song_title_artist(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        r = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe", "a.mp3",
                      ("Eyes On Fire", "Blue Foundation", 200.0, None))
        assert r["problems"] == ["title_artist_mismatch"]
        assert r["evidence"]["file_title"] == "Eyes On Fire"

    def test_version_mismatch(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        r = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe", "a.mp3",
                      ("Heart To Find (Live)", "Mat Joe", 200.0, None))
        assert r["problems"] == ["version_mismatch"]

    def test_isrc_mismatch_is_only_a_note(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        alone = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe", "a.mp3",
                          ("Heart To Find", "Mat Joe", 200.0, "USUM71703861"), isrc="GBAYE0601498")
        assert alone["problems"] == []
        assert alone["evidence"]["isrc_note"].startswith("ISRC differs: file USUM71703861")
        combined = _problems(conn, pl, tags, "t2", "Heart To Find", "Mat Joe", "b.mp3",
                             ("Heart To Find", "Mat Joe", 260.0, "USUM71703861"), isrc="GBAYE0601498")
        assert combined["problems"] == ["duration_off"]
        assert combined["evidence"]["reasons"]["duration_off"] == "4:20 vs 3:20"
        assert "isrc_note" in combined["evidence"]

    def test_equal_isrc_excuses_a_tag_score_miss(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        r = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe", "a.mp3",
                      ("ハート", "マット", 200.0, "GBAYE0601498"), isrc="GBAYE0601498")
        assert r["problems"] == []

    def test_untagged_file_is_judged_by_its_name(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        ok = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe",
                       "Mat Joe - Heart To Find.mp3", (None, None, 200.0, None))
        bad = _problems(conn, pl, tags, "t2", "Heart To Find", "Mat Joe",
                        "Other - Song.mp3", (None, None, 200.0, None))
        assert ok["problems"] == []
        assert bad["problems"] == ["title_artist_mismatch"]

    def test_missing_file(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        f = _add(conn, pl, "t1", "Heart To Find", "Mat Joe", "a.mp3")
        f.unlink()
        r = health.verify_file(db.get_track(conn, "pl_x", "t1"))
        assert r["problems"] == ["missing_file"]

    def test_fake_and_suspect_lossless(self, conn, sandbox_dirs, tags, monkeypatch):
        _, pl = sandbox_dirs
        monkeypatch.setattr(health, "check_file", lambda p: ac.AudioCheck(ac.FAKE, 16200, 30.0))
        r = _problems(conn, pl, tags, "t1", "Heart To Find", "Mat Joe", "a.flac",
                      ("Heart To Find", "Mat Joe", 200.0, None))
        assert r["problems"] == ["fake_lossless"]
        assert r["evidence"]["reasons"]["fake_lossless"] == "fake: steepest drop 30 dB at 16.2 kHz"
        monkeypatch.setattr(health, "check_file", lambda p: ac.AudioCheck(ac.SUSPECT, 20000, 20.0))
        r = _problems(conn, pl, tags, "t2", "Heart To Find", "Mat Joe", "b.flac",
                      ("Heart To Find", "Mat Joe", 200.0, None))
        assert r["problems"] == ["suspect_lossless"]

    def test_unchanged_file_reuses_its_spectrum_verdict(self, conn, sandbox_dirs, tags, monkeypatch):
        _, pl = sandbox_dirs
        _add(conn, pl, "t1", "Heart To Find", "Mat Joe", "a.flac")
        tags["a.flac"] = ("Heart To Find", "Mat Joe", 200.0, None)
        row = db.get_track(conn, "pl_x", "t1")
        db.save_file_check(conn, "pl_x", "t1", **health.verify_file(row))
        monkeypatch.setattr(health, "check_file", lambda p: pytest.fail("decoded again"))
        r = health.verify_file(row, db.get_file_check(conn, "pl_x", "t1"))
        assert r["audio_check"] == "ok"


class TestScanStep:
    def _library(self, conn, sandbox_dirs, tags, n=3):
        _, pl = sandbox_dirs
        for i in range(n):
            _add(conn, pl, f"t{i}", "Heart To Find", "Mat Joe", f"{i}.mp3")
            tags[f"{i}.mp3"] = ("Heart To Find", "Mat Joe", 200.0, None)
        return pl

    def test_bounded_resumable_and_skips_current_files(self, conn, sandbox_dirs, tags):
        pl = self._library(conn, sandbox_dirs, tags)
        beats = []
        assert health.scan_step(conn, max_files=2, heartbeat=lambda: beats.append(1)) == (2, False)
        assert health.scan_step(conn, max_files=2) == (1, True)
        assert health.scan_step(conn) == (0, True)
        assert len(beats) == 2
        # A changed file is checked again.
        (pl / "1.mp3").write_bytes(b"different audio")
        assert health.scan_step(conn) == (1, True)

    def test_rules_version_bump_rechecks_everything(self, conn, sandbox_dirs, tags, monkeypatch):
        self._library(conn, sandbox_dirs, tags)
        health.scan_step(conn)
        monkeypatch.setattr(health, "RULES_VERSION", health.RULES_VERSION + 1)
        assert health.scan_step(conn) == (3, True)

    def test_lossless_threshold_change_re_decodes(self, conn, sandbox_dirs, tags, monkeypatch):
        _, pl = sandbox_dirs
        _add(conn, pl, "t1", "Heart To Find", "Mat Joe", "a.flac")
        tags["a.flac"] = ("Heart To Find", "Mat Joe", 200.0, None)
        decodes = []
        monkeypatch.setattr(health, "check_file",
                            lambda p: decodes.append(p) or ac.AudioCheck(ac.OK, 21500, 2.0))
        assert health.scan_step(conn) == (1, True)
        assert health.scan_step(conn) == (0, True)
        monkeypatch.setattr(config, "LOSSLESS_MIN_DROP_DB", 12)
        assert health.scan_step(conn) == (1, True)
        assert len(decodes) == 2  # re-decoded under the new threshold

    def test_stops_on_shutdown_and_ignores_pending_rows(self, conn, sandbox_dirs, tags):
        self._library(conn, sandbox_dirs, tags)
        db.insert_track(conn, "pl_x", ("p1", "Pending", "X", "Alb"))
        assert health.scan_step(conn, should_stop=lambda: True) == (0, False)
        assert health.scan_step(conn) == (3, True)
        assert db.get_file_check(conn, "pl_x", "p1") is None


class TestDaemonTrigger:
    def test_runs_only_when_requested_or_due(self, conn, monkeypatch):
        steps = []
        monkeypatch.setattr(health, "scan_step", lambda c, **k: steps.append(1) or (0, True))
        monkeypatch.setattr(config, "LIBRARY_SCAN_INTERVAL_HOURS", 0)
        daemon._run_library_scan(conn)
        assert steps == []

        db.set_setting(conn, health.SCAN_PENDING_KEY, "1")
        daemon._run_library_scan(conn)
        assert steps == [1]
        assert db.get_setting(conn, health.SCAN_PENDING_KEY, "0") == "0"
        assert float(db.get_setting(conn, health.SCAN_COMPLETED_KEY, "0")) > 0
        daemon._run_library_scan(conn)
        assert steps == [1]

        monkeypatch.setattr(config, "LIBRARY_SCAN_INTERVAL_HOURS", 1)
        db.set_setting(conn, health.SCAN_COMPLETED_KEY, "0")
        daemon._run_library_scan(conn)
        assert steps == [1, 1]

    def test_unfinished_scan_stays_pending(self, conn, monkeypatch):
        monkeypatch.setattr(health, "scan_step", lambda c, **k: (200, False))
        db.set_setting(conn, health.SCAN_PENDING_KEY, "1")
        daemon._run_library_scan(conn)
        assert db.get_setting(conn, health.SCAN_PENDING_KEY, "0") == "1"


class TestQuarantine:
    def test_moves_never_deletes_and_resets_the_row(self, conn, sandbox_dirs, tags):
        _, pl = sandbox_dirs
        (pl / "pl_x").mkdir()
        f = pl / "pl_x" / "Wrong Song.mp3"
        f.write_bytes(b"audio")
        db.insert_track(conn, "pl_x", ("t1", "Heart To Find", "Mat Joe", "Alb"))
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(f))
        db.save_file_check(conn, "pl_x", "t1", path=str(f), size=5, mtime=1.0,
                           rules_version=1, problems=["title_artist_mismatch"])
        # A previous quarantine of the same name: no overwrite.
        qdir = pl / ".quarantine" / "pl_x"
        qdir.mkdir(parents=True)
        (qdir / "Wrong Song.mp3").write_bytes(b"older")

        assert health.quarantine_track(conn, "pl_x", "t1", str(f)) is True

        assert not f.exists()
        assert (qdir / "Wrong Song (2).mp3").read_bytes() == b"audio"
        assert (qdir / "Wrong Song.mp3").read_bytes() == b"older"
        row = db.get_track(conn, "pl_x", "t1")
        assert (row["status"], row["path"]) == ("pending", None)
        assert "Wrong Song.mp3" in db.get_tried_files(conn, "pl_x", "t1")
        assert db.get_file_check(conn, "pl_x", "t1") is None
        event = db.list_history(conn, event="quarantined")[0]
        assert event["data"]["quarantine_path"] == str(qdir / "Wrong Song (2).mp3")
        assert event["data"]["problems"] == "title_artist_mismatch"

    def test_missing_file_only_resets(self, conn, sandbox_dirs):
        db.insert_track(conn, "pl_x", ("t1", "S", "A", "Alb"))
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path="/nope/x.mp3")
        assert health.quarantine_track(conn, "pl_x", "t1", "/nope/x.mp3") is True
        assert db.get_track(conn, "pl_x", "t1")["status"] == "pending"

    def test_pending_row_and_failed_move_change_nothing(self, conn, sandbox_dirs, monkeypatch):
        _, pl = sandbox_dirs
        db.insert_track(conn, "pl_x", ("p1", "S", "A", "Alb"))
        assert health.quarantine_track(conn, "pl_x", "p1", None) is False
        f = _add(conn, pl, "t1", "S", "A", "x.mp3")

        def _fail(*a):
            raise OSError("read-only")

        monkeypatch.setattr(health.shutil, "move", _fail)
        assert health.quarantine_track(conn, "pl_x", "t1", str(f)) is False
        assert f.exists() and db.get_track(conn, "pl_x", "t1")["downloaded"] is True

    def test_row_changed_since_flagged_is_skipped(self, conn, sandbox_dirs):
        _, pl = sandbox_dirs
        new = _add(conn, pl, "t1", "S", "A", "new.mp3")  # re-downloaded meanwhile
        assert health.quarantine_track(conn, "pl_x", "t1", str(pl / "old.mp3")) is False
        assert new.exists() and db.get_track(conn, "pl_x", "t1")["downloaded"] is True
        skipped = db.list_history(conn, event="quarantine_skipped")[0]["data"]
        assert skipped["reason"].startswith("file changed since it was flagged")

    def test_file_outside_the_playlists_root_is_never_moved(self, conn, sandbox_dirs, tmp_path):
        outside = tmp_path / "elsewhere.mp3"
        outside.write_bytes(b"audio")
        db.insert_track(conn, "pl_x", ("t1", "S", "A", "Alb"))
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(outside))
        assert health.quarantine_track(conn, "pl_x", "t1", str(outside)) is False
        assert outside.exists() and db.get_track(conn, "pl_x", "t1")["downloaded"] is True
        assert db.list_history(conn, event="quarantine_skipped")[0]["data"]["reason"] == (
            "file is outside the playlists folder"
        )

    def test_daemon_drains_the_ui_queue(self, conn, sandbox_dirs):
        from spotify_slsk import runtime_state

        _, pl = sandbox_dirs
        f = _add(conn, pl, "t1", "S", "A", "x.mp3")
        runtime_state.enqueue_quarantine("pl_x", "t1", str(f))
        runtime_state.enqueue_quarantine("pl_x", "t1", str(f))  # double click
        daemon._process_quarantines(conn)
        assert not f.exists()
        assert runtime_state.pop_quarantines() == []
        assert len(db.list_history(conn, event="quarantined")) == 1

    def test_startup_reconcile_never_reads_the_quarantine(self, conn, sandbox_dirs):
        from spotify_slsk.library import reconcile

        _, pl = sandbox_dirs
        (pl / "pl_x").mkdir()
        qdir = pl / ".quarantine" / "pl_x"
        qdir.mkdir(parents=True)
        (qdir / "Mat Joe - Heart To Find.mp3").write_bytes(b"audio")
        db.insert_track(conn, "pl_x", ("t1", "Heart To Find", "Mat Joe", "Alb"))
        reconcile.startup_check(conn, "pl_x")
        assert db.get_track(conn, "pl_x", "t1")["status"] == "pending"
