"""Focused Telegram workflow regressions: onboarding, buttons, OTP warning, launchers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from mdcattendance import telegram_bot as tb
from mdcattendance.attendance import DEPARTMENT, mc_answers
from mdcattendance.config import Config
from mdcattendance.runner import AttendanceRunner, DuplicateRun, RunResult
from mdcattendance.scheduler import Scheduler
from mdcattendance.schedules import Planner
from mdcattendance.storage import SINGAPORE, AdditionalSubmissionConsent, StateStore
from mdcattendance.users import User

UID = 123
TODAY = datetime(2026, 10, 2, 8, 0, tzinfo=SINGAPORE)


class _FrozenDatetime(datetime):
    """Clock-independent stand-in for the Telegram module's datetime.now()."""

    @classmethod
    def now(cls, tz=None):
        return TODAY.astimezone(tz) if tz is not None else TODAY.replace(tzinfo=None)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []

    async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        message = SimpleNamespace(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            reply_to=reply_to_message_id,
            message_id=len(self.messages) + 1,
        )
        self.messages.append(message)
        return message


def make_users(directory: str, entries: dict[int, dict]) -> str:
    path = os.path.join(directory, "users.json")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(entries).encode())
    finally:
        os.close(fd)
    return path


def user_record() -> dict:
    return {"singpass_id": "S1234567A", "singpass_password": "secret", "department": DEPARTMENT}


class FakeQuery:
    def __init__(self, message_id: int) -> None:
        self.message = SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=UID))
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)


class FakeRecords:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def verify_source(self) -> bool:
        if self.fail:
            raise RuntimeError("synthetic source failure")
        return True


class FakeLookupRecords(FakeRecords):
    def __init__(self, check) -> None:
        super().__init__()
        self.check = check

    async def lookup(self, name, department, day, *, fresh=False):
        return self.check


class ReviewRunner:
    """Deterministic stand-in for the runner's reviewed-submission path.

    It mirrors the real runner's durable boundary: consent is consumed and the
    attempt is bound atomically by the store, and an interrupted submit leaves the
    journal at ``unknown`` before the cancellation propagates.
    """

    def __init__(self, records, store, outcome: str = "confirmed") -> None:
        self.records = records
        self.store = store
        self.outcome = outcome
        self.busy = False
        self.last_result = None
        self.calls: list[dict] = []

    async def run(self, cfg, otp, answers, **kwargs):
        self.calls.append({"answers": answers, **kwargs})
        attempt_id = self.store.prepare_consent(
            cfg.singpass_id,
            cfg.form_url,
            kwargs["attendance_date"],
            "submit",
            kwargs["consent"],
        )
        self.store.transition(attempt_id, "running")
        if self.outcome == "confirmed":
            self.store.transition(attempt_id, "submitting")
            self.store.transition(attempt_id, "confirmed", "submitted")
            self.last_result = RunResult("confirmed", attempt_id)
            return self.last_result
        if self.outcome == "cancel":
            self.store.transition(attempt_id, "submitting")
            self.store.transition(attempt_id, "unknown", "run cancelled")
            self.last_result = RunResult("unknown", attempt_id, "run cancelled")
            raise asyncio.CancelledError()
        raise AssertionError(f"unsupported review outcome {self.outcome}")


def confirmed_attempt(store, account: str, form: str, day) -> int:
    """Record one confirmed same-day journal attempt, as the runner would."""
    attempt_id = store.prepare(account, form, day, "submit")
    store.transition(attempt_id, "running")
    store.transition(attempt_id, "submitting")
    store.transition(attempt_id, "confirmed", "submitted")
    return attempt_id


class TelegramWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.users_path = make_users(self.directory.name, {UID: user_record()})
        self.cfg = Config(users_path=self.users_path, prompt_timeout=2, state_dir=self.directory.name)
        self.user = User(UID, "S1234567A", "secret", DEPARTMENT)

    def make_state(self, *, runner=None, scheduler=None, users_path=None):
        return SimpleNamespace(
            base_cfg=self.cfg,
            users_path=users_path or self.users_path,
            interactions={},
            active_chats=set(),
            app=SimpleNamespace(bot=FakeBot()),
            store=None,
            runner=runner if runner is not None else SimpleNamespace(records=FakeRecords()),
            scheduler=scheduler,
            otp_server=None,
            miniapp_server=None,
        )

    async def _wait_pending(self, interaction, seen: set[int]) -> int:
        for _ in range(2000):
            message_id = interaction._prompt_message_id
            if (
                interaction._pending_token is not None
                and interaction._future is not None
                and not interaction._future.done()
                and message_id is not None
                and message_id not in seen
            ):
                return message_id
            await asyncio.sleep(0)
        raise AssertionError("interaction never reached a fresh prompt")

    async def drive(self, interaction, coro, steps):
        seen: set[int] = set()
        task = asyncio.create_task(coro)
        try:
            for kind, value in steps:
                message_id = await self._wait_pending(interaction, seen)
                seen.add(message_id)
                if kind == "text":
                    accepted = interaction.deliver_text(
                        value,
                        user_id=interaction.chat_id,
                        chat_id=interaction.chat_id,
                        reply_to_message_id=message_id,
                    )
                else:
                    accepted = interaction.deliver_choice(
                        value,
                        user_id=interaction.chat_id,
                        chat_id=interaction.chat_id,
                        message_id=message_id,
                    )
                self.assertTrue(accepted, f"prompt did not accept {kind} {value!r}")
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def make_interaction(self, state):
        interaction = tb.Interaction(UID, state, state.app)
        state.interactions[UID] = interaction
        state.active_chats.add(UID)
        return interaction

    # ---- config -----------------------------------------------------------

    def test_miniapp_config_validation(self):
        self.assertFalse(Config().miniapp_enabled)
        self.assertEqual(Config().miniapp_port, 8766)
        enabled = Config(miniapp_public_url="https://example.ngrok.app")
        self.assertTrue(enabled.miniapp_enabled)
        self.assertEqual(enabled.miniapp_link("plan"), "https://example.ngrok.app#plan")
        for bad in (
            "http://example.ngrok.app",
            "https://user:pw@example.ngrok.app",
            "https://example.ngrok.app/?a=1",
            "https://example.ngrok.app/#frag",
            "https://",
        ):
            with self.assertRaises(ValueError):
                Config(miniapp_public_url=bad)
        with self.assertRaises(ValueError):
            Config(miniapp_port=70000)

    # ---- OTP warning ------------------------------------------------------

    async def test_otp_warning_fires_once_and_cancels_on_success(self):
        state = self.make_state()
        interaction = self.make_interaction(state)
        provider = tb.TelegramOtpProvider(interaction, http_awaited=True, warning_seconds=0.02)
        task = asyncio.create_task(provider.wait_for_otp(5))
        await self._wait_pending(interaction, set())
        prompt_id = interaction._prompt_message_id
        await asyncio.sleep(0.05)
        warnings = [m for m in state.app.bot.messages if m.reply_to == prompt_id]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Automatic phone OTP delivery has not arrived", warnings[0].text)
        accepted = interaction.deliver_otp(
            "123456", user_id=UID, chat_id=UID, reply_to_message_id=prompt_id
        )
        self.assertTrue(accepted)
        self.assertEqual(await task, "123456")
        await asyncio.sleep(0.05)
        self.assertEqual(len([m for m in state.app.bot.messages if m.reply_to == prompt_id]), 1)

    async def test_otp_warning_cancelled_when_delivery_arrives_first(self):
        state = self.make_state()
        interaction = self.make_interaction(state)
        provider = tb.TelegramOtpProvider(interaction, http_awaited=True, warning_seconds=0.2)
        task = asyncio.create_task(provider.wait_for_otp(5))
        await self._wait_pending(interaction, set())
        prompt_id = interaction._prompt_message_id
        self.assertTrue(
            interaction.deliver_otp("123456", user_id=UID, chat_id=UID, reply_to_message_id=prompt_id)
        )
        self.assertEqual(await task, "123456")
        await asyncio.sleep(0.3)
        self.assertEqual(
            [m for m in state.app.bot.messages if m.reply_to == prompt_id],
            [],
            "a warning must not fire after the OTP already arrived",
        )

    async def test_no_warning_without_configured_http_delivery(self):
        state = self.make_state()
        interaction = self.make_interaction(state)
        provider = tb.TelegramOtpProvider(interaction, http_awaited=False, warning_seconds=0.01)
        task = asyncio.create_task(provider.wait_for_otp(5))
        await self._wait_pending(interaction, set())
        prompt_id = interaction._prompt_message_id
        await asyncio.sleep(0.04)
        self.assertEqual([m for m in state.app.bot.messages if m.reply_to == prompt_id], [])
        self.assertTrue(
            interaction.deliver_otp("123456", user_id=UID, chat_id=UID, reply_to_message_id=prompt_id)
        )
        self.assertEqual(await task, "123456")

    async def test_scheduled_otp_holds_a_transient_session_only(self):
        state = self.make_state()
        provider = tb._ScheduledOtpProvider(state, self.user)
        self.assertEqual(state.interactions, {})
        task = asyncio.create_task(provider.wait_for_otp(5))
        for _ in range(1000):
            if UID in state.interactions:
                break
            await asyncio.sleep(0)
        interaction = state.interactions[UID]
        await self._wait_pending(interaction, set())
        self.assertIs(interaction._state, state)
        self.assertTrue(
            interaction.deliver_otp(
                "123456", user_id=UID, chat_id=UID, reply_to_message_id=interaction._prompt_message_id
            )
        )
        self.assertEqual(await task, "123456")
        self.assertEqual(state.interactions, {}, "the dispatch session must not leak")

    async def test_scheduled_otp_fails_closed_when_a_session_is_active(self):
        state = self.make_state()
        self.make_interaction(state)
        provider = tb._ScheduledOtpProvider(state, self.user)
        with self.assertRaises(tb.BusyRun):
            await provider.wait_for_otp(5)

    # ---- manual MC path ---------------------------------------------------

    async def test_manual_mc_collection_still_works(self):
        state = self.make_state()
        interaction = self.make_interaction(state)
        answers = await self.drive(
            interaction,
            tb._collect(interaction, self.user, dry_run=True, status="mc"),
            [("text", "Clinic A"), ("text", "0930hrs")],
        )
        cfg, _provider, collected = answers
        self.assertTrue(cfg.dry_run)
        self.assertEqual(collected, mc_answers("Clinic A", "0930hrs"))

    async def test_manual_run_survives_source_verification_failure(self):
        class ExplodingRunner:
            records = FakeRecords()

            async def run(self, *args, **kwargs):
                self.records = FakeRecords(fail=True)
                raise RuntimeError("source verification failed")

        state = self.make_state(runner=ExplodingRunner())
        interaction = self.make_interaction(state)
        await self.drive(
            interaction, tb._drive(interaction, self.user, dry_run=True), [("choice", "normal")]
        )
        self.assertNotIn(UID, state.interactions)
        self.assertTrue(
            any("could not complete" in m.text for m in state.app.bot.messages),
            "the user must still get a safe outcome message",
        )

    async def test_source_verify_failure_is_reported_not_fatal(self):
        self.assertFalse(await tb._verify_source(SimpleNamespace(runner=SimpleNamespace(records=None))))
        state = self.make_state(runner=SimpleNamespace(records=FakeRecords(fail=True)))
        self.assertFalse(await tb._verify_source(state))
        healthy = self.make_state(runner=SimpleNamespace(records=FakeRecords()))
        self.assertTrue(await tb._verify_source(healthy))

    # ---- onboarding -------------------------------------------------------

    def make_planner(self, *, entries=None):
        store = StateStore(self.directory.name)
        self.addCleanup(store.close)
        store.upgrade_planner(today=TODAY.date())
        planner = Planner(store, clock=lambda: TODAY)
        if entries is not None:
            self.users_path = make_users(self.directory.name, entries)
            self.cfg = Config(
                users_path=self.users_path, prompt_timeout=2, state_dir=self.directory.name
            )
        return store, planner

    def enable_auto(self, planner, uid=UID, name="LI RUN"):
        """Onboard one owner so a complete plan predicts automatic submission."""
        revision = planner.get_settings(uid)["revision"]
        settings = planner.set_name(uid, name, expected_revision=revision)
        settings = planner.confirm_name(uid, expected_revision=settings["revision"])
        return planner.save_settings(
            uid,
            expected_revision=settings["revision"],
            enabled=True,
            prompt_time="08:00",
            auto_time="08:30",
            otp_ready=True,
            policy_accepted=True,
        )

    async def test_onboarding_normalizes_confirms_and_detects_collision(self):
        _store, planner = self.make_planner()
        state = self.make_state(
            runner=SimpleNamespace(records=FakeRecords()), users_path=self.users_path
        )
        interaction = self.make_interaction(state)
        accepted = await self.drive(interaction, tb._onboard(interaction, self.user, planner, state), [
            ("text", " li  run "),
            ("choice", "name:yes"),
        ])
        self.assertTrue(accepted)
        settings = planner.get_settings(UID)
        self.assertEqual(settings["attendance_name"], "LI RUN")
        self.assertTrue(settings["name_confirmed"])
        self.assertFalse(settings["enabled"], "onboarding must never enable automatic mode")

    async def test_onboarding_rejects_ambiguous_identity(self):
        _store, planner = self.make_planner()
        planner.set_name(999, "LI RUN", expected_revision=0)
        self.users_path = make_users(
            self.directory.name, {UID: user_record(), 999: user_record()}
        )
        state = self.make_state(users_path=self.users_path)
        interaction = self.make_interaction(state)
        accepted = await self.drive(interaction, tb._onboard(interaction, self.user, planner, state), [
            ("text", "li run"),
            ("choice", "name:yes"),
        ])
        self.assertFalse(accepted)
        self.assertFalse(planner.get_settings(UID)["name_confirmed"])
        self.assertFalse(planner.get_settings(999)["name_confirmed"])
        self.assertTrue(any("Ask the operator" in m.text for m in state.app.bot.messages))

    # ---- decision buttons -------------------------------------------------

    async def make_scheduler(self, store):
        runner = AttendanceRunner(self.cfg, store, records=FakeRecords())

        async def prepare(uid, profile, details, deadline):
            raise AssertionError("the scheduler must not dispatch in these tests")

        async def notify(uid, text, *, decision=None):
            return None

        scheduler = Scheduler(
            store, runner, self.cfg, prepare, notify, users=lambda: {}, clock=lambda: TODAY
        )
        await scheduler.recover()
        self.addAsyncCleanup(scheduler.stop)
        return scheduler

    async def test_stale_button_and_revision_are_rejected(self):
        store, planner = self.make_planner()
        self.enable_auto(planner)
        scheduler = await self.make_scheduler(store)
        revision = planner.get_plan(UID)["revision"]
        planner.save_plan(
            UID, [{"date": TODAY.date().isoformat(), "profile": "normal", "details": {}}],
            expected_revision=revision,
        )
        decision = planner.ensure_decision(UID)
        prompted = planner.mark_prompt(
            UID, TODAY.date(), expected_revision=decision["revision"], message_id=10
        )
        self.assertEqual(
            prompted["revision"],
            decision["revision"],
            "notification bookkeeping must not invalidate the button revision",
        )
        state = SimpleNamespace(scheduler=scheduler, base_cfg=self.cfg, runner=None)
        context = SimpleNamespace(bot=FakeBot())

        wrong_message = FakeQuery(999)
        await tb._handle_decision_action(
            context, wrong_message, state, self.user, "keep", prompted["revision"]
        )
        self.assertEqual(planner.get_decision(UID)["state"], "awaiting")

        future_revision = FakeQuery(10)
        await tb._handle_decision_action(
            context, future_revision, state, self.user, "keep", prompted["revision"] + 5
        )
        self.assertEqual(planner.get_decision(UID)["state"], "awaiting")
        self.assertFalse(planner.get_decision(UID)["acknowledged"])

        # A newer edit bumps the decision revision; the older button must not rebase onto it.
        planner.save_plan(
            UID,
            [{"date": TODAY.date().isoformat(), "profile": "wfh", "details": {}}],
            expected_revision=planner.get_plan(UID)["revision"],
        )
        live = planner.get_decision(UID)
        self.assertNotEqual(live["revision"], prompted["revision"])
        older = FakeQuery(10)
        await tb._handle_decision_action(
            context, older, state, self.user, "keep", prompted["revision"]
        )
        self.assertEqual(planner.get_decision(UID)["profile"], "wfh")
        self.assertFalse(planner.get_decision(UID)["acknowledged"])

        planner.mark_prompt(
            UID, TODAY.date(), expected_revision=live["revision"], message_id=11
        )
        current = FakeQuery(11)
        await tb._handle_decision_action(
            context, current, state, self.user, "keep", live["revision"]
        )
        self.assertTrue(planner.get_decision(UID)["acknowledged"])

    def test_decision_callback_parsing(self):
        self.assertIsNone(tb._parse_decision_callback("sched:2026-10-02"))
        self.assertIsNone(tb._parse_decision_callback("set:2026-10-02:normal"))
        self.assertIsNone(tb._parse_decision_callback("dec:keep"))
        self.assertIsNone(tb._parse_decision_callback("dec:bogus:2"))
        self.assertIsNone(tb._parse_decision_callback("dec:keep:two"))
        self.assertEqual(tb._parse_decision_callback("dec:skip:7"), ("skip", 7))

    def test_answers_for_executable_profiles_only(self):
        self.assertEqual(tb._answers_for("normal")["Status"], "Present (Infinite Studios - IS)")
        self.assertEqual(tb._answers_for("wfh")["Status"], "Work-from-Home (WFH)")
        for blocked in ("ma", "mc", "skip", None):
            with self.assertRaises(tb.PlannerBlocked):
                tb._answers_for(blocked)

    def test_morning_keyboard_offers_records_and_review(self):
        state = SimpleNamespace(
            base_cfg=Config(),
            scheduler=None,
            miniapp_server=None,
        )
        recorded = tb._decision_keyboard(
            state,
            UID,
            {"revision": 4, "state": "recorded", "profile": "normal", "prompt_message_id": 9},
        )
        labels = [button.text for row in recorded.inline_keyboard for button in row]
        self.assertIn("Keep Existing", labels)
        self.assertIn("Submit Different Attendance", labels)
        awaiting = tb._decision_keyboard(
            state,
            UID,
            {"revision": 4, "state": "awaiting", "profile": "normal", "prompt_message_id": 9},
        )
        payloads = [button.callback_data for row in awaiting.inline_keyboard for button in row]
        self.assertIn("dec:keep:4", payloads)
        self.assertIn("dec:submit_now:4", payloads)
        self.assertNotIn(None, payloads)


    # ---- additional submission review ------------------------------------

    @contextmanager
    def _frozen_clock(self):
        with mock.patch.object(tb, "datetime", _FrozenDatetime):
            yield

    async def _review_setup(self, *, outcome: str = "confirmed"):
        store, planner = self.make_planner()
        self.enable_auto(planner)
        planner.save_plan(
            UID,
            [{"date": TODAY.date().isoformat(), "profile": "normal", "details": {}}],
            expected_revision=planner.get_plan(UID)["revision"],
        )
        scheduler = await self.make_scheduler(store)
        check = SimpleNamespace(
            status="not_found", records=(), checked_at=TODAY, digest="a" * 64, error_code=None
        )
        runner = ReviewRunner(FakeLookupRecords(check), store, outcome=outcome)
        state = SimpleNamespace(
            base_cfg=self.cfg,
            users_path=self.users_path,
            interactions={},
            active_chats=set(),
            app=SimpleNamespace(bot=FakeBot()),
            store=store,
            runner=runner,
            scheduler=scheduler,
            otp_server=None,
        )
        interaction = tb.Interaction(UID, state, state.app)
        state.interactions[UID] = interaction
        return store, planner, runner, state, interaction

    async def test_override_review_settles_without_needing_restart(self):
        store, planner, runner, state, interaction = await self._review_setup()
        with self._frozen_clock():
            await self.drive(
                interaction,
                tb._drive_review(interaction, self.user),
                [("choice", "profile:normal"), ("choice", "review:yes")],
            )
            self.assertNotIn(UID, state.interactions)
            self.assertEqual(len(runner.calls), 1)
            call = runner.calls[0]
            self.assertEqual(call["attendance_name"], "LI RUN")
            self.assertEqual(call["department"], DEPARTMENT)
            self.assertEqual(call["decision_uid"], UID)
            self.assertIsInstance(call["decision_revision"], int)
            # The frozen Telegram clock must drive the decision date, not the wall clock.
            self.assertEqual(call["attendance_date"], TODAY.date())
            self.assertEqual(call["deadline"].date(), TODAY.date())
            decision = planner.get_decision(UID)
            self.assertEqual(decision["state"], "confirmed")
            self.assertTrue(decision["consent"]["authorized"])
            self.assertTrue(decision["consent"]["consumed"])
            context = call["consent"]
            self.assertIsInstance(context, AdditionalSubmissionConsent)
            self.assertEqual((context.date, context.decision_uid), (TODAY.date(), UID))
            # A consumed review authorises exactly one attempt: reuse is rejected.
            with self.assertRaises(DuplicateRun):
                store.prepare_consent(
                    self.user.singpass_id, self.cfg.form_url, TODAY.date(), "submit", context
                )

    async def test_override_review_excludes_other_owner_and_form_attempts(self):
        store, planner, runner, state, interaction = await self._review_setup()
        day = TODAY.date()
        own = confirmed_attempt(store, self.user.singpass_id, self.cfg.form_url, day)
        other = confirmed_attempt(store, "S9999999Z", self.cfg.form_url, day)
        foreign = confirmed_attempt(store, self.user.singpass_id, "https://form.gov.sg/other", day)
        with self._frozen_clock():
            await self.drive(
                interaction,
                tb._drive_review(interaction, self.user),
                [("choice", "profile:normal"), ("choice", "review:yes")],
            )
        self.assertEqual(planner.get_decision(UID)["consent"]["local_attempt_ids"], [own])
        evidence = next(
            m.text for m in state.app.bot.messages if "Local submissions already recorded" in m.text
        )
        self.assertIn(f"#{own} confirmed", evidence)
        self.assertNotIn(f"#{other}", evidence)
        self.assertNotIn(f"#{foreign}", evidence)
        self.assertEqual(planner.get_decision(UID)["state"], "confirmed")

    async def test_override_review_cancellation_stays_conservatively_unknown(self):
        _store, planner, _runner, state, interaction = await self._review_setup(outcome="cancel")
        with self._frozen_clock(), self.assertRaises(asyncio.CancelledError):
            await self.drive(
                interaction,
                tb._drive_review(interaction, self.user),
                [("choice", "profile:normal"), ("choice", "review:yes")],
            )
        decision = planner.get_decision(UID)
        self.assertEqual(decision["state"], "unknown")
        self.assertIsNotNone(decision["attempt_id"])
        self.assertTrue(any("UNKNOWN" in m.text for m in state.app.bot.messages))

    # ---- reminders, notices and navigation --------------------------------

    async def test_reminder_promotes_prompt_and_rejects_stale_foreign_buttons(self):
        store, planner = self.make_planner()
        self.enable_auto(planner)
        scheduler = await self.make_scheduler(store)
        planner.save_plan(
            UID,
            [{"date": TODAY.date().isoformat(), "profile": "normal", "details": {}}],
            expected_revision=planner.get_plan(UID)["revision"],
        )
        decision = planner.ensure_decision(UID)
        self.assertEqual(decision["state"], "awaiting")
        prompted = planner.mark_prompt(
            UID, TODAY.date(), expected_revision=decision["revision"], message_id=10
        )
        promoted = planner.mark_reminder(
            UID, TODAY.date(), expected_revision=prompted["revision"], message_id=20
        )
        # Bookkeeping promotes the live prompt id without invalidating the buttons.
        self.assertEqual(promoted["revision"], decision["revision"])
        self.assertTrue(promoted["reminder_sent"])
        self.assertEqual(promoted["prompt_message_id"], 20)
        state = SimpleNamespace(scheduler=scheduler, base_cfg=self.cfg, runner=None)
        context = SimpleNamespace(bot=FakeBot())
        # The superseded prompt's buttons are stale.
        await tb._handle_decision_action(
            context, FakeQuery(10), state, self.user, "keep", promoted["revision"]
        )
        self.assertFalse(planner.get_decision(UID)["acknowledged"])
        # A foreign message id is rejected.
        await tb._handle_decision_action(
            context, FakeQuery(999), state, self.user, "skip", promoted["revision"]
        )
        self.assertEqual(planner.get_decision(UID)["state"], "awaiting")
        # The delivered reminder's own buttons act on the live revision.
        await tb._handle_decision_action(
            context, FakeQuery(20), state, self.user, "keep", promoted["revision"]
        )
        self.assertTrue(planner.get_decision(UID)["acknowledged"])

    def test_notification_markup_controls_only_for_marked_decisions(self):
        state = SimpleNamespace(base_cfg=Config(), scheduler=None, miniapp_server=None)
        controlled = tb._notification_markup(
            state,
            UID,
            {"_controls": True, "revision": 4, "state": "awaiting", "profile": "normal"},
        )
        payloads = [
            b.callback_data for row in controlled.inline_keyboard for b in row if b.callback_data
        ]
        self.assertIn("dec:keep:4", payloads)
        notice = tb._notification_markup(
            state, UID, {"revision": 4, "state": "missed", "profile": "normal"}
        )
        self.assertEqual(
            [b.callback_data for row in notice.inline_keyboard for b in row], ["menu:plan"]
        )
        self.assertIsNone(tb._notification_markup(state, UID, None))

    def test_paused_keyboard_never_claims_a_scheduled_time(self):
        state = SimpleNamespace(base_cfg=Config(), scheduler=None, miniapp_server=None)
        keyboard = tb._decision_keyboard(
            state, UID, {"revision": 3, "state": "held", "profile": "normal"}
        )
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertFalse(any("Keep" in label for label in labels))
        payloads = [
            b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data
        ]
        self.assertIn("dec:submit_now:3", payloads)

    async def test_disabled_miniapp_menu_callbacks_route_to_fallbacks(self):
        state = self.make_state()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"mdc": state}), bot=state.app.bot
        )
        for data in ("menu:today", "menu:plan", "menu:time"):
            query = FakeQuery(1)
            query.data = data
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(type="private", id=UID),
                effective_user=SimpleNamespace(id=UID),
                callback_query=query,
            )
            await tb._on_callback(update, context)
        texts = [m.text for m in state.app.bot.messages]
        # The launchers never promise an absent Mini App; they answer honestly and
        # keep the manual commands reachable.
        self.assertTrue(any("Mini App is unavailable" in text for text in texts))
        self.assertTrue(any("/attend" in text for text in texts))
        state_none = SimpleNamespace(base_cfg=Config(), scheduler=None, miniapp_server=None)
        self.assertEqual(
            tb._view_button(state_none, "today", "Open Today").callback_data, "menu:today"
        )
        commands = dict(tb._BOT_COMMANDS)
        self.assertIs(commands["today"], tb._today_cmd)

    # ---- Today fallback and reachability ---------------------------------

    def test_today_text_separates_unknown_from_source_records(self):
        unknown = tb._today_text(
            {
                "today": "2026-10-02",
                "decision": {"state": "awaiting"},
                "source_check": None,
                "local_outcome": None,
                "next_action": "automatic_submission",
            }
        )
        self.assertIn("could not be verified", unknown)
        self.assertNotIn("none found for today", unknown)
        found = tb._today_text(
            {
                "today": "2026-10-02",
                "decision": {"state": "confirmed"},
                "source_check": {
                    "status": "found",
                    "checked_at": "2026-10-02T08:00:00+08:00",
                    "error_code": None,
                    "digest": "x",
                },
                "local_outcome": "confirmed",
                "next_action": "none",
            }
        )
        self.assertIn("existing attendance found", found)
        self.assertIn("confirmed", found)

    async def test_today_command_gives_text_status_without_miniapp(self):
        async def _today(uid):
            return {
                "today": "2026-10-02",
                "decision": {"state": "awaiting"},
                "source_check": None,
                "local_outcome": None,
                "next_action": "automatic_submission",
            }

        state = self.make_state(scheduler=SimpleNamespace(today=_today))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"mdc": state}), bot=state.app.bot
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private", id=UID),
            effective_user=SimpleNamespace(id=UID),
            callback_query=None,
        )
        await tb._today_cmd(update, context)
        self.assertIn("could not be verified", state.app.bot.messages[-1].text)

    def test_keyboard_omits_change_when_editor_unavailable(self):
        def payloads(keyboard):
            return [
                b.callback_data
                for row in keyboard.inline_keyboard
                for b in row
                if b.callback_data
            ]

        decision = {"revision": 3, "state": "held", "profile": "normal"}
        unavailable = SimpleNamespace(base_cfg=Config(), scheduler=None, miniapp_server=None)
        absent = payloads(tb._decision_keyboard(unavailable, UID, decision))
        self.assertNotIn("dec:change:3", absent)
        self.assertIn("dec:submit_now:3", absent)
        self.assertIn("dec:skip:3", absent)
        available = SimpleNamespace(
            base_cfg=Config(miniapp_public_url="https://example.ngrok.app"),
            scheduler=None,
            miniapp_server=SimpleNamespace(),
        )
        self.assertIn("dec:change:3", payloads(tb._decision_keyboard(available, UID, decision)))

    async def test_change_without_editor_never_holds(self):
        store, planner = self.make_planner()
        self.enable_auto(planner)
        scheduler = await self.make_scheduler(store)
        planner.save_plan(
            UID,
            [{"date": TODAY.date().isoformat(), "profile": "normal", "details": {}}],
            expected_revision=planner.get_plan(UID)["revision"],
        )
        decision = planner.ensure_decision(UID)
        prompted = planner.mark_prompt(
            UID, TODAY.date(), expected_revision=decision["revision"], message_id=10
        )
        state = SimpleNamespace(
            scheduler=scheduler, base_cfg=self.cfg, runner=None, miniapp_server=None
        )
        context = SimpleNamespace(bot=FakeBot())
        # A stale Change button issued while the editor was live must not mutate today.
        await tb._handle_decision_action(
            context, FakeQuery(10), state, self.user, "change", prompted["revision"]
        )
        self.assertEqual(planner.get_decision(UID)["state"], "awaiting")
        self.assertTrue(any("/attend" in m.text for m in context.bot.messages))

    async def test_change_holds_and_opens_today_editor_when_available(self):
        store, planner = self.make_planner()
        self.enable_auto(planner)
        scheduler = await self.make_scheduler(store)
        planner.save_plan(
            UID,
            [{"date": TODAY.date().isoformat(), "profile": "normal", "details": {}}],
            expected_revision=planner.get_plan(UID)["revision"],
        )
        decision = planner.ensure_decision(UID)
        prompted = planner.mark_prompt(
            UID, TODAY.date(), expected_revision=decision["revision"], message_id=10
        )
        cfg = Config(
            users_path=self.users_path,
            prompt_timeout=2,
            state_dir=self.directory.name,
            miniapp_public_url="https://example.ngrok.app",
        )
        state = SimpleNamespace(
            scheduler=scheduler, base_cfg=cfg, runner=None, miniapp_server=SimpleNamespace()
        )
        context = SimpleNamespace(bot=FakeBot())
        await tb._handle_decision_action(
            context, FakeQuery(10), state, self.user, "change", prompted["revision"]
        )
        self.assertEqual(planner.get_decision(UID)["state"], "held")
        button = context.bot.messages[-1].reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.web_app.url, "https://example.ngrok.app#today?edit=1")


if __name__ == "__main__":
    unittest.main()
