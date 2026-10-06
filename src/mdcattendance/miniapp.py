"""Loopback Telegram Mini App gateway: signed-initData auth, owner-scoped JSON API.

The Mini App is served from the same origin as this API and proxies the two exact
OTP bearer routes to the independent loopback receiver. Nothing here trusts a
client-supplied uid, URL, profile or origin beyond the raw signed ``initData``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import ClientError, web

from .config import Config
from .records import SourceVerificationError
from .schedules import PlannerBlocked, PlannerConflict, PlannerInvalid

log = logging.getLogger("mdcattendance.miniapp")

_BODY_LIMIT = 16 * 1024
_OTP_BODY_LIMIT = 1024
_READ_TIMEOUT = 5
_PROXY_TIMEOUT = 10
_PROXY_MAX_RESPONSE = 8 * 1024
_RATE_LIMIT = 60
_RATE_WINDOW = 60.0
_AUTH_MAX_AGE = 3600
_AUTH_MAX_FUTURE = 30
_STATIC_FILES = ("index.html", "app.js", "style.css")
_STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
_OTP_PATHS = ("/otp", "/otp/pending")
_CSP = (
    "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://*.telegram.org"
)
# The decision row is durable server state; only UI-safe fields leave this process.
# ``consent`` carries the single-use review digest/answer hash and stays server-side.
_DECISION_FIELDS = (
    "day",
    "profile",
    "details",
    "state",
    "reason",
    "revision",
    "execute_at",
    "acknowledged",
    "reminder_sent",
    "prompt_sent",
    "attempt_id",
    "records_digest",
    "original",
)
_SETTINGS_FIELDS = (
    "attendance_name",
    "name_confirmed",
    "enabled",
    "prompt_time",
    "auto_time",
    "attendance_deadline",
    "time_margin_warning",
    "needs_review",
)
_ACTIONS = {
    "keep": set(),
    "hold": set(),
    "restore": set(),
    "skip": set(),
    "manual_submitted": set(),
    "submit_now": {"profile", "details"},
    "additional_review": {"profile", "details"},
    "additional_confirm": {"profile", "details", "consent_digest"},
}
_HTTP_LOG = logging.Logger(__name__)
_HTTP_LOG.disabled = True


class MiniAppInstallError(RuntimeError):
    """Explicit installation/configuration error (HTTP 500-class operator fault)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _security_headers(response: web.StreamResponse) -> None:
    headers = response.headers
    headers["Content-Security-Policy"] = _CSP
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    headers["Cache-Control"] = "no-store"


class _MiniAppRequest(web.BaseRequest):
    miniapp_handled: bool = False

    async def _prepare_hook(self, response: web.StreamResponse) -> None:
        # aiohttp parser failures bypass the handler. Rewrite those replies so the
        # default "400: Bad Request" body, which can echo request bytes, never ships.
        # This prepare hook is an aiohttp internal: review it when upgrading aiohttp.
        if not self.miniapp_handled and isinstance(response, web.Response):
            body = b'{"code":"malformed_request","message":"Invalid HTTP request"}'
            response.body = body
            response.content_type = "application/json"
            response.headers["Content-Length"] = str(len(body))
            self.writer.length = len(body)
        _security_headers(response)


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _init_data_fields(raw: str) -> dict[str, str] | None:
    """Parse raw Telegram initData; any duplicate key is a tamper signal."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:
            return None
        fields[key] = value
    return fields or None


def _verified_uid(fields: dict[str, str], bot_token: str, now: float) -> int | None:
    """Constant-time Telegram HMAC over the raw WebAppData key, then uid extraction.

    ``now`` is a Unix timestamp because ``auth_date`` is, unlike the loop clock.
    """
    if not bot_token:
        return None
    provided = fields.get("hash")
    if not isinstance(provided, str) or re.fullmatch(r"[0-9a-fA-F]{64}", provided) is None:
        return None
    check = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"
    )
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, provided.lower()):
        return None
    auth_date = fields.get("auth_date")
    if not isinstance(auth_date, str) or re.fullmatch(r"[0-9]{1,12}", auth_date) is None:
        return None
    age = now - int(auth_date)
    if age > _AUTH_MAX_AGE or age < -_AUTH_MAX_FUTURE:
        return None
    raw_user = fields.get("user")
    if not isinstance(raw_user, str) or not raw_user:
        return None
    try:
        user = json.loads(raw_user, object_pairs_hook=_strict_pairs)
    except (ValueError, TypeError):
        return None
    if not isinstance(user, dict):
        return None
    uid = user.get("id")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        return None
    return uid


class MiniAppServer:
    """One loopback HTTP server. The scheduler owns time and durable state."""

    def __init__(
        self,
        cfg: Config,
        scheduler: Any,
        *,
        otp_server: Any | None = None,
    ) -> None:
        if not isinstance(cfg, Config):
            raise TypeError("cfg must be a Config")
        self.cfg = cfg
        self.scheduler = scheduler
        self._otp_server = otp_server
        self._requested_port = cfg.miniapp_port
        self.port = cfg.miniapp_port
        self._public_origin = cfg.miniapp_public_url.rstrip("/")
        self._runner: web.ServerRunner | None = None
        self._client: Any | None = None
        self._running = False
        self._lifecycle_lock = asyncio.Lock()
        self._handlers: set[asyncio.Task] = set()
        self._rate: dict[int, tuple[int, float]] = {}
        self._static_assets: dict[str, bytes] = {}

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            await self._verify_source()
            self._static_assets = self._load_static()
            loop = asyncio.get_running_loop()
            http = web.Server(
                self._handle,
                request_factory=partial(
                    _MiniAppRequest, loop=loop, client_max_size=_BODY_LIMIT + 1
                ),
                handler_cancellation=True,
                access_log=None,
                logger=_HTTP_LOG,
                debug=False,
                keepalive_timeout=_READ_TIMEOUT,
                lingering_time=0,
                read_bufsize=_BODY_LIMIT,
                max_line_size=8192,
                max_field_size=8192,
                auto_decompress=False,
            )
            runner = web.ServerRunner(http, shutdown_timeout=1)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", self._requested_port).start()
                self.port = runner.addresses[0][1]
                self._client = self._new_client()
            except BaseException:
                await runner.cleanup()
                raise
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
            tasks = tuple(self._handlers)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._handlers.clear()
            self._rate.clear()
            client, self._client = self._client, None
            if client is not None:
                await client.close()
        finally:
            await runner.cleanup()

    @staticmethod
    def _new_client() -> Any:
        from aiohttp import ClientSession, ClientTimeout, DummyCookieJar

        # Anonymous, proxy-free, redirect-free: only the exact OTP loopback path is used.
        return ClientSession(
            cookie_jar=DummyCookieJar(),
            trust_env=False,
            timeout=ClientTimeout(total=_PROXY_TIMEOUT),
        )

    async def _verify_source(self) -> None:
        """A name/gid mismatch is an installation fault; transient reads stay non-fatal."""
        records = getattr(getattr(self.scheduler, "runner", None), "records", None)
        verify = getattr(records, "verify_source", None)
        if verify is None:
            return
        try:
            await verify()
        except SourceVerificationError as exc:
            if exc.code == "source_mismatch":
                raise MiniAppInstallError(
                    "source_mismatch",
                    "Configured worksheet name and gid disagree; correct the source "
                    "configuration before the Mini App can start",
                ) from exc
            log.warning(
                "Attendance source verification was unavailable at Mini App start (%s); "
                "owner lookups will surface it",
                exc.code,
            )
        except Exception:
            log.warning("Attendance source verification could not run; continuing unverified")

    def _load_static(self) -> dict[str, bytes]:
        directory = Path(__file__).with_name("web")
        assets: dict[str, bytes] = {}
        for name in _STATIC_FILES:
            try:
                assets[name] = (directory / name).read_bytes()
            except OSError as exc:
                raise MiniAppInstallError(
                    "missing_static", f"Packaged Mini App asset is missing: {name}"
                ) from exc
        return assets

    # ---------------------------------------------------------------------- auth

    def _users(self) -> dict[int, Any]:
        from .users import load_users

        try:
            return load_users(self.cfg.users_path)
        except Exception:
            log.error("Attendance allowlist could not be read for a Mini App request")
            raise _ApiError(503, "unavailable", "Authorisation service unavailable") from None

    def _authenticate(self, request: web.BaseRequest) -> tuple[int, dict[int, Any]]:
        if not self.cfg.telegram_bot_token:
            raise _ApiError(503, "unavailable", "Telegram authentication is not configured")
        headers = request.headers.getall("Authorization", [])
        if len(headers) != 1:
            raise _ApiError(401, "unauthorized", "Telegram authentication required")
        scheme, separator, raw = headers[0].partition(" ")
        if not separator or scheme.lower() != "tma" or not raw:
            raise _ApiError(401, "unauthorized", "Telegram authentication required")
        fields = _init_data_fields(raw)
        if fields is None:
            raise _ApiError(401, "unauthorized", "Telegram authentication required")
        uid = _verified_uid(fields, self.cfg.telegram_bot_token, time.time())
        if uid is None:
            raise _ApiError(401, "unauthorized", "Telegram authentication required")
        users = self._users()
        if uid not in users:
            raise _ApiError(403, "forbidden", "This account is not authorised")
        return uid, users

    def _check_origin(self, request: web.BaseRequest) -> None:
        origins = request.headers.getall("Origin", [])
        if not origins:
            return
        if len(origins) != 1 or not self._public_origin or origins[0] != self._public_origin:
            raise _ApiError(403, "forbidden", "Cross-origin request rejected")

    def _rate_limit(self, uid: int) -> None:
        now = asyncio.get_running_loop().time()
        count, start = self._rate.get(uid, (0, now))
        if now - start >= _RATE_WINDOW:
            count, start = 0, now
        if count >= _RATE_LIMIT:
            raise _ApiError(429, "rate_limited", "Too many requests; try again shortly")
        self._rate[uid] = (count + 1, start)

    # --------------------------------------------------------------------- body

    async def _read_body(self, request: web.BaseRequest, limit: int = _BODY_LIMIT) -> bytes:
        if request.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise _ApiError(415, "unsupported_media_type", "Compressed request bodies are rejected")
        if request.content_type.lower() != "application/json":
            raise _ApiError(415, "unsupported_media_type", "A JSON request body is required")
        length = request.content_length
        if length is not None and length > limit:
            raise _ApiError(413, "body_too_large", "Request body is too large")
        body = bytearray()
        try:
            async with asyncio.timeout(_READ_TIMEOUT):
                while len(body) <= limit:
                    chunk = await request.content.read(limit + 1 - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > limit:
                        raise _ApiError(413, "body_too_large", "Request body is too large")
        except TimeoutError:
            raise _ApiError(408, "read_timeout", "Request body timed out") from None
        except (ConnectionError, web.RequestPayloadError):
            raise _ApiError(400, "malformed_request", "Malformed request body") from None
        return bytes(body)

    async def _read_json(self, request: web.BaseRequest) -> dict[str, Any]:
        body = await self._read_body(request)
        try:
            payload = json.loads(body, object_pairs_hook=_strict_pairs)
        except (ValueError, UnicodeDecodeError):
            raise _ApiError(400, "malformed_request", "Malformed JSON body") from None
        if not isinstance(payload, dict):
            raise _ApiError(400, "malformed_request", "JSON body must be an object")
        return payload

    @staticmethod
    def _exact_keys(
        payload: dict[str, Any],
        required: set[str],
        optional: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        unknown = set(payload) - required - optional
        missing = required - set(payload)
        if unknown or missing:
            raise _ApiError(422, "invalid_request", "Unexpected or missing request fields")

    # ------------------------------------------------------------------ requests

    async def _handle(self, request: web.BaseRequest) -> web.Response:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            if not self._running:
                response = self._error(503, "unavailable", "Server is shutting down")
            elif request.path in _OTP_PATHS:
                response = await self._otp(request)
            elif request.path.startswith("/api/"):
                response = await self._api(request)
            else:
                response = self._static(request)
        except _ApiError as exc:
            response = self._error(exc.status, exc.code, exc.message)
        except PlannerConflict as exc:
            response = self._error(409, "revision_conflict", str(exc))
        except PlannerBlocked as exc:
            response = self._error(409, "blocked", str(exc))
        except PlannerInvalid as exc:
            response = self._error(422, "invalid_request", str(exc))
        except TimeoutError:
            response = self._error(409, "blocked", "The attendance deadline has passed")
        except ValueError as exc:
            response = self._error(422, "invalid_request", str(exc))
        except RuntimeError as exc:
            response = self._error(503, "unavailable", str(exc))
        except Exception:
            log.error("Mini App request failed; sensitive details omitted")
            response = self._error(500, "internal_error", "The request could not be completed")
        finally:
            if task is not None:
                self._handlers.discard(task)
        _security_headers(response)
        request.miniapp_handled = True  # type: ignore[attr-defined]
        response.force_close()
        return response

    @staticmethod
    def _error(status: int, code: str, message: str) -> web.Response:
        response = web.json_response({"code": code, "message": message}, status=status)
        if status == 429:
            response.headers["Retry-After"] = "60"
        return response

    def _static(self, request: web.BaseRequest) -> web.Response:
        if request.method not in {"GET", "HEAD"}:
            return self._error(405, "method_not_allowed", "Method not allowed")
        name = "index.html" if request.path == "/" else request.path.lstrip("/")
        if name not in self._static_assets or "/" in name or "\\" in name or name != name.strip():
            return self._error(404, "not_found", "Not found")
        response = web.Response(
            body=self._static_assets[name],
            headers={"Content-Type": _STATIC_TYPES[name]},
        )
        if request.method == "HEAD":
            response.body = b""
        return response

    async def _api(self, request: web.BaseRequest) -> web.Response:
        path = request.path
        method = request.method
        if path == "/api/me":
            if method != "GET":
                return self._error(405, "method_not_allowed", "Method not allowed")
            uid, users = self._authenticate(request)
            self._check_origin(request)
            self._rate_limit(uid)
            return self._json(self._me(uid, users))
        if path == "/api/name":
            if method != "PUT":
                return self._error(405, "method_not_allowed", "Method not allowed")
            uid, users = self._authenticate(request)
            self._check_origin(request)
            self._rate_limit(uid)
            return self._json(self._put_name(uid, users, await self._read_json(request)))
        if path == "/api/plan":
            uid, users = self._authenticate(request)
            self._check_origin(request)
            self._rate_limit(uid)
            if method == "GET":
                self._require_query(request, ())
                plan = self._planner().get_plan(uid)
                return self._json(plan)
            if method == "PUT":
                payload = await self._read_json(request)
                self._exact_keys(payload, {"expected_revision", "changes"})
                changes = payload["changes"]
                if not isinstance(changes, list):
                    raise _ApiError(422, "invalid_request", "changes must be a list")
                for change in changes:
                    if not isinstance(change, dict):
                        raise _ApiError(422, "invalid_request", "Each change must be an object")
                    self._exact_keys(change, {"date", "profile", "details"})
                expected = self._revision(payload["expected_revision"])
                plan = await self.scheduler.save_plan(uid, changes, expected_revision=expected)
                return self._json(plan)
            return self._error(405, "method_not_allowed", "Method not allowed")
        if path == "/api/today":
            if method != "GET":
                return self._error(405, "method_not_allowed", "Method not allowed")
            uid, _users = self._authenticate(request)
            self._check_origin(request)
            self._rate_limit(uid)
            self._require_query(request, ("refresh",))
            refresh = request.query.get("refresh")
            if refresh is not None and refresh != "1":
                raise _ApiError(422, "invalid_request", "refresh must be 1")
            view = await self.scheduler.today(uid, fresh=refresh == "1")
            return self._json(self._today_view(uid, view))
        if path == "/api/today/action":
            if method != "POST":
                return self._error(405, "method_not_allowed", "Method not allowed")
            uid, _users = self._authenticate(request)
            self._check_origin(request)
            self._rate_limit(uid)
            payload = await self._read_json(request)
            action = payload.get("action")
            if not isinstance(action, str) or action not in _ACTIONS:
                raise _ApiError(422, "invalid_request", "Unsupported action")
            required = {"action", "expected_revision"}
            if action == "additional_confirm":
                required.add("consent_digest")
            self._exact_keys(payload, required, _ACTIONS[action] - required)
            expected = self._revision(payload["expected_revision"])
            profile = payload.get("profile")
            details = payload.get("details")
            if "profile" in payload and not isinstance(profile, str):
                raise _ApiError(422, "invalid_request", "profile must be text")
            if "details" in payload and not isinstance(details, dict):
                raise _ApiError(422, "invalid_request", "details must be an object")
            if "consent_digest" in payload and not isinstance(payload["consent_digest"], str):
                raise _ApiError(422, "invalid_request", "consent_digest must be text")
            if action == "additional_review":
                return self._json(
                    await self._review(uid, expected, profile=profile, details=details)
                )
            await self.scheduler.action(
                uid,
                action,
                expected_revision=expected,
                profile=profile,
                details=details,
                consent_digest=payload.get("consent_digest"),
            )
            view = await self.scheduler.today(uid)
            return self._json(self._today_view(uid, view))
        if path == "/api/settings":
            if method != "PUT":
                return self._error(405, "method_not_allowed", "Method not allowed")
            uid, users = self._authenticate(request)
            self._check_origin(request)
            self._rate_limit(uid)
            payload = await self._read_json(request)
            self._exact_keys(payload, {"expected_revision", "enabled", "prompt_time", "auto_time"})
            enabled = payload["enabled"]
            prompt_time = payload["prompt_time"]
            auto_time = payload["auto_time"]
            if not isinstance(enabled, bool):
                raise _ApiError(422, "invalid_request", "enabled must be a boolean")
            if not isinstance(prompt_time, str) or not isinstance(auto_time, str):
                raise _ApiError(422, "invalid_request", "Times must use HH:MM")
            expected = self._revision(payload["expected_revision"])
            planner = self._planner()
            planner.save_settings(
                uid,
                expected_revision=expected,
                enabled=enabled,
                prompt_time=prompt_time,
                auto_time=auto_time,
                otp_ready=self.scheduler.otp_ready(uid),
                # An authenticated enable request is the explicit silent-execution
                # policy acceptance; there is no separate client-supplied flag.
                policy_accepted=enabled,
            )
            return self._json(self._me(uid, users))
        return self._error(404, "not_found", "Not found")

    @staticmethod
    def _require_query(request: web.BaseRequest, allowed: tuple[str, ...]) -> None:
        unknown = set(request.query) - set(allowed)
        if unknown:
            raise _ApiError(422, "invalid_request", "Unsupported query parameter")

    @staticmethod
    def _revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _ApiError(422, "invalid_request", "expected_revision must be a non-negative integer")
        return value

    def _planner(self) -> Any:
        try:
            return self.scheduler.planner
        except RuntimeError:
            raise _ApiError(503, "unavailable", "Planner is not available") from None

    def _json(self, payload: Any) -> web.Response:
        return web.json_response(payload)

    # --------------------------------------------------------------------- views

    def _safe_settings(self, settings: dict, uid: int) -> dict:
        result = {field: settings[field] for field in _SETTINGS_FIELDS}
        result["phone_configured"] = bool(self.scheduler.otp_ready(uid))
        return result

    @staticmethod
    def _safe_decision(decision: dict) -> dict:
        return {field: decision[field] for field in _DECISION_FIELDS if field in decision}

    def _today_view(self, uid: int, view: dict) -> dict:
        result = dict(view)
        if isinstance(result.get("settings"), dict):
            result["settings"] = self._safe_settings(result["settings"], uid)
        if isinstance(result.get("decision"), dict):
            result["decision"] = self._safe_decision(result["decision"])
        return result

    def _me(self, uid: int, users: dict[int, Any]) -> dict:
        settings = self._planner().get_settings(uid)
        user = users.get(uid)
        safe = self._safe_settings(settings, uid)
        name = safe["attendance_name"] or ""
        department = getattr(user, "department", "") or ""
        return {
            "name": name,
            "department": department,
            "onboarded": bool(safe["name_confirmed"] and name),
            "settings": safe,
            "revision": settings["revision"],
        }

    async def _review(
        self,
        uid: int,
        expected_revision: int,
        *,
        profile: str | None,
        details: dict | None,
    ) -> dict:
        """Reviewed single-use digest plus the exact owner evidence and answers.

        Canonical answers come from the same owner-only preparation the submission
        will use, so changed inputs recompute rather than trusting the digest alone.
        The scheduler's evidence refresh runs inside ``action``; the digest binds
        whatever it stored, and a replay or changed source fails at confirm time.
        """
        planner = self._planner()
        day = planner.ensure_decision(uid)["day"]
        # Records leave this process only when this identity maps to exactly one
        # provisioned department; an ambiguous mapping blocks exposure here too.
        self._assert_identity(uid, self._users())
        chosen_details, answers = await self.scheduler._answers_for(uid, day, profile, details)
        result = await self.scheduler.action(
            uid,
            "additional_review",
            expected_revision=expected_revision,
            profile=profile,
            details=details,
        )
        decision = planner.get_decision(uid) or {}
        observation = planner.get_observation(uid, day) or {}
        consent = result.get("consent") or {}
        return {
            "consent_digest": result["consent_digest"],
            "revision": result["revision"],
            "records": observation.get("records", []),
            "local_attempts": consent.get("local_attempt_ids", []),
            "answers": answers,
            "profile": decision.get("profile"),
            "details": chosen_details or {},
            "checked_at": observation.get("checked_at"),
        }

    def _assert_identity(self, uid: int, users: dict[int, Any]) -> None:
        user = users.get(uid)
        department = getattr(user, "department", "") or ""
        departments = {owner: getattr(row, "department", "") or "" for owner, row in users.items()}
        self._planner().assert_identity_unique(uid, department, departments)

    def _put_name(self, uid: int, users: dict[int, Any], payload: dict[str, Any]) -> dict:
        planner = self._planner()
        if set(payload) == {"name", "expected_revision"}:
            if not isinstance(payload["name"], str):
                raise _ApiError(422, "invalid_request", "name must be text")
            planner.set_name(
                uid, payload["name"], expected_revision=self._revision(payload["expected_revision"])
            )
        elif set(payload) == {"confirmed", "expected_revision"}:
            if payload["confirmed"] is not True:
                raise _ApiError(422, "invalid_request", "confirmed must be true")
            planner.confirm_name(uid, expected_revision=self._revision(payload["expected_revision"]))
        else:
            raise _ApiError(422, "invalid_request", "Unexpected or missing request fields")
        self._assert_identity(uid, users)
        return self._me(uid, users)

    # ------------------------------------------------------------------- gateway

    async def _otp(self, request: web.BaseRequest) -> web.Response:
        server = self._otp_server
        if server is None or not getattr(server, "_running", False):
            raise _ApiError(503, "unavailable", "OTP receiver is unavailable")
        if request.path == "/otp/pending":
            if request.method != "GET":
                return self._error(405, "method_not_allowed", "Method not allowed")
            method: str = "GET"
            body: bytes | None = None
        else:
            if request.method != "POST":
                return self._error(405, "method_not_allowed", "Method not allowed")
            body = await self._read_body(request, _OTP_BODY_LIMIT)
            method = "POST"
        authorization = request.headers.getall("Authorization", [])
        if len(authorization) != 1:
            raise _ApiError(401, "unauthorized", "OTP authentication required")
        headers = {"Authorization": authorization[0], "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = f"http://127.0.0.1:{server.port}{request.path}"
        client = self._client
        if client is None:
            raise _ApiError(503, "unavailable", "OTP receiver is unavailable")
        try:
            async with asyncio.timeout(_PROXY_TIMEOUT):
                async with client.request(
                    method, url, data=body, headers=headers, allow_redirects=False
                ) as upstream:
                    payload = await upstream.read()
                    status = upstream.status
                    content_type = upstream.headers.get("Content-Type", "application/json")
        except (TimeoutError, OSError, ClientError):
            raise _ApiError(503, "unavailable", "OTP receiver is unavailable") from None
        return web.Response(
            body=payload[:_PROXY_MAX_RESPONSE],
            status=status,
            headers={"Content-Type": content_type},
        )
