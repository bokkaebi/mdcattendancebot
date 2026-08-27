"""OTP providers: how the bot obtains the Singpass 2FA one-time password.

The default is interactive (``CliOtpProvider``): the operator types the OTP into
the terminal when the bot reaches the 2FA step. The HTTP bridge
(``server.OtpBridge``) is retained for when an external service posts the OTP;
enable it with ``--http-otp``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger("mdcattendance.otp")


class OtpProvider(Protocol):
    """A source of the 2FA one-time password."""

    async def wait_for_otp(self, timeout: float) -> str: ...


class CliOtpProvider:
    """Prompt the operator for the OTP on stdin.

    ``timeout`` is ignored: interactive entry takes as long as the operator
    needs. ``input()`` runs off the event loop (``asyncio.to_thread``) so the
    loop — and Ctrl+C — stay responsive while it blocks.
    """

    def __init__(self, prompt: str = "Enter the OTP shown on your device: ") -> None:
        self._prompt = prompt

    async def wait_for_otp(self, timeout: float) -> str:  # noqa: ARG002
        raw = await asyncio.to_thread(input, self._prompt)
        otp = raw.strip()
        if not otp:
            raise RuntimeError("no OTP was entered")
        return otp
