"""Validated runtime configuration and restricted local secret files."""

from __future__ import annotations

import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_FORM_URL = "https://form.gov.sg/6818179de1b7fe5377eb5c20"


def restrict_secret_file(path: str) -> None:
    """Restrict an existing regular secret file before reading it; fail closed."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Secret file must be a regular file, not a symlink")
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.chmod(path, 0o600)
    if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
        raise PermissionError("Secret file must have mode 0600")


@dataclass(frozen=True)
class Config:
    singpass_id: str = ""
    singpass_password: str = ""
    form_url: str = DEFAULT_FORM_URL
    otp_timeout: float = 300
    chromium_executable_path: str | None = None
    headless: bool = True
    slow_mo: int = 0
    discover: bool = False
    preflight: bool = False
    dry_run: bool = False
    navigation_timeout_ms: int = 60000
    form_settle_ms: int = 3000
    telegram_bot_token: str = ""
    users_path: str = "users.json"
    prompt_timeout: float = 600
    state_dir: str = "state"
    run_timeout: float = 600
    attendance_deadline: str = "09:00"
    retention_days: int = 90
    diagnostic_ttl_hours: float = 24
    otp_http_enabled: bool = False
    otp_http_port: int = 8765
    otp_http_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        for name in (
            "otp_timeout",
            "navigation_timeout_ms",
            "form_settle_ms",
            "prompt_timeout",
            "run_timeout",
            "retention_days",
            "diagnostic_ttl_hours",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and strictly positive")
        if self.slow_mo < 0:
            raise ValueError("slow_mo must be nonnegative")
        if not isinstance(self.otp_http_port, int) or not 0 <= self.otp_http_port <= 65535:
            raise ValueError("otp_http_port must be between 0 and 65535")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.attendance_deadline):
            raise ValueError("attendance_deadline must be HH:MM")
        if not self.form_url or not self.state_dir or not self.users_path:
            raise ValueError("form_url, state_dir and users_path must not be blank")
        if sum((self.discover, self.preflight, self.dry_run)) > 1:
            raise ValueError("Select only one of discover, preflight and dry_run")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError("Boolean settings must be true or false")
    return normalized in {"1", "true", "yes", "on"}


def load_config() -> Config:
    """Load an explicit restricted env file, or a local restricted .env if present."""
    configured = os.getenv("MDCATTENDANCE_ENV_FILE")
    path = configured if configured is not None else ".env"
    if configured is not None or Path(path).exists():
        restrict_secret_file(path)
        load_dotenv(path, override=False)
    return Config(
        singpass_id=os.getenv("SINGPASS_ID", "").strip(),
        singpass_password=os.getenv("SINGPASS_PASSWORD", ""),
        form_url=os.getenv("FORM_URL", DEFAULT_FORM_URL).strip(),
        otp_timeout=float(os.getenv("OTP_TIMEOUT", "300")),
        chromium_executable_path=os.getenv("CHROMIUM_EXECUTABLE_PATH", "").strip() or None,
        headless=_bool(os.getenv("HEADLESS"), True),
        slow_mo=int(os.getenv("SLOW_MO", "0")),
        discover=_bool(os.getenv("DISCOVER")),
        preflight=_bool(os.getenv("PREFLIGHT")),
        navigation_timeout_ms=int(os.getenv("NAVIGATION_TIMEOUT_MS", "60000")),
        form_settle_ms=int(os.getenv("FORM_SETTLE_MS", "3000")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        users_path=os.getenv("TELEGRAM_USERS_PATH", "users.json").strip(),
        prompt_timeout=float(os.getenv("PROMPT_TIMEOUT", "600")),
        state_dir=os.getenv("STATE_DIR", "state").strip(),
        run_timeout=float(os.getenv("RUN_TIMEOUT", "600")),
        attendance_deadline=os.getenv("ATTENDANCE_DEADLINE", "09:00").strip(),
        retention_days=int(os.getenv("RETENTION_DAYS", "90")),
        diagnostic_ttl_hours=float(os.getenv("DIAGNOSTIC_TTL_HOURS", "24")),
        otp_http_enabled=_bool(os.getenv("OTP_HTTP_ENABLED")),
        otp_http_port=int(os.getenv("OTP_HTTP_PORT", "8765")),
        otp_http_token=os.getenv("OTP_HTTP_TOKEN", ""),
    )
