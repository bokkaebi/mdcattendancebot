"""Restricted, credential-free attendance state and durable dispatch journal."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SINGAPORE = ZoneInfo("Asia/Singapore")
MODES = {"submit", "dry_run", "preflight", "discover"}
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
    "existing attendance found",
    "external source unavailable",
    "attendance identity required",
    "decision changed before submission",
    "source evidence changed before submission",
    "details omitted",
}


@dataclass(frozen=True)
class AdditionalSubmissionConsent:
    """One reviewed additional-submission authorisation, consumed at prepare.

    The fields mirror the durable review row exactly. A free-standing token is
    never accepted: the runner re-checks the date, canonical answer hash, source
    digest, reviewed attempt ids and consent digest against live and durable
    evidence before it creates an attempt, and the store consumes them in the
    same transaction as that attempt.
    """

    date: date
    answer_hash: str
    records_digest: str
    local_attempt_ids: tuple[int, ...]
    consent_digest: str
    decision_uid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.date, date) or isinstance(self.date, datetime):
            raise ValueError("Reviewed date must be a calendar date")
        for value, label in (
            (self.answer_hash, "answer hash"),
            (self.records_digest, "records digest"),
            (self.consent_digest, "consent digest"),
        ):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"Reviewed {label} must be SHA-256")
        if not isinstance(self.local_attempt_ids, tuple) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in self.local_attempt_ids
        ):
            raise ValueError("Reviewed local attempts must be positive integer ids")
        if self.decision_uid is not None and (
            not isinstance(self.decision_uid, int) or isinstance(self.decision_uid, bool)
        ):
            raise ValueError("Reviewed decision owner must be an integer uid")


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
            CREATE TABLE IF NOT EXISTS cli_consents (
                account TEXT NOT NULL,
                form TEXT NOT NULL,
                day TEXT NOT NULL,
                answer_hash TEXT NOT NULL,
                records_digest TEXT NOT NULL,
                attempt_ids TEXT NOT NULL,
                consent_digest TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
                created_at TEXT NOT NULL,
                consumed_at TEXT,
                PRIMARY KEY(account, form, day)
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
    ) -> int:
        if mode not in MODES:
            raise ValueError("Unsupported execution mode")
        day = iso_day(attendance_date)
        account_key = hashlib.sha256(account.strip().casefold().encode()).hexdigest()
        form_key = _form_key(form)
        stamp = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if mode == "submit":
                duplicate = self._db.execute(
                    """SELECT 1 FROM attempts WHERE account=? AND form=?
                       AND attendance_date=? AND mode='submit'
                       AND status IN ('prepared','running','submitting','confirmed','unknown')
                       LIMIT 1""",
                    (account_key, form_key, day),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateRun("Submission already recorded; review before resubmitting")
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

    def prepare_consent(
        self,
        account: str,
        form: str,
        attendance_date: str | date,
        mode: str,
        consent: AdditionalSubmissionConsent,
    ) -> int:
        """Atomically authorise one additional submission attempt and consume consent.

        Consent is never trusted as a free-standing object: its date must equal the
        attendance date, and its answer hash, external records digest and reviewed
        attempt ids are re-read from the durable authorisation row and checked
        against the live journal in the same transaction as the new attempt. The
        runner, not this durable primitive, owns the wall-clock "today" rule.
        """
        if mode != "submit":
            raise ValueError("Consent applies only to real submissions")
        if not isinstance(consent, AdditionalSubmissionConsent):
            raise ValueError("Consent must be an AdditionalSubmissionConsent")
        day = iso_day(attendance_date)
        if consent.date != date.fromisoformat(day):
            raise DuplicateRun("Consent is not for today's submission; review again")
        account_key = hashlib.sha256(account.strip().casefold().encode()).hexdigest()
        form_key = _form_key(form)
        stamp = _now()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            stored: dict | None = None
            if consent.decision_uid is not None:
                stored = self._decision_consent(consent, day)
            else:
                self._cli_consent(consent, account_key, form_key, day)
            self._assert_reviewed(account_key, form_key, day, list(consent.local_attempt_ids))
            cursor = self._db.execute(
                """INSERT INTO attempts
                   (account,form,attendance_date,mode,status,created_at,updated_at)
                   VALUES (?,?,?,?,'prepared',?,?)""",
                (account_key, form_key, day, mode, stamp, stamp),
            )
            attempt_id = cursor.lastrowid
            if attempt_id is None:
                raise RuntimeError("SQLite did not return a row id for the new attempt")
            if stored is not None:
                stored["consumed"] = True
                self._db.execute(
                    """UPDATE daily_decisions SET consent_json=?,attempt_id=?,
                       revision=revision+1,updated_at=? WHERE uid=? AND day=?""",
                    (
                        json.dumps(stored, sort_keys=True, separators=(",", ":")),
                        attempt_id,
                        _now(),
                        consent.decision_uid,
                        day,
                    ),
                )
            else:
                self._db.execute(
                    """UPDATE cli_consents SET consumed=1,consumed_at=?
                       WHERE account=? AND form=? AND day=?""",
                    (_now(), account_key, form_key, day),
                )
            self._db.commit()
            return attempt_id
        except BaseException:
            self._db.rollback()
            raise

    def authorize_cli_consent(
        self,
        account: str,
        form: str,
        attendance_date: str | date,
        *,
        answer_hash: str,
        records_digest: str,
        local_attempt_ids: list[int],
    ) -> AdditionalSubmissionConsent:
        """Durably authorise one reviewed additional submission; return the consent.

        Manual/CLI reviews have no Telegram decision row, so consent lives in an
        account/form/day row consumed by prepare_consent at attempt preparation.
        """
        day = iso_day(attendance_date)
        for value, label in ((answer_hash, "answer hash"), (records_digest, "records digest")):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"Reviewed {label} must be SHA-256")
        if not isinstance(local_attempt_ids, (list, tuple)) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in local_attempt_ids
        ):
            raise ValueError("Reviewed local attempts must be positive integer ids")
        ids = sorted(set(local_attempt_ids))
        account_key = hashlib.sha256(account.strip().casefold().encode()).hexdigest()
        form_key = _form_key(form)
        token = secrets.token_hex(32)
        consent_date = date.fromisoformat(day)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            actual = {
                row["id"]
                for row in self._db.execute(
                    """SELECT id FROM attempts WHERE account=? AND form=?
                       AND attendance_date=? AND mode='submit'""",
                    (account_key, form_key, day),
                )
            }
            if actual != set(ids):
                raise DuplicateRun("Reviewed attempts must match today's recorded attempts")
            self._db.execute(
                """INSERT INTO cli_consents
                   (account,form,day,answer_hash,records_digest,attempt_ids,
                    consent_digest,consumed,created_at,consumed_at)
                   VALUES (?,?,?,?,?,?,?,0,?,NULL)
                   ON CONFLICT(account,form,day) DO UPDATE SET
                   answer_hash=excluded.answer_hash,records_digest=excluded.records_digest,
                   attempt_ids=excluded.attempt_ids,consent_digest=excluded.consent_digest,
                   consumed=0,created_at=excluded.created_at,consumed_at=NULL""",
                (
                    account_key,
                    form_key,
                    day,
                    answer_hash,
                    records_digest,
                    json.dumps(ids),
                    token,
                    _now(),
                ),
            )
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        return AdditionalSubmissionConsent(
            date=consent_date,
            answer_hash=answer_hash,
            records_digest=records_digest,
            local_attempt_ids=tuple(ids),
            consent_digest=token,
        )

    def submission_attempts(
        self, account: str, form: str, attendance_date: str | date
    ) -> list[dict]:
        """Reviewed local blockers for one account, form and day; never resets history."""
        day = iso_day(attendance_date)
        account_key = hashlib.sha256(account.strip().casefold().encode()).hexdigest()
        return [
            dict(row)
            for row in self._db.execute(
                """SELECT * FROM attempts WHERE account=? AND form=? AND attendance_date=?
                   AND mode='submit' ORDER BY id""",
                (account_key, _form_key(form), day),
            )
        ]

    @staticmethod
    def _matches(stored: object, expected: object) -> bool:
        return isinstance(stored, str) and isinstance(expected, str) and hmac.compare_digest(
            stored, expected
        )

    def _decision_consent(self, consent: AdditionalSubmissionConsent, day: str) -> dict:
        """Validate the durable decision consent row without consuming it."""
        row = self._db.execute(
            "SELECT consent_json FROM daily_decisions WHERE uid=? AND day=?",
            (consent.decision_uid, day),
        ).fetchone()
        if row is None or row["consent_json"] is None:
            raise DuplicateRun("Authorised consent is no longer available; review again")
        stored = json.loads(row["consent_json"])
        if (
            not stored.get("authorized")
            or stored.get("consumed")
            or stored.get("date") != day
            or stored.get("answer_hash") != consent.answer_hash
            or stored.get("records_digest") != consent.records_digest
            or stored.get("local_attempt_ids") != list(consent.local_attempt_ids)
            or not self._matches(stored.get("consent_digest"), consent.consent_digest)
        ):
            raise DuplicateRun("Evidence or answers changed; review again")
        return stored

    def _cli_consent(
        self, consent: AdditionalSubmissionConsent, account_key: str, form_key: str, day: str
    ) -> sqlite3.Row:
        """Validate the durable manual/CLI consent row without consuming it."""
        row = self._db.execute(
            "SELECT * FROM cli_consents WHERE account=? AND form=? AND day=?",
            (account_key, form_key, day),
        ).fetchone()
        if row is None or row["consumed"]:
            raise DuplicateRun("Authorised consent is no longer available; review again")
        if (
            row["answer_hash"] != consent.answer_hash
            or row["records_digest"] != consent.records_digest
            or json.loads(row["attempt_ids"]) != list(consent.local_attempt_ids)
            or not self._matches(row["consent_digest"], consent.consent_digest)
        ):
            raise DuplicateRun("Evidence or answers changed; review again")
        return row

    def _assert_reviewed(
        self, account_key: str, form_key: str, day: str, reviewed: object
    ) -> None:
        """Every live local attempt for this account, form and day must be reviewed."""
        if not isinstance(reviewed, list) or any(
            not isinstance(value, int) or isinstance(value, bool) for value in reviewed
        ):
            raise DuplicateRun("An unreviewed attempt blocks this submission; review again")
        actual = {
            row["id"]
            for row in self._db.execute(
                """SELECT id FROM attempts WHERE account=? AND form=?
                   AND attendance_date=? AND mode='submit'""",
                (account_key, form_key, day),
            )
        }
        if actual != set(reviewed):
            raise DuplicateRun("An unreviewed attempt blocks this submission; review again")

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

    def upgrade_planner(self, *, today: date | None = None, attendance_deadline: str = "09:00") -> None:
        """Idempotent upgrade. Caller MUST hold run.lock and scheduler.lock at startup."""
        from .schedules import validate_time

        validate_time(attendance_deadline)
        day = iso_day(today or datetime.now(SINGAPORE).date())
        # execute(), not executescript(): DDL and imports must commit together.
        statements = (
            """CREATE TABLE IF NOT EXISTS attendance_settings (
                uid INTEGER PRIMARY KEY,
                attendance_name TEXT,
                name_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(name_confirmed IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                prompt_time TEXT NOT NULL DEFAULT '08:00',
                auto_time TEXT NOT NULL DEFAULT '08:30',
                revision INTEGER NOT NULL DEFAULT 0,
                plan_revision INTEGER NOT NULL DEFAULT 0,
                needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0,1))
            )""",
            """CREATE TABLE IF NOT EXISTS attendance_plans (
                uid INTEGER NOT NULL,
                day TEXT NOT NULL,
                profile TEXT NOT NULL CHECK(profile IN ('normal','wfh','ma','skip')),
                details_json TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(uid,day)
            )""",
            """CREATE TABLE IF NOT EXISTS daily_decisions (
                uid INTEGER NOT NULL,
                day TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                profile TEXT CHECK(profile IN ('normal','wfh','ma','skip')),
                details_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL CHECK(state IN (
                    'awaiting','held','ready','running','recorded','skipped',
                    'confirmed','failed','unknown','missed')),
                execute_at TEXT NOT NULL,
                prompt_message_id INTEGER,
                reminder_sent INTEGER NOT NULL DEFAULT 0 CHECK(reminder_sent IN (0,1)),
                prompt_sent INTEGER NOT NULL DEFAULT 0 CHECK(prompt_sent IN (0,1)),
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
                attempt_id INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
                records_digest TEXT,
                consent_json TEXT,
                reason TEXT NOT NULL DEFAULT '',
                original_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(uid,day)
            )""",
            """CREATE TABLE IF NOT EXISTS attendance_observations (
                uid INTEGER NOT NULL,
                day TEXT NOT NULL,
                digest TEXT NOT NULL,
                records_json TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                PRIMARY KEY(uid,day)
            )""",
            """CREATE TABLE IF NOT EXISTS attendance_migrations (
                name TEXT PRIMARY KEY, completed_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS cli_consents (
                account TEXT NOT NULL,
                form TEXT NOT NULL,
                day TEXT NOT NULL,
                answer_hash TEXT NOT NULL,
                records_digest TEXT NOT NULL,
                attempt_ids TEXT NOT NULL,
                consent_digest TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
                created_at TEXT NOT NULL,
                consumed_at TEXT,
                PRIMARY KEY(account, form, day)
            )""",
        )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self._db.execute(statement)
            imported = self._db.execute(
                "SELECT 1 FROM attendance_migrations WHERE name='legacy_planner_v1'"
            ).fetchone()
            if imported is None:
                for row in self._db.execute("SELECT uid,time FROM schedules"):
                    old_time = row["time"]
                    try:
                        validate_time(old_time)
                        acceptable = "08:00" < old_time < attendance_deadline
                    except ValueError:
                        acceptable = False
                    self._db.execute(
                        """INSERT OR IGNORE INTO attendance_settings
                           (uid,auto_time,needs_review) VALUES (?,?,?)""",
                        (row["uid"], old_time if acceptable else "08:30", int(not acceptable)),
                    )
                self._db.execute(
                    """INSERT OR IGNORE INTO attendance_plans(uid,day,profile)
                       SELECT uid,day,CASE status WHEN 'none' THEN 'skip' ELSE status END
                       FROM schedule_days WHERE day>=? AND status IN ('normal','wfh','none')""",
                    (day,),
                )
                for row in self._db.execute(
                    "SELECT uid,day,status FROM schedule_dispatch WHERE day=?", (day,)
                ):
                    state = row["status"]
                    if state not in {"confirmed", "failed", "unknown", "missed", "skipped"}:
                        state = "missed"
                    self._db.execute(
                        """INSERT OR IGNORE INTO attendance_settings(uid) VALUES (?)""",
                        (row["uid"],),
                    )
                    self._db.execute(
                        """INSERT OR IGNORE INTO daily_decisions
                           (uid,day,state,execute_at,reason,updated_at)
                           VALUES (?,?,?,?,'legacy_dispatch',?)""",
                        (row["uid"], row["day"], state, f"{day}T08:30:00+08:00", _now()),
                    )
                self._db.execute(
                    "INSERT INTO attendance_migrations VALUES ('legacy_planner_v1',?)", (_now(),)
                )
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def recover(self, *, attendance_deadline: str = "09:00") -> None:
        """Startup only, with run.lock and scheduler.lock held by the caller."""
        self.upgrade_planner(attendance_deadline=attendance_deadline)
        self.recover_attempts()
        with self._db:
            # A claimed decision must never be replayed, even if its attempt was not
            # created before the crash. Existing unknown attempt evidence wins.
            self._db.execute(
                """UPDATE daily_decisions SET
                   state=CASE WHEN EXISTS (
                       SELECT 1 FROM attempts WHERE id=daily_decisions.attempt_id
                       AND status='unknown') THEN 'unknown'
                       WHEN EXISTS (SELECT 1 FROM attempts WHERE id=daily_decisions.attempt_id
                       AND status='confirmed') THEN 'confirmed' ELSE 'failed' END,
                   reason='interrupted',revision=revision+1,updated_at=?
                   WHERE state='running'""",
                (_now(),),
            )


    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        today = datetime.now(SINGAPORE).date().isoformat()
        with self._db:
            # Old confirmed/unknown dates cannot execute again: the runner accepts only today.
            self._db.execute(
                """DELETE FROM attempts WHERE updated_at<? AND attendance_date<?
                   AND status NOT IN ('prepared','running','submitting')""",
                (cutoff.isoformat(timespec="microseconds"), today),
            )
        self._db.execute("PRAGMA incremental_vacuum(256)")
