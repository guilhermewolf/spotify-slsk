"""Library -> Rename (Tier 5): preview, confirm, daemon-side apply."""
from __future__ import annotations

import os

import pytest

from spotify_slsk import daemon, db, runtime_state
from spotify_slsk.library import rename
from tests.test_webui import post


@pytest.fixture(autouse=True)
def _empty_queue():
    runtime_state.pop_renames()
    yield
    runtime_state.pop_renames()


@pytest.fixture
def library(tmp_path, sandbox_dirs):
    """A playlist with three downloaded tracks: one to rename, one already
    named right, one whose file is gone."""
    _, playlists = sandbox_dirs
    folder = playlists / "pl_x"
    folder.mkdir()
    db_path = str(tmp_path / "t.db")
    conn = db.create_connection(db_path)
    db.upsert_playlist_meta(conn, "pid1", "pl_x", "Mix", "s")
    db.create_table(conn, "pl_x")
    for tid, name, fname in (
        ("t1", "Song One", "peer junk 01.mp3"),
        ("t2", "Song Two", "Artist - Song Two.mp3"),
        ("t3", "Song Three", "gone.mp3"),
    ):
        db.insert_track(conn, "pl_x", (tid, name, "Artist", "Album"))
        f = folder / fname
        if tid != "t3":
            f.write_bytes(tid.encode())
        db.update_download_status(conn, tid, "pl_x", success=True, file_path=str(f))
    db.set_setting(conn, "FILE_NAME_TEMPLATE", "{artist} - {title}")
    yield conn, db_path, folder
    conn.close()


def _client(db_path):
    from spotify_slsk.webui import create_app

    app = create_app(db_path, spotify_client=None)
    app.config.update(TESTING=True)
    return app.test_client()


class TestPlan:
    def test_lists_only_files_that_change_and_counts_skips(self, library):
        conn, _, folder = library
        plan, skipped = rename.rename_plan(conn, "pl_x", "{artist} - {title}")
        assert [(p["id"], p["old_name"], p["new_name"]) for p in plan] == [
            ("t1", "peer junk 01.mp3", "Artist - Song One.mp3"),
        ]
        assert skipped == 1  # t3's file is missing

    def test_outside_the_playlists_root_is_skipped(self, library, tmp_path):
        conn, _, _ = library
        outside = tmp_path / "elsewhere.mp3"
        outside.write_bytes(b"x")
        db.set_track_path(conn, "pl_x", "t1", str(outside))
        plan, skipped = rename.rename_plan(conn, "pl_x", "{artist} - {title}")
        assert plan == [] and skipped == 2

    def test_default_template_renames_nothing(self, library):
        conn, _, _ = library
        assert rename.rename_plan(conn, "pl_x", "{original_filename}")[0] == []


class TestNoChurn:
    def test_suffixed_file_is_not_renamed_again(self, library):
        conn, _, folder = library
        # t1 sits at "Artist - Song One (2).mp3" after an earlier collision.
        (folder / "peer junk 01.mp3").rename(folder / "Artist - Song One (2).mp3")
        db.set_track_path(conn, "pl_x", "t1", str(folder / "Artist - Song One (2).mp3"))
        plan, _ = rename.rename_plan(conn, "pl_x", "{artist} - {title}")
        assert plan == []
        assert rename.apply_rename(
            conn, "pl_x", "t1", str(folder / "Artist - Song One (2).mp3"), "Artist - Song One.mp3"
        ) is None
        assert (folder / "Artist - Song One (2).mp3").exists()
        assert db.list_history(conn, event="renamed") == []

    def test_already_named(self):
        assert rename.already_named("A - B (12).mp3", "A - B.mp3")
        assert not rename.already_named("A - B (x).mp3", "A - B.mp3")
        assert not rename.already_named("A - B.flac", "A - B.mp3")

    def test_preview_flags_collisions(self, library):
        conn, _, folder = library
        db.insert_track(conn, "pl_x", ("t5", "Song Five", "Artist", "Album"))
        (folder / "five.mp3").write_bytes(b"t5")
        db.update_download_status(conn, "t5", "pl_x", success=True, file_path=str(folder / "five.mp3"))
        plan, _ = rename.rename_plan(conn, "pl_x", "{artist}")  # everyone -> "Artist.mp3"
        assert [p["collision"] for p in plan] == [False, True, True]
        plan, _ = rename.rename_plan(conn, "pl_x", "{artist} - {title}")
        assert not any(p["collision"] for p in plan)


class TestApply:
    def test_renames_updates_db_and_history(self, library):
        conn, _, folder = library
        old = str(folder / "peer junk 01.mp3")
        new = rename.apply_rename(conn, "pl_x", "t1", old, "Artist - Song One.mp3")
        assert new == str(folder / "Artist - Song One.mp3")
        assert not os.path.exists(old) and open(new, "rb").read() == b"t1"
        assert db.get_track(conn, "pl_x", "t1")["path"] == new
        event = db.list_history(conn, event="renamed")[0]["data"]
        assert (event["old_path"], event["path"]) == (old, new)

    def test_collision_never_overwrites(self, library):
        conn, _, folder = library
        old = str(folder / "peer junk 01.mp3")
        new = rename.apply_rename(conn, "pl_x", "t1", old, "Artist - Song Two.mp3")
        assert new == str(folder / "Artist - Song Two (2).mp3")
        assert (folder / "Artist - Song Two.mp3").read_bytes() == b"t2"

    def test_db_failure_rolls_the_rename_back(self, library, monkeypatch):
        conn, _, folder = library
        old = str(folder / "peer junk 01.mp3")
        monkeypatch.setattr(rename, "set_track_path", lambda *a: False)
        assert rename.apply_rename(conn, "pl_x", "t1", old, "Artist - Song One.mp3") is None
        assert os.path.exists(old) and not (folder / "Artist - Song One.mp3").exists()
        assert db.get_track(conn, "pl_x", "t1")["path"] == old
        assert db.list_history(conn, event="renamed") == []

    def test_set_track_path_reports_failure(self, library):
        conn, _, _ = library
        assert db.set_track_path(conn, "pl_x", "t1", "/x.mp3") is True
        assert db.set_track_path(conn, "pl_x", "nope", "/x.mp3") is False
        assert db.set_track_path(conn, "pl_missing", "t1", "/x.mp3") is False

    def test_stale_or_unsafe_requests_are_skipped(self, library):
        conn, _, folder = library
        old = str(folder / "peer junk 01.mp3")
        assert rename.apply_rename(conn, "pl_x", "t1", "/other/path.mp3", "x.mp3") is None
        assert rename.apply_rename(conn, "pl_x", "t1", old, "../escape.mp3") is None
        assert rename.apply_rename(conn, "pl_x", "t1", old, "") is None
        assert rename.apply_rename(conn, "pl_x", "t3", str(folder / "gone.mp3"), "x.mp3") is None
        assert os.path.exists(old)
        assert db.list_history(conn, event="renamed") == []


class TestRoutes:
    def test_preview_confirm_then_queue_then_daemon_applies(self, library):
        conn, db_path, folder = library
        client = _client(db_path)
        html = client.get("/library/rename").get_data(as_text=True)
        assert "peer junk 01.mp3" in html and "Artist - Song One.mp3" in html
        assert "1 skipped" in html
        assert "Song Two.mp3</code>" not in html  # already right: not listed

        # First POST only asks for confirmation; nothing is queued.
        r = post(client, "/library/rename", data={"tracks": ["pid1:t1"]})
        assert b"Rename now" in r.data and b'name="confirm" value="1"' in r.data
        assert runtime_state.pending_renames() == 0

        post(client, "/library/rename", data={"tracks": ["pid1:t1"], "confirm": "1"})
        assert runtime_state.pending_renames() == 1
        daemon._process_renames(conn)
        assert (folder / "Artist - Song One.mp3").read_bytes() == b"t1"
        assert db.get_track(conn, "pl_x", "t1")["path"].endswith("Artist - Song One.mp3")

    def test_unknown_or_unneeded_selection_queues_nothing(self, library):
        _, db_path, _ = library
        client = _client(db_path)
        r = post(client, "/library/rename", data={"tracks": ["pid1:t2", "nope:t1"], "confirm": "1"},
                 follow_redirects=True)
        assert b"No files selected" in r.data
        assert runtime_state.pending_renames() == 0

    def test_requires_csrf(self, library):
        _, db_path, _ = library
        r = _client(db_path).post("/library/rename", data={"tracks": ["pid1:t1"], "confirm": "1"})
        assert r.status_code == 400

    def test_default_template_says_nothing_to_do(self, library):
        conn, db_path, _ = library
        db.delete_setting(conn, "FILE_NAME_TEMPLATE")
        html = _client(db_path).get("/library/rename").get_data(as_text=True)
        assert "nothing to rename" in html
