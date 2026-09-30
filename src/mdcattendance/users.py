"""Read-only, locally provisioned Telegram credential allowlist."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .attendance import DEPARTMENT
from .config import restrict_secret_file
from .otp import validate_otp_token


@dataclass(frozen=True)
class User:
    telegram_id: int
    singpass_id: str
    singpass_password: str
    department: str
    otp_token: str = field(default="", repr=False)


def load_users(path: str) -> dict[int, User]:
    """Read an existing restricted allowlist; never create or save credentials."""
    restrict_secret_file(path)
    with open(path, encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict):
        raise ValueError("User allowlist must be a JSON object")
    users: dict[int, User] = {}
    tokens: set[str] = set()
    for key, rec in raw.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
            raise ValueError("Allowlist keys must be positive Telegram user IDs")
        tid = int(key)
        if tid <= 0 or tid in users or not isinstance(rec, dict):
            raise ValueError("Invalid or duplicate Telegram user ID")
        sid = rec.get("singpass_id")
        password = rec.get("singpass_password")
        department = rec.get("department", DEPARTMENT)
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError("Allowlisted user is missing singpass_id")
        if not isinstance(password, str) or not password.strip():
            raise ValueError("Allowlisted user is missing singpass_password")
        if not isinstance(department, str) or department.strip() != DEPARTMENT:
            raise ValueError("Allowlisted user has an unsupported department")
        token = rec.get("otp_token", "")
        if not isinstance(token, str):
            raise ValueError("User OTP token must be a string")
        if token:
            validate_otp_token(token)
            if token in tokens:
                raise ValueError("User OTP tokens must be unique")
            tokens.add(token)
        users[tid] = User(tid, sid.strip(), password, DEPARTMENT, token)
    return users
