# Headless VPS deployment

Supported target: Ubuntu 24.04 LTS, Python 3.12 and Playwright's bundled Chromium.
Use one virtual environment, one unprivileged bot service and Telegram long
polling. No inbound firewall port, desktop, Xvfb, VNC or browser pool is needed.
HTTP OTP is off by default; optional phone forwarding uses an authenticated
loopback receiver and an outbound ngrok tunnel. The optional Telegram Mini App
planner is served in the same bot process on `127.0.0.1:8766`; the assigned HTTPS
domain forwards to that gateway, and only its exact `/otp` and `/otp/pending`
routes proxy to the receiver on 8765. Outbound HTTPS to Telegram,
Singpass and FormSG must work. Scheduling is
**assisted submission**, not unattended attendance: remain available for OTP and
MC clinic/timing prompts. Times use `Asia/Singapore`; the attendance form requires
submission **before 09:00**, so choose a deterministic earlier time such as 08:30.

The commands below are an operator procedure, not evidence of a verified VPS.
Local checks do not establish that Singpass accepts your VPS. Do not enable
scheduled attendance until the target authentication/fill/confirmation gate below
has passed. Never disable Chromium's sandbox to make the gate pass.

## Install one environment

Start from a clean copy of the completed branch (including its regenerated
`uv.lock`) at `/opt/mdcattendance`. Do not copy local `.env`, credentials, state,
browser dumps or legacy schedules into it. Install `uv` using its official
installation instructions, then make its executable available on the operator's
PATH. As the administering user:

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv ca-certificates
sudo useradd --system --create-home --home-dir /var/lib/mdcattendance --shell /usr/sbin/nologin mdcattendance
sudo install -d -o mdcattendance -g mdcattendance -m 0700 /var/lib/mdcattendance
sudo install -d -o root -g mdcattendance -m 0750 /etc/mdcattendance
cd /opt/mdcattendance
uv sync --frozen --no-dev --python /usr/bin/python3
sudo /opt/mdcattendance/.venv/bin/playwright install-deps chromium
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/mdcattendance/browsers /opt/mdcattendance/.venv/bin/playwright install chromium
sudo chown -R root:mdcattendance /opt/mdcattendance
sudo chmod -R g+rX,o-rwx /opt/mdcattendance
sudo install -o mdcattendance -g mdcattendance -m 0600 .env.example /etc/mdcattendance/env
```

Use bundled Chromium, not an arbitrary distro executable. After dependency
upgrades, repeat browser installation and the gate. The application directory
and environment must stay administrator-owned and read-only to the service.

## Provision secrets locally

Edit `/etc/mdcattendance/env` with `sudoedit`; retain ownership and mode 0600.
Set the bot token, `STATE_DIR=/var/lib/mdcattendance`,
`TELEGRAM_USERS_PATH=/etc/mdcattendance/users.json`, `HEADLESS=true`,
`ATTENDANCE_DEADLINE=09:00`, `RUN_TIMEOUT=600`, `OTP_TIMEOUT=180`,
`RETENTION_DAYS=90` and `DIAGNOSTIC_TTL_HOURS=24`. Set `MINIAPP_PUBLIC_URL` to the
bare assigned HTTPS origin (no path, query, fragment or credentials) to enable the
Mini App planner; leave it blank to keep the planner off. `MINIAPP_PORT=8766` is the
loopback gateway bound only on `127.0.0.1`; the OTP receiver keeps its own
`OTP_HTTP_ENABLED`/`OTP_HTTP_PORT=8765` settings. Leave
`CHROMIUM_EXECUTABLE_PATH` unset to use the bundled browser. CLI authenticated
runs additionally need `SINGPASS_ID` and `SINGPASS_PASSWORD` in this restricted
file. Telegram uses the private users file instead.

Provision the users file locally from `users.example.json`, not via chat:

```sh
sudo install -o mdcattendance -g mdcattendance -m 0600 /opt/mdcattendance/users.example.json /etc/mdcattendance/users.json
sudoedit /etc/mdcattendance/users.json
sudo chmod 0600 /etc/mdcattendance/env /etc/mdcattendance/users.json
```

Replace example records with only authorized numeric Telegram user IDs and their
credentials/department. Never send passwords to the bot or put secrets in command
arguments, shell history, logs or screenshots. Only allowlisted users in private
chats can interact. OTP must be six ASCII digits and a reply to the current
owning user's OTP prompt; stale replies are rejected. Consumed OTP messages are
deleted best-effort, not a guarantee of erasure from Telegram. Rotate leaked
credentials/tokens immediately.

## Optional phone OTP over free ngrok

This is operator-provisioned, not credential onboarding. Keep manual OTP if you do
not need forwarding; old unauthenticated forwarding endpoints are unsupported.
The receiver always binds `127.0.0.1`, never a public interface. Do not open an
inbound firewall port. No owned domain or VPN is required.

### Provision three different secrets

Generate each receiver token locally:

```sh
/opt/mdcattendance/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Use a private terminal without recording/capture; copy the result directly into
restricted configuration and the intended phone's protected automation settings.
Never put actual secrets in command arguments, URLs, shell history, chat, logs or
screenshots. Each token must be unique, 32–128 ASCII URL-safe characters
(`A-Za-z0-9_-`).

1. For Telegram, add `"otp_token": "<generated owner token>"` alongside each
   participating owner's existing credentials/department in
   `/etc/mdcattendance/users.json`. Blank/omitted tokens keep that owner manual.
   The users file is reloaded on every HTTP request: removal/rotation immediately
   revokes the old token. Keep ownership/mode from the provisioning section.
2. For CLI, put a different generated token in `OTP_HTTP_TOKEN` in the restricted
   env file. Telegram does not use this variable.
3. Create a free ngrok account, obtain its **agent authtoken** and its automatically
   assigned stable development domain from the dashboard. This authtoken belongs
   only in the ngrok config; it is not a receiver bearer token or Telegram bot token.

Set `OTP_HTTP_ENABLED=true` and `OTP_HTTP_PORT=8765` in
`/etc/mdcattendance/env`, then restart the bot. `OTP_HTTP_ENABLED` alone starts
the loopback receiver: Telegram starts it even before any owner has a token, and
the restricted users file is re-read on every request, so owners provisioned or
rotated later are picked up without another restart. An owner whose token is
blank/omitted stays manual and gets `401` for phone bearer requests while the
receiver keeps serving every other owner. `OTP_HTTP_TOKEN` is the CLI-only
bearer: Telegram ignores it, it never gates or disables the Telegram receiver,
and setting it does not change owner behavior. CLI env enablement races terminal
and HTTP OTP; `--http-otp` enables HTTP-only OTP, with profile/MC prompts still in
the terminal. For example, append `--http-otp` to the target dry-run command below.
Preflight neither binds nor checks this receiver/token.

The bot and a simultaneous CLI receiver cannot bind the same port. Prefer stopping
the bot for CLI gates. If both must be running, override CLI `OTP_HTTP_PORT=8767`
(or another free loopback port — never 8765 or the 8766 Mini App gateway) in its
invocation and deliberately point the phone/tunnel at that receiver's port;
the CLI needs its own token. This does not bypass the shared one-browser process lock.

### Install and configure the optional tunnel

Install the Linux amd64 standalone agent from the
[official download page](https://ngrok.com/download/linux); for other architectures,
choose its matching official build. As the administrator, run this POSIX-sh block.
Fish users must first run `sh`, then paste the block into that shell (and `exit`
afterwards). The private temporary workspace is removed on exit:

```sh
sh <<'SH'
set -eu
umask 077
workspace=$(mktemp -d)
trap 'rm -rf -- "$workspace"' EXIT
trap 'exit 1' HUP INT TERM
curl --fail --location https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz -o "$workspace/ngrok.tgz"
tar -xzf "$workspace/ngrok.tgz" -C "$workspace" ngrok
sudo install -o root -g root -m 0755 "$workspace/ngrok" /usr/local/bin/ngrok
SH
sudo install -o root -g mdcattendance -m 0640 /opt/mdcattendance/deploy/ngrok.yml.example /etc/mdcattendance/ngrok.yml
sudoedit /etc/mdcattendance/ngrok.yml
```

Replace `agent.authtoken` with the account's agent secret and the endpoint `url`
with `https://YOUR-ASSIGNED.ngrok-free.app` using your actual assigned domain.
Retain version 3, endpoint name `attendance`, and upstream
`http://127.0.0.1:8766` (the Mini App gateway; the OTP receiver on 8765 is reached
only through the gateway's exact proxied routes). Retain `agent.inspect_db_size: -1` and
`agent.web_addr: false` to disable local traffic storage and inspection UI.
Separately keep cloud **Full Capture OFF** in the ngrok dashboard: local settings
do not disable cloud Full Capture. ngrok terminates TLS and can access Mini App
traffic and the proxied OTP routes; use this provider only if you accept that trust
boundary. Do not enable capture, request dumps or verbose request logging on the
phone either.

```sh
sudo -u mdcattendance /usr/local/bin/ngrok config check --config /etc/mdcattendance/ngrok.yml
sudo install -o root -g root -m 0644 /opt/mdcattendance/deploy/mdcattendance-ngrok.service /etc/systemd/system/mdcattendance-ngrok.service
sudo systemctl daemon-reload
sudo systemctl start mdcattendance-ngrok.service
```

Start the configured bot manually for the dry-run gate; only enable either service
at boot after the target gates pass (`sudo systemctl enable --now
mdcattendance-ngrok.service` for the optional tunnel). The tunnel unit uses the same unprivileged
user and `ngrok start attendance --config /etc/mdcattendance/ngrok.yml`. Neither service
starts or requires the other: starting the tunnel for a CLI gate does not start
the bot/scheduler, and manual OTP still works when ngrok is down. Ordering only
places the tunnel after the bot when both are independently started.
No application download or automatic tunnel activation occurs.

### Public gateway routes and auth

The assigned domain reaches the loopback gateway, which serves the Mini App and,
for the two exact paths `/otp` and `/otp/pending`, forwards a bounded method, body
and `Authorization` header to the independent `127.0.0.1:8765` receiver. It exposes
no arbitrary proxy target or path and performs no auth translation; those routes
keep the owner bearer contract below. Every other path is Mini App traffic.
The gateway binds only when `MINIAPP_PUBLIC_URL` is set: with it blank nothing
listens on 8766 and tunneled phone OTP is unavailable, though manual OTP and the
CLI/`/attend` paths still work. The gateway never enables or starts the receiver
— that stays governed solely by `OTP_HTTP_ENABLED`. When the separate receiver is
off, its exact `/otp` and `/otp/pending` routes return `503` unavailable instead
of proxying or falling back to manual.

Mini App API requests must carry the Telegram `initData` the app was launched with
in `Authorization: tma ...`. The server validates its HMAC with the bot token using
a constant-time comparison, requires an `auth_date` no more than 30 seconds in the
future and at most 60 minutes old, reloads the allowlist, and derives the owner id only from
the verified payload. Invalid or expired requests get `401`; non-allowlisted get
`403`. Tokens, `initData` and other secrets must never appear in URLs, access logs or
error bodies, and personal responses are `no-store`.

The [free-plan limits](https://ngrok.com/docs/pricing-limits/free-plan-limits)
provide one assigned stable dev domain with HTTPS, 20,000 HTTP requests/month,
1 GB outbound/month and no endpoint timeout. Limits can change; recheck before
deployment. Each GET/POST counts: poll only during an active OTP window, for
example once every five seconds with a hard stop at `OTP_TIMEOUT`, not all day or
in an aggressive loop. See the
[v3 agent configuration](https://ngrok.com/docs/gateway/agent/config/v3) and
[traffic inspection privacy controls](https://ngrok.com/docs/obs/traffic-inspection).

### Phone automation: owner-bound GET, then POST

Configure the phone's HTTP action tool with the assigned HTTPS base URL and its
owner's bearer token in protected local settings. Grant only the SMS access needed
to recognize the intended Singpass message; no phone platform-specific automation
is assumed here. First test manually against a dry-run waiting for OTP.
For both requests set:

```text
Authorization: Bearer <owner receiver token>
ngrok-skip-browser-warning: 1
```

The ngrok header only bypasses its browser warning; it is not authentication.

1. While that owner's current login is waiting, `GET https://YOUR-ASSIGNED.ngrok-free.app/otp/pending`.
   A `200` JSON response is `{"request_id":"<opaque ID>","expires_in":<seconds>}`.
   `409` means no current pending OTP. Save the ID only for this run/window.
2. For the six ASCII digits from the expected current SMS, send
   `POST https://YOUR-ASSIGNED.ngrok-free.app/otp` with
   `Content-Type: application/json` and exactly:

   ```json
   {"request_id":"<ID from step 1>","otp":"123456"}
   ```

   Do not include `chat_id`; the bearer identifies the owner. `202` means delivery
   accepted, not successful login or attendance submission. Dispose of the OTP/ID
   locally; do not retain request bodies.

Bind an SMS to the current login, not merely to any six digits. No early OTP
queue exists. A request ID is consumed by either manual or HTTP completion and
expires on timeout, cancellation or shutdown. A late SMS must **not** be retried
against a freshly fetched request ID. On stale ID, uncertain delivery or network
failure, stop that forwarding attempt and use the current manual prompt or cancel
and deliberately start a new login; never blindly replay.

Receiver responses: `401` missing/invalid bearer; `400` malformed JSON or wrong
schema; `409` stale/wrong-owner/no pending ID; `413` body over 1024 bytes; `415`
non-JSON or compressed body; `422` invalid six-ASCII-digit OTP; `408` body read over
five seconds; `429` global rate limit (burst 60, refill one request/second).
All replies use `Cache-Control: no-store`. Never log secrets, request IDs or bodies.
Telegram manual replies must still answer the exact current prompt; first valid
delivery wins and clears the prompt. CLI HTTP-only mode has no terminal OTP
fallback: cancel and rerun without `--http-otp` (and with env HTTP disabled if the
receiver is unavailable) to return to manual operation.

## Target gate before boot enablement

Stop the service while performing CLI gates; CLI and scheduler share a process
lock, permitting only one active browser run. These invocations use exactly the
same user, environment and bundled browser as the service.

First verify the worksheet name/gid association. This imports only
`mdcattendance.records`, whose sheet/gid are fixed module constants read over an
anonymous HTTP request: no restricted env file (`MDCATTENDANCE_ENV_FILE`), no
`.env`, no credentials and no particular working directory are involved. Run it
as the service user with the already-synced venv interpreter — not `uv run`,
which would re-resolve and sync:

```sh
sudo -u mdcattendance /opt/mdcattendance/.venv/bin/python -c 'import asyncio; from mdcattendance.records import SubmissionRecords; asyncio.run(SubmissionRecords().verify_source()); print("Viewable/gid verified")'
```

On `source_mismatch` (or any other failure), fix the source or the configured
spreadsheet id/gid; never select a different tab. Only then run the gate
invocations:

```sh
sudo systemctl stop mdcattendance.service 2>/dev/null || true
sudo -u mdcattendance env MDCATTENDANCE_ENV_FILE=/etc/mdcattendance/env PLAYWRIGHT_BROWSERS_PATH=/opt/mdcattendance/browsers /opt/mdcattendance/.venv/bin/mdcattendance --preflight
sudo -u mdcattendance env MDCATTENDANCE_ENV_FILE=/etc/mdcattendance/env PLAYWRIGHT_BROWSERS_PATH=/opt/mdcattendance/browsers /opt/mdcattendance/.venv/bin/mdcattendance --day-type normal --dry-run
```

Preflight must pass without credentials. Dry-run must really authenticate, accept
an operator-provided OTP, fill and read back the selected profile, and exit
without clicking Submit. Repeat dry-run for the profiles you will actually use:
Present (IS) (`--day-type normal`) and WFH. MA branches can be gated only after
the authenticated form capture has recorded and verified every answer label,
conditional ordering and AM/PM/other-half status; until then automatic MA stays
blocked and is never enabled. MC remains a manual, interactive path (clinic and
timing prompts), not part of the planner. Observe that no
Chromium child survives normal completion, timeout or cancellation. If Singpass
rejects the VPS or selectors change, stop here and investigate; do not add stealth
flags, guessed selectors, `--no-sandbox`, or blind retries.

Ubuntu 24.04 can restrict unprivileged user namespaces through AppArmor. Inspect
kernel denial messages (`sudo journalctl -k`) if sandbox startup fails. Obtain a
reviewed, executable-path-specific AppArmor policy permitting Chromium user
namespaces for the installed browser; repeat the gate under systemd confinement.
Do not globally disable AppArmor or its user-namespace restriction. The unit
intentionally does not restrict namespaces or set `NoNewPrivileges`; both the
host policy and sandbox must permit the real browser launch.

Only after explicit authorization for a real attendance date, run one actual
submission (omit `--dry-run`) and verify both the application's confirmed result
and the real FormSG confirmation. This is not permission to submit while
installing:

```sh
sudo -u mdcattendance env MDCATTENDANCE_ENV_FILE=/etc/mdcattendance/env PLAYWRIGHT_BROWSERS_PATH=/opt/mdcattendance/browsers /opt/mdcattendance/.venv/bin/mdcattendance --day-type normal --attendance-name 'YOUR NAME IN ALL CAPS'
```

`--attendance-name` must be your full name in ALL CAPS exactly as it appears in the
form's MyInfo name field; it is prompted when omitted, and is required only for real
submissions. The runner re-checks the public Viewable worksheet for that name,
your provisioned department and today's date before login and again immediately before
the Submit click: an unavailable source blocks the run, and an existing same-day record
suppresses the attempt and is reported as already recorded.

A submission is not exactly-once: FormSG offers no transactional idempotency
contract. The durable journal records `submitting` before the click. Interrupted
or unconfirmed submission becomes `unknown`, never an automatic retry. Known
pre-submit failures become `failed` and stay stopped until an explicit owner retry;
Mini App **Retry submission** requires fresh source checks and a new OTP.
Ordinary repeats for the same account, form/date are blocked for confirmed or uncertain attempts.
Check actual form records before deciding whether to intentionally resubmit; CLI `--override` or
Telegram `/attend override` opens an explicit review (journalled attempts, fresh
source evidence, exact answers, typed confirmation) and authorises one additional
attempt only — it is not a recovery setting or a silent bypass. Scheduler never
overrides. Nonconfirmed actual CLI submissions exit
nonzero. Restarting or editing a schedule does not replay completed/unknown work.

### Production gates before enabling schedules

Local loopback/Playwright smoke and a successful local dry-run are **not** live
acceptance. Enabling schedules (and enabling either service at boot) requires all of
the following on the actual target, and this document does not claim any of them has
passed:

- The assigned HTTPS domain launches the Mini App from real Telegram Android, iOS
  and Desktop clients, including the menu button and `#today`/`#plan`/`#settings`
  deep links and the Today/Plan/Settings flows.
- VPS authenticated dry-runs pass for Present (IS) and WFH, and for every MA branch
  — but only after its authenticated form capture is complete; until then MA stays
  blocked and no MA branch is claimed verified.
- A real phone OTP is delivered over the tunnel, and the 60-second manual-fallback
  warning appears when delivery does not arrive.
- **Open blocker (stop before automatic schedules).** `Scheduler.otp_ready` still
  requires the CLI-only `OTP_HTTP_TOKEN` in addition to an enabled receiver and
  the owner's token, so Telegram per-owner HTTP OTP cannot enable automatic mode
  on its own. Leave automatic schedules off until this is fixed; setting a CLI
  token merely to satisfy the check is not a workaround.
- **Open blocker (stop before automatic schedules).** A decision-less CLI/`/attend`
  run does not retain prior positive source evidence when a later export omits the
  row, so its duplicate guard is not durable. Do not treat it as repeat-safe; keep
  automatic schedules off until this is fixed.
- The operator records acceptance of those results before `systemctl enable` and
  before any automatic schedule runs.

## Service, scheduling and shutdown

```sh
sudo install -o root -g root -m 0644 /opt/mdcattendance/deploy/mdcattendance.service /etc/systemd/system/mdcattendance.service
sudo systemctl daemon-reload
sudo systemctl start mdcattendance.service
sudo systemctl status mdcattendance.service
sudo journalctl -u mdcattendance.service -n 50 --no-pager
```

Exercise a private-chat `/dry_run` under this unit before enabling boot. Then:

```sh
sudo systemctl enable mdcattendance.service
```

Use `/name` to enter and confirm your ALL-CAPS MyInfo name, then `/schedule` and
`/time` (or the `Open Planner` menu button and the inline `Open Today` button) to
open the Mini App Today/Plan/Settings views; plans cover the next 14 Singapore dates with no
auto-fill of new horizon dates, and enabling auto mode requires accepting that
silence executes the saved plan. Use `/attend` for manual attendance, `/dry_run`
for no-submit rehearsal and `/cancel` to cancel your pending interaction or queued/running
scheduled work, including login before an OTP prompt exists. After `submitting` begins,
cancellation cannot promise that no entry was created; preserve an uncertain outcome.
Schedules must be strictly before the deadline. Busy scheduled work can defer only
within that window; missed or skipped work is notified, not silently retried after
the deadline. Keep the bot running and be available at the selected time for
OTP/MC details.

The scheduler has one morning decision prompt and one logical reminder shortly
before the auto time per owner/day. Delivery is recorded only after Telegram
returns a message ID; failed sends retry while eligible, never after the attendance
deadline. Only the latest delivered prompt/reminder owns the live buttons; older
keyboards are stale. A user-edit pause holds automatic fallback until an explicit
save or restore; disabled auto mode keeps a complete plan held. On restart or
midnight rollover, prior-day awaiting/ready decisions and verification holds become
`missed`, with one notification attempt. Deliberate user holds and terminal outcomes
are preserved. Interrupted prepared/running attempts become `failed`; interrupted
`submitting` attempts become `unknown`. Neither is automatically replayed.

SIGTERM cancels active work and closes browser resources. `KillMode=mixed` gives
the main process a 45-second graceful stop, then kills any remaining processes
in its cgroup. An interrupted submit remains uncertain in SQLite after restart.
`Restart=on-failure` is bounded by the unit start-rate limit; investigate repeated
failures rather than resetting the limit blindly. `systemctl stop` is an
intentional stop, not a request to retry attendance.

## Measure before applying memory/task limits

Do not guess Chromium `MemoryMax`/`TasksMax` from an idle Python process. The unit
has no guessed memory cap and allows tasks until a measured cap is installed.
Measure the **whole service cgroup** on the actual VPS, including browser children.
For separate login, OTP-wait and fill observations, run a private-chat dry-run
and sample continuously in a second terminal; record phase start/end wall times
as you watch the run (never OTP values). Repeat for Present (IS)/WFH and, once its
authenticated capture is complete, each MA branch, plus cancellation.

```sh
sudo sh -c 'CG=$(systemctl show --property=ControlGroup --value mdcattendance.service); while :; do date -Is; cat "/sys/fs/cgroup$CG/memory.current" "/sys/fs/cgroup$CG/memory.peak" "/sys/fs/cgroup$CG/pids.current" "/sys/fs/cgroup$CG/pids.peak"; sleep 1; done'
```

Ubuntu 24.04 uses cgroup v2. If the kernel lacks `pids.peak`, sample
`pids.current` and record its observed maximum instead. `memory.peak` is the
cgroup lifetime peak, not an instantaneous phase measurement: restart the service
between isolated phase runs if needed and record the peak after each run. The
service must be idle before restarting. Include browser launch, real login,
OTP waiting, filling, confirmation and shutdown, and leave measured headroom for
browser updates and other VPS processes. If the host cannot fit the peak without
swap/OOM, resize it; a lower cap is not a fix.

After deciding byte/task limits from those measurements, supply the actual
numbers (the commands prompt rather than guessing):

```sh
sudo bash -c 'read -r -p "Measured memory limit in bytes including headroom: " MEMORY_LIMIT; read -r -p "Measured task limit including headroom: " TASK_LIMIT; install -d -m 0755 /etc/systemd/system/mdcattendance.service.d; printf "[Service]\nMemoryMax=%s\nTasksMax=%s\n" "$MEMORY_LIMIT" "$TASK_LIMIT" > /etc/systemd/system/mdcattendance.service.d/resources.conf'
sudo systemctl daemon-reload
sudo systemctl restart mdcattendance.service
```

Repeat the real dry-run gate after applying limits; inspect `memory.events` for
OOM and verify no browser survives shutdown. Keep measured values and phase
observations in your operator records, without secrets. No target measurements
or real authentication/submit verification are claimed by this document.

## Bounded logs, state and artifacts

The unit rate-limits journal messages. Bound journal size and age as well (this
configuration applies to the host journal, not just this unit):

```sh
sudo install -d -m 0755 /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\nRuntimeMaxUse=50M\nMaxRetentionSec=7day\n' | sudo tee /etc/systemd/journald.conf.d/mdcattendance.conf
sudo systemctl restart systemd-journald
sudo journalctl --rotate
sudo journalctl --vacuum-time=7d --vacuum-size=100M
```

Do not enable debug update dumps or log credentials/OTP/authenticated HTML.
`STATE_DIR` is mode 0700 and SQLite/lock/artifact files are private. Terminal
attempts, including old confirmed/unknown outcomes, are pruned after
`RETENTION_DAYS` only for past attendance dates. The runner refuses historical
submissions, so pruning cannot replay those dates; current-date duplicate guards
and in-progress records remain. Recovery prunes during ordinary runs. Monitor
`sudo du -sh /var/lib/mdcattendance`; never blindly delete the journal to unblock
attendance. Restrict backups and never restore an old DB over newer attempts.

Routine tracing/video/screenshots/HTML dumps are off. `--discover` is an explicit
sensitive diagnostic operation, not normal production logging. Artifacts use
`/var/lib/mdcattendance/diagnostics/form-<uuid>.html` with exclusive private writes.
The default TTL is 24 hours (`DIAGNOSTIC_TTL_HOURS`). Install host cleanup so idle
or stopped application processes cannot retain diagnostics indefinitely:

```sh
printf 'd /var/lib/mdcattendance/diagnostics 0700 mdcattendance mdcattendance mM:24h\n' | sudo tee /etc/tmpfiles.d/mdcattendance.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/mdcattendance.conf
sudo systemctl enable --now systemd-tmpfiles-clean.timer
sudo systemd-tmpfiles --clean /etc/tmpfiles.d/mdcattendance.conf
sudo find /var/lib/mdcattendance/diagnostics -type f -name 'form-*.html' -mmin +1440 -delete
```

The [systemd 255 tmpfiles age syntax](https://www.freedesktop.org/software/systemd/man/255/tmpfiles.d.html#Age)
supports modification-time-only expiry. Daily cleanup may lag
expiry by one timer interval; application cleanup and the explicit `find` command
remove expired files when invoked. Match both cleanup ages if you change the TTL;
do not remove SQLite, WAL/SHM files or the lock.
Copying a diagnostic elsewhere loses automatic expiry: chmod it 0600, restrict
its parent directory and explicitly delete it within the same TTL. Never attach
authenticated pages or credentials to bug reports.

## Idempotent SQLite planner migration

There is no JSON compatibility reader, calendar execution path or HTTP OTP
migration shim. Stop the old bot first, then start the new one once. On startup
`StateStore` performs an idempotent schema upgrade in a transaction under the
existing process lock; it is safe to rerun after an interrupted upgrade. It adds
the planner tables (`attendance_settings`, `attendance_plans`, `daily_decisions`,
`attendance_observations`) and imports future legacy `schedule_days` rows whose
status is `normal`, `wfh` or `none` into plans, where `normal`/`wfh` keep their
profile and `none` becomes an explicit `skip`. Legacy `schedule_days` rows with any
other status (MC and the rest) are preserved as immutable history and are never
silently enabled in the new planner; MC remains the existing manual CLI/Telegram
path.

A legacy `schedules.time` becomes `auto_time` only when it is later than 08:00 and
earlier than the configured deadline; otherwise 08:30 is kept and the settings are
flagged for review. Every migrated owner starts with auto mode **disabled** until
they confirm their All-CAPS MyInfo name and review the auto-fallback policy in the
Mini App Settings. Current-date dispatch outcomes are imported so an already
started/completed run cannot be replayed; the legacy journal and dispatch tables
are retained unchanged as immutable historical evidence.

Preserve the SQLite DB through upgrades and keep restricted backups of it; changing
a plan does not clear attempt history. Do not drop or hand-edit the legacy tables to
clear a block. Disable/remove old webhook/OTP forwarding services and inbound
firewall openings, and never run the old bot alongside the new one, even during
migration.
