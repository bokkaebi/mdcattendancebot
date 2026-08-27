"""Telegram bot front-end for the Singpass attendance flow.

Exposes :func:`bot.run_flow` as a guided chat: ``/attend`` -> inline status
buttons -> (for MC) clinic + timing prompts -> OTP prompt at 2FA -> submit.
Each run is one Chromium instance; concurrency is bounded by
``MAX_CONCURRENT_RUNS``. Runs are headless by default; ``--headed`` shows
the browser windows. Per-user Singpass credentials live in ``users.json``
(gitignored, tightened to 0600 on load).

``/start`` onboards new users by collecting Singpass ID + password in-chat
(the password message is deleted after capture). ``/schedule`` opens a 2-week
calendar; ``/time`` sets the daily auto-submit time. A background scheduler
fires scheduled runs at the configured time ±10 min (random jitter).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta

import uvicorn
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .attendance import NORMAL_ANSWERS, WFH_ANSWERS, mc_answers
from .bot import run_flow
from .config import Config, load_config
from .otp import OtpProvider
from .scheduler import Scheduler
from .schedules import Schedule, load_schedules, save_schedule
from .server import (
    OtpDeliveryStatus,
    create_telegram_otp_app,
    make_server,
    serve_quietly,
    stop_server,
)
from .users import User, load_users, save_user

log = logging.getLogger("mdcattendance.telegram")


@dataclass
class BotState:
    base_cfg: Config
    users_path: str
    schedules_path: str
    interactions: dict[int, Interaction]
    active_chats: set[int]
    semaphore: asyncio.Semaphore
    app: Application
    otp_server: uvicorn.Server | None = None
    otp_server_task: asyncio.Task[None] | None = None
    scheduler: Scheduler | None = None


class Interaction:
    """One outstanding prompt per chat, resolved serially via a single future."""

    def __init__(self, chat_id: int, state: BotState, app: Application) -> None:
        self.chat_id = chat_id
        self._state = state
        self._app = app
        self._future: asyncio.Future[str] | None = None
        self._expecting: str | None = None  # "choice" | "otp" | "clinic" | "timing" | ...
        self.task: asyncio.Task[None] | None = None
        self._last_message_id: int | None = None
        self._cal_msg_id: int | None = None

    async def send(self, text: str) -> None:
        await self._app.bot.send_message(self.chat_id, text)

    async def ask_choice(self, prompt: str, options: list[tuple[str, str]]) -> str:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data)] for label, data in options]
        )
        self._expecting = "choice"
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._future = fut
        await self._app.bot.send_message(self.chat_id, prompt, reply_markup=keyboard)
        return await asyncio.wait_for(fut, timeout=self._state.base_cfg.prompt_timeout)

    async def ask_text(self, prompt: str, expecting: str, timeout: float) -> str:
        self._expecting = expecting
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._future = fut
        await self.send(prompt)
        return await asyncio.wait_for(fut, timeout=timeout)

    def deliver_text(self, text: str, *, message_id: int | None = None) -> bool:
        self._last_message_id = message_id
        if (
            self._expecting is not None
            and self._expecting not in {"choice", "schedule"}
            and self._future
            and not self._future.done()
        ):
            self._future.set_result(text)
            return True
        return False

    def deliver_otp(self, text: str) -> bool:
        if self._expecting == "otp" and self._future and not self._future.done():
            self._future.set_result(text)
            return True
        return False

    def deliver_choice(self, data: str) -> bool:
        if self._expecting == "choice" and self._future and not self._future.done():
            self._future.set_result(data)
            return True
        return False

    async def send_calendar(self, text: str, keyboard: InlineKeyboardMarkup) -> None:
        msg = await self._app.bot.send_message(self.chat_id, text, reply_markup=keyboard)
        self._cal_msg_id = msg.message_id

    async def wait_for_done(self) -> None:
        self._expecting = "schedule"
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._future = fut
        await asyncio.wait_for(fut, timeout=self._state.base_cfg.prompt_timeout)

    def deliver_done(self) -> bool:
        if self._expecting == "schedule" and self._future and not self._future.done():
            self._future.set_result(None)
            return True
        return False

    def clear_pending(self) -> None:
        if self._future and not self._future.done():
            self._future.cancel()
        self._future = None
        self._expecting = None


class TelegramOtpProvider:
    """Collects the Singpass 2FA OTP in-chat; satisfies :class:`OtpProvider`."""

    def __init__(self, interaction: Interaction) -> None:
        self._interaction = interaction

    async def wait_for_otp(self, timeout: float) -> str:
        text = await self._interaction.ask_text(
            "Waiting for the Singpass OTP. Reply here or use the HTTP OTP endpoint:",
            "otp",
            timeout,
        )
        if not re.search(r"\d", text):
            raise RuntimeError("OTP response contained no digits")
        return text


async def _deliver_http_otp(
    state: BotState, chat_id: int, otp: str
) -> OtpDeliveryStatus:
    interaction = state.interactions.get(chat_id)
    if interaction is None:
        return OtpDeliveryStatus.NO_ACTIVE_RUN
    if interaction.deliver_otp(otp):
        return OtpDeliveryStatus.ACCEPTED
    return OtpDeliveryStatus.NOT_WAITING


async def _drive(
    interaction: Interaction,
    user: User,
    *,
    dry_run: bool = False,
    status: str | None = None,
) -> None:
    state = interaction._state
    try:
        async with state.semaphore:
            if dry_run:
                answers = dict(NORMAL_ANSWERS)
            elif status is not None:
                if status == "wfh":
                    answers = dict(WFH_ANSWERS)
                elif status == "mc":
                    clinic = await interaction.ask_text(
                        "Clinic / Hospital / Medical Centre Name:",
                        "clinic",
                        state.base_cfg.prompt_timeout,
                    )
                    timing = await interaction.ask_text(
                        "Appointment Timing (24hr, e.g. 0930hrs):",
                        "timing",
                        state.base_cfg.prompt_timeout,
                    )
                    answers = mc_answers(clinic, timing)
                else:  # "normal"
                    answers = dict(NORMAL_ANSWERS)
            else:
                choice = await interaction.ask_choice(
                    "Select today's status:",
                    [
                        ("Normal", "normal"),
                        ("Work-from-Home", "wfh"),
                        ("Medical Certificate (MC)", "mc"),
                    ],
                )
                if choice == "normal":
                    answers = dict(NORMAL_ANSWERS)
                elif choice == "wfh":
                    answers = dict(WFH_ANSWERS)
                else:
                    clinic = await interaction.ask_text(
                        "Clinic / Hospital / Medical Centre Name:",
                        "clinic",
                        state.base_cfg.prompt_timeout,
                    )
                    timing = await interaction.ask_text(
                        "Appointment Timing (24hr, e.g. 0930hrs):",
                        "timing",
                        state.base_cfg.prompt_timeout,
                    )
                    answers = mc_answers(clinic, timing)
            answers["Department"] = user.department
            cfg = replace(
                state.base_cfg,
                singpass_id=user.singpass_id,
                singpass_password=user.singpass_password,
                dry_run=dry_run,
                discover=False,
                preflight=False,
            )
            otp: OtpProvider = TelegramOtpProvider(interaction)
            if dry_run:
                await interaction.send(
                    "Dry run: filling Normal attendance without submitting. "
                    "The browser will remain open for 30 seconds after the form is filled."
                )
            await interaction.send(
                "Logging in to Singpass — you'll be asked for your OTP when 2FA is reached."
            )
            await run_flow(cfg, otp, answers)
            if dry_run:
                await interaction.send(
                    "Dry run complete — Normal attendance was filled but not submitted. "
                    "The browser stayed open for 30 seconds and is now closed."
                )
            else:
                await interaction.send("Attendance submitted successfully.")
    except asyncio.CancelledError:
        await interaction.send("Run cancelled.")
        raise
    except TimeoutError:
        await interaction.send("Run timed out waiting for your response and was cancelled.")
    except Exception as exc:  # noqa: BLE001
        log.exception("attendance run failed")
        await interaction.send(f"Run failed: {exc}")
    finally:
        state.active_chats.discard(interaction.chat_id)
        state.interactions.pop(interaction.chat_id, None)


def _begin_session(
    state: BotState, app: Application, chat_id: int
) -> str | None:
    """Create+register an :class:`Interaction` for the chat.

    Returns ``None`` on success (the interaction is in
    ``state.interactions[chat_id]``; the caller assigns ``interaction.task``)
    or a human-readable busy reason.
    """
    if chat_id in state.active_chats:
        return "A session is already in progress. Send /cancel to abort it."
    interaction = Interaction(chat_id, state, app)
    state.interactions[chat_id] = interaction
    state.active_chats.add(chat_id)
    return None


def _begin_run(
    state: BotState,
    app: Application,
    chat_id: int,
    uid: int,
    *,
    dry_run: bool,
    status: str | None = None,
) -> str | None:
    """Start an attendance run for the chat.

    Returns ``None`` on success (the ``_drive`` task is already spawned) or a
    human-readable reason.
    """
    users = load_users(state.users_path)
    if uid not in users:
        return (
            f"Not registered — your Telegram user ID is {uid}. "
            "Ask the operator to add it as a key in users.json."
        )
    if chat_id in state.active_chats:
        return "A run is already in progress. Send /cancel to abort it."
    if state.semaphore._value == 0:
        return "Server busy (max concurrent runs reached); try again shortly."
    interaction = Interaction(chat_id, state, app)
    state.interactions[chat_id] = interaction
    state.active_chats.add(chat_id)
    interaction.task = asyncio.create_task(
        _drive(interaction, users[uid], dry_run=dry_run, status=status)
    )
    return None


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["mdc"]
    chat_id = update.effective_chat.id
    uid = update.effective_user.id if update.effective_user else 0
    users = load_users(state.users_path)
    if uid in users:
        await _show_menu(context.bot, chat_id, uid)
        return
    if chat_id in state.active_chats:
        await context.bot.send_message(
            chat_id,
            "An onboarding session is already in progress. Send /cancel to abort it.",
        )
        return
    interaction = Interaction(chat_id, state, context.application)
    state.interactions[chat_id] = interaction
    state.active_chats.add(chat_id)
    interaction.task = asyncio.create_task(_onboard(interaction, uid))
    await context.bot.send_message(
        chat_id,
        "👋 Welcome! Let's register you. I'll ask for your Singpass ID and password next.",
    )


async def _onboard(interaction: Interaction, uid: int) -> None:
    state = interaction._state
    timeout = state.base_cfg.prompt_timeout
    try:
        sid = await interaction.ask_text(
            "Enter your Singpass ID:", "onboard_id", timeout
        )
        await interaction.send(
            "Now enter your Singpass password. I'll delete this message right "
            "after I read it, so it won't stay in the chat."
        )
        pw = await interaction.ask_text("Password:", "onboard_pw", timeout)
        if interaction._last_message_id is not None:
            try:
                await interaction._app.bot.delete_message(
                    interaction.chat_id, interaction._last_message_id
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "Could not delete password message %s (best-effort).",
                    interaction._last_message_id,
                )
        sid = sid.strip()
        pw = pw.strip()
        if not sid or not pw:
            await interaction.send(
                "Singpass ID and password must not be empty. Send /start to try again."
            )
            return
        save_user(state.users_path, uid, sid, pw)
        await interaction.send(
            f"✅ Registered. Your Telegram user ID is {uid}.\n"
            f"This chat's ID: {interaction.chat_id}.\n"
            "You can now schedule attendance."
        )
        await _show_menu(interaction._app.bot, interaction.chat_id, uid)
    except asyncio.CancelledError:
        await interaction.send("Onboarding cancelled.")
        raise
    except TimeoutError:
        await interaction.send("Onboarding timed out waiting for your response.")
    except Exception as exc:  # noqa: BLE001
        log.exception("onboarding failed")
        await interaction.send(f"Onboarding failed: {exc}")
    finally:
        state.active_chats.discard(interaction.chat_id)
        state.interactions.pop(interaction.chat_id, None)


async def _start_run(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, dry_run: bool
) -> None:
    state: BotState = context.application.bot_data["mdc"]
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    reason = _begin_run(state, context.application, chat_id, uid, dry_run=dry_run)
    if reason:
        await context.bot.send_message(chat_id, reason)
        return
    if dry_run:
        start_text = "Starting dry run — filling Normal attendance without submitting."
    else:
        start_text = "Starting — select your status below."
    await context.bot.send_message(chat_id, start_text)


async def _attend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_run(update, context, dry_run=False)


async def _dry_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_run(update, context, dry_run=True)


async def _time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["mdc"]
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    users = load_users(state.users_path)
    if uid not in users:
        await context.bot.send_message(
            chat_id,
            f"Not registered — your Telegram user ID is {uid}. "
            "Ask the operator to add it as a key in users.json.",
        )
        return
    reason = _begin_session(state, context.application, chat_id)
    if reason:
        await context.bot.send_message(chat_id, reason)
        return
    interaction = state.interactions[chat_id]
    interaction.task = asyncio.create_task(_set_time(interaction, uid))


async def _set_time(interaction: Interaction, uid: int) -> None:
    state = interaction._state
    try:
        raw = await interaction.ask_text(
            "Enter your submission time (24h HH:MM, e.g. 09:00):",
            "time",
            state.base_cfg.prompt_timeout,
        )
        t = raw.strip()
        m = re.fullmatch(r"\d{2}:\d{2}", t)
        if not m:
            await interaction.send(
                "Invalid time. Use HH:MM 24-hour, e.g. 09:00. Send /time to try again."
            )
            return
        hh_s, mm_s = t.split(":")
        if not (0 <= int(hh_s) <= 23 and 0 <= int(mm_s) <= 59):
            await interaction.send(
                "Invalid time. Use HH:MM 24-hour, e.g. 09:00. Send /time to try again."
            )
            return
        save_schedule(state.schedules_path, uid, time=t)
        await interaction.send(
            f"⏰ Submission time set to {t}. "
            f"Scheduled runs will fire within ±10 min of {t}."
        )
    except asyncio.CancelledError:
        await interaction.send("Time setup cancelled.")
        raise
    except TimeoutError:
        await interaction.send("Time setup timed out waiting for your response.")
    except Exception as exc:  # noqa: BLE001
        log.exception("time setup failed")
        await interaction.send(f"Time setup failed: {exc}")
    finally:
        state.active_chats.discard(interaction.chat_id)
        state.interactions.pop(interaction.chat_id, None)


async def _show_menu(bot: Bot, chat_id: int, user_id: int) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 Schedule", callback_data="menu:schedule"),
                InlineKeyboardButton("⏰ Set Time", callback_data="menu:time"),
            ],
            [
                InlineKeyboardButton("✅ Attend Now", callback_data="menu:attend"),
                InlineKeyboardButton("🧪 Dry Run", callback_data="menu:dryrun"),
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data="menu:cancel"),
                InlineKeyboardButton("🆔 My ID", callback_data="menu:id"),
            ],
        ]
    )
    await bot.send_message(
        chat_id,
        f"Hi! Your Telegram user ID is {user_id}.\n"
        f"This chat's ID: {chat_id}.\n"
        "Tap a button below or use a command.",
        reply_markup=keyboard,
    )


def _calendar_keyboard(days: dict[str, str]) -> InlineKeyboardMarkup:
    """Build a 14-day grid (2 weeks, 7 per row) plus a full-width Done button."""
    today = date.today()
    rows: list[list[InlineKeyboardButton]] = []
    week: list[InlineKeyboardButton] = []
    for i in range(14):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        label = f"{d.strftime('%a')} {d.day}"
        status = days.get(ds)
        if status == "normal":
            label += " ✅"
        elif status == "wfh":
            label += " 🏠"
        elif status == "mc":
            label += " 🏥"
        elif status == "none":
            label += " ⌀"
        week.append(InlineKeyboardButton(label, callback_data=f"sched:{ds}"))
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        rows.append(week)
    rows.append([InlineKeyboardButton("✅ Done", callback_data="sched:done")])
    return InlineKeyboardMarkup(rows)


def _day_status_keyboard(date_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Normal", callback_data=f"set:{date_str}:normal"),
                InlineKeyboardButton("WFH", callback_data=f"set:{date_str}:wfh"),
                InlineKeyboardButton("MC", callback_data=f"set:{date_str}:mc"),
                InlineKeyboardButton("Skip", callback_data=f"set:{date_str}:none"),
            ],
            [InlineKeyboardButton("← Back", callback_data=f"set:{date_str}:back")],
        ]
    )


async def _schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["mdc"]
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    users = load_users(state.users_path)
    if uid not in users:
        await context.bot.send_message(
            chat_id,
            f"Not registered — your Telegram user ID is {uid}. "
            "Ask the operator to add it as a key in users.json.",
        )
        return
    reason = _begin_session(state, context.application, chat_id)
    if reason:
        await context.bot.send_message(chat_id, reason)
        return
    interaction = state.interactions[chat_id]
    interaction.task = asyncio.create_task(_schedule(interaction, uid))


async def _schedule(interaction: Interaction, uid: int) -> None:
    state = interaction._state
    try:
        sched = load_schedules(state.schedules_path).get(uid, Schedule("09:00", {}))
        keyboard = _calendar_keyboard(sched.days)
        await interaction.send_calendar("📅 Pick a day to set its status:", keyboard)
        await interaction.wait_for_done()
    except asyncio.CancelledError:
        await interaction.send("Schedule session cancelled.")
        raise
    except TimeoutError:
        await interaction.send(
            "Schedule session timed out — your changes were already saved as you made them."
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("schedule session failed")
        await interaction.send(f"Schedule failed: {exc}")
    finally:
        state.active_chats.discard(interaction.chat_id)
        state.interactions.pop(interaction.chat_id, None)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["mdc"]
    chat_id = update.effective_chat.id
    interaction = state.interactions.get(chat_id)
    if interaction and interaction.deliver_text(
        update.message.text, message_id=update.message.message_id
    ):
        return
    await context.bot.send_message(
        chat_id,
        "Send /start for the menu, /attend or /dry-run to start, or reply to the current prompt.",
    )


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    state: BotState = context.application.bot_data["mdc"]
    chat_id = q.message.chat_id
    interaction = state.interactions.get(chat_id)

    if q.data == "sched:done":
        if interaction and interaction.deliver_done():
            with contextlib.suppress(BadRequest):
                await q.edit_message_text("📅 Schedule session closed.")
        else:
            await context.bot.send_message(
                chat_id, "That schedule session is no longer active."
            )
    elif q.data.startswith("sched:"):
        if interaction and interaction._expecting == "schedule":
            day = q.data[6:]
            with contextlib.suppress(BadRequest):
                await q.edit_message_text(
                    f"📅 {day} — choose status:",
                    reply_markup=_day_status_keyboard(day),
                )
        else:
            await context.bot.send_message(
                chat_id, "That schedule session is no longer active."
            )
    elif q.data.startswith("set:"):
        if interaction and interaction._expecting == "schedule":
            _, day, status = q.data.split(":", 2)
            uid = update.effective_user.id if update.effective_user else 0
            if status == "back":
                sched = load_schedules(state.schedules_path).get(
                    uid, Schedule("09:00", {})
                )
                with contextlib.suppress(BadRequest):
                    await q.edit_message_text(
                        "📅 Pick a day to set its status:",
                        reply_markup=_calendar_keyboard(sched.days),
                    )
            else:
                save_schedule(state.schedules_path, uid, days={day: status})
                if state.scheduler:
                    state.scheduler.replan()
                sched = load_schedules(state.schedules_path).get(
                    uid, Schedule("09:00", {})
                )
                with contextlib.suppress(BadRequest):
                    await q.edit_message_text(
                        "📅 Pick a day to set its status:",
                        reply_markup=_calendar_keyboard(sched.days),
                    )
        else:
            await context.bot.send_message(
                chat_id, "That schedule session is no longer active."
            )
    elif q.data.startswith("menu:"):
        action = q.data[5:]
        if action == "cancel":
            await _cancel(update, context)
        elif action == "id":
            uid = update.effective_user.id if update.effective_user else "?"
            await context.bot.send_message(
                chat_id, f"Your Telegram user ID: {uid}\nThis chat's ID: {chat_id}"
            )
        elif chat_id in state.active_chats:
            await context.bot.send_message(
                chat_id, "A session is already in progress. /cancel to abort."
            )
        elif action == "attend":
            await _start_run(update, context, dry_run=False)
        elif action == "dryrun":
            await _start_run(update, context, dry_run=True)
        elif action == "schedule":
            await _schedule_cmd(update, context)
        elif action == "time":
            await _time(update, context)
    else:
        if interaction and interaction.deliver_choice(q.data):
            with contextlib.suppress(BadRequest):
                await q.edit_message_text(f"Selected: {q.data}")
        else:
            await context.bot.send_message(
                chat_id, "That button is no longer active."
            )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["mdc"]
    chat_id = update.effective_chat.id
    interaction = state.interactions.get(chat_id)
    if not interaction:
        await context.bot.send_message(chat_id, "Nothing to cancel.")
        return
    interaction.clear_pending()
    if interaction.task:
        interaction.task.cancel()
    await context.bot.send_message(chat_id, "Cancelling the current run...")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error(
        "Unhandled exception while handling update %s", update, exc_info=context.error
    )


async def _post_init(application: Application) -> None:
    state: BotState = application.bot_data["mdc"]
    cfg = state.base_cfg

    otp_app = create_telegram_otp_app(
        lambda chat_id, otp: _deliver_http_otp(state, chat_id, otp)
    )
    server = make_server(otp_app, cfg.telegram_otp_host, cfg.telegram_otp_port)
    task = asyncio.create_task(serve_quietly(server))
    state.otp_server = server
    state.otp_server_task = task

    for _ in range(20):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if not server.started:
        try:
            await stop_server(server, task)
        finally:
            state.otp_server = None
            state.otp_server_task = None
        raise RuntimeError(
            f"Telegram OTP server could not bind "
            f"{cfg.telegram_otp_host}:{cfg.telegram_otp_port}"
        )

    log.info(
        "Telegram OTP endpoint ready on %s:%s: POST /telegram/otp",
        cfg.telegram_otp_host,
        cfg.telegram_otp_port,
    )

    state.scheduler = Scheduler(state, cfg.schedules_path)
    await state.scheduler.start()
    log.info("Scheduler started")

    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Register / show menu"),
                BotCommand("schedule", "Schedule the next 2 weeks"),
                BotCommand("time", "Set submission time"),
                BotCommand("attend", "Submit attendance now"),
                BotCommand("dry_run", "Fill Normal without submitting"),
                BotCommand("cancel", "Abort the current run"),
            ]
        )
    except Exception:  # noqa: BLE001
        log.warning("Could not set bot command menu (non-fatal).")


async def _post_shutdown(application: Application) -> None:
    state: BotState = application.bot_data["mdc"]
    if state.scheduler is not None:
        await state.scheduler.stop()
        state.scheduler = None
    if state.otp_server is None or state.otp_server_task is None:
        return
    try:
        await stop_server(state.otp_server, state.otp_server_task)
    finally:
        state.otp_server = None
        state.otp_server_task = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the mdcattendance Telegram bot.")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium windows for attendance runs.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parse_args()
    cfg = load_config()
    if args.headed:
        cfg = replace(cfg, headless=False)
    if not cfg.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN must be set (see .env.example).")
    app = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["mdc"] = BotState(
        base_cfg=cfg,
        users_path=cfg.users_path,
        schedules_path=cfg.schedules_path,
        interactions={},
        active_chats=set(),
        semaphore=asyncio.Semaphore(cfg.max_concurrent_runs),
        app=app,
    )
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _start))
    app.add_handler(CommandHandler("attend", _attend))
    app.add_handler(CommandHandler("dry_run", _dry_run))
    app.add_handler(CommandHandler("schedule", _schedule_cmd))
    app.add_handler(CommandHandler("time", _time))
    app.add_handler(
        MessageHandler(filters.Regex(r"^/dry-run(?:@[A-Za-z0-9_]+)?\s*$"), _dry_run)
    )
    app.add_handler(CommandHandler("cancel", _cancel))
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_error_handler(_on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
