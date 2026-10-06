"""Clock-driven durable morning workflow: one decision per owner/day, no replay."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mdcattendance.attendance import DEPARTMENT, NORMAL_ANSWERS
from mdcattendance.config import Config
from mdcattendance.records import RecordCheck, SubmissionRecord
from mdcattendance.runner import BusyRun, DuplicateRun
from mdcattendance.scheduler import Scheduler
from mdcattendance.schedules import Planner, PlannerBlocked, PlannerConflict
from mdcattendance.storage import SINGAPORE, AdditionalSubmissionConsent, StateStore

FORM = "https://example.invalid/synthetic-attendance-form"
DAY = "2026-10-02"


@contextmanager
def startup_locks(store):
    descriptors = []
    try:
        for name in ("scheduler.lock", "run.lock"):
            fd = os.open(Path(store.state_dir) / name, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            descriptors.append(fd)
        yield
    finally:
        for fd in reversed(descriptors):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class StaticRecords:
    """Owner-scoped source evidence with a scripted sequence of check results."""

    def __init__(self, *checks):
        self._checks = list(checks)
        self.lookups = 0

    async def lookup(self, name, department, day, *, fresh=False):
        self.lookups += 1
        if not self._checks:
            raise RuntimeError("synthetic source has no scripted result left")
        result = self._checks[0] if len(self._checks) == 1 else self._checks.pop(0)
        return result(day) if callable(result) else result


def not_found(day, label="empty", checked_at=None) -> RecordCheck:
    return RecordCheck(
        "not_found",
        (),
        checked_at or datetime.now(SINGAPORE),
        hashlib.sha256(label.encode()).hexdigest(),
    )


def found(day, label="found", checked_at=None) -> RecordCheck:
    record = SubmissionRecord(
        datetime.combine(day, datetime.min.time(), SINGAPORE).replace(hour=7),
        "Synthetic status",
        (("Remarks (NSC/IS)", "Synthetic"),),
    )
    return RecordCheck(
        "found",
        (record,),
        checked_at or datetime.now(SINGAPORE),
        hashlib.sha256(label.encode()).hexdigest(),
    )


def unavailable(day, checked_at=None) -> RecordCheck:
    return RecordCheck(
        "unavailable", (), checked_at or datetime.now(SINGAPORE), None, "network_error"
    )


class StubOtp:
    """Deterministic OTP provider: the runner double never prompts for a code."""

    async def wait_for_otp(self, timeout):
        return "123456"


class StubRunner:
    """Runner-shaped double: journal-consistent attempts, scripted outcomes."""

    def __init__(self, store, *, script=None, records=None, busy=False, stages=None):
        self.store = store
        self.records = records
        self.busy = busy
        self.last_result = None
        self.calls = []
        self.script = list(script or [])
        self.stages = dict(stages or {})

    def recover(self) -> bool:
        self.store.recover(attendance_deadline="09:00")
        return True

    def stage(self, account, form, day):
        return self.stages.get((account, form, day.isoformat() if hasattr(day, "isoformat") else day))

    async def run(self, cfg, otp, answers, **kwargs):
        self.calls.append(SimpleNamespace(cfg=cfg, answers=dict(answers), kwargs=kwargs))
        status, check = self.script.pop(0) if self.script else ("confirmed", None)
        if callable(check):
            check = check(kwargs["attendance_date"])
        attempt_id = None
        if status in {"confirmed", "unknown", "failed"}:
            consent = kwargs.get("consent")
            if consent is None:
                attempt_id = self.store.prepare(
                    cfg.singpass_id, cfg.form_url, kwargs["attendance_date"], "submit"
                )
            else:
                # Mirrors the real runner: the reviewed context is consumed with the
                # attempt, so the same consent can never authorise a second submission.
                attempt_id = self.store.prepare_consent(
                    cfg.singpass_id, cfg.form_url, kwargs["attendance_date"], "submit", consent
                )
            self.store.transition(attempt_id, "running")
            if status in {"confirmed", "unknown"}:
                self.store.transition(attempt_id, "submitting")
                self.store.transition(attempt_id, status)
            else:
                self.store.transition(attempt_id, "failed")
        result = SimpleNamespace(
            status=status, attempt_id=attempt_id, detail="", record_check=check
        )
        self.last_result = result
        return result


class Notifier:
    def __init__(self, deliver=True):
        self.messages = []
        self.deliver = deliver

    async def __call__(self, uid, text, *, decision=None):
        self.messages.append(SimpleNamespace(uid=uid, text=text, decision=decision))
        return len(self.messages) if self.deliver else None

    def texts(self, needle=""):
        return [message.text for message in self.messages if needle in message.text]


class SchedulerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 2, 8, 0, tzinfo=SINGAPORE)
        self.prepared = []
        self.schedulers = []
        with startup_locks(self.store):
            self.store.upgrade_planner(today=self.now.date(), attendance_deadline="09:00")
        self.planner = Planner(self.store, "09:00", clock=lambda: self.now)

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)

    async def onboard(self, uid=7, profile="normal", details=None, name="SYNTHETIC OWNER", plan=True):
        revision = self.planner.get_settings(uid)["revision"]
        settings = self.planner.set_name(uid, name, expected_revision=revision)
        settings = self.planner.confirm_name(uid, expected_revision=settings["revision"])
        self.planner.save_settings(
            uid,
            expected_revision=settings["revision"],
            enabled=True,
            prompt_time="08:00",
            auto_time="08:30",
            otp_ready=True,
            policy_accepted=True,
        )
        if not plan:
            return self.planner.get_settings(uid)
        plan_revision = self.planner.get_plan(uid)["revision"]
        self.planner.save_plan(
            uid,
            [{"date": DAY, "profile": profile, "details": details or {}}],
            expected_revision=plan_revision,
        )
        return self.planner.get_settings(uid)

    def build(
        self, *, script=None, records=None, busy=False, with_user=True, deliver=True, prepare=None
    ):
        runner = StubRunner(self.store, script=script, records=records, busy=busy)
        notifier = Notifier(deliver)
        cfg = Config(
            singpass_id="synthetic-account",
            form_url=FORM,
            state_dir=self.directory.name,
            attendance_deadline="09:00",
            otp_http_enabled=True,
            otp_http_token="synthetic-receiver-token",
        )

        async def default_prepare(uid, profile, details, deadline):
            self.prepared.append((uid, profile, dict(details)))
            return cfg, StubOtp(), dict(NORMAL_ANSWERS)

        users = (
            (lambda: {7: SimpleNamespace(department=DEPARTMENT, otp_token="synthetic-otp-token")})
            if with_user
            else (lambda: {})
        )
        scheduler = Scheduler(
            self.store,
            runner,
            cfg,
            prepare or default_prepare,
            notifier,
            users=users,
            clock=lambda: self.now,
        )
        self.schedulers.append(scheduler)
        self.runner = runner
        self.notifier = notifier
        return scheduler

    async def drain(self, timeout=5):
        tasks = set(self.schedulers[-1]._executions.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        for scheduler in self.schedulers:
            await scheduler.stop()

    def decision(self, uid=7):
        return self.planner.get_decision(uid)

    async def test_silent_window_executes_saved_profile_exactly_once(self):
        # Closing the Mini App is irrelevant: nothing here talks to a client.
        await self.onboard()
        scheduler = self.build(script=[("confirmed", None)], records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        prompted = self.decision()
        self.assertTrue(prompted["prompt_sent"])
        self.assertEqual(prompted["prompt_message_id"], 1)
        self.assertEqual(self.runner.calls, [])
        self.advance(25)
        await scheduler.tick(self.now)
        self.assertEqual(len(self.notifier.texts("five minutes")), 1)
        self.advance(5)
        await scheduler.tick(self.now)
        await self.drain()
        settled = self.decision()
        self.assertEqual(settled["state"], "confirmed")
        self.assertEqual(len(self.runner.calls), 1)
        call = self.runner.calls[0]
        self.assertEqual(self.prepared, [(7, "normal", {})])
        self.assertEqual(call.kwargs["decision_uid"], 7)
        self.assertEqual(call.kwargs["attendance_name"], "SYNTHETIC OWNER")
        self.assertEqual(call.kwargs["department"], DEPARTMENT)
        self.assertIsNone(call.kwargs["consent"])
        self.advance(1)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.store.attempts()), 1)

    async def test_reminder_is_not_repeated_once_acknowledged(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        decision = self.decision()
        await scheduler.action(7, "keep", expected_revision=decision["revision"])
        self.advance(25)
        await scheduler.tick(self.now)
        self.assertEqual(self.notifier.texts("five minutes"), [])
        self.assertEqual(self.decision()["state"], "awaiting")

    async def test_no_plan_prompts_without_any_fallback(self):
        await self.onboard(plan=False)
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        decision = self.decision()
        self.assertEqual((decision["state"], decision["reason"]), ("held", "unplanned"))
        self.assertTrue(decision["prompt_sent"])
        self.assertTrue(self.notifier.texts("No plan for today"))
        self.assertTrue(self.notifier.texts("Fewer than three future weekdays"))
        self.now = self.now.replace(hour=8, minute=30)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.decision()["state"], "held")

    async def test_incomplete_ma_is_prompted_but_never_falls_back(self):
        await self.onboard(profile="ma", details={"period": "both", "timing": "0830"})
        scheduler = self.build(records=StaticRecords(not_found))
        self.assertEqual(self.decision(), None)
        await scheduler.tick(self.now)
        await scheduler.tick(self.now.replace(hour=8, minute=30))
        self.advance(5)
        await scheduler.tick(self.now)
        decision = self.decision()
        self.assertEqual((decision["state"], decision["reason"]), ("held", "incomplete"))
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(decision["prompt_sent"])

    async def test_existing_submission_is_prompted_and_never_auto_executes(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(lambda day: found(day)))
        await scheduler.tick(self.now)
        decision = self.decision()
        self.assertEqual((decision["state"], decision["reason"]), ("recorded", "records_found"))
        self.assertTrue(decision["prompt_sent"])
        self.assertTrue(self.notifier.texts("already recorded"))
        self.advance(30)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.decision()["state"], "recorded")

    def test_recorded_prompt_shows_details_and_keeps_retained_evidence(self):
        record = SubmissionRecord(
            self.now,
            "PRESENT AT EVENT",
            (
                ("Remarks (Event Name)", "Synthetic Event Alpha"),
                ("Remarks (Location & Reporting Time)", "Synthetic Location Beta 0900"),
            ),
        )
        check = RecordCheck("found", (record,), self.now, hashlib.sha256(b"alpha").hexdigest())
        text = Scheduler._recorded_prompt(check, None)
        for expected in (
            "2026-10-02 08:00",
            "PRESENT AT EVENT",
            "Synthetic Event Alpha",
            "Synthetic Location Beta 0900",
        ):
            self.assertIn(expected, text)
        self.assertIn("suppress", text.lower())
        self.assertNotIn(check.digest, text)

        # Retained positive evidence must stay visible and keep suppression when a later
        # source check is absent or unavailable, without the two looking alike.
        observation = {
            "records": [record.to_dict()],
            "checked_at": self.now.isoformat(),
            "digest": hashlib.sha256(b"alpha").hexdigest(),
        }
        absent = Scheduler._recorded_prompt(not_found(DAY, checked_at=self.now), observation)
        unavailable_text = Scheduler._recorded_prompt(
            unavailable(DAY, checked_at=self.now), observation
        )
        for rendered in (absent, unavailable_text):
            self.assertIn("Synthetic Event Alpha", rendered)
            self.assertIn("Synthetic Location Beta 0900", rendered)
            self.assertIn("2026-10-02 08:00", rendered)
            self.assertIn("suppress", rendered.lower())
        self.assertNotEqual(absent, unavailable_text)

        # Long source cells are bounded so Telegram delivery cannot fail.
        huge = SubmissionRecord(self.now, "S" * 5000, (("Remarks (Event Name)", "E" * 5000),))
        bounded = Scheduler._recorded_prompt(
            RecordCheck("found", (huge,), self.now, hashlib.sha256(b"huge").hexdigest()), None
        )
        self.assertLessEqual(len(bounded), 4096)

    async def test_submit_now_races_the_cutoff_with_one_winner(self):
        await self.onboard()
        scheduler = self.build(script=[("confirmed", None)], records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        decision = self.decision()
        self.advance(5)
        ready = await scheduler.action(
            7, "submit_now", expected_revision=decision["revision"]
        )
        self.assertEqual(ready["state"], "ready")
        self.advance(25)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.decision()["state"], "confirmed")
        self.assertEqual(len(self.store.attempts()), 1)
        self.advance(1)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(len(self.runner.calls), 1)

    async def test_stale_revision_conflict_protects_the_newer_choice(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        stale = self.decision()["revision"]
        kept = await scheduler.action(7, "keep", expected_revision=stale)
        self.assertTrue(kept["acknowledged"])
        with self.assertRaises(PlannerConflict):
            await scheduler.action(7, "hold", expected_revision=stale)
        self.assertEqual(self.decision()["state"], "awaiting")
        self.advance(20)
        held = await scheduler.action(7, "hold", expected_revision=kept["revision"])
        self.assertEqual((held["state"], held["reason"]), ("held", "user_edit"))
        self.advance(10)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.decision()["state"], "held")

    async def test_restart_resumes_pending_without_replaying_running_attempt(self):
        await self.onboard()
        await self.onboard(uid=8, name="OTHER SYNTHETIC OWNER")
        pending = self.planner.ensure_decision(7)
        self.planner.mark_prompt(7, DAY, expected_revision=pending["revision"], message_id=11)
        self.now = self.now.replace(hour=8, minute=30)
        running = self.planner.ensure_decision(8)
        claimed = self.planner.claim_decision(8, DAY, expected_revision=running["revision"])
        attempt_id = self.store.prepare(
            "synthetic-other-account", FORM, self.now.date(), "submit"
        )
        self.store.transition(attempt_id, "running")
        self.store.transition(attempt_id, "submitting")
        self.planner.bind_attempt(8, DAY, attempt_id, expected_revision=claimed["revision"])
        scheduler = self.build(script=[("confirmed", None)], records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual([call.kwargs["decision_uid"] for call in self.runner.calls], [7])
        self.assertEqual(self.decision(7)["state"], "confirmed")
        self.assertEqual(self.decision(8)["state"], "unknown")
        self.assertEqual(self.store.get_attempt(attempt_id)["status"], "unknown")

    async def test_busy_browser_and_unavailable_source_stop_at_deadline(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found), busy=True)
        with patch("mdcattendance.scheduler._BUSY_RETRY_SECONDS", 0.01):
            await scheduler.tick(self.now)
            self.now = self.now.replace(hour=8, minute=30)
            await scheduler.tick(self.now)
            self.assertEqual(self.decision()["state"], "running")
            self.now = self.now.replace(hour=9, minute=0)
            await self.drain(timeout=10)
        self.assertEqual(self.decision()["state"], "missed")
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(self.notifier.texts("no automatic retry"))

    async def test_busy_owner_session_is_queued_not_failed(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        attempts = []

        async def queued_prepare(uid, profile, details, deadline):
            attempts.append(uid)
            if len(attempts) < 3:
                raise BusyRun("owner session active")
            self.prepared.append((uid, profile, dict(details)))
            return scheduler.base_cfg, StubOtp(), dict(NORMAL_ANSWERS)

        scheduler.prepare = queued_prepare
        with patch("mdcattendance.scheduler._BUSY_RETRY_SECONDS", 0.01):
            await scheduler.tick(self.now)
            self.now = self.now.replace(hour=8, minute=30)
            await scheduler.tick(self.now)
            await self.drain(timeout=10)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.decision()["state"], "confirmed")
        self.assertEqual(len(self.runner.calls), 1)

    async def test_unavailable_source_holds_then_misses_at_deadline(self):
        await self.onboard()
        records = StaticRecords(unavailable)
        scheduler = self.build(records=records)
        self.now = self.now.replace(hour=8, minute=30)
        await scheduler.tick(self.now)
        held = self.decision()
        self.assertEqual((held["state"], held["reason"]), ("held", "verification"))
        self.assertEqual(records.lookups, 2)
        self.now += timedelta(seconds=30)
        await scheduler.tick(self.now)
        self.assertEqual(self.decision()["revision"], held["revision"])
        self.assertEqual(records.lookups, 2)
        self.now += timedelta(seconds=30)
        await scheduler.tick(self.now)
        self.assertEqual(self.decision()["state"], "held")
        self.assertEqual(records.lookups, 3)
        self.now = self.now.replace(hour=9, minute=0)
        await scheduler.tick(self.now)
        self.assertEqual(self.decision()["state"], "missed")
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(self.notifier.texts("rechecking every minute"))

    async def test_missing_otp_credentials_suspend_automatic_with_one_warning(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found), with_user=False)
        await scheduler.tick(self.now)
        self.now = self.now.replace(hour=8, minute=30)
        await scheduler.tick(self.now)
        held = self.decision()
        self.assertEqual((held["state"], held["reason"]), ("held", "otp_unavailable"))
        warnings = self.notifier.texts("suspended")
        self.assertEqual(len(warnings), 1)
        self.advance(5)
        await scheduler.tick(self.now)
        self.assertEqual(len(self.notifier.texts("suspended")), 1)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.decision()["state"], "held")

    async def test_additional_submission_requires_reviewed_consent(self):
        await self.onboard()
        scheduler = self.build(
            script=[("failed", lambda day: found(day, checked_at=self.now))],
            records=StaticRecords(lambda day: found(day, checked_at=self.now)),
        )
        with patch(
            "mdcattendance.scheduler.answer_hash",
            lambda answers: hashlib.sha256(repr(sorted(answers)).encode()).hexdigest(),
        ):
            await scheduler.tick(self.now)
            recorded = self.decision()
            self.assertEqual(recorded["state"], "recorded")
            review = await scheduler.action(
                7, "additional_review", expected_revision=recorded["revision"]
            )
            self.assertFalse(review["consent"]["consumed"])
            confirmed = await scheduler.action(
                7,
                "additional_confirm",
                expected_revision=review["revision"],
                consent_digest=review["consent_digest"],
            )
            self.assertTrue(confirmed["consent"]["authorized"])
            self.assertFalse(confirmed["consent"]["consumed"])
            self.advance(30)
            await scheduler.tick(self.now)
            await self.drain()
        (context,) = [call.kwargs["consent"] for call in self.runner.calls]
        self.assertIsInstance(context, AdditionalSubmissionConsent)
        self.assertNotIsInstance(context, str)
        self.assertEqual((context.date.isoformat(), context.decision_uid), (DAY, 7))
        # The reviewed context was consumed with the attempt: reuse cannot authorise
        # a second submission.
        with self.assertRaises(DuplicateRun):
            self.store.prepare_consent(
                "synthetic-account", FORM, DAY, "submit", context
            )
        settled = self.decision()
        self.assertEqual(settled["state"], "recorded")
        self.assertIsNotNone(settled["attempt_id"])

    async def test_notification_failure_is_retried_without_duplicate_execution(self):
        await self.onboard()
        scheduler = self.build(script=[("confirmed", None)], records=StaticRecords(not_found))
        self.notifier.deliver = False
        await scheduler.tick(self.now)
        self.assertFalse(self.decision()["prompt_sent"])
        self.advance(1)
        await scheduler.tick(self.now)
        self.assertEqual(len(self.notifier.messages), 2)
        self.notifier.deliver = True
        self.advance(1)
        await scheduler.tick(self.now)
        self.assertTrue(self.decision()["prompt_sent"])
        self.assertEqual(self.runner.calls, [])
        self.advance(29)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.decision()["state"], "confirmed")

    async def test_today_view_and_lifecycle_lock(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        with self.assertRaises(RuntimeError):
            _ = scheduler.planner
        await scheduler.start()
        view = await scheduler.today(7, fresh=True)
        self.assertEqual(view["today"], DAY)
        self.assertEqual(view["decision"]["profile"], "normal")
        self.assertTrue(view["otp_ready"])
        self.assertEqual(view["observation"]["digest"], hashlib.sha256(b"empty").hexdigest())
        self.assertEqual(view["source_check"]["status"], "not_found")
        self.assertEqual(view["source_check"]["digest"], hashlib.sha256(b"empty").hexdigest())
        self.assertIsNone(view["local_outcome"])
        self.assertIsNone(view["stage"])
        self.assertEqual(view["next_action"], "automatic_submission")
        other = self.build(records=StaticRecords(not_found))
        with self.assertRaises(BusyRun):
            await other.start()
        await scheduler.stop()
        await other.start()
        await other.stop()
        self.assertEqual(self.runner.calls, [])

    async def test_today_exposes_truthful_owner_scoped_stage(self):
        await self.onboard()
        await self.onboard(uid=8, name="OTHER SYNTHETIC OWNER")
        scheduler = self.build(records=StaticRecords(not_found))
        scheduler._users = lambda: {
            7: SimpleNamespace(
                department=DEPARTMENT,
                otp_token="synthetic-otp-token",
                singpass_id="synthetic-account",
            ),
            8: SimpleNamespace(
                department=DEPARTMENT,
                otp_token="synthetic-otp-token",
                singpass_id="other-account",
            ),
        }
        await scheduler.start()
        self.assertIsNone((await scheduler.today(7))["stage"])
        # A due, explicitly queued decision waits for the single browser slot.
        decision = self.decision(7)
        ready = await scheduler.action(7, "submit_now", expected_revision=decision["revision"])
        self.assertEqual(ready["state"], "ready")
        self.assertEqual((await scheduler.today(7))["stage"], "waiting_for_browser")
        # Claimed but not yet inside the runner is still queued, and another owner's
        # in-flight progress must never leak into this owner's view.
        self.planner.claim_decision(7, DAY, expected_revision=ready["revision"])
        self.runner.stages[("other-account", FORM, DAY)] = "submitting"
        self.assertEqual((await scheduler.today(7))["stage"], "waiting_for_browser")
        # The runner's own truthful stage for this exact account/form/day is surfaced.
        self.runner.stages[("synthetic-account", FORM, DAY)] = "otp"
        self.assertEqual((await scheduler.today(7))["stage"], "otp")

    async def test_today_plan_save_replaces_the_delivered_prompt(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        prompted = self.decision()
        self.assertTrue(prompted["prompt_sent"])
        first_message = prompted["prompt_message_id"]
        await scheduler.save_plan(
            7,
            [{"date": DAY, "profile": "wfh", "details": {}}],
            expected_revision=self.planner.get_plan(7)["revision"],
        )
        updated = self.decision()
        self.assertEqual(updated["profile"], "wfh")
        self.assertTrue(updated["prompt_sent"])
        self.assertGreater(updated["prompt_message_id"], first_message)
        self.assertTrue(self.notifier.texts("Planned WFH"))


    async def test_manual_submitted_refreshes_evidence_without_asserting_success(self):
        await self.onboard()
        records = StaticRecords(not_found, unavailable)
        scheduler = self.build(records=records)
        await scheduler.tick(self.now)
        decision = self.decision()
        held = await scheduler.action(
            7, "manual_submitted", expected_revision=decision["revision"]
        )
        self.assertEqual((held["state"], held["reason"]), ("held", "manual_submitted"))
        self.assertEqual(records.lookups, 2)
        self.assertEqual(len(self.notifier.texts("could not be confirmed")), 1)
        self.assertEqual(self.runner.calls, [])


    async def test_today_source_check_surfaces_unavailable_without_rows(self):
        await self.onboard()
        scheduler = self.build(
            records=StaticRecords(
                lambda day: RecordCheck(
                    "unavailable", (), self.now, None, "record_date_format"
                )
            )
        )
        await scheduler.start()
        view = await scheduler.today(7, fresh=True)
        self.assertEqual(
            view["source_check"],
            {
                "status": "unavailable",
                "checked_at": self.now.isoformat(),
                "error_code": "record_date_format",
                "digest": None,
            },
        )

    async def test_today_ui_read_reuses_last_check_and_keeps_unavailable(self):
        await self.onboard()
        records = StaticRecords(
            lambda day: RecordCheck("unavailable", (), self.now, None, "network_error")
        )
        scheduler = self.build(records=records)
        await scheduler.start()
        first = await scheduler.today(7, fresh=True)
        self.assertEqual(first["source_check"]["status"], "unavailable")
        # A non-forced read must not rebuild the empty retained observation as
        # not_found, and it must not hit the source again inside the cache window.
        second = await scheduler.today(7)
        self.assertEqual(second["source_check"], first["source_check"])
        self.assertEqual(records.lookups, 1)

    async def test_same_valued_owner_aba_still_conflicts_during_the_source_read(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        before = self.decision()

        def aba(day):
            # The owner edits away and back while the read is in flight: profile,
            # details, execute_at, acknowledged and original all end up identical,
            # yet two owner revisions were consumed. Claiming the action on the newer
            # revision without re-review would silently rebase the owner's choice.
            held = self.planner.action(7, "hold", expected_revision=self.decision()["revision"])
            self.planner.action(7, "restore", expected_revision=held["revision"])
            return not_found(day, checked_at=self.now)

        scheduler.runner.records = StaticRecords(aba)
        with self.assertRaises(PlannerConflict):
            await scheduler.action(7, "submit_now", expected_revision=before["revision"])
        after = self.decision()
        self.assertEqual(after["state"], "awaiting")
        self.assertEqual(after["revision"], before["revision"] + 2)

    async def test_local_outcome_falls_back_to_the_same_day_journal(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        scheduler._users = lambda: {
            7: SimpleNamespace(
                department=DEPARTMENT,
                otp_token="synthetic-otp-token",
                singpass_id="synthetic-account",
            )
        }
        await scheduler.start()
        attempt = self.store.prepare("synthetic-account", FORM, self.now.date(), "submit")
        self.store.transition(attempt, "running")
        self.store.transition(attempt, "failed")
        self.assertIsNone(self.planner.ensure_decision(7)["attempt_id"])
        view = await scheduler.today(7)
        self.assertEqual(view["local_outcome"], "failed")

    async def test_plan_edit_cannot_overwrite_unbound_terminal_attempt(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.start()
        scheduler._users = lambda: {
            7: SimpleNamespace(
                department=DEPARTMENT,
                otp_token="synthetic-otp-token",
                singpass_id="synthetic-account",
            )
        }
        # A failed local attempt today is not a terminal outcome: the plan stays editable.
        failed = self.store.prepare("synthetic-account", FORM, self.now.date(), "submit")
        self.store.transition(failed, "running")
        self.store.transition(failed, "failed")
        revision = self.planner.get_plan(7)["revision"]
        await scheduler.save_plan(
            7, [{"date": DAY, "profile": "wfh", "details": {}}], expected_revision=revision
        )
        # A confirmed attempt today, never bound to a decision, blocks an ordinary overwrite.
        confirmed = self.store.prepare("synthetic-account", FORM, self.now.date(), "submit")
        self.store.transition(confirmed, "running")
        self.store.transition(confirmed, "submitting")
        self.store.transition(confirmed, "confirmed")
        self.assertIsNone(self.planner.ensure_decision(7)["attempt_id"])
        latest = self.planner.get_plan(7)["revision"]
        with self.assertRaises(PlannerBlocked):
            await scheduler.save_plan(
                7, [{"date": DAY, "profile": "normal", "details": {}}], expected_revision=latest
            )
        # Tomorrow's plan is unaffected by today's terminal outcome.
        tomorrow = (self.now.date() + timedelta(days=1)).isoformat()
        await scheduler.save_plan(
            7, [{"date": tomorrow, "profile": "normal", "details": {}}], expected_revision=latest
        )

    async def test_failed_reminder_is_retried_and_binds_the_latest_message(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        prompted = self.decision()
        self.assertEqual(prompted["prompt_message_id"], 1)
        self.advance(25)
        self.notifier.deliver = False
        await scheduler.tick(self.now)
        failed = self.decision()
        # A send that never reached the owner must not consume the reminder.
        self.assertFalse(failed["reminder_sent"])
        self.assertEqual(failed["prompt_message_id"], prompted["prompt_message_id"])
        self.notifier.deliver = True
        self.advance(1)
        await scheduler.tick(self.now)
        reminded = self.decision()
        self.assertTrue(reminded["reminder_sent"])
        self.assertEqual(reminded["revision"], failed["revision"])
        # Only the delivered reminder became the live prompt message.
        self.assertEqual(reminded["prompt_message_id"], len(self.notifier.messages))
        self.assertTrue(self.notifier.messages[-1].decision["_controls"])
        self.assertNotIn("_controls", self.planner.get_decision(7))

    async def test_paused_plan_prompt_is_truthful_and_never_runs(self):
        await self.onboard()
        decision = self.planner.ensure_decision(7)
        held = self.planner.action(7, "hold", expected_revision=decision["revision"])
        self.assertEqual((held["state"], held["reason"]), ("held", "user_edit"))
        scheduler = self.build(script=[("confirmed", None)], records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        self.assertTrue(self.decision()["prompt_sent"])
        self.assertTrue(self.notifier.texts("paused"))
        self.assertFalse(self.notifier.texts("automatic submission at 08:30"))
        self.now = self.now.replace(hour=8, minute=30)
        await scheduler.tick(self.now)
        await self.drain()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(
            (self.decision()["state"], self.decision()["reason"]), ("held", "user_edit")
        )

    async def test_midnight_rollover_expires_unfinished_only(self):
        await self.onboard()
        await self.onboard(uid=8, name="OTHER SYNTHETIC OWNER")
        await self.onboard(uid=9, name="THIRD SYNTHETIC OWNER")
        # A recorded outcome is terminal and must survive expiration untouched.
        self.planner.merge_observation(8, DAY, found(self.now.date()))
        self.assertEqual(self.planner.ensure_decision(8)["state"], "recorded")
        bounded = self.planner.ensure_decision(9)
        self.planner.update_decision(
            9, DAY, expected_revision=bounded["revision"], state="held", reason="verification"
        )
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        self.assertEqual(self.decision(7)["state"], "awaiting")
        self.now = self.now.replace(day=3)
        await scheduler.tick(self.now)
        self.assertEqual(
            (self.planner.get_decision(7, DAY)["state"], self.planner.get_decision(7, DAY)["reason"]),
            ("missed", "deadline"),
        )
        self.assertEqual(
            (
                self.planner.get_decision(9, DAY)["state"],
                self.planner.get_decision(9, DAY)["reason"],
            ),
            ("missed", "deadline"),
        )
        # A recorded outcome is never rewritten by expiration.
        self.assertEqual(self.planner.get_decision(8, DAY)["state"], "recorded")
        expired = self.notifier.texts("expired without being completed")
        self.assertEqual(len(expired), 2)
        await scheduler.tick(self.now)
        self.assertEqual(len(self.notifier.texts("expired without being completed")), 2)

    async def test_restart_expires_the_previous_day_once(self):
        await self.onboard()
        scheduler = self.build(records=StaticRecords(not_found))
        await scheduler.tick(self.now)
        self.assertEqual(self.decision()["state"], "awaiting")
        await scheduler.stop()
        self.now = self.now.replace(day=3)
        restarted = self.build(records=StaticRecords(not_found))
        self.assertTrue(await restarted.recover())
        row = self.planner.get_decision(7, DAY)
        self.assertEqual((row["state"], row["reason"]), ("missed", "deadline"))
        self.assertEqual(len(self.notifier.texts("expired without being completed")), 1)
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
