"""htmx-driven actions answer with an HX-Trigger toast instead of a flash +
reload; classic form POSTs keep the redirect + flash."""
import json

from tests.test_queue import TX, FakeClient, FakeTransfers
from tests.test_webui import app_tmp, client, post  # noqa: F401  (fixtures)

HX = {"HX-Request": "true"}


def toasts(response):
    return json.loads(response.headers["HX-Trigger"])["toast"]["items"]


def test_htmx_run_now_swaps_the_table_and_toasts(client):  # noqa: F811
    r = post(client, "/system/tasks/housekeeping/run", headers=HX)
    assert r.status_code == 200
    assert toasts(r) == [{"message": "Housekeeping: queued.", "level": "success"}]
    assert b'id="tasks"' in r.data
    # Consumed by the toast, so it does not reappear as a flash later.
    assert b"Housekeeping: queued." not in client.get("/system/tasks").data
    r = post(client, "/system/tasks/housekeeping/run", headers=HX)
    assert toasts(r)[0]["message"] == "Housekeeping is already queued."


def test_classic_post_still_flashes(client):  # noqa: F811
    r = post(client, "/system/tasks/housekeeping/run")
    assert r.status_code == 302 and "HX-Trigger" not in r.headers
    assert b"Housekeeping: queued." in client.get("/system/tasks").data


def test_htmx_backup_now_returns_the_status_page(client):  # noqa: F811
    r = post(client, "/system/tasks/db_backup/run", {"next": "status"}, headers=HX)
    assert r.status_code == 200 and b'id="backups"' in r.data
    assert toasts(r)[0]["message"] == "Database backup: queued."


def test_htmx_queue_cancel_toasts_errors_too(client, monkeypatch):  # noqa: F811
    from spotify_slsk.slskd import client as slskd_client

    transfers = FakeTransfers()
    transfers.cancel_download = lambda *a, **k: False
    monkeypatch.setattr(slskd_client, "new_client", lambda timeout=None: FakeClient(transfers))
    r = post(client, "/queue/cancel", {"username": "peer1", "id": TX}, headers=HX)
    assert r.status_code == 200 and b'id="queue-live"' in r.data
    assert toasts(r)[0]["level"] == "error"


def test_forms_are_htmx_with_a_classic_fallback_and_no_inline_js(client):  # noqa: F811
    html = client.get("/system/tasks").data
    assert b'hx-post="/system/tasks/housekeeping/run"' in html
    assert b'method="POST" action="/system/tasks/housekeeping/run"' in html
    assert b"toast.js" in html and b"<script>" not in html and b"onclick" not in html
    assert client.get("/static/toast.js").status_code == 200
