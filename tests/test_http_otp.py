"""Offline OTP delivery over real loopback HTTP, with no attendance execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout

from mdcattendance.__main__ import amain
from mdcattendance.config import Config, load_config
from mdcattendance.otp import validate_otp_token
from mdcattendance.server import OtpHttpServer
from mdcattendance.users import load_users

CLI_TOKEN = "a" * 32
TELEGRAM_TOKEN = "b" * 32
ROTATED_TOKEN = "c" * 32


class ManualInput:
    """Controllable manual input whose completion reveals cancellation/draining."""

    def __init__(self):
        self.value = asyncio.get_running_loop().create_future()
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.cancelled = False

    async def wait_for_otp(self, timeout):
        self.entered.set()
        try:
            async with asyncio.timeout(timeout):
                return await self.value
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.closed.set()


class HttpOtpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.credentials = {"cli": CLI_TOKEN, "7": TELEGRAM_TOKEN}
        self.server = OtpHttpServer(lambda: self.credentials, port=0)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.client = ClientSession(timeout=ClientTimeout(total=2), trust_env=False)
        self.addAsyncCleanup(self.client.close)
        self.providers = {owner: self.server.provider(owner) for owner in self.credentials}

    async def request(self, method, path="/otp", *, token=CLI_TOKEN, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if token is not None:
            headers.setdefault("Authorization", f"Bearer {token}")
        async with self.client.request(
            method,
            f"http://127.0.0.1:{self.server.port}{path}",
            headers=headers,
            **kwargs,
        ) as response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            text = await response.text()
            for secret in (CLI_TOKEN, TELEGRAM_TOKEN, ROTATED_TOKEN):
                self.assertNotIn(secret, text)
            submitted = kwargs.get("json")
            if isinstance(submitted, dict):
                otp = submitted.get("otp")
                if isinstance(otp, str) and len(otp) >= 6:
                    self.assertNotIn(otp, text)
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            return response.status, payload

    async def pending(self, token=CLI_TOKEN):
        async with asyncio.timeout(2):
            for _ in range(50):
                status, payload = await self.request("GET", "/otp/pending", token=token)
                if status == 200:
                    self.assertEqual(set(payload), {"request_id", "expires_in"})
                    self.assertIsInstance(payload["request_id"], str)
                    self.assertTrue(payload["request_id"])
                    self.assertGreater(payload["expires_in"], 0)
                    return payload["request_id"]
                self.assertEqual(status, 409)
                await asyncio.sleep(0)
        self.fail("OTP wait never became pending")

    def wait(self, owner="cli", *, fallback=None, timeout=5):
        provider = self.providers[owner] if fallback is None else self.server.provider(owner, fallback)
        task = asyncio.create_task(provider.wait_for_otp(timeout))
        self.addAsyncCleanup(self.cancel_task, task)
        return task

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def deliver(self, request_id, otp="123456", *, token=CLI_TOKEN):
        return await self.request("POST", token=token, json={"request_id": request_id, "otp": otp})

    async def test_authentication_is_required_and_revocation_is_immediate(self):
        wait = self.wait()
        request_id = await self.pending()
        for token in (None, "wrong-token-that-is-not-a-credential"):
            with self.subTest(token=token):
                self.assertEqual((await self.request("GET", "/otp/pending", token=token))[0], 401)
                self.assertEqual((await self.deliver(request_id, token=token))[0], 401)
        self.assertEqual(
            (await self.request("GET", f"/otp/pending?token={CLI_TOKEN}", token=None))[0],
            401,
        )
        self.assertEqual(
            (
                await self.request(
                    "GET",
                    "/otp/pending",
                    token=None,
                    headers={"Authorization": f"Basic {CLI_TOKEN}"},
                )
            )[0],
            401,
        )
        del self.credentials["cli"]
        self.assertEqual((await self.request("GET", "/otp/pending"))[0], 401)
        self.assertEqual((await self.deliver(request_id))[0], 401)
        self.assertFalse(wait.done())
        self.credentials["cli"] = ROTATED_TOKEN
        self.assertEqual(await self.pending(ROTATED_TOKEN), request_id)
        self.assertEqual((await self.deliver(request_id))[0], 401)
        self.assertEqual((await self.deliver(request_id, token=ROTATED_TOKEN))[0], 202)
        self.assertEqual(await asyncio.wait_for(wait, 2), "123456")

    async def test_two_owners_cannot_cross_deliver_or_reuse_a_consumed_request(self):
        cli_wait = self.wait()
        telegram_wait = self.wait("7")
        cli_id = await self.pending()
        telegram_id = await self.pending(TELEGRAM_TOKEN)
        self.assertNotEqual(cli_id, telegram_id)
        self.assertEqual((await self.deliver(cli_id, token=TELEGRAM_TOKEN))[0], 409)
        self.assertEqual((await self.deliver(telegram_id))[0], 409)
        self.assertFalse(cli_wait.done())
        self.assertFalse(telegram_wait.done())
        self.assertEqual(
            await self.deliver(telegram_id, "654321", token=TELEGRAM_TOKEN),
            (202, {"status": "accepted"}),
        )
        self.assertEqual(await asyncio.wait_for(telegram_wait, 2), "654321")
        self.assertFalse(cli_wait.done())
        self.assertEqual(await self.deliver(cli_id), (202, {"status": "accepted"}))
        self.assertEqual((await self.deliver(cli_id))[0], 409)
        self.assertEqual(await asyncio.wait_for(cli_wait, 2), "123456")
        for token in (CLI_TOKEN, TELEGRAM_TOKEN):
            self.assertEqual((await self.request("GET", "/otp/pending", token=token))[0], 409)

    async def test_early_delivery_is_rejected_not_queued_for_a_later_wait(self):
        self.assertEqual((await self.request("GET", "/otp/pending"))[0], 409)
        self.assertEqual((await self.deliver("not-an-active-request"))[0], 409)
        wait = self.wait()
        request_id = await self.pending()
        self.assertFalse(wait.done())
        self.assertEqual((await self.deliver("not-an-active-request"))[0], 409)
        self.assertFalse(wait.done())
        self.assertEqual((await self.deliver(request_id, "654321"))[0], 202)
        self.assertEqual(await asyncio.wait_for(wait, 2), "654321")

    async def test_invalid_otp_and_http_bodies_do_not_consume_the_pending_request(self):
        wait = self.wait()
        request_id = await self.pending()
        for otp in ("12345", "1234567", "１２３４５６", "١٢٣٤٥٦", " 123456", "123456\n", 123456, None):
            with self.subTest(otp=otp):
                self.assertEqual((await self.deliver(request_id, otp))[0], 422)
                self.assertFalse(wait.done())
        malformed = [
            ({"data": "{", "headers": {"Content-Type": "application/json"}}, 400),
            ({"data": "plain text", "headers": {"Content-Type": "text/plain"}}, 415),
            ({"json": {"request_id": request_id}}, 400),
            ({"json": {"request_id": request_id, "otp": "123456", "extra": True}}, 400),
            ({"json": []}, 400),
            ({"data": b" " * 1025, "headers": {"Content-Type": "application/json"}}, 413),
        ]
        for body, expected in malformed:
            with self.subTest(body=body):
                self.assertEqual((await self.request("POST", **body))[0], expected)
                self.assertFalse(wait.done())
        self.assertEqual(await self.pending(), request_id)
        body = json.dumps({"request_id": request_id, "otp": "000001"}).ljust(1024)
        self.assertEqual(
            (await self.request("POST", data=body, headers={"Content-Type": "application/json"}))[0],
            202,
        )
        self.assertEqual(await asyncio.wait_for(wait, 2), "000001")

    async def test_cancellation_expires_old_id_and_drains_manual_input(self):
        manual = ManualInput()
        first = self.wait(fallback=manual)
        old_id = await self.pending()
        await asyncio.wait_for(manual.entered.wait(), 2)
        first.cancel()
        async with asyncio.timeout(2):
            with self.assertRaises(asyncio.CancelledError):
                await first
        self.assertTrue(manual.closed.is_set())
        self.assertTrue(manual.cancelled)
        self.assertEqual((await self.request("GET", "/otp/pending"))[0], 409)
        self.assertEqual((await self.deliver(old_id))[0], 409)
        second = self.wait()
        current_id = await self.pending()
        self.assertNotEqual(current_id, old_id)
        self.assertEqual((await self.deliver(old_id))[0], 409)
        self.assertFalse(second.done())
        self.assertEqual((await self.deliver(current_id))[0], 202)
        self.assertEqual(await asyncio.wait_for(second, 2), "123456")

    async def test_timeout_expires_pending_state_and_drains_manual_input(self):
        manual = ManualInput()
        wait = self.wait(fallback=manual, timeout=0.02)
        async with asyncio.timeout(2):
            with self.assertRaises(TimeoutError):
                await wait
        self.assertTrue(manual.entered.is_set())
        self.assertTrue(manual.closed.is_set())
        self.assertTrue(manual.value.done())
        self.assertEqual((await self.request("GET", "/otp/pending"))[0], 409)
        self.assertEqual((await self.deliver("expired-request"))[0], 409)

    async def test_manual_and_http_winners_both_expire_state_and_drain_losers(self):
        for winner in ("manual", "http"):
            with self.subTest(winner=winner):
                manual = ManualInput()
                wait = self.wait(fallback=manual)
                request_id = await self.pending()
                await asyncio.wait_for(manual.entered.wait(), 2)
                if winner == "manual":
                    manual.value.set_result("654321")
                    expected = "654321"
                else:
                    self.assertEqual((await self.deliver(request_id))[0], 202)
                    expected = "123456"
                self.assertEqual(await asyncio.wait_for(wait, 2), expected)
                self.assertTrue(manual.closed.is_set())
                self.assertEqual(manual.cancelled, winner == "http")
                self.assertTrue(manual.value.done())
                self.assertEqual((await self.request("GET", "/otp/pending"))[0], 409)
                self.assertEqual((await self.deliver(request_id))[0], 409)

    async def test_malformed_http_does_not_echo_secrets_in_response_or_logs(self):
        records = []

        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        logger = logging.getLogger("aiohttp.server")
        handler = Recorder()
        logger.addHandler(handler)
        try:
            async with asyncio.timeout(2):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
                try:
                    writer.write(
                        (
                            "GET /otp/pending HTTP/1.1\r\n"
                            "Host: 127.0.0.1\r\n"
                            f"Authorization: Bearer {CLI_TOKEN}\r\n"
                            f"X Bad-{CLI_TOKEN}-123456: x\r\n\r\n"
                        ).encode("ascii")
                    )
                    await writer.drain()
                    response = await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()
        finally:
            logger.removeHandler(handler)
        head, body = response.split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        self.assertEqual(lines[0].split()[1], "400")
        headers = {
            name.lower(): value.strip() for name, value in (line.split(":", 1) for line in lines[1:])
        }
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("content-type", "").split(";")[0], "application/json")
        self.assertIsInstance(json.loads(body), dict)
        for secret in (CLI_TOKEN, "123456"):
            self.assertNotIn(secret, response.decode("utf-8"))
            self.assertNotIn(secret, "\n".join(records))

    async def test_rate_excess_is_rejected_without_creating_pending_input(self):
        responses = await asyncio.gather(*(self.request("GET", "/otp/pending") for _ in range(80)))
        statuses = [status for status, _ in responses]
        self.assertIn(429, statuses)
        self.assertLessEqual(set(statuses), {409, 429})

    async def test_stop_cancels_active_wait_and_is_idempotent(self):
        await self.server.start()
        manual = ManualInput()
        wait = self.wait(fallback=manual)
        await self.pending()
        await asyncio.wait_for(manual.entered.wait(), 2)
        url = f"http://127.0.0.1:{self.server.port}/otp/pending"
        async with asyncio.timeout(2):
            await self.server.stop()
            with self.assertRaises(asyncio.CancelledError):
                await wait
        self.assertTrue(manual.closed.is_set())
        self.assertTrue(manual.cancelled)
        await self.server.stop()
        with self.assertRaises(ClientConnectionError):
            async with self.client.get(url, headers={"Authorization": f"Bearer {CLI_TOKEN}"}):
                self.fail("Stopped OTP receiver still accepts connections")


class CliOtpCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_cli_rejects_missing_or_malformed_tokens_without_starting(self):
        args = Namespace(
            discover=False,
            preflight=False,
            dry_run=False,
            headed=False,
            otp_timeout=None,
            state_dir=None,
            http_otp=True,
            override=False,
            day_type="normal",
        )
        for token in ("", "short", "a" * 31 + "/", "a" * 31 + "é"):
            with self.subTest(token=token), TemporaryDirectory() as directory:
                cfg = Config(
                    singpass_id="offline-test-account",
                    singpass_password="not-a-credential",
                    form_url="https://example.invalid/form",
                    state_dir=directory,
                    otp_http_token=token,
                )
                error = StringIO()
                with (
                    patch("mdcattendance.__main__.load_config", return_value=cfg),
                    patch(
                        "mdcattendance.__main__.OtpHttpServer",
                        side_effect=AssertionError("Invalid token must not start a receiver"),
                    ),
                    patch(
                        "mdcattendance.__main__.AttendanceRunner",
                        side_effect=AssertionError("Invalid token must not start attendance"),
                    ),
                    redirect_stderr(error),
                ):
                    self.assertEqual(await amain(args), 2)
                self.assertFalse(any(Path(directory).iterdir()))
                if token:
                    self.assertNotIn(token, error.getvalue())


class OtpCredentialTests(unittest.TestCase):
    def test_url_safe_token_boundaries(self):
        for token in ("a" * 32, "Z_9-" * 32):
            self.assertEqual(validate_otp_token(token), token)
        for token in ("a" * 31, "a" * 129, "a" * 31 + " ", "a" * 31 + "/", "a" * 31 + "é"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                validate_otp_token(token)
        with self.assertRaises(ValueError):
            validate_otp_token("")

    def test_explicit_restricted_env_loads_http_settings_without_local_secrets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fake.env"
            path.write_text(
                f"OTP_HTTP_ENABLED=true\nOTP_HTTP_PORT=9012\nOTP_HTTP_TOKEN={CLI_TOKEN}\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            with patch.dict(os.environ, {"MDCATTENDANCE_ENV_FILE": str(path)}, clear=True):
                cfg = load_config()
            self.assertTrue(cfg.otp_http_enabled)
            self.assertEqual(cfg.otp_http_port, 9012)
            self.assertEqual(cfg.otp_http_token, CLI_TOKEN)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_allowlist_supports_manual_users_and_rejects_invalid_or_duplicate_tokens(self):
        def record(token=None):
            value = {"singpass_id": "offline-user", "singpass_password": "not-a-credential"}
            if token is not None:
                value["otp_token"] = token
            return value

        with TemporaryDirectory() as directory:
            path = Path(directory) / "fake-allowlist.json"
            path.write_text(
                json.dumps({"7": record(), "8": record(""), "9": record(TELEGRAM_TOKEN)}),
                encoding="utf-8",
            )
            path.chmod(0o600)
            users = load_users(str(path))
            self.assertEqual(users[7].otp_token, "")
            self.assertEqual(users[8].otp_token, "")
            self.assertEqual(users[9].otp_token, TELEGRAM_TOKEN)
            invalid = [
                {"7": record(CLI_TOKEN), "8": record(CLI_TOKEN)},
                {"7": record("short")},
                {"7": record("a" * 31 + "/")},
                {"7": record("a" * 31 + "é")},
                {"7": record(123456)},
            ]
            for raw in invalid:
                with self.subTest(raw=raw):
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_users(str(path))


if __name__ == "__main__":
    unittest.main()
