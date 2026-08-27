"""Per-user submission schedule store for the Telegram bot.

``schedules.json`` maps Telegram user ids to a submission time and a per-day
status. The file is gitignored and tightened to 0600 on write. Reloaded on
each schedule change so edits take effect without a restart.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Schedule:
    time: str
    days: dict[str, str] = field(default_factory=dict)


def load_schedules(path: str) -> dict[int, Schedule]:
    """Load ``schedules.json``. Missing file -> {} (no schedules).

    Tolerates a missing ``"time"`` (defaults to ``"09:00"``) and a missing
    ``"days"`` (defaults to ``{}``). Non-string day values are ignored.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    schedules: dict[int, Schedule] = {}
    for key, rec in raw.items():
        try:
            uid = int(key)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        t = str(rec.get("time", "09:00")).strip() or "09:00"
        days: dict[str, str] = {}
        days_raw = rec.get("days", {})
        if isinstance(days_raw, dict):
            for dk, dv in days_raw.items():
                if isinstance(dv, str):
                    days[str(dk)] = dv
        schedules[uid] = Schedule(t, days)
    return schedules


def save_schedule(
    path: str,
    uid: int,
    *,
    time: str | None = None,
    days: dict[str, str] | None = None,
) -> None:
    """Read-modify-write one user's schedule atomically; tighten to 0600.

    If ``time`` is given it overwrites the stored time. If ``days`` is given
    each key is merged into the stored days (setting a day to ``"none"`` keeps
    the key so it renders as cleared).
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    key = str(uid)
    rec = raw.get(key)
    if not isinstance(rec, dict):
        rec = {}
    if time is not None:
        rec["time"] = time
    if days is not None:
        existing = rec.get("days", {})
        if not isinstance(existing, dict):
            existing = {}
        existing.update(days)
        rec["days"] = existing
    raw[key] = rec
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    _ensure_perms(path)


def _ensure_perms(path: str) -> None:
    """Tighten the schedule file to 0600 if it is more open."""
    try:
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass
