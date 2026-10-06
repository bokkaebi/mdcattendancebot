# mdcattendance

Operator guide for assisted Normal/WFH/MC attendance submission. A sandboxed headless Chromium logs in
with your Singpass password, you provide the six-ASCII-digit OTP when prompted, the form is filled, read
back, then submitted and confirmed. Times use Singapore time; the 09:00 deadline (`ATTENDANCE_DEADLINE`)
means schedule strictly earlier (e.g. 08:30). Submission is assisted: stay available for OTP and MC prompts.

An optional Telegram Mini App planner (Today/Plan/Settings) plans the next 14 Singapore dates with the
Present (IS), WFH and MA profiles. It reuses the same scheduler, runner and safety gates as the CLI and
Telegram bot — it is another front end, not a second implementation. Automatic MA remains blocked until
an operator captures the real authenticated form controls (see [the MA section](#plan-profile-notes)).

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
A real submission additionally requires `--attendance-name` (prompted when absent), which must be
your full name in ALL CAPS exactly as it appears in the form's MyInfo name field; dry-run,
discovery and preflight need no name. Before login and again immediately before Submit the
CLI re-reads the public Viewable worksheet for your name, department and date: an unavailable
source blocks the run, and an existing same-day record prevents a new attempt and is reported
as already recorded.

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

Private-chat commands: `/start` and `/help` (the same menu, which registers `/today` and offers
the inline `Open Today` button), `/name` (enter and confirm your ALL-CAPS MyInfo name),
`/schedule` (open the planner Mini App at `#plan`), `/time` (open Mini App
settings at `#settings`), `/today` (today's status, or open the Mini App when it is live),
`/attend` (real submission; `override` argument for an intentional
resubmission review), `/dry_run` (fill and verify, no submission), `/cancel` (cancel current
interaction/run). OTP replies must answer the exact current OTP prompt from the owning user;
stale replies are rejected.

`/name` prompts `Enter your full name in ALL CAPS, exactly as it appears in the attendance form's
MyInfo name field.` The value is trimmed, collapsed and uppercased, then shown with your provisioned
department for a confirmation reply before it is saved. Unconfirmed or changed names disable
automatic execution until reconfirmed; an unconfirmed name never submits.

**Warning:** scheduled runs and `/attend` perform real submissions — they are not test modes. Use `/dry_run` to test Telegram without submitting. Keep schedule times strictly before 09:00
and be available for prompts.

Scheduled reminder notices: a reminder is delivered up to five minutes before the auto time and
is retried on each scheduler tick until it is delivered or the auto time passes, so a failed send
never consumes it. Only the latest successfully delivered reminder (or the morning prompt) carries
live decision buttons for its revision; once a newer message owns them, an older message's buttons
are rejected as stale. The morning prompt shows the owner-only record timestamp, status and bounded
relevant details, with positive warnings retained, and never another owner's data. After a restart,
prior-day decisions still awaiting/ready (or in the bounded verification hold) are closed once as
`missed` without replaying anything; running attempts and confirmed/unknown outcomes are never
replayed or altered.

## Telegram Mini App planner

Set `MINIAPP_PUBLIC_URL` to the bare assigned HTTPS origin (no path, query, fragment or
credentials) and start the bot as usual. It serves the Mini App on `127.0.0.1:8766`
(`MINIAPP_PORT`) inside the Telegram process; the OTP receiver stays independent on 8765.
When `MINIAPP_PUBLIC_URL` is blank the planner is disabled and `/attend`, `/dry_run` and
`/cancel` still work. Open it from the `Open Planner` chat menu button, the inline
`Open Today` button, or `/schedule` / `/time`; the active tab lives in the URL
fragment (`#today`, `#plan`, `#settings`) and no state is kept in browser storage.

When the Mini App server is not running, the chat menu button is reset to the command list, and
`/schedule` and `/time` (and their command-menu labels) report the Mini App as unavailable in text
instead of offering a WebApp button, while `/today` returns today's status as text. The morning
keyboard omits **Change Today** rather than holding a day whose editor cannot be reached, so an
unavailable editor never leaves an unexpected hold. When the Mini App is live, **Change Today**
deep-links to `#today?edit=1`, which opens the Today editor after the server-side hold. Native
manual OTP and `/cancel` work whether or not the Mini App is up.

Each API request carries the `initData` the app was launched with in
`Authorization: tma ...`; the server validates its HMAC with the bot token, requires an
`auth_date` no more than 30 seconds in the future and at most 60 minutes old, reloads the
allowlist and derives the owner id only from the verified payload. Unsigned, tampered, expired or
non-allowlisted requests get `401`/`403`. Mutations are JSON, same-origin, at most 16 KiB,
strictly field-checked and bounded to 60 requests per minute per owner. Tokens and other
secrets never appear in URLs or logs, and personal responses are `no-store`.

- **Today** shows the full date, your confirmed name, the planned Present (IS)/WFH/MA
  profile, the source check time with a warning that source updates may lag, and the automatic
  action and time. Change Today is primary; Submit Now and Skip Today are secondary. An
  existing same-day record replaces the pending card with its timestamp and status and
  states exactly `Automatic submission is suppressed.`; **Keep existing** is the safe
  primary action, and the secondary **Submit different attendance** opens an explicit
  one-time review that can only append a new form entry, never edit or replace the
  existing one. While a run is active the card shows the waiting stage
  (browser slot, source checks, login, OTP, form, submitting) and cancellation limits.
  Known pre-submit failures offer **Retry submission** with fresh record checks and OTP;
  `unknown` stays distinct and never automatically retries. The Today tab polls every
  10 seconds (paused while the page is hidden, a dialog is open, an input/select/text field
  is focused or a mutation is in flight); **Refresh records** forces a fresh source check.
  The Plan tab is not live-polled: it is refetched at the Singapore horizon rollover
  (and on an explicit save or conflict re-review) instead.
- **Plan** covers exactly the next 14 Singapore dates. Missing days are `Not planned` and
  an explicit skip shows as `Skip`; they are different. Each day carries a readiness label;
  a saved MA day still reads `Unavailable` and stays a draft, never executable here.
  New horizon dates are never auto-filled. Edits stay unsaved until an explicit Save Plan,
  and the unsaved-change count is shown. Bulk actions (Fill Weekdays with Present (IS),
  Select Multiple, Copy Previous Week with a preview) never overwrite a submitted outcome.
  Fewer than three future planned weekdays triggers one planner prompt per day alongside
  the normal message. Closing the app does not stop server schedules; the app never
  executes attendance itself.
- **Settings** shows the confirmed uppercase name and department, the auto-mode switch
  (enabling it requires accepting that **silence executes the saved plan** at the auto
  time), the prompt time (default `08:00`), auto time (default `08:30`) and the 09:00
  deadline in Singapore time, and the phone state. The phone is labelled `Configured`, not
  `Online`, because configuration alone does not prove delivery. Pause sets `enabled=false`
  without deleting planned dates; the accepted silence policy is only re-asked when turning
  automatic on from off, so changing times or saving while on does not drop it.

Editing today first pauses fallback with a server-side hold before the editor opens; closing
the editor without saving leaves fallback held rather than silently restoring it. **Restore
original** restores the prior profile and resumes automatic fallback only when automatic mode
is still enabled and the restored profile is complete — with automatic mode paused, the
restored day stays held and disabled. `/cancel` owns your queued and running scheduled work
even before an OTP prompt; after submitting starts it cannot promise a clean stop.
Saving a changed All-CAPS name disables automatic mode and re-holds an awaiting/ready day,
but it preserves a deliberate Today hold you made yourself instead of overwriting it. Re-saving
the same already-confirmed normalised name is a no-op that keeps the retained positive source
evidence and today's decision, so it never re-triggers onboarding.
A dirty editor warns before the app closes, and a pending additional-submission review holds the
closing guard too; drafts live only in memory, and a failed save stays unsaved. Passive refresh
preserves focused controls and never rebases drafts. A stale revision or changed date horizon
requires explicit re-review; an out-of-window draft must be copied or explicitly discarded,
never silently dropped. While a dialog is open the rest of the app is inert and focus stays in
the sheet; safe-area insets are applied once and the theme follows Telegram's colour scheme
with contrast-checked navigation and skip-link colours in both light and dark.

### Plan profile notes

Present (IS) is stored internally as `normal` and reuses `NORMAL_ANSWERS`. WFH reuses the
existing full-day `WFH_ANSWERS`, including the Both AM & PM answer and the manager-approval
declaration. MA is selectable but **automatic MA is blocked**: no authenticated capture of
the real form controls exists, so its exact answer labels, conditional reveal order and
allowed AM/PM/other-half statuses are not known. The planner accepts only an MA `period` of
`am`/`pm`/`both` plus a 24-hour `HH:MM`/`HHMM` timing, rejects every other-half option, and
never reuses MC answers or invents declarations. Saving a `period`/`timing` stores only a
draft: the day still reads `Unavailable` and has no executable profile. An MA day stays
held/incomplete and never falls back automatically until an operator, using their own
credentials and OTP, performs an authenticated form discovery that records those
labels/ordering/statuses and verifies every branch. Existing manual MC submission via the
CLI/Telegram is unchanged and is not part of the planner.

## Owner records and source checks

Before every real submission the runner reads the public Viewable worksheet
(`/spreadsheets/d/1rAtRXT3GK2EQsdl1opMQpiTYfZJ-mCvjkNES8hz1vTQ/export?format=csv&gid=6355673`)
and matches the normalized full `[Myinfo] Name` plus your provisioned department — exact,
case-insensitive-after-uppercasing, with no fuzzy, substring or Telegram-display-name
matching. Timestamps are interpreted as month/day/year `HH:mm` in Singapore time; day-first
is never auto-detected. The UI always shows `Checked at` and warns that source updates may
lag, and an unavailable source is a distinct state, never empty results.

A matching row whose timestamp is malformed is ignored only when its written year
unambiguously differs from the requested year. A same-year malformed match fails closed with
`Record date format needs correction` and blocks the run; correct the source rather than
bypassing it with a heuristic. Verify the configured worksheet name/gid association before
installation with `uv run python` calling `SubmissionRecords().verify_source()` (it imports
only `mdcattendance.records`, so no `.env` credentials are loaded); install only when it
succeeds. A `source_mismatch` must be fixed at the source, never by selecting a different tab.

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
for phone access, without an owned domain, VPN or inbound firewall opening. The
assigned HTTPS domain now reaches the Mini App gateway on 8766, and that gateway
forwards only the exact `/otp` and `/otp/pending` routes to the receiver on 8765 —
there is no arbitrary proxy target or path, and the same owner bearer auth still
applies. The gateway starts only when `MINIAPP_PUBLIC_URL` is set, so tunneled
phone OTP requires the Mini App to be enabled; manual OTP works regardless.

Automatic delivery is announced while it is awaited. If no delivery arrives within
60 seconds of the prompt, the bot sends one manual-fallback warning bound to the
current prompt. A configured phone or token is not proof the phone is online, so
manual OTP remains available; disabling or removing the token suspends automatic
execution with a warning while manual submission continues.

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

SQLite state lives in `STATE_DIR` (default `state/`, mode 0700) and holds the attempts journal, the
planner's attendance settings and plans, daily decisions and per-owner observations, and the process
lock. Point every CLI and Telegram client at the same directory; only one browser run is active at a
time. Never reset or replace this journal to bypass duplicate protection.

Schema upgrades are idempotent transactions run under the existing startup process lock. They add the
planner tables and migrate legacy data in place; the legacy journal tables are retained unchanged as
immutable historical evidence and are never replayed or used as an active scheduling source. Never
hand-edit or drop those tables to clear a block.

A run ends `confirmed`, `failed` (known pre-submit failure), or `unknown` (interrupted or unconfirmed — check the real form record; never auto-retried).
Confirmed and unknown attempts are journalled in SQLite and block ordinary duplicates for the same account, form and date; submission is not exactly-once.
Resubmission after checking the actual form record is an explicit exception: CLI `--override` (real submissions only) or `/attend override`. Each override opens a review that shows the journalled attempts, the fresh source evidence and the exact answers, and requires a typed `yes`; confirmation authorises one attempt only, and changed records or answers require a new review. There is no broad force/override boolean at any real submission caller: the confirmation carries the reviewed attempt IDs, date, canonical answer hash and records digest and is consumed once. The scheduler never overrides.
A run also ends `recorded` (the source already shows your entry) or `blocked` (source unavailable, name/department missing, deadline passed) — neither creates a confirmed bot attempt.
Unknown outcomes are never auto-retried, and a new same-day source row during an OTP wait aborts before the Submit click.

## Known gaps (open)

Two P1 issues remain; MA mapping and target acceptance are also pending.

- The scheduler's automatic phone-OTP **readiness** check for Telegram owners still requires
  the CLI-only `OTP_HTTP_TOKEN` in addition to the owner's `users.json` `otp_token` and enabled
  receiver, so a Telegram run can be held `otp_unavailable` even when its owner token and
  receiver are available. Per-owner receiver bearer authentication itself is intact; only
  the readiness predicate is wrong. Use `/attend` or `/dry_run` manually in the meantime.
- A decision-less CLI run or a direct `/attend` does not retain prior positive source evidence when a
  later export omits the row, so a transient omission can weaken the block/report for that path. The
  planner path retains the evidence.
- The MA form mapping is still unverified: exact labels, reveal order and allowed statuses are unknown,
  so an MA day is never executable (see [the MA section](#plan-profile-notes)).
- No real Telegram client, phone OTP, Singpass login, worksheet, authenticated VPS, dry-run or real
  submission acceptance has been performed, and no deployment has been enabled.

## Deployment

For Ubuntu 24.04 VPS setup with systemd and the required target dry-run, measurement and authorised-submission gate, see [DEPLOYMENT.md](DEPLOYMENT.md).
Do not enable scheduled attendance until that gate passes; local checks do not verify target authentication or submission.
