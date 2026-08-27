"""Jittered auto-submit scheduler for scheduled attendance days.

A background loop replans each local day, computing a per-user fire time
(configured time ±10 min random jitter). When ``now`` passes a user's fire
time the scheduler spawns an attendance run via :func:`._begin_run`.

Past-due schedules are skipped (no backfill) to avoid double-submitting a day
the user already handled manually. If a fire hits a busy chat or a full
concurrency semaphore, one skip message is sent and the run is marked fired
(no retry).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from .schedules import load_schedules

if TYPE_CHECKING:
    from .telegram_bot import BotState

log = logging.getLogger("mdcattendance.scheduler")

_JITTER_MIN = -10
_JITTER_MAX = 10
_TICK_SECONDS = 30


class Scheduler:
    """Background task that fires scheduled attendance runs."""

    def __init__(self, state: BotState, schedules_path: str) -> None:
        self._state = state
        self._schedules_path = schedules_path
        self._task: asyncio.Task[None] | None = None
        self._planned_date: date | None = None
        self._pending: dict[int, tuple[datetime, str]] = {}
        self._fired: set[int] = set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def replan(self) -> None:
        """Force a re-plan on the next tick (call after a schedule edit)."""
        self._planned_date = None

    async def _loop(self) -> None:
        try:
            while True:
                now = datetime.now()
                if now.date() != self._planned_date:
                    await self._plan_today()
                    now = datetime.now()
                for uid in list(self._pending):
                    fire_at, status = self._pending[uid]
                    if now >= fire_at and uid not in self._fired:
                        self._pending.pop(uid, None)
                        self._fired.add(uid)
                        asyncio.create_task(self._fire(uid, status))
                await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _plan_today(self) -> None:
        today = date.today()
        self._planned_date = today
        self._pending = {}
        self._fired = set()
        now = datetime.now()
        today_iso = today.isoformat()
        schedules = load_schedules(self._schedules_path)
        for uid, sched in schedules.items():
            status = sched.days.get(today_iso)
            if status in (None, "none"):
                continue
            t = sched.time or "09:00"
            try:
                hh, mm = t.split(":")
                fire_at = datetime.combine(today, time(int(hh), int(mm))) + timedelta(
                    minutes=random.randint(_JITTER_MIN, _JITTER_MAX)
                )
            except (ValueError, TypeError):
                log.warning("Bad time %r for user %s — skipping today", t, uid)
                continue
            if fire_at < now:
                continue
            self._pending[uid] = (fire_at, status)

    async def _fire(self, uid: int, status: str) -> None:
        from .telegram_bot import _begin_run

        state = self._state
        chat_id = uid
        reason = _begin_run(state, state.app, chat_id, uid, dry_run=False, status=status)
        if reason is not None:
            await state.app.bot.send_message(
                chat_id, f"⏰ Scheduled {status} run skipped: {reason}"
            )
            return
        await state.app.bot.send_message(
            chat_id, f"📅 Auto-submitting your {status} attendance now…"
        )
