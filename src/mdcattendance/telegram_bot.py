"""Private, allowlisted Telegram interface for assisted attendance submission."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
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
from .miniapp import MiniAppServer
from .otp import OtpProvider, validate_otp
from .records import SubmissionRecords
from .runner import AttendanceRunner, BusyRun, DuplicateRun, answer_hash
from .scheduler import Scheduler
from .schedules import PlannerBlocked, PlannerConflict, PlannerInvalid
from .server import OtpHttpServer
from .storage import AdditionalSubmissionConsent, StateStore
from .users import User, load_users

log = logging.getLogger("mdcattendance.telegram")
SG = ZoneInfo("Asia/Singapore")

# One manual-fallback warning per OTP prompt. Automatic phone delivery is
# announced, but silence alone never starts or retries a submission.
_OTP_WARNING_SECONDS = 60.0
_ONBOARD_PROMPT = (
    "Enter your full name in ALL CAPS, exactly as it appears in the attendance form's "
    "MyInfo name field."
)
_DECISION_ACTIONS = {"keep", "change", "submit_now", "skip", "review"}
_PROFILE_LABELS = {
    "normal": "Present (IS)",
    "wfh": "Work-from-Home",
    "ma": "MA",
    "skip": "Skip",
}


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
    miniapp_server: MiniAppServer | None = None


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

    async def warn_fallback(self) -> None:
        """Best-effort single warning, replied to the live OTP prompt message."""
        if self._pending_token is None or self._future is None or self._future.done():
            return
        with contextlib.suppress(TelegramError):
            await self._app.bot.send_message(
                self.chat_id,
                "Automatic phone OTP delivery has not arrived. Reply to the OTP prompt with the "
                "current six-digit code, or resend it from the provisioned phone.",
                reply_to_message_id=self._prompt_message_id,
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
        if self._expecting in {None, "otp", "choice"}:
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
    """Manual reply fallback, with one bounded warning while phone delivery is awaited."""

    def __init__(
        self,
        interaction: Interaction,
        *,
        http_awaited: bool = False,
        warning_seconds: float = _OTP_WARNING_SECONDS,
    ) -> None:
        self._interaction = interaction
        self._http_awaited = http_awaited
        self._warning_seconds = warning_seconds

    async def wait_for_otp(self, timeout: float) -> str:
        if self._http_awaited:
            prompt = (
                "Automatic phone delivery of the Singpass OTP is awaited. If it does not arrive, "
                "reply to THIS message with the current six-digit code. Earlier replies are not "
                "accepted."
            )
        else:
            prompt = (
                "Reply to THIS message with the current six-digit Singpass OTP, or use phone "
                "delivery if provisioned. Earlier replies are not accepted."
            )
        warning: asyncio.Task | None = None
        if self._http_awaited and timeout > self._warning_seconds:
            warning = asyncio.create_task(self._warn_once())
        try:
            return await self._interaction.ask_text(prompt, "otp", timeout)
        finally:
            if warning is not None:
                warning.cancel()
                await asyncio.gather(warning, return_exceptions=True)

    async def _warn_once(self) -> None:
        await asyncio.sleep(self._warning_seconds)
        await self._interaction.warn_fallback()


class _ScheduledOtpProvider:
    """Owns a transient interaction only while a dispatch actually awaits an OTP.

    prepare() must not hold an owner session while queued for the browser slot:
    that would block /attend, /name and reviews with BusyRun and leak a session
    when a review only needs canonical answers. A genuine conflict still fails
    closed the moment an OTP is requested.
    """

    def __init__(self, state: BotState, user: User) -> None:
        self._state = state
        self._user = user

    async def wait_for_otp(self, timeout: float) -> str:
        interaction = _begin_session(self._state, self._user.telegram_id)
        try:
            return await _otp_provider(self._state, interaction, self._user).wait_for_otp(timeout)
        finally:
            _release(interaction)


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


def _planner(state: BotState):
    """The Planner exists only after the scheduler has recovered under startup locks."""
    return state.scheduler.planner if state.scheduler is not None else None


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


def _settings_cfg(state: BotState, user: User, *, dry_run: bool = False) -> Config:
    return replace(
        state.base_cfg,
        singpass_id=user.singpass_id,
        singpass_password=user.singpass_password,
        dry_run=dry_run,
        discover=False,
        preflight=False,
    )


def _otp_provider(state: BotState, interaction: Interaction, user: User) -> OtpProvider:
    current = load_users(state.users_path).get(user.telegram_id)
    http_ready = state.otp_server is not None and current is not None and bool(current.otp_token)
    telegram_provider = TelegramOtpProvider(interaction, http_awaited=http_ready)
    if not http_ready or state.otp_server is None:
        return telegram_provider
    return state.otp_server.provider(str(user.telegram_id), telegram_provider)


def _answers_for(profile: str | None, details: dict | None = None) -> Answers:
    """Executable planner profiles only; MC stays manual and MA needs real form evidence."""
    if profile == "normal":
        return dict(NORMAL_ANSWERS)
    if profile == "wfh":
        return dict(WFH_ANSWERS)
    if profile == "ma":
        raise PlannerBlocked(
            "Medical Appointment needs the authenticated form capture and cannot execute yet"
        )
    raise PlannerBlocked("Only Present (IS) and Work-from-Home can be submitted automatically")


def _today_deadline(cfg: Config) -> datetime:
    return datetime.combine(datetime.now(SG).date(), time.fromisoformat(cfg.attendance_deadline), SG)


def _record_text(check) -> str:
    stamp = check.checked_at.astimezone(SG).strftime("%Y-%m-%d %H:%M")
    if check.status == "found":
        rows = "; ".join(
            f"{record.timestamp.astimezone(SG):%Y-%m-%d %H:%M} {record.status}"
            for record in check.records
        )
        return f"Existing attendance found for today ({rows}; checked {stamp} SGT)."
    if check.status == "unavailable":
        return f"Attendance records could not be verified (checked {stamp} SGT)."
    return f"No existing attendance found (checked {stamp} SGT). Updates from the form may lag."


def _result_message(result) -> str:
    check = getattr(result, "record_check", None)
    if check is not None and check.status == "found":
        return (
            f"{_record_text(check)} Submission was suppressed to avoid a duplicate entry; "
            "use the review flow for an explicit additional submission."
        )
    if check is not None and check.status == "unavailable":
        return (
            f"{_record_text(check)} No submission was attempted. Retry once the source is reachable."
        )
    return {
        "confirmed": "Attendance submission confirmed.",
        "dry_run": "Dry run complete: attendance filled and verified, not submitted.",
        "unknown": "Submission outcome UNKNOWN. It may have been submitted. "
        "Do not retry automatically; verify with the operator.",
        "failed": "Attendance failed before submission. No confirmed submission was recorded.",
    }.get(
        result.status,
        "No attendance submission was confirmed. Check the recorded outcome with the operator.",
    )


def _evidence_summary(check, attempts: list[dict], answers: Answers) -> str:
    lines = [_record_text(check)]
    for record in check.records:
        details = ", ".join(f"{key}: {value}" for key, value in record.details) or "no details"
        lines.append(f"- {record.timestamp.astimezone(SG):%Y-%m-%d %H:%M} {record.status} ({details})")
    local = ", ".join(f"#{row['id']} {row['status']}" for row in attempts) or "none"
    lines.append(f"Local submissions already recorded today: {local}.")
    chosen = ", ".join(f"{key}={value}" for key, value in answers.items() if key != "Department")
    lines.append(f"Answers to submit: {chosen}.")
    lines.append(
        "Confirmation authorises one additional FormSG entry only; it never edits or replaces "
        "an existing entry."
    )
    return "\n".join(lines)


async def _verify_source(state: BotState) -> bool:
    """Installation-time check. A mismatch must never block manual attendance."""
    records = getattr(state.runner, "records", None)
    if records is None:
        return False
    try:
        await records.verify_source()
    except Exception:
        log.warning(
            "Configured Viewable worksheet could not be verified; manual attendance stays available"
        )
        return False
    return True


def _parse_decision_callback(data: str) -> tuple[str, int] | None:
    """Morning/record buttons carry an action and the revision they were built from."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "dec":
        return None
    action, revision = parts[1], parts[2]
    if action not in _DECISION_ACTIONS or not revision.isdigit():
        return None
    return action, int(revision)


def _miniapp_available(state: BotState) -> bool:
    """A WebApp launcher is live only while the Mini App server is actually running."""
    return state.miniapp_server is not None


def _edit_today_link(cfg: Config) -> str:
    """The existing Today editor deep link; the fragment suffix stays out of Config."""
    return f"{cfg.miniapp_link('today')}?edit=1"


def _view_button(state: BotState, view: str, label: str) -> InlineKeyboardButton:
    if _miniapp_available(state):
        return InlineKeyboardButton(
            label, web_app=WebAppInfo(state.base_cfg.miniapp_link(view))
        )
    return InlineKeyboardButton(label, callback_data=f"menu:{view}")


_MINIAPP_UNAVAILABLE = (
    "The Telegram Mini App is unavailable on this deployment. Use /attend or /dry_run to run "
    "attendance, /name to set your MyInfo name, or /cancel to stop."
)


async def _send_view(update: Update, context: ContextTypes.DEFAULT_TYPE, view: str, label: str) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    if _miniapp_available(state):
        await context.bot.send_message(
            user.telegram_id,
            f"{label}: open the planner Mini App.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            label, web_app=WebAppInfo(state.base_cfg.miniapp_link(view))
                        )
                    ]
                ]
            ),
        )
        return
    await context.bot.send_message(user.telegram_id, _MINIAPP_UNAVAILABLE)


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
    return _settings_cfg(state, user, dry_run=dry_run), _otp_provider(state, interaction, user), answers


async def _onboard(interaction: Interaction, user: User, planner, state: BotState) -> bool:
    """Durable uppercase MyInfo name: prompt, normalize, confirm, check uniqueness."""
    uid = user.telegram_id
    settings = planner.get_settings(uid)
    while True:
        suggestion = settings["attendance_name"] or "not set"
        raw = await interaction.ask_text(
            f"{_ONBOARD_PROMPT}\nCurrent saved name: {suggestion}",
            "name",
            state.base_cfg.prompt_timeout,
        )
        try:
            settings = planner.set_name(uid, raw, expected_revision=settings["revision"])
            break
        except (PlannerInvalid, PlannerBlocked) as exc:
            await interaction.send(f"Name not saved: {exc}")
    choice = await interaction.ask_choice(
        f"Save attendance name {settings['attendance_name']} for department {user.department}?",
        [("Confirm", "name:yes"), ("Retry", "name:no")],
    )
    if choice != "name:yes":
        await interaction.send("Name not confirmed. Send /name to try again.")
        return False
    settings = planner.confirm_name(uid, expected_revision=settings["revision"])
    departments = {tid: record.department for tid, record in load_users(state.users_path).items()}
    try:
        planner.assert_identity_unique(uid, user.department, departments)
    except PlannerBlocked as exc:
        await interaction.send(
            f"Attendance name not accepted: {exc}. Ask the operator to resolve the mapping."
        )
        return False
    await interaction.send(
        f"Attendance name {settings['attendance_name']} confirmed for {user.department}. "
        "Automatic mode stays off until you accept the policy in the Mini App settings."
    )
    return True


async def _resolve_identity(
    interaction: Interaction, user: User, state: BotState
) -> tuple[str, str] | None:
    planner = _planner(state)
    if planner is None:
        await interaction.send("The attendance planner is not running; no submission was started.")
        return None
    settings = planner.get_settings(user.telegram_id)
    if not settings["name_confirmed"] or not settings["attendance_name"]:
        if not await _onboard(interaction, user, planner, state):
            return None
        settings = planner.get_settings(user.telegram_id)
    return settings["attendance_name"], user.department


async def _drive(interaction: Interaction, user: User, *, dry_run: bool) -> None:
    state = interaction._state
    try:
        cfg, otp, answers = await _collect(interaction, user, dry_run=dry_run)
        kwargs: dict = {}
        if not dry_run:
            identity = await _resolve_identity(interaction, user, state)
            if identity is None:
                return
            attendance_name, department = identity
            kwargs = {
                "attendance_date": datetime.now(SG).date(),
                "attendance_name": attendance_name,
                "department": department,
                "deadline": _today_deadline(cfg),
            }
        await interaction.send(
            "Starting attendance. You will be asked for OTP when required."
            + (" Dry run: no submission." if dry_run else "")
        )
        result = await state.runner.run(cfg, otp, answers, **kwargs)
        await interaction.send(_result_message(result))
    except BusyRun:
        await interaction.send("Browser busy. Try again later; this request was not queued.")
    except DuplicateRun:
        await interaction.send(
            "An existing attempt blocks this submission. Verify its outcome first. "
            "Use /attend override for an explicit reviewed additional submission."
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
    except (PlannerBlocked, PlannerConflict) as exc:
        await interaction.send(f"Submission was not started: {exc}")
    except Exception:
        log.error("Attendance session failed; sensitive details omitted")
        await interaction.send(
            "Attendance could not complete. Check the recorded outcome before retrying; "
            "an unconfirmed submission must not be assumed to have failed."
        )
    finally:
        _release(interaction)


async def _settle_review(state: BotState, uid: int, day, *, result=None) -> None:
    """Settle a claimed review from the confirmed result, or the bound journal.

    Never assumes a blind failure: without a result the scheduler reads the real
    attempt bound to today's decision, so a submitting attempt stays UNKNOWN.
    """
    scheduler = state.scheduler
    if scheduler is None:
        return
    if result is not None:
        await scheduler.settle(uid, day.isoformat(), result=result)
    else:
        await scheduler.settle_from_journal(uid, day.isoformat())


async def _drive_review(interaction: Interaction, user: User, *, profile: str | None = None) -> None:
    """Explicit additional-submission review: evidence, prior attempts, then consent."""
    state = interaction._state
    today = None
    claimed = False
    try:
        cfg = _settings_cfg(state, user)
        identity = await _resolve_identity(interaction, user, state)
        if identity is None:
            return
        attendance_name, department = identity
        planner = _planner(state)
        assert planner is not None
        if profile is None:
            choice = await interaction.ask_choice(
                "Additional submission: choose the profile.",
                [("Present (IS)", "profile:normal"), ("Work-from-Home", "profile:wfh")],
            )
            profile = choice.split(":", 1)[1]
        answers = _answers_for(profile, {})
        today = datetime.now(SG).date()
        check = await state.runner.records.lookup(attendance_name, department, today, fresh=True)
        # Exact owner account and configured form for today only: another owner's
        # ids/outcomes and unrelated forms must never enter the review.
        attempts = state.store.submission_attempts(cfg.singpass_id, cfg.form_url, today)
        await interaction.send(_evidence_summary(check, attempts, answers))
        if check.status == "unavailable":
            await interaction.send(
                "External records are unavailable, so an additional submission cannot be authorised. "
                "No submission was started."
            )
            return
        choice = await interaction.ask_choice(
            "Authorise exactly one additional submission now?",
            [("Authorise", "review:yes"), ("Abort", "review:no")],
        )
        if choice != "review:yes":
            await interaction.send("Additional submission cancelled; nothing was submitted.")
            return
        # The reviewed consent binds the owner's own durable, fresh evidence row.
        planner.merge_observation(user.telegram_id, today, check)
        decision = planner.ensure_decision(user.telegram_id)
        hashed = answer_hash(answers)
        ids = [row["id"] for row in attempts]
        consent = planner.review_consent(
            user.telegram_id,
            expected_revision=decision["revision"],
            answer_hash=hashed,
            local_attempt_ids=ids,
            profile=profile,
            details={},
        )
        consumed = planner.consume_consent(
            user.telegram_id,
            expected_revision=consent["revision"],
            consent_digest=consent["consent_digest"],
            answer_hash=hashed,
            local_attempt_ids=ids,
        )
        # The claimed durable row is the only source of the reviewed fields: the
        # runner is never handed the server-side digest on its own.
        durable = consumed["consent"]
        context = AdditionalSubmissionConsent(
            date=date.fromisoformat(durable["date"]),
            answer_hash=durable["answer_hash"],
            records_digest=durable["records_digest"],
            local_attempt_ids=tuple(durable["local_attempt_ids"]),
            consent_digest=durable["consent_digest"],
            decision_uid=user.telegram_id,
        )
        claim = planner.claim_decision(
            user.telegram_id, today, expected_revision=consumed["revision"]
        )
        claimed = True
        result = await state.runner.run(
            cfg,
            _otp_provider(state, interaction, user),
            answers,
            attendance_date=today,
            attendance_name=attendance_name,
            department=department,
            deadline=_today_deadline(cfg),
            decision_uid=user.telegram_id,
            decision_revision=claim["revision"],
            consent=context,
        )
        await _settle_review(state, user.telegram_id, today, result=result)
        await interaction.send(_result_message(result))
    except BusyRun:
        await interaction.send("Browser busy. Try again later; this request was not queued.")
    except TimeoutError:
        await interaction.send("Input timed out. No new run was started.")
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramError):
            await interaction.send(
                "Review cancelled. If submission had begun its outcome may be UNKNOWN; "
                "verify before retrying."
            )
        raise
    except (PlannerBlocked, PlannerConflict, PlannerInvalid) as exc:
        await interaction.send(f"Additional submission was not started: {exc}")
    except DuplicateRun:
        await interaction.send(
            "An unreviewed local attempt blocks this additional submission; review again."
        )
    except Exception:
        log.error("Additional submission review failed; sensitive details omitted")
        await interaction.send(
            "The review could not complete. No additional submission is claimed; "
            "check the recorded outcome before retrying."
        )
    finally:
        try:
            if claimed and today is not None:
                await _settle_review(state, user.telegram_id, today)
        finally:
            _release(interaction)


async def _begin_or_notify(state: BotState, uid: int) -> Interaction | None:
    try:
        return _begin_session(state, uid)
    except BusyRun:
        await state.app.bot.send_message(uid, "A session is active. Use /cancel first.")
        return None


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    keyboard = InlineKeyboardMarkup(
        [
            [_view_button(state, "plan", "Open Planner")],
            [
                _view_button(state, "today", "Open Today"),
                InlineKeyboardButton("Attend Now", callback_data="menu:attend"),
            ],
            [
                InlineKeyboardButton("Dry Run", callback_data="menu:dryrun"),
                InlineKeyboardButton("Cancel", callback_data="menu:cancel"),
            ],
        ]
    )
    await context.bot.send_message(
        user.telegram_id,
        "Assisted attendance: OTP and MC details require your input. Planned profiles use "
        "Asia/Singapore times. Use /attend override only for an intentional resubmission after "
        "checking the previous outcome.",
        reply_markup=keyboard,
    )


async def _start_run(update: Update, context: ContextTypes.DEFAULT_TYPE, *, dry_run: bool) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    if state.runner.busy:
        await context.bot.send_message(user.telegram_id, "Browser busy. Try again later.")
        return
    interaction = await _begin_or_notify(state, user.telegram_id)
    if interaction is None:
        return
    interaction.task = asyncio.create_task(_drive(interaction, user, dry_run=dry_run))
    interaction.task.add_done_callback(_task_done)


async def _attend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    args = context.args or []
    if args and args != ["override"]:
        await context.bot.send_message(user.telegram_id, "Use /attend, /attend override, or /dry_run.")
        return
    if args == ["override"]:
        state: BotState = context.application.bot_data["mdc"]
        if state.runner.busy:
            await context.bot.send_message(user.telegram_id, "Browser busy. Try again later.")
            return
        interaction = await _begin_or_notify(state, user.telegram_id)
        if interaction is None:
            return
        interaction.task = asyncio.create_task(_drive_review(interaction, user))
        interaction.task.add_done_callback(_task_done)
        return
    await _start_run(update, context, dry_run=False)


async def _dry_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_run(update, context, dry_run=True)


async def _name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    planner = _planner(state)
    if planner is None:
        await context.bot.send_message(
            user.telegram_id, "The attendance planner is not running; onboarding is unavailable."
        )
        return
    interaction = await _begin_or_notify(state, user.telegram_id)
    if interaction is None:
        return

    async def run() -> None:
        try:
            await _onboard(interaction, user, planner, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("Onboarding failed; sensitive details omitted")
            await interaction.send("Onboarding could not complete. No name change was saved.")
        finally:
            _release(interaction)

    interaction.task = asyncio.create_task(run())
    interaction.task.add_done_callback(_task_done)


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    interaction = state.interactions.get(user.telegram_id)
    if interaction is not None:
        interaction.clear_pending()
        if interaction.task:
            interaction.task.cancel()
    scheduled = state.scheduler is not None and await state.scheduler.cancel(user.telegram_id)
    if interaction is None and not scheduled:
        await context.bot.send_message(user.telegram_id, "Nothing to cancel.")
        return
    await context.bot.send_message(
        user.telegram_id, "Cancelling. If submission began, verify the outcome before retrying."
    )


async def _schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_view(update, context, "plan", "Open Planner")


async def _time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_view(update, context, "settings", "Open Settings")


_DECISION_STATE_TEXT = {
    "awaiting": "Automatic attendance is scheduled for today.",
    "ready": "Automatic attendance is scheduled for today.",
    "held": "Today's automatic fallback is paused and waits for your decision.",
    "running": "A submission is in progress for today.",
    "confirmed": "Today's attendance was submitted and confirmed.",
    "unknown": "Today's outcome is UNKNOWN; it may have been submitted. Do not retry automatically.",
    "failed": "Today's submission failed before confirmation.",
    "skipped": "Today's automatic attendance was skipped.",
    "recorded": "Attendance for today is already recorded; automatic submission was suppressed.",
    "missed": "Today's automatic window was missed.",
}


def _today_text(view: dict) -> str:
    """Bounded text status: source evidence and the local outcome never merge."""
    decision = view.get("decision") or {}
    state_text = _DECISION_STATE_TEXT.get(decision.get("state", ""), "Today's status is unknown.")
    lines = [f"Today ({view.get('today', '')}): {state_text}"]
    source = view.get("source_check")
    if view.get("identity_suspended"):
        lines.append(
            "Attendance-name mapping is suspended. The operator must resolve the shared name "
            "and department before attendance records or submissions can be verified."
        )
    elif source is None:
        lines.append(
            "Attendance records: could not be verified for your account right now "
            "(unknown; not a record of no attendance)."
        )
    elif source.get("status") == "found":
        lines.append("Attendance records: existing attendance found for today.")
    elif source.get("status") == "unavailable":
        lines.append("Attendance records: the source is currently unavailable.")
    else:
        lines.append("Attendance records: none found for today yet.")
    local = view.get("local_outcome")
    if local:
        lines.append(f"Local submission record today: {local}.")
    if view.get("next_action") == "action_required":
        lines.append("A decision is needed; use /start to see the available buttons.")
    if view.get("identity_suspended"):
        lines.append("This text status is read-only; /cancel still works for your active session.")
    else:
        lines.append("This text status is read-only; /attend submits, /dry_run only verifies.")
    return "\n".join(lines)


async def _today_status(state: BotState, uid: int) -> str:
    scheduler = state.scheduler
    if scheduler is None:
        return (
            "The attendance planner is not running, so today's status is unavailable. "
            "Use /attend to submit manually."
        )
    try:
        view = await scheduler.today(uid)
    except (RuntimeError, PlannerBlocked, PlannerConflict, PlannerInvalid):
        log.warning("Today status could not be read; sensitive details omitted")
        return (
            "Today's status is unavailable right now. Use /attend to submit manually and do not "
            "assume an earlier submission succeeded."
        )
    return _today_text(view)


async def _today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _guard(update, context)
    if user is None:
        return
    state: BotState = context.application.bot_data["mdc"]
    if _miniapp_available(state):
        await _send_view(update, context, "today", "Open Today")
        return
    await context.bot.send_message(user.telegram_id, await _today_status(state, user.telegram_id))


async def _handle_decision_action(
    context: ContextTypes.DEFAULT_TYPE,
    query,
    state: BotState,
    user: User,
    action: str,
    revision: int,
) -> None:
    """Act on a morning/record button only while its own prompt message is current."""
    planner = _planner(state)
    decision = planner.get_decision(user.telegram_id) if planner is not None else None
    if (
        planner is None
        or state.scheduler is None
        or decision is None
        or query.message is None
        or decision["prompt_message_id"] != query.message.message_id
        or revision != decision["revision"]
    ):
        await query.answer("That button is no longer active.")
        return
    uid = user.telegram_id
    try:
        # The button's own revision is authoritative: notification bookkeeping never
        # bumps the decision revision, so an older button can never rebase onto a
        # newer plan. Conflicting mutations raise PlannerConflict instead.
        if action == "keep":
            await state.scheduler.action(uid, "keep", expected_revision=revision)
            await context.bot.send_message(uid, "Keeping the saved plan for today.")
        elif action == "change":
            if not _miniapp_available(state):
                # No reachable editor: leave today's plan untouched (never hold blindly),
                # and keep the manual paths reachable instead of stranding the owner.
                await context.bot.send_message(
                    uid,
                    "The plan editor Mini App is unavailable, so today's plan is unchanged. "
                    "Use /attend, /dry_run, /name or /cancel from the menu.",
                )
                return
            # Pause fallback on the server before the editor is opened.
            await state.scheduler.action(uid, "hold", expected_revision=revision)
            await _send_editor(context, state.base_cfg, uid, "Edit today's plan")
        elif action == "submit_now":
            await state.scheduler.action(uid, "submit_now", expected_revision=revision)
            await context.bot.send_message(
                uid, "Submitting the saved plan now; the run waits for the single browser slot."
            )
        elif action == "skip":
            await state.scheduler.action(uid, "skip", expected_revision=revision)
            await context.bot.send_message(uid, "Today's automatic attendance is skipped.")
        elif action == "review":
            if state.runner.busy:
                await context.bot.send_message(uid, "Browser busy. Try again later.")
                return
            interaction = await _begin_or_notify(state, uid)
            if interaction is None:
                return
            interaction.task = asyncio.create_task(_drive_review(interaction, user))
            interaction.task.add_done_callback(_task_done)
    except (PlannerBlocked, PlannerConflict, PlannerInvalid) as exc:
        await context.bot.send_message(uid, f"That action is no longer available: {exc}")


async def _send_editor(context: ContextTypes.DEFAULT_TYPE, cfg: Config, uid: int, label: str) -> None:
    """Open the existing Today editor fragment; only called when the Mini App is live."""
    await context.bot.send_message(
        uid,
        f"{label}. Fallback for today is paused until you save or restore the plan.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, web_app=WebAppInfo(_edit_today_link(cfg)))]]
        ),
    )


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
            "menu:cancel": _cancel,
            "menu:plan": _schedule_cmd,
            "menu:time": _time,
            "menu:today": _today_cmd,
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
    parsed = _parse_decision_callback(data)
    if parsed is None:
        await context.bot.send_message(user.telegram_id, "That button is no longer active.")
        return
    action, revision = parsed
    await _handle_decision_action(context, q, state, user, action, revision)


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

        # Start the configured loopback receiver even before any owner has a token:
        # the callback is reloaded per request, so owners provisioned later still get
        # phone OTP instead of silently falling back to manual entry.
        state.otp_server = OtpHttpServer(credentials, state.base_cfg.otp_http_port)
        try:
            await state.otp_server.start()
        except BaseException:
            await state.otp_server.stop()
            raise
    await _verify_source(state)

    async def notify(uid: int, text: str, *, decision=None) -> int | None:
        if uid not in load_users(state.users_path):
            return None
        markup = _notification_markup(state, uid, decision)
        message = await application.bot.send_message(uid, text, reply_markup=markup)
        return message.message_id

    async def prepare(uid: int, profile: str, details: dict, deadline: datetime):
        user = load_users(state.users_path).get(uid)
        if user is None:
            raise ValueError("scheduled user no longer authorised")
        if (deadline - datetime.now(SG)).total_seconds() <= 0:
            raise TimeoutError("attendance deadline passed")
        try:
            answers = _answers_for(profile, details)
        except PlannerBlocked as exc:
            await notify(uid, f"Automatic attendance is not available: {exc}")
            raise
        return _settings_cfg(state, user), _ScheduledOtpProvider(state, user), answers

    try:
        state.scheduler = Scheduler(
            state.store,
            state.runner,
            state.base_cfg,
            prepare,
            notify,
            users=lambda: load_users(state.users_path),
        )
        await state.scheduler.start()
        if state.base_cfg.miniapp_enabled:
            server = MiniAppServer(state.base_cfg, state.scheduler, otp_server=state.otp_server)
            try:
                await server.start()
            except asyncio.CancelledError:
                await server.stop()
                raise
            except Exception:
                # The Mini App is optional: a routing/bind failure must never stop
                # Telegram, manual OTP or the scheduler from running.
                with contextlib.suppress(Exception):
                    await server.stop()
                log.warning(
                    "Mini App server failed to start; Telegram, manual attendance and "
                    "scheduled prompts remain available"
                )
            else:
                state.miniapp_server = server
                with contextlib.suppress(TelegramError):
                    await application.bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(
                            text="Open Planner",
                            web_app=WebAppInfo(state.base_cfg.miniapp_link("plan")),
                        )
                    )
        if state.miniapp_server is None:
            # A previous run may have installed the Mini App chat button; clear it back to
            # the command list so the menu never points at a disabled or dead Mini App.
            with contextlib.suppress(TelegramError):
                await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        # Descriptions reflect the Mini App server that actually came up, not merely a
        # configured URL: an unavailable UI must not be promised in the command menu.
        available = state.miniapp_server is not None
        with contextlib.suppress(TelegramError):
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Show private attendance menu"),
                    BotCommand("help", "Show the private attendance menu"),
                    BotCommand("name", "Set and confirm your ALL-CAPS MyInfo name"),
                    BotCommand(
                        "schedule",
                        "Open the planner Mini App"
                        if available
                        else "Planner Mini App unavailable; use /attend or /dry_run",
                    ),
                    BotCommand("today", "Show today's attendance status"),
                    BotCommand(
                        "time",
                        "Open Mini App settings"
                        if available
                        else "Mini App settings unavailable; use /name to set your name",
                    ),
                    BotCommand("attend", "Submit; explicit override review for resubmission"),
                    BotCommand("dry_run", "Fill and verify without submitting"),
                    BotCommand("cancel", "Cancel current interaction/run"),
                ]
            )
    except BaseException:
        await _post_stop(application)
        raise


def _notification_markup(
    state: BotState, uid: int, decision: dict | None
) -> InlineKeyboardMarkup | None:
    """Only morning/reminder notices carry live decision controls (private marker).

    Every other decision-bearing notice (outcome, pause, expiration) gets a
    navigation link: its callbacks would be bound to a different message and would
    be rejected as stale, so rendering them would be a dead button.
    """
    if decision is None:
        return None
    if decision.get("_controls"):
        return _decision_keyboard(state, uid, decision)
    return InlineKeyboardMarkup([[_view_button(state, "plan", "Open Planner")]])


def _action_row(state: BotState, button) -> list[InlineKeyboardButton]:
    """Change is omitted when its editor cannot be reached; manual paths stay."""
    if _miniapp_available(state):
        return [button("change", "Change Today"), button("submit_now", "Submit Now")]
    return [button("submit_now", "Submit Now")]


def _decision_keyboard(state: BotState, uid: int, decision: dict) -> InlineKeyboardMarkup | None:
    revision = decision["revision"]

    def button(action: str, label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"dec:{action}:{revision}")

    if decision["state"] == "recorded":
        return InlineKeyboardMarkup(
            [
                [
                    button("keep", "Keep Existing"),
                    button("review", "Submit Different Attendance"),
                ]
            ]
        )
    if decision["state"] not in {"awaiting", "held", "ready"}:
        return None
    planner = _planner(state)
    auto_time = planner.get_settings(uid)["auto_time"] if planner is not None else ""
    label = _PROFILE_LABELS.get(decision["profile"] or "", "the saved plan")
    if decision["state"] == "held":
        # Nothing runs automatically while paused: never label a button with a
        # scheduled time that will not fire. Change is only offered when its editor
        # is actually reachable.
        rows = [
            _action_row(state, button),
            [button("skip", "Skip Today"), _view_button(state, "plan", "Open Planner")],
        ]
        return InlineKeyboardMarkup(rows)
    rows = [
        [button("keep", f"Keep {label} at {auto_time}".strip())],
        _action_row(state, button),
        [button("skip", "Skip Today"), _view_button(state, "plan", "Open Planner")],
    ]
    return InlineKeyboardMarkup(rows)


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
        # Stop the Mini App gateway first so no new actions or OTP proxy requests can
        # arrive while the scheduler, browser runner and OTP receiver are torn down.
        if state.miniapp_server is not None:
            server, state.miniapp_server = state.miniapp_server, None
            with contextlib.suppress(Exception):
                await server.stop()
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


# Every command the dispatcher registers; /today must stay reachable from the menu.
_BOT_COMMANDS = (
    ("start", _start),
    ("help", _start),
    ("name", _name),
    ("attend", _attend),
    ("dry_run", _dry_run),
    ("schedule", _schedule_cmd),
    ("today", _today_cmd),
    ("time", _time),
    ("cancel", _cancel),
)


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
    runner = AttendanceRunner(cfg, store, records=SubmissionRecords())
    app = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["mdc"] = BotState(cfg, cfg.users_path, {}, set(), app, store, runner)
    for command, handler in _BOT_COMMANDS:
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
