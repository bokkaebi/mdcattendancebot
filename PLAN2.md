# PLAN2 — Headless VPS Attendance Bot

## Goal

Run the existing Normal, WFH, and MC attendance flows on a low-resource,
display-free VPS. Keep one bot service, one active browser run, and explicit
submission outcomes. Preserve manual attendance, Telegram scheduling,
dry-run, preflight, and discovery modes.

## Architecture

```text
CLI / Telegram / Scheduler
  -> collect required profile inputs
  -> shared attendance runner
  -> launch headless Chromium
  -> Singpass login + OTP -> fill -> verify -> submit
  -> record outcome -> close Chromium
```

- Python, Playwright, python-telegram-bot, stdlib SQLite and aiohttp for optional OTP HTTP.
- Telegram long polling; no public webhook or HTTP receiver by default.
- Optional authenticated `127.0.0.1:8765` receiver races manual OTP for the current
  owner/run; CLI `--http-otp` selects HTTP-only OTP. Optional separately installed
  ngrok agent forwards an assigned free HTTPS domain; no inbound firewall opening.
- Chromium starts only for ready requests; no idle pool or saved login sessions.
- No desktop, Xvfb, VNC, Redis, Celery, or separate worker service.

## Implementation Plan

### 1. Prove the VPS authentication path

- Use a Playwright-supported Linux distribution and bundled Chromium dependencies.
- Run headless under an unprivileged account with browser sandboxing enabled.
- Verify preflight, real authentication, and a profile dry-run on the target VPS.
- Stop if Singpass rejects the VPS environment; do not assume local success transfers.
- Measure peak memory during login, OTP waiting, and filling before sizing the VPS.

### 2. Separate interfaces from execution

- Keep explicit ordered profiles as the executable answer source of truth.
- CLI and Telegram collect and validate status, clinic, and timing before execution.
- Add one shared runner for account locking, browser execution, and outcome recording.
- Make the scheduler call the runner, not private Telegram handlers.
- Keep accessibility-based filling and fatal handling of unapplied answers.
- Read back expected selections and text immediately before submission.
- Prefer verified authentication selectors; fail clearly on an unexpected page.

### 3. Make execution bounded and restart-safe

- Permit one active browser run; reject manual requests when busy and defer scheduled
  work only within its allowed attendance window.
- Bound OTP waiting and total run duration; close Chromium on every exit path.
- Store schedules and attempts in SQLite, separately from credentials.
- Persist `prepared -> running -> submitting -> confirmed`, with `failed` for known
  pre-submission failures and `unknown` for an unconfirmed submission attempt.
- Persist `submitting` before clicking submit. Never automatically retry an unknown
  outcome; recover interrupted attempts conservatively after restart.
- Block ordinary duplicate submissions by account, form, and attendance date;
  require an explicit override for intentional resubmission.
- Do not claim exactly-once delivery without FormSG support.

### 4. Secure Telegram and scheduling

- Provision credentials locally in a restricted file; remove password onboarding
  through Telegram. Allowlist users and accept interaction only in private chats.
- Accept OTP only for the owning user's current pending run; validate its format
  and discard it after use. Do not log credentials or OTP contents.
- Enable phone OTP only with locally provisioned unique bearer tokens: CLI env
  token or Telegram per-owner users-file token, not ngrok's agent authtoken.
  GET `/otp/pending` then POST `/otp` with request ID and six ASCII digits.
  Reload Telegram credentials per request; consume/expire IDs on every exit.
  No early queue, unauthenticated legacy endpoint or late-SMS replay to a new ID.
  Keep loopback binding, bounded bodies/time/rate, no-store replies and no capture;
  ngrok TLS termination is an explicit provider trust boundary.
- Use `Asia/Singapore` for scheduling and browser timezone.
- Use deterministic times before the attendance deadline; no random jitter.
- Preserve completed attempts when schedules change; notify on missed or skipped runs.
- Describe scheduling as assisted submission: OTP and MC details may require input.

### 5. Deploy and verify

- Deploy one virtual environment and systemd service; start on boot and restart on failure.
- Use a dedicated writable state directory, restricted secrets, and bounded journald logs.
- Cancel active tasks and close browsers during shutdown; preserve uncertain outcomes.
- Set memory and task limits from measured usage, not guessed Chromium requirements.
- Disable routine tracing, video, screenshots, and authenticated HTML dumps;
  keep discovery explicit and diagnostic artifacts restricted and short-lived.
- Keep focused regression checks for missing answers, stale OTP delivery, schedule
  replanning/restart duplication, and unknown outcomes without automatic retry.
- Exercise a real headless fill, then one explicitly authorised submit and confirmation.
- Update deployment documentation and remove superseded paths after cutover.

## Acceptance

- Existing profiles and operator modes work without a display on the target VPS.
- No Chromium process remains idle or survives a completed/cancelled run.
- Scheduling and manual requests share the same submission safeguards.
- Restarts do not automatically replay confirmed or uncertain submissions.
- Peak memory is measured and fits the chosen VPS; logs and stored data are bounded.
- No public credential onboarding or unauthenticated OTP endpoint remains.

These are target acceptance gates, not claims of target proof. Local regressions
and sandboxed fixture-browser dry-runs have exercised HTTP OTP, profile fill/readback
and cleanup, but do not establish live Singpass/VPS acceptance. Target authenticated
dry-runs, whole-service memory/task measurements and an explicitly authorised real
submit/confirmation remain operator-only gates and have not passed here.

## Out of Scope

Additional attendance profiles, multi-browser concurrency, Docker, generic workflow
engines, persistent browser sessions, and distributed infrastructure.
