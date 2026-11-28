"""Guest login: a second password that can only use the listed tabs (enforced by the server)."""
import http.client
import json
import re
import threading

import pytest

from nakama import ui


@pytest.fixture(autouse=True)
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "SESSION_FILE", tmp_path / "ui-sessions.json")
    monkeypatch.setattr(ui.time, "sleep", lambda s: None)  # the wrong-password delay


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("NAKAMA_UI_PASSWORD", "owner-pw")
    monkeypatch.setenv("NAKAMA_UI_GUEST_PASSWORD", "guest-pw")
    monkeypatch.delenv("NAKAMA_UI_GUEST_TABS", raising=False)
    srv = ui.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _raw(srv, method, path, headers=None, data=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    conn.request(method, path, body=data, headers={"Host": "localhost", **(headers or {})})
    res = conn.getresponse()
    return res.status, dict(res.getheaders()), res.read().decode("utf-8", "replace")


def _login(srv, pw, user="x"):
    """The session cookie the login form hands out, or None when the password is wrong."""
    code, headers, _ = _raw(srv, "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"},
                            f"username={user}&password={pw}".encode())
    assert code == 303
    if headers.get("Location") != "/":
        return None
    return headers["Set-Cookie"].split(";", 1)[0]


def _req(srv, method, path, pw=None, token=None, body=None, cookie=None):
    headers = {"Host": "localhost"}
    cookie = cookie or (_login(srv, pw) if pw else None)
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["X-Nakama-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    code, _, text = _raw(srv, method, path, headers, data)
    return code, text


def _token(page: str) -> str:
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def test_login_page_and_redirects(server):
    code, headers, _ = _raw(server, "GET", "/")
    assert code == 303 and headers["Location"] == "/login"
    code, _, page = _raw(server, "GET", "/login")
    assert code == 200 and 'action="/login"' in page and "パスワードが違います" not in page
    assert _login(server, "wrong") is None
    assert "パスワードが違います" in _raw(server, "GET", "/login?e=1")[2]
    assert _req(server, "GET", "/api/overview", token="x")[0] == 401  # API: no redirect, plain 401
    cookie = _login(server, "owner-pw")
    assert "HttpOnly" in _raw(server, "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"},
                              b"password=owner-pw")[1]["Set-Cookie"]
    assert _raw(server, "GET", "/login", {"Cookie": cookie})[1].get("Location") == "/"  # already logged in


def test_logout_and_password_change_end_sessions(server, monkeypatch):
    cookie = _login(server, "owner-pw")
    assert _req(server, "GET", "/", cookie=cookie)[0] == 200
    code, headers, _ = _raw(server, "POST", "/logout", {"Cookie": cookie})
    assert code == 303 and headers["Location"] == "/login" and "Max-Age=0" in headers["Set-Cookie"]
    assert _req(server, "GET", "/", cookie=cookie)[0] == 303  # the old cookie no longer works
    cookie = _login(server, "guest-pw")
    assert _req(server, "GET", "/", cookie=cookie)[0] == 200
    monkeypatch.setattr(server.RequestHandlerClass, "guest_password", "new-guest-pw")  # password changed in .env
    assert _req(server, "GET", "/", cookie=cookie)[0] == 303


def test_page_tells_each_login_its_tabs(server):
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


def test_me_and_guest_can_approve_credit(server, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nakama import credit

    monkeypatch.setattr(credit, "DIR", tmp_path / "credit")
    fleet = SimpleNamespace(credit={"enabled": True}, ui_users={"owner": {"name": "A"}, "guest": {"color": "green"}},
                            owner={"name": "Your Name"}, timezone="Asia/Tokyo")
    monkeypatch.setattr(credit, "load_fleet", lambda: fleet)
    monkeypatch.setattr(ui, "load_fleet", lambda: fleet)
    (tmp_path / "credit" / "statements").mkdir(parents=True)
    (tmp_path / "credit" / "statements" / "202611.json").write_text(json.dumps(
        {"month": "202611", "billed": None, "balance": None, "closed": False, "rows": []}), encoding="utf-8")
    assert ui.ui_user("owner") == {"role": "owner", "name": "A", "color": "blue"}  # from ui_users (nobody logged in yet)
    assert ui.ui_user("guest")["name"] == "ゲスト"
    cookie = _login(server, "guest-pw", user="yuka")  # the name typed at login is what is shown
    token = _token(_req(server, "GET", "/", cookie=cookie)[1])
    me = json.loads(_req(server, "GET", "/api/overview", token=token, cookie=cookie)[1])["me"]
    assert me == {"role": "guest", "name": "yuka", "color": "green"}
    code, body = _req(server, "POST", "/api/credit/approve", token=token, cookie=cookie, body={"month": "202611"})
    data = json.loads(body)
    assert code == 200 and data["approvals"]["guest"]["name"] == "yuka"
    assert [u["name"] for u in data["approvers"]] == ["A", "yuka"] and "承認待ち: A" in data["problems"]


def test_guest_sees_only_family_requests(server, tmp_path, monkeypatch):
    from nakama import devreq

    monkeypatch.setenv("NAKAMA_UI_GUEST_TABS", "credit,devreq")
    srv = ui.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(devreq, "FILE", tmp_path / "requests.json")
        private = devreq.add("仕事の話", note="memo")
        shared = devreq.add("家族向け", family=True)
        cookie = _login(srv, "guest-pw", user="yuka")
        token = _token(_req(srv, "GET", "/", cookie=cookie)[1])
        data = json.loads(_req(srv, "GET", "/api/devreq", token=token, cookie=cookie)[1])
        assert data["guest"] and [r["id"] for r in data["requests"]] == [shared["id"]]
        code, body = _req(srv, "POST", "/api/devreq/add", token=token, cookie=cookie, body={"text": "献立を出して", "note": "x"})
        row = json.loads(body)
        assert code == 200 and (row["by"], row["by_name"], row["family"], row["note"]) == ("guest", "yuka", True, "")
        assert _req(srv, "POST", "/api/devreq/update", token=token, cookie=cookie,
                    body={"id": private["id"], "family": True})[0] == 403
        assert not devreq.load()[0]["family"]
        devreq.update(private["id"], family=True)  # the owner can share one later
        assert len(devreq.for_family()) == 3 and "note" not in devreq.for_family()[0]
    finally:
        srv.shutdown()
