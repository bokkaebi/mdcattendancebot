"""Per-user Singpass credential store for the Telegram bot.

``users.json`` maps Telegram user ids to Singpass credentials. The file is
gitignored and tightened to 0600 on load. Reloaded on each ``/attend`` so
edits take effect without a restart.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .attendance import DEPARTMENT


@dataclass(frozen=True)
class User:
    telegram_id: int
    singpass_id: str
    singpass_password: str
    department: str


def load_users(path: str) -> dict[int, User]:
    """Load and validate ``users.json``. Missing file -> {} (no users)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    users: dict[int, User] = {}
    for key, rec in raw.items():
        tid = int(key)
        sid = str(rec.get("singpass_id", "")).strip()
        spw = str(rec.get("singpass_password", "")).strip()
        if not sid or not spw:
            raise ValueError(f"{path}: user {tid} missing singpass_id/singpass_password")
        dept = str(rec.get("department", DEPARTMENT)).strip() or DEPARTMENT
        users[tid] = User(tid, sid, spw, dept)
    _ensure_perms(path)
    return users


def _ensure_perms(path: str) -> None:
    """Tighten the credential file to 0600 if it is more open."""
    try:
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


def save_user(
    path: str,
    uid: int,
    singpass_id: str,
    singpass_password: str,
    department: str = DEPARTMENT,
) -> None:
    """Upsert one user into ``users.json`` atomically; tighten to 0600.

    Existing entries are preserved untouched. The upserted record always
    carries ``department`` (defaulting to :data:`DEPARTMENT`).
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw[str(uid)] = {
        "singpass_id": singpass_id,
        "singpass_password": singpass_password,
        "department": department,
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    _ensure_perms(path)
