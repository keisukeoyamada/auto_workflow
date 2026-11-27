"""Obsidian vault in the dashboard, without changing its folder structure.

Settings live in config/vault.json (git-ignored):
  {"root": "C:/path/to/vault",
   "folders": {"<relative folder>": "show" | "hide" | "lock"},   # default: show
   "claude":  {"<relative folder>": true | false},               # folders the chat may read (default: no)
   "password": {"salt": "...", "hash": "..."}}                   # PBKDF2 for locked folders

Rules (all paths are normalised first: no "..", "\\" or case tricks):
- "lock" applies to the folder and EVERYTHING below it, whatever the children say. Locked content opens
  only after the password in this server session, and is never readable by Claude (chat / agents).
- "hide": the folder (and below) is not listed and cannot be read.
- Otherwise the most specific "folders" / "claude" setting wins.
- Removing or changing a lock, or moving the root, requires an unlocked session when a lock exists.
- Edits are written in place to existing text files only, keeping encoding (BOM) and line endings,
  with a backup in logs/vault-backup/ and an mtime check against Obsidian.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import CONFIG_DIR, LOGS_DIR

FILE = CONFIG_DIR / "vault.json"
BACKUP_DIR = LOGS_DIR / "vault-backup"
UNLOCK_SECONDS = 30 * 60
TEXT_EXT = {".md", ".txt", ".csv", ".json", ".yaml", ".yml"}
SKIP_DIRS = {".obsidian", ".trash", ".git"}
_unlocked: dict[str, float] = {}  # token -> expiry (in memory: a restart locks everything again)
_unlock_lock = threading.Lock()   # one password attempt at a time (keeps the 1 s penalty meaningful)


class VaultError(Exception):
    pass


def settings() -> dict[str, Any]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("folders", {})
    data.setdefault("claude", {})
    return data


def _save(data: dict[str, Any]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def root() -> Path | None:
    raw = settings().get("root")
    p = Path(raw).expanduser() if raw else None
    return p.resolve() if p and p.is_dir() else None


def _key(rel: str) -> str:
    """Canonical key for settings lookups (Windows paths are case-insensitive)."""
    return rel.strip("/").casefold()


def _rel(path: Path) -> str:
    r = root()
    return "" if path == r else path.relative_to(r).as_posix()


def normalise(rel: str) -> str:
    """Resolve a client-supplied relative path to the canonical in-vault form, refusing tricks."""
    rel = (rel or "").strip()
    if "\\" in rel or any(part == ".." for part in PurePosixPath(rel).parts):
        raise VaultError("不正なパスです")
    return _rel(resolve(rel))


def resolve(rel: str) -> Path:
    r = root()
    if r is None:
        raise VaultError("vault の root が未設定です（設定 > Vault）")
    p = (r / rel).resolve() if rel else r
    if p != r and r not in p.parents:
        raise VaultError("vault の外です")
    return p


def _prefixes(rel: str) -> list[str]:
    parts = rel.split("/") if rel else []
    return ["/".join(parts[:i]) for i in range(len(parts) + 1)]


def mode(rel: str) -> str:
    """lock wins from any ancestor; otherwise the most specific setting (default show)."""
    table = {_key(k): v for k, v in settings()["folders"].items()}
    prefixes = [_key(p) for p in _prefixes(rel)]
    if any(table.get(p) == "lock" for p in prefixes):
        return "lock"
    if any(table.get(p) == "hide" for p in prefixes):
        return "hide"
    return "show"


def claude_setting(rel: str) -> bool | None:
    """Most specific explicit claude flag on the path (None = nothing set)."""
    table = {_key(k): v for k, v in settings()["claude"].items()}
    for p in reversed(_prefixes(rel)):
        if _key(p) in table:
            return bool(table[_key(p)])
    return None


def claude_allowed(rel: str) -> bool:
    return mode(rel) == "show" and claude_setting(rel) is True


# ---------- password / unlock ----------

def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def set_password(new: str, current: str = "") -> None:
    data = settings()
    if data.get("password") and not check_password(current):
        raise VaultError("今のパスワードが違います")
    if len(new) < 8:
        raise VaultError("8 文字以上にしてください")
    salt = secrets.token_hex(16)
    data["password"] = {"salt": salt, "hash": _hash(new, salt)}
    _save(data)


def check_password(password: str) -> bool:
    pw = settings().get("password") or {}
    return bool(pw) and hmac.compare_digest(_hash(password, pw["salt"]), pw["hash"])


def unlock(password: str) -> str:
    if not settings().get("password"):
        raise VaultError("先にパスワードを設定してください")
    with _unlock_lock:  # serialise attempts so parallel requests can't skip the delay
        if not check_password(password):
            time.sleep(1)
            raise VaultError("パスワードが違います")
    token = secrets.token_urlsafe(24)
    _unlocked[token] = time.time() + UNLOCK_SECONDS
    return token


def is_unlocked(token: str) -> bool:
    exp = _unlocked.get(token or "")
    return bool(exp and exp > time.time())


# ---------- browse / read / write ----------

def _visible_entry(p: Path) -> bool:
    return not (p.name in SKIP_DIRS or p.name.startswith("."))


def listing(rel: str, token: str = "") -> dict[str, Any]:
    rel = normalise(rel)
    folder = resolve(rel)
    if not folder.is_dir():
        raise VaultError("フォルダではありません")
    m = mode(rel)
    if m == "hide":
        raise VaultError("非表示のフォルダです")
    if m == "lock" and not is_unlocked(token):
        return {"rel": rel, "locked": True, "mode": m, "entries": []}
    entries = []
    for p in sorted(folder.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if not _visible_entry(p):
            continue
        child = _rel(p)
        cm = mode(child)
        if cm == "hide":
            continue
        if p.is_dir() or p.suffix.lower() in TEXT_EXT:
            entries.append({"name": p.name, "rel": child, "dir": p.is_dir(), "mode": cm,
                            "claude": claude_allowed(child), "mtime": p.stat().st_mtime})
    return {"rel": rel, "locked": False, "mode": m, "claude": claude_allowed(rel), "entries": entries}


def _check_file(rel: str, token: str) -> tuple[str, Path]:
    rel = normalise(rel)
    p = resolve(rel)
    if any(not _visible_entry(Path(part)) for part in PurePosixPath(rel).parts):
        raise VaultError("設定用のフォルダは開けません")
    m = mode(rel)
    if m == "hide":
        raise VaultError("非表示です")
    if m == "lock" and not is_unlocked(token):
        raise VaultError("🔒 パスワードで開いてください")
    if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
        raise VaultError("既存のテキストファイルだけ開けます")
    return rel, p


def read(rel: str, token: str = "") -> dict[str, Any]:
    rel, p = _check_file(rel, token)
    if p.stat().st_size > 2_000_000:
        raise VaultError("大きすぎます")
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        editable = True
    except UnicodeDecodeError:  # e.g. Shift_JIS: show it, but never save it back (would corrupt it)
        text = raw.decode("cp932", errors="replace")
        editable = False
    return {"rel": rel, "text": text.replace("\r\n", "\n"), "mtime": p.stat().st_mtime, "locked": mode(rel) == "lock",
            "editable": editable}


def _backup(p: Path, rel: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(rel.casefold().encode("utf-8")).hexdigest()[:8]  # same name in other folders must not collide
    shutil.copy2(p, BACKUP_DIR / f"{p.stem}.{tag}.{datetime.now():%Y%m%d-%H%M%S-%f}{p.suffix}")


def write(rel: str, text: str, expected_mtime: float, token: str = "") -> dict[str, Any]:
    rel, p = _check_file(rel, token)
    if abs(p.stat().st_mtime - float(expected_mtime)) > 0.001:
        raise VaultError("Obsidian など別の場所で更新されています。再読み込みしてから編集してください")
    raw = p.read_bytes()
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VaultError("UTF-8 以外のファイルは保存できません（文字化けを防ぐため）") from exc
    bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = b"\r\n" in raw
    body = text.replace("\r\n", "\n")
    if crlf:
        body = body.replace("\n", "\r\n")
    _backup(p, rel)
    tmp = p.with_name(p.name + ".nakama.tmp")
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8"))
    os.replace(tmp, p)
    return {"rel": rel, "mtime": p.stat().st_mtime}


def configure(root_path: str | None = None, folder: str | None = None, folder_mode: str | None = None,
              claude: bool | None = None, token: str = "") -> dict[str, Any]:
    data = settings()
    has_lock = any(v == "lock" for v in data["folders"].values())
    if root_path is not None:
        if has_lock and not is_unlocked(token):
            raise VaultError("🔒 があるため、場所の変更にはパスワードで開いた状態が必要です")
        rp = Path(root_path.strip().strip('"')).expanduser()
        if not rp.is_dir():
            raise VaultError(f"フォルダが見つかりません: {root_path}")
        data["root"] = rp.resolve().as_posix()
    if folder is not None:
        folder = normalise(folder)
        if mode(folder) == "lock" and not is_unlocked(token):
            raise VaultError("🔒 のフォルダの設定を変えるには、先にパスワードで開いてください")
        if folder_mode is not None:
            if folder_mode not in ("show", "hide", "lock"):
                raise VaultError("mode は show / hide / lock")
            if folder_mode == "lock" and not data.get("password"):
                raise VaultError("🔒 にする前にパスワードを設定してください")
            data["folders"] = {k: v for k, v in data["folders"].items() if _key(k) != _key(folder)}
            data["folders"][folder] = folder_mode
        if claude is not None:
            data["claude"] = {k: v for k, v in data["claude"].items() if _key(k) != _key(folder)}
            data["claude"][folder] = bool(claude)
    _save(data)
    return {k: v for k, v in data.items() if k != "password"} | {"has_password": bool(data.get("password"))}


def claude_roots() -> tuple[list[Path], list[Path]]:
    """(folders the chat may read, folders it must never read) — the chat guard re-checks every path."""
    r = root()
    if r is None:
        return [], []
    data = settings()
    allow = []
    for k, v in data["claude"].items():
        try:
            if v and claude_allowed(normalise(k)):
                allow.append(resolve(normalise(k)))
        except VaultError:
            continue
    deny = []
    for k, v in data["folders"].items():
        try:
            if v in ("lock", "hide"):
                deny.append(resolve(normalise(k)))
        except VaultError:
            continue
    return allow, deny


def chat_may_read(path: Path) -> bool | None:
    """For a path inside the vault: True/False by the vault rules; None if the path is not in the vault."""
    r = root()
    if r is None or not (path == r or r in path.parents):
        return None
    rel = _rel(path)
    if any(not _visible_entry(Path(part)) for part in PurePosixPath(rel).parts):
        return False
    if mode(rel) != "show":
        return False
    return claude_setting(rel) is not False  # explicit "no" wins; otherwise the project/claude roots decide
