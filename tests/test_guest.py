"""Guest login: a second password that can only use the listed tabs (enforced by the server)."""
import base64
import http.client
import json
import re
import threading

import pytest

from nakama import ui


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("NAKAMA_UI_PASSWORD", "owner-pw")
    monkeypatch.setenv("NAKAMA_UI_GUEST_PASSWORD", "guest-pw")
    monkeypatch.delenv("NAKAMA_UI_GUEST_TABS", raising=False)
    srv = ui.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _req(srv, method, path, pw=None, token=None, body=None):
    headers = {"Host": "localhost"}
    if pw:
        headers["Authorization"] = "Basic " + base64.b64encode(f"anyone:{pw}".encode()).decode()
    if token:
        headers["X-Nakama-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    return res.status, res.read().decode("utf-8", "replace")


def _token(page: str) -> str:
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def test_page_tells_each_login_its_tabs(server):
    assert _req(server, "GET", "/")[0] == 401
    assert _req(server, "GET", "/", pw="wrong")[0] == 401
    code, owner_page = _req(server, "GET", "/", pw="owner-pw")
    assert code == 200 and 'const GUEST_TABS = "".split' in owner_page
    code, guest_page = _req(server, "GET", "/", pw="guest-pw")
    assert code == 200 and 'const GUEST_TABS = "credit".split' in guest_page


def test_guest_api_is_limited_to_its_tabs(server):
    token = _token(_req(server, "GET", "/", pw="guest-pw")[1])
    code, body = _req(server, "GET", "/api/overview", pw="guest-pw", token=token)
    data = json.loads(body)
    assert code == 200 and data["guest"] and data["features"] == {"credit": True} and data["agents"] == []
    assert _req(server, "GET", "/api/credit/months", pw="guest-pw", token=token)[0] == 200
    for path in ("/api/settings", "/api/log", "/api/rongo", "/api/tasks", "/api/file?path=knowledge/rulebook.md"):
        assert _req(server, "GET", path, pw="guest-pw", token=token)[0] == 403, path
    assert _req(server, "POST", "/api/run", pw="guest-pw", token=token, body={"agent": "henji"})[0] == 403
    assert _req(server, "POST", "/api/settings/save", pw="guest-pw", token=token, body={})[0] == 403


def test_owner_keeps_full_access(server):
    token = _token(_req(server, "GET", "/", pw="owner-pw")[1])
    code, body = _req(server, "GET", "/api/overview", pw="owner-pw", token=token)
    assert code == 200 and not json.loads(body).get("guest")
    assert _req(server, "GET", "/api/log", pw="owner-pw", token=token)[0] == 200


def test_guest_tabs_from_env(monkeypatch):
    monkeypatch.setenv("NAKAMA_UI_PASSWORD", "o")
    monkeypatch.setenv("NAKAMA_UI_GUEST_PASSWORD", "g")
    monkeypatch.setenv("NAKAMA_UI_GUEST_TABS", "credit, rongo, settings")  # unknown tabs are dropped
    srv = ui.make_server("127.0.0.1", 0)
    try:
        assert srv.RequestHandlerClass.guest_tabs == ("credit", "rongo")
    finally:
        srv.server_close()
