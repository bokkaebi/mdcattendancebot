"""Real-loopback Mini App gateway: signed auth, strict JSON, owner scope, OTP proxy."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp import ClientSession, DummyCookieJar

from mdcattendance.attendance import DEPARTMENT, NORMAL_ANSWERS
from mdcattendance.config import Config
from mdcattendance.miniapp import MiniAppInstallError, MiniAppServer
from mdcattendance.records import RecordCheck, SourceVerificationError, SubmissionRecord
from mdcattendance.scheduler import Scheduler
from mdcattendance.schedules import Planner
from mdcattendance.server import OtpHttpServer
from mdcattendance.storage import SINGAPORE, StateStore
from mdcattendance.users import load_users

FORM = "https://example.invalid/synthetic-attendance-form"
DAY = "2026-10-02"
BOT_TOKEN = "123456:synthetic-bot-token"
PUBLIC_ORIGIN = "https://mini.example.invalid"
OTP_TOKEN = "a" * 32


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
    """Owner-scoped source evidence with a scripted sequence of results."""

    def __init__(self, *checks):
        self._checks = list(checks)
        self.lookups = 0

    async def lookup(self, name, department, day, *, fresh=False):
        self.lookups += 1
        if not self._checks:
            raise RuntimeError("synthetic source has no scripted result left")
        return self._checks[0] if len(self._checks) == 1 else self._checks.pop(0)


class VerifyingRecords(StaticRecords):
    def __init__(self, code, *checks):
        super().__init__(*checks)
        self.code = code

    async def verify_source(self):
        if self.code:
            raise SourceVerificationError(self.code)
        return True


class StubOtp:
    async def wait_for_otp(self, timeout):
        return "123456"


class StubRunner:
    def __init__(self, store, *, records=None, script=None):
        self.store = store
        self.records = records
        self.script = list(script or [])
        self.calls = []

    def recover(self):
        self.store.recover(attendance_deadline="09:00")
        return True

    def stage(self, account, form, day):
        return None

    async def run(self, cfg, otp, answers, **kwargs):
        self.calls.append(kwargs)
        status, _ = self.script.pop(0) if self.script else ("confirmed", None)
        return SimpleNamespace(status=status, attempt_id=None, detail="", record_check=None)

    def shutdown(self):
        return None


class Notifier:
    def __init__(self):
        self.messages = []

    async def __call__(self, uid, text, *, decision=None):
        self.messages.append((uid, text))
        return len(self.messages)


class MiniAppCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(self.directory.name)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 2, 8, 0, tzinfo=SINGAPORE)
        with startup_locks(self.store):
            self.store.upgrade_planner(today=self.now.date(), attendance_deadline="09:00")
        self.planner = Planner(self.store, "09:00", clock=lambda: self.now)
        self.users_path = os.path.join(self.directory.name, "users.json")
        self._write_users()
        self.cfg = Config(
            singpass_id="synthetic-account-7",
            form_url=FORM,
            state_dir=self.directory.name,
            attendance_deadline="09:00",
            telegram_bot_token=BOT_TOKEN,
            users_path=self.users_path,
            otp_http_enabled=True,
            otp_http_token="synthetic-receiver-token",
            miniapp_public_url=PUBLIC_ORIGIN,
            miniapp_port=0,
        )
        self.client = ClientSession(cookie_jar=DummyCookieJar(), trust_env=False)
        self.addAsyncCleanup(self.client.close)
        self.schedulers = []
        self.started_schedulers = []
        self.servers = []
        self.base = ""
        self.addAsyncCleanup(self._shutdown)
        self.onboard(7, "SYNTHETIC OWNER", "normal")

    async def _shutdown(self):
        for server in reversed(self.servers):
            await server.stop()
        for scheduler in reversed(self.started_schedulers):
            await scheduler.stop()

    # --------------------------------------------------------------- harness

    def _write_users(self, owners=None):
        owners = owners or {
            7: ("synthetic-account-7", DEPARTMENT),
            8: ("synthetic-account-8", DEPARTMENT),
        }
        payload = {
            str(uid): {
                "singpass_id": account,
                "singpass_password": "synthetic-password",
                "department": department,
                "otp_token": "otp-token-" + str(uid).rjust(22, "0"),
            }
            for uid, (account, department) in owners.items()
        }
        Path(self.users_path).write_text(json.dumps(payload))
        os.chmod(self.users_path, 0o600)

    def onboard(self, uid=7, name="SYNTHETIC OWNER", profile="normal", day=DAY):
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
        plan_revision = self.planner.get_plan(uid)["revision"]
        self.planner.save_plan(
            uid, [{"date": day, "profile": profile, "details": {}}], expected_revision=plan_revision
        )

    def not_found(self, label="empty"):
        return RecordCheck("not_found", (), self.now, hashlib.sha256(label.encode()).hexdigest())

    def found(self, label="found"):
        record = SubmissionRecord(self.now, "Synthetic status", (("Remarks (NSC/IS)", "Synthetic"),))
        return RecordCheck("found", (record,), self.now, hashlib.sha256(label.encode()).hexdigest())

    def unavailable(self, code="network_error"):
        return RecordCheck("unavailable", (), self.now, None, code)

    def build(self, records=None):
        runner = StubRunner(self.store, records=records or StaticRecords(self.not_found()))
        scheduler = Scheduler(
            self.store,
            runner,
            self.cfg,
            self._prepare,
            Notifier(),
            users=lambda: load_users(self.users_path),
            clock=lambda: self.now,
        )
        self.schedulers.append(scheduler)
        self.runner = runner
        return scheduler

    async def _prepare(self, uid, profile, details, deadline):
        return self.cfg, StubOtp(), dict(NORMAL_ANSWERS)

    async def launch(self, records=None, otp_server=None, scheduler=None):
        scheduler = scheduler or self.build(records)
        # Recover the planner without starting the background tick loop: the API
        # surface is tested here, and one deterministic source read per request keeps
        # the scripted evidence sequencing exact.
        await scheduler.recover()
        self.started_schedulers.append(scheduler)
        server = MiniAppServer(self.cfg, scheduler, otp_server=otp_server)
        await server.start()
        self.servers.append(server)
        self.server = server
        self.scheduler = scheduler
        self.base = f"http://127.0.0.1:{server.port}"
        return scheduler

    async def test_telegram_cancel_owns_queued_work_without_an_otp_session(self):
        from mdcattendance.telegram_bot import _cancel

        await self.launch()
        decision = self.planner.ensure_decision(7)
        await self.scheduler.action(7, "submit_now", expected_revision=decision["revision"])

        async def send_message(*args, **kwargs):
            return None

        state = SimpleNamespace(
            users_path=self.users_path, interactions={}, scheduler=self.scheduler
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"mdc": state}),
            bot=SimpleNamespace(send_message=send_message),
        )

        def update(uid):
            return SimpleNamespace(
                effective_chat=SimpleNamespace(id=uid, type="private"),
                effective_user=SimpleNamespace(id=uid),
                callback_query=None,
            )

        await _cancel(update(8), context)
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "ready")
        await _cancel(update(7), context)
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "held")
        self.now = self.now.replace(hour=8, minute=31)
        await self.scheduler.tick()
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "held")

        # Cancel a newly claimed task before its coroutine gets an instruction.
        decision = self.planner.get_decision(7, DAY)
        await self.scheduler.action(7, "restore", expected_revision=decision["revision"])
        await self.scheduler.tick()
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "running")
        await _cancel(update(7), context)
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "failed")
        await self.scheduler.tick()
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "failed")
        decision = self.planner.get_decision(7, DAY)
        status, view, _ = await self.call(
            "POST", "/api/today/action",
            body={"action": "submit_now", "expected_revision": decision["revision"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(view["decision"]["state"], "ready")
        self.assertEqual(self.runner.calls, [])

    async def test_source_failure_cannot_resume_an_owner_cancel(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        waiting = True
        records = StaticRecords(self.not_found())

        async def lookup(name, department, day, *, fresh=False):
            if waiting:
                entered.set()
                await release.wait()
                return self.unavailable()
            return self.not_found()

        records.lookup = lookup
        await self.launch(records=records)
        decision = self.planner.ensure_decision(7)
        self.planner.mark_prompt(
            7, DAY, expected_revision=decision["revision"], message_id=99
        )
        self.now = self.now.replace(hour=8, minute=31)
        dispatch = asyncio.create_task(self.scheduler.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(await self.scheduler.cancel(7))
        finally:
            release.set()
            await dispatch
        waiting = False
        await self.scheduler.today(7, fresh=True)
        self.now = self.now.replace(minute=32)
        await self.scheduler.tick()
        self.assertEqual(self.planner.get_decision(7, DAY)["state"], "held")

    async def test_poll_finishing_during_editor_keeps_conflict_review(self):
        import re

        from playwright.async_api import async_playwright, expect

        await self.launch()
        snapshot_ready = asyncio.Event()
        release_snapshot = asyncio.Event()
        snapshot_delivered = asyncio.Event()
        hold_poll = False

        async def forward(route):
            headers = await route.request.all_headers()
            # This test serves HTTP loopback; the installed HTTPS origin is
            # exercised separately. Keep the actual gateway's Origin check.
            headers["origin"] = PUBLIC_ORIGIN
            response = await route.fetch(headers=headers)
            delayed = hold_poll and route.request.url.endswith("/api/today")
            if delayed:
                snapshot_ready.set()
                await release_snapshot.wait()
            await route.fulfill(response=response)
            if delayed:
                snapshot_delivered.set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                page.set_default_timeout(5_000)
                await page.add_init_script(
                    "window.Telegram={WebApp:{initData:"
                    + json.dumps(self.sign(7))
                    + ",themeParams:{},ready(){},expand(){},onEvent(){},"
                    "enableClosingConfirmation(){},disableClosingConfirmation(){}}};"
                )
                await page.route("https://telegram.org/**", lambda route: route.abort())
                await page.route("**/api/**", forward)
                await page.clock.install(time=self.now)
                await page.goto(self.base + "/#plan")
                date_button = page.get_by_role(
                    "button", name=re.compile(r"Tuesday, 6 October")
                )
                await expect(date_button).to_be_visible()

                # A new server day makes the passive read want a new horizon.
                # Its response is delayed until an editor holds unsaved intent.
                self.now = self.now.replace(day=3)
                hold_poll = True
                await page.clock.fast_forward(10_001)
                await asyncio.wait_for(snapshot_ready.wait(), 5)
                await date_button.click()
                await page.get_by_role("button", name="Edit this date", exact=True).click()
                profile = page.get_by_role("combobox", name="Profile")
                await profile.focus()
                await profile.select_option("wfh")
                await expect(profile).to_be_focused()
                revision = self.planner.get_plan(7)["revision"]
                self.planner.save_plan(
                    7,
                    [
                        {"date": "2026-10-06", "profile": "normal", "details": {}},
                        {"date": "2026-10-07", "profile": "wfh", "details": {}},
                    ],
                    expected_revision=revision,
                )
                release_snapshot.set()
                await asyncio.wait_for(snapshot_delivered.wait(), 5)
                await page.evaluate(
                    "() => new Promise(resolve => requestAnimationFrame("
                    "() => requestAnimationFrame(resolve)))"
                )
                await page.get_by_role(
                    "button", name=re.compile("Save for date", re.I)
                ).click()
                await expect(
                    page.get_by_text("This date changed on the server", exact=True)
                ).to_be_visible()
                saved = next(
                    day for day in self.planner.get_plan(7)["days"]
                    if day["date"] == "2026-10-06"
                )
                self.assertEqual(saved["profile"], "normal")
                page.once("dialog", lambda dialog: dialog.accept())
                await page.get_by_role(
                    "button", name="Keep my edit and use latest revision", exact=True
                ).click()
                await page.get_by_role(
                    "button", name=re.compile("Save for date", re.I)
                ).click()
                await expect(page.get_by_role(
                    "button", name=re.compile(r"Wednesday, 7 October.*WFH")
                )).to_be_visible()
                await expect(page.get_by_role(
                    "button", name="Save Plan", exact=True
                )).to_be_disabled()

                # A clean pre-submit cancellation offers an explicit retry;
                # the passive scheduler must leave the failed decision alone.
                hold_poll = False
                decision = self.planner.ensure_decision(7)
                await self.scheduler.action(
                    7, "submit_now", expected_revision=decision["revision"],
                    profile="normal", details={},
                )
                await self.scheduler.tick()
                await self.scheduler.cancel(7)
                self.assertEqual(self.planner.get_decision(7)["state"], "failed")
                await page.goto(self.base + "/#today")
                await page.reload()
                retry = page.get_by_role("button", name="Retry submission", exact=True)
                await expect(retry).to_be_visible()
                page.once("dialog", lambda dialog: dialog.accept())
                await retry.click()
                await expect(retry).to_be_hidden()
                self.assertEqual(self.planner.get_decision(7)["state"], "ready")

                self.runner.records = StaticRecords(self.found())
                await page.get_by_role("button", name="Refresh records", exact=True).click()
                await expect(
                    page.get_by_text("Remarks (NSC/IS): Synthetic", exact=True)
                ).to_be_visible()
                await page.get_by_role(
                    "button", name="Submit different attendance", exact=True
                ).click()
                await page.get_by_role("button", name="Review evidence", exact=True).click()
                await expect(page.get_by_role(
                    "button", name="Authorize one additional submission", exact=True
                )).to_be_visible()
                await expect(page.locator("#sheet :focus")).to_have_count(1)

                await page.get_by_role("button", name="Cancel review", exact=True).click()
                self.runner.records = StaticRecords(self.not_found())
                await page.get_by_role("link", name="Plan", exact=True).click()
                await page.get_by_role(
                    "button", name=re.compile(r"Monday, 5 October.*Not planned")
                ).click()
                await page.get_by_role(
                    "button", name="Fill 1 selected weekday(s) with Present (IS)", exact=True
                ).click()
                self.now = self.now.replace(day=6)
                await page.clock.fast_forward(10_001)
                keep = page.get_by_role(
                    "button", name="Keep my draft and use latest revision", exact=True
                )
                await expect(keep).to_be_visible()
                await keep.click()
                await expect(keep).to_be_visible()
                await expect(page.get_by_role(
                    "button", name="Save Plan (1 unsaved)", exact=True
                )).to_be_visible()
                await page.get_by_role(
                    "button", name="Use server values for these dates", exact=True
                ).click()
                await expect(page.get_by_role(
                    "button", name="Save Plan", exact=True
                )).to_be_disabled()
                await expect(page.get_by_role(
                    "button", name=re.compile(r"Monday, 19 October.*Not planned")
                )).to_be_visible()
            finally:
                release_snapshot.set()
                await browser.close()

    # --------------------------------------------------------------- requests

    def sign(self, uid, *, auth_date=None, extra=None, user=None, digest=None):
        fields = {
            "auth_date": str(int(auth_date if auth_date is not None else time.time())),
            "query_id": "AAAsynthetic",
            "user": user if user is not None else json.dumps({"id": uid}, separators=(",", ":")),
        }
        if extra:
            fields.update(extra)
        data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        signature = digest or hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        return urlencode({**fields, "hash": signature})

    def _request(self, method, path, *, uid=7, body=None, raw=None, init=None, auth=True,
                 headers=None, origin=None, content_type="application/json"):
        request_headers = dict(headers or {})
        if auth:
            request_headers.setdefault(
                "Authorization", "tma " + (init if init is not None else self.sign(uid))
            )
        if origin is not None:
            request_headers["Origin"] = origin
        kwargs = {"headers": request_headers}
        if raw is not None:
            if content_type is not None:
                request_headers["Content-Type"] = content_type
            kwargs["data"] = raw
        elif body is not None:
            kwargs["json"] = body
        return self.client.request(method, self.base + path, **kwargs)

    async def call(self, method, path, **kwargs):
        async with self._request(method, path, **kwargs) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = None
            return response.status, payload, response.headers

    async def text(self, method, path, **kwargs):
        async with self._request(method, path, **kwargs) as response:
            return response.status, await response.text(), response.headers

    def assert_code(self, payload, code):
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("code"), code)
        self.assertIsInstance(payload.get("message"), str)

    # ------------------------------------------------------------------- auth

    async def test_signed_initdata_rejects_tamper_expiry_and_unknown_owner(self):
        await self.launch()
        status, payload, _ = await self.call("GET", "/api/me", auth=False)
        self.assertEqual(status, 401)
        self.assert_code(payload, "unauthorized")
        # A valid signature for uid 7 is accepted.
        status, payload, _ = await self.call("GET", "/api/me")
        self.assertEqual((status, payload["name"]), (200, "SYNTHETIC OWNER"))
        self.assertEqual(payload["department"], DEPARTMENT)
        self.assertTrue(payload["onboarded"])
        self.assertNotIn("uid", payload["settings"])
        # Tampered hash, expired auth_date and a future auth_date are rejected.
        tampered = self.sign(7, digest="0" * 64)
        self.assertEqual((await self.call("GET", "/api/me", init=tampered))[0], 401)
        expired = self.sign(7, auth_date=time.time() - 3601)
        self.assertEqual((await self.call("GET", "/api/me", init=expired))[0], 401)
        future = self.sign(7, auth_date=time.time() + 31)
        self.assertEqual((await self.call("GET", "/api/me", init=future))[0], 401)
        # A correctly signed but unallowlisted account is forbidden, not unauthorised.
        status, payload, _ = await self.call("GET", "/api/me", uid=999)
        self.assertEqual(status, 403)
        self.assert_code(payload, "forbidden")
        # A bearer OTP token is not a Mini App identity.
        status, _, _ = await self.call(
            "GET", "/api/me", auth=False, headers={"Authorization": "Bearer " + OTP_TOKEN}
        )
        self.assertEqual(status, 401)

    async def test_duplicate_fields_and_wrong_scheme_rejected(self):
        await self.launch()
        valid = self.sign(7)
        duplicated = valid + "&auth_date=1"
        self.assertEqual((await self.call("GET", "/api/me", init=duplicated))[0], 401)
        dup_user = self.sign(7, user='{"id":7,"id":8}')
        self.assertEqual((await self.call("GET", "/api/me", init=dup_user))[0], 401)
        for malformed in ("not-a-query", "hash=zz", ""):
            status, _, _ = await self.call(
                "GET", "/api/me", auth=False, headers={"Authorization": "tma " + malformed}
            )
            self.assertEqual(status, 401)
        wrong_scheme = self.sign(7)
        status, _, _ = await self.call(
            "GET", "/api/me", auth=False, headers={"Authorization": "Basic " + wrong_scheme}
        )
        self.assertEqual(status, 401)

    # ------------------------------------------------------- query and origin

    async def test_strict_query_ignores_client_uid_and_forces_fresh(self):
        records = StaticRecords(self.not_found(), self.unavailable())
        await self.launch(records=records)
        # A client-supplied uid query is never identity and is rejected outright.
        status, payload, _ = await self.call("GET", "/api/today?uid=8")
        self.assertEqual(status, 422)
        self.assert_code(payload, "invalid_request")
        self.assertEqual((await self.call("GET", "/api/today?refresh=2"))[0], 422)
        status, payload, _ = await self.call("GET", "/api/today")
        self.assertEqual(status, 200)
        self.assertEqual(payload["source_check"]["status"], "not_found")
        self.assertEqual(records.lookups, 1)
        # Only an explicit refresh=1 forces a new source read; unavailable is preserved.
        status, payload, _ = await self.call("GET", "/api/today?refresh=1")
        self.assertEqual((status, payload["source_check"]["status"]), (200, "unavailable"))
        self.assertEqual(records.lookups, 2)

    async def test_origin_and_body_and_content_limits(self):
        await self.launch()
        status, payload, _ = await self.call("GET", "/api/me", origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assert_code(payload, "forbidden")
        self.assertEqual(
            (await self.call("GET", "/api/me", origin=PUBLIC_ORIGIN))[0], 200
        )
        revision = self.planner.ensure_decision(7)["revision"]
        action = {"action": "keep", "expected_revision": revision}
        self.assertEqual(
            (
                await self.call(
                    "POST",
                    "/api/today/action",
                    raw=json.dumps(action).encode(),
                    content_type="text/plain",
                )
            )[0],
            415,
        )
        self.assertEqual(
            (
                await self.call(
                    "POST",
                    "/api/today/action",
                    raw=json.dumps(action).encode(),
                    headers={"Content-Encoding": "gzip"},
                )
            )[0],
            415,
        )
        self.assertEqual(
            (await self.call("POST", "/api/today/action", raw=b"x" * (17 * 1024)))[0], 413
        )
        duplicate = b'{"action":"keep","action":"hold","expected_revision":0}'
        status, payload, _ = await self.call("POST", "/api/today/action", raw=duplicate)
        self.assertEqual(status, 400)
        self.assert_code(payload, "malformed_request")
        self.assertEqual((await self.call("POST", "/api/today/action", raw=b"[]"))[0], 400)
        # Irrelevant fields for an action are rejected, not silently ignored.
        status, payload, _ = await self.call(
            "POST", "/api/today/action", body={**action, "profile": "normal"}
        )
        self.assertEqual(status, 422)
        self.assert_code(payload, "invalid_request")

    async def test_origin_rejects_mixed_case_and_default_port_variants(self):
        # A browser serializes the Origin header canonically: lowercased host and no
        # default :443, so the configured public URL must match that exact form.
        self.cfg = replace(self.cfg, miniapp_public_url="https://Mini.example.invalid:443")
        self.assertEqual(self.cfg.miniapp_public_url, PUBLIC_ORIGIN)
        await self.launch()
        revision = self.planner.get_settings(7)["revision"]
        body = {"name": "SYNTHETIC OWNER", "expected_revision": revision}
        status, payload, _ = await self.call(
            "PUT", "/api/name", body=body, origin="https://mini.example.invalid"
        )
        self.assertEqual((status, payload["name"]), (200, "SYNTHETIC OWNER"))
        for other in (
            "https://Mini.example.invalid:443",
            "https://mini.example.invalid:8443",
            "https://evil.example",
        ):
            self.assertEqual(
                (await self.call("PUT", "/api/name", body=body, origin=other))[0], 403, other
            )

    async def test_nondefault_port_origin_is_preserved(self):
        self.cfg = replace(self.cfg, miniapp_public_url="https://Mini.example.invalid:8443")
        self.assertEqual(self.cfg.miniapp_public_url, "https://mini.example.invalid:8443")
        await self.launch()
        status, _, _ = await self.call(
            "GET", "/api/me", origin="https://mini.example.invalid:8443"
        )
        self.assertEqual(status, 200)
        self.assertEqual((await self.call("GET", "/api/me", origin=PUBLIC_ORIGIN))[0], 403)

    async def test_expanded_ipv6_origin_compresses_and_signed_write_succeeds(self):
        # Loopback transport stays HTTP 127.0.0.1; only the Origin string is compared
        # here. A browser serializes [0:0:0:0:0:0:0:1]:8443 as [::1]:8443, so the
        # expanded configured form must normalize to that exact value.
        expanded = "https://[0:0:0:0:0:0:0:1]:8443"
        self.cfg = replace(self.cfg, miniapp_public_url=expanded)
        self.assertEqual(self.cfg.miniapp_public_url, "https://[::1]:8443")
        await self.launch()
        revision = self.planner.get_settings(7)["revision"]
        body = {"name": "SYNTHETIC OWNER", "expected_revision": revision}
        status, payload, _ = await self.call(
            "PUT", "/api/name", body=body, origin="https://[::1]:8443"
        )
        self.assertEqual((status, payload["name"]), (200, "SYNTHETIC OWNER"))
        # The uncompressed spelling is not what a browser sends: it must not match.
        for other in (expanded, "https://[0:0:0:0:0:0:0:1]", "https://evil.example"):
            self.assertEqual(
                (await self.call("PUT", "/api/name", body=body, origin=other))[0], 403, other
            )

    async def test_mapped_ipv6_origin_uses_browser_hex_tail(self):
        # An IPv4-mapped IPv6 origin is serialized by browsers with a hex tail
        # ([::ffff:c000:201]), while the configured spelling may carry the dotted quad.
        # The normalized origin must equal the browser form so a real write is authorised.
        self.cfg = replace(
            self.cfg, miniapp_public_url="https://[::ffff:192.0.2.1]:8443"
        )
        self.assertEqual(self.cfg.miniapp_public_url, "https://[::ffff:c000:201]:8443")
        await self.launch()
        revision = self.planner.get_settings(7)["revision"]
        body = {"name": "SYNTHETIC OWNER", "expected_revision": revision}
        status, payload, _ = await self.call(
            "PUT", "/api/name", body=body, origin="https://[::ffff:c000:201]:8443"
        )
        self.assertEqual((status, payload["name"]), (200, "SYNTHETIC OWNER"))
        for other in (
            "https://[::ffff:192.0.2.1]:8443",
            "https://[0:0:0:0:0:ffff:c000:201]:8443",
            "https://evil.example",
        ):
            self.assertEqual(
                (await self.call("PUT", "/api/name", body=body, origin=other))[0], 403, other
            )

    def test_public_url_boundaries_and_idna(self):
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="http://mini.example.invalid")
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://mini.example.invalid/path")
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://mini.example.invalid?x=1")
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://mini.example.invalid#tab")
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://user@mini.example.invalid")
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://mini.example.invalid:99999")
        self.assertEqual(
            Config(miniapp_public_url="https://münchen.example/").miniapp_public_url,
            "https://xn--mnchen-3ya.example",
        )
        self.assertEqual(
            Config(miniapp_public_url="https://[2001:DB8::1]:8443").miniapp_public_url,
            "https://[2001:db8::1]:8443",
        )
        self.assertEqual(
            Config(miniapp_public_url="https://[2001:DB8:0:0:0:0:0:1]:443").miniapp_public_url,
            "https://[2001:db8::1]",
        )
        self.assertEqual(
            Config(miniapp_public_url="https://[::FFFF:192.0.2.1]:443").miniapp_public_url,
            "https://[::ffff:c000:201]",
        )
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://[fe80::1%eth0]")
        with self.assertRaises(ValueError):
            Config(miniapp_public_url="https://[not:an:ip]")
        self.assertEqual(Config().miniapp_public_url, "")

    async def test_rate_limit_is_per_owner(self):
        await self.launch()
        for index in range(60):
            self.assertEqual((await self.call("GET", "/api/me"))[0], 200, index)
        status, payload, headers = await self.call("GET", "/api/me")
        self.assertEqual(status, 429)
        self.assert_code(payload, "rate_limited")
        self.assertEqual(headers.get("Retry-After"), "60")
        # A different verified owner has an independent budget.
        self.assertEqual((await self.call("GET", "/api/me", uid=8))[0], 200)

    async def test_multiowner_scope_is_isolated(self):
        self.onboard(8, "OTHER SYNTHETIC OWNER", "wfh")
        await self.launch()
        _, plan7, _ = await self.call("GET", "/api/plan")
        _, plan8, _ = await self.call("GET", "/api/plan", uid=8)
        self.assertEqual(plan7["days"][0]["profile"], "normal")
        self.assertEqual(plan8["days"][0]["profile"], "wfh")
        _, me8, _ = await self.call("GET", "/api/me", uid=8)
        self.assertEqual((me8["name"], me8["onboarded"]), ("OTHER SYNTHETIC OWNER", True))
        _, view7, _ = await self.call("GET", "/api/today")
        self.assertEqual(view7["settings"]["attendance_name"], "SYNTHETIC OWNER")

    # --------------------------------------------------------------- contract

    async def test_me_name_settings_contract(self):
        await self.launch()
        payload = (await self.call("GET", "/api/me"))[2]
        settings = (await self.call("GET", "/api/me"))[1]["settings"]
        self.assertEqual(
            set(settings),
            {
                "attendance_name",
                "name_confirmed",
                "enabled",
                "prompt_time",
                "auto_time",
                "attendance_deadline",
                "time_margin_warning",
                "needs_review",
                "phone_configured",
            },
        )
        self.assertIsInstance(settings["enabled"], bool)
        self.assertIsInstance(settings["phone_configured"], bool)
        self.assertTrue(settings["phone_configured"])
        self.assertNotIn("uid", settings)
        self.assertNotIn("plan_revision", settings)
        self.assertNotIn("consent", settings)
        del payload
        # A second independent owner starts un-onboarded at revision 0.
        status, me, _ = await self.call("GET", "/api/me", uid=8)
        self.assertEqual((status, me["revision"], me["onboarded"]), (200, 0, False))
        status, payload, _ = await self.call(
            "PUT", "/api/name", uid=8, body={"name": " li  run ", "expected_revision": 0}
        )
        self.assertEqual((status, payload["name"], payload["onboarded"]), (200, "LI RUN", False))
        status, payload, _ = await self.call(
            "PUT",
            "/api/name",
            uid=8,
            body={"confirmed": True, "expected_revision": payload["revision"]},
        )
        self.assertEqual((status, payload["onboarded"]), (200, True))
        revision = payload["revision"]
        # Unknown settings fields are rejected, and enabling needs confirmation/OTP.
        status, payload, _ = await self.call(
            "PUT",
            "/api/settings",
            uid=8,
            body={
                "expected_revision": revision,
                "enabled": True,
                "prompt_time": "08:00",
                "auto_time": "08:30",
                "policy_accepted": True,
            },
        )
        self.assertEqual(status, 422)
        self.assert_code(payload, "invalid_request")
        status, payload, _ = await self.call(
            "PUT",
            "/api/settings",
            uid=8,
            body={
                "expected_revision": revision,
                "enabled": True,
                "prompt_time": "08:30",
                "auto_time": "08:00",
            },
        )
        self.assertEqual(status, 422)
        status, payload, _ = await self.call(
            "PUT",
            "/api/settings",
            uid=8,
            body={
                "expected_revision": revision,
                "enabled": True,
                "prompt_time": "08:00",
                "auto_time": "08:30",
            },
        )
        self.assertEqual((status, payload["settings"]["enabled"]), (200, True))

    async def test_plan_revisions_horizon_and_terminal_today(self):
        await self.launch()
        status, plan, _ = await self.call("GET", "/api/plan")
        self.assertEqual(status, 200)
        self.assertEqual(plan["today"], DAY)
        self.assertEqual(len(plan["days"]), 14)
        self.assertEqual(set(plan), {"today", "days", "revision"})
        self.assertEqual(set(plan["days"][0]), {"date", "profile", "details", "ready", "revision"})
        revision = plan["revision"]
        status, payload, _ = await self.call(
            "PUT", "/api/plan", body={"expected_revision": 0, "changes": []}
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            (
                await self.call(
                    "PUT",
                    "/api/plan",
                    body={
                        "expected_revision": 0,
                        "changes": [{"date": DAY, "profile": "wfh", "details": {}}],
                    },
                )
            )[0],
            409,
        )
        # An atomic batch with one out-of-horizon date changes nothing.
        future = "2026-10-16"
        status, payload, _ = await self.call(
            "PUT",
            "/api/plan",
            body={
                "expected_revision": revision,
                "changes": [
                    {"date": DAY, "profile": "wfh", "details": {}},
                    {"date": future, "profile": "normal", "details": {}},
                ],
            },
        )
        self.assertEqual((status, payload["code"]), (422, "invalid_request"))
        self.assertEqual(
            self.planner.get_plan(7)["days"][0]["profile"], "normal"
        )
        # An unbound confirmed local attempt blocks an ordinary today overwrite.
        attempt = self.store.prepare("synthetic-account-7", FORM, self.now.date(), "submit")
        self.store.transition(attempt, "running")
        self.store.transition(attempt, "submitting")
        self.store.transition(attempt, "confirmed")
        latest = (await self.call("GET", "/api/plan"))[1]["revision"]
        status, payload, _ = await self.call(
            "PUT",
            "/api/plan",
            body={
                "expected_revision": latest,
                "changes": [{"date": DAY, "profile": "wfh", "details": {}}],
            },
        )
        self.assertEqual((status, payload["code"]), (409, "blocked"))
        # A future date is still editable and returns the exact plan shape.
        status, payload, _ = await self.call(
            "PUT",
            "/api/plan",
            body={
                "expected_revision": latest,
                "changes": [{"date": "2026-10-03", "profile": "wfh", "details": {}}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"][1]["profile"], "wfh")

    async def test_today_composite_is_safe_and_truthful(self):
        await self.launch(records=StaticRecords(self.unavailable()))
        status, view, _ = await self.call("GET", "/api/today?refresh=1")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(view),
            {
                "today",
                "settings",
                "decision",
                "identity_suspended",
                "observation",
                "source_check",
                "local_outcome",
                "next_action",
                "next_action_at",
                "stage",
                "otp_ready",
            },
        )
        self.assertFalse(view["identity_suspended"])
        # An unavailable source is never rendered as an empty result.
        self.assertEqual(view["source_check"]["status"], "unavailable")
        self.assertEqual(view["source_check"]["error_code"], "network_error")
        self.assertNotIn("consent", view["decision"])
        self.assertNotIn("uid", view["settings"])
        self.assertTrue(view["otp_ready"])

    async def test_identity_collision_suspends_evidence_without_cross_owner_leak(self):
        # Two owners claim the same attendance name and department. The mapping is
        # ambiguous, so both are suspended before any daily decision settles and
        # neither view may expose records, source evidence or progress.
        self.onboard(8, "SYNTHETIC OWNER", "normal")
        records = StaticRecords(self.found())
        await self.launch(records=records)
        status, view7, _ = await self.call("GET", "/api/today?refresh=1")
        self.assertEqual(status, 200)
        self.assertTrue(view7["identity_suspended"])
        self.assertIsNone(view7["observation"])
        self.assertIsNone(view7["source_check"])
        self.assertIsNone(view7["stage"])
        self.assertEqual(view7["decision"]["reason"], "identity_collision")
        self.assertFalse(view7["settings"]["name_confirmed"])
        self.assertFalse(view7["settings"]["enabled"])
        # A suspended mapping performs no owner record lookup at all.
        self.assertEqual(records.lookups, 0)
        # The second owner is suspended by the same collision and sees no evidence
        # either; neither view carries the other owner's account identity.
        status, view8, _ = await self.call("GET", "/api/today?refresh=1", uid=8)
        self.assertEqual(status, 200)
        self.assertTrue(view8["identity_suspended"])
        self.assertIsNone(view8["source_check"])
        self.assertIsNone(view8["stage"])
        self.assertEqual(records.lookups, 0)
        self.assertNotIn("synthetic-account-8", json.dumps(view7))
        self.assertNotIn("synthetic-account-7", json.dumps(view8))

    async def test_action_schema_and_hold_restore_skip_submit(self):
        await self.launch()
        status, view, _ = await self.call("GET", "/api/today")
        self.assertEqual(status, 200)
        revision = view["decision"]["revision"]
        status, view, _ = await self.call(
            "POST",
            "/api/today/action",
            body={"action": "hold", "expected_revision": revision},
        )
        self.assertEqual((status, view["decision"]["state"]), (200, "held"))
        status, view, _ = await self.call(
            "POST",
            "/api/today/action",
            body={"action": "restore", "expected_revision": view["decision"]["revision"]},
        )
        self.assertEqual(view["decision"]["state"], "awaiting")
        held_revision = view["decision"]["revision"]
        status, view, _ = await self.call(
            "POST",
            "/api/today/action",
            body={"action": "skip", "expected_revision": held_revision},
        )
        self.assertEqual((status, view["decision"]["state"]), (200, "skipped"))
        # A stale revision conflicts instead of overwriting.
        status, payload, _ = await self.call(
            "POST",
            "/api/today/action",
            body={"action": "keep", "expected_revision": held_revision},
        )
        self.assertEqual((status, payload["code"]), (409, "revision_conflict"))

    async def test_additional_review_and_confirm_single_use(self):
        records = StaticRecords(self.found("first"), self.found("second"))
        await self.launch(records=records)
        status, view, _ = await self.call("GET", "/api/today?refresh=1")
        self.assertEqual(view["decision"]["state"], "recorded")
        revision = view["decision"]["revision"]
        status, review, _ = await self.call(
            "POST",
            "/api/today/action",
            body={"action": "additional_review", "expected_revision": revision, "profile": "normal"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(review),
            {
                "consent_digest",
                "revision",
                "records",
                "local_attempts",
                "answers",
                "profile",
                "details",
                "checked_at",
            },
        )
        self.assertEqual(review["profile"], "normal")
        self.assertTrue(review["records"])
        self.assertIsInstance(review["answers"], dict)
        self.assertEqual(review["answers"]["Department"], DEPARTMENT)
        # Confirming consumes the digest and returns the safe Today composite.
        status, view, _ = await self.call(
            "POST",
            "/api/today/action",
            body={
                "action": "additional_confirm",
                "expected_revision": review["revision"],
                "consent_digest": review["consent_digest"],
                "profile": "normal",
            },
        )
        self.assertEqual((status, view["decision"]["state"]), (200, "ready"))
        # The same digest can never authorise a second submission.
        status, payload, _ = await self.call(
            "POST",
            "/api/today/action",
            body={
                "action": "additional_review",
                "expected_revision": view["decision"]["revision"],
                "profile": "normal",
            },
        )
        self.assertEqual(status, 200)
        stale_digest = review["consent_digest"]
        status, payload, _ = await self.call(
            "POST",
            "/api/today/action",
            body={
                "action": "additional_confirm",
                "expected_revision": payload["revision"],
                "consent_digest": stale_digest,
                "profile": "normal",
            },
        )
        self.assertEqual((status, payload["code"]), (409, "blocked"))

    async def test_additional_confirm_rejects_changed_evidence(self):
        records = StaticRecords(self.not_found("a"), self.not_found("b"), self.not_found("c"))
        await self.launch(records=records)
        status, view, _ = await self.call("GET", "/api/today?refresh=1")
        self.assertEqual(status, 200)
        status, review, _ = await self.call(
            "POST",
            "/api/today/action",
            body={
                "action": "additional_review",
                "expected_revision": view["decision"]["revision"],
                "profile": "normal",
            },
        )
        self.assertEqual(status, 200)
        # The source changes before the owner confirms: the reviewed digest is void.
        status, view, _ = await self.call("GET", "/api/today?refresh=1")
        self.assertEqual(status, 200)
        status, payload, _ = await self.call(
            "POST",
            "/api/today/action",
            body={
                "action": "additional_confirm",
                "expected_revision": view["decision"]["revision"],
                "consent_digest": review["consent_digest"],
                "profile": "normal",
            },
        )
        self.assertEqual((status, payload["code"]), (409, "blocked"))

    async def test_static_whitelist_and_unknown_routes(self):
        await self.launch()
        status, page, headers = await self.text("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html", page.lower())
        self.assertIn("https://telegram.org", page)
        self.assertIn("telegram.org", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        for asset, mime in (
            ("/app.js", "text/javascript"),
            ("/style.css", "text/css"),
        ):
            status, _, asset_headers = await self.text("GET", asset)
            self.assertEqual(status, 200)
            self.assertIn(mime, asset_headers.get("Content-Type", ""))
        for blocked in ("/nope", "/%2e%2e/%2e%2e/etc/passwd", "/api/unknown", "/nope?uid=7"):
            self.assertEqual((await self.call("GET", blocked, auth=False))[0], 404)
        # A known API path still demands auth rather than 404-ing.
        self.assertEqual((await self.call("GET", "/api/plan", auth=False))[0], 401)

    # --------------------------------------------------------- source + OTP

    async def test_source_mismatch_blocks_start_and_transient_continues(self):
        mismatch = self.build(records=VerifyingRecords("source_mismatch", self.not_found()))
        server = MiniAppServer(self.cfg, mismatch)
        with self.assertRaises(MiniAppInstallError) as caught:
            await server.start()
        self.assertEqual(caught.exception.code, "source_mismatch")
        await server.stop()
        transient = self.build(records=VerifyingRecords("network_error", self.not_found()))
        await self.launch(scheduler=transient)
        self.assertEqual((await self.call("GET", "/api/me"))[0], 200)

    async def test_otp_gateway_forwards_bearer_and_ids(self):
        receiver = OtpHttpServer(lambda: {"cli": OTP_TOKEN}, 0)
        await receiver.start()
        self.addAsyncCleanup(receiver.stop)
        await self.launch(otp_server=receiver)
        pending = asyncio.create_task(receiver.provider("cli").wait_for_otp(30))
        self.addAsyncCleanup(lambda: pending.cancel())
        await asyncio.sleep(0)
        bearer = {"Authorization": "Bearer " + OTP_TOKEN}
        status, body, _ = await self.call("GET", "/otp/pending", auth=False, headers=bearer)
        self.assertEqual(status, 200)
        request_id = body["request_id"]
        # No Mini App identity is required or accepted on the bearer route.
        self.assertEqual(
            (
                await self.call(
                    "GET", "/otp/pending", auth=False, headers={"Authorization": "tma x"}
                )
            )[0],
            401,
        )
        self.assertEqual((await self.call("GET", "/otp", auth=False, headers=bearer))[0], 405)
        self.assertEqual(
            (
                await self.call(
                    "POST", "/otp", auth=False, body={"request_id": request_id, "otp": "123456"}
                )
            )[0],
            401,
        )
        status, body, _ = await self.call(
            "POST",
            "/otp",
            auth=False,
            headers=bearer,
            body={"request_id": request_id, "otp": "123456"},
        )
        self.assertEqual((status, body), (202, {"status": "accepted"}))
        self.assertEqual(await pending, "123456")
        # A stale request id is rejected by the receiver and passed through unchanged.
        status, body, _ = await self.call(
            "POST",
            "/otp",
            auth=False,
            headers=bearer,
            body={"request_id": request_id, "otp": "123456"},
        )
        self.assertEqual(status, 409)
        self.assertNotIn(OTP_TOKEN, json.dumps(body))
        # Oversized bodies never reach the receiver.
        status, _, _ = await self.call(
            "POST", "/otp", auth=False, headers=bearer, raw=b"x" * 2000
        )
        self.assertEqual(status, 413)

    async def test_otp_gateway_unavailable_without_receiver(self):
        await self.launch(otp_server=None)
        status, payload, _ = await self.call(
            "POST",
            "/otp",
            auth=False,
            headers={"Authorization": "Bearer " + OTP_TOKEN},
            body={"request_id": "x", "otp": "123456"},
        )
        self.assertEqual(status, 503)
        self.assert_code(payload, "unavailable")


if __name__ == "__main__":
    unittest.main()
