# mdcattendance

Operator guide for assisted Normal/WFH/MC attendance submission. A sandboxed headless Chromium logs in
with your Singpass password, you provide the six-ASCII-digit OTP when prompted, the form is filled, read
back, then submitted and confirmed. Times use Singapore time; the 09:00 deadline (`ATTENDANCE_DEADLINE`)
means schedule strictly earlier (e.g. 08:30). Submission is assisted: stay available for OTP and MC prompts.

## Setup and safe checks

Requires Python >= 3.11 and `uv`. Install, then run the safe checks (no login, no submission;
`--preflight` checks login selectors without credentials):

```sh
uv sync --locked
uv run playwright install chromium
uv run python -m unittest discover -s tests -v
uvx ruff check src tests
uv run mdcattendance --preflight
```

## Credentials (.env)

Create a restricted env file once; never overwrite an existing one:

```sh
test -e .env || install -m0600 .env.example .env
chmod 0600 .env
```

Edit `.env` locally (e.g. `vim .env`) and set `SINGPASS_ID`, `SINGPASS_PASSWORD` and `FORM_URL`. Never put passwords in shell commands, chat, logs or screenshots. Keep the file mode 0600, never
commit it, and point `MDCATTENDANCE_ENV_FILE` at it if it lives elsewhere.

## Dry-run first (no submission)

`--preflight`, `--discover` and `--dry-run` are mutually exclusive; none submits. Dry-run every profile you will use before any real submission:

```sh
uv run mdcattendance --day-type normal --dry-run
uv run mdcattendance --day-type wfh --dry-run
uv run mdcattendance --day-type mc --dry-run
```

A dry-run really logs in and fills the form but stops before Submit; expect `Dry-run completed;
no submission performed.` You will be prompted for a six-digit OTP, and for MC also the clinic
name and appointment timing in 24-hour `HHMM` form (e.g. `0930hrs`). Add `--headed` to watch the
browser window. `--discover` is a sensitive diagnostic mode that saves authenticated form HTML
into the restricted `STATE_DIR` diagnostics directory. Artifacts are private and must never be
shared; they expire after `DIAGNOSTIC_TTL_HOURS` (default 24) and are cleaned on later browser
runs — idle cleanup is covered in DEPLOYMENT.md, not guaranteed at exactly 24 hours.

After authorisation for a real date, omit `--dry-run` to submit (e.g. `uv run mdcattendance --day-type normal`).

## Telegram bot

Provision a bot token in `.env` (`TELEGRAM_BOT_TOKEN`), then create the private allowlist locally
from the example (never via chat):

```sh
test -e users.json || install -m0600 users.example.json users.json
chmod 0600 users.json
```

Edit `users.json` so its keys are your numeric Telegram user ID(s) with your Singpass credentials
and department, then start the bot:

```sh
uv run mdcattendance-tg
```

Private-chat commands: `/start` (menu), `/schedule` (next 14 days), `/time` (schedule time), `/attend` (real submission; `override` argument for intentional resubmission), `/dry_run` (fill
and verify, no submission), `/cancel` (cancel current interaction/run). OTP replies must answer
the exact current OTP prompt from the owning user; stale replies are rejected.

**Warning:** scheduled runs and `/attend` perform real submissions — they are not test modes. Use `/dry_run` to test Telegram without submitting. Keep schedule times strictly before 09:00
and be available for prompts.

## Optional phone OTP

Manual OTP remains the default; no HTTP listener is started. To enable phone delivery,
set `OTP_HTTP_ENABLED=true` and `OTP_HTTP_PORT=8765` in the restricted env file.
Generate a unique secret locally with `uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'`
and transfer it only into private configuration (never chat or logs). Tokens must be
32–128 ASCII URL-safe characters (`A-Za-z0-9_-`).

- CLI: set `OTP_HTTP_TOKEN` in `.env`. Env enablement races phone delivery against
  terminal OTP; `--http-otp` forces HTTP-only OTP and enables the receiver.
- Telegram: add a unique `otp_token` to each participating owner's `users.json`
  record. `OTP_HTTP_TOKEN` is not used for Telegram. Owners without a token remain
  manual; the bot binds only when enabled and at least one owner has a token.
  Users-file token rotation/removal takes effect on the next HTTP request.

```sh
uv run mdcattendance --day-type normal --dry-run --http-otp
uv run mdcattendance --day-type wfh --dry-run --http-otp
uv run mdcattendance --day-type mc --dry-run --http-otp
```

These still log in and fill without submitting; profile/MC details remain terminal
prompts. Preflight neither starts nor checks the receiver/token. The receiver binds
only `127.0.0.1`; use the optional free-ngrok setup in [DEPLOYMENT.md](DEPLOYMENT.md)
for phone access, without an owned domain, VPN or inbound firewall opening.

Phone workflow: while the owner's current run is waiting, send authenticated
`GET /otp/pending`, then `POST /otp` with exactly
`{"request_id":"<returned ID>","otp":"123456"}` and `Content-Type: application/json`.
Both requests use `Authorization: Bearer <owner token>`; never include a chat ID.
Poll sparingly only during a run. A `202` consumes the request; stale/expired IDs
return `409`. Do not fetch a fresh ID to retry a late SMS. See deployment guidance
for all responses, privacy settings and manual fallback. Legacy unauthenticated
forwarding endpoints are unsupported.

OTP troubleshooting: `OTP page ready; waiting for OTP delivery` means the browser
detected supported OTP inputs. Until then, no HTTP OTP request is pending. If the
OTP page is visible but that log never appears, inspect its input attributes;
the detector accepts one input with the user-reported exact label
`Enter 6-digit OTP code` or `autocomplete="one-time-code"`, or six
`maxlength="1" inputmode="numeric"` inputs. Detection and entry use the same
supported controls; unrelated telephone inputs are not accepted. Use headed mode
and a dry-run.

## State, outcomes and duplicates

SQLite state lives in `STATE_DIR` (default `state/`, mode 0700) and holds attempts, schedules and
the process lock. Point every CLI and Telegram client at the same directory; only one browser run
is active at a time. Never reset or replace this journal to bypass duplicate protection.

A run ends `confirmed`, `failed` (known pre-submit failure), or `unknown` (interrupted or unconfirmed — check the real form record; never auto-retried).
Confirmed and unknown attempts are journalled in SQLite and block ordinary duplicates for the same account, form and date; submission is not exactly-once.
Resubmission after checking the actual form record is an explicit exception: CLI `--override` (real submissions only) or `/attend override`. The scheduler never overrides.

## Deployment

For Ubuntu 24.04 VPS setup with systemd and the required target dry-run, measurement and authorised-submission gate, see [DEPLOYMENT.md](DEPLOYMENT.md).
Do not enable scheduled attendance until that gate passes; local checks do not verify target authentication or submission.
