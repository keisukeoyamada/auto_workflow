"""devbox (implements 着手OK requests) and its dashboard side, offline: a local bare repo stands in for GitHub."""
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from nakama import devbox, devimpl, devreq


def test_checks():
    assert devbox.check_paths([".github/workflows/ci.yml", "nakama/ui.py", "Dockerfile", "scripts/nas-autodeploy.sh"]) == (
        [".github/workflows/ci.yml", "scripts/nas-autodeploy.sh"], ["Dockerfile"])
    assert devbox.check_words(["def f():\n  return 'Sumitomo'"], ["sumitomo", "taro"]) == ["sumitomo"]
    assert devbox.parse_review('ok\n{"ok": true, "problems": [], "pr_title": "T", "change": "c", "usage": "u"}')["ok"]
    assert not devbox.parse_review("no json")["ok"]
    env = devbox._auth_env("tok")
    assert "tok" not in json.dumps(env) and env["GIT_CONFIG_KEY_0"].endswith("extraheader")  # base64, never plain


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """A tiny 'repository' with a passing test, served from a local bare repo instead of github.com."""
    seed = tmp_path / "seed"
    (seed / "tests").mkdir(parents=True)
    (seed / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (seed / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (seed / "conftest.py").write_text("import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n")
    g = lambda *a, cwd=seed: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    g("init", "-q", "-b", "main")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    g("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    monkeypatch.setattr(devbox, "WORK", tmp_path / "work")
    monkeypatch.setattr(devbox, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(devbox, "SECRETS", tmp_path / "secrets")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "forbidden.txt").write_text("# names\nHANAKO\n")
    real_git = devbox.git

    def fake_git(*args, **kw):  # clone from the local bare repo instead of github.com
        if args and args[0] == "clone":
            args = ("clone", "-q", str(bare), args[-1])
        return real_git(*args, **kw)

    monkeypatch.setattr(devbox, "git", fake_git)
    monkeypatch.setattr(devbox, "run", lambda cmd, cwd=None, as_agent=True, timeout=900, env=None: subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout))
    return tmp_path


def _job():
    return {"id": "q007", "text": "引き算もほしい", "repo_slug": "owner/repo", "rounds": 3,
            "triage": {"spec": "## やること\n- sub(a, b) を足す"}}


def _agent(edits):
    """A fake Claude: implementer rounds apply edits[n]; the reviewer approves."""
    calls = []

    async def agent(system_prompt, prompt, read_only):
        calls.append((read_only, prompt))
        if read_only:
            return '{"ok": true, "problems": [], "pr_title": "引き算を追加", "change": "sub を追加", "usage": "calc.sub"}'
        repo = devbox.WORK / "repo"
        edits[min(sum(1 for c in calls if not c[0]) - 1, len(edits) - 1)](repo)
        return "実装しました"
    return agent, calls


def test_process_implements_tests_and_publishes(box):
    def good(repo):
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
        (repo / "tests" / "test_sub.py").write_text("from calc import sub\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")

    def broken(repo):
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")  # breaks the existing test

    agent, calls = _agent([broken, good])
    published = {}
    res = asyncio.run(devbox.process(_job(), agent=agent, token="tok",
                                     publish=lambda: published.setdefault("pr", {"number": 42, "url": "https://x/pull/42"})))
    assert res["ok"] and res["pr"]["number"] == 42 and res["title"] == "引き算を追加"
    impl_prompts = [p for ro, p in calls if not ro]
    assert len(impl_prompts) == 2 and "テストが失敗しました" in impl_prompts[1]  # the test failure went back
    log = subprocess.run(["git", "log", "-1", "--format=%an %s"], cwd=box / "work" / "repo", capture_output=True, text=True).stdout
    assert log.startswith("devbox 要望 q007: 引き算を追加")


def test_process_refuses_protected_files_and_forbidden_words(box):
    def ci(repo):
        (repo / ".github").mkdir(exist_ok=True)
        (repo / ".github" / "ci.yml").write_text("skip: true\n")

    def secret(repo):
        (repo / "calc.py").write_text("OWNER = 'Hanako'\n\ndef add(a, b):\n    return a + b\n")

    agent, calls = _agent([ci, secret, secret])
    res = asyncio.run(devbox.process(_job(), agent=agent, token="tok", publish=lambda: pytest.fail("must not publish")))
    assert not res["ok"] and "通りませんでした" in res["error"]
    prompts = [p for ro, p in calls if not ro]
    assert "変更できません" in prompts[1] and "禁止語" in prompts[2]
    assert not (box / "work" / "repo" / ".github" / "ci.yml").exists()  # undone


def test_process_needs_token_and_repo(box):
    assert "トークン" in asyncio.run(devbox.process(_job(), token=""))["error"]
    assert "リポジトリ" in asyncio.run(devbox.process(_job() | {"repo_slug": ""}, token="t"))["error"]


def test_dashboard_side_queue_and_results(tmp_path, monkeypatch):
    monkeypatch.setattr(devreq, "FILE", tmp_path / "requests.json")
    monkeypatch.setattr(devimpl, "QUEUE", tmp_path / "devq")
    fleet = SimpleNamespace(devreq={"implement": {"enabled": True, "budget_usd": 2}})
    monkeypatch.setattr(devimpl, "load_fleet", lambda: fleet)
    monkeypatch.setattr(devimpl, "_repo_slug", lambda: "owner/repo")
    a, b = devreq.add("A"), devreq.add("B")
    devreq.update(a["id"], status="ready")
    assert devimpl.enqueue()["queued"] == [a["id"]] and devimpl.enqueue()["queued"] == []  # only once
    job = json.loads((tmp_path / "devq" / "jobs" / f"{a['id']}.json").read_text())
    assert job["repo_slug"] == "owner/repo" and job["budget_usd"] == 2 and devreq.load()[0]["status"] == "working"
    (tmp_path / "devq" / "results").mkdir()
    (tmp_path / "devq" / "results" / f"{a['id']}.json").write_text(json.dumps(
        {"id": a["id"], "ok": True, "pr": {"number": 42, "url": "https://github.com/o/r/pull/42"}, "change": "c", "usage": "u"}))
    (tmp_path / "devq" / "results" / f"{b['id']}.json").write_text(json.dumps({"id": b["id"], "ok": False, "error": "x"}))
    assert devimpl.ingest() == [a["id"], b["id"]]
    ra, rb = devreq.load()
    assert (ra["status"], ra["pr"], ra["change"]) == ("review", "https://github.com/o/r/pull/42", "c")
    assert rb["status"] == "consult" and "x" in rb["note"]
    assert devimpl.ingest() == [] and not devimpl.status()["alive"]
    devreq.update(b["id"], family=True)
    with pytest.raises(PermissionError):  # the family cannot move a request into the devbox flow
        devreq.update(b["id"], status="working", guest=True)
    fleet.devreq = {}
    with pytest.raises(PermissionError):
        devimpl.enqueue()
