"""Single-flight execution with durable, conservative submission outcomes."""

from __future__ import annotations

import asyncio
import fcntl
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .attendance import Answers, validate_answers
from .bot import run_flow
from .config import Config
from .otp import OtpProvider
from .storage import SINGAPORE, StateStore
from .storage import DuplicateRun as DuplicateRun


class BusyRun(RuntimeError):
    """Another process or task owns the one browser slot."""


@dataclass(frozen=True)
class RunResult:
    status: str
    attempt_id: int
    detail: str = ""


class AttendanceRunner:
    def __init__(self, cfg: Config, store: StateStore) -> None:
        if Path(cfg.state_dir).expanduser().resolve() != Path(store.state_dir):
            raise ValueError("Runner and store must share one state directory")
        self.cfg = cfg
        self.store = store
        self._lock_path = str(Path(store.state_dir) / "run.lock")
        self._busy = False
        self.last_result: RunResult | None = None
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    def _acquire_file_lock(self) -> int:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BusyRun("Another attendance run is active") from None
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_file_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def busy(self) -> bool:
        if self._busy:
            return True
        try:
            fd = self._acquire_file_lock()
        except BusyRun:
            return True
        self._release_file_lock(fd)
        return False

    def recover(self) -> bool:
        """Startup recovery; the scheduler also holds its lifecycle lock."""
        if self._busy or self._closed:
            return False
        try:
            fd = self._acquire_file_lock()
        except BusyRun:
            return False
        try:
            self.store.recover()
        finally:
            self._release_file_lock(fd)
        return True

    async def run(
        self,
        cfg: Config,
        otp: OtpProvider,
        answers: Answers,
        *,
        attendance_date: date | None = None,
        override: bool = False,
    ) -> RunResult:
        if self._closed:
            raise RuntimeError("Attendance runner is shut down")
        if self._busy:
            raise BusyRun("Another attendance run is active")
        # No await before this guard and flock: callers cannot launch two browsers.
        self._busy = True
        fd: int | None = None
        attempt_id: int | None = None
        self.last_result = None
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            fd = self._acquire_file_lock()
            self.store.recover_attempts()
            mode = (
                "preflight"
                if cfg.preflight
                else "discover"
                if cfg.discover
                else "dry_run"
                if cfg.dry_run
                else "submit"
            )
            day = attendance_date or datetime.now(SINGAPORE).date()
            if not isinstance(day, date) or isinstance(day, datetime):
                raise ValueError("Attendance date must be a date")
            if mode == "submit" and day != datetime.now(SINGAPORE).date():
                raise ValueError("Submission is permitted only for today's Singapore date")
            attempt_id = self.store.prepare(
                cfg.singpass_id,
                cfg.form_url,
                day,
                mode,
                override=override,
            )
            self.store.transition(attempt_id, "running")
            if mode in {"submit", "dry_run"}:
                validate_answers(answers)

            async def before_submit() -> None:
                if day != datetime.now(SINGAPORE).date():
                    raise ValueError("Attendance date changed before submission")
                # This transaction commits before the browser is permitted to click.
                self.store.transition(attempt_id, "submitting")

            async with asyncio.timeout(cfg.run_timeout):
                status = await run_flow(
                    cfg,
                    otp,
                    answers,
                    before_submit=before_submit if mode == "submit" else None,
                )
            expected = "discovered" if mode == "discover" else "confirmed" if mode == "submit" else mode
            if status != expected:
                raise RuntimeError("Browser did not produce the expected outcome")
            self.store.transition(attempt_id, status)
            self.last_result = RunResult(status, attempt_id)
            return self.last_result
        except asyncio.CancelledError:
            if attempt_id is not None:
                self._failed_result(attempt_id, "run cancelled")
            raise
        except TimeoutError:
            if attempt_id is None:
                raise
            return self._failed_result(attempt_id, "run timed out")
        except Exception:
            if attempt_id is None:
                raise
            return self._failed_result(attempt_id, "execution failed")
        except BaseException:
            if attempt_id is not None:
                self._failed_result(attempt_id, "execution failed")
            raise
        finally:
            if fd is not None:
                self._release_file_lock(fd)
            self._busy = False
            if task is not None:
                self._tasks.discard(task)

    def _failed_result(self, attempt_id: int, detail: str) -> RunResult:
        current = self.store.get_attempt(attempt_id)["status"]
        status = "unknown" if current == "submitting" else "failed"
        self.store.transition(attempt_id, status, detail)
        self.last_result = RunResult(status, attempt_id, detail)
        return self.last_result

    async def shutdown(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
