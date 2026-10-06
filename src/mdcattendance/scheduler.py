"""Durable morning decisions: one owner/day window, claimed and executed exactly once."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from .attendance import Answers
from .config import Config
from .otp import OtpProvider
from .records import RecordCheck
from .runner import AttendanceRunner, BusyRun, DuplicateRun, answer_hash
from .schedules import (
    Planner,
    PlannerBlocked,
    PlannerConflict,
    PlannerInvalid,
    validate_profile,
)
from .storage import SINGAPORE, AdditionalSubmissionConsent, StateStore

log = logging.getLogger("mdcattendance.scheduler")
_TICK_SECONDS = 30
_BUSY_RETRY_SECONDS = 1
_NOTIFICATION_TIMEOUT = 10
_SOURCE_TIMEOUT_SECONDS = 30
# UI reads reuse the last owner check for this long instead of forcing a source fetch.
_SOURCE_CACHE_SECONDS = 30
_VERIFICATION_RETRY_SECONDS = 60
_REMINDER_LEAD = timedelta(minutes=5)
_MIN_FUTURE_WEEKDAYS = 3
# Owner-only recorded prompt: render the latest evidence compactly so a long source cell
# can never push the Telegram message past its delivery limit.
_MAX_PROMPT_RECORDS = 3
_MAX_FIELD_CHARS = 80
_MAX_SUMMARY_CHARS = 1000
_LABELS = {"normal": "Present (IS)", "wfh": "WFH", "ma": "MA", "skip": "Skip"}
_FINAL_STATES = {"recorded", "skipped", "confirmed", "failed", "unknown", "missed"}
_EVIDENCE_ACTIONS = {"additional_review", "additional_confirm", "submit_now", "manual_submitted"}

Prepare = Callable[[int, str, dict, datetime], Awaitable[tuple[Config, OtpProvider, Answers]]]
Notify = Callable[..., Awaitable[int | None]]


class Scheduler:
    """Clock-driven durable workflow shared by Telegram and the Mini App API.

    Owner mutations go through the same :class:`Planner` primitives; the scheduler
    only owns time, notification retries and the single-browser dispatch queue.
    """

    def __init__(
        self,
        store: StateStore,
        runner: AttendanceRunner,
        base_cfg: Config,
        prepare: Prepare,
        notify: Notify,
        *,
        users: Callable[[], Mapping[int, object]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.base_cfg = base_cfg
        self.prepare = prepare
        self.notify = notify
        self.clock = clock or (lambda: datetime.now(SINGAPORE))
        self._users = users or (lambda: {})
        self._planner: Planner | None = None
        self._task: asyncio.Task[None] | None = None
        self._executions: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._tick_lock = asyncio.Lock()
        self._lock_fd: int | None = None
        self._recovered = False
        self._stopping = False
        self._verified_at: dict[tuple[int, str], datetime] = {}
        self._checks: dict[tuple[int, str], tuple[float, RecordCheck]] = {}
        self._warned: set[tuple[int, str, str]] = set()
        self._expired_through: date | None = None

    @property
    def planner(self) -> Planner:
        return self._planner_or_raise()

    def _planner_or_raise(self) -> Planner:
        if self._planner is None:
            raise RuntimeError("Scheduler planner is not available until start or recovery")
        return self._planner

    def _now(self) -> datetime:
        instant = self.clock()
        if (
            not isinstance(instant, datetime)
            or instant.tzinfo is None
            or instant.utcoffset() is None
        ):
            raise ValueError("Scheduler clock must be timezone-aware")
        return instant.astimezone(SINGAPORE)

    def _instant(self, now: datetime | None) -> datetime:
        if now is None:
            return self._now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Scheduler time must be timezone-aware")
        return now.astimezone(SINGAPORE)

    def _allowlist(self) -> Mapping[int, object]:
        try:
            return self._users() or {}
        except Exception:
            log.error("Attendance allowlist could not be read; automatic execution suspended")
            return {}

    def otp_ready(self, uid: int) -> bool:
        """Automatic mode needs an enabled receiver and this owner's provisioned token."""
        if not self.base_cfg.otp_http_enabled or not self.base_cfg.otp_http_token:
            return False
        user = self._allowlist().get(uid)
        return bool(user is not None and getattr(user, "otp_token", ""))

    def _acquire_lifecycle_lock(self) -> None:
        if self._lock_fd is not None:
            return
        path = str(Path(self.store.state_dir) / "scheduler.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BusyRun("Another scheduler owns the state directory") from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _release_lifecycle_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _interrupted_decisions(self) -> list[tuple[int, str]]:
        table = self.store._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='daily_decisions' AND type='table'"
        ).fetchone()
        if table is None:
            return []
        return [
            (row["uid"], row["day"])
            for row in self.store._db.execute(
                "SELECT uid,day FROM daily_decisions WHERE state='running'"
            )
        ]

    async def recover(self) -> bool:
        """Runner journal recovery first, then the shared planner. Idempotent."""
        if self._recovered:
            return True
        self._acquire_lifecycle_lock()
        interrupted = self._interrupted_decisions()
        if not self.runner.recover():
            return False
        self._planner = Planner(
            self.store, self.base_cfg.attendance_deadline, clock=self.clock
        )
        self._recovered = True
        for uid, day in interrupted:
            await self._notify(
                uid,
                f"Attendance for {day} was interrupted by a restart; it will not be retried "
                "automatically.",
                decision=self._planner.get_decision(uid, day),
            )
        await self._expire_prior_decisions(self._now().date())
        return True

    async def _expire_prior_decisions(self, today: date) -> None:
        """Close every prior-day unfinished decision exactly once, notifying each owner.

        A restart or a midnight rollover must not leave yesterday's awaiting/ready rows
        (or the bounded verification hold) dangling: each becomes ``missed`` with the
        deadline reason, journal history is untouched, and the owner is told once.
        Running attempts and confirmed/unknown outcomes are never replayed or altered.
        """
        if self._expired_through is not None and today <= self._expired_through:
            return
        self._expired_through = today
        for row in self._planner_or_raise().expire_decisions(before=today):
            await self._notify(
                row["uid"],
                f"Attendance for {row['day']} expired without being completed; no automatic retry.",
                decision=row,
            )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._acquire_lifecycle_lock()
        await self.recover()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        loop_handle = self._task
        self._task = None
        executions = dict(self._executions)
        tasks: list[asyncio.Task[None]] = [*executions.values()]
        if loop_handle is not None:
            tasks.append(loop_handle)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first instruction cannot run its finally block.
        for uid, day in executions:
            await self.settle_from_journal(uid, day)
        self._executions.clear()
        self._release_lifecycle_lock()
        self._planner = None
        self._recovered = False

    async def cancel(self, uid: int) -> bool:
        """Cancel this owner's work, including the queue before an OTP prompt."""
        owned = [(day, task) for (owner, day), task in self._executions.items() if owner == uid]
        for _day, task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*(task for _day, task in owned), return_exceptions=True)
            for day, task in owned:
                if self._executions.get((uid, day)) is task:
                    # A coroutine cancelled before starting cannot run its finally.
                    await self.settle_from_journal(uid, day)
                    if self._executions.get((uid, day)) is task:
                        self._executions.pop((uid, day))
            return True
        instant = self._now()
        planner = self._planner_or_raise()
        decision = planner.get_decision(uid, instant.date().isoformat())
        if decision is not None and (
            decision["state"] == "ready"
            or (decision["state"] == "held" and decision["reason"] == "verification")
            or (decision["state"] == "awaiting"
                and datetime.fromisoformat(decision["execute_at"]) <= instant)
        ):
            planner.action(uid, "hold", expected_revision=decision["revision"])
            return True
        return False

    def replan(self) -> None:
        """Settings and plans are re-read every tick; nothing is cached here."""
        return None

    async def save_plan(
        self, uid: int, changes: list[dict], *, expected_revision: int
    ) -> dict:
        """Owner plan save shared by the Mini App API and Telegram launchers.

        Delegates the atomic write to the planner, then refreshes today's already
        delivered morning prompt when this save replaced its revision, so the sent
        keyboard cannot stay stale. A failed notification never fails the save: the
        planner reset the prompt bookkeeping, so the normal tick retries until the
        deadline.
        """
        planner = self._planner_or_raise()
        today = self._now().date().isoformat()
        # An ordinary plan edit must never overwrite today's real local outcome, even
        # when the attempt was never bound to a decision (e.g. a CLI run). Only a
        # confirmed/unknown terminal attempt blocks; failures stay editable.
        if any(
            isinstance(change, dict) and str(change.get("date", ""))[:10] == today
            for change in (changes or [])
        ):
            user = self._allowlist().get(uid)
            if user is not None:
                rows = self.store.submission_attempts(
                    getattr(user, "singpass_id", ""), self.base_cfg.form_url, today
                )
                if any(row["status"] in {"confirmed", "unknown"} for row in rows):
                    raise PlannerBlocked(
                        "Today's attendance outcome cannot be replaced by a plan edit"
                    )
        before = planner.get_decision(uid)
        prompted = bool(before and before["prompt_sent"])
        plan = planner.save_plan(uid, changes, expected_revision=expected_revision)
        after = planner.get_decision(uid)
        if (
            prompted
            and before is not None
            and after is not None
            and after["revision"] != before["revision"]
            and self._now() < self._deadline(after["day"])
        ):
            await self._prompt(uid, after["day"], planner.get_settings(uid))
        return plan

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never include exception or update dumps; they may hold personal input.
                log.error("Scheduled attendance tick failed; no automatic submission performed")
            await asyncio.sleep(_TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        if self._stopping:
            return
        instant = self._instant(now)
        async with self._tick_lock:
            if not self._recovered and not await self.recover():
                return
            await self._expire_prior_decisions(instant.date())
            planner = self._planner_or_raise()
            deadline = self._at(instant.date(), self.base_cfg.attendance_deadline)
            for uid in planner.users():
                if self._stopping:
                    return
                try:
                    await self._tick_user(uid, instant, deadline)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.error(
                        "Scheduled attendance tick failed for one owner; "
                        "no automatic submission performed"
                    )

    @staticmethod
    def _at(day: date, hhmm: str) -> datetime:
        return datetime.combine(day, time.fromisoformat(hhmm), SINGAPORE)

    def _deadline(self, day: str | date) -> datetime:
        value = day if isinstance(day, date) else date.fromisoformat(day)
        return self._at(value, self.base_cfg.attendance_deadline)

    def _remaining(self, deadline: datetime) -> float:
        return (deadline - self._now()).total_seconds()

    async def _tick_user(self, uid: int, instant: datetime, deadline: datetime) -> None:
        planner = self._planner_or_raise()
        day = instant.date().isoformat()
        settings = planner.get_settings(uid)
        decision = planner.ensure_decision(uid)
        state = decision["state"]
        if instant >= deadline:
            await self._close_window(uid, day, decision)
            return
        if state in _FINAL_STATES or state == "running":
            if state == "running" and (uid, day) not in self._executions:
                await self.settle_from_journal(uid, day)
            return
        enabled = settings["enabled"]
        if (
            enabled
            and not decision["prompt_sent"]
            and instant >= self._at(instant.date(), settings["prompt_time"])
        ):
            await self._prompt(uid, day, settings)
            decision = planner.get_decision(uid, day) or decision
        auto_at = self._at(instant.date(), settings["auto_time"])
        if (
            enabled
            and decision["state"] == "awaiting"
            and not decision["acknowledged"]
            and not decision["reminder_sent"]
            and auto_at - _REMINDER_LEAD <= instant < auto_at
        ):
            await self._remind(uid, day, settings, decision)
            decision = planner.get_decision(uid, day) or decision
        user = self._allowlist().get(uid)
        if user is None:
            if decision["state"] == "awaiting" and instant >= auto_at:
                await self._hold(
                    uid,
                    day,
                    decision,
                    "otp_unavailable",
                    "No provisioned credentials are available for this owner; automatic "
                    "submission is suspended.",
                )
            return
        await self._maybe_execute(uid, day, instant, settings, decision, enabled, user)

    async def _close_window(self, uid: int, day: str, decision: dict) -> None:
        eligible = decision["state"] in {"awaiting", "ready"} or (
            decision["state"] == "held" and decision["reason"] == "verification"
        )
        if not eligible:
            return
        try:
            self._planner_or_raise().update_decision(
                uid,
                day,
                expected_revision=decision["revision"],
                state="missed",
                reason="deadline",
            )
        except (PlannerConflict, PlannerBlocked):
            return
        await self._notify(
            uid,
            f"Attendance for {day} was not submitted before the deadline; no automatic retry.",
            decision=self._planner_or_raise().get_decision(uid, day),
        )

    async def _maybe_execute(
        self,
        uid: int,
        day: str,
        instant: datetime,
        settings: dict,
        decision: dict,
        enabled: bool,
        user: object,
    ) -> None:
        planner = self._planner_or_raise()
        state, reason = decision["state"], decision["reason"]
        if state == "held" and reason == "verification":
            auto_at = self._at(instant.date(), settings["auto_time"])
            if instant < auto_at or not self._verification_due(uid, day, instant):
                return
            try:
                decision = planner.update_decision(
                    uid,
                    day,
                    expected_revision=decision["revision"],
                    state="awaiting",
                    reason="",
                )
            except (PlannerConflict, PlannerBlocked):
                return
            state = decision["state"]
        if state not in {"awaiting", "ready"}:
            return
        if state == "awaiting" and not enabled:
            return
        if instant < datetime.fromisoformat(decision["execute_at"]):
            return
        if not settings["name_confirmed"]:
            await self._hold(
                uid,
                day,
                decision,
                "onboarding",
                "Confirm the uppercase attendance name in the planner; automatic submission is "
                "suspended until then.",
            )
            return
        if state == "awaiting" and not self.otp_ready(uid):
            await self._hold(
                uid,
                day,
                decision,
                "otp_unavailable",
                "Phone OTP delivery is not available; automatic submission is suspended. "
                "Manual submission is still possible.",
            )
            return
        check = await self._refresh_records(uid)
        decision = planner.get_decision(uid, day) or decision
        if decision["state"] == "recorded":
            await self._note_once(
                uid,
                day,
                "records",
                f"An existing submission for {day} was found at the source; automatic submission "
                "is suppressed. Open the planner to review an explicit additional submission.",
                decision,
            )
            return
        if check is None or check.status == "unavailable":
            await self._hold(
                uid,
                day,
                decision,
                "verification",
                f"The attendance source could not be verified for {day}; rechecking every minute "
                "until the deadline. No submission has been attempted.",
            )
            return
        try:
            claimed = planner.claim_decision(uid, day, expected_revision=decision["revision"])
        except (PlannerConflict, PlannerBlocked):
            return
        self._executions[(uid, day)] = asyncio.create_task(
            self._execute(uid, day, claimed, self._deadline(day), user)
        )

    async def _prompt(self, uid: int, day: str, settings: dict) -> None:
        planner = self._planner_or_raise()
        check = await self._refresh_records(uid)
        decision = planner.get_decision(uid, day)
        if decision is None:
            return
        observation = planner.get_observation(uid, day)
        text = self._prompt_text(day, settings, decision, observation, check)
        # Only the morning prompt (and its reminder) carries live decision controls;
        # the private marker is never persisted and never leaves this notification.
        message_id = await self._notify(uid, text, decision={**decision, "_controls": True})
        if message_id is None:
            return
        try:
            planner.mark_prompt(
                uid, day, expected_revision=decision["revision"], message_id=message_id
            )
        except (PlannerConflict, PlannerBlocked):
            return

    def _prompt_text(
        self,
        day: str,
        settings: dict,
        decision: dict,
        observation: dict | None,
        check: RecordCheck | None = None,
    ) -> str:
        state, reason, profile = decision["state"], decision["reason"], decision["profile"]
        label = _LABELS.get(profile or "", profile or "")
        auto_time = settings["auto_time"]
        lines = [f"Attendance for {day} (Asia/Singapore)."]
        if state == "recorded":
            lines.append(self._recorded_prompt(check, observation))
        elif profile == "skip" or state == "skipped":
            lines.append("Today is marked Skip.")
        elif profile is None:
            lines.append(
                "No plan for today. Open the planner to choose one; there is no automatic fallback."
            )
        elif reason == "incomplete":
            lines.append(
                f"Planned {label} is incomplete, so nothing will be submitted automatically. "
                "Open the planner to finish it."
            )
        elif state == "held":
            lines.append(self._held_prompt(label, reason))
        elif state == "ready":
            lines.append(
                f"{label} is queued to submit now once the single browser slot is free; stay "
                "available for the OTP prompt."
            )
        else:
            lines.append(
                f"Planned {label}; automatic submission at {auto_time} if you do not respond. "
                "Use the buttons below to keep it, change today, submit now or skip."
            )
        if state != "recorded":
            if (check is not None and check.status == "found") or (
                observation is not None and observation["records"]
            ):
                lines.append(
                    "An existing submission is already recorded for today; automatic submission "
                    "is suppressed unless you explicitly review a different attendance."
                )
            elif observation is not None:
                lines.append(
                    f"Source checked at {observation['checked_at']} (source updates may lag)."
                )
            else:
                lines.append("Source not verified yet (source updates may lag).")
        lines.append(f"The attendance window closes at {self.base_cfg.attendance_deadline}.")
        if self._low_future_plan(uid=decision["uid"]):
            lines.append(
                "Fewer than three future weekdays are planned in the next two weeks; open the "
                "planner to fill them."
            )
        return "\n".join(lines)

    @staticmethod
    def _held_prompt(label: str, reason: str) -> str:
        """Truthful paused/held wording: never promise a run that cannot happen."""
        if reason == "user_edit":
            return (
                "Today's automatic submission is paused. Restore or save the plan to resume it; "
                "nothing runs automatically while it stays paused."
            )
        if reason == "disabled":
            return (
                "Automatic attendance is off, so this complete plan will not submit by itself. "
                "Enable it in the planner, or use Submit Now for a one-off run."
            )
        if reason == "onboarding":
            return (
                "Automatic submission is suspended until you confirm your attendance name "
                "(send /name)."
            )
        if reason == "otp_unavailable":
            return (
                "Automatic submission is suspended because phone OTP delivery is not available; "
                "submit manually with /attend or /dry_run."
            )
        if reason == "verification":
            return (
                "Automatic submission is paused while the attendance source is verified; it "
                "rechecks until the deadline and has attempted nothing yet."
            )
        if reason == "manual_submitted":
            return (
                "You marked today as manually submitted; review the planner if the outcome needs "
                "correcting."
            )
        return f"Automatic submission for {label or 'today'} is paused (reason: {reason})."

    @staticmethod
    def _clip(value: Any) -> str:
        text = " ".join(str(value).split())
        if len(text) > _MAX_FIELD_CHARS:
            text = text[: _MAX_FIELD_CHARS - 1] + "…"
        return text

    @classmethod
    def _entry_summary(cls, record: Any) -> str:
        """One display row from a RecordCheck row or its retained observation dict."""
        if isinstance(record, Mapping):
            timestamp, status, details = record["timestamp"], record["status"], record["details"]
        else:
            timestamp, status, details = record.timestamp, record.status, record.details
        when = (
            datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
        ).astimezone(SINGAPORE)
        pairs = details.items() if isinstance(details, Mapping) else details
        rendered = ", ".join(f"{cls._clip(key)}={cls._clip(value)}" for key, value in pairs)
        parts = [f"{when:%Y-%m-%d %H:%M}", cls._clip(status)]
        if rendered:
            parts.append(rendered)
        return " ".join(parts)

    @classmethod
    def _records_summary(cls, records) -> str:
        rendered = "; ".join(cls._entry_summary(record) for record in records)
        if len(rendered) > _MAX_SUMMARY_CHARS:
            rendered = rendered[: _MAX_SUMMARY_CHARS - 1] + "…"
        return rendered

    @classmethod
    def _recorded_prompt(cls, check: RecordCheck | None, observation: dict | None) -> str:
        if check is not None and check.status == "found":
            rows = cls._records_summary(check.records[:_MAX_PROMPT_RECORDS])
            stamp = f"checked {check.checked_at.astimezone(SINGAPORE):%Y-%m-%d %H:%M} SGT"
            return (
                f"Existing attendance is already recorded for today ({rows}; {stamp}; source "
                "updates may lag). Automatic submission is suppressed unless you explicitly "
                "review a different attendance."
            )
        if observation is not None and observation["records"]:
            rows = cls._records_summary(observation["records"][:_MAX_PROMPT_RECORDS])
            when = datetime.fromisoformat(observation["checked_at"]).astimezone(SINGAPORE)
            if check is None:
                current = "not verified"
            elif check.status == "not_found":
                current = "found no matching entry"
            else:
                current = f"{check.status}"
            return (
                "Retained evidence still records an existing attendance for today "
                f"({rows}; observed {when:%Y-%m-%d %H:%M} SGT; current source check: {current}; "
                "source updates may lag — an absent or unavailable source check does not clear "
                "this). Automatic submission is suppressed unless you explicitly review a "
                "different attendance."
            )
        if observation is not None:
            return (
                "An existing submission is already recorded for today (last checked "
                f"{observation['checked_at']}; source updates may lag). Automatic submission is "
                "suppressed unless you explicitly review a different attendance."
            )
        return (
            "An existing submission is already recorded for today; automatic submission is "
            "suppressed unless you explicitly review a different attendance."
        )

    def _low_future_plan(self, *, uid: int) -> bool:
        days = self._planner_or_raise().get_plan(uid)["days"]
        planned = sum(
            1
            for row in days[1:]
            if row["ready"]
            and row["profile"] != "skip"
            and date.fromisoformat(row["date"]).weekday() < 5
        )
        return planned < _MIN_FUTURE_WEEKDAYS

    async def _remind(self, uid: int, day: str, settings: dict, decision: dict) -> None:
        planner = self._planner_or_raise()
        # Deliver first: a failed send must not consume the one reminder in the window,
        # so the next eligible tick retries.
        message_id = await self._notify(
            uid,
            f"Automatic {_LABELS.get(decision['profile'], '')} attendance for {day} runs at "
            f"{settings['auto_time']} (about five minutes). Use Keep, Change Today, Submit Now or "
            "Skip on this message; open the planner for anything else.",
            decision={**decision, "_controls": True},
        )
        if message_id is None:
            return
        try:
            # Promotes the delivered reminder to the live prompt message id without a
            # revision bump, so these buttons work and the earlier prompt's go stale.
            planner.mark_reminder(
                uid, day, expected_revision=decision["revision"], message_id=message_id
            )
        except (PlannerConflict, PlannerBlocked):
            return

    async def _hold(self, uid: int, day: str, decision: dict, reason: str, text: str) -> None:
        planner = self._planner_or_raise()
        if decision["state"] == "held" and decision["reason"] == reason:
            await self._note_once(uid, day, reason, text, decision)
            return
        try:
            held = planner.update_decision(
                uid, day, expected_revision=decision["revision"], state="held", reason=reason
            )
        except (PlannerConflict, PlannerBlocked):
            return
        await self._note_once(uid, day, reason, text, held)

    async def _note_once(self, uid: int, day: str, key: str, text: str, decision: dict) -> None:
        marker = (uid, day, key)
        if marker in self._warned:
            return
        self._warned.add(marker)
        await self._notify(uid, text, decision=decision)

    def _verification_due(self, uid: int, day: str, instant: datetime) -> bool:
        last = self._verified_at.get((uid, day))
        return last is None or (instant - last).total_seconds() >= _VERIFICATION_RETRY_SECONDS

    async def _refresh_records(
        self,
        uid: int,
        *,
        expected_revision: int | None = None,
        fresh: bool = True,
    ) -> RecordCheck | None:
        """Selected-owner lookup merged into durable observations.

        ``expected_revision`` is validated immediately before the merge: an owner
        change during the network await invalidates the caller's revision even when
        the owner restored the same values (ABA), so the caller re-reviews instead of
        rebasing onto a revision its owner never saw. Only the evidence bump that
        ``merge_observation`` performs itself is allowed through.
        """
        planner = self._planner_or_raise()
        day = self._now().date().isoformat()
        self._verified_at[(uid, day)] = self._now()
        settings = planner.get_settings(uid)
        user = self._allowlist().get(uid)
        name = settings["attendance_name"]
        records = getattr(self.runner, "records", None)
        if not settings["name_confirmed"] or not name or user is None or records is None:
            return None
        try:
            async with asyncio.timeout(_SOURCE_TIMEOUT_SECONDS):
                check = await records.lookup(
                    name, getattr(user, "department", ""), date.fromisoformat(day), fresh=fresh
                )
        except Exception:
            log.warning("Attendance source check failed for one owner; treating as unavailable")
            check = None
        if expected_revision is not None:
            current = planner.get_decision(uid, day)
            if current is None or current["revision"] != expected_revision:
                raise PlannerConflict("Decision changed while refreshing evidence; review again")
        if check is None:
            return None
        try:
            planner.merge_observation(uid, day, check)
        except PlannerInvalid:
            log.error("Attendance source check was rejected by the durable observation store")
            return None
        self._checks[(uid, day)] = (monotonic(), check)
        return check

    def _recent_check(self, uid: int, day: str) -> RecordCheck | None:
        """The last owner check while it is still fresh enough to render without a fetch."""
        remembered = self._checks.get((uid, day))
        if remembered is not None and monotonic() - remembered[0] < _SOURCE_CACHE_SECONDS:
            return remembered[1]
        return None

    async def _execute(
        self, uid: int, day: str, decision: dict, deadline: datetime, user: object
    ) -> None:
        planner = self._planner_or_raise()
        try:
            settings = planner.get_settings(uid)
            cfg: Config | None = None
            otp: OtpProvider | None = None
            answers: Answers | None = None
            consent_context: AdditionalSubmissionConsent | None = None
            while True:
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    await self.settle(uid, day, status="missed")
                    return
                if cfg is None:
                    # One preparation per claimed decision; a busy owner session is queued.
                    try:
                        async with asyncio.timeout(remaining):
                            cfg, otp, answers = await self.prepare(
                                uid, decision["profile"], decision["details"], deadline
                            )
                    except BusyRun:
                        await asyncio.sleep(min(_BUSY_RETRY_SECONDS, remaining))
                        continue
                    # The authorised context is rebuilt from the claimed durable row,
                    # never passed to the runner as a bare digest.
                    consent_row = decision["consent"]
                    if (
                        consent_row
                        and consent_row.get("authorized")
                        and not consent_row["consumed"]
                    ):
                        consent_context = AdditionalSubmissionConsent(
                            date=date.fromisoformat(consent_row["date"]),
                            answer_hash=consent_row["answer_hash"],
                            records_digest=consent_row["records_digest"],
                            local_attempt_ids=tuple(consent_row["local_attempt_ids"]),
                            consent_digest=consent_row["consent_digest"],
                            decision_uid=uid,
                        )
                    else:
                        consent_context = None
                if self.runner.busy:
                    await asyncio.sleep(min(_BUSY_RETRY_SECONDS, remaining))
                    continue
                # One preparation always precedes this point; narrow for the runner call.
                assert cfg is not None and otp is not None and answers is not None
                # The browser itself is bounded by the remaining attendance window.
                bounded = replace(cfg, run_timeout=min(cfg.run_timeout, remaining))
                try:
                    result = await self.runner.run(
                        bounded,
                        otp,
                        answers,
                        attendance_date=deadline.date(),
                        attendance_name=settings["attendance_name"],
                        department=getattr(user, "department", ""),
                        consent=consent_context,
                        deadline=deadline,
                        decision_uid=uid,
                        decision_revision=decision["revision"],
                    )
                except BusyRun:
                    await asyncio.sleep(min(_BUSY_RETRY_SECONDS, remaining))
                    continue
                await self.settle(uid, day, result=result)
                return
        except DuplicateRun:
            await self.settle_from_journal(uid, day)
        except TimeoutError:
            if self._remaining(deadline) <= 0:
                await self.settle(uid, day, status="missed")
            else:
                await self.settle_from_journal(uid, day)
        except asyncio.CancelledError:
            await self.settle_from_journal(uid, day)
            raise
        except Exception:
            await self.settle_from_journal(uid, day)
        finally:
            self._executions.pop((uid, day), None)

    @staticmethod
    def _outcome(status: str, check: object, detail: str, attempt_id: int | None) -> tuple[str, str]:
        # A run that may already have clicked is never laundered into a clean failure or
        # a source-recorded result, even when a matching row exists: the positive owner
        # observation is recorded separately and surfaced as its own evidence.
        if status == "unknown":
            return "unknown", "execution_failed"
        if getattr(check, "status", None) == "found":
            return "recorded", "records_found"
        if status == "recorded":
            return "recorded", "records_found"
        if status == "confirmed":
            return ("confirmed", "") if attempt_id is not None else ("unknown", "execution_failed")
        if status == "missed":
            return "missed", "deadline"
        if status == "blocked":
            if detail == "attendance window closed":
                return "missed", "deadline"
            if detail == "attendance identity required":
                return "held", "onboarding"
            # Source unavailable or source evidence changed: hold for a bounded recheck.
            return "held", "verification"
        return "failed", "execution_failed"

    @staticmethod
    def _outcome_text(day: str, decision: dict) -> str:
        state = decision["state"]
        if state == "confirmed":
            return f"Attendance for {day} was submitted and confirmed."
        if state == "unknown":
            return (
                f"Attendance for {day} has an UNKNOWN outcome; it may have been submitted. "
                "Do not retry automatically."
            )
        if state == "recorded":
            return (
                f"Attendance for {day} is already recorded at the source; no submission was made."
            )
        if state == "missed":
            return f"Attendance for {day} was not submitted before the deadline; no automatic retry."
        if state == "held":
            paused = {
                "user_edit": "you paused today's plan",
                "disabled": "automatic attendance is off",
                "otp_unavailable": "phone OTP delivery is not available",
                "verification": "the attendance source could not be verified",
                "onboarding": "your attendance name is not confirmed",
                "unplanned": "no plan is saved for today",
                "incomplete": "the saved plan is incomplete",
                "manual_submitted": "you marked a manual submission that needs review",
            }.get(decision["reason"], "action is required")
            return (
                f"Attendance for {day} is paused: {paused}. Automatic submission stays off until "
                "you act."
            )
        return f"Attendance for {day} failed before submission; no confirmed submission was recorded."

    async def settle(
        self, uid: int, day: str, *, result: object = None, status: str | None = None
    ) -> None:
        planner = self._planner_or_raise()
        decision = planner.get_decision(uid, day)
        if decision is None or decision["state"] != "running":
            return
        attempt_id = decision["attempt_id"]
        check = None
        detail = ""
        if result is not None:
            status = getattr(result, "status", None)
            attempt_id = getattr(result, "attempt_id", None) or attempt_id
            check = getattr(result, "record_check", None)
            detail = getattr(result, "detail", "") or ""
        state, reason = self._outcome(status or "failed", check, detail, attempt_id)
        # The attempt journal outranks any caller-supplied status: an attempt that may
        # have clicked (submitting/unknown) stays unknown rather than a clean failure.
        if attempt_id is not None and self.store.get_attempt(attempt_id)["status"] in {
            "submitting",
            "unknown",
        }:
            state, reason = "unknown", "execution_failed"
        changes: dict[str, Any] = {"state": state, "reason": reason}
        if attempt_id is not None:
            changes["attempt_id"] = attempt_id
        try:
            updated = planner.update_decision(
                uid, day, expected_revision=decision["revision"], **changes
            )
        except PlannerConflict:
            current = planner.get_decision(uid, day)
            if current is None or current["state"] != "running":
                return
            try:
                updated = planner.update_decision(
                    uid, day, expected_revision=current["revision"], **changes
                )
            except (PlannerConflict, PlannerBlocked):
                log.error("Attendance decision could not be settled; journal evidence retained")
                return
        except PlannerBlocked:
            log.error("Attendance outcome conflicts with the attempt journal; retained for review")
            return
        await self._notify(uid, self._outcome_text(day, updated), decision=updated)

    async def settle_from_journal(self, uid: int, day: str) -> None:
        decision = self._planner_or_raise().get_decision(uid, day)
        if decision is None or decision["state"] != "running":
            return
        attempt_id = decision["attempt_id"]
        if attempt_id is None:
            await self.settle(uid, day, status="failed")
            return
        status = self.store.get_attempt(attempt_id)["status"]
        await self.settle(
            uid,
            day,
            status={"confirmed": "confirmed", "unknown": "unknown", "submitting": "unknown"}.get(
                status, "failed"
            ),
        )

    async def _notify(self, uid: int, text: str, decision: dict | None = None) -> int | None:
        try:
            async with asyncio.timeout(_NOTIFICATION_TIMEOUT):
                return await self.notify(uid, text, decision=decision)
        except Exception:
            log.warning("Scheduled attendance notification could not be delivered")
            return None

    async def today(self, uid: int, *, fresh: bool = False) -> dict:
        """Shared read model for the Telegram Mini App and Telegram buttons."""
        planner = self._planner_or_raise()
        decision = planner.ensure_decision(uid)
        day = decision["day"]
        settings = planner.get_settings(uid)
        user = self._allowlist().get(uid)
        # An ambiguous mapping suspends both owners' evidence, progress and records.
        # The check runs whenever this owner claims a name, even after a prior
        # suspension cleared name_confirmed: otherwise the persisted suspension would
        # read as "not yet confirmed" and expose the very records it suppressed, and a
        # collision is still explicit before any daily decision row exists.
        identity_suspended = False
        if (
            user is not None
            and getattr(user, "department", "")
            and settings["attendance_name"]
        ):
            try:
                planner.assert_identity_unique(
                    uid,
                    getattr(user, "department", ""),
                    {
                        owner: getattr(other, "department", "")
                        for owner, other in self._allowlist().items()
                    },
                )
            except PlannerBlocked:
                # The suspension disabled this mapping; reload the persisted settings
                # and decision instead of returning the pre-suspension snapshot.
                identity_suspended = True
                settings = planner.get_settings(uid)
                decision = planner.get_decision(uid, day) or decision
        check: RecordCheck | None = None
        observation = None
        if not identity_suspended:
            if not fresh:
                check = self._recent_check(uid, day)
            if check is None:
                # UI reads reuse a bounded in-memory check; an unavailable reading stays
                # visible here instead of being reconstructed as found/not_found from the
                # retained observation rows, and polling never forces the source.
                check = await self._refresh_records(uid, fresh=fresh)
                decision = planner.get_decision(uid, day) or decision
            observation = planner.get_observation(uid, day)
        next_action, next_action_at = self._next_action(decision)
        return {
            "today": day,
            "settings": settings,
            "decision": decision,
            "identity_suspended": identity_suspended,
            "observation": observation,
            "source_check": self._source_check(check, observation),
            "local_outcome": self._local_outcome(uid, decision),
            "next_action": next_action,
            "next_action_at": next_action_at,
            "stage": self._runner_stage(uid, decision) if not identity_suspended else None,
            "otp_ready": self.otp_ready(uid),
        }

    def _runner_stage(self, uid: int, decision: dict) -> str | None:
        """Truthful owner-scoped progress; another owner's browser work is hidden.

        The runner keys its stages by this owner's account/form/day, so a busy
        browser held by someone else yields None here and is reported as waiting.
        """
        state = decision["state"]
        if state == "running":
            user = self._allowlist().get(uid)
            stage_fn = getattr(self.runner, "stage", None)
            if user is None or stage_fn is None:
                return "waiting_for_browser"
            current = stage_fn(
                getattr(user, "singpass_id", ""), self.base_cfg.form_url, decision["day"]
            )
            return current or "waiting_for_browser"
        if state == "ready":
            execute_at = decision.get("execute_at")
            if (
                execute_at
                and datetime.fromisoformat(execute_at) <= self._now()
                < self._deadline(decision["day"])
            ):
                return "waiting_for_browser"
        return None

    @staticmethod
    def _source_check(
        check: RecordCheck | None, observation: dict | None
    ) -> dict[str, Any] | None:
        """Latest owner source evidence: the four UI-safe fields, never full rows."""
        if check is not None:
            return {
                "status": check.status,
                "checked_at": check.checked_at.isoformat(),
                "error_code": check.error_code,
                "digest": check.digest,
            }
        if observation is not None:
            return {
                "status": "found" if observation["records"] else "not_found",
                "checked_at": observation["checked_at"],
                "error_code": None,
                "digest": observation["digest"],
            }
        return None

    def _local_outcome(self, uid: int, decision: dict) -> str | None:
        attempt_id = decision["attempt_id"]
        if attempt_id is None:
            # Ordinary manual/profile attempts are not bound to a decision, so fall
            # back to the latest same-day journal row for this exact identity and form.
            user = self._allowlist().get(uid)
            if user is None:
                return None
            rows = self.store.submission_attempts(
                getattr(user, "singpass_id", ""), self.base_cfg.form_url, decision["day"]
            )
            attempt_id = rows[-1]["id"] if rows else None
        if attempt_id is None:
            return None
        return self.store.get_attempt(attempt_id)["status"]

    @staticmethod
    def _next_action(decision: dict) -> tuple[str, str | None]:
        state = decision["state"]
        if state == "running":
            return "submission_in_progress", None
        if state in {"awaiting", "ready"}:
            return "automatic_submission", decision["execute_at"]
        if state == "held":
            return "action_required", None
        return "none", None

    async def action(
        self,
        uid: int,
        action: str,
        *,
        expected_revision: int,
        profile: str | None = None,
        details: dict | None = None,
        consent_digest: str | None = None,
    ) -> dict:
        """Shared mutation entry point for the Mini App API and Telegram buttons."""
        planner = self._planner_or_raise()
        check: RecordCheck | None = None
        if action in _EVIDENCE_ACTIONS:
            check, expected_revision = await self._refresh_evidence(uid, expected_revision)
        if action == "additional_review":
            return await self._review(
                uid, expected_revision=expected_revision, profile=profile, details=details
            )
        if action == "additional_confirm":
            return await self._confirm(
                uid,
                expected_revision=expected_revision,
                consent_digest=consent_digest,
                profile=profile,
                details=details,
            )
        if action == "manual_submitted":
            return await self._manual_submitted(
                uid, expected_revision=expected_revision, check=check
            )
        return planner.action(
            uid, action, expected_revision=expected_revision, profile=profile, details=details
        )

    async def _refresh_evidence(
        self, uid: int, expected_revision: int
    ) -> tuple[RecordCheck | None, int]:
        """Validate the owner revision, refresh source evidence, return the safe one.

        ``_refresh_records`` validates the revision at the exact moment it merges, so
        an owner change during the source read conflicts here even when it restored
        the same profile/details (ABA) and the revision delta looks like evidence.
        """
        planner = self._planner_or_raise()
        before = planner.ensure_decision(uid)
        day = before["day"]
        if before["revision"] != expected_revision:
            raise PlannerConflict("Decision changed; refresh before acting")
        check = await self._refresh_records(uid, expected_revision=expected_revision)
        after = planner.get_decision(uid, day)
        if after is None:
            raise PlannerConflict("Decision changed; refresh before acting")
        return check, after["revision"]

    async def _manual_submitted(
        self, uid: int, *, expected_revision: int, check: RecordCheck | None
    ) -> dict:
        """Owner-declared manual submission: refresh evidence, never assert success."""
        planner = self._planner_or_raise()
        day = planner.ensure_decision(uid)["day"]
        decision = planner.get_decision(uid, day) or planner.ensure_decision(uid)
        if decision["state"] == "recorded":
            # The source now confirms it; keep the suppressed state.
            return decision
        held = planner.action(
            uid, "manual_submitted", expected_revision=decision["revision"]
        )
        if check is None or check.status == "unavailable":
            await self._note_once(
                uid,
                day,
                "manual_submitted_unavailable",
                "Manual submission recorded, but the attendance source is currently "
                "unavailable; it could not be confirmed. Review the planner before the deadline.",
                held,
            )
        return held

    def _reviewed_attempts(self, uid: int, day: str) -> list[int]:
        """Local submit blockers for this owner's account/form/day, as reviewed."""
        user = self._allowlist().get(uid)
        if user is None:
            return []
        return [
            row["id"]
            for row in self.store.submission_attempts(
                getattr(user, "singpass_id", ""), self.base_cfg.form_url, day
            )
        ]

    async def _answers_for(
        self, uid: int, day: str, profile: str | None, details: dict | None
    ) -> tuple[dict, Answers]:
        decision = self._planner_or_raise().ensure_decision(uid)
        chosen = decision["profile"] if profile is None else profile
        raw = decision["details"] if profile is None and details is None else (details or {})
        chosen_details, _ = validate_profile(chosen, raw)
        _cfg, _otp, answers = await self.prepare(
            uid, chosen, chosen_details, self._deadline(day)
        )
        return chosen_details, answers

    async def _review(
        self, uid: int, *, expected_revision: int, profile: str | None, details: dict | None
    ) -> dict:
        planner = self._planner_or_raise()
        day = planner.ensure_decision(uid)["day"]
        chosen_details, answers = await self._answers_for(uid, day, profile, details)
        return planner.review_consent(
            uid,
            expected_revision=expected_revision,
            answer_hash=answer_hash(answers),
            local_attempt_ids=self._reviewed_attempts(uid, day),
            profile=profile,
            details=chosen_details,
        )

    async def _confirm(
        self,
        uid: int,
        *,
        expected_revision: int,
        consent_digest: str | None,
        profile: str | None,
        details: dict | None,
    ) -> dict:
        planner = self._planner_or_raise()
        if not isinstance(consent_digest, str) or not consent_digest:
            raise PlannerInvalid("A reviewed consent digest is required")
        day = planner.ensure_decision(uid)["day"]
        _chosen_details, answers = await self._answers_for(uid, day, profile, details)
        return planner.consume_consent(
            uid,
            expected_revision=expected_revision,
            consent_digest=consent_digest,
            answer_hash=answer_hash(answers),
            local_attempt_ids=self._reviewed_attempts(uid, day),
        )
