"""Tier 3 validation in the import path: the fake-lossless check, ISRC
evidence and the suspect-lossless upgrade.

The spectrum check itself is tested in test_audio_check.py; here
`check_file` is stubbed so the tests exercise the *decisions* (reject,
delete, blocklist, persist, upgrade) on a real temp filesystem.
"""
import shutil

import pytest

from spotify_slsk import config, db
from spotify_slsk.importer import audio_check as ac
from spotify_slsk.importer import process as importer_process
from spotify_slsk.library import acquire
from spotify_slsk.matching.normalize import version_tier
from spotify_slsk.models import Track


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    db.create_table(c, "pl_x")
    db.insert_track(c, "pl_x", ("t1", "Heart To Find", "Mat Joe", "Alb", 200_000, "GBAYE0601498"))
    yield c
    c.close()


@pytest.fixture
def tagged(monkeypatch):
    monkeypatch.setattr(
        importer_process, "extract_metadata_from_file",
        lambda p: ("Heart To Find", "Mat Joe", "Alb"),
    )
    monkeypatch.setattr(importer_process, "tag_audio_file", lambda *a, **k: True)


def _stub_check(monkeypatch, result):
    calls = []

    def _check(path):
        calls.append(path)
        return result

    monkeypatch.setattr(importer_process, "check_file", _check)
    return calls


SOURCE = {"user": "peer", "filename": "@@a\\Mat Joe - Heart To Find.flac", "size": 5}


def _download(downloads, name="Mat Joe - Heart To Find.flac"):
    f = downloads / name
    f.write_bytes(b"audio")
    return f


class TestLosslessCheckOnImport:
    def test_fake_flac_is_deleted_blocklisted_and_tried(self, conn, tagged, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _stub_check(monkeypatch, ac.AudioCheck(ac.FAKE, 16200, 21.0))
        f = _download(downloads)

        ok, final = importer_process.process_downloaded_file(
            str(f), "pl_x", conn, track_id="t1", source=SOURCE
        )

        assert (ok, final) == (False, None)
        assert not f.exists()
        # Thin margins: a false positive must not ban the file for every track.
        assert not db.is_blocklisted(conn, "peer", SOURCE["filename"])
        assert "Mat Joe - Heart To Find.flac" in db.get_tried_files(conn, "pl_x", "t1")
        rejected = db.list_history(conn, event="import_rejected")[0]
        assert rejected["data"]["reason"] == "fake lossless (cutoff 16.2 kHz, drop 21 dB)"
        assert db.get_track(conn, "pl_x", "t1")["downloaded"] is False

    @pytest.mark.parametrize("verdict,banned", [(ac.UPSAMPLED, False), (ac.PADDED_16, True)])
    def test_other_reject_verdicts(self, conn, tagged, monkeypatch, sandbox_dirs, verdict, banned):
        downloads, _ = sandbox_dirs
        _stub_check(monkeypatch, ac.AudioCheck(verdict, 22000, 40.0))
        f = _download(downloads)
        ok, _ = importer_process.process_downloaded_file(str(f), "pl_x", conn, track_id="t1", source=SOURCE)
        assert ok is False and not f.exists()
        # Only the exact padded-16 verdict bans the peer file globally.
        assert db.is_blocklisted(conn, "peer", SOURCE["filename"]) is banned

    def test_suspect_is_kept_recorded_and_made_due_for_upgrade(self, conn, tagged, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _stub_check(monkeypatch, ac.AudioCheck(ac.SUSPECT, 20000, 30.0))
        conn.execute("UPDATE pl_x SET last_upgrade_check = CURRENT_TIMESTAMP")
        conn.commit()

        ok, final = importer_process.process_downloaded_file(
            str(_download(downloads)), "pl_x", conn, track_id="t1", source=SOURCE
        )

        assert ok is True
        check = db.get_file_check(conn, "pl_x", "t1")
        assert (check["audio_check"], check["audio_cutoff_hz"], check["path"]) == ("suspect", 20000, final)
        assert check["problems"] == ["suspect_lossless"]
        assert [t.id for t, _ in db.get_upgrade_candidates(conn, "pl_x", 168)] == ["t1"]
        assert db.list_history(conn, event="imported")[0]["data"]["audio"].startswith("suspect")

    def test_unknown_never_rejects(self, conn, tagged, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _stub_check(monkeypatch, ac.AudioCheck(ac.UNKNOWN, error="CalledProcessError"))
        ok, _ = importer_process.process_downloaded_file(
            str(_download(downloads)), "pl_x", conn, track_id="t1"
        )
        assert ok is True
        assert db.get_file_check(conn, "pl_x", "t1")["audio_check"] == "unknown"

    def test_skipped_for_lossy_formats_reconcile_and_when_disabled(self, conn, tagged, monkeypatch, sandbox_dirs):
        downloads, playlists = sandbox_dirs
        calls = _stub_check(monkeypatch, ac.AudioCheck(ac.FAKE, 16000, 30.0))
        ok, _ = importer_process.process_downloaded_file(
            str(_download(downloads, "Mat Joe - Heart To Find.mp3")), "pl_x", conn, track_id="t1"
        )
        assert ok is True
        # The import still records a (lossy, unchecked) check row.
        assert db.get_file_check(conn, "pl_x", "t1")["audio_check"] is None

        (playlists / "pl_x").mkdir(exist_ok=True)
        in_library = playlists / "pl_x" / "Mat Joe - Heart To Find.flac"
        in_library.write_bytes(b"audio")
        importer_process.process_downloaded_file(str(in_library), "pl_x", conn, reconcile=True)
        assert in_library.exists()

        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        importer_process.process_downloaded_file(str(_download(downloads)), "pl_x", conn, track_id="t1")
        assert calls == []

    def test_heartbeat_is_touched_around_the_check(self, conn, tagged, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        _stub_check(monkeypatch, ac.AudioCheck(ac.OK, 21500, 3.0))
        touches = []
        monkeypatch.setattr(importer_process.runtime_state, "touch_heartbeat", lambda: touches.append(1))
        importer_process.process_downloaded_file(str(_download(downloads)), "pl_x", conn, track_id="t1")
        assert len(touches) == 2

    @pytest.mark.skipif(shutil.which("flac") is None, reason="flac binary not installed")
    def test_real_transcode_is_rejected_end_to_end(self, conn, tagged, sandbox_dirs):
        from tests.test_audio_check import _lowpass, _noise, _to_int, _write_flac

        downloads, _ = sandbox_dirs
        f = downloads / "Mat Joe - Heart To Find.flac"
        _write_flac(f, _to_int(_lowpass(_noise(44100), 44100, 16000), 16), 44100, 16)
        ok, _ = importer_process.process_downloaded_file(str(f), "pl_x", conn, track_id="t1", source=SOURCE)
        assert ok is False and not f.exists()
        assert db.list_history(conn, event="import_rejected")[0]["data"]["reason"].startswith("fake lossless (cutoff 16.")


class TestSuspectUpgrade:
    def test_suspect_file_is_re_searched_for_a_lossless_copy(self, conn, monkeypatch, sandbox_dirs):
        _, playlists = sandbox_dirs
        current = playlists / "Mat Joe - Heart To Find.flac"
        current.write_bytes(b"x")
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(current))
        db.save_file_check(conn, "pl_x", "t1", path=str(current), size=1, mtime=1.0,
                           rules_version=0, audio_check="suspect")
        seen = {}

        def _search(**kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr(acquire, "search_and_download", _search)
        track = Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x")
        acquire.try_upgrade_track(track, "pl_x", conn, str(current))

        assert seen["lossless_only"] is True
        # The same version is allowed; the current file never again.
        assert seen["max_version_tier"] == version_tier(current.name) + 1
        assert current.name in db.get_tried_files(conn, "pl_x", "t1")

    def test_ok_file_at_top_tier_is_left_alone(self, conn, monkeypatch, sandbox_dirs):
        monkeypatch.setattr(acquire, "search_and_download", lambda **k: pytest.fail("searched"))
        track = Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x")
        acquire.try_upgrade_track(track, "pl_x", conn, "/x/Mat Joe - Heart To Find (Extended Mix).flac")


class TestLosslessOnlyCandidates:
    def test_lossy_files_are_rejected_when_replacing_a_suspect_file(self):
        from spotify_slsk.matching.candidates import evaluate_candidates

        results = [{"username": "u", "files": [
            {"filename": "Mat Joe - Heart To Find.mp3", "size": 8_000_000, "length": 200, "bitRate": 320},
            {"filename": "Mat Joe - Heart To Find.flac", "size": 25_000_000, "length": 200},
        ]}]
        out = evaluate_candidates(results, "Heart To Find", "Mat Joe", lossless_only=True)
        assert [(c["ext"], c["accepted"]) for c in out] == [(".mp3", False), (".flac", True)]
        assert out[0]["reason"].startswith("not lossless")


class TestFlacPlausibility:
    """Metadata-only, before anything is downloaded."""

    def _eval(self, size, length=200, bit_depth=None, sample_rate=None):
        from spotify_slsk.matching.candidates import evaluate_candidates

        f = {"filename": "Mat Joe - Heart To Find.flac", "size": size, "length": length,
             "bitDepth": bit_depth, "sampleRate": sample_rate}
        return evaluate_candidates([{"username": "u", "files": [f]}], "Heart To Find", "Mat Joe")[0]

    def test_cd_flac_below_400_kbps_is_rejected(self):
        c = self._eval(size=7_500_000)  # 300 kbps
        assert c["accepted"] is False
        assert c["reason"].startswith("FLAC at 300 kbps is implausibly low for 16-bit/44.1 kHz")

    def test_normal_cd_flac_is_accepted(self):
        c = self._eval(size=22_000_000, bit_depth=16, sample_rate=44100)  # 880 kbps
        assert c["accepted"] is True and c["note"] is None

    def test_thin_hi_res_is_demoted_not_rejected(self):
        thin = self._eval(size=22_500_000, bit_depth=24, sample_rate=96000)  # 900 kbps
        assert thin["accepted"] is True
        assert thin["note"].startswith("likely upsampled: 900 kbps for 24-bit/96 kHz")
        fat = self._eval(size=60_000_000, bit_depth=24, sample_rate=96000)
        assert fat["note"] is None

    def test_demoted_sorts_after_its_peers(self):
        from spotify_slsk.matching.candidates import sort_candidates

        thin = dict(self._eval(size=22_500_000, bit_depth=24, sample_rate=96000), user="thin")
        cd = dict(self._eval(size=22_000_000, bit_depth=16, sample_rate=44100), user="cd")
        assert [c["user"] for c in sort_candidates([thin, cd])] == ["cd", "thin"]


class TestIsrcTags:
    def test_normalize(self):
        from spotify_slsk.importer.tags import normalize_isrc

        assert normalize_isrc("gb-aye-06-01498") == "GBAYE0601498"
        assert normalize_isrc(b"USUM71703861") == "USUM71703861"
        assert normalize_isrc("not an isrc") is None
        assert normalize_isrc(None) is None

    def test_reads_id3_and_mp4_shapes(self, monkeypatch):
        from mutagen.id3 import ID3, TSRC

        from spotify_slsk.importer import tags

        id3 = ID3()
        id3.add(TSRC(encoding=3, text="GBAYE0601498"))
        monkeypatch.setattr(tags, "MutagenFile", lambda p: type("F", (), {"tags": id3})())
        assert tags.read_isrc("x.mp3") == "GBAYE0601498"
        mp4 = {"----:com.apple.iTunes:ISRC": [b"USUM71703861"]}
        monkeypatch.setattr(tags, "MutagenFile", lambda p: type("F", (), {"tags": mp4})())
        assert tags.read_isrc("x.m4a") == "USUM71703861"
        monkeypatch.setattr(tags, "MutagenFile", lambda p: (_ for _ in ()).throw(OSError("x")))
        assert tags.read_isrc("x.mp3") is None

    @pytest.mark.skipif(shutil.which("flac") is None, reason="flac binary not installed")
    def test_flac_round_trip(self, tmp_path):
        from spotify_slsk.importer.tags import read_isrc, write_tags
        from tests.test_audio_check import _noise, _to_int, _write_flac

        f = tmp_path / "a.flac"
        _write_flac(f, _to_int(_noise(44100, 1), 16), 44100, 16)
        assert read_isrc(str(f)) is None
        write_tags(str(f), "T", "A", "Alb", isrc="GBAYE0601498")
        assert read_isrc(str(f)) == "GBAYE0601498"
        write_tags(str(f), "T", "A", "Alb")  # no ISRC known: keep the file's
        assert read_isrc(str(f)) == "GBAYE0601498"


class TestIsrcOnImport:
    def _run(self, conn, monkeypatch, downloads, title, artist, isrc):
        monkeypatch.setattr(importer_process, "extract_metadata_from_file", lambda p: (title, artist, "Alb"))
        tagged = []
        monkeypatch.setattr(importer_process, "tag_audio_file", lambda *a, **k: tagged.append(k) or True)
        monkeypatch.setattr(importer_process, "read_isrc", lambda p: isrc)
        monkeypatch.setattr(config, "LOSSLESS_CHECK_ENABLED", False)
        f = _download(downloads, "x.mp3")
        ok, _ = importer_process.process_downloaded_file(str(f), "pl_x", conn, track_id="t1", source=SOURCE)
        return ok, f, tagged

    def test_different_isrc_alone_never_rejects(self, conn, monkeypatch, sandbox_dirs):
        # Single vs album vs remaster: same song, different ISRC.
        downloads, _ = sandbox_dirs
        monkeypatch.setattr(importer_process, "audio_length_seconds", lambda p: 201.0)
        ok, _, tagged = self._run(conn, monkeypatch, downloads, "Heart To Find", "Mat Joe", "USUM71703861")
        assert ok is True
        event = db.list_history(conn, event="imported")[0]["data"]
        assert event["isrc_mismatch"] == "ISRC mismatch (file USUM71703861, expected GBAYE0601498)"
        assert tagged[0]["isrc"] is None  # the file's own ISRC is kept

    def test_different_isrc_plus_duration_off_rejects(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        monkeypatch.setattr(importer_process, "audio_length_seconds", lambda p: 260.0)
        ok, f, _ = self._run(conn, monkeypatch, downloads, "Heart To Find", "Mat Joe", "USUM71703861")
        assert ok is False and not f.exists()
        assert db.list_blocklist(conn) == []
        assert "x.mp3" in db.get_tried_files(conn, "pl_x", "t1")
        assert db.list_history(conn, event="import_rejected")[0]["data"]["reason"] == (
            "duration 4:20 vs 3:20 + ISRC mismatch (file USUM71703861, expected GBAYE0601498)"
        )

    def test_different_isrc_plus_low_score_rejects_with_both_reasons(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        ok, _, _ = self._run(conn, monkeypatch, downloads, "Eyes On Fire", "Blue Foundation", "USUM71703861")
        assert ok is False
        assert db.list_history(conn, event="import_rejected")[0]["data"]["reason"].startswith(
            "low score + ISRC mismatch"
        )

    def test_equal_isrc_overrides_a_low_tag_score(self, conn, monkeypatch, sandbox_dirs):
        # Tags in another script/spelling: no artist overlap, same recording.
        downloads, _ = sandbox_dirs
        ok, _, tagged = self._run(conn, monkeypatch, downloads, "ハート", "マット", "GBAYE0601498")
        assert ok is True
        assert db.list_history(conn, event="imported")[0]["data"]["match"] == "isrc"
        assert tagged[0]["isrc"] == "GBAYE0601498"

    def test_equal_isrc_never_overrides_a_version_mismatch(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        ok, f, _ = self._run(conn, monkeypatch, downloads, "Heart To Find (Live)", "Mat Joe", "GBAYE0601498")
        assert ok is False and not f.exists()

    def test_no_file_isrc_falls_back_to_tags_and_writes_spotifys(self, conn, monkeypatch, sandbox_dirs):
        downloads, _ = sandbox_dirs
        ok, _, tagged = self._run(conn, monkeypatch, downloads, "Heart To Find", "Mat Joe", None)
        assert ok is True and tagged[0]["isrc"] == "GBAYE0601498"


class TestQualityUpgrade:
    """Tier 4: a file below the profile cutoff is re-searched for the same
    version in a strictly better quality - only with upgrades allowed."""

    @pytest.fixture
    def profile(self, monkeypatch):
        from spotify_slsk.matching.quality import make_profile

        def _set(upgrades=True, allowed=("FLAC_24", "FLAC_16"), cutoff="FLAC_24"):
            monkeypatch.setattr(config, "QUALITY_PROFILE",
                                make_profile(list(allowed), cutoff, upgrades)[0])
        return _set

    def _row(self, conn, sandbox_dirs, name="Mat Joe - Heart To Find (Extended Mix).flac",
             quality="FLAC_16"):
        _, playlists = sandbox_dirs
        current = playlists / name
        current.write_bytes(b"x")
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(current))
        if quality:
            db.set_track_quality(conn, "pl_x", "t1", quality)
        return current

    def _capture(self, monkeypatch):
        seen = {}

        def _search(**kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr(acquire, "search_and_download", _search)
        return seen

    def test_below_cutoff_top_tier_is_searched_for_better_quality(
        self, conn, monkeypatch, sandbox_dirs, profile
    ):
        profile(upgrades=True)
        current = self._row(conn, sandbox_dirs)
        seen = self._capture(monkeypatch)
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))
        assert seen["better_quality_than"] == "FLAC_16"
        assert seen["max_version_tier"] == 0 and seen["lossless_only"] is False

    def test_upgrades_off_means_no_quality_search(self, conn, monkeypatch, sandbox_dirs, profile):
        profile(upgrades=False)
        current = self._row(conn, sandbox_dirs)
        monkeypatch.setattr(acquire, "search_and_download", lambda **k: pytest.fail("searched"))
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))

    def test_at_cutoff_is_left_alone(self, conn, monkeypatch, sandbox_dirs, profile):
        profile(upgrades=True, cutoff="FLAC_16")
        current = self._row(conn, sandbox_dirs)
        monkeypatch.setattr(acquire, "search_and_download", lambda **k: pytest.fail("searched"))
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))

    def test_tier_upgrade_unaffected_when_quality_meets_cutoff(
        self, conn, monkeypatch, sandbox_dirs, profile
    ):
        profile(upgrades=True, cutoff="FLAC_16")
        current = self._row(conn, sandbox_dirs, name="Mat Joe - Heart To Find.flac")
        seen = self._capture(monkeypatch)
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))
        assert seen["better_quality_than"] is None
        assert seen["max_version_tier"] == version_tier(current.name)

    def test_quality_is_backfilled_from_the_file(self, conn, monkeypatch, sandbox_dirs, profile):
        profile(upgrades=True)
        current = self._row(conn, sandbox_dirs, quality=None)
        monkeypatch.setattr(acquire, "quality_of_file", lambda p: "FLAC_16")
        seen = self._capture(monkeypatch)
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))
        assert db.get_track(conn, "pl_x", "t1")["quality"] == "FLAC_16"
        assert seen["better_quality_than"] == "FLAC_16"

    def test_suspect_stays_on_the_lossless_path(self, conn, monkeypatch, sandbox_dirs, profile):
        profile(upgrades=True)
        current = self._row(conn, sandbox_dirs, quality="FLAC_24")
        db.save_file_check(conn, "pl_x", "t1", path=str(current), size=1, mtime=1.0,
                           rules_version=0, audio_check="suspect")
        seen = self._capture(monkeypatch)
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))
        assert seen["lossless_only"] is True and seen["better_quality_than"] is None


class TestBetterQualityCandidates:
    def _eval(self, monkeypatch, *files, **kw):
        from spotify_slsk.matching.candidates import evaluate_candidates
        from spotify_slsk.matching.quality import make_profile

        monkeypatch.setattr(config, "QUALITY_PROFILE",
                            make_profile(["FLAC_24", "FLAC_16"], "FLAC_24", True)[0])
        return evaluate_candidates(
            [{"username": "u", "files": list(files)}], "Heart To Find", "Mat Joe", **kw)

    def _flac(self, name, depth):
        return {"filename": name, "size": 90_000_000 if depth == 24 else 33_000_000,
                "length": 300, "bitDepth": depth, "sampleRate": 96000 if depth == 24 else 44100}

    def test_same_tier_needs_strictly_better_quality(self, monkeypatch):
        hi, cd = self._eval(
            monkeypatch,
            self._flac("Mat Joe - Heart To Find (Extended Mix).flac", 24),
            self._flac("Mat Joe - Heart To Find (Extended Mix).flac", 16),
            max_version_tier=0, better_quality_than="FLAC_16",
        )
        assert hi["accepted"] is True
        assert cd["reason"] == "not a better version or quality than the current file"

    def test_worse_version_is_never_a_quality_upgrade(self, monkeypatch):
        (plain,) = self._eval(
            monkeypatch, self._flac("Mat Joe - Heart To Find.flac", 24),
            max_version_tier=1, better_quality_than="FLAC_16",
        )
        assert plain["accepted"] is False


class TestVersionUpgradeNeverLowersQuality:
    def _eval(self, monkeypatch, *files, **kw):
        from spotify_slsk.matching.candidates import evaluate_candidates
        from spotify_slsk.matching.quality import make_profile

        monkeypatch.setattr(config, "QUALITY_PROFILE", make_profile(
            ["FLAC_24", "FLAC_16", "MP3_320"], "MP3_320", False)[0])
        return evaluate_candidates(
            [{"username": "u", "files": list(files)}], "Heart To Find", "Mat Joe", **kw)

    EXT_MP3 = {"filename": "Mat Joe - Heart To Find (Extended Mix).mp3",
               "size": 12_000_000, "length": 300, "bitRate": 320}
    EXT_FLAC = {"filename": "Mat Joe - Heart To Find (Extended Mix).flac",
                "size": 33_000_000, "length": 300, "bitDepth": 16}

    def test_better_tier_at_lower_quality_is_rejected(self, monkeypatch):
        mp3, flac = self._eval(monkeypatch, self.EXT_MP3, self.EXT_FLAC,
                               max_version_tier=2, current_quality="FLAC_16")
        assert mp3["reason"] == "lower quality than the current file (FLAC 16-bit)"
        assert flac["accepted"] is True

    def test_unknown_current_quality_allows_as_before(self, monkeypatch):
        (mp3,) = self._eval(monkeypatch, self.EXT_MP3, max_version_tier=2)
        assert mp3["accepted"] is True

    def _upgrade(self, conn, monkeypatch, sandbox_dirs, name, quality):
        _, playlists = sandbox_dirs
        current = playlists / name
        current.write_bytes(b"x")
        db.update_download_status(conn, "t1", "pl_x", success=True, file_path=str(current))
        if quality:
            db.set_track_quality(conn, "pl_x", "t1", quality)
        else:
            monkeypatch.setattr(acquire, "quality_of_file", lambda p: None)
        seen = {}
        monkeypatch.setattr(acquire, "search_and_download", lambda **k: seen.update(k))
        acquire.try_upgrade_track(Track("t1", "Heart To Find", "Mat Joe", "Alb", "pl_x"),
                                  "pl_x", conn, str(current))
        return seen

    def test_upgrade_pass_passes_the_current_quality(self, conn, monkeypatch, sandbox_dirs):
        seen = self._upgrade(conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.flac",
                             "FLAC_16")
        assert seen["current_quality"] == "FLAC_16" and seen["lossless_only"] is False

    def test_unknown_quality_lossless_file_needs_lossless(self, conn, monkeypatch, sandbox_dirs):
        seen = self._upgrade(conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.flac", None)
        assert seen["current_quality"] is None and seen["lossless_only"] is True

    def test_unknown_quality_lossy_file_as_before(self, conn, monkeypatch, sandbox_dirs):
        seen = self._upgrade(conn, monkeypatch, sandbox_dirs, "Mat Joe - Heart To Find.mp3", None)
        assert seen["current_quality"] is None and seen["lossless_only"] is False
