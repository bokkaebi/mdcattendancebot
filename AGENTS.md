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
CLI profile + MC details (if needed)
  -> FormSG "Log in with Singpass"
  -> Singpass "Use password"
  -> credentials from .env
  -> OTP from terminal (default) or HTTP bridge (--http-otp)
  -> consent "I Agree"
  -> wait for FormSG SPA to settle
  -> apply ordered attendance profile
  -> submit "Submit now" (skipped by --dry-run)
```

- `attendance.py` owns exact screenshot-derived labels and ordered profiles.
  Order matters: Department -> Status -> conditionally mounted questions -> acknowledgements.
- `bot.py` owns navigation/authentication and delegates OTP/profile filling.
- `formfiller.py` is generic and strict. It targets accessible roles/names,
  retries conditionally mounted fields, and refuses to submit if any expected
  answer was not applied.
- `otp.py` defines `OtpProvider`; `CliOtpProvider` uses
  `asyncio.to_thread(input, ...)`. `server.OtpBridge` is the optional HTTP
  implementation.

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
| `src/mdcattendance/server.py` | optional FastAPI HTTP OTP bridge |
| `src/mdcattendance/config.py` | frozen env-backed `Config` |
| `src/mdcattendance/__main__.py` | argparse and async orchestration |
| `website/snapshots/*.png` | selected-answer source of truth |
| `form-page.html` | gitignored `--discover` output |

## Development Commands

```sh
uv sync
uv run playwright install chromium

uv run mdcattendance                         # prompts normal/wfh/mc
uv run mdcattendance --day-type normal
uv run mdcattendance --day-type wfh
uv run mdcattendance --day-type mc           # prompts clinic + appointment timing
uv run mdcattendance --headed --day-type mc
uv run mdcattendance --headed --dry-run --day-type normal  # fill, wait 30s, never submit
uv run mdcattendance --discover              # auth, save form-page.html, no submit
uv run mdcattendance --preflight              # no credentials/OTP submitted
uv run mdcattendance --http-otp --day-type normal

uvx ruff check src
```

With `--http-otp`, the external service calls:

```sh
curl -X POST http://127.0.0.1:8080/otp \
  -H 'Content-Type: application/json' \
  -d '{"otp":"123456"}'
```

## Code Conventions & Common Patterns

- Python 3.14, `uv`, Playwright async API, FastAPI/uvicorn only for
  `--http-otp`.
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
- `.env`, `form-page.html`, and local answer artifacts remain gitignored.

## Important Files

- `.env.example` — required credentials and optional runtime settings.
- `pyproject.toml` / `uv.lock` — dependencies, entry point, lint config.
- `src/mdcattendance/attendance.py` — exact profile values; update alongside screenshots.
- `src/mdcattendance/bot.py` — selector provenance (`VERIFIED`, `INFERRED`, `USER-REPORTED`).

## Runtime / Tooling Preferences

- Run commands through `uv run`; do not invoke the venv interpreter directly.
- Playwright's bundled Chromium uses the Ubuntu 24.04 fallback on this CachyOS
  machine. `CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium` opts into system Chromium.
- Shell is fish. Keep examples portable or explicitly fish-compatible.
- Never commit Singpass credentials, discovered authenticated HTML, or personal
  attendance answers.

## Testing & QA

Verification is behavioral and focused:

- Live preflight confirms FormSG -> Singpass -> password fields.
- A real authenticated run confirmed OTP submission, `I Agree`, and redirect
  back to authenticated FormSG.
- The saved FormSG DOM confirmed `radiogroup` semantics; screenshot profiles
  confirm exact selected values.
- CLI profile/MC prompts and ruff have been smoke-checked.

The remaining end-to-end proof is a real profile fill + submit. Keep any further
verification narrowly targeted; do not add broad test scaffolding for this
personal automation script.
