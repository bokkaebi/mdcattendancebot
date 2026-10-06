"""Single-flight execution with durable, conservative submission outcomes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING

from .attendance import Answers, validate_answers
from .bot import run_flow
from .config import Config
from .otp import OtpProvider
from .records import RecordCheck, SubmissionRecords
from .storage import SINGAPORE, AdditionalSubmissionConsent, StateStore, iso_day
from .storage import DuplicateRun as DuplicateRun

if TYPE_CHECKING:  # avoids an import cycle with the planner primitives
    from .schedules import Planner


class BusyRun(RuntimeError):
    """Another process or task owns the one browser slot."""


def answer_hash(answers: Answers) -> str:
    """Canonical identity of exactly the answers a submission will send."""
    canonical = json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunResult:
    status: str
    attempt_id: int | None = None
    detail: str = ""
    record_check: RecordCheck | None = None


class _Stop(RuntimeError):
    """Internal: a durable boundary prevented, or re-classified, a submission."""

    def __init__(self, status: str, detail: str, check: RecordCheck | None = None) -> None:
        self.status = status
        self.detail = detail
        self.check = check
        super().__init__(detail)


class AttendanceRunner:
    """The one browser slot; every caller shares these local and source checks."""

    def __init__(
        self,
        cfg: Config,
        store: StateStore,
        records: SubmissionRecords | None = None,
    ) -> None:
        if Path(cfg.state_dir).expanduser().resolve() != Path(store.state_dir):
            raise ValueError("Runner and store must share one state directory")
        self.cfg = cfg
        self.store = store
        # One shared anonymous source reader: callers must not build their own.
        self.records = records if records is not None else SubmissionRecords()
        self._lock_path = str(Path(store.state_dir) / "run.lock")
        self._busy = False
        self.last_result: RunResult | None = None
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        # Truthful per-run progress keyed by (account, form, day); stage names are
        # public, but no credential, OTP or phone data ever enters this map.
        self._stages: dict[tuple[str, str, str], str] = {}

    def stage(self, account: str, form: str, attendance_date: str | date) -> str | None:
        """Current truthful stage for one account/form/day, or None when idle."""
        return self._stages.get((account, form, iso_day(attendance_date)))

    def _acquire_file_lock(self) -> int:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BusyRun("Another attendance run is active") from None
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_file_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def busy(self) -> bool:
        if self._busy:
            return True
        try:
            fd = self._acquire_file_lock()
        except BusyRun:
            return True
        self._release_file_lock(fd)
        return False

    def recover(self) -> bool:
        """Startup recovery; the scheduler also holds its lifecycle lock."""
        if self._busy or self._closed:
            return False
        try:
            fd = self._acquire_file_lock()
        except BusyRun:
            return False
        try:
            self.store.recover(attendance_deadline=self.cfg.attendance_deadline)
        finally:
            self._release_file_lock(fd)
        return True

    def _planner(self, cfg: Config) -> Planner:
        from .schedules import Planner

        return Planner(self.store, cfg.attendance_deadline)

    def _observe(
        self, cfg: Config, uid: int | None, day: date, check: RecordCheck
    ) -> int | None:
        """Retain selected-owner evidence durably; return the decision revision."""
        if uid is None:
            return None
        planner = self._planner(cfg)
        planner.merge_observation(uid, day, check)
        decision = planner.get_decision(uid, day)
        if decision is None or decision["state"] != "running":
            raise _Stop("blocked", "decision changed before submission", check)
        return decision["revision"]

    def _retained_records(self, cfg: Config, uid: int | None, day: date) -> bool:
        """True while durable selected-owner evidence still holds a positive row.

        The live export may momentarily drop a row (source lag); retention means the
        local blocker survives until an explicit reviewed additional submission.
        """
        if uid is None:
            return False
        observation = self._planner(cfg).get_observation(uid, day)
        return bool(observation and observation["records"])

    def _bind_attempt(
        self,
        cfg: Config,
        uid: int,
        day: date,
        attempt_id: int,
        expected_revision: int | None,
    ) -> int:
        """Bind an ordinary attempt to its decision before the browser starts.

        A concurrent owner change raises before login, so recovery and cancellation
        always see a journal boundary tied to the same decision revision.
        """
        from .schedules import PlannerBlocked, PlannerConflict

        # A decision-bound submit must carry the revision its owner reviewed; a missing
        # one is never guessed or cast, it aborts before login like any stale revision.
        if expected_revision is None:
            raise _Stop("blocked", "decision revision missing before submission")
        try:
            decision = self._planner(cfg).bind_attempt(
                uid, day, attempt_id, expected_revision=expected_revision
            )
        except (PlannerConflict, PlannerBlocked):
            raise _Stop("blocked", "decision changed before submission") from None
        return decision["revision"]

    async def run(
        self,
        cfg: Config,
        otp: OtpProvider,
        answers: Answers,
        *,
        attendance_date: date | None = None,
        attendance_name: str | None = None,
        department: str | None = None,
        consent: AdditionalSubmissionConsent | None = None,
        deadline: datetime | None = None,
        decision_uid: int | None = None,
        decision_revision: int | None = None,
    ) -> RunResult:
        if self._closed:
            raise RuntimeError("Attendance runner is shut down")
        if self._busy:
            raise BusyRun("Another attendance run is active")
        # No await before this guard and flock: callers cannot launch two browsers.
        self._busy = True
        fd: int | None = None
        attempt_id: int | None = None
        stage_key: tuple[str, str, str] | None = None
        self.last_result = None
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            mode = (
                "preflight"
                if cfg.preflight
                else "discover"
                if cfg.discover
                else "dry_run"
                if cfg.dry_run
                else "submit"
            )
            day = attendance_date or datetime.now(SINGAPORE).date()
            if not isinstance(day, date) or isinstance(day, datetime):
                raise ValueError("Attendance date must be a date")
            if mode == "submit" and day != datetime.now(SINGAPORE).date():
                raise ValueError("Submission is permitted only for today's Singapore date")
            if consent is not None and not isinstance(consent, AdditionalSubmissionConsent):
                raise ValueError("consent must be an AdditionalSubmissionConsent or None")
            if consent is not None and mode != "submit":
                raise ValueError("consent applies only to real submissions")
            key = (cfg.singpass_id, cfg.form_url, day.isoformat())
            stage_key = key

            def on_stage(name: str) -> None:
                # Truthful progress only: no credential, OTP or phone data reaches here.
                self._stages[key] = name

            fd = self._acquire_file_lock()
            self._stages[key] = "waiting_for_browser"
            # Schema upgrade plus attempt recovery under the run lock. The startup
            # variant (StateStore.recover) additionally resets "running" decisions and
            # belongs to the scheduler lifecycle locks, never to a live dispatch.
            self.store.upgrade_planner(attendance_deadline=cfg.attendance_deadline)
            self.store.recover_attempts()
            limit: datetime = datetime.combine(
                day, time.fromisoformat(cfg.attendance_deadline), SINGAPORE
            )
            if deadline is not None:
                if deadline.tzinfo is None or deadline.utcoffset() is None:
                    raise ValueError("Submission deadline must be timezone-aware")
                requested = deadline.astimezone(SINGAPORE)
                if requested.date() != day:
                    raise ValueError("Submission deadline must fall on the attendance date")
                # A caller-supplied deadline may shorten the window but never
                # extend the configured Singapore cutoff.
                limit = min(limit, requested)
            expected_revision: int | None = None
            consent_digest: str | None = None
            owner: str = attendance_name if isinstance(attendance_name, str) else ""
            owner_department: str = department if isinstance(department, str) else ""

            async def before_submit() -> None:
                if attempt_id is None:
                    raise RuntimeError("Submission boundary reached without an attempt")
                now = datetime.now(SINGAPORE)
                if day != now.date() or now >= limit:
                    raise _Stop("blocked", "attendance window closed")
                fresh = await self.records.lookup(owner, owner_department, day, fresh=True)
                if fresh.status == "unavailable":
                    raise _Stop("blocked", "external source unavailable", fresh)
                if consent_digest is not None and fresh.digest != consent_digest:
                    self._observe(cfg, decision_uid, day, fresh)
                    raise _Stop("blocked", "source evidence changed before submission", fresh)
                if decision_uid is not None:
                    decision = self._planner(cfg).get_decision(decision_uid, day)
                    if (
                        decision is None
                        or decision["state"] != "running"
                        or (
                            expected_revision is not None
                            and decision["revision"] != expected_revision
                        )
                    ):
                        raise _Stop("blocked", "decision changed before submission", fresh)
                if consent_digest is None and (
                    fresh.status == "found" or self._retained_records(cfg, decision_uid, day)
                ):
                    self._observe(cfg, decision_uid, day, fresh)
                    raise _Stop("recorded", "existing attendance found", fresh)
                # The deadline is re-checked after the network await above; this
                # transaction commits before the browser is permitted to click.
                if datetime.now(SINGAPORE) >= limit:
                    raise _Stop("blocked", "attendance window closed")
                self.store.transition(attempt_id, "submitting")

            async with asyncio.timeout(cfg.run_timeout):
                if mode == "submit":
                    if (
                        not isinstance(attendance_name, str)
                        or not isinstance(department, str)
                        or not attendance_name.strip()
                        or not department.strip()
                    ):
                        raise _Stop("blocked", "attendance identity required")
                    if datetime.now(SINGAPORE) >= limit:
                        raise _Stop("blocked", "attendance window closed")
                    self._stages[key] = "checking_records"
                    check = await self.records.lookup(owner, owner_department, day, fresh=True)
                    if decision_uid is not None:
                        decision = self._planner(cfg).get_decision(decision_uid, day)
                        if (
                            decision is None
                            or decision["state"] != "running"
                            or (
                                decision_revision is not None
                                and decision["revision"] != decision_revision
                            )
                        ):
                            raise _Stop("blocked", "decision changed before submission", check)
                    if check.status == "unavailable":
                        raise _Stop("blocked", "external source unavailable", check)
                    if decision_uid is not None:
                        expected_revision = self._observe(cfg, decision_uid, day, check)
                    if consent is not None:
                        # A review authorises one attempt only, and never as a bare
                        # token: every reviewed field is re-checked against live
                        # evidence and the durable row before any browser work.
                        if consent.decision_uid != decision_uid:
                            raise DuplicateRun(
                                "Consent belongs to a different decision; review again"
                            )
                        if consent.date != day:
                            raise DuplicateRun(
                                "Consent is not for today's attendance date; review again"
                            )
                        if answer_hash(answers) != consent.answer_hash:
                            raise DuplicateRun("Reviewed answers changed; review again")
                        if check.digest is None:
                            raise _Stop("blocked", "external source unavailable", check)
                        if check.digest != consent.records_digest:
                            self._observe(cfg, decision_uid, day, check)
                            raise DuplicateRun("Reviewed evidence changed; review again")
                        consent_digest = consent.records_digest
                    elif check.status == "found" or self._retained_records(
                        cfg, decision_uid, day
                    ):
                        # Automatic work never duplicates a same-day entry, even when
                        # the current export momentarily omits a retained row.
                        return RunResult("recorded", None, "existing attendance found", check)
                    if datetime.now(SINGAPORE) >= limit:
                        raise _Stop("blocked", "attendance window closed")
                    validate_answers(answers)
                    if consent is not None:
                        attempt_id = self.store.prepare_consent(
                            cfg.singpass_id,
                            cfg.form_url,
                            day,
                            "submit",
                            consent,
                        )
                        if decision_uid is not None:
                            # Consent consumption bound the attempt atomically; adopt the
                            # revision it committed so later owner edits still invalidate.
                            bound = self._planner(cfg).get_decision(decision_uid, day)
                            if bound is not None:
                                expected_revision = bound["revision"]
                    else:
                        attempt_id = self.store.prepare(
                            cfg.singpass_id, cfg.form_url, day, "submit"
                        )
                        if decision_uid is not None:
                            expected_revision = self._bind_attempt(
                                cfg, decision_uid, day, attempt_id, expected_revision
                            )
                else:
                    if mode == "dry_run":
                        validate_answers(answers)
                    attempt_id = self.store.prepare(cfg.singpass_id, cfg.form_url, day, mode)
                self.store.transition(attempt_id, "running")
                status = await run_flow(
                    cfg,
                    otp,
                    answers,
                    before_submit=before_submit if mode == "submit" else None,
                    on_stage=on_stage,
                )
                expected = (
                    "discovered"
                    if mode == "discover"
                    else "confirmed"
                    if mode == "submit"
                    else mode
                )
                if status != expected:
                    raise RuntimeError("Browser did not produce the expected outcome")
                self.store.transition(attempt_id, status)
                self.last_result = RunResult(status, attempt_id)
                return self.last_result
        except _Stop as stop:
            return self._stop_result(stop, attempt_id)
        except asyncio.CancelledError:
            if attempt_id is not None:
                self._failed_result(attempt_id, "run cancelled")
            raise
        except TimeoutError:
            if attempt_id is None:
                raise
            return self._failed_result(attempt_id, "run timed out")
        except Exception:
            if attempt_id is None:
                raise
            return self._failed_result(attempt_id, "execution failed")
        except BaseException:
            if attempt_id is not None:
                self._failed_result(attempt_id, "execution failed")
            raise
        finally:
            if stage_key is not None:
                self._stages.pop(stage_key, None)
            if fd is not None:
                self._release_file_lock(fd)
            self._busy = False
            if task is not None:
                self._tasks.discard(task)

    def _stop_result(self, stop: _Stop, attempt_id: int | None) -> RunResult:
        """Record a pre-click abort as a known failure, reporting the real outcome."""
        if attempt_id is not None:
            state = self.store.get_attempt(attempt_id)["status"]
            status = "unknown" if state == "submitting" else "failed"
            self.store.transition(attempt_id, status, stop.detail)
        self.last_result = RunResult(stop.status, attempt_id, stop.detail, stop.check)
        return self.last_result

    def _failed_result(self, attempt_id: int, detail: str) -> RunResult:
        current = self.store.get_attempt(attempt_id)["status"]
        status = "unknown" if current == "submitting" else "failed"
        self.store.transition(attempt_id, status, detail)
        self.last_result = RunResult(status, attempt_id, detail)
        return self.last_result

    async def shutdown(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
