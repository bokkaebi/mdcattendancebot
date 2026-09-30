"""Private, allowlisted Telegram interface for assisted attendance submission."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .attendance import NORMAL_ANSWERS, WFH_ANSWERS, Answers, mc_answers, validate_answers
from .config import Config, load_config
from .otp import OtpProvider, validate_otp
from .runner import AttendanceRunner, BusyRun, DuplicateRun
from .scheduler import Scheduler
from .schedules import Schedule, load_schedules, save_schedule
from .server import OtpHttpServer
from .storage import StateStore
from .users import User, load_users

log = logging.getLogger("mdcattendance.telegram")
SG = ZoneInfo("Asia/Singapore")


@dataclass
class BotState:
    base_cfg: Config
    users_path: str
    interactions: dict[int, Interaction]
    active_chats: set[int]
    app: Application
    store: StateStore
    runner: AttendanceRunner
    scheduler: Scheduler | None = None
    otp_server: OtpHttpServer | None = None


class Interaction:
    """One user-owned session with one current, message-bound prompt."""

    def __init__(self, chat_id: int, state: BotState, app: Application) -> None:
        self.chat_id = chat_id
        self._state = state
        self._app = app
        self.task: asyncio.Task | None = None
        self._future: asyncio.Future[str] | None = None
        self._expecting: str | None = None
        self._pending_token: object | None = None
        self._prompt_message_id: int | None = None
        self._choices: set[str] = set()
        self._cal_msg_id: int | None = None
        self.scheduled = False

    async def send(self, text: str) -> None:
        await self._app.bot.send_message(self.chat_id, text)

    def clear_pending(self) -> None:
        if self._future is not None and not self._future.done():
            self._future.cancel()
        self._future = None
        self._expecting = None
        self._pending_token = None
        self._prompt_message_id = None
        self._choices.clear()

    async def _ask(
        self, prompt: str, expecting: str, timeout: float, markup=None, choices: set[str] | None = None
    ) -> str:
        self.clear_pending()
        token = object()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_token = token
        self._future = future
        self._expecting = expecting
        self._choices = choices or set()
        try:
            msg = await self._app.bot.send_message(self.chat_id, prompt, reply_markup=markup)
            self._prompt_message_id = msg.message_id
            return await asyncio.wait_for(future, timeout)
        finally:
            if self._pending_token is token:
                self.clear_pending()

    async def ask_text(self, prompt: str, expecting: str, timeout: float) -> str:
        return await self._ask(prompt, expecting, timeout, ForceReply(selective=True))

    async def ask_choice(self, prompt: str, options: list[tuple[str, str]]) -> str:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data)] for label, data in options]
        )
        return await self._ask(
            prompt,
            "choice",
            self._state.base_cfg.prompt_timeout,
            keyboard,
            {data for _, data in options},
        )

    def _current(self, user_id: int, chat_id: int, reply_to_message_id: int | None) -> bool:
        return (
            user_id == self.chat_id == chat_id
            and self._state.interactions.get(user_id) is self
            and self._pending_token is not None
            and self._prompt_message_id is not None
            and reply_to_message_id == self._prompt_message_id
            and self._future is not None
            and not self._future.done()
        )

    def deliver_otp(
        self, text: str, *, user_id: int, chat_id: int, reply_to_message_id: int | None
    ) -> bool:
        if self._expecting != "otp" or not self._current(user_id, chat_id, reply_to_message_id):
            return False
        future = self._future
        if future is None or future.done():
            return False
        otp = validate_otp(text)
        future.set_result(otp)
        return True

    def deliver_text(
        self, text: str, *, user_id: int, chat_id: int, reply_to_message_id: int | None
    ) -> bool:
        if self._expecting in {None, "otp", "choice", "schedule"}:
            return False
        if not self._current(user_id, chat_id, reply_to_message_id):
            return False
        future = self._future
        if future is None or future.done():
            return False
        future.set_result(text.strip())
        return True

    def deliver_choice(self, data: str, *, user_id: int, chat_id: int, message_id: int) -> bool:
        if self._expecting != "choice" or data not in self._choices:
            return False
        if not self._current(user_id, chat_id, message_id):
            return False
        future = self._future
        if future is None or future.done():
            return False
        future.set_result(data)
        return True


class TelegramOtpProvider:
    def __init__(self, interaction: Interaction) -> None:
        self._interaction = interaction

    async def wait_for_otp(self, timeout: float) -> str:
        return await self._interaction.ask_text(
            "Reply to THIS message with the current six-digit Singpass OTP, or use phone "
            "delivery if provisioned. Earlier replies are not accepted.",
            "otp",
            timeout,
        )


def _task_done(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("Telegram session task failed; sensitive details omitted")


def _release(interaction: Interaction) -> None:
    interaction.clear_pending()
    state = interaction._state
    if state.interactions.get(interaction.chat_id) is interaction:
        state.interactions.pop(interaction.chat_id)
        state.active_chats.discard(interaction.chat_id)


def _begin_session(state: BotState, uid: int) -> Interaction:
    if uid in state.interactions:
        raise BusyRun("session busy")
    interaction = Interaction(uid, state, state.app)
    state.interactions[uid] = interaction
    state.active_chats.add(uid)
    return interaction


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
    chat, sender = update.effective_chat, update.effective_user
    if chat is None or sender is None or chat.type != "private" or chat.id != sender.id:
        if update.callback_query:
            await update.callback_query.answer("Private chats only.")
        return None
    state: BotState = context.application.bot_data["mdc"]
    user = load_users(state.users_path).get(sender.id)
    if user is None:
        if update.callback_query:
            await update.callback_query.answer("Not authorised.")
        else:
            await context.bot.send_message(
                chat.id, "Not authorised. Ask the operator to provision your credentials locally."
            )
    return user


async def _collect(
    interaction: Interaction, user: User, *, dry_run: bool = False, status: str | None = None
) -> tuple[Config, OtpProvider, Answers]:
    state = interaction._state
    if status is None:
        status = await interaction.ask_choice(
            "Select attendance status:",
            [("Normal", "normal"), ("Work-from-Home", "wfh"), ("Medical Certificate (MC)", "mc")],
        )
    if status == "normal":
        answers = dict(NORMAL_ANSWERS)
    elif status == "wfh":
        answers = dict(WFH_ANSWERS)
    elif status == "mc":
        while True:
            clinic = await interaction.ask_text(
                "Clinic / Hospital / Medical Centre Name:", "clinic", state.base_cfg.prompt_timeout
            )
            timing = await interaction.ask_text(
                "Appointment Timing (24hr, e.g. 0930hrs):", "timing", state.base_cfg.prompt_timeout
            )
            try:
                answers = mc_answers(clinic, timing)
                validate_answers(answers)
                break
            except ValueError:
                await interaction.send(
                    "Enter a nonblank clinic and valid 24-hour HHMM timing (optionally followed by hrs)."
                )
    else:
        raise ValueError("unsupported attendance status")
    validate_answers(answers)
    cfg = replace(
        state.base_cfg,
        singpass_id=user.singpass_id,
        singpass_password=user.singpass_password,
        dry_run=dry_run,
        discover=False,
        preflight=False,
    )
    provider: OtpProvider = TelegramOtpProvider(interaction)
    current_user = load_users(state.users_path).get(user.telegram_id)
    if state.otp_server is not None and current_user is not None and current_user.otp_token:
        provider = state.otp_server.provider(str(user.telegram_id), provider)
    return cfg, provider, answers


async def _drive(interaction: Interaction, user: User, *, dry_run: bool, override: bool) -> None:
    state = interaction._state
    try:
        cfg, otp, answers = await _collect(interaction, user, dry_run=dry_run)
        await interaction.send(
            "Starting attendance. You will be asked for OTP when required."
            + (" Dry run: no submission." if dry_run else "")
        )
        result = await state.runner.run(cfg, otp, answers, override=override)
        messages = {
            "confirmed": "Attendance submission confirmed.",
            "dry_run": "Dry run complete: attendance filled and verified, not submitted.",
            "unknown": "Submission outcome UNKNOWN. It may have been submitted. "
            "Do not retry automatically; verify with the operator.",
            "failed": "Attendance failed before submission. No confirmed submission was recorded.",
        }
        await interaction.send(
            messages.get(
                result.status,
                "No attendance submission was confirmed. Check the recorded outcome with the operator.",
            )
        )
    except BusyRun:
        await interaction.send("Browser busy. Try again later; this request was not queued.")
    except DuplicateRun:
        await interaction.send(
            "An existing attempt blocks this submission. Verify its outcome first. "
            "Intentional resubmission requires /attend override."
        )
    except TimeoutError:
        await interaction.send("Input timed out. No new run was started.")
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramError):
            await interaction.send(
                "Run cancelled. If submission had begun its outcome may be UNKNOWN; "
                "verify before retrying."
            )
        raise
    except Exception:
        log.error("Attendance session failed; sensitive details omitted")
        await interaction.send(
            "Attendance could not complete. Check the recorded outcome before retrying; "
            "an unconfirmed submission must not be assumed to have failed."
        )
    finally:
        _release(interaction)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Schedule", callback_data="menu:schedule"),
                InlineKeyboardButton("Set Time", callback_data="menu:time"),
            ],
            [
                InlineKeyboardButton("Attend Now", callback_data="menu:attend"),
                InlineKeyboardButton("Dry Run", callback_data="menu:dryrun"),
            ],
            [InlineKeyboardButton("Cancel", callback_data="menu:cancel")],
        ]
    )
    await context.bot.send_message(
        user.telegram_id,
        "Assisted attendance: OTP and MC details require your input. Schedule times use Asia/Singapore. "
        "Use /attend override only for an intentional resubmission after checking the previous outcome.",
        reply_markup=keyboard,
    )


async def _start_run(update: Update, context: ContextTypes.DEFAULT_TYPE, *, dry_run: bool) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    args = context.args or []
    if args and (dry_run or args != ["override"]):
        await context.bot.send_message(user.telegram_id, "Use /attend, /attend override, or /dry_run.")
        return
    state: BotState = context.application.bot_data["mdc"]
    if state.runner.busy:
        await context.bot.send_message(user.telegram_id, "Browser busy. Try again later.")
        return
    try:
        interaction = _begin_session(state, user.telegram_id)
    except BusyRun:
        await context.bot.send_message(user.telegram_id, "A session is active. Use /cancel first.")
        return
    interaction.task = asyncio.create_task(
        _drive(interaction, user, dry_run=dry_run, override=args == ["override"])
    )
    interaction.task.add_done_callback(_task_done)


async def _attend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_run(update, context, dry_run=False)


async def _dry_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_run(update, context, dry_run=True)


def _calendar_keyboard(days: dict[str, str]) -> InlineKeyboardMarkup:
    today = datetime.now(SG).date()
    buttons = []
    for offset in range(14):
        day = today + timedelta(days=offset)
        status = days.get(day.isoformat(), "unset")
        buttons.append(
            InlineKeyboardButton(
                f"{day:%a} {day.day} {status}", callback_data=f"sched:{day.isoformat()}"
            )
        )
    return InlineKeyboardMarkup(
        [buttons[:7], buttons[7:], [InlineKeyboardButton("Done", callback_data="sched:done")]]
    )


def _day_status_keyboard(day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"set:{day}:{status}")
                for label, status in [
                    ("Normal", "normal"),
                    ("WFH", "wfh"),
                    ("MC", "mc"),
                    ("Skip", "none"),
                    ("Back", "back"),
                ]
            ]
        ]
    )


async def _settings(interaction: Interaction, *, calendar: bool) -> None:
    state, uid = interaction._state, interaction.chat_id
    try:
        if calendar:
            interaction._expecting = "schedule"
            interaction._future = asyncio.get_running_loop().create_future()
            schedule = load_schedules(state.store.path).get(uid, Schedule("08:30", {}))
            msg = await state.app.bot.send_message(
                uid,
                "Pick a day to set status. Changes save immediately. "
                "OTP and MC input are still required.",
                reply_markup=_calendar_keyboard(schedule.days),
            )
            interaction._cal_msg_id = msg.message_id
            await asyncio.wait_for(interaction._future, state.base_cfg.prompt_timeout)
        else:
            raw = await interaction.ask_text(
                "Enter HH:MM Singapore time, strictly before "
                f"{state.base_cfg.attendance_deadline} (e.g. 08:30):",
                "time",
                state.base_cfg.prompt_timeout,
            )
            try:
                parsed = datetime.strptime(raw, "%H:%M")
                if parsed.strftime("%H:%M") != raw or raw >= state.base_cfg.attendance_deadline:
                    raise ValueError("invalid schedule time")
                save_schedule(state.store.path, uid, time=raw)
            except ValueError:
                await interaction.send(
                    "Invalid time. Use HH:MM strictly before the attendance deadline. "
                    "Send /time to try again."
                )
                return
            if state.scheduler:
                state.scheduler.replan()
            await interaction.send(
                f"Schedule time set to {raw} Asia/Singapore; no random jitter. "
                "OTP/input must complete before the deadline."
            )
    except TimeoutError:
        await interaction.send(
            "Settings session timed out. Calendar changes already saved remain saved."
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.error("Settings session failed; sensitive details omitted")
        await interaction.send("Could not update settings.")
    finally:
        _release(interaction)


async def _start_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, *, calendar: bool) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    try:
        interaction = _begin_session(state, user.telegram_id)
    except BusyRun:
        await context.bot.send_message(user.telegram_id, "A session is active. Use /cancel first.")
        return
    interaction.task = asyncio.create_task(_settings(interaction, calendar=calendar))
    interaction.task.add_done_callback(_task_done)


async def _time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_settings(update, context, calendar=False)


async def _schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_settings(update, context, calendar=True)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None or update.message is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    interaction = state.interactions.get(user.telegram_id)
    message = update.message
    text = message.text
    if not isinstance(text, str):
        return
    reply_id = message.reply_to_message.message_id if message.reply_to_message else None
    if interaction:
        if interaction._expecting == "otp":
            try:
                accepted = interaction.deliver_otp(
                    text,
                    user_id=user.telegram_id,
                    chat_id=message.chat_id,
                    reply_to_message_id=reply_id,
                )
            except ValueError:
                accepted = False
            # Delete attempted OTP delivery, including invalid/stale replies, best effort.
            with contextlib.suppress(TelegramError):
                await message.delete()
            if accepted:
                return
            await interaction.send(
                "OTP not accepted. Reply to the CURRENT OTP prompt with exactly six ASCII digits."
            )
            return
        if interaction.deliver_text(
            text, user_id=user.telegram_id, chat_id=message.chat_id, reply_to_message_id=reply_id
        ):
            return
    try:
        validate_otp(text)
    except ValueError:
        pass
    else:
        with contextlib.suppress(TelegramError):
            await message.delete()
    await context.bot.send_message(
        user.telegram_id,
        "No matching current prompt. Use /start or reply to the current prompt; "
        "old replies are not accepted.",
    )


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    q = update.callback_query
    if q is None or q.message is None:
        return
    await q.answer()
    state: BotState = context.application.bot_data["mdc"]
    interaction = state.interactions.get(user.telegram_id)
    data = q.data or ""
    if data.startswith("menu:"):
        handlers = {
            "menu:attend": _attend,
            "menu:dryrun": _dry_run,
            "menu:schedule": _schedule_cmd,
            "menu:time": _time,
            "menu:cancel": _cancel,
        }
        handler = handlers.get(data)
        if handler:
            await handler(update, context)
        return
    if interaction and interaction.deliver_choice(
        data, user_id=user.telegram_id, chat_id=q.message.chat.id, message_id=q.message.message_id
    ):
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(f"Selected: {data}")
        return
    if (
        not interaction
        or interaction._expecting != "schedule"
        or q.message.message_id != interaction._cal_msg_id
    ):
        await context.bot.send_message(user.telegram_id, "That button is no longer active.")
        return
    if data == "sched:done":
        if interaction._future and not interaction._future.done():
            interaction._future.set_result("done")
        with contextlib.suppress(BadRequest):
            await q.edit_message_text("Schedule session closed.")
        return
    today = datetime.now(SG).date()
    valid_days = {(today + timedelta(days=i)).isoformat() for i in range(14)}
    if data.startswith("sched:") and data[6:] in valid_days:
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(
                f"{data[6:]} — choose status:", reply_markup=_day_status_keyboard(data[6:])
            )
    elif data.startswith("set:"):
        parts = data.split(":")
        if (
            len(parts) != 3
            or parts[1] not in valid_days
            or parts[2] not in {"normal", "wfh", "mc", "none", "back"}
        ):
            return
        _, day, status = parts
        if status != "back":
            save_schedule(state.store.path, user.telegram_id, days={day: status})
            if state.scheduler:
                state.scheduler.replan()
        schedule = load_schedules(state.store.path).get(user.telegram_id, Schedule("08:30", {}))
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(
                "Pick a day to set status:", reply_markup=_calendar_keyboard(schedule.days)
            )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    interaction = state.interactions.get(user.telegram_id)
    if interaction is None:
        await context.bot.send_message(user.telegram_id, "Nothing to cancel.")
        return
    interaction.clear_pending()
    if interaction.task:
        interaction.task.cancel()
    await context.bot.send_message(
        user.telegram_id, "Cancelling. If submission began, verify the outcome before retrying."
    )


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Never dump updates, exception text, traceback locals, tokens, or OTPs.
    log.error("Telegram handler failed; sensitive details omitted")


async def _post_init(application: Application) -> None:
    state: BotState = application.bot_data["mdc"]
    if state.base_cfg.otp_http_enabled:

        def credentials() -> dict[str, str]:
            return {
                str(uid): user.otp_token
                for uid, user in load_users(state.users_path).items()
                if user.otp_token
            }

        if credentials():
            state.otp_server = OtpHttpServer(credentials, state.base_cfg.otp_http_port)
            try:
                await state.otp_server.start()
            except BaseException:
                await state.otp_server.stop()
                raise

    async def prepare(uid: int, status: str, deadline: datetime):
        user = load_users(state.users_path).get(uid)
        if user is None:
            raise ValueError("scheduled user no longer authorised")
        interaction = _begin_session(state, uid)
        interaction.scheduled = True
        interaction.task = asyncio.current_task()
        try:
            remaining = (deadline - datetime.now(SG)).total_seconds()
            if remaining <= 0:
                raise TimeoutError("attendance deadline passed")
            async with asyncio.timeout(remaining):
                await interaction.send(
                    "Scheduled assisted attendance is ready. "
                    "Provide requested MC details and OTP before the deadline."
                )
                return await _collect(interaction, user, status=status)
        except BaseException:
            _release(interaction)
            raise

    async def notify(uid: int, text: str) -> None:
        interaction = state.interactions.get(uid)
        if interaction is not None and interaction.scheduled:
            _release(interaction)
        if uid in load_users(state.users_path):
            await application.bot.send_message(uid, text)

    try:
        state.scheduler = Scheduler(state.store, state.runner, state.base_cfg, prepare, notify)
        await state.scheduler.start()
        with contextlib.suppress(TelegramError):
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Show private attendance menu"),
                    BotCommand("schedule", "Schedule the next 14 days"),
                    BotCommand("time", "Set Singapore schedule time"),
                    BotCommand("attend", "Submit; explicit override argument for resubmission"),
                    BotCommand("dry_run", "Fill and verify without submitting"),
                    BotCommand("cancel", "Cancel current interaction/run"),
                ]
            )
    except BaseException:
        await _post_stop(application)
        raise


async def _post_stop(application: Application) -> None:
    state: BotState = application.bot_data["mdc"]
    tasks = {
        interaction.task for interaction in state.interactions.values() if interaction.task is not None
    }
    for interaction in tuple(state.interactions.values()):
        interaction.clear_pending()
    for task in tasks:
        task.cancel()
    try:
        if state.scheduler is not None:
            await state.scheduler.stop()
    finally:
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await state.runner.shutdown()
            finally:
                try:
                    if state.otp_server is not None:
                        await state.otp_server.stop()
                finally:
                    for interaction in tuple(state.interactions.values()):
                        _release(interaction)


async def _post_shutdown(application: Application) -> None:
    state: BotState = application.bot_data["mdc"]
    try:
        await _post_stop(application)
    finally:
        state.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private mdcattendance Telegram bot.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium windows.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Transport logs can include the bot token in request URLs.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    cfg = load_config()
    if args.headed:
        cfg = replace(cfg, headless=False)
    if not cfg.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN must be provisioned locally.")
    load_users(cfg.users_path)
    store = StateStore(cfg.state_dir, cfg.retention_days)
    runner = AttendanceRunner(cfg, store)
    app = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["mdc"] = BotState(cfg, cfg.users_path, {}, set(), app, store, runner)
    for command, handler in [
        ("start", _start),
        ("help", _start),
        ("attend", _attend),
        ("dry_run", _dry_run),
        ("schedule", _schedule_cmd),
        ("time", _time),
        ("cancel", _cancel),
    ]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(MessageHandler(filters.Regex(r"^/dry-run(?:@[A-Za-z0-9_]+)?\s*$"), _dry_run))
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_error_handler(_on_error)
    try:
        app.run_polling(allowed_updates=["message", "callback_query"])
    finally:
        # run_polling invokes post_shutdown even when post_init fails.
        store.close()


if __name__ == "__main__":
    main()
