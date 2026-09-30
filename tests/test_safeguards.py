"""Offline safeguards: real DOM read-back and durable execution journals."""

from __future__ import annotations

import asyncio
import os
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from mdcattendance.attendance import NORMAL_ANSWERS
from mdcattendance.config import Config
from mdcattendance.formfiller import fill_form, verify_form
from mdcattendance.runner import AttendanceRunner, DuplicateRun
from mdcattendance.scheduler import Scheduler
from mdcattendance.schedules import save_schedule
from mdcattendance.storage import StateStore
from mdcattendance.telegram_bot import Interaction

SG = ZoneInfo("Asia/Singapore")


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


class DurableRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = Config(
            singpass_id="offline-test-account",
            singpass_password="not-a-credential",
            form_url="https://example.invalid/form",
            state_dir=self.directory.name,
        )
        self.store = StateStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.runner = AttendanceRunner(self.cfg, self.store)
        self.addAsyncCleanup(self.runner.shutdown)

    async def test_confirmed_and_unknown_survive_restart_and_block_replay(self):
        for outcome in ("confirmed", "unknown"):
            with self.subTest(outcome=outcome):
                cfg = replace(self.cfg, form_url=f"https://example.invalid/{outcome}")
                calls = []
                observed = []

                async def executor(
                    cfg, otp, answers, *, before_submit, outcome=outcome, calls=calls, observed=observed
                ):
                    calls.append("execute")
                    await before_submit()
                    attempt = max(self.store.attempts(), key=lambda row: row["id"])
                    observed.append(attempt["status"])
                    if outcome == "unknown":
                        raise RuntimeError("confirmation unavailable")
                    return "confirmed"

                with patch("mdcattendance.runner.run_flow", executor):
                    result = await self.runner.run(cfg, None, dict(NORMAL_ANSWERS))
                    self.assertEqual(result.status, outcome)
                    self.assertEqual(observed, ["submitting"])
                    self.assertEqual(self.store.get_attempt(result.attempt_id)["status"], outcome)
                    reopened = StateStore(self.directory.name)
                    restarted = AttendanceRunner(cfg, reopened)
                    try:
                        with self.assertRaises(DuplicateRun):
                            await restarted.run(cfg, None, dict(NORMAL_ANSWERS))
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
                        task = asyncio.create_task(self.runner.run(cfg, None, dict(NORMAL_ANSWERS)))
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
            datetime.now(SG).date().isoformat(),
            "submit",
        )
        self.store.transition(attempt, "running")
        self.store.transition(attempt, "submitting")
        with (
            patch("mdcattendance.runner.run_flow", side_effect=AssertionError("must not replay")),
            self.assertRaises(DuplicateRun),
        ):
            await self.runner.run(self.cfg, None, dict(NORMAL_ANSWERS))
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


class ScheduleJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_replan_and_restart_preserve_completed_dispatch(self):
        with TemporaryDirectory() as directory:
            cfg = Config(
                singpass_id="offline-schedule-account",
                singpass_password="not-a-credential",
                form_url="https://example.invalid/scheduled",
                state_dir=directory,
                attendance_deadline="23:59",
            )
            store = StateStore(directory)
            runner = AttendanceRunner(cfg, store)
            now = datetime.now(SG).replace(hour=8, minute=31, second=0, microsecond=0)

            class FrozenDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return now.astimezone(tz) if tz else now.replace(tzinfo=None)

            self.enterContext(patch("mdcattendance.scheduler.datetime", FrozenDatetime))
            day = now.date().isoformat()
            save_schedule(store.path, 7, time="00:00", days={day: "normal"})
            prepared = []
            terminal = asyncio.Event()

            async def prepare(uid, status, deadline):
                prepared.append((uid, status))
                return cfg, None, dict(NORMAL_ANSWERS)

            async def notify(uid, text):
                dispatch = store.get_schedule_dispatch(uid, day)
                if dispatch and dispatch["status"] != "collecting":
                    terminal.set()

            async def executor(cfg, otp, answers, *, before_submit):
                await before_submit()
                return "confirmed"

            scheduler = Scheduler(store, runner, cfg, prepare, notify)
            try:
                with patch("mdcattendance.runner.run_flow", executor):
                    await scheduler.tick(now)
                    await asyncio.wait_for(terminal.wait(), 2)
                    save_schedule(store.path, 7, time="00:01", days={day: "wfh"})
                    scheduler.replan()
                    await scheduler.tick(now)
                    await scheduler.stop()
                self.assertEqual(prepared, [(7, "normal")])
                self.assertEqual(store.get_schedule_dispatch(7, day)["status"], "confirmed")
                save_schedule(store.path, 8, time="00:00", days={day: "normal"})
                self.assertTrue(store.claim_schedule(8, day))
                await runner.shutdown()
                store.close()
                store = StateStore(directory)
                runner = AttendanceRunner(cfg, store)
                scheduler = Scheduler(store, runner, cfg, prepare, notify)
                await scheduler.tick(now)
                await scheduler.stop()
                self.assertEqual(prepared, [(7, "normal")])
                self.assertEqual(store.get_schedule_dispatch(7, day)["status"], "confirmed")
                self.assertEqual(store.get_schedule_dispatch(8, day)["status"], "missed")
            finally:
                await scheduler.stop()
                await runner.shutdown()
                store.close()


if __name__ == "__main__":
    unittest.main()
