/*
 * Attendance planner UI. Plain ES module, native controls, no framework.
 *
 * Security/UX rules enforced here:
 * - Authorization comes only from the raw Telegram.WebApp.initData; no storage,
 *   no URL, no client-supplied uid. Missing initData = instruct launch via Telegram.
 * - All dynamic text is written with textContent; there is no raw HTML rendering.
 * - Drafts live in memory. A failed API call never claims success; a 409 keeps the
 *   draft and offers an explicit refresh/re-review instead of overwriting.
 * - Closing the Mini App with unsaved changes or a pending review asks to confirm.
 */

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const initData = tg && typeof tg.initData === "string" ? tg.initData : "";

const VIEW_IDS = { today: "view-today", plan: "view-plan", settings: "view-settings" };
const PROFILES = ["normal", "wfh", "ma", "skip"];
const PROFILE_LABEL = {
  normal: "Present (IS)",
  wfh: "WFH",
  ma: "MA",
  skip: "Skip",
};
const PROFILE_SHORT = { normal: "Present", wfh: "WFH", ma: "MA", skip: "Skip" };
const EDITABLE_FINAL = ["running", "recorded", "skipped", "confirmed", "unknown", "missed"];
// Failed pre-submit runs remain editable only through an explicit owner action;
// the scheduler never resumes them automatically.
// Terminal day states excluded from bulk fill/copy and shown read-only in the plan:
const TERMINAL_DAY_STATES = new Set(["recorded", "confirmed", "unknown", "skipped", "running", "missed"]);
// Attempt outcomes that make today immutable even before a decision state lands.
const TERMINAL_OUTCOMES = new Set(["confirmed", "unknown"]);
const TERMINAL_TEXT = {
  recorded: "Recorded at the attendance source",
  confirmed: "Confirmed submitted",
  unknown: "Unknown outcome (may have been submitted)",
  skipped: "Skipped",
  running: "Submission in progress",
  missed: "Missed before the deadline",
};
const STAGE_TEXT = {
  waiting_for_browser: "Waiting for the attendance browser.",
  checking_records: "Checking the attendance source.",
  login: "Signing in with Singpass.",
  otp: "Waiting for the phone OTP prompt in Telegram.",
  form: "Filling the attendance form.",
  submitting: "Submitting the attendance form.",
};
const LOCAL_OUTCOME = {
  confirmed: "FormSG confirmed this submission; the sheet may lag.",
  unknown: "Unknown outcome. Check the source before retrying; it is not a failure.",
  failed: "Failed before submission. A retry is a new explicit decision.",
  submitting: "Submission is in progress.",
};
const SOURCE_ERRORS = {
  record_date_format:
    "Record date format needs correction at the source. No bypass is applied; the operator must correct the worksheet.",
  invalid_headers: "The attendance source headers could not be verified.",
  source_mismatch: "The attendance source did not match the expected worksheet.",
  network_error: "The attendance source could not be reached.",
  http_error: "The attendance source returned an error.",
  response_too_large: "The attendance source response was too large.",
  unsafe_redirect: "The attendance source redirect was rejected.",
  invalid_response: "The attendance source returned an unusable response.",
  invalid_csv: "The attendance source data could not be parsed.",
  invalid_encoding: "The attendance source encoding was invalid.",
  invalid_row_width: "The attendance source contains malformed rows.",
};

const state = {
  view: "today",
  me: null,
  plan: null,
  today: null,
  server: new Map(),
  planDraft: new Map(),
  selection: new Set(),
  multi: false,
  edit: null,
  review: null,
  reviewChoice: null,
  reviewError: null,
  planConflict: null,
  settingsConflict: null,
  settingsDraft: null,
  settingsError: null,
  showNameForm: false,
  nameInput: "",
  nameError: null,
  fatal: null,
  loadError: null,
};

let pendingEdit = false;
let sheetState = null;
let sheetReturnKey = null;
let activationFocusKey = null;
let idCounter = 0;

/* Bounded visible-page refresh. Plain interval, one request in flight, paused while
 * the page is hidden, a sheet is open or a mutation is running; GET /api/today reuses
 * the server's cached check, and only the explicit "Refresh records" button forces a
 * fresh source read (?refresh=1). */
const REFRESH_MS = 10000;
let refreshTimer = null;
let refreshInflight = false;
let backgroundStopped = false;
let mutationInflight = 0;
let mutationVersion = 0;

const uid = () => "ui-" + ++idCounter;

/* ------------------------------------------------------------------ DOM utils */

function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "value") node.value = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function card(...kids) {
  return h("section", { class: "card" }, ...kids);
}

function tag(text, kind) {
  return h("span", { class: "tag" + (kind ? " " + kind : ""), text });
}

function banner(kind, ...kids) {
  return h("div", { class: "banner " + kind }, ...kids);
}

function viewEl(name) {
  return document.getElementById(VIEW_IDS[name]);
}

function loading() {
  return h("p", { class: "muted", text: "Loading…" });
}

function announce(message) {
  document.getElementById("live").textContent = message;
}

function showError(error) {
  const message = error && error.message ? error.message : String(error);
  const box = document.getElementById("error-banner");
  if (box) {
    // Re-showing the same visible error must not re-announce it via #alert twice.
    if (box.textContent === message && !box.hidden) return;
    box.textContent = message;
    box.hidden = false;
  }
  document.getElementById("alert").textContent = message;
}

function clearError() {
  const box = document.getElementById("error-banner");
  if (box) {
    box.hidden = true;
    box.textContent = "";
  }
  document.getElementById("alert").textContent = "";
}

function busy(button, on) {
  if (!button) return;
  if (on) {
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
  } else {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

/* -------------------------------------------------------------------- formats */

function fmtDate(iso) {
  const d = new Date(iso + "T00:00:00+08:00");
  return new Intl.DateTimeFormat("en-SG", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "Asia/Singapore",
  }).format(d);
}

function fmtShort(iso) {
  const d = new Date(iso + "T00:00:00+08:00");
  return new Intl.DateTimeFormat("en-SG", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "Asia/Singapore",
  }).format(d);
}

function fmtIso(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-SG", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Singapore",
  }).format(d);
}

function fmtClock(value) {
  if (!value) return "";
  if (/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return value;
  return fmtIso(value);
}

/* The ISO date is authoritative (Asia/Singapore calendar day). Parsing it as an
 * instant and reading host-local getDate()/getDay() shows the previous day in any
 * timezone west of +08:00, so the number and weekday come from the string parts. */
function dayNumber(iso) {
  return Number(String(iso).slice(8, 10));
}

function weekdayIndex(iso) {
  const [year, month, day] = String(iso).split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}

function weekday(iso) {
  const index = weekdayIndex(iso);
  return index >= 1 && index <= 5;
}

/* A day is locked for editing when the server refuses the edit or a terminal outcome
 * already exists. Today's terminal decision state / attempt outcome comes from the
 * today view. Later dates have no decision row, so they are never edit-locked. */
function lockedDay(date) {
  if (!state.plan) return false;
  if (date === state.plan.today && state.today) {
    const decision = state.today.decision || {};
    if (TERMINAL_DAY_STATES.has(decision.state)) return true;
    if (TERMINAL_OUTCOMES.has(state.today.local_outcome)) return true;
  }
  return false;
}

function serverProfile(date) {
  const server = state.server.get(date);
  return server ? server.profile : null;
}

// Future Skip entries are editable predictions, not submitted outcomes.
function bulkExcluded(date) {
  return lockedDay(date);
}

function lockedReason(date) {
  if (date === state.plan.today && state.today) {
    const decision = state.today.decision || {};
    if (TERMINAL_TEXT[decision.state]) return TERMINAL_TEXT[decision.state];
    if (TERMINAL_TEXT[state.today.local_outcome]) return TERMINAL_TEXT[state.today.local_outcome];
  }
  if (serverProfile(date) === "skip") return "Skipped";
  return "Locked";
}

/* Editable weekday targets: all 14 days, or just the selected dates. */
function fillTargets() {
  if (!state.plan) return [];
  const source = state.selection.size ? Array.from(state.selection) : state.plan.days.map((day) => day.date);
  return source.filter((date) => weekday(date) && !bulkExcluded(date));
}

/* ---------------------------------------------------------------------- API */

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const STATUS_TEXT = {
  401: "Your Telegram session is not authorised. Open the planner from Telegram again.",
  403: "This account is not on the attendance allowlist.",
  404: "That action is not available.",
  405: "That action is not allowed.",
  408: "The request timed out. Try again.",
  409: "The state changed. Review the latest information before continuing.",
  422: "That input was not accepted. Correct it and try again.",
  429: "Too many requests. Wait a moment and try again.",
  503: "The planner is temporarily unavailable.",
};

function statusMessage(status, code) {
  if (code === "revision_conflict") {
    return "This view is out of date. Re-review the latest information.";
  }
  if (code === "blocked") {
    return "The server refused this action for safety. Review the latest information.";
  }
  if (code === "rate_limited") return "Too many requests. Wait a moment and try again.";
  return STATUS_TEXT[status] || "The request failed (" + status + ").";
}

async function api(path, options) {
  const opts = options || {};
  const mutating = (opts.method || "GET") !== "GET";
  if (mutating) {
    mutationInflight += 1;
    mutationVersion += 1;
  }
  try {
    return await apiRequest(path, opts);
  } finally {
    if (mutating) mutationInflight -= 1;
  }
}

async function apiRequest(path, opts) {
  if (!initData) {
    throw new ApiError(0, "unauthenticated", "Open this planner from Telegram.");
  }
  let response;
  try {
    response = await fetch(path, {
      method: opts.method || "GET",
      headers: {
        Authorization: "tma " + initData,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    throw new ApiError(0, "network_error", "No connection. Your draft is kept in memory.");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const code = payload && payload.code ? payload.code : "http_" + response.status;
    const message = payload && payload.message ? payload.message : statusMessage(response.status, code);
    throw new ApiError(response.status, code, message);
  }
  return payload;
}

/* ------------------------------------------------------------------- guards */

function settingsDraft() {
  if (!state.me) return null;
  if (state.settingsDraft) return state.settingsDraft;
  const s = state.me.settings;
  return {
    enabled: s.enabled,
    prompt_time: s.prompt_time,
    auto_time: s.auto_time,
    policy: false,
  };
}

function settingsDirty() {
  if (!state.settingsDraft || !state.me) return false;
  const s = state.me.settings;
  const d = state.settingsDraft;
  return (
    d.enabled !== s.enabled || d.prompt_time !== s.prompt_time || d.auto_time !== s.auto_time
  );
}

function canon(details) {
  const out = {};
  for (const key of Object.keys(details || {}).sort()) {
    if (details[key] !== "" && details[key] !== undefined && details[key] !== null) {
      out[key] = details[key];
    }
  }
  return JSON.stringify(out);
}

function dirtyDates() {
  const result = [];
  for (const [date, value] of state.planDraft) {
    const server = state.server.get(date);
    const left = JSON.stringify({ profile: value.profile, details: JSON.parse(canon(value.details)) });
    const right = server
      ? JSON.stringify({ profile: server.profile, details: JSON.parse(canon(server.details)) })
      : JSON.stringify({ profile: null, details: {} });
    if (left !== right) result.push(date);
  }
  return result.sort();
}

function hasUnsaved() {
  if (dirtyDates().length > 0) return true;
  if (state.edit && state.edit.dirty) return true;
  if (state.review !== null) return true;
  if (settingsDirty()) return true;
  if (state.nameInput.trim() !== "") return true;
  return false;
}

function updateClosingGuard() {
  const dirty = hasUnsaved();
  if (tg && typeof tg.enableClosingConfirmation === "function") {
    if (dirty) {
      if (typeof tg.isClosingConfirmationEnabled === "function" && !tg.isClosingConfirmationEnabled()) {
        tg.enableClosingConfirmation();
      } else if (typeof tg.isClosingConfirmationEnabled !== "function") {
        tg.enableClosingConfirmation();
      }
    } else if (typeof tg.disableClosingConfirmation === "function") {
      tg.disableClosingConfirmation();
    }
  }
}

window.addEventListener("beforeunload", (event) => {
  if (!hasUnsaved()) return undefined;
  event.preventDefault();
  event.returnValue = "";
  return "";
});

/* --------------------------------------------------------------------- sheet */

const SHEET_ACTIVE_IDS = new Set(["sheet", "sheet-backdrop", "live", "alert"]);

/* While the dialog is open the rest of the page is inert, so focus cannot leave the
 * sheet by pointer, Tab or assistive tech. */
function setBackgroundInert(on) {
  for (const child of document.body.children) {
    if (SHEET_ACTIVE_IDS.has(child.id)) continue;
    child.inert = on;
  }
}

function openSheet(title, build) {
  sheetReturnKey = captureFocusKey() || activationFocusKey;
  sheetState = { build };
  document.getElementById("sheet-title").textContent = title;
  rebuildSheet();
  setBackgroundInert(true);
  document.getElementById("sheet-backdrop").hidden = false;
  const sheet = document.getElementById("sheet");
  sheet.hidden = false;
  const first = sheet.querySelector("button, input, select, textarea, a[href]");
  if (first) first.focus();
  updateClosingGuard();
}

function rebuildSheet() {
  if (!sheetState) return;
  const body = document.getElementById("sheet-body");
  const focusKey = captureFocusKey();
  body.replaceChildren();
  idCounter = 0;
  sheetState.build(body);
  restoreFocus(focusKey);
  updateClosingGuard();
}

function closeSheet() {
  const returnKey = sheetReturnKey;
  sheetReturnKey = null;
  setBackgroundInert(false);
  document.getElementById("sheet").hidden = true;
  document.getElementById("sheet-backdrop").hidden = true;
  document.getElementById("sheet-body").replaceChildren();
  sheetState = null;
  state.edit = null;
  state.reviewError = null;
  updateClosingGuard();
  render();
  restoreFocus(returnKey);
}

function requestCloseSheet() {
  if (state.edit && state.edit.dirty) {
    if (!window.confirm("Discard the unsaved changes for this date?")) return;
    state.edit.dirty = false;
  }
  closeSheet();
}

document.addEventListener("keydown", (event) => {
  const sheet = document.getElementById("sheet");
  if (sheet.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    requestCloseSheet();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = Array.from(
    sheet.querySelectorAll("button, input, select, textarea, a[href]")
  ).filter((node) => !node.disabled && node.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

/* ------------------------------------------------------------------- routing */

function parseHash() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("?");
  const name = parts[0];
  const params = new URLSearchParams(parts[1] || "");
  return {
    view: VIEW_IDS[name] ? name : "today",
    edit: params.get("edit") === "1",
  };
}

function applyRoute(initial) {
  const route = parseHash();
  const changed = state.view !== route.view;
  state.view = route.view;
  for (const key of Object.keys(VIEW_IDS)) {
    viewEl(key).hidden = key !== route.view;
  }
  for (const link of document.querySelectorAll(".bottom-nav a")) {
    if (link.dataset.view === route.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (initial && route.edit) {
    pendingEdit = true;
    history.replaceState(null, "", "#" + route.view);
  }
  render();
  if (changed) window.scrollTo(0, 0);
}

window.addEventListener("hashchange", () => applyRoute(false));

/* ----------------------------------------------------------------- loading */

function adoptPlan(plan, keepDraft) {
  const dirty = keepDraft ? new Set(dirtyDates()) : null;
  state.plan = plan;
  const server = new Map();
  for (const day of plan.days) {
    server.set(day.date, { profile: day.profile, details: day.details || {} });
  }
  state.server = server;
  const next = new Map();
  for (const day of plan.days) {
    const existing = state.planDraft.get(day.date);
    next.set(day.date, dirty && dirty.has(day.date) && existing ? existing : server.get(day.date));
  }
  state.planDraft = next;
}

async function load() {
  if (!initData) {
    state.fatal = new ApiError(0, "unauthenticated", "");
    render();
    return;
  }
  state.fatal = null;
  state.loadError = null;
  try {
    const results = await Promise.all([api("/api/me"), api("/api/plan"), api("/api/today")]);
    state.me = results[0];
    adoptPlan(results[1], false);
    state.planConflict = null;
    state.settingsConflict = null;
    state.today = results[2];
    render();
    updateClosingGuard();
    if (pendingEdit) {
      pendingEdit = false;
      startChangeToday();
    }
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      state.fatal = error;
    } else {
      state.loadError = error.message || String(error);
    }
    render();
  }
}

async function refreshToday() {
  state.today = await api("/api/today");
  return state.today;
}

async function refreshTodayOnly() {
  try {
    await refreshToday();
  } catch (error) {
    showError(error);
  }
}

async function refreshPlan() {
  const plan = await api("/api/plan");
  adoptPlan(plan, true);
}

/* Passive refresh eligibility: one request at a time, never while hidden, a sheet is
 * open (editor/review/copy), a mutation is running, or the session is dead. */
function refreshPaused() {
  return (
    backgroundStopped ||
    document.hidden ||
    sheetState !== null ||
    mutationInflight > 0 ||
    state.fatal !== null ||
    !state.me
  );
}

/* Polling may only re-render the Today subtree, and only while the Today tab is
 * visible with no focused control, so it never closes a keyboard or drops focus
 * inside an editor/draft elsewhere. */
function renderTodayIfSafe() {
  if (state.fatal || !state.me || state.view !== "today" || sheetState !== null) return;
  if (focusOnInput()) return;
  const focusKey = captureFocusKey();
  renderToday();
  restoreFocus(focusKey);
}

function renderPlanIfSafe() {
  if (state.fatal || !state.me || state.view !== "plan") return;
  if (focusOnInput()) return;
  const focusKey = captureFocusKey();
  renderPlan();
  restoreFocus(focusKey);
}

function focusOnInput() {
  const active = document.activeElement;
  if (!active) return false;
  const tagName = active.tagName;
  return (
    tagName === "INPUT" ||
    tagName === "SELECT" ||
    tagName === "TEXTAREA" ||
    active.isContentEditable === true
  );
}

/* The cached /api/today read makes ready/running stages, the source check time and
 * settled confirmed/failed/unknown outcomes appear live. A mutation that lands during
 * the fetch wins: the stale response is discarded rather than overwriting it. */
async function backgroundRefresh() {
  if (refreshInflight || refreshPaused()) return;
  const version = mutationVersion;
  refreshInflight = true;
  let today;
  try {
    today = await api("/api/today");
    if (version !== mutationVersion || refreshPaused()) return;
    const changed = JSON.stringify(state.today) !== JSON.stringify(today);
    state.today = today;
    if (changed) renderTodayIfSafe();
    await handlePlanHorizon(today.today);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      backgroundStopped = true;
      state.fatal = error;
      render();
    }
    return;
  } finally {
    refreshInflight = false;
  }
}

/* A stale cached horizon is rebuilt only for a clean, visible Plan tab: the new 14
 * dates come from the server and a date with no row stays Not planned (no autofill).
 * A dirty draft is never rebased: the existing explicit conflict review is shown and
 * the draft is retained in memory. */
async function handlePlanHorizon(serverToday) {
  if (refreshPaused() || focusOnInput() || !state.plan || state.view !== "plan") return;
  if (state.plan.today === serverToday || state.planConflict) return;
  if (dirtyDates().length > 0) {
    await openPlanConflict({
      message:
        "The plan horizon advanced to a new day. Your unsaved draft is kept; review the latest 14 dates before saving.",
    });
    renderPlanIfSafe();
    return;
  }
  const version = mutationVersion;
  try {
    const plan = await api("/api/plan");
    if (refreshPaused() || focusOnInput() || version !== mutationVersion || dirtyDates().length > 0) return;
    adoptPlan(plan, false);
  } catch (error) {
    return;
  }
  renderPlanIfSafe();
}

/* --------------------------------------------------------------- today view */

function renderHeader() {
  document.getElementById("app-title").textContent = "Attendance";
  const sub = document.getElementById("app-sub");
  if (!state.me) {
    sub.textContent = "";
    return;
  }
  const name = state.me.name || "Name not set";
  const department = state.me.department || "department not set";
  sub.textContent = name + " • " + department;
}

function profileSummary(profile, details) {
  if (profile === "normal") return "Present (Infinite Studios - IS)";
  if (profile === "wfh") return "Both AM & PM • manager approval required";
  if (profile === "ma") {
    const parts = [];
    if (details && details.period) parts.push("Period: " + details.period.toUpperCase());
    if (details && details.timing) parts.push("Timing: " + details.timing);
    return parts.length ? parts.join(" • ") : "Unavailable";
  }
  if (profile === "skip") return "Skip — no attendance planned";
  return "Not planned";
}

function readiness(profile, details) {
  if (profile === "normal" || profile === "wfh") return { text: "Ready", kind: "ok" };
  // MA details can be complete and MA is still not executable: the form has no
  // authenticated MA mapping, so this is a permanent prerequisite, not a missing
  // field the owner can fill in here.
  if (profile === "ma") return { text: "Unavailable", kind: "warn" };
  if (profile === "skip") return { text: "Skip", kind: "" };
  return { text: "Not planned", kind: "warn" };
}

const MA_UNAVAILABLE_TEXT =
  "MA cannot be submitted: this form has no authenticated MA mapping yet, so the profile stays unavailable regardless of the period or timing you enter.";

function sourceBlock(check) {
  const body = h("div");
  if (!check) {
    body.append(h("p", { class: "small muted", text: "No attendance source check yet." }));
  } else {
    const statusText =
      check.status === "found"
        ? "A record for you exists today."
        : check.status === "not_found"
          ? "No record for you found today."
          : "The attendance source is unavailable.";
    body.append(h("p", { class: "row" }, tag(statusText, check.status === "found" ? "warn" : "")));
    if (check.checked_at) {
      body.append(
        h("p", {
          class: "small muted",
          text:
            "Checked at " + fmtIso(check.checked_at) + ". The public sheet may lag; updates can take time to appear.",
        })
      );
    }
    if (check.status === "unavailable" && check.error_code) {
      body.append(
        h("p", {
          class: "small error",
          text: SOURCE_ERRORS[check.error_code] || "The attendance source is unavailable.",
        })
      );
    }
  }
  // The dedicated action forces a fresh source read; the passive poll never does.
  const actions = h("div", { class: "actions" });
  actions.append(
    actionButton("Refresh records", "", async () => {
      const version = ++mutationVersion;
      try {
        const today = await api("/api/today?refresh=1");
        if (version !== mutationVersion || sheetState !== null) return;
        state.today = today;
        renderToday();
        announce("Attendance source re-checked. The sheet may still lag.");
      } catch (error) {
        if (error.status === 401 || error.status === 403) {
          backgroundStopped = true;
          state.fatal = error;
          render();
          return;
        }
        throw error;
      }
    })
  );
  body.append(actions);
  return card(h("h2", { text: "Attendance source" }), body);
}

function recordList(records) {
  const list = h("ul", { class: "list" });
  for (const record of records) {
    list.append(
      h(
        "li",
        {},
        h("div", { class: "big", text: record.timestamp ? fmtIso(record.timestamp) : "Record" }),
        h("div", { text: record.status || "" }),
        record.details ? h("div", { class: "small muted", text: Object.entries(record.details).map(([question, answer]) => question + ": " + answer).join(" · ") }) : null
      )
    );
  }
  return list;
}

function actionButton(label, kind, handler) {
  const button = h("button", { type: "button", class: kind || "", text: label });
  button.addEventListener("click", async () => {
    const focusKey = captureFocusKey();
    activationFocusKey = focusKey;
    const scope = button.closest("#sheet-body, .card") || button.parentElement;
    const controls = Array.from(scope.querySelectorAll("button, input, select, textarea"),
      (control) => [control, control.disabled]);
    for (const [control] of controls) control.disabled = true;
    mutationInflight += 1;
    mutationVersion += 1;
    busy(button, true);
    clearError();
    try {
      await handler();
    } catch (error) {
      showError(error);
    } finally {
      busy(button, false);
      mutationInflight -= 1;
      for (const [control, disabled] of controls) control.disabled = disabled;
      if (document.activeElement === document.body) restoreFocus(focusKey);
    }
  });
  return button;
}

function renderToday() {
  const root = viewEl("today");
  root.replaceChildren();
  if (!state.today || !state.me) {
    root.append(loading());
    return;
  }
  const t = state.today;
  const decision = t.decision;
  const settings = t.settings;

  root.append(
    card(
      h("div", { class: "row" },
        h("span", { class: "big", text: fmtDate(t.today) }),
        tag(settings.enabled ? "Automatic on" : "Automatic paused", settings.enabled ? "ok" : "warn")
      ),
      h("p", { class: "muted small", text: t.today })
    )
  );

  if (t.identity_suspended) {
    // A collision disables both owners' mapping before any daily decision settles.
    // Show only the operator-resolution state: no plan, evidence, records or progress
    // may be inferred while the identity is ambiguous.
    root.append(
      banner(
        "warn",
        h("strong", { text: "Attendance mapping suspended" }),
        h("p", {
          class: "small",
          text:
            "Another account is mapped to the same attendance name and department. Automatic submission is paused and no records are shown while the mapping is ambiguous.",
        }),
        h("p", { class: "small muted", text: "An operator must resolve the duplicate mapping before attendance can run again." })
      )
    );
    return;
  }

  if (!state.me.onboarded) {
    root.append(
      banner(
        "warn",
        h("strong", { text: "Confirm your attendance name" }),
        h("p", {
          class: "small",
          text:
            "Enter your full name in ALL CAPS exactly as it appears in the attendance form MyInfo name field, then confirm it in Settings.",
        }),
        actionButton("Open Settings", "primary", async () => {
          location.hash = "#settings";
        })
      )
    );
  }

  const profile = decision.profile;
  const details = decision.details || {};
  const ready = readiness(profile, details);
  const stateText = {
    awaiting: "Waiting for the automatic time",
    ready: "Queued for submission",
    held: "Action required",
    running: "Submission in progress",
    recorded: "Recorded",
    skipped: "Skipped",
    confirmed: "Confirmed",
    failed: "Failed",
    unknown: "Unknown outcome",
    missed: "Missed",
  }[decision.state] || decision.state;

  root.append(
    card(
      h("h2", { text: "Today's plan" }),
      h("div", { class: "row" },
        h("span", { class: "big", text: PROFILE_LABEL[profile] || "Not planned" }),
        tag(ready.text, ready.kind)
      ),
      h("p", { class: "muted", text: profileSummary(profile, details) }),
      profile === "ma" ? h("p", { class: "small warn", text: MA_UNAVAILABLE_TEXT }) : null,
      h("p", { class: "small muted", text: "State: " + stateText }),
      decision.reason === "user_edit"
        ? h("p", { class: "small warn", text: "Automatic fallback is paused while you edit today." })
        : null,
      h("p", {
        class: "small muted",
        text:
          "Next: " +
          ({
            automatic_submission: t.next_action_at
              ? "automatic submission at " + fmtClock(t.next_action_at)
              : "automatic submission",
            action_required: "your decision is needed",
            submission_in_progress: "submission in progress",
            none: "nothing scheduled",
          }[t.next_action] || "nothing scheduled"),
      })
    )
  );

  root.append(sourceBlock(t.source_check));

  const records = t.observation && t.observation.records ? t.observation.records : [];
  if (records.length) {
    const actions = h("div", { class: "actions" });
    actions.append(
      actionButton("Keep existing", "primary", async () => {
        await todayAction("keep", {});
        announce("Existing attendance kept.");
      })
    );
    actions.append(actionButton("Submit different attendance", "", async () => {
      await refreshToday();
      openReviewSheet();
    }));
    const latest = t.source_check;
    const latestText =
      latest && latest.status === "found"
        ? "The latest check found these records."
        : latest && latest.status === "unavailable"
          ? "The latest check is unavailable; these retained records are not confirmed by it."
          : "The latest check found no record; these retained records are not confirmed by it.";
    root.append(
      banner("warn",
        h("strong", { text: "Existing attendance found" }),
        h("p", { class: "small", text: "Automatic submission is suppressed." }),
        h("p", { class: "small", text: latestText }),
        latest && latest.checked_at
          ? h("p", { class: "small muted", text: "Latest check at " + fmtIso(latest.checked_at) + ". The sheet may lag." })
          : null
      )
    );
    root.append(
      card(
        h("h2", { text: "Your records" }),
        h("p", { class: "small muted", text: "Retained same-day evidence; it is not cleared when a later export omits it." }),
        h("p", { class: "small", text: "Submitting different attendance opens a one-time review and adds a new entry; it does not edit the existing record." }),
        recordList(records),
        actions
      )
    );
  } else if (decision.state === "recorded") {
    root.append(
      banner("warn", h("strong", { text: "Recorded" }), h("p", { class: "small", text: "Automatic submission is suppressed." }))
    );
  }

  if (t.local_outcome) {
    root.append(
      card(
        h("h2", { text: "Local submission history" }),
        h("p", { class: t.local_outcome === "failed" || t.local_outcome === "unknown" ? "error" : "", text: LOCAL_OUTCOME[t.local_outcome] || t.local_outcome })
      )
    );
  }

  if (t.stage) {
    const cancelText =
      t.stage === "submitting"
        ? "Submission has started; changes cannot promise cancellation."
        : "Before submission starts you can cancel by sending /cancel in Telegram.";
    root.append(
      card(
        h("h2", { text: "In progress" }),
        h("p", { text: STAGE_TEXT[t.stage] || "Submission in progress." }),
        h("p", { class: "small muted", text: cancelText })
      )
    );
  }

  const editable = !EDITABLE_FINAL.includes(decision.state) && !records.length &&
    !TERMINAL_OUTCOMES.has(t.local_outcome);
  const actions = h("div", { class: "actions stack" });
  if (editable) {
    actions.append(actionButton("Change Today", "primary", async () => startChangeToday()));
    if ((profile === "normal" || profile === "wfh") && ["awaiting", "ready", "held", "failed"].includes(decision.state)) {
      const retry = decision.state === "failed";
      actions.append(actionButton(retry ? "Retry submission" : "Submit Now", "", async () => {
        if (!window.confirm((retry ? "Retry" : "Submit") + " attendance now for " + (PROFILE_LABEL[profile] || profile) + "? A new OTP and fresh record checks are required.")) return;
        await todayAction("submit_now", {});
        announce("Submission queued.");
      }));
    }
    if (decision.state === "held" && decision.original) {
      actions.append(actionButton("Restore original", "", async () => {
        await todayAction("restore", {});
        announce("Original prediction restored.");
      }));
    }
    if (decision.state !== "skipped") {
      actions.append(actionButton("Skip today", "link", async () => {
        if (!window.confirm("Skip attendance for today?")) return;
        await todayAction("skip", {});
        announce("Today skipped.");
      }));
    }
    if (decision.state === "held" && !records.length && decision.reason !== "manual_submitted") {
      actions.append(actionButton("I already submitted manually", "link", async () => {
        if (!window.confirm("Mark that you have already submitted attendance outside the planner?")) return;
        await todayAction("manual_submitted", {});
        announce("Recorded as manually submitted; not verified as success.");
      }));
    }
  }
  if (actions.childElementCount) root.append(card(h("h2", { text: "Actions" }), actions));

  if (state.review !== null) {
    root.append(
      banner("warn",
        h("strong", { text: "Additional submission review pending" }),
        h("p", { class: "small", text: "Nothing is submitted until you authorize once." })
      )
    );
    const reviewActions = h("div", { class: "actions" });
    reviewActions.append(actionButton("Resume review", "primary", async () => openReviewSheet()));
    reviewActions.append(actionButton("Discard review", "link", async () => {
      state.review = null;
      state.reviewChoice = null;
      updateClosingGuard();
      render();
    }));
    root.append(reviewActions);
  }
}

/* -------------------------------------------------------------- today ops */

async function todayAction(name, extra) {
  const revision =
    extra && extra.expected_revision !== undefined
      ? extra.expected_revision
      : state.today
        ? state.today.decision.revision
        : 0;
  const body = { action: name, expected_revision: revision };
  if (extra && "profile" in extra) body.profile = extra.profile;
  if (extra && "details" in extra) body.details = extra.details;
  if (extra && "consent_digest" in extra) body.consent_digest = extra.consent_digest;
  let result;
  try {
    result = await api("/api/today/action", { method: "POST", body });
  } catch (error) {
    if (error.status === 409) {
      try {
        await refreshToday();
      } catch (inner) {
        // keep the original conflict error; the banner already explains it.
      }
      // Never silently swap the plan revision under a dirty draft: surface the same
      // explicit conflict review the Save Plan path uses. The review flow handles its
      // own 409 (evidence/answers changed) without touching the plan revision.
      if (name !== "additional_review" && name !== "additional_confirm") {
        await openPlanConflict(error);
      }
      render();
    }
    throw error;
  }
  if (name === "additional_review") {
    state.review = result;
    return result;
  }
  state.today = result;
  if (name === "submit_now" || name === "restore" || name === "skip") {
    try {
      await refreshPlan();
    } catch (error) {
      showError(error);
    }
  }
  render();
  return result;
}

async function startChangeToday() {
  clearError();
  try {
    if (!state.today) await refreshToday();
    const decision = state.today.decision;
    if (decision.state !== "held" || decision.reason !== "user_edit") {
      await todayAction("hold", {});
    }
    openEdit(state.today.today, true);
  } catch (error) {
    showError(error);
  }
}

function openEdit(date, isToday) {
  const server = state.planDraft.get(date) || { profile: null, details: {} };
  const decision = isToday && state.today ? state.today.decision : null;
  const profile = decision ? decision.profile : server.profile;
  const details = decision ? decision.details || {} : server.details || {};
  state.edit = { date, isToday: !!isToday, profile: profile || null, details: Object.assign({}, details), dirty: false, error: null, conflict: null };
  openSheet(isToday ? "Change today" : "Edit date", buildEditSheet);
}

function normalizeDetails(profile, details) {
  if (profile !== "ma") return { details: {} };
  const out = {};
  const period = details.period;
  if (period) {
    if (!["am", "pm", "both"].includes(period)) return { error: "Choose a valid MA period" };
    out.period = period;
  }
  let timing = String(details.timing || "").trim().replace(/[^0-9]/g, "");
  if (timing) {
    if (timing.length === 4) timing = timing.slice(0, 2) + ":" + timing.slice(2);
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(timing)) return { error: "Enter timing as HH:MM or HHMM" };
    out.timing = timing;
  }
  return { details: out };
}

function profileSelect(current, onChange) {
  const id = uid();
  const select = h("select", { id });
  const options = [["", "Not planned"], ["normal", "Present (IS)"], ["wfh", "WFH"], ["ma", "MA"], ["skip", "Skip"]];
  for (const [value, label] of options) {
    select.append(h("option", { value, selected: (current || "") === value }, label));
  }
  select.addEventListener("change", () => onChange(select.value || null));
  return h("label", { class: "field", for: id }, h("span", { text: "Profile" }), select);
}

function buildEditSheet(body) {
  const edit = state.edit;
  body.append(h("p", { class: "small muted", text: fmtDate(edit.date) }));
  if (edit.isToday) {
    body.append(
      h("p", { class: "small warn", text: "Changing today pauses automatic fallback until you save or restore the original." })
    );
  }

  body.append(
    profileSelect(edit.profile, (value) => {
      edit.profile = value;
      edit.dirty = true;
      rebuildSheet();
    })
  );

  if (edit.profile === "wfh") {
    body.append(
      card(
        h("p", { class: "small", text: "Status: Work-from-Home (WFH)" }),
        h("p", { class: "small", text: "Is your status for AM or PM? Both (AM & PM)" }),
        h("p", { class: "small muted", text: "A manager-approval declaration is submitted with this profile." })
      )
    );
  } else if (edit.profile === "ma") {
    const fieldset = h("fieldset");
    fieldset.append(h("legend", { text: "MA details" }));
    const periodId = uid();
    const period = h("select", { id: periodId });
    for (const [value, label] of [["", "Choose period"], ["am", "AM"], ["pm", "PM"], ["both", "Both (AM & PM)"]]) {
      period.append(h("option", { value, selected: (edit.details.period || "") === value }, label));
    }
    period.addEventListener("change", () => {
      edit.details = Object.assign({}, edit.details, { period: period.value || undefined });
      edit.dirty = true;
      updateClosingGuard();
    });
    fieldset.append(h("label", { class: "field", for: periodId }, h("span", { text: "Period" }), period));

    const timingId = uid();
    const timing = h("input", {
      id: timingId,
      type: "text",
      inputmode: "numeric",
      maxlength: 5,
      placeholder: "HH:MM",
      autocomplete: "off",
      value: edit.details.timing || "",
      "aria-describedby": timingId + "-help",
    });
    timing.addEventListener("input", () => {
      edit.details = Object.assign({}, edit.details, { timing: timing.value });
      edit.dirty = true;
      updateClosingGuard();
    });
    fieldset.append(h("label", { class: "field", for: timingId }, h("span", { text: "Timing (HH:MM or HHMM)" }), timing));
    fieldset.append(
      h("p", { class: "small warn", id: timingId + "-help", text: "MA stays a draft until authenticated form details are captured. It cannot run automatically or be submitted yet. Other-half status options are not available." })
    );
    body.append(fieldset);
  } else if (edit.profile === "skip") {
    body.append(h("p", { class: "small muted", text: "Skip means no attendance is planned for this date." }));
  }

  if (edit.error) body.append(h("p", { class: "error", role: "alert", text: edit.error }));

  if (edit.conflict) {
    const server = edit.conflict.server;
    body.append(
      card(
        banner("warn",
          h("strong", { text: "This date changed on the server" }),
          h("p", { class: "small", text: "Server now: " + (PROFILE_LABEL[server.profile] || "Not planned") + " — " + profileSummary(server.profile, server.details) }),
          h("p", { class: "small", text: "Your edit: " + (PROFILE_LABEL[edit.profile] || "Not planned") + " — " + profileSummary(edit.profile, edit.details) })
        ),
        h("div", { class: "actions stack" },
          actionButton("Reload the server value into this editor", "", async () => {
            adoptPlan(edit.conflict.latest, true);
            state.planConflict = null;
            edit.profile = server.profile;
            edit.details = Object.assign({}, server.details);
            edit.dirty = false;
            edit.conflict = null;
            edit.error = null;
            updateClosingGuard();
            rebuildSheet();
            announce("Latest server value loaded for this date; nothing to save.");
          }),
          actionButton("Keep my edit and use latest revision", "primary", async () => {
            if (!window.confirm("Load the latest server revision and keep your edit for this date?")) return;
            adoptPlan(edit.conflict.latest, true);
            state.planConflict = null;
            edit.conflict = null;
            edit.error = null;
            updateClosingGuard();
            rebuildSheet();
            announce("Latest revision loaded; your edit is kept. Press Save for date to apply it.");
          })
        )
      )
    );
  }

  const actions = h("div", { class: "actions stack" });
  actions.append(
    actionButton("Save for date", "primary", async () => {
      const result = normalizeDetails(edit.profile, edit.details);
      if (result.error) {
        edit.error = result.error;
        rebuildSheet();
        return;
      }
      const change = { date: edit.date, profile: edit.profile, details: result.details };
      try {
        const plan = await api("/api/plan", {
          method: "PUT",
          body: { expected_revision: state.plan.revision, changes: [change] },
        });
        const dirty = new Set(dirtyDates());
        const kept = state.planDraft;
        adoptPlan(plan, false);
        for (const date of dirty) {
          if (date !== edit.date && kept.has(date)) state.planDraft.set(date, kept.get(date));
        }
        edit.dirty = false;
        try {
          await refreshToday();
        } catch (inner) {
          // the plan itself saved; a today refresh failure does not undo it.
        }
        closeSheet();
        announce("Date saved.");
      } catch (error) {
        if (error.status === 409 || error.status === 422) {
          let latest = null;
          let server = { profile: null, details: {} };
          try {
            latest = await api("/api/plan");
            const day = latest.days.find((row) => row.date === edit.date);
            if (day) server = { profile: day.profile, details: day.details || {} };
          } catch (inner) {
            // keep the conflict message; no revision is adopted on a failed refresh.
          }
          edit.error = (error.message || "The plan changed.") + " Your edit is kept; review the latest value for this date before saving again.";
          edit.conflict = latest ? { latest, server } : null;
        } else {
          edit.error = error.message;
        }
        updateClosingGuard();
        rebuildSheet();
      }
    })
  );
  if (edit.isToday) {
    const canSubmit = edit.profile === "normal" || edit.profile === "wfh";
    actions.append(
      actionButton(canSubmit ? "Submit now" : "Submit now (needs Present or WFH)", "", async () => {
        if (!canSubmit) {
          edit.error = "Submit now needs Present (IS) or WFH. MA cannot be submitted yet.";
          rebuildSheet();
          return;
        }
        if (!window.confirm("Submit attendance now for " + PROFILE_LABEL[edit.profile] + "?")) return;
        await todayAction("submit_now", { profile: edit.profile, details: {} });
        closeSheet();
        announce("Submission queued.");
      })
    );
  }
  body.append(actions);
}

/* ------------------------------------------------------------------ review */

function openReviewSheet() {
  if (!state.reviewChoice) {
    const decision = state.today ? state.today.decision : null;
    state.reviewChoice = {
      profile: decision && PROFILES.includes(decision.profile) ? decision.profile : "normal",
      details: decision && decision.details ? Object.assign({}, decision.details) : {},
    };
  }
  openSheet("Additional submission", buildReviewSheet);
}

function answerList(answers) {
  const list = h("ul", { class: "list" });
  if (!answers) {
    list.append(h("li", { class: "muted", text: "No answers returned." }));
    return list;
  }
  let entries = [];
  if (Array.isArray(answers)) {
    entries = answers.map((item, index) =>
      Array.isArray(item) ? item : ["Answer " + (index + 1), item]
    );
  } else {
    entries = Object.entries(answers);
  }
  for (const [key, value] of entries) {
    list.append(
      h(
        "li",
        {},
        h("div", { class: "small muted", text: String(key) }),
        h("div", { text: Array.isArray(value) ? value.join(", ") : String(value) })
      )
    );
  }
  return list;
}

function buildReviewSheet(body) {
  const review = state.review;
  if (!review) {
    body.append(
      h("p", { class: "small muted", text: "Choose the profile to review. Nothing is submitted until you authorize once." })
    );
    body.append(
      profileSelect(state.reviewChoice.profile, (value) => {
        state.reviewChoice.profile = value;
        rebuildSheet();
      })
    );
    if (state.reviewChoice.profile === "ma") {
      const timingId = uid();
      const timing = h("input", { id: timingId, type: "text", inputmode: "numeric", maxlength: 5, placeholder: "HH:MM", autocomplete: "off", value: state.reviewChoice.details.timing || "" });
      timing.addEventListener("input", () => {
        state.reviewChoice.details = Object.assign({}, state.reviewChoice.details, { timing: timing.value });
        updateClosingGuard();
      });
      const periodId = uid();
      const period = h("select", { id: periodId });
      for (const [value, label] of [["", "Choose period"], ["am", "AM"], ["pm", "PM"], ["both", "Both (AM & PM)"]]) {
        period.append(h("option", { value, selected: (state.reviewChoice.details.period || "") === value }, label));
      }
      period.addEventListener("change", () => {
        state.reviewChoice.details = Object.assign({}, state.reviewChoice.details, { period: period.value || undefined });
        updateClosingGuard();
      });
      body.append(h("label", { class: "field", for: periodId }, h("span", { text: "Period" }), period));
      body.append(h("label", { class: "field", for: timingId }, h("span", { text: "Timing (HH:MM)" }), timing));
    }
    if (state.reviewError) body.append(h("p", { class: "error", role: "alert", text: state.reviewError }));
    const actions = h("div", { class: "actions stack" });
    actions.append(
      actionButton("Review evidence", "primary", async () => {
        const choice = state.reviewChoice;
        if (!choice.profile || choice.profile === "skip") {
          state.reviewError = "Choose Present (IS), WFH or MA.";
          rebuildSheet();
          return;
        }
        const result = normalizeDetails(choice.profile, choice.details);
        if (result.error) {
          state.reviewError = result.error;
          rebuildSheet();
          return;
        }
        try {
          const reviewed = await todayAction("additional_review", { profile: choice.profile, details: result.details });
          state.review = reviewed;
          state.reviewChoice = { profile: reviewed.profile, details: reviewed.details || {} };
          state.reviewError = null;
          rebuildSheet();
        } catch (error) {
          state.reviewError = error.message;
          rebuildSheet();
        }
      })
    );
    actions.append(actionButton("Cancel", "link", async () => requestCloseSheet()));
    body.append(actions);
    return;
  }

  body.append(
    card(
      h("h2", { text: "Proposed submission" }),
      h("p", { class: "big", text: PROFILE_LABEL[review.profile] || review.profile }),
      h("p", { class: "small muted", text: profileSummary(review.profile, review.details) })
    )
  );
  body.append(
    card(
      h("h2", { text: "Records at review" }),
      h("p", { class: "small muted", text: "Checked at " + fmtIso(review.checked_at) + ". The sheet may lag." }),
      recordList(review.records || [])
    )
  );
  if (review.local_attempts && review.local_attempts.length) {
    const list = h("ul", { class: "list" });
    for (const id of review.local_attempts) {
      list.append(h("li", { text: "Local submission attempt #" + id }));
    }
    body.append(card(h("h2", { text: "Local submission history" }), list));
  } else {
    body.append(card(h("h2", { text: "Local submission history" }), h("p", { class: "small muted", text: "No local submission attempts today." })));
  }
  body.append(card(h("h2", { text: "Exact answers to submit" }), answerList(review.answers)));

  if (state.reviewError) body.append(h("p", { class: "error", role: "alert", text: state.reviewError }));

  const actions = h("div", { class: "actions stack" });
  actions.append(
    actionButton("Authorize one additional submission", "primary", async () => {
      try {
        await todayAction("additional_confirm", {
          consent_digest: review.consent_digest,
          expected_revision: review.revision,
          profile: state.reviewChoice.profile,
          details: state.reviewChoice.details,
        });
        state.review = null;
        state.reviewChoice = null;
        state.reviewError = null;
        closeSheet();
        announce("One additional submission authorized.");
      } catch (error) {
        if (error.status === 409) {
          state.review = null;
          state.reviewChoice = null;
          state.reviewError = "Evidence or answers changed. Close and review again.";
        } else {
          state.reviewError = error.message;
        }
        updateClosingGuard();
        rebuildSheet();
      }
    })
  );
  actions.append(actionButton("Cancel review", "link", async () => requestCloseSheet()));
  body.append(actions);
}

/* -------------------------------------------------------------------- plan */

function lowFutureWeekdays() {
  if (!state.plan) return false;
  let count = 0;
  let index = 0;
  for (const day of state.plan.days) {
    index += 1;
    if (index === 1) continue;
    const draft = state.planDraft.get(day.date);
    if (weekday(day.date) && draft && (draft.profile === "normal" || draft.profile === "wfh" || draft.profile === "ma")) {
      count += 1;
    }
  }
  return count < 3;
}

function dayClass(draft, entry) {
  const profile = draft.profile;
  return "day" + (profile ? " " + profile : "") + (lockedDay(entry.date) ? " terminal" : "");
}

function dayButton(entry) {
  const draft = state.planDraft.get(entry.date) || { profile: null, details: {} };
  const locked = lockedDay(entry.date);
  const button = h("button", {
    type: "button",
    id: "day-" + entry.date,
    class: dayClass(draft, entry),
    dataset: { date: entry.date },
    "aria-pressed": state.selection.has(entry.date) ? "true" : "false",
    "aria-label":
      fmtDate(entry.date) +
      ": " +
      (PROFILE_LABEL[draft.profile] || "Not planned") +
      (locked ? " (" + lockedReason(entry.date) + "; not editable)" : ""),
  });
  button.append(h("span", { class: "dow", text: fmtShort(entry.date).split(",")[0] }));
  button.append(h("span", { text: String(dayNumber(entry.date)) }));
  button.append(h("span", { class: "st", text: PROFILE_SHORT[draft.profile] || "—" }));
  button.addEventListener("click", () => selectDate(entry.date));
  return button;
}

function selectDate(date) {
  if (state.multi) {
    if (state.selection.has(date)) state.selection.delete(date);
    else state.selection.add(date);
  } else {
    state.selection = new Set([date]);
  }
  render();
}

function selectedCard(date) {
  const draft = state.planDraft.get(date) || { profile: null, details: {} };
  const ready = readiness(draft.profile, draft.details);
  const locked = lockedDay(date);
  const body = card(
    h("h2", { text: fmtDate(date) }),
    h("div", { class: "row" },
      h("span", { class: "big", text: PROFILE_LABEL[draft.profile] || "Not planned" }),
      locked ? tag("Locked", "danger") : tag(ready.text, ready.kind)
    ),
    h("p", { class: "muted", text: profileSummary(draft.profile, draft.details) })
  );
  if (locked) {
    body.append(
      h("p", {
        class: "small warn",
        text: "Read-only: " + lockedReason(date) + ". This date cannot be edited or bulk-filled.",
      })
    );
    return body;
  }
  const actions = h("div", { class: "actions" });
  actions.append(
    actionButton("Edit this date", "", async () => {
      if (date === state.plan.today) await startChangeToday();
      else openEdit(date, false);
    })
  );
  body.append(actions);
  return body;
}

async function fillWeekdays() {
  const targets = fillTargets();
  if (!targets.length) {
    announce("No editable weekday dates in scope; nothing was filled.");
    return;
  }
  const includesToday = state.plan.today && targets.includes(state.plan.today);
  // Today needs the server hold acknowledged before its draft is touched; a failed
  // hold skips today only and still fills the other draft dates.
  let todayHeld = true;
  if (includesToday) {
    try {
      await todayAction("hold", {});
    } catch (error) {
      todayHeld = false;
      showError(error);
    }
  }
  let changed = 0;
  let todaySkipped = false;
  for (const date of targets) {
    if (date === state.plan.today && !todayHeld) {
      todaySkipped = true;
      continue;
    }
    state.planDraft.set(date, { profile: "normal", details: {} });
    changed += 1;
  }
  render();
  updateClosingGuard();
  const scope = state.selection.size ? "selected weekday(s)" : "editable weekdays";
  let message = "Set " + changed + " " + scope + " to Present (IS) in the draft.";
  if (todaySkipped) message += " Today was not changed because the server did not acknowledge the hold.";
  announce(message);
}

function copyPreview() {
  const dates = state.plan.days.map((day) => day.date);
  const first = dates.slice(0, 7);
  const second = dates.slice(7, 14);
  const rows = [];
  for (let i = 0; i < second.length; i += 1) {
    const source = state.planDraft.get(first[i]) || { profile: null, details: {} };
    rows.push({
      from: first[i],
      to: second[i],
      profile: source.profile,
      details: source.details,
      locked: bulkExcluded(second[i]),
    });
  }
  const openRows = rows.filter((row) => !row.locked);
  openSheet("Copy previous week", (body) => {
    body.append(h("p", { class: "small muted", text: "The first week's draft will replace the second week's draft for these dates. Nothing is saved until you press Save Plan. Recorded, running, skipped, missed or unknown dates are not overwritten." }));
    if (!openRows.length) {
      body.append(banner("warn", h("strong", { text: "Nothing can be copied" }), h("p", { class: "small", text: "Every date in the second week is locked by an existing or terminal outcome." })));
    }
    const list = h("ul", { class: "list" });
    for (const row of rows) {
      list.append(
        h("li", {},
          h("div", { class: "small muted", text: fmtShort(row.to) }),
          h("div", { text: PROFILE_LABEL[row.profile] || "Not planned" }),
          h("div", { class: "small muted", text: profileSummary(row.profile, row.details) }),
          row.locked ? tag("Locked — not overwritten", "danger") : null
        )
      );
    }
    body.append(list);
    body.append(
      h("div", { class: "actions stack" },
        actionButton("Apply copy to draft", "primary", async () => {
          if (!openRows.length) {
            closeSheet();
            announce("Nothing was copied; every target date is locked.");
            return;
          }
          for (const row of openRows) {
            state.planDraft.set(row.to, { profile: row.profile, details: Object.assign({}, row.details) });
          }
          closeSheet();
          const skipped = rows.length - openRows.length;
          announce(
            "Copied the first week into " + openRows.length + " second-week draft date(s)." +
              (skipped ? " " + skipped + " locked date(s) were not overwritten." : "")
          );
        }),
        actionButton("Cancel", "link", async () => requestCloseSheet())
      )
    );
  });
}

function planToolbar() {
  const wrap = h("div", { class: "actions stack" });
  wrap.append(
    actionButton(state.multi ? "Single select" : "Select multiple", "", async () => {
      state.multi = !state.multi;
      render();
    })
  );
  const targets = fillTargets();
  wrap.append(
    actionButton(
      state.selection.size
        ? "Fill " + targets.length + " selected weekday(s) with Present (IS)"
        : "Fill all editable weekdays with Present (IS)",
      "",
      async () => {
        await fillWeekdays();
      }
    )
  );
  if (state.selection.size) {
    wrap.append(
      actionButton("Clear selection", "link", async () => {
        state.selection = new Set();
        render();
      })
    );
  }
  wrap.append(actionButton("Copy previous week", "", async () => copyPreview()));
  return wrap;
}

/* An explicit conflict review: the latest server plan is shown beside the draft and
 * nothing is rebased until the owner picks a resolution. The stale revision is never
 * silently swapped for a fresh one underneath a still-dirty draft. */
async function openPlanConflict(error) {
  const version = mutationVersion;
  let latest;
  try {
    latest = await api("/api/plan");
  } catch (inner) {
    showError(inner);
    return;
  }
  if (sheetState !== null || version !== mutationVersion) return;
  const affected = dirtyDates();
  showError(error);
  if (!affected.length) {
    // No owner draft is outstanding, so loading the latest revision rebases nothing.
    adoptPlan(latest, true);
    state.planConflict = null;
    announce("The plan changed on the server. The latest revision was loaded; no draft changes were outstanding.");
    return;
  }
  state.planConflict = { message: error.message, latest, affected };
  announce("The plan changed on the server. Review the latest values against your draft before saving again.");
}

function planConflictCard() {
  const conflict = state.planConflict;
  const latest = new Map(
    conflict.latest.days.map((day) => [day.date, { profile: day.profile, details: day.details || {} }])
  );
  const list = h("ul", { class: "list" });
  for (const date of conflict.affected) {
    const draft = state.planDraft.get(date) || { profile: null, details: {} };
    const server = latest.get(date) || { profile: null, details: {} };
    list.append(
      h("li", {},
        h("div", { class: "small muted", text: fmtShort(date) }),
        h("div", { text: "Server now: " + (PROFILE_LABEL[server.profile] || "Not planned") }),
        h("div", { text: "Your draft: " + (PROFILE_LABEL[draft.profile] || "Not planned") })
      )
    );
  }
  const actions = h("div", { class: "actions stack" });
  actions.append(
    actionButton("Refresh latest", "", async () => {
      const refreshed = await api("/api/plan");
      state.planConflict = Object.assign({}, conflict, { latest: refreshed });
      render();
      announce("Latest server plan reloaded. Compare it with your draft before resolving.");
    })
  );
  actions.append(
    actionButton("Keep my draft and use latest revision", "primary", async () => {
      const latestDates = new Set(conflict.latest.days.map((day) => day.date));
      if (conflict.affected.some((date) => !latestDates.has(date))) {
        showError(new Error("This draft contains dates outside the latest 14-day window. Copy their answers to an editable date, or use server values to explicitly discard them."));
        return;
      }
      if (
        !window.confirm(
          "Load the latest server revision and keep your unsaved draft for " +
            conflict.affected.length +
            " date(s)?"
        )
      ) {
        return;
      }
      adoptPlan(conflict.latest, true);
      state.planConflict = null;
      render();
      announce("Latest revision loaded; your draft is kept. Press Save Plan to submit it.");
    })
  );
  actions.append(
    actionButton("Use server values for these dates", "link", async () => {
      adoptPlan(conflict.latest, true);
      for (const date of conflict.affected) {
        const server = latest.get(date);
        state.planDraft.set(date, server ? { profile: server.profile, details: Object.assign({}, server.details) } : { profile: null, details: {} });
      }
      state.planConflict = null;
      render();
      updateClosingGuard();
      announce("Server values restored for the conflicting dates.");
    })
  );
  return card(
    banner("warn",
      h("strong", { text: "Re-review the latest plan before saving" }),
      h("p", { class: "small", text: conflict.message }),
      h("p", { class: "small", text: "Your draft is kept in memory. Nothing is overwritten or saved until you refresh/re-review and resolve this." })
    ),
    h("p", { class: "small muted", text: "Latest server values versus your draft:" }),
    list,
    actions
  );
}

async function savePlan() {
  const changes = dirtyDates().map((date) => {
    const value = state.planDraft.get(date);
    return { date, profile: value.profile, details: value.details };
  });
  if (!changes.length) {
    announce("No plan changes to save.");
    return;
  }
  try {
    const result = await api("/api/plan", {
      method: "PUT",
      body: { expected_revision: state.plan.revision, changes },
    });
    adoptPlan(result, false);
    state.selection = new Set();
    state.planConflict = null;
    try {
      await refreshToday();
    } catch (inner) {
      // the plan saved; a today refresh failure does not undo it.
    }
    announce("Plan saved.");
  } catch (error) {
    if (error.status === 409 || error.status === 422) {
      await openPlanConflict(error);
    } else {
      showError(error);
    }
  }
  render();
  updateClosingGuard();
}

function renderPlan() {
  const root = viewEl("plan");
  root.replaceChildren();
  if (!state.plan) {
    root.append(loading());
    return;
  }
  if (lowFutureWeekdays()) {
    root.append(
      banner("warn",
        h("strong", { text: "Fewer than three future weekdays are planned" }),
        h("p", { class: "small", text: "Plan the next two weeks. Nothing is filled automatically." })
      )
    );
  }
  const days = state.plan.days;
  if (state.planConflict) root.append(planConflictCard());
  const weeks = [days.slice(0, 7), days.slice(7, 14)];
  weeks.forEach((week, index) => {
    const section = h("section", { class: "week" });
    section.append(h("h3", { text: index === 0 ? "This week" : "Next week" }));
    const grid = h("div", { class: "days" });
    for (const entry of week) grid.append(dayButton(entry));
    section.append(grid);
    root.append(section);
  });

  if (state.selection.size === 1) {
    root.append(selectedCard(Array.from(state.selection)[0]));
  } else if (state.selection.size > 1) {
    root.append(card(h("h2", { text: state.selection.size + " dates selected" }), h("p", { class: "small muted", text: "Use the bulk actions below." })));
  } else {
    root.append(card(h("p", { class: "small muted", text: "Tap a date to see its full plan and edit it." })));
  }

  root.append(card(h("h2", { text: "Bulk actions" }), planToolbar()));

  const dirty = dirtyDates();
  const saveRow = h("div", { class: "actions" });
  const saveButton = actionButton(
    dirty.length ? "Save Plan (" + dirty.length + " unsaved)" : "Save Plan",
    "primary",
    async () => savePlan()
  );
  if (!dirty.length || state.planConflict) saveButton.disabled = true;
  saveRow.append(saveButton);
  const saveHint = state.planConflict
    ? "Resolve the server conflict above before saving."
    : dirty.length
      ? "Unsaved dates: " + dirty.join(", ")
      : "No unsaved changes.";
  root.append(card(h("h2", { text: "Save" }), h("p", { class: "small muted", text: saveHint }), saveRow));
}

/* ---------------------------------------------------------------- settings */

function validateSettings(draft, settings) {
  const timeRe = /^([01]\d|2[0-3]):[0-5]\d$/;
  if (!timeRe.test(draft.prompt_time)) return "Prompt time must be HH:MM.";
  if (!timeRe.test(draft.auto_time)) return "Automatic time must be HH:MM.";
  if (!(draft.prompt_time < draft.auto_time)) return "Prompt time must be before the automatic time.";
  if (!(draft.auto_time < settings.attendance_deadline)) return "Automatic time must be before the attendance deadline.";
  if (draft.enabled) {
    if (!settings.name_confirmed) return "Confirm your name before enabling automatic mode.";
    if (!settings.phone_configured) return "Phone OTP is not configured, so automatic mode stays off.";
    if (!settings.enabled && !draft.policy) return "Accept the automatic execution policy to enable.";
  }
  return null;
}

async function loadMeConflict(target) {
  try {
    state.me = await api("/api/me");
    if (target === "settings") {
      const latest = state.me.settings;
      state.settingsConflict = true;
      state.settingsError =
        "The server values changed. Latest saved: automatic " +
        (latest.enabled ? "on" : "off") +
        ", prompt " +
        latest.prompt_time +
        ", automatic " +
        latest.auto_time +
        ". Your draft is kept; press Save settings again to overwrite with it.";
    } else {
      const name = state.me.settings.attendance_name || "not set";
      state.nameError =
        "The server name changed. Latest saved name: " +
        name +
        ". Your input is kept; press Save name again to overwrite it.";
    }
  } catch (inner) {
    if (target === "settings") state.settingsError = inner.message;
    else state.nameError = inner.message;
  }
}

async function saveSettings(overrides) {
  const settings = state.me.settings;
  const draft = Object.assign({}, settingsDraft(), overrides || {});
  const error = validateSettings(draft, settings);
  if (error) {
    state.settingsError = error;
    render();
    return;
  }
  try {
    const result = await api("/api/settings", {
      method: "PUT",
      body: {
        expected_revision: state.me.revision,
        enabled: draft.enabled,
        prompt_time: draft.prompt_time,
        auto_time: draft.auto_time,
      },
    });
    state.me = result;
    state.settingsDraft = null;
    state.settingsError = null;
    state.settingsConflict = null;
    await refreshTodayOnly();
    render();
    updateClosingGuard();
    announce("Settings saved.");
  } catch (err) {
    if (err.status === 409) await loadMeConflict("settings");
    else state.settingsError = err.message;
    render();
  }
}

async function saveName() {
  const name = state.nameInput.trim();
  if (!name) {
    state.nameError = "Enter your full name in ALL CAPS.";
    render();
    return;
  }
  try {
    state.me = await api("/api/name", {
      method: "PUT",
      body: { name, expected_revision: state.me.revision },
    });
    state.nameInput = "";
    state.nameError = null;
    state.showNameForm = false;
    await refreshTodayOnly();
    render();
    announce("Name saved. Confirm it below to enable attendance lookup.");
  } catch (error) {
    if (error.status === 409) await loadMeConflict("name");
    else state.nameError = error.message;
    render();
  }
}

async function confirmName() {
  try {
    state.me = await api("/api/name", {
      method: "PUT",
      body: { confirmed: true, expected_revision: state.me.revision },
    });
    state.nameError = null;
    await refreshTodayOnly();
    render();
    announce("Name confirmed.");
  } catch (error) {
    if (error.status === 409) await loadMeConflict("name");
    else state.nameError = error.message;
    render();
  }
}

function settingsTimeField(label, value, onChange, fieldId) {
  const id = fieldId || uid();
  const input = h("input", { id, type: "time", value });
  input.addEventListener("change", () => onChange(input.value));
  return h("label", { class: "field", for: id }, h("span", { text: label }), input);
}

function nameSection(settings) {
  const wrap = h("div");
  if (settings.attendance_name && !settings.name_confirmed) {
    wrap.append(
      banner("warn",
        h("strong", { text: "Confirm your attendance name" }),
        h("p", { class: "small", text: "Normalised name: " + settings.attendance_name }),
        h("p", { class: "small", text: "Department: " + (state.me.department || "not provisioned") })
      )
    );
    wrap.append(
      h("div", { class: "actions" },
        actionButton("Confirm name", "primary", async () => confirmName()),
        actionButton("Edit name", "link", async () => {
          state.showNameForm = true;
          state.nameInput = settings.attendance_name || "";
          render();
        })
      )
    );
  } else if (settings.attendance_name && settings.name_confirmed) {
    wrap.append(
      h("p", { class: "big", text: settings.attendance_name }),
      h("p", { class: "muted", text: "Department: " + (state.me.department || "not provisioned") }),
      h("p", { class: "small muted", text: "Changing your name disables automatic mode until you confirm it again." })
    );
    if (!state.showNameForm) {
      wrap.append(
        h("div", { class: "actions" },
          actionButton("Change name", "", async () => {
            state.showNameForm = true;
            state.nameInput = "";
            render();
          })
        )
      );
    }
  }
  if (!settings.attendance_name || state.showNameForm) {
    const id = "set-name-input";
    const input = h("input", {
      id,
      type: "text",
      value: state.nameInput,
      maxlength: 200,
      autocapitalize: "characters",
      autocomplete: "name",
      spellcheck: "false",
      "aria-describedby": id + "-help",
    });
    input.addEventListener("input", () => {
      state.nameInput = input.value;
      updateClosingGuard();
    });
    wrap.append(
      h("label", { class: "field", for: id },
        h("span", { text: "Full name in ALL CAPS, exactly as in the attendance form MyInfo name field" }),
        input
      ),
      h("p", { class: "small muted", id: id + "-help", text: "Names are normalised (trimmed, collapsed, uppercased). No other spelling matches." })
    );
    if (state.nameError) wrap.append(h("p", { class: "error", role: "alert", text: state.nameError }));
    const nameActions = h("div", { class: "actions" });
    nameActions.append(actionButton("Save name", "primary", async () => saveName()));
    if (settings.attendance_name) {
      nameActions.append(
        actionButton("Cancel", "link", async () => {
          state.showNameForm = false;
          state.nameInput = "";
          state.nameError = null;
          render();
          updateClosingGuard();
        })
      );
    }
    wrap.append(nameActions);
  }
  return card(h("h2", { text: "Attendance name" }), wrap);
}

function renderSettings() {
  const root = viewEl("settings");
  root.replaceChildren();
  if (!state.me) {
    root.append(loading());
    return;
  }
  const settings = state.me.settings;
  root.append(nameSection(settings));

  if (state.settingsConflict) {
    root.append(
      banner("warn",
        h("strong", { text: "Server values changed" }),
        h("p", { class: "small", text: "Latest saved: automatic " + (settings.enabled ? "on" : "off") + ", prompt " + settings.prompt_time + ", automatic " + settings.auto_time + ". Your unsaved draft is kept; press Save settings again to overwrite it." }),
        h("div", { class: "actions" },
          actionButton("Reload latest and discard my draft", "", async () => {
            state.settingsDraft = null;
            state.settingsConflict = null;
            state.settingsError = null;
            render();
            updateClosingGuard();
            announce("Latest server settings loaded; your unsaved draft was discarded.");
          })
        )
      )
    );
  }

  if (settings.needs_review) {
    root.append(
      banner("warn", h("strong", { text: "Review migrated times" }), h("p", { class: "small", text: "Imported times need your confirmation before automatic mode runs." }))
    );
  }

  const draft = settingsDraft();
  const automation = h("div");
  const enabledId = "set-enabled";
  const enabled = h("input", { id: enabledId, type: "checkbox", checked: draft.enabled });
  enabled.addEventListener("change", () => {
    state.settingsDraft = Object.assign({}, settingsDraft(), { enabled: enabled.checked });
    render();
    updateClosingGuard();
  });
  automation.append(
    h("label", { class: "check", for: enabledId },
      enabled,
      h("span", { text: "Automatic mode — if you do not reply, the saved plan is submitted at the automatic time." })
    )
  );
  const policyId = "set-policy";
  const policy = h("input", { id: policyId, type: "checkbox", checked: draft.policy });
  policy.addEventListener("change", () => {
    state.settingsDraft = Object.assign({}, settingsDraft(), { policy: policy.checked });
    updateClosingGuard();
  });
  automation.append(
    h("label", { class: "check", for: policyId },
      policy,
      h("span", { text: "I understand that silence executes my saved plan at the automatic time." })
    )
  );
  automation.append(settingsTimeField("Morning prompt (Singapore)", draft.prompt_time, (value) => {
    state.settingsDraft = Object.assign({}, settingsDraft(), { prompt_time: value });
    render();
    updateClosingGuard();
  }, "set-prompt-time"));
  automation.append(settingsTimeField("Automatic time (Singapore)", draft.auto_time, (value) => {
    state.settingsDraft = Object.assign({}, settingsDraft(), { auto_time: value });
    render();
    updateClosingGuard();
  }, "set-auto-time"));
  const deadlineId = "set-deadline";
  automation.append(
    h("label", { class: "field", for: deadlineId },
      h("span", { text: "Attendance deadline (Singapore, fixed)" }),
      h("input", { id: deadlineId, type: "time", value: settings.attendance_deadline, readonly: true })
    )
  );
  if (settings.time_margin_warning) {
    automation.append(h("p", { class: "small warn", text: "Automatic time is less than 10 minutes before the deadline. This is a warning, not a guarantee." }));
  }
  automation.append(
    h("p", { class: "small muted", text: "Phone OTP: " + (settings.phone_configured ? "Configured" : "Not configured") })
  );
  automation.append(
    h("p", { class: "small muted", text: "No OTP code is entered in this app. Telegram prompts you when a code is needed; the manual fallback stays available." })
  );

  const status = settingsDirty() ? "Unsaved changes" : "All changes saved";
  automation.append(h("p", { class: "small " + (settingsDirty() ? "warn" : "muted"), text: status + ". Enabled: " + (settings.enabled ? "on" : "off") + " (saved)." }));
  if (state.settingsError) automation.append(h("p", { class: "error", role: "alert", text: state.settingsError }));
  const actions = h("div", { class: "actions" });
  actions.append(actionButton("Save settings", "primary", async () => saveSettings()));
  if (settings.enabled) {
    actions.append(actionButton("Pause", "", async () => saveSettings({ enabled: false })));
  }
  automation.append(actions);
  root.append(card(h("h2", { text: "Automatic mode" }), automation));
}

/* ------------------------------------------------------------------ render */

function fatalCard(error) {
  const wrap = h("div", { class: "center-screen" });
  wrap.append(h("h2", { text: "Open this planner from Telegram" }));
  wrap.append(
    h("p", { class: "muted", text: error && error.message ? error.message : "A valid Telegram session is required." })
  );
  wrap.append(
    h("p", { class: "muted", text: "Launch the bot chat and tap the Open Planner menu button. There is no browser fallback." })
  );
  return wrap;
}

function captureFocusKey() {
  const active = document.activeElement;
  if (!active) return null;
  if (active.id) return active.id;
  const scope = active.closest('[role="dialog"], [role="region"], section[aria-label]');
  return active.tagName === "BUTTON" && scope ? { scope: scope.id, label: active.textContent } : null;
}

/* Every re-render replaces the view subtrees; controls keep stable ids so focus is
 * restored instead of being dropped to <body> (which also forces mobile keyboards shut). */
function restoreFocus(key) {
  if (!key) return;
  const scope = typeof key === "string" ? null : document.getElementById(key.scope);
  if (scope && scope.hidden) return;
  const node = typeof key === "string" ? document.getElementById(key) :
    scope && Array.from(scope.querySelectorAll("button")).find((button) => button.textContent === key.label);
  if (node && node.closest("[hidden]")) return;
  const sheet = document.getElementById("sheet");
  const target = node || (!sheet.hidden && sheet.querySelector("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]")) || document.getElementById("main");
  if (target && target !== document.activeElement) {
    try {
      target.focus({ preventScroll: true });
    } catch (error) {
      target.focus();
    }
  }
}

function setViewBusy(name, on) {
  const node = viewEl(name);
  if (!node) return;
  if (on) node.setAttribute("aria-busy", "true");
  else node.removeAttribute("aria-busy");
}

function render() {
  const focusKey = captureFocusKey();
  renderHeader();
  if (state.fatal) {
    for (const key of Object.keys(VIEW_IDS)) viewEl(key).replaceChildren();
    viewEl(state.view).append(fatalCard(state.fatal));
    document.querySelector(".bottom-nav").hidden = true;
    restoreFocus(focusKey);
    return;
  }
  document.querySelector(".bottom-nav").hidden = false;
  if (state.loadError && !state.me) {
    for (const key of Object.keys(VIEW_IDS)) viewEl(key).replaceChildren();
    const wrap = h("div", { class: "center-screen" });
    wrap.append(h("h2", { text: "Could not load the planner" }));
    wrap.append(h("p", { class: "muted", text: state.loadError }));
    const retry = actionButton("Retry", "primary", async () => load());
    wrap.append(h("div", { class: "actions" }, retry));
    viewEl(state.view).append(wrap);
    restoreFocus(focusKey);
    return;
  }
  renderToday();
  renderPlan();
  renderSettings();
  setViewBusy("today", !state.today || !state.me);
  setViewBusy("plan", !state.plan);
  setViewBusy("settings", !state.me);
  updateClosingGuard();
  restoreFocus(focusKey);
}

/* ------------------------------------------------------------------- boot */

function applyTheme() {
  const root = document.documentElement;
  if (tg && tg.colorScheme) root.dataset.theme = tg.colorScheme;
  const params = tg && tg.themeParams ? tg.themeParams : null;
  const map = {
    bg_color: "--bg",
    secondary_bg_color: "--card",
    section_bg_color: "--card",
    text_color: "--fg",
    subtitle_text_color: "--muted",
    hint_color: "--muted",
    link_color: "--accent",
    button_color: "--accent",
    button_text_color: "--accent-fg",
    destructive_text_color: "--danger",
  };
  if (params) {
    for (const key of Object.keys(map)) {
      if (params[key]) root.style.setProperty(map[key], params[key]);
    }
  }
  if (tg) {
    if (typeof tg.setHeaderColor === "function") tg.setHeaderColor("bg_color");
    if (typeof tg.setBackgroundColor === "function") tg.setBackgroundColor("bg_color");
  }
  const themeColor = getComputedStyle(root).getPropertyValue("--bg").trim();
  if (themeColor) {
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.setAttribute("name", "theme-color");
      document.head.append(meta);
    }
    meta.setAttribute("content", themeColor);
  }
}

/* Telegram supplies safe-area insets that env() cannot see inside its webview. They
 * are written to CSS variables and max()'d with the native env() values in style.css. */
function applySafeArea() {
  if (!tg) return;
  const root = document.documentElement;
  const nativeInset = tg.safeAreaInset || {};
  const contentInset = tg.contentSafeAreaInset || {};
  const top = Math.max(Number(nativeInset.top) || 0, Number(contentInset.top) || 0);
  const bottom = Math.max(Number(nativeInset.bottom) || 0, Number(contentInset.bottom) || 0);
  root.style.setProperty("--tg-top", top + "px");
  root.style.setProperty("--tg-bottom", bottom + "px");
}

/* Visual-viewport height keeps the bottom sheet above the on-screen keyboard. */
function applyViewport() {
  const viewport = window.visualViewport;
  if (viewport) {
    document.documentElement.style.setProperty("--vvh", Math.round(viewport.height) + "px");
  }
}

function boot() {
  applyTheme();
  if (tg) {
    if (typeof tg.onEvent === "function") {
      tg.onEvent("themeChanged", applyTheme);
      tg.onEvent("safeAreaChanged", applySafeArea);
      tg.onEvent("contentSafeAreaChanged", applySafeArea);
      tg.onEvent("viewportChanged", applyViewport);
    }
    if (typeof tg.ready === "function") tg.ready();
    if (typeof tg.expand === "function") tg.expand();
    if (typeof tg.disableVerticalSwipes === "function") tg.disableVerticalSwipes();
    applySafeArea();
  }
  applyViewport();
  if (window.visualViewport && typeof window.visualViewport.addEventListener === "function") {
    window.visualViewport.addEventListener("resize", applyViewport);
  }
  const sheet = document.getElementById("sheet");
  // Keep a focused control reachable when the mobile keyboard covers the sheet.
  sheet.addEventListener("focusin", (event) => {
    const target = event.target;
    if (!target || typeof target.scrollIntoView !== "function") return;
    window.setTimeout(() => target.scrollIntoView({ block: "nearest" }), 250);
  });
  const skipLink = document.querySelector(".skip-link");
  if (skipLink) {
    // Move focus without a hash change, which would otherwise reset the active tab.
    skipLink.addEventListener("click", (event) => {
      event.preventDefault();
      const main = document.getElementById("main");
      if (main) main.focus();
    });
  }
  document.getElementById("sheet-close").addEventListener("click", requestCloseSheet);
  document.getElementById("sheet-backdrop").addEventListener("click", requestCloseSheet);
  applyRoute(true);
  load();
  startBackgroundRefresh();
}

/* One interval for the lifetime of the page; each tick is a no-op while paused. Coming
 * back to the foreground refreshes immediately instead of waiting for the next tick. */
function startBackgroundRefresh() {
  if (!initData || refreshTimer !== null) return;
  refreshTimer = window.setInterval(backgroundRefresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) backgroundRefresh();
  });
}

boot();
