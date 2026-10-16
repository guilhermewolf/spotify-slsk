"""/queue: slskd transfers joined to tracks, cancel / cancel + blocklist,
and the UI's pending requests. slskd is a fake (new_client is patched)."""
import pytest

from spotify_slsk import db, runtime_state
from spotify_slsk.slskd import client as slskd_client
from spotify_slsk.slskd.models import Transfer
from tests.test_webui import app_tmp, client, post  # noqa: F401  (fixtures)

FILE = "Music\\Artist\\01 - Song.flac"


def _downloads(state="InProgress"):
    return [{
        "username": "peer1",
        "directories": [{"directory": "Music\\Artist", "files": [{
            "id": "tx1", "filename": FILE, "size": 40_000_000, "state": state,
            "bytesTransferred": 10_000_000, "averageSpeed": 1_000_000.0, "percentComplete": 25.0,
        }]}],
    }]


class FakeTransfers:
    def __init__(self, state="InProgress"):
        self.state = state
        self.cancelled = []

    def get_all_downloads(self, includeRemoved=False):
        return _downloads(self.state)

    def get_download(self, username, id):
        return _downloads(self.state)[0]["directories"][0]["files"][0]

    def cancel_download(self, username, id, remove=False):
        self.cancelled.append((username, id, remove))
        return True


class FakeClient:
    def __init__(self, transfers):
        self.transfers = transfers


@pytest.fixture
def fake(monkeypatch):
    transfers = FakeTransfers()
    timeouts = []
    monkeypatch.setattr(slskd_client, "new_client",
                        lambda timeout=None: timeouts.append(timeout) or FakeClient(transfers))
    transfers.timeouts = timeouts
    return transfers


@pytest.fixture
def seeded(app_tmp):  # noqa: F811
    conn = db.create_connection(app_tmp.config["DB_PATH"])
    db.upsert_playlist_meta(conn, "pid1", "pl_x", "Mix", "s")
    db.create_table(conn, "pl_x")
    db.insert_track(conn, "pl_x", ("t1", "Song", "Artist", "Album"))
    conn.close()
    return app_tmp


def test_model_flattens_all_users_and_computes_eta():
    (t,) = Transfer.list_from_all_json(_downloads())
    assert t.username == "peer1" and t.id == "tx1"
    assert t.eta_seconds == 30.0
    (done,) = Transfer.list_from_all_json(_downloads("Completed, Succeeded"))
    assert done.eta_seconds is None
    assert Transfer.list_from_all_json({"not": "a list"}) == []


def test_transfer_is_joined_to_the_daemons_current_track(seeded, client, fake):  # noqa: F811
    runtime_state.set_current_transfer(
        {"username": "peer1", "filename": FILE, "table_name": "pl_x", "track_id": "t1"}
    )
    try:
        r = client.get("/queue")
    finally:
        runtime_state.set_current_transfer(None)
    html = r.data.decode()
    assert r.status_code == 200
    assert "01 - Song.flac" in html and "Song — Artist" in html and "Mix" in html
    assert "25%" in html and "hx-trigger=\"every 3s\"" in html
    assert 'name="csrf_token"' in html and "onclick" not in html and "<script>" not in html
    assert fake.timeouts[0] == 5.0  # a short timeout: the page polls


def test_history_joins_a_transfer_too(seeded, client, fake):  # noqa: F811
    conn = db.create_connection(seeded.config["DB_PATH"])
    db.record_history(conn, "download_failed", table_name="pl_x", track_id="t1",
                      username="peer1", filename=FILE)
    conn.close()
    assert "Song — Artist" in client.get("/queue").data.decode()


def test_slskd_down_still_renders(client, monkeypatch):  # noqa: F811
    def boom(timeout=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(slskd_client, "new_client", boom)
    r = client.get("/queue")
    assert r.status_code == 200 and b"slskd unavailable (ConnectionError)" in r.data


def test_cancel_and_blocklist(seeded, client, fake):  # noqa: F811
    assert client.post("/queue/cancel", data={"username": "peer1", "id": "tx1"}).status_code == 400
    r = post(client, "/queue/cancel", {"username": "peer1", "id": "tx1"}, follow_redirects=True)
    assert b"Cancelled 01 - Song.flac" in r.data
    assert fake.cancelled == [("peer1", "tx1", False)]
    conn = db.create_connection(seeded.config["DB_PATH"])
    assert db.list_blocklist(conn) == []
    conn.close()

    post(client, "/queue/cancel", {"username": "peer1", "id": "tx1", "blocklist": "1"})
    conn = db.create_connection(seeded.config["DB_PATH"])
    (entry,) = db.list_blocklist(conn)
    conn.close()
    assert (entry["username"], entry["filename"], entry["size"]) == ("peer1", FILE, 40_000_000)


def test_pending_requests_are_listed_without_being_consumed(seeded, client, fake):  # noqa: F811
    runtime_state.pop_quarantines()
    runtime_state.enqueue_quarantine("pl_x", "t1", "/p/x.flac")
    try:
        html = client.get("/queue").data.decode()
        assert "Quarantines" in html and "/p/x.flac" in html
        assert runtime_state.peek_quarantines() == [("pl_x", "t1", "/p/x.flac")]
    finally:
        runtime_state.pop_quarantines()


def test_grab_publishes_the_current_transfer_only_while_it_runs(monkeypatch, tmp_path):
    from spotify_slsk.slskd import transfers

    seen = []

    class Boom:
        class transfers:  # noqa: N801
            @staticmethod
            def enqueue(**kwargs):
                seen.append(runtime_state.get_current_transfer())
                raise ConnectionError("down")

    monkeypatch.setattr(slskd_client, "get_client", lambda: Boom)
    conn = db.create_connection(str(tmp_path / "t.db"))
    cand = {"user": "peer1", "filename": FILE, "size": 1}
    assert transfers.grab_candidate(cand, conn, "pl_x", "t1") is None
    conn.close()
    assert seen == [{"username": "peer1", "filename": FILE, "table_name": "pl_x", "track_id": "t1"}]
    assert runtime_state.get_current_transfer() is None
