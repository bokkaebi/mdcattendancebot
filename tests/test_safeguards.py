"""Offline safeguards: real DOM read-back and durable execution journals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from mdcattendance.attendance import NORMAL_ANSWERS
from mdcattendance.bot import SINGPASS_HOST, _enter_otp, _wait_for_otp_page
from mdcattendance.config import Config
from mdcattendance.formfiller import fill_form, verify_form
from mdcattendance.records import RecordCheck, SubmissionRecord
from mdcattendance.runner import AttendanceRunner, DuplicateRun
from mdcattendance.storage import StateStore
from mdcattendance.telegram_bot import Interaction

SG = ZoneInfo("Asia/Singapore")
OWNER = "OFFLINE OWNER"
DEPARTMENT = "Offline Department"
# Pinned Singapore instant: durable submission success cases must not follow the
# host clock. A real run at 23:59:30 would clamp any same-day deadline into the past.
FIXED_NOW = datetime(2026, 10, 2, 8, 0, tzinfo=SG)
# Evidence timestamps share the pinned instant so checked_at can never sit ahead
# of the frozen planner clock.
FIXED_UTC = FIXED_NOW.astimezone(UTC)


class _GenuineDatetimeCheck(type):
    """Accept genuine datetime objects in isinstance(..., FixedRunnerDatetime).

    The stand-in subclasses datetime only to fake now(); production
    ``isinstance(value, datetime)`` checks must still recognise real values.
    """

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


class FixedRunnerDatetime(datetime, metaclass=_GenuineDatetimeCheck):
    """Runner clock pinned to FIXED_NOW; asyncio timeouts stay on real monotonic time."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


def absent_check() -> RecordCheck:
    return RecordCheck(
        "not_found",
        (),
        FIXED_UTC,
        hashlib.sha256(b"synthetic-absent").hexdigest(),
    )


def record_check(*records: SubmissionRecord) -> RecordCheck:
    digest = hashlib.sha256(
        json.dumps([record.to_dict() for record in records], sort_keys=True).encode()
    ).hexdigest()
    return RecordCheck(
        "found" if records else "not_found", tuple(records), FIXED_UTC, digest
    )


class SyntheticRecords:
    """Injectable owner-evidence reader; the last supplied check repeats."""

    def __init__(self, *checks: RecordCheck):
        self.checks = list(checks)
        self.calls: list[tuple[str, str, bool]] = []

    async def lookup(self, name, department, day, *, fresh=False):  # noqa: ANN001, ANN201
        self.calls.append((name, department, fresh))
        if not self.checks:
            raise AssertionError("unexpected extra source lookup")
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]


class FormReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        executable = os.environ.get("CHROMIUM_EXECUTABLE_PATH")
        browser_path = executable or self.playwright.chromium.executable_path
        if not Path(browser_path).is_file():
            self.skipTest(
                "Chromium unavailable: install with `uv run playwright install chromium` "
                "or set CHROMIUM_EXECUTABLE_PATH"
            )
        self.browser = await self.playwright.chromium.launch(
            headless=True, chromium_sandbox=True, executable_path=executable
        )
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(1000)
        await self.page.set_content("""
            <fieldset role="radiogroup" aria-label="Status">
              <label><input type="radio" name="status" value="Present">Present</label>
              <label><input type="radio" name="status" value="Absent">Absent</label>
            </fieldset>
            <label>Remarks<input aria-label="Remarks"></label>
            <fieldset aria-label="Duties">
              <label><input type="checkbox">Office</label>
              <label><input type="checkbox">Field</label>
            </fieldset>
        """)
        self.answers = {"Status": "Present", "Remarks": "NIL", "Duties": ["Office"]}

    async def test_missing_answer_prevents_complete_fill_and_readback(self):
        answers = {**self.answers, "Required missing question": "NIL"}
        with (
            patch("mdcattendance.formfiller.FIELD_REVEAL_DELAY_MS", 0),
            patch("mdcattendance.formfiller.MAX_STALLED_PASSES", 1),
            self.assertRaises(RuntimeError),
        ):
            await fill_form(self.page, answers)
        with self.assertRaises(RuntimeError):
            await verify_form(self.page, answers)

    async def test_mutated_radio_text_and_checkbox_are_rejected_without_repair(self):
        mutations = [
            ('input[value="Absent"]', "checked", True),
            ('input[aria-label="Remarks"]', "value", "changed"),
            ('input[type="checkbox"]', "checked", False),
        ]
        for selector, property_name, value in mutations:
            with self.subTest(field=selector):
                with patch("mdcattendance.formfiller.FIELD_REVEAL_DELAY_MS", 0):
                    await fill_form(self.page, self.answers)
                await verify_form(self.page, self.answers)
                await self.page.locator(selector).first.evaluate(
                    "(element, change) => { element[change[0]] = change[1]; }",
                    [property_name, value],
                )
                with self.assertRaises(RuntimeError):
                    await verify_form(self.page, self.answers)
                self.assertEqual(
                    await self.page.locator(selector).first.evaluate(
                        "(element, key) => element[key]", property_name
                    ),
                    value,
                )

    async def test_reported_singpass_otp_label_without_autocomplete_is_supported(self):
        await self.page.route(
            f"https://{SINGPASS_HOST}/**",
            lambda route: route.fulfill(
                content_type="text/html",
                body="""
                    <input type="hidden" value="unchanged">
                    <input type="tel" maxlength="6" aria-label="Enter 6-digit OTP code">
                    <input type="tel" maxlength="6" aria-label="Phone number">
                """,
            ),
        )
        await self.page.goto(f"https://{SINGPASS_HOST}/offline-otp")
        await _wait_for_otp_page(self.page, timeout_ms=1000)
        await _enter_otp(self.page, "123456")
        self.assertEqual(
            await self.page.get_by_label("Enter 6-digit OTP code", exact=True).input_value(),
            "123456",
        )
        self.assertEqual(await self.page.get_by_label("Phone number").input_value(), "")
        self.assertEqual(await self.page.locator('input[type="hidden"]').input_value(), "unchanged")
        await self.page.get_by_label("Enter 6-digit OTP code", exact=True).evaluate(
            "(input) => input.removeAttribute('aria-label')"
        )
        with self.assertRaises(RuntimeError):
            await _wait_for_otp_page(self.page, timeout_ms=300)
        with self.assertRaises(RuntimeError):
            await _enter_otp(self.page, "654321")


class DurableRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = Config(
            singpass_id="offline-test-account",
            singpass_password="not-a-credential",
            form_url="https://example.invalid/form",
            state_dir=self.directory.name,
            attendance_deadline="23:59",
        )
        self.store = StateStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.records = SyntheticRecords(absent_check())
        self.runner = AttendanceRunner(self.cfg, self.store, self.records)
        self.addAsyncCleanup(self.runner.shutdown)
        runner_clock = patch("mdcattendance.runner.datetime", FixedRunnerDatetime)
        runner_clock.start()
        self.addCleanup(runner_clock.stop)
        self.day = FIXED_NOW.date()
        self.deadline = datetime.combine(self.day, time(9, 0), SG)

    async def test_confirmed_and_unknown_survive_restart_and_block_replay(self):
        for outcome in ("confirmed", "unknown"):
            with self.subTest(outcome=outcome):
                cfg = replace(self.cfg, form_url=f"https://example.invalid/{outcome}")
                calls = []
                observed = []

                async def executor(
                    cfg,
                    otp,
                    answers,
                    *,
                    before_submit,
                    on_stage=None,
                    outcome=outcome,
                    calls=calls,
                    observed=observed,
                ):
                    calls.append("execute")
                    await before_submit()
                    attempt = max(self.store.attempts(), key=lambda row: row["id"])
                    observed.append(attempt["status"])
                    if outcome == "unknown":
                        raise RuntimeError("confirmation unavailable")
                    return "confirmed"

                with patch("mdcattendance.runner.run_flow", executor):
                    result = await self.runner.run(
                        cfg,
                        None,
                        dict(NORMAL_ANSWERS),
                        attendance_name=OWNER,
                        department=DEPARTMENT,
                        deadline=self.deadline,
                    )
                    self.assertEqual(result.status, outcome)
                    self.assertEqual(observed, ["submitting"])
                    self.assertEqual(self.store.get_attempt(result.attempt_id)["status"], outcome)
                    reopened = StateStore(self.directory.name)
                    restarted = AttendanceRunner(cfg, reopened, SyntheticRecords(absent_check()))
                    try:
                        with self.assertRaises(DuplicateRun):
                            await restarted.run(
                                cfg,
                                None,
                                dict(NORMAL_ANSWERS),
                                attendance_name=OWNER,
                                department=DEPARTMENT,
                                deadline=self.deadline,
                            )
                        self.assertEqual(reopened.get_attempt(result.attempt_id)["status"], outcome)
                    finally:
                        await restarted.shutdown()
                        reopened.close()
                self.assertEqual(calls, ["execute"])

    async def test_timeout_and_cancellation_record_submission_boundary(self):
        for submitted in (False, True):
            for cancel in (False, True):
                with self.subTest(submitted=submitted, cancel=cancel):
                    cfg = replace(
                        self.cfg,
                        form_url=f"https://example.invalid/{submitted}/{cancel}",
                        run_timeout=0.05 if not cancel else 10,
                    )
                    entered = asyncio.Event()
                    calls = []

                    async def executor(
                        cfg,
                        otp,
                        answers,
                        *,
                        before_submit,
                        on_stage=None,
                        submitted=submitted,
                        calls=calls,
                        entered=entered,
                    ):
                        calls.append("execute")
                        if submitted:
                            await before_submit()
                        entered.set()
                        await asyncio.Event().wait()

                    with patch("mdcattendance.runner.run_flow", executor):
                        task = asyncio.create_task(
                            self.runner.run(
                                cfg,
                                None,
                                dict(NORMAL_ANSWERS),
                                attendance_name=OWNER,
                                department=DEPARTMENT,
                                deadline=self.deadline,
                            )
                        )
                        await asyncio.wait_for(entered.wait(), 2)
                        if cancel:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        else:
                            result = await asyncio.wait_for(task, 2)
                            self.assertEqual(result.status, "unknown" if submitted else "failed")
                    attempt = max(self.store.attempts(), key=lambda row: row["id"])
                    self.assertEqual(attempt["status"], "unknown" if submitted else "failed")
                    self.assertFalse(self.runner.busy)
                    self.assertEqual(calls, ["execute"])

    async def test_interrupted_submission_recovers_unknown_not_replayed(self):
        attempt = self.store.prepare(
            self.cfg.singpass_id,
            self.cfg.form_url,
            self.day.isoformat(),
            "submit",
        )
        self.store.transition(attempt, "running")
        self.store.transition(attempt, "submitting")
        with (
            patch("mdcattendance.runner.run_flow", side_effect=AssertionError("must not replay")),
            self.assertRaises(DuplicateRun),
        ):
            await self.runner.run(
                self.cfg,
                None,
                dict(NORMAL_ANSWERS),
                attendance_name=OWNER,
                department=DEPARTMENT,
                deadline=self.deadline,
            )
        self.assertEqual(self.store.get_attempt(attempt)["status"], "unknown")


class OtpOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_owner_old_prompt_and_replaced_run_cannot_deliver(self):
        sent = asyncio.Queue()
        counter = 100

        async def send_message(*args, **kwargs):
            nonlocal counter
            counter += 1
            message = SimpleNamespace(message_id=counter)
            await sent.put(message)
            return message

        state = SimpleNamespace(interactions={})
        app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        interaction = Interaction(7, state, app)
        state.interactions[7] = interaction
        first = asyncio.create_task(interaction.ask_text("OTP", "otp", 2))
        prompt = await asyncio.wait_for(sent.get(), 1)
        # send_message returns before the prompt identity is assigned.
        await asyncio.sleep(0)
        self.assertFalse(
            interaction.deliver_otp(
                "123456", user_id=8, chat_id=7, reply_to_message_id=prompt.message_id
            )
        )
        self.assertFalse(
            interaction.deliver_otp(
                "123456", user_id=7, chat_id=8, reply_to_message_id=prompt.message_id
            )
        )
        self.assertFalse(
            interaction.deliver_otp(
                "123456", user_id=7, chat_id=7, reply_to_message_id=prompt.message_id - 1
            )
        )
        self.assertFalse(first.done())
        interaction.clear_pending()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(interaction.ask_text("New OTP", "otp", 2))
        current = await asyncio.wait_for(sent.get(), 1)
        await asyncio.sleep(0)
        self.assertFalse(
            interaction.deliver_otp(
                "123456", user_id=7, chat_id=7, reply_to_message_id=prompt.message_id
            )
        )
        with self.assertRaises(ValueError):
            interaction.deliver_otp(
                "１２３４５６", user_id=7, chat_id=7, reply_to_message_id=current.message_id
            )
        self.assertFalse(second.done())
        state.interactions[7] = Interaction(7, state, app)
        self.assertFalse(
            interaction.deliver_otp(
                "123456", user_id=7, chat_id=7, reply_to_message_id=current.message_id
            )
        )
        state.interactions[7] = interaction
        self.assertTrue(
            interaction.deliver_otp(
                "123456", user_id=7, chat_id=7, reply_to_message_id=current.message_id
            )
        )
        self.assertEqual(await second, "123456")
        self.assertFalse(
            interaction.deliver_otp(
                "123456", user_id=7, chat_id=7, reply_to_message_id=current.message_id
            )
        )


if __name__ == "__main__":
    unittest.main()
