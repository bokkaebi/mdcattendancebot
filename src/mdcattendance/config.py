"""Configuration loaded from environment / .env for the mdcattendance bot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_FORM_URL = "https://form.gov.sg/6818179de1b7fe5377eb5c20"


@dataclass(frozen=True)
class Config:
    singpass_id: str
    singpass_password: str
    form_url: str
    otp_host: str
    otp_port: int
    otp_timeout: float
    chromium_executable_path: str | None
    headless: bool
    slow_mo: int
    discover: bool
    preflight: bool
    dry_run: bool
    navigation_timeout_ms: int
    form_settle_ms: int
    telegram_bot_token: str
    users_path: str
    max_concurrent_runs: int
    prompt_timeout: float
    telegram_otp_host: str
    telegram_otp_port: int
    schedules_path: str


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    """Load configuration from environment / .env.

    Singpass credentials are validated by the caller (``__main__``), so that
    ``--preflight`` can verify selectors without credentials being set.
    """
    load_dotenv()

    singpass_id = os.getenv("SINGPASS_ID", "").strip()
    singpass_password = os.getenv("SINGPASS_PASSWORD", "").strip()

    return Config(
        singpass_id=singpass_id,
        singpass_password=singpass_password,
        form_url=os.getenv("FORM_URL", DEFAULT_FORM_URL).strip(),
        otp_host=os.getenv("OTP_HOST", "127.0.0.1").strip(),
        otp_port=int(os.getenv("OTP_PORT", "8080")),
        otp_timeout=float(os.getenv("OTP_TIMEOUT", "300")),
        chromium_executable_path=(os.getenv("CHROMIUM_EXECUTABLE_PATH") or "").strip()
        or None,
        headless=_bool(os.getenv("HEADLESS"), default=True),
        slow_mo=int(os.getenv("SLOW_MO", "0")),
        discover=_bool(os.getenv("DISCOVER")),
        preflight=_bool(os.getenv("PREFLIGHT")),
        dry_run=False,
        navigation_timeout_ms=int(os.getenv("NAVIGATION_TIMEOUT_MS", "60000")),
        form_settle_ms=int(os.getenv("FORM_SETTLE_MS", "3000")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        users_path=os.getenv("TELEGRAM_USERS_PATH", "users.json").strip(),
        max_concurrent_runs=int(os.getenv("MAX_CONCURRENT_RUNS", "2")),
        prompt_timeout=float(os.getenv("PROMPT_TIMEOUT", "600")),
        telegram_otp_host=os.getenv("TELEGRAM_OTP_HOST", "0.0.0.0").strip(),
        telegram_otp_port=int(os.getenv("TELEGRAM_OTP_PORT", "8080")),
        schedules_path=os.getenv("TELEGRAM_SCHEDULES_PATH", "schedules.json").strip(),
    )
