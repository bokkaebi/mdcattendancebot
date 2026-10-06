"""Validated attendance settings, plans and daily decisions in StateStore's SQLite database."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any

from .storage import SINGAPORE, StateStore, iso_day


def validate_time(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value) is None
        or int(value[:2]) > 23
    ):
        raise ValueError("Schedule time must use HH:MM (00:00 to 23:59)")


class PlannerInvalid(ValueError):
    """Invalid owner input (HTTP 422)."""


class PlannerConflict(RuntimeError):
    """An expected revision is stale (HTTP 409); refresh before reviewing again."""


class PlannerBlocked(RuntimeError):
    """A durable execution or evidence boundary prevents this action (HTTP 409)."""


_FINAL = {"recorded", "skipped", "confirmed", "unknown", "missed"}
_PROFILES = {"normal", "wfh", "ma", "skip"}
_REASONS = {
    "",
    "unplanned",
    "incomplete",
    "user_edit",
    "manual_submitted",
    "verification",
    "records_found",
    "interrupted",
    "deadline",
    "execution_failed",
    "otp_unavailable",
    "legacy_dispatch",
    "onboarding",
    "disabled",
    "identity_collision",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _revision(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PlannerInvalid("Expected revision must be a nonnegative integer")


def _uid(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PlannerInvalid("User id must be a positive integer")


def validate_profile(profile: str | None, details: dict | None = None) -> tuple[dict, bool]:
    """Validate planner inputs. MA cannot execute without authenticated form evidence."""
    if profile is not None and (not isinstance(profile, str) or profile not in _PROFILES):
        raise PlannerInvalid("Unsupported attendance profile")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise PlannerInvalid("Profile details must be an object")
    if profile != "ma":
        if details:
            raise PlannerInvalid("This profile does not accept details")
        return {}, profile in {"normal", "wfh"}
    if set(details) - {"period", "timing", "other_status"}:
        raise PlannerInvalid("Unsupported MA detail")
    period = details.get("period")
    if period is not None and (not isinstance(period, str) or period not in {"am", "pm", "both"}):
        raise PlannerInvalid("MA period must be am, pm or both")
    result = dict(details)
    timing = details.get("timing")
    if timing is not None:
        if not isinstance(timing, str):
            raise PlannerInvalid("MA timing must use HH:MM or HHMM")
        if re.fullmatch(r"\d{4}", timing):
            timing = timing[:2] + ":" + timing[2:]
        validate_time(timing)
        result["timing"] = timing
    # There are no verified other-half options. Never accept arbitrary sheet labels.
    if details.get("other_status") is not None:
        raise PlannerInvalid("MA other-half options require authenticated form evidence")
    result.pop("other_status", None)
    return result, False


class Planner:
    """Synchronous transactional primitives shared by Telegram, scheduler and HTTP.

    Settings and whole-plan revisions are independent. Each decision has its own
    revision. All owner mutations use BEGIN IMMEDIATE and require the matching
    revision; no network or browser work belongs inside these transactions.
    """

    def __init__(
        self,
        store: StateStore,
        attendance_deadline: str = "09:00",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        validate_time(attendance_deadline)
        self.store = store
        self._db = store._db
        self.attendance_deadline = attendance_deadline
        self.clock = clock or (lambda: datetime.now(SINGAPORE))
        if (
            self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='daily_decisions' AND type='table'"
            ).fetchone()
            is None
        ):
            raise RuntimeError("Planner schema must be upgraded under startup process locks")

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PlannerInvalid("Planner clock must be timezone-aware")
        return now.astimezone(SINGAPORE)

    def _day(self, day: str | date | None = None, *, today_only: bool = False) -> str:
        current = self._now().date()
        value = iso_day(current if day is None else day)
        if today_only and value != current.isoformat():
            raise PlannerInvalid("This action is permitted only for today")
        return value

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    @staticmethod
    def _check(row: dict, expected_revision: int, field: str = "revision") -> None:
        _revision(expected_revision)
        if row[field] != expected_revision:
            raise PlannerConflict("State changed; refresh and review before saving")

    def _settings(self, uid: int) -> dict:
        _uid(uid)
        row = self._db.execute("SELECT * FROM attendance_settings WHERE uid=?", (uid,)).fetchone()
        return (
            dict(row)
            if row is not None
            else {
                "uid": uid,
                "attendance_name": None,
                "name_confirmed": 0,
                "enabled": 0,
                "prompt_time": "08:00",
                "auto_time": "08:30",
                "revision": 0,
                "plan_revision": 0,
                "needs_review": 0,
            }
        )

    def get_settings(self, uid: int) -> dict:
        result = self._settings(uid)
        for field in ("enabled", "name_confirmed", "needs_review"):
            result[field] = bool(result[field])
        result["attendance_deadline"] = self.attendance_deadline
        result["time_margin_warning"] = (
            datetime.combine(date.min, time.fromisoformat(self.attendance_deadline))
            - datetime.combine(date.min, time.fromisoformat(result["auto_time"]))
        ).total_seconds() < 600
        return result

    def _insert_settings(self, uid: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO attendance_settings(uid) VALUES (?)", (uid,))

    def set_name(self, uid: int, name: str, *, expected_revision: int) -> dict:
        from .records import normalize_name

        name = normalize_name(name)
        with self._transaction():
            settings = self._settings(uid)
            self._check(settings, expected_revision)
            decision = self.get_decision(uid)
            if decision and decision["state"] == "running":
                raise PlannerBlocked("Name cannot change during an attendance run")
            if settings["attendance_name"] == name and settings["name_confirmed"]:
                # Re-saving the same confirmed mapping is a no-op: observations, consent,
                # settings and today's decision belong to this identity and must survive.
                return self.get_settings(uid)
            self._insert_settings(uid)
            self._db.execute(
                """UPDATE attendance_settings SET attendance_name=?,name_confirmed=0,
                   enabled=0,revision=revision+1 WHERE uid=?""",
                (name, uid),
            )
            # Observations belong to the confirmed mapping, not merely the Telegram uid.
            self._db.execute("DELETE FROM attendance_observations WHERE uid=?", (uid,))
            self._db.execute(
                """UPDATE daily_decisions SET consent_json=NULL,revision=revision+1,
                   state=CASE WHEN state IN ('awaiting','ready') THEN 'held' ELSE state END,
                   reason=CASE WHEN state IN ('awaiting','ready') THEN 'onboarding'
                          ELSE reason END, updated_at=? WHERE uid=? AND day=?""",
                (self._now().isoformat(), uid, self._day()),
            )
        return self.get_settings(uid)

    def confirm_name(self, uid: int, *, expected_revision: int) -> dict:
        with self._transaction():
            settings = self._settings(uid)
            self._check(settings, expected_revision)
            if not settings["attendance_name"]:
                raise PlannerInvalid("Enter an attendance name before confirming")
            self._db.execute(
                """UPDATE attendance_settings SET name_confirmed=1,revision=revision+1
                   WHERE uid=?""",
                (uid,),
            )
        return self.get_settings(uid)

    def assert_identity_unique(
        self, uid: int, department: str, owner_departments: dict[int, str]
    ) -> None:
        """Provisioning supplies departments; a collision disables both mappings."""
        settings = self._settings(uid)
        if not settings["attendance_name"] or not isinstance(department, str) or not department.strip():
            raise PlannerBlocked("A confirmed name and provisioned department are required")
        normalized_department = " ".join(department.split()).casefold()
        conflicting = [
            row["uid"]
            for row in self._db.execute(
                "SELECT uid FROM attendance_settings WHERE attendance_name=? AND uid!=?",
                (settings["attendance_name"], uid),
            )
            if " ".join(owner_departments.get(row["uid"], "").split()).casefold()
            == normalized_department
        ]
        if not conflicting:
            return
        with self._transaction():
            for owner in [uid, *conflicting]:
                self._db.execute(
                    """UPDATE attendance_settings SET enabled=0,name_confirmed=0,
                       revision=revision+1 WHERE uid=?""",
                    (owner,),
                )
                self._db.execute(
                    """UPDATE daily_decisions SET consent_json=NULL,revision=revision+1,
                       state=CASE WHEN state IN ('awaiting','ready','held') THEN 'held' ELSE state END,
                       reason='identity_collision',updated_at=? WHERE uid=? AND day=?""",
                    (self._now().isoformat(), owner, self._day()),
                )
        raise PlannerBlocked("Attendance identity is ambiguous; operator resolution required")

    def save_settings(
        self,
        uid: int,
        *,
        expected_revision: int,
        enabled: bool,
        prompt_time: str,
        auto_time: str,
        otp_ready: bool = False,
        policy_accepted: bool = False,
    ) -> dict:
        if not isinstance(enabled, bool):
            raise PlannerInvalid("Enabled must be a boolean")
        validate_time(prompt_time)
        validate_time(auto_time)
        if not prompt_time < auto_time < self.attendance_deadline:
            raise PlannerInvalid("Prompt must precede automatic time and attendance deadline")
        with self._transaction():
            settings = self._settings(uid)
            self._check(settings, expected_revision)
            if enabled and not (
                settings["name_confirmed"] and otp_ready and (settings["enabled"] or policy_accepted)
            ):
                raise PlannerInvalid("Confirm name, configure phone OTP and accept automatic policy")
            decision = self.get_decision(uid)
            if decision and decision["state"] == "running":
                raise PlannerBlocked("Settings cannot change during an attendance run")
            self._insert_settings(uid)
            self._db.execute(
                """UPDATE attendance_settings SET enabled=?,prompt_time=?,auto_time=?,
                   needs_review=0,revision=revision+1 WHERE uid=?""",
                (int(enabled), prompt_time, auto_time, uid),
            )
            if decision and decision["state"] in {"awaiting", "ready", "held"}:
                changes = {"execute_at": f"{self._day()}T{auto_time}:00+08:00"}
                if not enabled and (
                    decision["state"] != "held"
                    or decision["reason"] in {"disabled", "onboarding", "otp_unavailable"}
                ):
                    changes.update(state="held", reason="disabled")
                elif decision["state"] == "held" and decision["reason"] in {"disabled", "onboarding"}:
                    _, ready = validate_profile(decision["profile"], decision["details"])
                    state, reason = self._profile_state(decision["profile"], ready, enabled)
                    changes.update(state=state, reason=reason)
                self._write_decision(uid, self._day(), **changes)
        return self.get_settings(uid)

    def get_plan(self, uid: int) -> dict:
        settings = self._settings(uid)
        today = self._now().date()
        end = today + timedelta(days=13)
        rows = {
            row["day"]: row
            for row in self._db.execute(
                "SELECT * FROM attendance_plans WHERE uid=? AND day BETWEEN ? AND ?",
                (uid, today.isoformat(), end.isoformat()),
            )
        }
        days = []
        for offset in range(14):
            day = (today + timedelta(days=offset)).isoformat()
            row = rows.get(day)
            profile = row["profile"] if row is not None else None
            details = json.loads(row["details_json"]) if row is not None else {}
            _, ready = validate_profile(profile, details)
            days.append(
                {
                    "date": day,
                    "profile": profile,
                    "details": details,
                    "ready": ready,
                    "revision": row["revision"] if row is not None else 0,
                }
            )
        return {"today": today.isoformat(), "days": days, "revision": settings["plan_revision"]}

    @staticmethod
    def _editable(decision: dict | None) -> None:
        if decision and decision["state"] in _FINAL | {"running"}:
            raise PlannerBlocked("Executed, terminal or running attendance cannot be edited")

    def save_plan(self, uid: int, changes: list[dict], *, expected_revision: int) -> dict:
        if not isinstance(changes, list) or not changes or len(changes) > 14:
            raise PlannerInvalid("Provide 1 to 14 date changes")
        today = self._now().date()
        validated = []
        seen = set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"date", "profile", "details"}:
                raise PlannerInvalid("Each change must contain date, profile and details")
            day = iso_day(change["date"])
            if not today.isoformat() <= day <= (today + timedelta(days=13)).isoformat():
                raise PlannerInvalid("Only the next 14 Singapore dates may be edited")
            if day in seen:
                raise PlannerInvalid("A date may appear only once in a plan save")
            seen.add(day)
            details, ready = validate_profile(change["profile"], change["details"])
            validated.append((day, change["profile"], details, ready))
        with self._transaction():
            settings = self._settings(uid)
            self._check(settings, expected_revision, "plan_revision")
            for day, _, _, _ in validated:
                self._editable(self.get_decision(uid, day))
            self._insert_settings(uid)
            next_revision = expected_revision + 1
            for day, profile, details, ready in validated:
                if profile is None:
                    self._db.execute("DELETE FROM attendance_plans WHERE uid=? AND day=?", (uid, day))
                else:
                    self._db.execute(
                        """INSERT INTO attendance_plans VALUES (?,?,?,?,?)
                           ON CONFLICT(uid,day) DO UPDATE SET profile=excluded.profile,
                           details_json=excluded.details_json,revision=excluded.revision""",
                        (uid, day, profile, _json(details), next_revision),
                    )
                decision = self.get_decision(uid, day)
                if decision:
                    state, reason = self._profile_state(profile, ready, settings["enabled"])
                    self._write_decision(
                        uid,
                        day,
                        profile=profile,
                        details_json=_json(details),
                        state=state,
                        reason=reason,
                        acknowledged=0,
                        consent_json=None,
                        original_json=None,
                        prompt_sent=0,
                        prompt_message_id=None,
                        execute_at=f"{day}T{settings['auto_time']}:00+08:00",
                    )
            self._db.execute(
                "UPDATE attendance_settings SET plan_revision=? WHERE uid=?",
                (next_revision, uid),
            )
        return self.get_plan(uid)

    @staticmethod
    def _profile_state(
        profile: str | None, ready: bool, enabled: bool = True
    ) -> tuple[str, str]:
        if profile == "skip":
            return "skipped", ""
        if profile is None:
            return "held", "unplanned"
        if not ready:
            return "held", "incomplete"
        # Automatic mode is authoritative: a complete profile never predicts an automatic
        # submission while the owner has paused it.
        return ("awaiting", "") if enabled else ("held", "disabled")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for field, target in (
            ("details_json", "details"),
            ("original_json", "original"),
            ("consent_json", "consent"),
        ):
            value = result.pop(field)
            result[target] = json.loads(value) if value is not None else None
        for field in ("reminder_sent", "prompt_sent", "acknowledged"):
            result[field] = bool(result[field])
        return result

    def get_decision(self, uid: int, day: str | date | None = None) -> dict | None:
        _uid(uid)
        return self._decode(
            self._db.execute(
                "SELECT * FROM daily_decisions WHERE uid=? AND day=?", (uid, self._day(day))
            ).fetchone()
        )

    def decisions(self, day: str | date | None = None) -> list[dict]:
        return [
            result
            for row in self._db.execute(
                "SELECT * FROM daily_decisions WHERE day=? ORDER BY uid", (self._day(day),)
            )
            if (result := self._decode(row)) is not None
        ]

    def users(self) -> list[int]:
        return [row["uid"] for row in self._db.execute("SELECT uid FROM attendance_settings")]

    def expire_decisions(self, before: date) -> list[dict]:
        """Mark overdue unexecuted decisions missed once, at or after the day rollover.

        Only prior-day rows still expecting automatic execution are terminalized:
        awaiting/ready predictions and verification holds. Terminal outcomes, running
        executions, user or incomplete holds and journal-bound attempts are preserved
        untouched, so a second call never re-expires or rewrites history.
        """
        cutoff = iso_day(before)
        expired: list[dict] = []
        with self._transaction():
            rows = self._db.execute(
                """SELECT * FROM daily_decisions WHERE day<?
                   AND (state IN ('awaiting','ready')
                        OR (state='held' AND reason='verification'))
                   ORDER BY day,uid""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                self._db.execute(
                    """UPDATE daily_decisions SET state='missed',reason='deadline',
                       revision=revision+1,updated_at=? WHERE uid=? AND day=?""",
                    (self._now().isoformat(), row["uid"], row["day"]),
                )
                decision = self.get_decision(row["uid"], row["day"])
                if decision is not None:
                    expired.append(decision)
        return expired

    def ensure_decision(self, uid: int, day: str | date | None = None) -> dict:
        day = self._day(day, today_only=True)
        with self._transaction():
            settings = self._settings(uid)
            row = self._db.execute(
                "SELECT * FROM attendance_plans WHERE uid=? AND day=?", (uid, day)
            ).fetchone()
            profile = row["profile"] if row is not None else None
            details = json.loads(row["details_json"]) if row is not None else {}
            _, ready = validate_profile(profile, details)
            state, reason = self._profile_state(profile, ready, settings["enabled"])
            observation = self.get_observation(uid, day)
            if observation and observation["records"]:
                state, reason = "recorded", "records_found"
            self._db.execute(
                """INSERT OR IGNORE INTO daily_decisions
                   (uid,day,profile,details_json,state,execute_at,records_digest,reason,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    uid,
                    day,
                    profile,
                    _json(details),
                    state,
                    f"{day}T{settings['auto_time']}:00+08:00",
                    observation["digest"] if observation else None,
                    reason,
                    self._now().isoformat(),
                ),
            )
        result = self.get_decision(uid, day)
        assert result is not None
        return result

    def _write_decision(self, uid: int, day: str, **changes) -> None:
        assignments = ",".join(f"{field}=?" for field in changes)
        self._db.execute(
            f"""UPDATE daily_decisions SET {assignments},revision=revision+1,updated_at=?
                WHERE uid=? AND day=?""",
            (*changes.values(), self._now().isoformat(), uid, day),
        )

    def _required(self, uid: int, day: str, expected_revision: int) -> dict:
        row = self.get_decision(uid, day)
        if row is None:
            raise PlannerBlocked("No daily decision exists")
        self._check(row, expected_revision)
        return row

    def update_decision(
        self,
        uid: int,
        day: str | date,
        *,
        expected_revision: int,
        state: str,
        reason: str = "",
        attempt_id: int | None = None,
        execute_at: str | None = None,
    ) -> dict:
        day = self._day(day)
        if reason not in _REASONS:
            raise PlannerInvalid("Unsupported decision reason")
        allowed = {
            "awaiting": {"ready", "held", "recorded", "missed"},
            "ready": {"held", "recorded", "missed"},
            "held": {"awaiting", "ready", "recorded", "missed"},
            # A claimed run that never reached the form is missed when its window closes.
            "running": {"confirmed", "failed", "unknown", "recorded", "held", "missed"},
        }
        with self._transaction():
            decision = self._required(uid, day, expected_revision)
            if state not in allowed.get(decision["state"], set()):
                raise PlannerBlocked("Invalid decision state transition")
            bound_id = attempt_id if attempt_id is not None else decision["attempt_id"]
            if state in {"confirmed", "unknown"} and bound_id is None:
                raise PlannerBlocked("Attempt journal evidence is required for this outcome")
            if state == "recorded":
                observation = self.get_observation(uid, day)
                if not observation or not observation["records"]:
                    raise PlannerBlocked("Positive owner evidence is required for recorded attendance")
            if decision["state"] == "running" and bound_id is not None:
                attempt = self.store.get_attempt(bound_id)
                permitted = {
                    "confirmed": {"confirmed"},
                    "unknown": {"unknown"},
                    "failed": {"failed"},
                    "recorded": {"failed"},
                    "held": {"failed"},
                    "missed": {"failed"},
                }
                if attempt["status"] not in permitted.get(state, set()):
                    raise PlannerBlocked("Attempt journal does not establish this safe outcome")
            if (
                decision["state"] == "held"
                and state in {"ready", "awaiting"}
                and decision["reason"] != "verification"
            ):
                raise PlannerBlocked("User or incomplete holds require explicit owner action")
            if state == "ready" and not validate_profile(decision["profile"], decision["details"])[1]:
                raise PlannerBlocked("Incomplete profiles cannot execute")
            changes: dict[str, Any] = {"state": state, "reason": reason}
            if attempt_id is not None:
                if not isinstance(attempt_id, int) or isinstance(attempt_id, bool):
                    raise PlannerInvalid("Attempt id must be an integer")
                self.store.get_attempt(attempt_id)
                changes["attempt_id"] = attempt_id
            if execute_at is not None:
                instant = datetime.fromisoformat(execute_at)
                if instant.tzinfo is None or instant.astimezone(SINGAPORE).date().isoformat() != day:
                    raise PlannerInvalid("Execution time must be timezone-aware and on this date")
                changes["execute_at"] = instant.isoformat()
            self._write_decision(uid, day, **changes)
        result = self.get_decision(uid, day)
        assert result is not None
        return result

    def bind_attempt(
        self, uid: int, day: str | date, attempt_id: int, *, expected_revision: int
    ) -> dict:
        """Bind the runner journal id immediately after preparation, before browser work."""
        day = self._day(day, today_only=True)
        if not isinstance(attempt_id, int) or isinstance(attempt_id, bool) or attempt_id <= 0:
            raise PlannerInvalid("Attempt id must be a positive integer")
        attempt = self.store.get_attempt(attempt_id)
        if attempt["attendance_date"] != day or attempt["mode"] != "submit":
            raise PlannerInvalid("Only today's real submission attempt may bind this decision")
        with self._transaction():
            row = self._required(uid, day, expected_revision)
            if row["state"] != "running" or row["attempt_id"] is not None:
                raise PlannerBlocked("Only an unbound running decision accepts an attempt")
            self._write_decision(uid, day, attempt_id=attempt_id)
        result = self.get_decision(uid, day)
        assert result is not None
        return result

    def claim_decision(self, uid: int, day: str | date, *, expected_revision: int) -> dict:
        day = self._day(day, today_only=True)
        with self._transaction():
            row = self._required(uid, day, expected_revision)
            settings = self._settings(uid)
            now = self._now()
            deadline = datetime.combine(
                now.date(), time.fromisoformat(self.attendance_deadline), SINGAPORE
            )
            if now >= deadline:
                raise PlannerBlocked("Attendance deadline has passed")
            if row["state"] not in {"awaiting", "ready"}:
                raise PlannerBlocked("Decision is not eligible for execution")
            if now < datetime.fromisoformat(row["execute_at"]):
                raise PlannerBlocked("Decision execution time has not arrived")
            if not settings["name_confirmed"]:
                raise PlannerBlocked("Attendance name must be confirmed before execution")
            if row["state"] == "awaiting" and not (settings["enabled"] and settings["name_confirmed"]):
                raise PlannerBlocked("Automatic attendance is disabled")
            if not validate_profile(row["profile"], row["details"])[1]:
                raise PlannerBlocked("Incomplete profiles cannot execute")
            observation = self.get_observation(uid, day)
            consent = row["consent"]
            if (
                observation
                and observation["records"]
                and not (
                    consent
                    and consent.get("authorized")
                    and not consent["consumed"]
                    and consent["records_digest"] == observation["digest"]
                )
            ):
                raise PlannerBlocked("Existing attendance suppresses automatic execution")
            self._write_decision(uid, day, state="running", reason="", attempt_id=None)
        result = self.get_decision(uid, day)
        assert result is not None
        return result

    def action(
        self,
        uid: int,
        action: str,
        *,
        expected_revision: int,
        profile: str | None = None,
        details: dict | None = None,
    ) -> dict:
        day = self._day(today_only=True)
        if action not in {"keep", "hold", "restore", "skip", "submit_now", "manual_submitted"}:
            raise PlannerInvalid("Unsupported daily action")
        with self._transaction():
            row = self._required(uid, day, expected_revision)
            if action != "keep" or row["state"] == "running":
                self._editable(row)
            changes: dict[str, Any] = {} if action == "keep" else {"consent_json": None}
            if action == "keep":
                changes["acknowledged"] = 1
            elif action in {"hold", "manual_submitted"}:
                if row["original"] is None:
                    changes["original_json"] = _json(
                        {
                            "profile": row["profile"],
                            "details": row["details"],
                            "execute_at": row["execute_at"],
                        }
                    )
                changes.update(state="held", reason="user_edit" if action == "hold" else action)
            elif action == "restore":
                original = row["original"]
                if row["state"] != "held" or not original:
                    raise PlannerBlocked("No held original prediction exists")
                _, ready = validate_profile(original["profile"], original["details"])
                state, reason = self._profile_state(
                    original["profile"], ready, self._settings(uid)["enabled"]
                )
                changes.update(
                    profile=original["profile"],
                    details_json=_json(original["details"]),
                    execute_at=original["execute_at"],
                    state=state,
                    reason=reason,
                    original_json=None,
                    acknowledged=0,
                )
            elif action == "skip":
                changes.update(
                    state="skipped", profile="skip", details_json="{}", reason="", acknowledged=1
                )
            elif action == "submit_now":
                chosen = row["profile"] if profile is None else profile
                chosen_details = row["details"] if profile is None and details is None else details
                chosen_details, ready = validate_profile(chosen, chosen_details)
                if not ready:
                    raise PlannerInvalid("Choose a complete executable profile")
                if self._now().time().replace(tzinfo=None) >= time.fromisoformat(
                    self.attendance_deadline
                ):
                    raise PlannerBlocked("Attendance deadline has passed")
                observation = self.get_observation(uid, day)
                if observation and observation["records"]:
                    raise PlannerBlocked("Existing attendance requires additional submission review")
                changes.update(
                    profile=chosen,
                    details_json=_json(chosen_details),
                    state="ready",
                    execute_at=self._now().isoformat(),
                    reason="",
                    acknowledged=1,
                )
            if action in {"restore", "skip", "submit_now"}:
                self._sync_prediction(uid, day, changes["profile"], json.loads(changes["details_json"]))
            self._write_decision(uid, day, **changes)
        result = self.get_decision(uid, day)
        assert result is not None
        return result

    def _sync_prediction(self, uid: int, day: str, profile: str | None, details: dict) -> None:
        self._insert_settings(uid)
        self._db.execute(
            "UPDATE attendance_settings SET plan_revision=plan_revision+1 WHERE uid=?", (uid,)
        )
        if profile is None:
            self._db.execute("DELETE FROM attendance_plans WHERE uid=? AND day=?", (uid, day))
        else:
            self._db.execute(
                """INSERT INTO attendance_plans(uid,day,profile,details_json,revision)
                   VALUES (?,?,?,?,(SELECT plan_revision FROM attendance_settings WHERE uid=?))
                   ON CONFLICT(uid,day) DO UPDATE SET profile=excluded.profile,
                   details_json=excluded.details_json,revision=excluded.revision""",
                (uid, day, profile, _json(details), uid),
            )

    def mark_prompt(
        self,
        uid: int,
        day: str | date,
        *,
        expected_revision: int,
        message_id: int,
    ) -> dict:
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise PlannerInvalid("Message id must be an integer")
        return self._mark(uid, day, expected_revision, prompt_sent=1, prompt_message_id=message_id)

    def mark_reminder(
        self,
        uid: int,
        day: str | date,
        *,
        expected_revision: int,
        message_id: int,
    ) -> dict:
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise PlannerInvalid("Message id must be an integer")
        # Promote the prompt pointer to the delivered reminder so its buttons stay live and
        # the superseded prompt keyboard goes stale. No revision bump (server bookkeeping).
        return self._mark(
            uid, day, expected_revision, reminder_sent=1, prompt_message_id=message_id
        )

    def _mark(self, uid: int, day: str | date, expected_revision: int, **changes) -> dict:
        day = self._day(day, today_only=True)
        with self._transaction():
            row = self._required(uid, day, expected_revision)
            if "reminder_sent" in changes and (
                row["state"] != "awaiting" or row["acknowledged"] or row["reminder_sent"]
            ):
                raise PlannerBlocked("Decision is not eligible for a reminder")
            # Server-internal notification bookkeeping (prompt/reminder delivery) must not
            # bump the owner-facing revision: the keyboard was built from that revision and
            # an exact-match button would otherwise be stale on arrival. Real owner
            # mutations still bump via _write_decision.
            assignments = ",".join(f"{field}=?" for field in changes)
            self._db.execute(
                f"""UPDATE daily_decisions SET {assignments},updated_at=?
                    WHERE uid=? AND day=?""",
                (*changes.values(), self._now().isoformat(), uid, day),
            )
        result = self.get_decision(uid, day)
        assert result is not None
        return result

    def get_observation(self, uid: int, day: str | date) -> dict | None:
        _uid(uid)
        row = self._db.execute(
            "SELECT * FROM attendance_observations WHERE uid=? AND day=?",
            (uid, self._day(day)),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["records"] = json.loads(result.pop("records_json"))
        return result

    def merge_observation(self, uid: int, day: str | date, check) -> dict | None:
        """Accept only the selected-owner RecordCheck, never a CSV snapshot."""
        _uid(uid)
        day = self._day(day)
        if check.status not in {"found", "not_found", "unavailable"}:
            raise PlannerInvalid("Unsupported record check status")
        if check.status == "unavailable":
            return self.get_observation(uid, day)
        records = [
            record.to_dict() if hasattr(record, "to_dict") else record for record in check.records
        ]
        if not all(
            isinstance(record, dict) and set(record) == {"timestamp", "status", "details"}
            for record in records
        ):
            raise PlannerInvalid("Observation records must be selected-owner display records")
        if not isinstance(check.digest, str) or re.fullmatch(r"[0-9a-f]{64}", check.digest) is None:
            raise PlannerInvalid("Available source check must carry a canonical digest")
        if (check.status == "found") != bool(records):
            raise PlannerInvalid("Record status disagrees with selected-owner rows")
        checked_at = (
            check.checked_at.isoformat() if isinstance(check.checked_at, datetime) else check.checked_at
        )
        if not isinstance(checked_at, str):
            raise PlannerInvalid("Record check time must be an ISO timestamp")
        checked = datetime.fromisoformat(checked_at)
        if checked.tzinfo is None:
            raise PlannerInvalid("Record check time must be timezone-aware")
        with self._transaction():
            old = self.get_observation(uid, day)
            counts = Counter(_json(record) for record in records)
            if old:
                counts |= Counter(_json(record) for record in old["records"])
            merged = [json.loads(value) for value in sorted(counts) for _ in range(counts[value])]
            # Full source-row hashes may include redacted values. Retention concerns
            # blocker evidence only; consent always binds the latest source digest.
            digest = check.digest
            self._db.execute(
                """INSERT INTO attendance_observations VALUES (?,?,?,?,?)
                   ON CONFLICT(uid,day) DO UPDATE SET digest=excluded.digest,
                   records_json=excluded.records_json,checked_at=excluded.checked_at""",
                (uid, day, digest, _json(merged), checked_at),
            )
            decision = self.get_decision(uid, day)
            if decision:
                changes: dict[str, Any] = {"records_digest": digest}
                if decision["records_digest"] != digest:
                    changes["consent_json"] = None
                authorized = (
                    decision["consent"]
                    and decision["consent"].get("authorized")
                    and not decision["consent"]["consumed"]
                    and decision["consent"]["records_digest"] == digest
                )
                if merged and decision["state"] in {"awaiting", "held", "ready"} and not authorized:
                    changes.update(state="recorded", reason="records_found")
                if any(decision.get(key) != value for key, value in changes.items()):
                    self._write_decision(uid, day, **changes)
        return self.get_observation(uid, day)

    @staticmethod
    def _consent_inputs(answer_hash: str, local_attempt_ids: list[int]) -> list[int]:
        if not isinstance(answer_hash, str) or re.fullmatch(r"[0-9a-f]{64}", answer_hash) is None:
            raise PlannerInvalid("Canonical answer hash must be SHA-256")
        if not isinstance(local_attempt_ids, list) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in local_attempt_ids
        ):
            raise PlannerInvalid("Reviewed local attempts must be positive integer ids")
        return sorted(set(local_attempt_ids))

    def review_consent(
        self,
        uid: int,
        *,
        expected_revision: int,
        answer_hash: str,
        local_attempt_ids: list[int],
        profile: str | None = None,
        details: dict | None = None,
    ) -> dict:
        day = self._day(today_only=True)
        ids = self._consent_inputs(answer_hash, local_attempt_ids)
        with self._transaction():
            row = self._required(uid, day, expected_revision)
            if row["state"] == "running":
                raise PlannerBlocked("Cannot review another submission during a run")
            chosen = row["profile"] if profile is None else profile
            chosen_details = row["details"] if profile is None and details is None else details
            chosen_details, ready = validate_profile(chosen, chosen_details)
            if not ready:
                raise PlannerInvalid("Additional submission needs a complete executable profile")
            observation = self.get_observation(uid, day)
            if observation is None:
                raise PlannerBlocked("Fresh external evidence is required before review")
            for attempt_id in ids:
                attempt = self.store.get_attempt(attempt_id)
                if attempt["attendance_date"] != day or attempt["mode"] != "submit":
                    raise PlannerInvalid("Reviewed attempts must be today's real submissions")
            age = (self._now() - datetime.fromisoformat(observation["checked_at"])).total_seconds()
            if not 0 <= age <= 30:
                raise PlannerBlocked("Refresh external evidence before additional submission review")
            digest = secrets.token_hex(32)
            consent = {
                "date": day,
                "answer_hash": answer_hash,
                "local_attempt_ids": ids,
                "records_digest": observation["digest"],
                "consent_digest": digest,
                "authorized": False,
                "consumed": False,
            }
            self._write_decision(
                uid,
                day,
                profile=chosen,
                details_json=_json(chosen_details),
                consent_json=_json(consent),
            )
        result = self.get_decision(uid, day)
        assert result is not None
        return {"consent_digest": digest, "revision": result["revision"], "consent": consent}

    def consume_consent(
        self,
        uid: int,
        *,
        expected_revision: int,
        consent_digest: str,
        answer_hash: str,
        local_attempt_ids: list[int],
    ) -> dict:
        day = self._day(today_only=True)
        ids = self._consent_inputs(answer_hash, local_attempt_ids)
        if not isinstance(consent_digest, str):
            raise PlannerInvalid("Consent digest must be text")
        with self._transaction():
            row = self._required(uid, day, expected_revision)
            consent = row["consent"]
            observation = self.get_observation(uid, day)
            if (
                row["state"] == "running"
                or not consent
                or consent["consumed"]
                or consent.get("authorized")
                or not secrets.compare_digest(consent["consent_digest"], consent_digest)
                or consent["date"] != day
                or consent["answer_hash"] != answer_hash
                or consent["local_attempt_ids"] != ids
                or observation is None
                or consent["records_digest"] != observation["digest"]
            ):
                raise PlannerBlocked("Evidence or answers changed; review again")
            if self._now().time().replace(tzinfo=None) >= time.fromisoformat(self.attendance_deadline):
                raise PlannerBlocked("Attendance deadline has passed")
            # Confirmation authorizes one attempt; runner consumes atomically with
            # attempt preparation, not at this UI confirmation boundary.
            consent["authorized"] = True
            self._write_decision(
                uid,
                day,
                consent_json=_json(consent),
                state="ready",
                execute_at=self._now().isoformat(),
                reason="",
                acknowledged=1,
                attempt_id=None,
            )
        result = self.get_decision(uid, day)
        assert result is not None
        return result
