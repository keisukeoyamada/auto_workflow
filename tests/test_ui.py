"""Offline tests for the dashboard server (no API calls)."""
import http.client
import json
import threading

import pytest

from nakama import ui
from nakama.config import WORKSPACE_DIR


def test_safe_path_blocks_secrets_and_traversal():
    for bad in (".env", "config/fleet.yaml", "workspace/../.env", "agents/henji/AGENT.md"):
        with pytest.raises(PermissionError):
            ui._safe_path(bad)
    assert ui._safe_path("knowledge/rulebook.md").name == "rulebook.md"


def test_set_status_only_on_allowed_folders(tmp_path, monkeypatch):
    folder = WORKSPACE_DIR / "action_items"
    folder.mkdir(parents=True, exist_ok=True)
    item = folder / "zz-test-item.md"
    item.write_text("---\nstatus: open\nsource: test\n---\nbody\n", encoding="utf-8")
    try:
        ui.set_status("workspace/action_items/zz-test-item.md", "done")
        assert "status: done" in item.read_text(encoding="utf-8")
        with pytest.raises(PermissionError):
            ui.set_status("workspace/action_items/zz-test-item.md", "hacked")
    finally:
        item.unlink(missing_ok=True)
    with pytest.raises(PermissionError):
        ui.set_status("knowledge/rulebook.md", "done")


@pytest.fixture()
def server():
    srv = ui.make_server("127.0.0.1", 0)  # port 0 = pick a free port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _get(srv, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path, headers={"Host": "localhost", **(headers or {})})
    res = conn.getresponse()
    return res.status, res.read()


def test_api_requires_token_and_local_host(server):
    token = server.RequestHandlerClass.token
    assert _get(server, "/api/overview")[0] == 403                                   # no token
    assert _get(server, "/api/overview", {"Host": "evil.example", "X-Nakama-Token": token})[0] == 403
    status, body = _get(server, "/api/overview", {"X-Nakama-Token": token})
    assert status == 200 and "agents" in json.loads(body)
    status, body = _get(server, "/")
    assert status == 200 and token.encode() in body                                   # page carries the token
    assert _get(server, "/api/file?path=.env", {"X-Nakama-Token": token})[0] == 403
