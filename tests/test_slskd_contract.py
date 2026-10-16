"""Contract tests for the slskd boundary.

Two halves:

* **Shapes** - fixture JSON in slskd's real wire format (camelCase, missing
  optional fields, locked files, every transfer state seen in production)
  parsed through ``slskd.models`` and judged by ``evaluate_candidates``.
* **Failures** - slskd HTTP errors (401, 404 search gone, 500) and
  transport failures (connection refused, timeouts) raised by the
  slskd-api client at every call site we use: search create / poll /
  responses / delete, enqueue, transfer polling. None may crash the cycle,
  every created search is deleted, and the _tried / blocklist / history
  decisions follow the documented policy (only a definitive peer-file
  fault blocklists; a transient API error records nothing permanent).

slskd-api attaches ``raise_for_status`` to every response, so an HTTP
error reaches us as ``requests.HTTPError`` - which is what the fakes raise.
"""
import copy
import json
import pathlib

import pytest
import requests

from spotify_slsk import config, db
from spotify_slsk.library import acquire
from spotify_slsk.matching.candidates import evaluate_candidates
from spotify_slsk.matching.quality import profile_from_formats
from spotify_slsk.models import Track
from spotify_slsk.slskd import client as slskd_client
from spotify_slsk.slskd import search as slskd_search
from spotify_slsk.slskd import transfers as slskd_transfers
from spotify_slsk.slskd.models import (
    PeerFile,
    Search,
    SearchResponse,
    Transfer,
    TransferOutcome,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "slskd"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


MULTI = _load("search_responses_multi_peer.json")
TRANSFERS = _load("transfer_states.json")


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = "http://slskd.invalid:5030/api/v0/x"
    return requests.HTTPError(f"{status} Client Error", response=response)


ERRORS = {
    "401": lambda: _http_error(401),
    "404": lambda: _http_error(404),
    "500": lambda: _http_error(500),
    "connection_refused": lambda: requests.ConnectionError("[Errno 111] Connection refused"),
    "connect_timeout": lambda: requests.ConnectTimeout("connect timed out"),
    "read_timeout": lambda: requests.ReadTimeout("read timed out"),
}


@pytest.fixture(autouse=True)
def _default_tunables(monkeypatch):
    """The expectations below assume the shipped defaults."""
    monkeypatch.setattr(
        config, "QUALITY_PROFILE",
        profile_from_formats([".flac", ".mp3", ".aiff", ".wav"]),
    )
    monkeypatch.setattr(config, "MIN_EFFECTIVE_MP3_KBPS", 280)
    monkeypatch.setattr(config, "MAX_DURATION_DIFF_SECONDS", 15)
    monkeypatch.setattr(config, "EARLY_STOP_RESPONSES", 20)
    monkeypatch.setattr(config, "MIN_PEER_UPLOAD_SPEED", 0)


@pytest.fixture
def conn(tmp_path):
    c = db.create_connection(str(tmp_path / "t.db"))
    db.create_table(c, "pl_x")
    db.insert_track(c, "pl_x", ("t1", "Lose Yourself", "Eminem", "Alb"))
    yield c
    c.close()


# ---- shapes -----------------------------------------------------------------


class TestSearchResponseParsing:
    def test_lossless_file_has_no_bitrate_but_depth_and_rate(self):
        peer = SearchResponse.from_json(MULTI["responses"][0])
        flac = peer.files[0]
        assert flac.bit_rate is None
        assert (flac.bit_depth, flac.sample_rate) == (24, 88200)

    def test_empty_extension_field_falls_back_to_the_filename(self):
        flac = SearchResponse.from_json(MULTI["responses"][0]).files[0]
        assert flac.extension == ""
        assert flac.ext == ".flac"
        assert flac.basename == "08 - Daft Punk - Get Lucky.flac"

    def test_vbr_flag_and_reported_bitrate(self):
        mp3 = SearchResponse.from_json(MULTI["responses"][1]).files[0]
        assert mp3.is_variable_bit_rate is True and mp3.bit_rate == 245

    def test_locked_files_are_parsed_separately(self):
        peer = SearchResponse.from_json(MULTI["responses"][0])
        assert [f.is_locked for f in peer.locked_files] == [True]
        assert all(not f.is_locked for f in peer.files)

    def test_missing_optional_fields_default_instead_of_raising(self):
        peer = SearchResponse.from_json(MULTI["responses"][4])
        assert peer.upload_speed == 0
        assert peer.has_free_upload_slot is None and peer.queue_length is None
        assert peer.files[0].length is None and peer.files[0].bit_rate is None
        assert peer.files[1].filename == ""

    def test_null_username_defaults(self):
        assert SearchResponse.from_json({"username": None, "files": []}).username == "unknown"

    def test_client_has_an_http_timeout(self, monkeypatch):
        # A slskd that accepts but never answers must not hang the daemon.
        seen = {}
        monkeypatch.setattr(
            slskd_client.slskd_api, "SlskdClient", lambda **kw: seen.update(kw)
        )
        monkeypatch.delenv("SLSKD_HTTP_TIMEOUT", raising=False)
        slskd_client.new_client()
        assert seen["timeout"] == 30.0

    @pytest.mark.parametrize("garbage", [None, [], "x", 3])
    def test_non_dict_payloads_parse_to_empty_values(self, garbage):
        assert SearchResponse.from_json(garbage).files == []
        assert PeerFile.from_json(garbage).filename == ""
        assert Search.from_json(garbage).id is None
        assert Transfer.list_from_user_json(garbage) == []

    def test_null_numbers_and_bools_are_not_numbers(self):
        f = PeerFile.from_json({"filename": "a.mp3", "bitRate": None, "length": True, "size": "12"})
        assert (f.bit_rate, f.length, f.size) == (None, None, None)


class TestMultiPeerEvaluation:
    def _evaluate(self):
        t = MULTI["track"]
        return evaluate_candidates(
            MULTI["responses"], t["title"], t["artist"], expected_duration_ms=t["duration_ms"]
        )

    def test_every_offered_file_gets_the_expected_verdict(self):
        got = {(c["user"], c["basename"]): (c["accepted"], c["reason"]) for c in self._evaluate()}
        want = {
            (e["user"], e["basename"]): (e["accepted"], e.get("reason"))
            for e in MULTI["expected"]
        }
        assert got == want

    def test_locked_files_never_become_candidates(self):
        names = {c["basename"] for c in self._evaluate()}
        assert "08 - Get Lucky (Hi-Res).flac" not in names

    def test_peer_metadata_is_carried_onto_the_candidate(self):
        by_user = {c["user"]: c for c in self._evaluate() if c["accepted"]}
        assert by_user["lossless_peer"]["bit_depth"] == 24
        assert by_user["lossless_peer"]["sample_rate"] == 88200
        assert by_user["cbr_peer"]["queue_length"] == 0
        assert by_user["cbr_peer"]["user_upload_speed"] == 2097152
        assert by_user["sparse_peer"]["free_slot"] is None

    def test_raw_dicts_and_parsed_responses_evaluate_identically(self):
        t = MULTI["track"]
        parsed = [SearchResponse.from_json(r) for r in MULTI["responses"]]
        assert evaluate_candidates(
            parsed, t["title"], t["artist"], expected_duration_ms=t["duration_ms"]
        ) == self._evaluate()


class TestTransferStateContract:
    CASES = TRANSFERS["cases"]

    @pytest.mark.parametrize("case", CASES, ids=[c["state"] for c in CASES])
    def test_outcome(self, case):
        outcome = Transfer(id="x", filename="f", state=case["state"]).outcome
        assert outcome == (TransferOutcome(case["expected"]) if case["expected"] else None)

    @pytest.mark.parametrize("case", CASES, ids=[c["state"] for c in CASES])
    def test_only_errored_rejected_aborted_are_the_peers_fault(self, case):
        fault = Transfer(id="x", filename="f", state=case["state"]).peer_fault
        assert fault is (case["state"] in (
            "Completed, Errored", "Completed, Rejected", "Completed, Aborted",
        ))

    def test_user_payload_is_flattened_across_directories(self):
        payload = copy.deepcopy(TRANSFERS["template"])
        payload["directories"].append({"directory": "x", "files": [
            {"id": "2", "filename": "x\\b.flac", "size": 2, "state": "InProgress"},
        ]})
        payload["directories"].append({"directory": "empty", "files": None})
        out = Transfer.list_from_user_json(payload)
        assert [(t.id, t.basename) for t in out] == [
            ("00000000-0000-0000-0000-000000000001", "Artist - Song (Original Mix).flac"),
            ("2", "b.flac"),
        ]


# ---- failures: searches -----------------------------------------------------


class _FakeSearches:
    def __init__(self, fail_on=None, error=None, state="Completed", responses=None):
        self.fail_on, self.error = fail_on, error
        self._state, self._responses = state, responses or []
        self.deleted = []

    def _maybe_fail(self, op):
        if op == self.fail_on:
            raise self.error()

    def search_text(self, **kwargs):
        self._maybe_fail("create")
        return {"id": "sid-1", "state": "InProgress"}

    def state(self, search_id):
        self._maybe_fail("poll")
        return {"id": search_id, "state": self._state}

    def search_responses(self, search_id):
        self._maybe_fail("responses")
        return self._responses

    def delete(self, search_id):
        self.deleted.append(search_id)
        self._maybe_fail("delete")
        return True


def _client_with(searches=None, transfers=None):
    client = type("Client", (), {})()
    client.searches = searches
    client.transfers = transfers
    return client


class TestSearchHttpErrors:
    @pytest.mark.parametrize("error", ERRORS.values(), ids=ERRORS.keys())
    def test_create_failure_returns_nothing_and_has_nothing_to_delete(self, monkeypatch, error):
        fake = _FakeSearches("create", error)
        monkeypatch.setattr(slskd_client, "get_client", lambda: _client_with(fake))
        assert slskd_search._run_one_search("q", timeout=1) == []
        assert fake.deleted == []

    @pytest.mark.parametrize("op", ["poll", "responses"])
    @pytest.mark.parametrize("error", ERRORS.values(), ids=ERRORS.keys())
    def test_failure_after_create_still_deletes_the_search(self, monkeypatch, error, op):
        fake = _FakeSearches(op, error)
        monkeypatch.setattr(slskd_client, "get_client", lambda: _client_with(fake))
        assert slskd_search._run_one_search("q", timeout=1) == []
        assert fake.deleted == ["sid-1"]

    @pytest.mark.parametrize("error", ERRORS.values(), ids=ERRORS.keys())
    def test_delete_failure_does_not_lose_the_results(self, monkeypatch, error):
        fake = _FakeSearches("delete", error, responses=MULTI["responses"])
        monkeypatch.setattr(slskd_client, "get_client", lambda: _client_with(fake))
        out = slskd_search._run_one_search("q", timeout=1)
        assert [r.username for r in out] == [r["username"] for r in MULTI["responses"]]
        assert fake.deleted == ["sid-1"]

    def test_search_without_an_id_is_not_polled(self, monkeypatch):
        fake = _FakeSearches()
        fake.search_text = lambda **kw: {}
        fake.state = lambda sid: pytest.fail("polled a search that has no id")
        monkeypatch.setattr(slskd_client, "get_client", lambda: _client_with(fake))
        assert slskd_search._run_one_search("q", timeout=1) == []

    @pytest.mark.parametrize("op", ["create", "poll", "responses"])
    def test_cycle_survives_and_records_one_no_candidate_failure(self, monkeypatch, conn, op):
        fake = _FakeSearches(op, ERRORS["500"])
        monkeypatch.setattr(slskd_client, "get_client", lambda: _client_with(fake))
        ok = acquire.handle_track_download(
            Track("t1", "Lose Yourself", "Eminem", "Alb", "pid"), "pl_x", conn
        )
        assert ok is False
        assert db.get_track(conn, "pl_x", "t1")["attempts"] == 1
        assert db.list_blocklist(conn) == []
        assert db.get_tried_files(conn, "pl_x", "t1") == []
        events = db.list_history(conn)
        assert [(e["event"], e["data"]["reason"]) for e in events] == [
            ("download_failed", "no acceptable candidate (0 results)"),
        ]
        # Every query of the waterfall that got an id was cleaned up.
        assert set(fake.deleted) <= {"sid-1"}
        if op != "create":
            assert fake.deleted


# ---- failures: enqueue and transfer polling ---------------------------------

_CANDIDATE = {"user": "peer", "filename": "@@a\\Eminem - Lose Yourself.flac", "size": 1}


class _FakeTransfers:
    def __init__(self, enqueue_error=None, poll_error=None, state=None):
        self.enqueue_error, self.poll_error, self.state = enqueue_error, poll_error, state
        self.enqueued = []

    def enqueue(self, username, files):
        if self.enqueue_error:
            raise self.enqueue_error()
        self.enqueued.append((username, files))
        return True

    def get_downloads(self, username):
        if self.poll_error:
            raise self.poll_error()
        return {"username": username, "directories": [{"directory": "@@a", "files": [{
            "id": "tid", "filename": _CANDIDATE["filename"], "size": 1, "state": self.state,
        }]}]}


def _outcome_of_grab(conn, monkeypatch, fake, clock=None):
    monkeypatch.setattr(slskd_client, "get_client", lambda: _client_with(transfers=fake))
    if clock is not None:
        # Pending states poll until the 300 s wait gives up; advance a fake
        # clock instead of sleeping.
        monkeypatch.setattr(slskd_transfers.time, "time", lambda: clock[0])

        def _sleep(seconds):
            clock[0] += seconds
            return False

        monkeypatch.setattr(slskd_client, "interruptible_sleep", _sleep)
    path = slskd_transfers.grab_candidate(dict(_CANDIDATE), conn, "pl_x", "t1")
    return (
        path,
        [(b["username"], b["filename"]) for b in db.list_blocklist(conn)],
        db.get_tried_files(conn, "pl_x", "t1"),
        [(e["event"], e["data"].get("reason")) for e in db.list_history(conn)],
    )


class TestEnqueueAndPollingHttpErrors:
    """A raised API error is transient: nothing permanent (no _tried entry,
    no blocklist, no history) is recorded; the track's own attempt counter
    bounds the retries."""

    @pytest.mark.parametrize("error", ERRORS.values(), ids=ERRORS.keys())
    def test_enqueue_error(self, conn, monkeypatch, error):
        path, blocked, tried, events = _outcome_of_grab(
            conn, monkeypatch, _FakeTransfers(enqueue_error=error)
        )
        assert (path, blocked, tried, events) == (None, [], [], [])

    @pytest.mark.parametrize("error", ERRORS.values(), ids=ERRORS.keys())
    def test_transfer_polling_error(self, conn, monkeypatch, error):
        fake = _FakeTransfers(poll_error=error)
        path, blocked, tried, events = _outcome_of_grab(conn, monkeypatch, fake)
        assert fake.enqueued, "the enqueue itself succeeded"
        assert (path, blocked, tried, events) == (None, [], [], [])


class TestTransferStatesThroughGrab:
    """Every production state, end to end through grab_candidate."""

    def test_succeeded_imports_and_records_the_grab(self, conn, monkeypatch):
        monkeypatch.setattr(slskd_transfers, "_await_completed_file", lambda n: "/dl/" + n)
        monkeypatch.setattr(slskd_transfers, "_wait_for_external_processing", lambda p: True)
        path, blocked, tried, events = _outcome_of_grab(
            conn, monkeypatch, _FakeTransfers(state="Completed, Succeeded")
        )
        assert path == "/dl/Eminem - Lose Yourself.flac"
        assert (blocked, tried, events) == ([], [], [("grabbed", None)])

    @pytest.mark.parametrize("state", ["Completed, Errored", "Completed, Rejected", "Completed, Aborted"])
    def test_peer_fault_states_blocklist_the_file(self, conn, monkeypatch, state):
        path, blocked, tried, events = _outcome_of_grab(
            conn, monkeypatch, _FakeTransfers(state=state)
        )
        assert path is None
        assert blocked == [("peer", _CANDIDATE["filename"])]
        assert tried == ["Eminem - Lose Yourself.flac"]
        assert events == [("download_failed", f"transfer {state.lower()}")]

    @pytest.mark.parametrize("state", ["Completed, TimedOut", "Completed, Cancelled"])
    def test_ambiguous_failures_only_go_to_tried(self, conn, monkeypatch, state):
        path, blocked, tried, events = _outcome_of_grab(
            conn, monkeypatch, _FakeTransfers(state=state)
        )
        assert (path, blocked) == (None, [])
        assert tried == ["Eminem - Lose Yourself.flac"]
        assert events == [("download_failed", f"transfer {state.lower()}")]

    @pytest.mark.parametrize(
        "state", ["Queued, Remotely", "Queued, Locally", "Requested", "Initializing", "InProgress"]
    )
    def test_pending_states_time_out_without_blocklisting(self, conn, monkeypatch, state):
        path, blocked, tried, events = _outcome_of_grab(
            conn, monkeypatch, _FakeTransfers(state=state), clock=[0.0]
        )
        assert (path, blocked) == (None, [])
        assert tried == ["Eminem - Lose Yourself.flac"]
        assert events == [("download_failed", "transfer timed out")]

    def test_transfer_never_listed_is_a_soft_failure(self, conn, monkeypatch):
        fake = _FakeTransfers()
        fake.get_downloads = lambda username: {"username": username, "directories": []}
        path, blocked, tried, events = _outcome_of_grab(conn, monkeypatch, fake, clock=[0.0])
        assert (path, blocked, tried) == (None, [], ["Eminem - Lose Yourself.flac"])
        assert events == [("download_failed", "transfer not found in slskd")]
