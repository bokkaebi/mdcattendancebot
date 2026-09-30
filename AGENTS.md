# Repository Guidelines

## Project Overview

`mdcattendance` automates the MINDEF MDC Attendance Form at
`https://form.gov.sg/6818179de1b7fe5377eb5c20` using Playwright and Singpass
password login. The bot prompts for an attendance profile (`normal`, `wfh`, or
`mc`), completes Singpass + OTP + MyInfo consent, fills the conditional FormSG
questions, and submits.

Screenshot source of truth:

- `website/snapshots/initial.png` — initial authenticated form (Name, Department, Status).
- `website/snapshots/normal.png` — completed Normal-day answers.
- `website/snapshots/wfh.png` — completed WFH-day answers.
- `website/snapshots/mc.png` — completed MC-day answers; clinic and appointment timing intentionally blank and prompted per run.

## Architecture & Data Flow

```text
CLI / allowlisted private Telegram chat / SQLite scheduler
  -> validate ordered attendance profile and required MC details
  -> shared AttendanceRunner + cross-process flock
  -> sandboxed headless bundled Chromium (Asia/Singapore)
  -> Singpass password + bounded owning-user OTP + MyInfo consent
  -> apply answers -> verify read-back
  -> persist submitting -> click "End of form. Submit now" -> verify confirmation
  -> persist outcome -> close browser
```

- `attendance.py` owns exact screenshot-derived labels and ordered profiles.
  Order matters: Department -> Status -> conditionally mounted questions -> acknowledgements.
- `runner.py` owns account/date duplicate protection, one active browser, total
  timeout, cancellation and conservative outcome recording.
- `storage.py` stores attempts, schedules and dispatch journal in private SQLite.
  Unknown submissions never auto-retry; there is no exactly-once guarantee.
- `scheduler.py` calls the runner directly, not Telegram handlers. Scheduled work
  is assisted submission: OTP and MC details still require the owner.
- `bot.py` owns sandboxed browser navigation/authentication and cleanup.
- `formfiller.py` strictly applies and verifies every expected answer.
- `otp.py` provides bounded terminal OTP and six-ASCII-digit validation.
- `telegram_bot.py` uses long polling, locally provisioned restricted credentials,
  a private-chat allowlist and prompt-bound OTP. No credential onboarding exists.
- `server.py` optionally provides an aiohttp OTP receiver bound only to
  `127.0.0.1` (default 8765), off by default. Bearer auth identifies the owner:
  GET `/otp/pending`, then POST `/otp` with exactly `request_id` and `otp`.
  CLI uses `OTP_HTTP_TOKEN`; Telegram uses unique per-owner `otp_token` values
  reloaded from the users file per request. Owners without tokens remain manual.
  CLI `--http-otp` forces HTTP-only OTP; env enablement races manual input.
  Consume/expire IDs on manual/HTTP completion, timeout, cancellation and shutdown;
  never queue early OTP or retry a late SMS against a new ID. Bound bodies,
  read time and request rate; use no-store replies and no secret/request logging.
  Legacy unauthenticated endpoints are unsupported.
- Optional operator-installed ngrok uses an assigned free HTTPS dev domain and a
  separate agent authtoken; no VPN or inbound firewall opening. Disable local
  inspection and cloud Full Capture separately. ngrok terminates TLS and can
  access OTP data. Main bot operation/manual OTP must not depend on the tunnel.

FormSG is a React SPA. After redirecting back from MyInfo consent, the bot
waits for `form[novalidate] div[role="radiogroup"]` to mount — never
`networkidle`, which reCAPTCHA/analytics sockets keep from settling — plus
`FORM_SETTLE_MS` (default 3000 ms) before discover/fill.

## Key Directories

| Path | Purpose |
| --- | --- |
| `src/mdcattendance/attendance.py` | `DayType`, Normal/WFH/MC profiles, CLI prompts |
| `src/mdcattendance/bot.py` | Playwright Singpass -> FormSG workflow |
| `src/mdcattendance/formfiller.py` | radios, checkboxes, text/date filling; strict completion invariant |
| `src/mdcattendance/otp.py` | terminal OTP provider protocol/implementation |
| `src/mdcattendance/server.py` | optional owner/run-bound authenticated loopback OTP receiver |
| `src/mdcattendance/config.py` | frozen env-backed `Config` |
| `src/mdcattendance/__main__.py` | argparse and async orchestration |
| `website/snapshots/*.png` | selected-answer source of truth |
| `src/mdcattendance/runner.py` | shared execution, process lock and submission safeguards |
| `src/mdcattendance/storage.py` | private SQLite attempts, schedules and dispatch journal |
| `src/mdcattendance/scheduler.py` | deterministic Singapore assisted scheduling before 09:00 |
| `src/mdcattendance/telegram_bot.py` | private allowlisted long-polling interface |
| `state/diagnostics/form-*.html` | explicit restricted discovery output with TTL |
| `DEPLOYMENT.md`, `deploy/mdcattendance.service` | Ubuntu 24.04 deployment and target gates |

## Development Commands

```sh
uv sync
uv run playwright install chromium

uv run mdcattendance                         # prompts normal/wfh/mc
uv run mdcattendance --day-type normal
uv run mdcattendance --day-type wfh
uv run mdcattendance --day-type mc           # prompts clinic + appointment timing
uv run mdcattendance --headed --day-type mc
uv run mdcattendance --headed --dry-run --day-type normal  # fill and verify, never submit
uv run mdcattendance --discover              # auth, private diagnostic HTML, no submit
uv run mdcattendance --preflight             # no credentials/OTP submitted
uv run mdcattendance-tg                      # restricted local allowlist, long polling

uvx ruff check src
```


## Code Conventions & Common Patterns

- Python >=3.11 (Ubuntu 24.04 deployment uses 3.12), `uv`, Playwright async API,
  python-telegram-bot, stdlib SQLite and aiohttp for optional OTP HTTP.
- Use accessible Playwright locators (`get_by_role`, `get_by_label`), never
  generated Chakra CSS classes.
- FormSG radio questions use `role="radiogroup"` with names such as
  `3. Status`; `_question_name()` allows the numeric prefix.
- Chakra radio/checkbox inputs are visually hidden under labels. Use
  `check(force=True)` after a scoped accessible-name match so the correct input
  changes and events dispatch without pointer interception.
- Acknowledgement questions can share titles; match their unique checkbox text.
- Conditional fields are applied iteratively with a short reveal delay.
- Missing expected answers are fatal. Never downgrade the strict failure to a
  warning or submit a partial form.
- `Config` is frozen; CLI overrides use `dataclasses.replace`.
- Restricted `.env`/`MDCATTENDANCE_ENV_FILE`, `users.json`, state and diagnostics
  remain uncommitted. Never read real local credentials during development.

## Important Files

- `.env.example` — required credentials and optional runtime settings.
- `pyproject.toml` / `uv.lock` — dependencies, entry point, lint config.
- `src/mdcattendance/attendance.py` — exact profile values; update alongside screenshots.
- `src/mdcattendance/bot.py` — selector provenance (`VERIFIED`, `INFERRED`, `USER-REPORTED`).

## Runtime / Tooling Preferences

- Development commands use `uv run`; deployed systemd uses the single synced
  environment's entry point directly, without syncing at startup.
- Use bundled Chromium with sandboxing and user namespaces. Ubuntu 24.04 is the
  supported VPS; do not equate local launch success with target Singpass acceptance.
- Shell is fish. Keep examples portable or explicitly fish-compatible.
- Never commit Singpass credentials, discovered authenticated HTML, or personal
  attendance answers.

## Testing & QA

Verification is behavioral and focused. Do not treat historical local results as
proof of the current target VPS:

- Run focused regressions for missing/changed answers, stale OTP, schedule journal
  restart/replan behavior and unknown outcomes without automatic retry.
- Exercise a sandboxed headless browser and verify browser teardown on completion,
  cancellation and timeout.
- On the actual VPS, pass preflight and authenticated profile dry-run before
  enabling schedules. Stop if Singpass rejects the environment.
- Submit only with explicit operator authorization; verify real confirmation.
- Measure whole-service peak memory/tasks during login, OTP waiting and filling
  before imposing cgroup limits. Follow `DEPLOYMENT.md` for bounded logs/state,
  diagnostic TTL, shutdown and clean JSON-to-SQLite cutover.
