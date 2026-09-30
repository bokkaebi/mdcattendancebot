"""Loopback-only, owner-authenticated delivery for the currently awaited OTP."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import cast

from aiohttp import web

from .otp import OtpProvider, validate_otp, validate_otp_token

_BODY_LIMIT = 1024
_READ_TIMEOUT = 5
_RATE_BURST = 60
_RATE_PER_SECOND = 1
_HTTP_LOG = logging.Logger(__name__)
_HTTP_LOG.disabled = True


class _OtpRequest(web.BaseRequest):
    otp_handled: bool = False

    async def _prepare_hook(self, response: web.StreamResponse) -> None:
        # aiohttp parser failures bypass the application handler. Its request_factory
        # still creates this request; keep those replies and parser logs secret-free.
        # This prepare hook is an aiohttp internal: review it when upgrading aiohttp.
        if not self.otp_handled and isinstance(response, web.Response):
            body = b'{"error":"invalid HTTP request"}'
            response.body = body
            response.content_type = "application/json"
            response.headers["Content-Length"] = str(len(body))
            self.writer.length = len(body)
        response.headers["Cache-Control"] = "no-store"


@dataclass
class _Pending:
    owner: str
    request_id: str
    expires_at: float
    future: asyncio.Future[str]


class _HttpOtpProvider:
    def __init__(self, server: OtpHttpServer, owner: str, fallback: OtpProvider | None) -> None:
        self._server = server
        self._owner = owner
        self._fallback = fallback

    async def wait_for_otp(self, timeout: float) -> str:
        if not self._server._running:
            raise RuntimeError("OTP HTTP receiver is not running")
        task = asyncio.create_task(self._server._wait_for_otp(self._owner, self._fallback, timeout))
        self._server._waiters.add(task)
        try:
            return await task
        finally:
            self._server._waiters.discard(task)


class OtpHttpServer:
    def __init__(self, credentials: Callable[[], Mapping[str, str]], port: int = 8765) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("OTP HTTP port must be between 0 and 65535")
        self._credentials = credentials
        self._requested_port = port
        self.port = port
        self._runner: web.ServerRunner | None = None
        self._running = False
        self._lifecycle_lock = asyncio.Lock()
        self._pending: dict[str, _Pending] = {}
        self._waiters: set[asyncio.Task[str]] = set()
        self._handlers: set[asyncio.Task] = set()
        self._rate_tokens = float(_RATE_BURST)
        self._rate_updated = 0.0

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            loop = asyncio.get_running_loop()
            http = web.Server(
                self._handle,
                request_factory=partial(_OtpRequest, loop=loop, client_max_size=_BODY_LIMIT + 1),
                handler_cancellation=True,
                access_log=None,
                logger=_HTTP_LOG,
                debug=False,
                keepalive_timeout=_READ_TIMEOUT,
                lingering_time=0,
                read_bufsize=4096,
                max_line_size=4096,
                max_field_size=4096,
                auto_decompress=False,
            )
            runner = web.ServerRunner(http, shutdown_timeout=1)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", self._requested_port).start()
                self.port = runner.addresses[0][1]
            except BaseException:
                await runner.cleanup()
                raise
            self._rate_tokens = float(_RATE_BURST)
            self._rate_updated = loop.time()
            self._runner = runner
            self._running = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            runner = self._runner
            if runner is None:
                return
            self._running = False
            cleanup = asyncio.create_task(self._shutdown(runner))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                self._runner = None

    async def _shutdown(self, runner: web.ServerRunner) -> None:
        try:
            tasks = tuple(self._waiters | self._handlers)
            for task in tasks:
                task.cancel()
            for pending in self._pending.values():
                pending.future.cancel()
            self._pending.clear()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await runner.cleanup()

    def provider(self, owner: str, fallback: OtpProvider | None = None) -> OtpProvider:
        if not isinstance(owner, str) or not owner:
            raise ValueError("OTP owner must be a non-empty string")
        return _HttpOtpProvider(self, owner, fallback)

    def _remove(self, pending: _Pending) -> bool:
        if self._pending.get(pending.owner) is not pending:
            return False
        del self._pending[pending.owner]
        return True

    def _current(self, owner: str) -> _Pending | None:
        pending = self._pending.get(owner)
        if pending is not None and (
            pending.future.done() or pending.expires_at <= asyncio.get_running_loop().time()
        ):
            self._remove(pending)
            if not pending.future.done():
                pending.future.set_exception(TimeoutError("OTP request expired"))
            return None
        return pending

    async def _wait_for_otp(self, owner: str, fallback: OtpProvider | None, timeout: float) -> str:
        if not math.isfinite(timeout):
            raise ValueError("OTP timeout must be finite")
        if timeout <= 0:
            raise TimeoutError("OTP request expired")
        if self._current(owner) is not None:
            raise RuntimeError("An OTP request is already pending for this owner")
        loop = asyncio.get_running_loop()
        pending = _Pending(owner, secrets.token_urlsafe(32), loop.time() + timeout, loop.create_future())
        self._pending[owner] = pending
        manual = (
            asyncio.create_task(self._manual(pending, fallback, timeout))
            if fallback is not None
            else None
        )
        try:
            async with asyncio.timeout(timeout):
                return await pending.future
        finally:
            self._remove(pending)
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()
            if manual is not None:
                manual.cancel()
                await asyncio.gather(manual, return_exceptions=True)

    async def _manual(self, pending: _Pending, fallback: OtpProvider, timeout: float) -> None:
        try:
            code = validate_otp(await fallback.wait_for_otp(timeout))
        except asyncio.CancelledError:
            if self._current(pending.owner) is pending and self._remove(pending):
                pending.future.cancel()
            raise
        except Exception as exc:
            if self._current(pending.owner) is pending and self._remove(pending):
                pending.future.set_exception(exc)
        else:
            if self._current(pending.owner) is pending and self._remove(pending):
                pending.future.set_result(code)

    def _owner(self, request: web.BaseRequest) -> str | None:
        headers = request.headers.getall("Authorization", [])
        if len(headers) != 1:
            return None
        scheme, separator, presented = headers[0].partition(" ")
        if not separator or scheme.lower() != "bearer":
            return None
        try:
            validate_otp_token(presented)
            owners = list(self._credentials().items())
            seen: set[str] = set()
            authenticated = None
            for owner, token in owners:
                if not isinstance(owner, str) or not owner:
                    return None
                validate_otp_token(token)
                if token in seen:
                    return None
                seen.add(token)
                if hmac.compare_digest(token, presented):
                    authenticated = owner
            return authenticated
        except Exception:
            return None

    def _allow_request(self) -> bool:
        now = asyncio.get_running_loop().time()
        # ponytail: one global bucket; per-owner buckets only if one phone crowds out others.
        self._rate_tokens = min(
            _RATE_BURST, self._rate_tokens + (now - self._rate_updated) * _RATE_PER_SECOND
        )
        self._rate_updated = now
        if self._rate_tokens < 1:
            return False
        self._rate_tokens -= 1
        return True

    @staticmethod
    def _error(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    async def _handle(self, request: web.BaseRequest) -> web.Response:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            if not self._running:
                response = self._error(503, "receiver unavailable")
            elif not self._allow_request():
                response = self._error(429, "rate limit exceeded")
            else:
                owner = self._owner(request)
                if owner is None:
                    response = self._error(401, "unauthorized")
                elif request.path == "/otp/pending":
                    response = (
                        self._get_pending(owner)
                        if request.method == "GET"
                        else self._error(405, "method not allowed")
                    )
                elif request.path == "/otp":
                    response = (
                        await self._post_otp(request, owner)
                        if request.method == "POST"
                        else self._error(405, "method not allowed")
                    )
                else:
                    response = self._error(404, "not found")
            cast(_OtpRequest, request).otp_handled = True
            response.force_close()
            return response
        finally:
            if task is not None:
                self._handlers.discard(task)

    def _get_pending(self, owner: str) -> web.Response:
        pending = self._current(owner)
        if pending is None:
            return self._error(409, "no pending OTP request")
        return web.json_response(
            {
                "request_id": pending.request_id,
                "expires_in": max(0, pending.expires_at - asyncio.get_running_loop().time()),
            }
        )

    @staticmethod
    def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    async def _post_otp(self, request: web.BaseRequest, owner: str) -> web.Response:
        if request.content_length is not None and request.content_length > _BODY_LIMIT:
            return self._error(413, "request body too large")
        if (
            request.content_type != "application/json"
            or request.headers.get("Content-Encoding", "identity").lower() != "identity"
        ):
            return self._error(415, "JSON required")
        body = bytearray()
        try:
            async with asyncio.timeout(_READ_TIMEOUT):
                while len(body) <= _BODY_LIMIT:
                    chunk = await request.content.read(_BODY_LIMIT + 1 - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > _BODY_LIMIT:
                        return self._error(413, "request body too large")
            payload = json.loads(body, object_pairs_hook=self._json_object)
        except TimeoutError:
            return self._error(408, "request body timed out")
        except (ValueError, ConnectionError, web.RequestPayloadError):
            return self._error(400, "malformed JSON")
        finally:
            body.clear()
        if self._owner(request) != owner:
            return self._error(401, "unauthorized")
        if not isinstance(payload, dict) or set(payload) != {"request_id", "otp"}:
            return self._error(400, "invalid JSON fields")
        if not isinstance(payload["request_id"], str):
            return self._error(400, "invalid request id")
        code = payload.pop("otp")
        if not isinstance(code, str) or len(code) != 6:
            return self._error(422, "OTP must contain six ASCII digits")
        try:
            code = validate_otp(code)
        except ValueError:
            return self._error(422, "OTP must contain six ASCII digits")
        pending = self._current(owner)
        if pending is None or pending.request_id != payload["request_id"]:
            return self._error(409, "OTP request is not pending")
        self._remove(pending)
        pending.future.set_result(code)
        return web.json_response({"status": "accepted"}, status=202)
