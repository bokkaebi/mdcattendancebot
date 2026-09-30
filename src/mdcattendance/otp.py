"""Bounded OTP input and validation shared by terminal, Telegram and HTTP."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Protocol


class OtpProvider(Protocol):
    async def wait_for_otp(self, timeout: float) -> str: ...


def validate_otp(text: str) -> str:
    value = text.strip()
    if re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError("OTP must contain exactly six ASCII digits")
    return value


def validate_otp_token(token: str) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
        raise ValueError("OTP token must contain 32–128 URL-safe ASCII characters")
    return token


async def read_terminal_line(prompt: str, timeout: float) -> str:
    """Wait on stdin readiness; always remove the reader on timeout/cancellation."""
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    future: asyncio.Future[str] = loop.create_future()
    data = bytearray()
    print(prompt, end="", flush=True)

    def ready() -> None:
        if future.done():
            return
        try:
            chunk = os.read(fd, 1)
            if not chunk:
                raise EOFError("Terminal input closed")
            if chunk == b"\n":
                future.set_result(data.decode(sys.stdin.encoding or "utf-8"))
            else:
                data.extend(chunk)
                if len(data) > 4096:
                    raise ValueError("Terminal input too long")
        except Exception as exc:
            future.set_exception(exc)

    try:
        loop.add_reader(fd, ready)
    except (PermissionError, NotImplementedError) as exc:
        raise RuntimeError("Interactive input requires a terminal or pipe") from exc
    try:
        async with asyncio.timeout(timeout):
            return await future
    finally:
        loop.remove_reader(fd)
        data.clear()


class CliOtpProvider:
    def __init__(self, prompt: str = "Enter the six-digit OTP shown on your device: ") -> None:
        self._prompt = prompt

    async def wait_for_otp(self, timeout: float) -> str:
        return validate_otp(await read_terminal_line(self._prompt, timeout))
