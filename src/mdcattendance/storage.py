"""Restricted, credential-free attendance state and durable dispatch journal."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SINGAPORE = ZoneInfo("Asia/Singapore")
MODES = {"submit", "dry_run", "preflight", "discover"}
TERMINAL_STATUSES = {"confirmed", "failed", "unknown", "dry_run", "preflight", "discovered"}
DISPATCH_STATUSES = TERMINAL_STATUSES | {"missed", "skipped"}
_SAFE_DETAILS = {
    "",
    "interrupted before submission",
    "submission interrupted",
    "execution failed",
    "run timed out",
    "run cancelled",
    "submission not confirmed",
    "duplicate submission blocked",
    "attendance window closed",
    "scheduled preparation failed",
    "scheduled run cancelled",
    "scheduled time must be before deadline",
    "browser busy until deadline",
    "scheduled date missed",
    "details omitted",
}


class DuplicateRun(RuntimeError):
    """A submission for this account, form and day already exists."""


def safe_detail(detail: str) -> str:
    """Never persist arbitrary exception messages or operator-supplied text."""
    return detail if detail in _SAFE_DETAILS else "details omitted"


def iso_day(value: str | date) -> str:
    if isinstance(value, datetime):
        raise ValueError("Attendance date must be a date, not a timestamp")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError("Attendance date must be an ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Attendance date must use YYYY-MM-DD")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _form_key(form: str) -> str:
    # Query strings, userinfo and fragments are not part of a FormSG form's identity.
    parsed = urlsplit(form.strip())
    host = (parsed.hostname or "").lower()
    if parsed.port is not None:
        host += f":{parsed.port}"
    identity = f"{host}{parsed.path.rstrip('/')}"
    return hashlib.sha256(identity.encode()).hexdigest()


class StateStore:
    """One SQLite database; credentials and answers never enter this API."""

    def __init__(self, state_dir: str, retention_days: int = 90) -> None:
        if (
            not isinstance(retention_days, int)
            or isinstance(retention_days, bool)
            or retention_days <= 0
        ):
            raise ValueError("Retention must be a positive number of days")
        directory = Path(state_dir).expanduser().resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.state_dir = str(directory)
        self.path = str(directory / "attendance.sqlite3")
        self.retention_days = retention_days
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._db = sqlite3.connect(self.path, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA secure_delete = ON")
        self._db.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY,
                account TEXT NOT NULL,
                form TEXT NOT NULL,
                attendance_date TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('submit','dry_run','preflight','discover')),
                status TEXT NOT NULL CHECK(status IN (
                    'prepared','running','submitting','confirmed','failed','unknown',
                    'dry_run','preflight','discovered')),
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempt_identity
                ON attempts(account, form, attendance_date, mode, status);
            CREATE TABLE IF NOT EXISTS schedules (
                uid INTEGER PRIMARY KEY,
                time TEXT NOT NULL DEFAULT '08:30'
            );
            CREATE TABLE IF NOT EXISTS schedule_days (
                uid INTEGER NOT NULL REFERENCES schedules(uid),
                day TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('normal','wfh','mc','none')),
                PRIMARY KEY(uid, day)
            );
            CREATE TABLE IF NOT EXISTS schedule_dispatch (
                uid INTEGER NOT NULL,
                day TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'collecting','confirmed','failed','unknown','dry_run','preflight',
                    'discovered','missed','skipped')),
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(uid, day)
            );
        """)

    def close(self) -> None:
        self._db.close()

    def prepare(
        self,
        account: str,
        form: str,
        attendance_date: str | date,
        mode: str,
        override: bool = False,
    ) -> int:
        if mode not in MODES:
            raise ValueError("Unsupported execution mode")
        day = iso_day(attendance_date)
        account_key = hashlib.sha256(account.strip().casefold().encode()).hexdigest()
        form_key = _form_key(form)
        stamp = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if mode == "submit" and not override:
                duplicate = self._db.execute(
                    """SELECT 1 FROM attempts WHERE account=? AND form=?
                       AND attendance_date=? AND mode='submit'
                       AND status IN ('prepared','running','submitting','confirmed','unknown')
                       LIMIT 1""",
                    (account_key, form_key, day),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateRun("Submission already recorded; explicit override required")
            cursor = self._db.execute(
                """INSERT INTO attempts
                   (account,form,attendance_date,mode,status,created_at,updated_at)
                   VALUES (?,?,?,?,'prepared',?,?)""",
                (account_key, form_key, day, mode, stamp, stamp),
            )
            attempt_id = cursor.lastrowid
            if attempt_id is None:
                raise RuntimeError("SQLite did not return a row id for the new attempt")
            self._db.commit()
            return attempt_id
        except BaseException:
            self._db.rollback()
            raise

    def transition(self, attempt_id: int, status: str, detail: str = "") -> None:
        allowed = {
            "prepared": {"running", "failed"},
            "running": {"submitting", "failed", "dry_run", "preflight", "discovered"},
            "submitting": {"confirmed", "unknown"},
        }
        with self._db:
            row = self._db.execute(
                "SELECT status FROM attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if status not in allowed.get(row["status"], set()):
                raise ValueError("Invalid attempt state transition")
            self._db.execute(
                "UPDATE attempts SET status=?,detail=?,updated_at=? WHERE id=?",
                (status, safe_detail(detail), _now(), attempt_id),
            )

    def get_attempt(self, attempt_id: int) -> dict:
        row = self._db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return dict(row)

    def attempts(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM attempts ORDER BY id")]

    def recover_attempts(self) -> None:
        """Caller MUST hold run.lock; do not invalidate live schedule collectors."""
        stamp = _now()
        with self._db:
            self._db.execute(
                """UPDATE attempts SET status='failed',detail='interrupted before submission',
                   updated_at=? WHERE status IN ('prepared','running')""",
                (stamp,),
            )
            self._db.execute(
                """UPDATE attempts SET status='unknown',detail='submission interrupted',
                   updated_at=? WHERE status='submitting'""",
                (stamp,),
            )
        self._prune()

    def recover(self) -> None:
        """Startup only, with run.lock and scheduler.lock held by the caller."""
        self.recover_attempts()
        with self._db:
            self._db.execute(
                """UPDATE schedule_dispatch SET status='missed',detail='scheduled run cancelled',
                   updated_at=? WHERE status='collecting'""",
                (_now(),),
            )

    def claim_schedule(self, uid: int, day: str) -> bool:
        day = iso_day(day)
        stamp = _now()
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO schedule_dispatch
                   (uid,day,status,created_at,updated_at) VALUES (?,?,'collecting',?,?)""",
                (uid, day, stamp, stamp),
            )
        return cursor.rowcount == 1

    def finish_schedule(self, uid: int, day: str, status: str, detail: str = "") -> None:
        if status not in DISPATCH_STATUSES:
            raise ValueError("Unsupported scheduled outcome")
        with self._db:
            cursor = self._db.execute(
                """UPDATE schedule_dispatch SET status=?,detail=?,updated_at=?
                   WHERE uid=? AND day=? AND status='collecting'""",
                (status, safe_detail(detail), _now(), uid, iso_day(day)),
            )
            if cursor.rowcount != 1:
                raise ValueError("Schedule dispatch is absent or already completed")

    def get_schedule_dispatch(self, uid: int, day: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM schedule_dispatch WHERE uid=? AND day=?",
            (uid, iso_day(day)),
        ).fetchone()
        return dict(row) if row is not None else None

    def collecting_dispatches(self) -> list[dict]:
        return [
            dict(row)
            for row in self._db.execute(
                "SELECT * FROM schedule_dispatch WHERE status='collecting' ORDER BY day,uid"
            )
        ]

    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        today = datetime.now(SINGAPORE).date().isoformat()
        oldest_day = (datetime.now(SINGAPORE).date() - timedelta(days=self.retention_days)).isoformat()
        with self._db:
            # Old confirmed/unknown dates cannot execute again: the runner accepts only today.
            self._db.execute(
                """DELETE FROM attempts WHERE updated_at<? AND attendance_date<?
                   AND status NOT IN ('prepared','running','submitting')""",
                (cutoff.isoformat(timespec="microseconds"), today),
            )
            self._db.execute(
                "DELETE FROM schedule_dispatch WHERE day<? AND status!='collecting'",
                (oldest_day,),
            )
            self._db.execute("DELETE FROM schedule_days WHERE day<?", (oldest_day,))
        self._db.execute("PRAGMA incremental_vacuum(256)")
