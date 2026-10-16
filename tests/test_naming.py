"""FILE_NAME_TEMPLATE (Tier 5): rendering, Lidarr-style sanitising,
validation, and collision-safe placement of newly imported files."""
from __future__ import annotations

import pytest

from spotify_slsk import config, db
from spotify_slsk.config import validate_setting
from spotify_slsk.importer import process as importer_process
from spotify_slsk.library import naming

ROW = {
    "id": "t1", "name": "Desires Ignite - Radio Edit", "artists": "Reber, Kris M",
    "album": "Desires Ignite", "album_artist": "Reber", "track_number": 2,
    "disc_number": 1, "release_date": "2021-03-05", "position": 7,
    "isrc": "GBAYE0601498",
}
PEER = "03 - reber - desires ignite (radio edit).flac"


class TestRender:
    def test_default_keeps_the_peer_basename_untouched(self):
        assert naming.file_name(naming.DEFAULT_TEMPLATE, ROW, PEER) == PEER
        # Even characters the sanitiser would replace: today's behaviour.
        assert naming.file_name(naming.DEFAULT_TEMPLATE, ROW, "a: b?.mp3") == "a: b?.mp3"

    def test_all_tokens(self):
        t = "{playlist_index:03} {artist} - {title} [{album} {album_artist} {track:02} {disc} {year} {isrc} {quality}]"
        assert naming.file_name(t, ROW, PEER, "FLAC_16") == (
            "007 Reber, Kris M - Desires Ignite - Radio Edit "
            "[Desires Ignite Reber 02 1 2021 GBAYE0601498 FLAC 16-bit].flac"
        )

    def test_original_filename_token_and_extension_implied(self):
        assert naming.file_name("{track:02} {original_filename}", ROW, PEER) == (
            "02 03 - reber - desires ignite (radio edit).flac"
        )

    def test_missing_values_leave_no_dangling_separators(self):
        row = {"name": "Song", "artists": "Artist"}
        assert naming.file_name("{artist} - {album} - {title}", row, "x.mp3") == "Artist - Song.mp3"
        assert naming.file_name("{track:02} - {title} ({year})", row, "x.mp3") == "Song.mp3"

    def test_empty_render_falls_back_to_original(self):
        assert naming.file_name("{album}", {"name": "S"}, "peer.mp3") == "peer.mp3"


class TestSanitise:
    @pytest.mark.parametrize("title,expected", [
        ("Re: Stacks", "Re - Stacks.mp3"),
        ("12:34", "12-34.mp3"),
        ("AC/DC\\Live", "AC+DC+Live.mp3"),
        ('Why? <Not> "Me" | *', "Why! Not Me.mp3"),
        ("Tab\there\x00", "Tabhere.mp3"),
        ("...Baby One More Time...", "Baby One More Time.mp3"),
        ("  spaced  out  ", "spaced out.mp3"),
    ])
    def test_illegal_characters(self, title, expected):
        assert naming.file_name("{title}", {"name": title}, "x.mp3") == expected

    @pytest.mark.parametrize("title", ["CON", "nul", "Com1", "LPT9", "aux"])
    def test_reserved_windows_names(self, title):
        assert naming.file_name("{title}", {"name": title}, "x.flac") == f"{title}_.flac"

    def test_reserved_only_as_whole_name(self):
        assert naming.file_name("{title}", {"name": "Console"}, "x.flac") == "Console.flac"

    def test_length_cap_in_bytes_on_a_character_boundary(self):
        name = naming.file_name("{title}", {"name": "é" * 300}, "x.flac")
        assert len(name.encode()) <= naming.MAX_NAME_BYTES
        assert name.endswith(".flac") and name.encode().decode() == name


class TestValidate:
    @pytest.mark.parametrize("template,error", [
        ("{artist} - {nope}", "unknown token {nope}"),
        ("{title:02}", "{title} is text"),
        ("{artist}/{title}", "not a path"),
        ("{artist} - {title", "unbalanced"),
        ("plain", "at least one {token}"),
        ("   ", "must not be empty"),
    ])
    def test_rejects(self, template, error):
        assert error in naming.validate_template(template)
        normalised, message = validate_setting("template", template)
        assert normalised is None and error in message

    def test_accepts(self):
        assert validate_setting("template", "{track:02} - {artist} - {title}") == (
            "{track:02} - {artist} - {title}", None,
        )

    def test_bad_stored_template_keeps_previous(self, tmp_path, monkeypatch):
        conn = db.create_connection(str(tmp_path / "t.db"))
        monkeypatch.setattr(config, "FILE_NAME_TEMPLATE", "{title}")
        db.set_setting(conn, "FILE_NAME_TEMPLATE", "{bogus}")
        config.reload(conn)
        assert config.FILE_NAME_TEMPLATE == "{title}"
        db.set_setting(conn, "FILE_NAME_TEMPLATE", "{artist} - {title}")
        config.reload(conn)
        assert config.FILE_NAME_TEMPLATE == "{artist} - {title}"
        conn.close()


class TestSettingsPage:
    def test_example_rendered_and_unknown_token_refused(self, tmp_path):
        from spotify_slsk.webui import create_app
        from tests.test_webui import post

        db.create_connection(str(tmp_path / "ui.db")).close()
        app = create_app(str(tmp_path / "ui.db"), spotify_client=None)
        app.config.update(TESTING=True)
        client = app.test_client()
        html = client.get("/settings").get_data(as_text=True)
        assert f"Example: <code>{naming.EXAMPLE_ORIGINAL}</code>" in html
        r = post(client, "/settings", data={"FILE_NAME_TEMPLATE": "{artist} - {nope}"}, follow_redirects=True)
        assert "unknown token {nope}" in r.get_data(as_text=True)
        post(client, "/settings", data={"FILE_NAME_TEMPLATE": "{track:02} - {title}"})
        html = client.get("/settings").get_data(as_text=True)
        assert "Example: <code>02 - Desires Ignite - Radio Edit.flac</code>" in html


class TestImportPlacement:
    def _import(self, conn, monkeypatch, downloads, name):
        monkeypatch.setattr(importer_process, "extract_metadata_from_file", lambda p: ("Song", "Artist", None))
        monkeypatch.setattr(importer_process, "tag_audio_file", lambda *a, **k: True)
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        f = downloads / name
        f.write_bytes(b"new audio")
        return importer_process.process_downloaded_file(str(f), "pl_x", conn, track_id="t1")

    @pytest.fixture
    def conn(self, tmp_path):
        c = db.create_connection(str(tmp_path / "t.db"))
        db.create_table(c, "pl_x")
        db.insert_track(c, "pl_x", ("t1", "Song", "Artist", "Album"), {"track_number": 4})
        yield c
        c.close()

    def test_template_names_new_imports(self, conn, monkeypatch, sandbox_dirs):
        downloads, playlists = sandbox_dirs
        monkeypatch.setattr(config, "FILE_NAME_TEMPLATE", "{track:02} - {artist} - {title}")
        ok, final = self._import(conn, monkeypatch, downloads, "peer name.mp3")
        assert ok and final == str(playlists / "pl_x" / "04 - Artist - Song.mp3")

    def test_never_overwrites_an_existing_file(self, conn, monkeypatch, sandbox_dirs):
        downloads, playlists = sandbox_dirs
        monkeypatch.setattr(config, "FILE_NAME_TEMPLATE", naming.DEFAULT_TEMPLATE)
        existing = playlists / "pl_x" / "peer name.mp3"
        existing.parent.mkdir()
        existing.write_bytes(b"another track's audio")
        ok, final = self._import(conn, monkeypatch, downloads, "peer name.mp3")
        assert ok and final == str(playlists / "pl_x" / "peer name (2).mp3")
        assert existing.read_bytes() == b"another track's audio"
