"""Deterministic Singapore schedules with durable, window-bounded dispatches."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path

from .attendance import Answers
from .config import Config
from .otp import OtpProvider
from .runner import AttendanceRunner, BusyRun, DuplicateRun
from .schedules import load_schedules, validate_time
from .storage import SINGAPORE, StateStore

log = logging.getLogger("mdcattendance.scheduler")
_TICK_SECONDS = 30
_BUSY_RETRY_SECONDS = 1
_NOTIFICATION_TIMEOUT = 10
Prepare = Callable[[int, str, datetime], Awaitable[tuple[Config, OtpProvider, Answers]]]
Notify = Callable[[int, str], Awaitable[None]]


class Scheduler:
    def __init__(
        self,
        store: StateStore,
        runner: AttendanceRunner,
        base_cfg: Config,
        prepare: Prepare,
        notify: Notify,
    ) -> None:
        validate_time(base_cfg.attendance_deadline)
        self.store = store
        self.runner = runner
        self.base_cfg = base_cfg
        self.prepare = prepare
        self.notify = notify
        self._task: asyncio.Task[None] | None = None
        self._dispatch_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._tick_lock = asyncio.Lock()
        self._lock_fd: int | None = None
        self._recovered = False
        self._stopping = False

    def _acquire_lifecycle_lock(self) -> None:
        if self._lock_fd is not None:
            return
        path = str(Path(self.store.state_dir) / "scheduler.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BusyRun("Another scheduler owns the state directory") from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._acquire_lifecycle_lock()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        loop = self._task
        self._task = None
        dispatches = dict(self._dispatch_tasks)
        tasks = [*dispatches.values()]
        if loop is not None:
            tasks.append(loop)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first instruction cannot run its finally block.
        for uid, day in dispatches:
            journal = self.store.get_schedule_dispatch(uid, day)
            if journal is not None and journal["status"] == "collecting":
                await self._finish(uid, day, "missed", "scheduled run cancelled")
        self._dispatch_tasks.clear()
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None
        self._recovered = False

    def replan(self) -> None:
        """Settings are queried each tick; completed journal rows are never reset."""
        return None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never include exception/update dumps, which may contain personal input.
                log.error("Scheduled attendance tick failed; no automatic submission performed")
            await asyncio.sleep(_TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        if self._stopping:
            return
        instant = now or datetime.now(SINGAPORE)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Scheduler time must be timezone-aware")
        instant = instant.astimezone(SINGAPORE)
        async with self._tick_lock:
            self._acquire_lifecycle_lock()
            if not self._recovered:
                interrupted = self.store.collecting_dispatches()
                # Both locks are held during recovery. A live CLI run is never recovered.
                if self.runner.recover():
                    self._recovered = True
                    for row in interrupted:
                        await self._notify(
                            row["uid"],
                            f"Scheduled attendance for {row['day']} missed after restart; "
                            "no automatic retry.",
                        )
            today = instant.date()
            deadline_time = time.fromisoformat(self.base_cfg.attendance_deadline)
            deadline = datetime.combine(today, deadline_time, SINGAPORE)
            schedules = load_schedules(self.store.path)
            for uid, schedule in schedules.items():
                if self._stopping:
                    return
                for day, status in schedule.days.items():
                    if status == "none" or day > today.isoformat():
                        continue
                    if self.store.get_schedule_dispatch(uid, day) is not None:
                        continue
                    if day < today.isoformat():
                        if self.store.claim_schedule(uid, day):
                            await self._finish(uid, day, "missed", "scheduled date missed")
                        continue
                    fire = datetime.combine(today, time.fromisoformat(schedule.time), SINGAPORE)
                    if instant >= deadline:
                        if self.store.claim_schedule(uid, day):
                            await self._finish(uid, day, "missed", "attendance window closed")
                    elif fire >= deadline:
                        if self.store.claim_schedule(uid, day):
                            await self._finish(
                                uid, day, "skipped", "scheduled time must be before deadline"
                            )
                    elif (
                        self._recovered
                        and instant >= fire
                        and not self.runner.busy
                        and self.store.claim_schedule(uid, day)
                    ):
                        key = (uid, day)
                        self._dispatch_tasks[key] = asyncio.create_task(
                            self._dispatch(uid, day, status, deadline)
                        )

    @staticmethod
    def _remaining(deadline: datetime) -> float:
        return (deadline - datetime.now(SINGAPORE)).total_seconds()

    async def _dispatch(self, uid: int, day: str, status: str, deadline: datetime) -> None:
        running = False
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                await self._finish(uid, day, "missed", "attendance window closed")
                return
            async with asyncio.timeout(remaining):
                cfg, otp, answers = await self.prepare(uid, status, deadline)
            while True:
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    await self._finish(uid, day, "missed", "browser busy until deadline")
                    return
                if self.runner.busy:
                    await asyncio.sleep(min(_BUSY_RETRY_SECONDS, remaining))
                    continue
                # The browser itself is bounded by the remaining attendance window.
                bounded_cfg = replace(cfg, run_timeout=min(cfg.run_timeout, remaining))
                try:
                    running = True
                    result = await self.runner.run(
                        bounded_cfg,
                        otp,
                        answers,
                        attendance_date=deadline.date(),
                    )
                except BusyRun:
                    running = False
                    await asyncio.sleep(min(_BUSY_RETRY_SECONDS, remaining))
                    continue
                running = False
                await self._finish(uid, day, result.status, result.detail)
                return
        except DuplicateRun:
            running = False
            await self._finish(uid, day, "skipped", "duplicate submission blocked")
        except TimeoutError:
            await self._finish(uid, day, "missed", "attendance window closed")
        except asyncio.CancelledError:
            result = self.runner.last_result if running else None
            outcome = result.status if result is not None else "missed"
            await self._finish(uid, day, outcome, "scheduled run cancelled")
            raise
        except Exception:
            await self._finish(uid, day, "failed", "scheduled preparation failed")
        finally:
            self._dispatch_tasks.pop((uid, day), None)

    async def _finish(self, uid: int, day: str, status: str, detail: str = "") -> None:
        journal = self.store.get_schedule_dispatch(uid, day)
        if journal is None or journal["status"] != "collecting":
            return
        self.store.finish_schedule(uid, day, status, detail)
        text = f"Scheduled attendance for {day}: {status}."
        if detail:
            text += f" {detail}."
        if status == "unknown":
            text += " Submission may have reached FormSG; do not retry automatically."
        await self._notify(uid, text)

    async def _notify(self, uid: int, text: str) -> None:
        try:
            async with asyncio.timeout(_NOTIFICATION_TIMEOUT):
                await self.notify(uid, text)
        except Exception:
            log.warning("Scheduled attendance notification could not be delivered")
