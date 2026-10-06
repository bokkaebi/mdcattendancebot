"""One submission gate: shared local journal and fresh owner-evidence checks."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mdcattendance.attendance import NORMAL_ANSWERS, WFH_ANSWERS
from mdcattendance.config import Config
from mdcattendance.records import RecordCheck, SubmissionRecord
from mdcattendance.runner import AttendanceRunner, DuplicateRun, answer_hash
from mdcattendance.schedules import Planner
from mdcattendance.storage import SINGAPORE, AdditionalSubmissionConsent, StateStore

OWNER = "GATE OWNER"
DEPARTMENT = "Gate Department"
UID = 7
# Pinned Singapore instant for the whole suite. The gate's "today" and cutoff must
# not follow the host clock: a real run at 23:59:30 would clamp the deadline into
# the past and turn every success case into a closed window.
SYNTHETIC_NOW = datetime(2026, 10, 2, 8, 0, tzinfo=SINGAPORE)
# Same pinned instant for every evidence timestamp: checked_at must never sit
# ahead of the frozen planner clock (a positive-age check would reject it).
SYNTHETIC_UTC = SYNTHETIC_NOW.astimezone(UTC)


def absent() -> RecordCheck:
    return RecordCheck(
        "not_found", (), SYNTHETIC_UTC, hashlib.sha256(b"gate-absent").hexdigest()
    )


def unavailable(code: str = "http_error") -> RecordCheck:
    return RecordCheck("unavailable", (), SYNTHETIC_UTC, None, code)


def record(status: str = "Present (Infinite Studios - IS)") -> SubmissionRecord:
    return SubmissionRecord(
        datetime(2026, 10, 2, 0, 32, tzinfo=SINGAPORE),
        status,
        (("Remarks (NSC/IS)", "NIL"),),
    )


def present(*records: SubmissionRecord) -> RecordCheck:
    rows = tuple(records)
    digest = hashlib.sha256(
        json.dumps([row.to_dict() for row in rows], sort_keys=True).encode()
    ).hexdigest()
    return RecordCheck("found", rows, SYNTHETIC_UTC, digest)


class GateRecords:
    """Owner-evidence stand-in; checks are consumed in order, the last one repeats."""

    def __init__(self, *checks: RecordCheck) -> None:
        self.checks = list(checks)
        self.calls: list[tuple[str, str, bool]] = []

    async def lookup(self, name, department, day, *, fresh=False):  # noqa: ANN001, ANN201
        self.calls.append((name, department, fresh))
        if not self.checks:
            raise AssertionError("unexpected extra source lookup")
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]


class _DatetimeIdentityMeta(type):
    """Make the frozen stand-in a drop-in for the real datetime type.

    Production code validates evidence with ``isinstance(value, datetime)``.
    The stand-in subclasses datetime only to fake ``now()``; without forwarding
    the check, every genuine datetime object would look foreign and be rejected.
    """

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


class FrozenClock:
    """Fix the runner and planner clocks so deadline boundaries are deterministic."""

    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __enter__(self) -> FrozenClock:
        clock = self

        class FrozenDatetime(datetime, metaclass=_DatetimeIdentityMeta):
            @classmethod
            def now(cls, tz=None):
                return clock.instant.astimezone(tz) if tz else clock.instant.replace(tzinfo=None)

        self._patches = (
            patch("mdcattendance.runner.datetime", FrozenDatetime),
            patch("mdcattendance.schedules.datetime", FrozenDatetime),
        )
        for patcher in self._patches:
            patcher.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        for patcher in reversed(self._patches):
            patcher.stop()
        return False

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


class SubmissionGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = Config(
            singpass_id="gate-account",
            singpass_password="not-a-credential",
            form_url="https://example.invalid/gate",
            state_dir=self.directory.name,
            attendance_deadline="23:59",
        )
        self.store = StateStore(self.directory.name)
        self.addCleanup(self.store.close)
        # One pinned clock for the whole suite: "today", the cutoff and every
        # caller-supplied deadline stay on the same synthetic Singapore instant.
        self.clock = self.enterContext(FrozenClock(SYNTHETIC_NOW))
        self.day = SYNTHETIC_NOW.date()
        self.deadline = datetime.combine(self.day, time(9, 0), SINGAPORE)

    def make_runner(self, *checks: RecordCheck) -> tuple[AttendanceRunner, GateRecords]:
        records = GateRecords(*checks)
        runner = AttendanceRunner(self.cfg, self.store, records)
        self.addAsyncCleanup(runner.shutdown)
        return runner, records

    def prior_attempt(self, status: str = "failed") -> int:
        attempt = self.store.prepare(
            self.cfg.singpass_id, self.cfg.form_url, self.day, "submit"
        )
        self.store.transition(attempt, "running")
        if status in {"unknown", "confirmed"}:
            self.store.transition(attempt, "submitting")
        self.store.transition(attempt, status)
        return attempt

    def running_decision(self, uid: int = UID) -> None:
        """A claimed decision bound to nothing yet, revision 0."""
        self.store.upgrade_planner(attendance_deadline=self.cfg.attendance_deadline)
        self.store._db.execute(
            """INSERT INTO daily_decisions (uid,day,state,execute_at,updated_at)
               VALUES (?,?,'running',?,?)""",
            (
                uid,
                self.day.isoformat(),
                f"{self.day.isoformat()}T08:30:00+08:00",
                datetime.now(UTC).isoformat(),
            ),
        )
        self.store._db.commit()

    async def test_new_record_during_otp_aborts_before_the_click(self):
        runner, records = self.make_runner(absent(), present(record()))
        clicked = []

        async def flow(cfg, otp, answers, *, before_submit, on_stage=None):
            await before_submit()
            clicked.append("submitted")
            return "confirmed"

        with patch("mdcattendance.runner.run_flow", flow):
            result = await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                deadline=self.deadline,
            )
        self.assertEqual(clicked, [])
        self.assertEqual(result.status, "recorded")
        self.assertEqual(result.detail, "existing attendance found")
        self.assertEqual(result.record_check.status, "found")
        self.assertIsNotNone(result.attempt_id)
        self.assertEqual(self.store.get_attempt(result.attempt_id)["status"], "failed")
        self.assertEqual(len(records.calls), 2)

    async def test_unknown_local_attempt_blocks_even_with_an_empty_source(self):
        attempt = self.prior_attempt("unknown")
        runner, records = self.make_runner(absent())
        with (
            patch("mdcattendance.runner.run_flow", side_effect=AssertionError("no browser")),
            self.assertRaises(DuplicateRun),
        ):
            await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                deadline=self.deadline,
            )
        self.assertEqual(self.store.get_attempt(attempt)["status"], "unknown")
        self.assertEqual(records.calls, [(OWNER, DEPARTMENT, True)])

    async def test_unavailable_source_blocks_before_any_attempt_or_login(self):
        runner, _ = self.make_runner(unavailable())
        with patch("mdcattendance.runner.run_flow", side_effect=AssertionError("no browser")):
            result = await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                deadline=self.deadline,
            )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.detail, "external source unavailable")
        self.assertEqual(result.record_check.error_code, "http_error")
        self.assertEqual(
            self.store.submission_attempts(self.cfg.singpass_id, self.cfg.form_url, self.day),
            [],
        )

    async def test_submit_without_identity_is_blocked_before_any_attempt(self):
        runner, records = self.make_runner(absent())
        with patch("mdcattendance.runner.run_flow", side_effect=AssertionError("no browser")):
            result = await runner.run(self.cfg, None, dict(NORMAL_ANSWERS))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.detail, "attendance identity required")
        self.assertIsNone(result.attempt_id)
        self.assertEqual(records.calls, [])
        self.assertEqual(
            self.store.submission_attempts(self.cfg.singpass_id, self.cfg.form_url, self.day),
            [],
        )

    async def test_consent_rejects_changed_context_and_reuse(self):
        prior = self.prior_attempt()
        answers = dict(NORMAL_ANSWERS)
        check = present(record())
        consent = self.store.authorize_cli_consent(
            self.cfg.singpass_id,
            self.cfg.form_url,
            self.day,
            answer_hash=answer_hash(answers),
            records_digest=check.digest,
            local_attempt_ids=[prior],
        )
        self.assertIsInstance(consent, AdditionalSubmissionConsent)

        async def flow(cfg, otp, answers, *, before_submit, on_stage=None):
            await before_submit()
            return "confirmed"

        async def rejected(
            *, run_answers, consent_obj, records_check, decision_uid=None
        ) -> None:
            runner, _ = self.make_runner(records_check)
            with (
                patch("mdcattendance.runner.run_flow", flow),
                self.assertRaises(DuplicateRun),
            ):
                await runner.run(
                    self.cfg,
                    None,
                    run_answers,
                    attendance_name=OWNER,
                    department=DEPARTMENT,
                    consent=consent_obj,
                    deadline=self.deadline,
                    decision_uid=decision_uid,
                )

        # Changed answers, changed source evidence, a stale date, unreviewed attempt
        # ids and a foreign decision owner are all rejected before any browser work.
        await rejected(run_answers=dict(WFH_ANSWERS), consent_obj=consent, records_check=check)
        await rejected(
            run_answers=answers,
            consent_obj=consent,
            records_check=present(record("Changed (IS)")),
        )
        await rejected(
            run_answers=answers,
            consent_obj=replace(consent, date=self.day - timedelta(days=1)),
            records_check=check,
        )
        await rejected(
            run_answers=answers,
            consent_obj=replace(consent, local_attempt_ids=(999,)),
            records_check=check,
        )
        await rejected(
            run_answers=answers,
            consent_obj=replace(consent, decision_uid=UID),
            records_check=check,
        )

        accepted, _ = self.make_runner(check)
        with patch("mdcattendance.runner.run_flow", flow):
            result = await accepted.run(
                self.cfg,
                None,
                answers,
                attendance_name=OWNER,
                department=DEPARTMENT,
                consent=consent,
                deadline=self.deadline,
            )
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(self.store.get_attempt(result.attempt_id)["status"], "confirmed")

        reused, _ = self.make_runner(check)
        with (
            patch("mdcattendance.runner.run_flow", flow),
            self.assertRaises(DuplicateRun),
        ):
            await reused.run(
                self.cfg,
                None,
                answers,
                attendance_name=OWNER,
                department=DEPARTMENT,
                consent=consent,
                deadline=self.deadline,
            )

    async def test_consent_rejects_a_bare_token_string(self):
        prior = self.prior_attempt()
        check = present(record())
        consent = self.store.authorize_cli_consent(
            self.cfg.singpass_id,
            self.cfg.form_url,
            self.day,
            answer_hash=answer_hash(dict(NORMAL_ANSWERS)),
            records_digest=check.digest,
            local_attempt_ids=[prior],
        )
        runner, _ = self.make_runner(check)
        with (
            patch("mdcattendance.runner.run_flow", side_effect=AssertionError("no browser")),
            self.assertRaises(ValueError),
        ):
            await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                consent=consent.consent_digest,
                deadline=self.deadline,
            )

    async def test_deadline_is_rechecked_after_the_network_await(self):
        # Independent clock instance on top of the suite clock: advancing past the
        # callback deadline must still deliberately reject the click.
        clock = FrozenClock(SYNTHETIC_NOW)
        runner, _ = self.make_runner(absent())

        async def flow(cfg, otp, answers, *, before_submit, on_stage=None):
            clock.advance(60)
            await before_submit()
            return "confirmed"

        with clock, patch("mdcattendance.runner.run_flow", flow):
            result = await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_date=self.day,
                attendance_name=OWNER,
                department=DEPARTMENT,
                deadline=clock.instant + timedelta(seconds=30),
            )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.detail, "attendance window closed")
        self.assertIsNotNone(result.attempt_id)
        self.assertEqual(self.store.get_attempt(result.attempt_id)["status"], "failed")

    async def test_decision_bound_positive_observation_survives_disappearance(self):
        self.store.upgrade_planner(attendance_deadline="09:00")
        # Legacy-style fixture: a claimed decision, inserted directly.
        self.store._db.execute(
            """INSERT INTO daily_decisions (uid,day,state,execute_at,updated_at)
               VALUES (?,?,'running',?,?)""",
            (
                UID,
                self.day.isoformat(),
                f"{self.day.isoformat()}T08:30:00+08:00",
                datetime.now(UTC).isoformat(),
            ),
        )
        self.store._db.commit()
        runner, _ = self.make_runner(present(record()))
        with patch("mdcattendance.runner.run_flow", side_effect=AssertionError("no browser")):
            result = await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                decision_uid=UID,
                deadline=self.deadline,
            )
        self.assertEqual(result.status, "recorded")
        self.assertIsNone(result.attempt_id)
        planner = Planner(self.store, "09:00")
        self.assertTrue(planner.get_observation(UID, self.day)["records"])
        # A later export that omits the row must not clear the local blocker.
        planner.merge_observation(UID, self.day, absent())
        observation = planner.get_observation(UID, self.day)
        self.assertTrue(observation["records"])
        self.assertEqual(observation["records"][0]["status"], "Present (Infinite Studios - IS)")

    async def test_ordinary_run_binds_the_attempt_to_the_decision_before_login(self):
        self.running_decision()
        runner, _ = self.make_runner(absent(), absent())
        planner = Planner(self.store, self.cfg.attendance_deadline)
        seen: dict = {}

        async def flow(cfg, otp, answers, *, before_submit, on_stage=None):
            bound = planner.get_decision(UID, self.day)
            seen["attempt_id"] = bound["attempt_id"]
            seen["attempt_status"] = self.store.get_attempt(bound["attempt_id"])["status"]
            await before_submit()
            return "confirmed"

        with patch("mdcattendance.runner.run_flow", flow):
            result = await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                decision_uid=UID,
                decision_revision=0,
                deadline=self.deadline,
            )
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(seen["attempt_id"], result.attempt_id)
        self.assertEqual(seen["attempt_status"], "running")
        self.assertEqual(
            planner.get_decision(UID, self.day)["attempt_id"], result.attempt_id
        )

    async def test_binding_without_a_decision_revision_aborts_before_login(self):
        self.running_decision()
        runner, _ = self.make_runner(absent())
        # A decision-bound submit that lost its reviewed revision is blocked before any
        # browser work rather than being cast to a guessed revision.
        with self.assertRaises(RuntimeError) as caught:
            runner._bind_attempt(self.cfg, UID, self.day, 1, None)
        self.assertEqual(caught.exception.status, "blocked")
        self.assertEqual(caught.exception.detail, "decision revision missing before submission")

    async def test_retained_positive_suppresses_an_empty_current_export(self):
        self.running_decision()
        planner = Planner(self.store, self.cfg.attendance_deadline)
        planner.merge_observation(UID, self.day, present(record()))
        runner, records = self.make_runner(absent())
        with patch("mdcattendance.runner.run_flow", side_effect=AssertionError("no browser")):
            result = await runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                decision_uid=UID,
                deadline=self.deadline,
            )
        self.assertEqual(result.status, "recorded")
        self.assertEqual(result.detail, "existing attendance found")
        self.assertIsNone(result.attempt_id)
        self.assertEqual(records.calls, [(OWNER, DEPARTMENT, True)])
        self.assertTrue(planner.get_observation(UID, self.day)["records"])

    async def test_ordinary_interrupted_submitting_decision_recovers_unknown(self):
        self.running_decision()
        attempt = self.store.prepare(
            self.cfg.singpass_id, self.cfg.form_url, self.day, "submit"
        )
        self.store.transition(attempt, "running")
        self.store.transition(attempt, "submitting")
        planner = Planner(self.store, self.cfg.attendance_deadline)
        planner.bind_attempt(UID, self.day, attempt, expected_revision=0)
        self.store.recover(attendance_deadline=self.cfg.attendance_deadline)
        self.assertEqual(planner.get_decision(UID, self.day)["state"], "unknown")
        self.assertEqual(self.store.get_attempt(attempt)["status"], "unknown")

    async def test_non_submit_modes_need_no_identity_or_source_lookup(self):
        runner, records = self.make_runner(absent())
        for mode, expected in (
            ("preflight", "preflight"),
            ("discover", "discovered"),
            ("dry_run", "dry_run"),
        ):
            with self.subTest(mode=mode):
                cfg = replace(self.cfg, **{mode: True})

                async def flow(cfg, otp, answers, *, expected=expected, before_submit, on_stage=None):
                    return expected

                with patch("mdcattendance.runner.run_flow", flow):
                    result = await runner.run(cfg, None, dict(NORMAL_ANSWERS))
                self.assertEqual(result.status, expected)
                self.assertIsNotNone(result.attempt_id)
                attempt = self.store.get_attempt(result.attempt_id)
                self.assertEqual(attempt["mode"], mode)
        self.assertEqual(records.calls, [])


if __name__ == "__main__":
    unittest.main()
