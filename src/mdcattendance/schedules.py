"""Validated schedules in StateStore's SQLite database, separate from its journal."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .storage import iso_day

DEFAULT_SCHEDULE_TIME = "08:30"
DAY_STATUSES = {"normal", "wfh", "mc", "none"}


def validate_time(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value) is None
        or int(value[:2]) > 23
    ):
        raise ValueError("Schedule time must use HH:MM (00:00 to 23:59)")


def _validate_days(days: dict[str, str]) -> None:
    if not isinstance(days, dict):
        raise ValueError("Schedule days must map ISO dates to attendance statuses")
    for day, status in days.items():
        iso_day(day)
        if not isinstance(day, str) or not isinstance(status, str) or status not in DAY_STATUSES:
            raise ValueError("Unsupported scheduled attendance status or date")


@dataclass(frozen=True)
class Schedule:
    time: str
    days: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_time(self.time)
        _validate_days(self.days)


def load_schedules(path: str) -> dict[int, Schedule]:
    """Read persisted schedules; never clear or reinterpret dispatch outcomes."""
    file = Path(path).expanduser().resolve()
    if not file.exists():
        return {}
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.execute("BEGIN")
        schedules = {
            int(uid): Schedule(time)
            for uid, time in db.execute("SELECT uid,time FROM schedules ORDER BY uid")
        }
        for uid, day, status in db.execute("SELECT uid,day,status FROM schedule_days ORDER BY uid,day"):
            iso_day(day)
            if status not in DAY_STATUSES:
                raise ValueError("Unsupported scheduled attendance status")
            schedules[int(uid)].days[day] = status
    return schedules


def save_schedule(
    path: str,
    uid: int,
    *,
    time: str | None = None,
    days: dict[str, str] | None = None,
) -> None:
    """Atomically merge one user's settings without modifying attempts or dispatches."""
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ValueError("User id must be a positive integer")
    if time is not None:
        validate_time(time)
    if days is not None:
        _validate_days(days)
    file = Path(path).expanduser().resolve()
    with closing(sqlite3.connect(file.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO schedules(uid,time) VALUES (?,?)",
                (uid, DEFAULT_SCHEDULE_TIME),
            )
            if time is not None:
                db.execute("UPDATE schedules SET time=? WHERE uid=?", (time, uid))
            if days is not None:
                db.executemany(
                    """INSERT INTO schedule_days(uid,day,status) VALUES (?,?,?)
                       ON CONFLICT(uid,day) DO UPDATE SET status=excluded.status""",
                    [(uid, day, status) for day, status in days.items()],
                )
