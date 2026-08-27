"""HTTP OTP receivers for the CLI and Telegram bot.

The CLI bridge uses one ``asyncio.Queue``. The Telegram receiver authenticates
requests and routes each OTP by chat id to the active interaction. Both uvicorn
servers share their caller's asyncio event loop with the Playwright flow.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from enum import StrEnum

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("mdcattendance.server")


class OtpRequest(BaseModel):
    """Body of ``POST /otp``."""

    otp: str = Field(..., min_length=1, description="The one-time password to enter.")


class TelegramOtpRequest(BaseModel):
    """Body of ``POST /telegram/otp``."""

    chat_id: int
    otp: str = Field(..., min_length=1, description="The one-time password to enter.")


class OtpDeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    NO_ACTIVE_RUN = "no_active_run"
    NOT_WAITING = "not_waiting"


OtpDeliverer = Callable[[int, str], Awaitable[OtpDeliveryStatus]]


class OtpBridge:
    """Single-slot channel: the browser flow awaits one OTP; the server delivers it."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.received: str | None = None

    async def submit(self, otp: str) -> None:
        self.received = otp
        await self._queue.put(otp)

    async def wait_for_otp(self, timeout: float) -> str:
        """Block until an OTP arrives via the HTTP endpoint, or ``timeout`` elapses."""
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)


def create_app(bridge: OtpBridge) -> FastAPI:
    app = FastAPI(title="mdcattendance OTP bridge", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/otp")
    async def submit_otp(req: OtpRequest) -> dict[str, str]:
        otp = req.otp.strip()
        if not otp:
            raise HTTPException(status_code=400, detail="otp must not be empty")
        await bridge.submit(otp)
        log.info("OTP accepted (%d chars)", len(otp))
        return {"status": "accepted"}

    return app




def create_telegram_otp_app(deliver: OtpDeliverer) -> FastAPI:
    """Create an unauthenticated, per-chat OTP receiver for the Telegram bot."""

    app = FastAPI(
        title="mdcattendance Telegram OTP receiver",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/telegram/otp", status_code=202)
    async def submit_telegram_otp(req: TelegramOtpRequest) -> dict[str, str]:
        otp = req.otp.strip()
        if not any(char.isdigit() for char in otp):
            raise HTTPException(status_code=422, detail="otp must contain at least one digit")

        result = await deliver(req.chat_id, otp)
        if result is OtpDeliveryStatus.NO_ACTIVE_RUN:
            raise HTTPException(status_code=404, detail="no active run for this chat_id")
        if result is OtpDeliveryStatus.NOT_WAITING:
            raise HTTPException(status_code=409, detail="active run is not waiting for an OTP")
        if result is not OtpDeliveryStatus.ACCEPTED:
            raise RuntimeError(f"unknown OTP delivery status: {result}")

        log.info("Telegram OTP accepted for chat %d (%d chars)", req.chat_id, len(otp))
        return {"status": result.value}
    return app


def make_server(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    # PTB owns SIGINT/SIGTERM; uvicorn must not install competing handlers.
    server.capture_signals = nullcontext  # type: ignore[method-assign]
    return server


async def serve_quietly(server: uvicorn.Server) -> None:
    """Run uvicorn without allowing its ``SystemExit`` to crash the shared loop."""
    try:
        await server.serve()
    except SystemExit:
        log.warning("OTP server exited (bind failed? port in use?)")


async def stop_server(
    server: uvicorn.Server,
    task: asyncio.Task[None],
    *,
    timeout: float = 5,
) -> None:
    """Request graceful shutdown, then force and cancel if it stalls."""
    server.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        server.force_exit = True
        try:
            await asyncio.wait_for(task, timeout=2)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
