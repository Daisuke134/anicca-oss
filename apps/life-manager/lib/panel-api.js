// LM-33b: authenticated, read-only JSON model for the Life Manager panel.
"use strict";

const crypto = require("node:crypto");
const { cookieValue, csrfToken, panelScopeCookie, sessionScope, sessionUid } = require("./panel-auth.js");
const { buildControlCenter, claimCalendarOAuthState, executeUserCommand, validateCommand } = require("./user-command.js");
const { interpretCalendarEvent } = require("./calendar-interpreter.js");
const { getCalendar } = require("./transport/index.js");
const { lockedDiscoveryGates } = require("./feature-discovery.js");
const { DISCOVERY_STRINGS } = require("./i18n.js");
const { buildScorePeriods, computePanelScores } = require("./panel-score-semantics.js");
const { presentPanelSection } = require("./panel-presentation.js");
const { normalizePhone } = require("./telegram-onboard.js");
const { paymentLink } = require("./payment-link.js");
const { isHelperBlock } = require("./wake-filter.js");
const { projectMoneyPrinter } = require("./money-printer-projection.js");
const { answerHumanTask } = require("./money-printer-human-task.js");
const { createOpportunity } = require("./money-printer-opportunity.js");
const { enqueueBrowserJob } = require("./browser-job-store.js");

const ENDPOINTS = new Set(["money-printer", "timeline", "scores", "ledger", "gates", "settings"]);
const HUMAN_TASK_NEXT_ENDPOINT = "money-printer/human-task/next";
const HUMAN_TASK_ANSWER_ENDPOINT = "money-printer/human-task/answer";
const MONEY_PRINTER_OPPORTUNITY_ENDPOINT = "money-printer/opportunity";
const MONEY_PRINTER_WORKROOM_ENDPOINT = "money-printer/workroom";
const MONEY_PRINTER_FIND_WORK_ENDPOINT = "money-printer/find-work";
const MERCOR_FIND_WORK_GOAL = "Open https://work.mercor.com/explore and list the paid roles that are currently open. "
  + "For each role return its title, its short description, and its pay if shown. Do not log in. "
  + "Stop at any login, OTP, CAPTCHA, KYC, camera, microphone, or personal-experience question.";
const ONBOARDING_ACTIONS = new Set(["name.save", "home.save", "notifications.enable", "phone.save", "phone.skip", "call.enable", "call.skip", "payment.skip"]);
const CALL_MINUTES_BEFORE = Object.freeze([10, 5]);
const SCORE_ORGANS = Object.freeze(["daily", "physical", "mental", "financial"]);
const SORTED_SCORE_ORGANS = Object.freeze([...SCORE_ORGANS].sort());

function headers(key) {
  return { apikey: key, Authorization: `Bearer ${key}` };
}

async function jsonOr(response, fallback) {
  try { return await response.json(); } catch { return fallback; }
}

function configuredTimeZone(value) {
  const candidate = String(value || "Asia/Tokyo");
  try {
    new Intl.DateTimeFormat("en", { timeZone: candidate }).format(0);
    return candidate;
  } catch {
    return "UTC";
  }
}

function scoreUnavailable(reason) {
  const error = new Error(reason);
  error.scoreUnavailableReason = reason;
  return error;
}

function validScoreSnapshotRows(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  if (keys.length !== SCORE_ORGANS.length || keys.some((key, index) => key !== SORTED_SCORE_ORGANS[index])) return false;
  return SCORE_ORGANS.every((organ) => Array.isArray(value[organ]));
}

function zonedParts(ms, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(ms));
  return Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
}

function dateKey(ms, timeZone) {
  const part = zonedParts(ms, timeZone);
  return `${part.year}-${part.month}-${part.day}`;
}

function zonedMidnightMs(key, timeZone) {
  const [year, month, day] = key.split("-").map(Number);
  const wallUtc = Date.UTC(year, month - 1, day);
  let instant = wallUtc;
  for (let pass = 0; pass < 2; pass++) {
    const part = zonedParts(instant, timeZone);
    const represented = Date.UTC(
      Number(part.year), Number(part.month) - 1, Number(part.day),
      Number(part.hour), Number(part.minute), Number(part.second),
    );
    instant = wallUtc - (represented - instant);
  }
  return instant;
}

function todayBounds(nowMs, timeZone) {
  const key = dateKey(nowMs, timeZone);
  const startMs = zonedMidnightMs(key, timeZone);
  const tomorrowKey = dateKey(startMs + 36 * 60 * 60 * 1000, timeZone);
  return { key, startMs, endMs: zonedMidnightMs(tomorrowKey, timeZone) };
}

async function readRows(table, params, opts = {}, optional = false) {
  if (!opts.supaUrl || !opts.supaKey) throw new Error("panel database is not configured");
  const url = new URL(`${String(opts.supaUrl).replace(/\/$/, "")}/rest/v1/${table}`);
  for (const [name, value] of Object.entries(params || {})) url.searchParams.set(name, value);
  const response = await (opts.fetchImpl || fetch)(url.toString(), { headers: headers(opts.supaKey) });
  if (!response.ok) {
    const body = await jsonOr(response, {});
    if (optional && (response.status === 404 || body.code === "PGRST205" || body.code === "42P01")) {
      return { rows: [], missing: true };
    }
    throw new Error(`panel ${table} read failed (${response.status})`);
  }
  const rows = await jsonOr(response, []);
  return { rows: Array.isArray(rows) ? rows : [], missing: false };
}

async function readUser(uid, select, opts) {
  const { rows } = await readRows("lm_users", { uid: `eq.${uid}`, select, limit: "1" }, opts);
  return rows[0] || null;
}

async function readPanelPreferences(uid, opts) {
  const { rows } = await readRows("lm_panel_preferences", { uid: `eq.${uid}`, select: "call_time_zone,call_enabled,notifications_enabled,daily_automation_enabled", limit: "1" }, opts, true);
  return rows[0] || {};
}

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function rounded(value) {
  return Number(value.toFixed(12));
}

async function timeline(uid, opts) {
  const preferences = await readPanelPreferences(uid, opts);
  const timeZone = configuredTimeZone(preferences.call_time_zone || opts.timeZone);
  const nowMs = opts.nowMs == null ? Date.now() : opts.nowMs;
  const bounds = todayBounds(nowMs, timeZone);
  const user = await readUser(uid, "gmail_account_id", opts);
  const calendar = opts.calendar || getCalendar({
    apiKey: opts.composioKey || process.env.COMPOSIO_API_KEY,
    gmailAccountId: user && user.gmail_account_id,
  });
  const rawEvents = await calendar.listEventsRaw(uid, {
    timeMin: new Date(bounds.startMs).toISOString(),
    timeMax: new Date(bounds.endMs).toISOString(),
  });
  const sorted = (Array.isArray(rawEvents) ? rawEvents : []).slice().sort((left, right) => {
    const leftMs = Date.parse((left.start || {}).dateTime || (left.start || {}).date || "");
    const rightMs = Date.parse((right.start || {}).dateTime || (right.start || {}).date || "");
    return leftMs - rightMs;
  });
  const events = sorted.map((event, index) => ({
    id: event.id || "",
    summary: event.summary || "予定",
    start_at: (event.start || {}).dateTime || (event.start || {}).date || null,
    end_at: (event.end || {}).dateTime || (event.end || {}).date || null,
    location: event.location || null,
    interpretation: interpretCalendarEvent(event, {
      now: new Date(nowMs).toISOString(), timeZone,
      previousEvent: index > 0 ? sorted[index - 1] : null,
    }),
  }));
  const { rows: calls } = await readRows("lm_wake_log", {
    uid: `eq.${uid}`,
    called_at: `gte.${new Date(bounds.startMs).toISOString()}`,
    and: `(called_at.lt.${new Date(bounds.endMs).toISOString()})`,
    select: "event_key,called_at,answered_at",
    order: "called_at.asc",
  }, opts);
  return {
    date: bounds.key,
    timezone: timeZone,
    events,
    calls: calls.map((row) => ({
      event_key: row.event_key,
      called_at: row.called_at,
      answered_at: row.answered_at || null,
    })),
  };
}

async function scores(uid, opts) {
  const nowMs = opts.nowMs == null ? Date.now() : opts.nowMs;
  const preferences = await readPanelPreferences(uid, opts);
  const timeZone = configuredTimeZone(preferences.call_time_zone || opts.timeZone);
  let periods;
  try {
    periods = buildScorePeriods(nowMs, timeZone);
  } catch {
    throw scoreUnavailable("period_resolution_failed");
  }
  if (!opts.supaUrl || !opts.supaKey) throw new Error("panel database is not configured");
  const rpcPeriods = Object.fromEntries(SCORE_ORGANS.map((organ) => [organ, {
    start_at: periods[organ].start_at,
    end_at: periods[organ].end_at,
  }]));
  const response = await (opts.fetchImpl || fetch)(`${String(opts.supaUrl).replace(/\/$/, "")}/rest/v1/rpc/lm_panel_score_outcome_snapshot`, {
    method: "POST",
    headers: { ...headers(opts.supaKey), "content-type": "application/json" },
    body: JSON.stringify({ p_uid: uid, p_periods: rpcPeriods }),
  });
  if (!response.ok) {
    await jsonOr(response, {});
    throw scoreUnavailable("source_table_unavailable");
  }
  let snapshot = await jsonOr(response, null);
  if (Array.isArray(snapshot) && snapshot.length === 1) snapshot = snapshot[0];
  if (!snapshot || typeof snapshot !== "object" || snapshot.overflow === true) {
    throw scoreUnavailable(snapshot && snapshot.overflow === true ? "source_outcome_limit" : "source_table_unavailable");
  }
  const rowsByOrgan = snapshot.rows_by_organ;
  if (!validScoreSnapshotRows(rowsByOrgan)) {
    throw scoreUnavailable("source_table_unavailable");
  }
  return { organs: computePanelScores({ uid, ...rowsByOrgan }, periods, timeZone) };
}

function aggregateCosts(rows) {
  const result = { no_data: rows.length === 0, entries: rows.length, total_est_usd: 0, by_kind: {} };
  for (const row of rows) {
    const kind = String(row.kind || "unknown");
    const item = result.by_kind[kind] || { entries: 0, quantity: 0, est_usd: 0 };
    item.entries += 1;
    item.quantity += finite(row.quantity);
    item.est_usd += finite(row.est_usd);
    result.total_est_usd += finite(row.est_usd);
    result.by_kind[kind] = item;
  }
  result.total_est_usd = rounded(result.total_est_usd);
  for (const item of Object.values(result.by_kind)) {
    item.quantity = rounded(item.quantity);
    item.est_usd = rounded(item.est_usd);
  }
  return result;
}

async function ledger(uid, opts) {
  const { rows: costs } = await readRows("lm_api_cost", {
    uid: `eq.${uid}`, select: "ts,kind,quantity,unit,est_usd,meta", order: "ts.desc",
  }, opts);
  const user = await readUser(uid, "agent_wallet_address", opts);
  const wallet = user && String(user.agent_wallet_address || "");
  let financialEntries = [];
  if (wallet) {
    const financial = await readRows("lm_agent_earnings", {
      wallet_address: `eq.${wallet}`,
      select: "entry_key,kind,amount_minor,amount_atomic,amount_decimals,currency,occurred_at,tx_hash,source,meta",
      order: "occurred_at.desc,entry_key.desc",
    }, opts);
    financialEntries = financial.rows;
  }
  const { rows: reportReceipts } = await readRows("lm_financial_report_receipts", {
    uid: `eq.${uid}`,
    status: "eq.sent",
    select: "report_kind,period_key,period_end,snapshot,snapshot_hash,status,telegram_message_id",
    order: "period_end.desc",
    limit: "20",
  }, opts);
  return {
    apiCostEntries: costs,
    financialEntries,
    reportReceipts,
  };
}

async function gates(uid, opts) {
  const nowMs = opts.nowMs == null ? Date.now() : opts.nowMs;
  const user = await readUser(uid, "payout_destination", opts);
  const { rows: locations } = await readRows("lm_user_locations", {
    uid: `eq.${uid}`, select: "uid,observed_at,expires_at", limit: "1",
  }, opts);
  const location = locations[0] || null;
  const locked = new Set(lockedDiscoveryGates({
    location,
    payoutDestination: user && user.payout_destination,
  }, nowMs));
  return { gates: [
    {
      id: "location",
      unlocked: !locked.has("location"),
      unlock_method: DISCOVERY_STRINGS.ja.location.text,
    },
    {
      id: "payout",
      unlocked: !locked.has("payout"),
      unlock_method: DISCOVERY_STRINGS.ja.payout.text,
    },
  ] };
}

async function settings(uid, opts) {
  const user = await readUser(uid,
    "call_language,wake_policy,calendar_provider,gmail_account_id,telegram_chat_id", opts);
  const preferences = await readPanelPreferences(uid, opts);
  let calendar = false;
  try { calendar = opts.scope ? await composioCalendarStatus(opts.scope, { ...opts, composioKey: opts.composioKey || process.env.COMPOSIO_API_KEY }) === "ACTIVE" : false; } catch { calendar = false; }
  return {
    call_language: user && user.call_language != null ? user.call_language : null,
    call_schedule: {
      time_zone: configuredTimeZone(preferences.call_time_zone || opts.timeZone),
      minutes_before: [...CALL_MINUTES_BEFORE],
      wake_policy: user && user.wake_policy != null ? user.wake_policy : "travel-only",
    },
    connections: {
      calendar,
      gmail: Boolean(user && user.gmail_account_id),
      telegram: Boolean(user && user.telegram_chat_id),
    },
  };
}

async function moneyPrinter(scope, opts) {
  if (!scope || !scope.uid || typeof opts.moneyPrinterSource !== "function") {
    throw new Error("money printer source unavailable");
  }
  const input = await opts.moneyPrinterSource(scope);
  if (!input || input.tenantId !== scope.uid) throw new Error("money printer scope mismatch");
  return projectMoneyPrinter(input);
}

function runtimeJobRef(uid, jobId) {
  return `runtime-job://${encodeURIComponent(uid)}/${encodeURIComponent(jobId)}`;
}

function opportunityRef(uid, opportunityId) {
  return `opportunity://${encodeURIComponent(uid)}/${encodeURIComponent(opportunityId)}`;
}

function workroomError(message, status = 502) {
  const error = new Error(message);
  error.status = status;
  return error;
}

async function createMoneyPrinterOpportunity(scope, body, opts) {
  const expectedKeys = ["source_url", "title", "goal_statement", "value_minor", "currency"];
  if (!body || typeof body !== "object" || Array.isArray(body)
    || Object.keys(body).length !== expectedKeys.length
    || Object.keys(body).some((key) => !expectedKeys.includes(key))) {
    throw workroomError("invalid_opportunity", 400);
  }
  let created;
  try {
    created = await createOpportunity({
      tenantId: scope.uid,
      sourceUrl: body.source_url,
      title: body.title,
      goalStatement: body.goal_statement,
      valueMinor: body.value_minor,
      currency: body.currency,
      observedAt: new Date(opts.nowMs == null ? Date.now() : opts.nowMs).toISOString(),
    }, opts.opportunityStore || opts.runtimeStore);
  } catch (error) {
    if (error && /^money printer opportunity (?:input|source URL|title|goal statement|value|currency|observed time) invalid$/.test(error.message)) {
      throw workroomError("invalid_opportunity", 400);
    }
    throw error;
  }
  return {
    opportunity_id: created.opportunity_id,
    job_ref: runtimeJobRef(scope.uid, created.job_id),
    status: created.status,
  };
}

async function createMoneyPrinterFindWork(scope, opts) {
  const enqueue = opts.enqueueBrowserJobImpl || enqueueBrowserJob;
  const stamp = String(opts.nowMs == null ? Date.now() : opts.nowMs);
  const { job } = await enqueue({
    uid: scope.uid,
    chatId: `money-printer:${scope.uid}`,
    messageId: `find-work:${stamp}`,
    updateId: `find-work:${stamp}`,
    rawPrompt: MERCOR_FIND_WORK_GOAL,
    classification: {
      locale: "en",
      goal: MERCOR_FIND_WORK_GOAL,
      actionKind: "explore_listings",
      requiresLogin: false,
      principalKind: "none",
    },
  }, opts);
  return { job_id: job.id, status: job.status };
}

async function workroom(scope, opportunityId, opts) {
  if (typeof opts.moneyPrinterSource !== "function") throw workroomError("workroom unavailable");
  const input = await opts.moneyPrinterSource(scope);
  if (!input || input.tenantId !== scope.uid || !Array.isArray(input.opportunities) || !Array.isArray(input.runtimeJobs)) {
    throw workroomError("workroom unavailable");
  }
  const opportunity = input.opportunities.find((row) => row && row.tenant_id === scope.uid && row.opportunity_id === opportunityId);
  const jobId = `goal:${opportunityId}`;
  const job = input.runtimeJobs.find((row) => row && row.tenant_id === scope.uid && row.job_id === jobId);
  if (!opportunity || !job) throw workroomError("not_found", 404);

  const projected = projectMoneyPrinter({
    ...input,
    opportunities: [opportunity],
    runtimeJobs: [job],
    generalReceipts: [],
    applicationReceipts: [],
    humanTasks: input.humanTasks.filter((row) => row && row.tenant_id === scope.uid && row.job_id === jobId),
    earnings: [],
  });
  const card = Object.values(projected.columns).flat().find((candidate) => candidate.opportunity_ref === opportunityRef(scope.uid, opportunityId));
  if (!card) throw workroomError("workroom unavailable");
  const jobRef = runtimeJobRef(scope.uid, jobId);
  const activity = projected.activity
    .filter((item) => item.ref === card.opportunity_ref || item.ref === jobRef || item.job_ref === jobRef)
    .map((item) => ({
      kind: item.kind, ref: item.ref, ...(item.job_ref ? { job_ref: item.job_ref } : {}),
      status: item.status, observed_at: item.observed_at,
    }));
  if (activity.length === 0) throw workroomError("workroom unavailable");
  return {
    opportunity_id: opportunityId,
    title: card.title,
    value_minor: card.value_minor,
    currency: card.currency,
    source_url: card.source_url,
    status: card.status,
    job_ref: jobRef,
    activity,
  };
}

function humanTaskError(message, status) { const error = new Error(message); error.status = status; return error; }

function safeHumanTask(task, scope) {
  if (task == null) return null;
  if (!task || typeof task !== "object" || Array.isArray(task)
    || (task.uid != null && String(task.uid) !== String(scope.uid))
    || !/^[0-9a-f]{64}$/.test(String(task.task_id || ""))
    || !Number.isInteger(task.version) || task.version < 1 || task.version > 1_000_000
    || typeof task.question !== "string" || !task.question.trim()
    || !(typeof task.required_format === "string"
      || (task.required_format && typeof task.required_format === "object"))
    || typeof task.reason_code !== "string" || !task.reason_code.trim()) {
    throw humanTaskError("human_task_unavailable", 502);
  }
  return {
    task_id: task.task_id,
    version: task.version,
    question: task.question,
    required_format: task.required_format,
    reason_code: task.reason_code,
  };
}

async function readNextHumanTask(scope, store, browserHandoffReader) {
  const reader = store && (store.readNext || store.next || store.readNextHumanTask);
  if (typeof reader !== "function") throw humanTaskError("human_task_unavailable", 502);
  const task = safeHumanTask(await reader.call(store, scope), scope);
  if (!task) return null;
  let available = false;
  if (task.reason_code === "provider_interview" && typeof browserHandoffReader === "function") {
    const handoff = await browserHandoffReader(scope.uid);
    available = Boolean(handoff && handoff.expired === false);
  }
  return { ...task, browser_takeover_available: available };
}

function humanTaskErrorResponse(error) {
  const message = String(error && error.message || "");
  if (message === "invalid_json") return { status: 400, body: { error: "invalid_json" } };
  if (message === "body_too_large") return { status: 413, body: { error: "body_too_large" } };
  if (["idempotency_conflict", "idempotency_in_progress", "idempotency_failed"].includes(message)) {
    return { status: 409, body: { error: message } };
  }
  if (message === "human task scope mismatch") return { status: 401, body: { error: "unauthorized" } };
  if (message === "human task answer conflict" || message === "human task version conflict") {
    return { status: 409, body: { error: "human_task_conflict" } };
  }
  if (message === "provider_unconfirmed") return { status: 409, body: { error: "provider_unconfirmed" } };
  if (message === "human task store unavailable" || message === "human task answer not read back"
    || message === "human_task_unavailable" || message === "panel_store_read_failed" || message === "panel_receipt_unavailable") {
    return { status: 502, body: { error: "human_task_unavailable" } };
  }
  if (message.startsWith("human task ")) return { status: 400, body: { error: "human_task_invalid" } };
  return { status: 502, body: { error: "human_task_unavailable" } };
}

function sendJson(res, status, body, extraHeaders = {}) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    ...extraHeaders,
  });
  res.end(JSON.stringify(body));
}

function sendPanelSection(res, section, candidate, opts) {
  try {
    const transformed = typeof opts.responseCandidateTransform === "function"
      ? opts.responseCandidateTransform(section, candidate)
      : candidate;
    sendJson(res, 200, presentPanelSection(section, transformed));
  } catch (error) {
    if (error && error.code === "section_unavailable" && error.section === section) {
      sendJson(res, 422, { error: "section_unavailable", section });
      return;
    }
    throw error;
  }
}

function onboardingError(message, status) { const error = new Error(message); error.status = status; return error; }
function normalizedOnboardingPhone(value) {
  const raw = String(value || "").trim();
  return /^[+\d()\s.-]+$/.test(raw) ? normalizePhone(raw) : null;
}

async function onboardingRpc(name, body, opts = {}) {
  if (!opts.supaUrl || !opts.supaKey) throw onboardingError("onboarding_unavailable", 502);
  const response = await (opts.fetchImpl || fetch)(`${String(opts.supaUrl).replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
    method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const value = await jsonOr(response, {});
  if (!response.ok) {
    const message = String(value && (value.message || value.error || value.hint) || "");
    if (message.includes("scope_mismatch")) throw onboardingError("unauthorized", 401);
    if (message.includes("onboarding_conflict")) throw onboardingError("onboarding_conflict", 409);
    if (message.includes("invalid_name")) throw onboardingError("invalid_name", 400);
    if (message.includes("invalid_home_address")) throw onboardingError("invalid_home_address", 400);
    if (message.includes("invalid_phone")) throw onboardingError("invalid_phone", 400);
    if (message.includes("invalid_onboarding_action")) throw onboardingError("invalid_action", 400);
    throw onboardingError("onboarding_unavailable", 502);
  }
  return Array.isArray(value) ? value[0] || null : value;
}

async function readCalendarStatus(scope, opts = {}) {
  if (!opts.composioKey && !opts.composioCalendarStatusImpl && !process.env.COMPOSIO_API_KEY) throw onboardingError("calendar_unavailable", 502);
  let status;
  try {
    status = await (opts.composioCalendarStatusImpl || composioCalendarStatus)(scope, { ...opts, composioKey: opts.composioKey || process.env.COMPOSIO_API_KEY });
  } catch (error) {
    if (error && error.status === 401) throw error;
    throw onboardingError("calendar_unavailable", 502);
  }
  if (!["ACTIVE", "MISSING", "DISABLED", "INACTIVE"].includes(status)) throw onboardingError("calendar_unavailable", 502);
  return status;
}

async function refreshCalendar(scope, store, opts = {}) {
  const status = await readCalendarStatus(scope, opts);
  try {
    if (typeof store.syncCalendarStatus === "function") {
      if (await store.syncCalendarStatus(scope, status) === false) throw new Error("calendar_sync_failed");
    } else {
      await onboardingRpc("sync_lm_panel_calendar_status", { p_uid: scope.uid, p_chat_id: scope.chatId, p_status: status }, opts);
    }
  } catch (error) {
    if (error && error.status === 401) throw error;
    throw onboardingError("calendar_unavailable", 502);
  }
  return status;
}

function onboardingMutation(body, pathAction) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw onboardingError("invalid_json", 400);
  const rawAction = body.action !== undefined ? body.action : body.type !== undefined ? body.type : pathAction;
  if (typeof rawAction !== "string" || !ONBOARDING_ACTIONS.has(rawAction.trim())) throw onboardingError("invalid_action", 400);
  const action = rawAction.trim(), hasPayload = Object.hasOwn(body, "payload");
  if (hasPayload && (!body.payload || typeof body.payload !== "object" || Array.isArray(body.payload))) throw onboardingError("invalid_json", 400);
  const payload = hasPayload ? { ...body.payload } : { ...body };
  for (const key of ["action", "type", "payload", "uid", "tg", "telegram_id", "chat_id", "paid", "plan_status", "stripe_customer_id", "stripe_subscription_id", "current_period_end", "stripe_event_at"]) delete payload[key];
  if (hasPayload && Object.keys(body).some((key) => !["action", "type", "payload", "uid", "tg", "telegram_id", "chat_id", "paid", "plan_status", "stripe_customer_id", "stripe_subscription_id", "current_period_end", "stripe_event_at"].includes(key))) throw onboardingError("invalid_json", 400);
  const allowed = action === "name.save" ? new Set(["name"]) : action === "home.save" ? new Set(["home_address", "homeAddress"]) : action === "phone.save" ? new Set(["phone"]) : new Set();
  if (Object.keys(payload).some((key) => !allowed.has(key))) throw onboardingError("invalid_json", 400);
  if (action === "name.save") { if (typeof payload.name !== "string") throw onboardingError("invalid_name", 400); const name = payload.name.trim(); if (!name || name.length > 120) throw onboardingError("invalid_name", 400); payload.name = name; }
  if (action === "home.save") { const rawHome = payload.home_address !== undefined ? payload.home_address : payload.homeAddress; if (typeof rawHome !== "string") throw onboardingError("invalid_home_address", 400); const home = rawHome.trim(); if (!home || home.length > 240) throw onboardingError("invalid_home_address", 400); payload.home_address = home; delete payload.homeAddress; }
  if (action === "phone.save") { if (typeof payload.phone !== "string") throw onboardingError("invalid_phone", 400); const phone = normalizedOnboardingPhone(payload.phone); if (!phone) throw onboardingError("invalid_phone", 400); payload.phone = phone; }
  return { action, payload };
}

function onboardingResponse(value, opts = {}, scope = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw onboardingError("onboarding_unavailable", 502);
  const body = {};
  for (const key of ["step", "stage", "name", "calendarConnected", "homeAddress", "notificationsEnabled", "phone", "callEnabled", "paid"]) {
    if (Object.hasOwn(value, key)) body[key] = value[key];
  }
  const aliases = { payment: "dashboard", pay: "dashboard", done: "dashboard", gmail: "dashboard" };
  body.step = aliases[String(body.step || body.stage || "")] || String(body.step || body.stage || "");
  return body;
}

async function addNextEvent(body, scope, opts) {
  body.nextEvent = null;
  const nowMs = opts.nowMs == null ? Date.now() : opts.nowMs;
  try {
    const result = await timeline(scope.uid, { ...opts, nowMs });
    const next = (result && Array.isArray(result.events) ? result.events : []).find((item) => {
      if (!item || isHelperBlock(item.summary)) return false;
      const startMs = Date.parse(String(item.start_at || ""));
      return Number.isFinite(startMs) && startMs > nowMs;
    });
    if (next) body.nextEvent = { summary: String(next.summary || "予定"), startAt: next.start_at };
  } catch { body.nextEvent = null; }
  return body;
}

function onboardingRequestHash(parsed) {
  return mutationRequestHash(parsed.action, parsed.payload, "payload");
}

function onboardingReceiptConflict(message) { return onboardingError(message, 409); }

function mutationRequestHash(action, body, bodyKey = "body") {
  return crypto.createHash("sha256").update(JSON.stringify({ action, [bodyKey]: body })).digest("hex");
}

function replayMutationReceipt(receipt, requestHash) {
  if (!receipt) return null;
  const storedHash = String(receipt.requestHash || receipt.request_hash || "");
  if (storedHash !== requestHash) throw onboardingReceiptConflict("idempotency_conflict");
  if (receipt.status === "succeeded" && receipt.result && typeof receipt.result === "object") return receipt.result;
  if (receipt.status === "pending") throw onboardingReceiptConflict("idempotency_in_progress");
  throw onboardingReceiptConflict("idempotency_failed");
}

function replayOnboardingReceipt(receipt, requestHash) {
  return replayMutationReceipt(receipt, requestHash);
}

async function claimMutationReceipt(scope, key, action, body, store, requestHash = mutationRequestHash(action, body), commandType = action) {
  if (!store || typeof store.readReceipt !== "function" || typeof store.claimReceipt !== "function" || typeof store.finishReceipt !== "function") {
    throw onboardingError("panel_receipt_unavailable", 502);
  }
  const existing = await store.readReceipt(scope, key);
  const replay = replayMutationReceipt(existing, requestHash);
  if (replay) return { requestHash, replay, claimed: false };
  const claimed = await store.claimReceipt(scope, key, { requestHash, commandType, status: "pending" });
  if (claimed) return { requestHash, replay: null, claimed: true };
  const raced = await store.readReceipt(scope, key);
  const racedReplay = replayMutationReceipt(raced, requestHash);
  if (racedReplay) return { requestHash, replay: racedReplay, claimed: false };
  throw onboardingReceiptConflict("idempotency_in_progress");
}

async function claimOnboardingReceipt(scope, key, parsed, store) {
  return claimMutationReceipt(scope, key, parsed.action, parsed.payload, store, onboardingRequestHash(parsed), "onboarding.transition");
}

async function readJson(req) {
  return new Promise((resolve, reject) => {
    let raw = "", settled = false;
    const noop = () => {};
    const cleanup = () => { req.removeListener("data", onData); req.removeListener("end", onEnd); req.removeListener("error", onError); req.on("error", noop); };
    const fail = error => { if (settled) return; settled = true; raw = ""; cleanup(); reject(error); };
    const onData = chunk => { if (settled) return; raw += chunk; if (Buffer.byteLength(raw) > 32 * 1024) fail(Object.assign(new Error("body_too_large"), { status: 413 })); };
    const onEnd = () => { if (settled) return; settled = true; cleanup(); try { resolve(JSON.parse(raw || "{}")); } catch { reject(Object.assign(new Error("invalid_json"), { status: 400 })); } };
    const onError = error => fail(error);
    req.on("data", onData); req.on("end", onEnd); req.on("error", onError);
  });
}

function timingEqual(left, right) {
  const a = Buffer.from(String(left || "")), b = Buffer.from(String(right || ""));
  return a.length === b.length && a.length > 0 && require("node:crypto").timingSafeEqual(a, b);
}

function createSupabaseCommandStore(opts = {}) {
  const base = String(opts.supaUrl || "").replace(/\/$/, "");
  const fetchImpl = opts.fetchImpl || fetch;
  async function rows(table, query) {
    const response = await fetchImpl(`${base}/rest/v1/${table}?${query}`, { headers: headers(opts.supaKey) });
    if (!response.ok) throw new Error("panel_store_read_failed");
    const body = await jsonOr(response, []); return Array.isArray(body) ? body : [];
  }
  async function patch(table, scope, body) {
    const response = await fetchImpl(`${base}/rest/v1/${table}?uid=eq.${encodeURIComponent(scope.uid)}`, { method: "PATCH", headers: { ...headers(opts.supaKey), "content-type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ ...body, updated_at: new Date().toISOString() }) });
    if (!response.ok) throw new Error("panel_store_write_failed");
    const result = await jsonOr(response, []); return result[0] || body;
  }
  return {
    async assertCurrentScope(scope) { return Boolean((await rows("lm_users", new URLSearchParams({ uid: `eq.${scope.uid}`, telegram_chat_id: `eq.${scope.chatId}`, select: "uid", limit: "1" })))[0]); },
    async readUser(scope) { return (await rows("lm_users", new URLSearchParams({ uid: `eq.${scope.uid}`, telegram_chat_id: `eq.${scope.chatId}`, select: "uid,name,telegram_chat_id,phone,call_language,wake_policy,calendar_provider,gmail_account_id,payout_destination", limit: "1" })))[0] || null; },
    async readPreferences(scope) { return (await rows("lm_panel_preferences", new URLSearchParams({ uid: `eq.${scope.uid}`, select: "call_enabled,notifications_enabled,daily_automation_enabled,delegation_enabled,call_time_zone", limit: "1" })))[0] || {}; },
    async readLocation(scope) { return (await rows("lm_user_locations", new URLSearchParams({ uid: `eq.${scope.uid}`, select: "observed_at,expires_at", limit: "1" })))[0] || null; },
    async readReceipt(scope, key) { const row = (await rows("lm_panel_command_receipts", new URLSearchParams({ uid: `eq.${scope.uid}`, chat_id: `eq.${scope.chatId}`, idempotency_key: `eq.${key}`, select: "request_hash,status,result", limit: "1" })))[0]; return row ? { requestHash: row.request_hash, status: row.status, result: row.result } : null; },
    async claimReceipt(scope, key, value) { const response = await fetchImpl(`${base}/rest/v1/lm_panel_command_receipts`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json", Prefer: "return=minimal" }, body: JSON.stringify({ uid: scope.uid, chat_id: scope.chatId, idempotency_key: key, request_hash: value.requestHash, command_type: value.commandType, status: value.status }) }); if (response.status === 409) return false; if (!response.ok) throw new Error("panel_receipt_failed"); return true; },
    async finishReceipt(scope, key, value) { const response = await fetchImpl(`${base}/rest/v1/lm_panel_command_receipts?uid=eq.${encodeURIComponent(scope.uid)}&chat_id=eq.${encodeURIComponent(scope.chatId)}&idempotency_key=eq.${encodeURIComponent(key)}`, { method: "PATCH", headers: { ...headers(opts.supaKey), "content-type": "application/json", Prefer: "return=minimal" }, body: JSON.stringify({ status: value.status, result: value.result, updated_at: new Date().toISOString() }) }); if (!response.ok) throw new Error("panel_receipt_failed"); },
    async patchPreferences(scope, body) { const existing = await this.readPreferences(scope); if (!Object.keys(existing).length) { const response = await fetchImpl(`${base}/rest/v1/lm_panel_preferences`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json", Prefer: "resolution=merge-duplicates,return=representation" }, body: JSON.stringify({ uid: scope.uid, ...body }) }); if (!response.ok) throw new Error("panel_store_write_failed"); const result = await jsonOr(response, []); return result[0] || { ...existing, ...body }; } return patch("lm_panel_preferences", scope, body); },
    async patchUser(scope, body) { return patch("lm_users", scope, body); },
    async mutatePreferences(scope, body) { const response = await fetchImpl(`${base}/rest/v1/rpc/mutate_lm_panel_preferences`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify({ p_uid: scope.uid, p_chat_id: scope.chatId, p_patch: body }) }); if (!response.ok) throw new Error("scope_mismatch"); return jsonOr(response, body); },
    async mutateUser(scope, body) { const response = await fetchImpl(`${base}/rest/v1/rpc/mutate_lm_panel_user`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify({ p_uid: scope.uid, p_chat_id: scope.chatId, p_patch: body }) }); if (!response.ok) throw new Error("scope_mismatch"); return jsonOr(response, body); },
    async readOnboardingState(scope) { return onboardingRpc("lm_panel_onboarding_state", { p_uid: scope.uid, p_chat_id: scope.chatId }, opts); },
    async mutateOnboarding(scope, action, payload) { return onboardingRpc("lm_panel_onboarding_transition", { p_uid: scope.uid, p_chat_id: scope.chatId, p_action: action, p_payload: payload || {} }, opts); },
    async syncCalendarStatus(scope, status) { return onboardingRpc("sync_lm_panel_calendar_status", { p_uid: scope.uid, p_chat_id: scope.chatId, p_status: status }, opts); },
    async mutateOnboardingWithCalendar(scope, status, action, payload) { return onboardingRpc("lm_panel_onboarding_transition_with_calendar", { p_uid: scope.uid, p_chat_id: scope.chatId, p_status: status, p_action: action, p_payload: payload || {} }, opts); },
    async createOAuthState(scope, state) { const response = await fetchImpl(`${base}/rest/v1/rpc/create_lm_panel_oauth_state`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify({ p_state_hash: state.stateHash, p_uid: scope.uid, p_chat_id: scope.chatId, p_provider: state.provider, p_expires_at: state.expiresAt }) }); if (!response.ok) throw new Error("oauth_state_failed"); const value = await jsonOr(response, false); const claimed = Array.isArray(value) ? value[0] === true : value === true; if (!claimed) { const error = new Error("oauth_state_in_progress"); error.status = 409; throw error; } return true; },
    async createTelegramOAuthState(scope, state) { const response = await fetchImpl(`${base}/rest/v1/rpc/create_lm_telegram_oauth_state`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify({ p_state_hash: state.stateHash, p_uid: scope.uid, p_chat_id: scope.chatId, p_expires_at: state.expiresAt }) }); if (!response.ok) throw new Error("oauth_state_failed"); const value = await jsonOr(response, false); const created = Array.isArray(value) ? value[0] === true : value === true; if (!created) throw new Error("oauth_state_failed"); return true; },
    async claimOAuthState(scope, stateHash) { const response = await fetchImpl(`${base}/rest/v1/rpc/claim_lm_panel_oauth_state`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify({ p_state_hash: stateHash, p_uid: scope.uid, p_chat_id: scope.chatId }) }); if (!response.ok) throw new Error("oauth_state_failed"); return jsonOr(response, false); },
    async claimTelegramOAuthState(stateHash) { const response = await fetchImpl(`${base}/rest/v1/rpc/claim_lm_telegram_oauth_state`, { method: "POST", headers: { ...headers(opts.supaKey), "content-type": "application/json" }, body: JSON.stringify({ p_state_hash: stateHash }) }); if (!response.ok) throw new Error("oauth_state_failed"); const value = await jsonOr(response, []); return Array.isArray(value) ? value[0] || null : value || null; },
  };
}

async function handleTelegramOAuthCallback(req, res, opts = {}) {
  if (req.method !== "GET") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET" }); return; }
  const url = new URL(req.url || "/", "http://telegram.local");
  const state = String(url.searchParams.get("state") || "");
  if (!/^[A-Za-z0-9_-]{43}$/.test(state)) { res.writeHead(403, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" }); res.end("invalid connection"); return; }
  const store = opts.commandStore || createSupabaseCommandStore(opts);
  const claimed = await store.claimTelegramOAuthState(crypto.createHash("sha256").update(state).digest("hex"));
  const scope = claimed && { uid: String(claimed.uid || ""), chatId: String(claimed.chat_id || "") };
  if (!scope || !scope.uid || !/^[1-9][0-9]{0,19}$/.test(scope.chatId) || !await store.assertCurrentScope(scope)) {
    res.writeHead(403, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" }); res.end("connection expired"); return;
  }
  const status = await (opts.composioCalendarStatusImpl || composioCalendarStatus)(scope, opts);
  if (status !== "ACTIVE") { res.writeHead(403, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" }); res.end("calendar connection not verified"); return; }
  await store.syncCalendarStatus(scope, status);
  const ja = /^ja(?:-|$)/i.test(String(url.searchParams.get("lang") || ""));
  const sent = await opts.sendMessage(scope.chatId, ja
    ? "Google Calendarを接続しました。\n\n自宅の住所を教えてください。"
    : "Google Calendar is connected.\n\nWhat is your home address?");
  if (!sent || sent.ok !== true) throw new Error("telegram_callback_send_failed");
  const returnUrl = new URL(String(opts.telegramReturnUrl || ""));
  if (returnUrl.protocol !== "https:" || returnUrl.hostname !== "t.me" || returnUrl.username || returnUrl.password
    || !/^\/[A-Za-z][A-Za-z0-9_]{4,31}$/.test(returnUrl.pathname) || returnUrl.search || returnUrl.hash) throw new Error("telegram_return_unavailable");
  res.writeHead(303, { Location: returnUrl.toString(), "cache-control": "no-store", "referrer-policy": "no-referrer" });
  res.end();
}

async function handlePanelOAuthCallback(req, res, opts = {}) {
  if (req.method !== "GET") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET" }); return; }
  const session = cookieValue(req.headers.cookie, "__Host-lm_panel_session") || cookieValue(req.headers.cookie, "lm_panel_session");
  const scope = await (opts.sessionScopeImpl || sessionScope)(session, opts);
  if (!scope) { res.writeHead(401, { "content-type": "text/plain", "cache-control": "no-store" }); res.end("unauthorized"); return; }
  const renewedCookie = panelScopeCookie(scope);
  if (renewedCookie && typeof res.setHeader === "function") res.setHeader("Set-Cookie", renewedCookie);
  const state = new URL(req.url || "/", "http://panel.local").searchParams.get("state");
  const store = opts.commandStore || createSupabaseCommandStore(opts);
  if (store.assertCurrentScope && !await store.assertCurrentScope(scope)) { res.writeHead(401, { "content-type": "text/plain", "cache-control": "no-store" }); res.end("unauthorized"); return; }
  const claimed = await claimCalendarOAuthState(scope, state, { store });
  let verified = false;
  let location = "/panel";
  if (claimed) {
    try { verified = await (opts.composioCalendarStatusImpl || composioCalendarStatus)(scope, opts) === "ACTIVE"; } catch { verified = false; }
    if (verified && typeof store.readOnboardingState === "function") {
      try {
        const onboarding = await store.readOnboardingState(scope);
        const step = String(onboarding && (onboarding.step || onboarding.stage) || "");
        if (step && step !== "dashboard" && step !== "done") location = "/panel/onboarding";
      } catch { location = "/panel"; }
    }
  }
  res.writeHead(verified ? 303 : 403, { ...(verified ? { Location: location } : {}), "cache-control": "no-store", "referrer-policy": "no-referrer" });
  res.end(verified ? "" : "calendar connection not verified");
}

function exactCalendarAccount(scope, item) {
  const owner = item && (item.user_id || item.userId || item.connection?.user_id);
  const toolkit = item && (item.toolkit_slug || item.toolkit?.slug || item.toolkit?.slug_name);
  return Boolean(item && item.id && String(owner) === String(scope.uid) && toolkit === "googlecalendar");
}

function sameEnabledCalendarAccount(item, id) {
  return Boolean(item && item.id === id && item.status === "ACTIVE" && item.is_disabled !== true
    && (item.enabled === undefined || item.enabled === true));
}

function sameDisabledCalendarAccount(item, id) {
  return Boolean(item && item.id === id && item.status !== "ACTIVE" && item.is_disabled === true
    && (item.enabled === undefined || item.enabled === false));
}

async function composioCalendarStatus(scope, opts = {}) {
  if (!opts.composioKey) throw new Error("provider_unavailable");
  const response = await (opts.fetchImpl || fetch)(`https://backend.composio.dev/api/v3/connected_accounts?user_ids=${encodeURIComponent(scope.uid)}&toolkit_slugs=googlecalendar`, { headers: { "x-api-key": opts.composioKey } });
  if (!response.ok) throw new Error("provider_failed");
  const body = await jsonOr(response, {});
  const items = Array.isArray(body.items) ? body.items : [];
  if (items.length > 1) throw new Error("provider_ambiguous");
  if (items.length === 0) return "MISSING";
  if (!exactCalendarAccount(scope, items[0])) throw new Error("provider_ownership");
  return items[0].status === "ACTIVE" && items[0].is_disabled !== true
    && (items[0].enabled === undefined || items[0].enabled === true) ? "ACTIVE" : "DISABLED";
}

async function composioCalendarAccounts(scope, opts = {}) {
  if (!opts.composioKey) throw new Error("provider_unavailable");
  const url = `https://backend.composio.dev/api/v3/connected_accounts?user_ids=${encodeURIComponent(scope.uid)}&toolkit_slugs=googlecalendar`;
  const response = await (opts.fetchImpl || fetch)(url, { headers: { "x-api-key": opts.composioKey } });
  if (!response.ok) throw new Error("provider_failed");
  const body = await jsonOr(response, {});
  const items = Array.isArray(body.items) ? body.items : [];
  if (items.some(item => !exactCalendarAccount(scope, item))) throw new Error("provider_ownership");
  return items;
}

async function composioCalendarDisconnect(scope, opts = {}) {
  const accounts = await composioCalendarAccounts(scope, opts);
  if (accounts.length === 0) return { provider: "calendar", state: "action_required" };
  if (accounts.length !== 1 || !accounts[0].id) throw new Error("provider_ambiguous");
  const account = accounts[0];
  if (account.status !== "ACTIVE" || account.is_disabled === true || account.enabled === false) return { provider: "calendar", state: "action_required" };
  const response = await (opts.fetchImpl || fetch)(`https://backend.composio.dev/api/v3/connected_accounts/${encodeURIComponent(account.id)}/status`, {
    method: "PATCH",
    headers: { "x-api-key": opts.composioKey, "content-type": "application/json" },
    body: JSON.stringify({ enabled: false }),
  });
  if (!response.ok) throw new Error("provider_failed");
  const readback = await composioCalendarAccounts(scope, opts);
  if (readback.length !== 1 || !sameDisabledCalendarAccount(readback[0], account.id)) {
    const rollback = await (opts.fetchImpl || fetch)(`https://backend.composio.dev/api/v3/connected_accounts/${encodeURIComponent(account.id)}/status`, { method: "PATCH", headers: { "x-api-key": opts.composioKey, "content-type": "application/json" }, body: JSON.stringify({ enabled: true }) });
    if (!rollback.ok) throw new Error("provider_rollback_failed");
    const restored = await composioCalendarAccounts(scope, opts);
    if (restored.length !== 1 || !sameEnabledCalendarAccount(restored[0], account.id)) throw new Error("provider_rollback_failed");
    throw new Error("provider_readback_failed");
  }
  return { provider: "calendar", state: "action_required" };
}

async function composioCalendarStart(scope, opts = {}) {
  const accounts = await composioCalendarAccounts(scope, opts);
  if (accounts.length === 0) return null;
  if (accounts.length !== 1 || !accounts[0].id) throw new Error("provider_ambiguous");
  const account = accounts[0];
  if (account.status === "ACTIVE" && account.is_disabled !== true
    && (account.enabled === undefined || account.enabled === true)) return { provider: "calendar", state: "connected" };
  if (account.is_disabled !== true && account.enabled !== false) return null;
  const response = await (opts.fetchImpl || fetch)(`https://backend.composio.dev/api/v3/connected_accounts/${encodeURIComponent(account.id)}/status`, {
    method: "PATCH",
    headers: { "x-api-key": opts.composioKey, "content-type": "application/json" },
    body: JSON.stringify({ enabled: true }),
  });
  if (!response.ok) throw new Error("provider_failed");
  const readback = await composioCalendarAccounts(scope, opts);
  if (readback.length !== 1 || !sameEnabledCalendarAccount(readback[0], account.id)) throw new Error("provider_readback_failed");
  return { provider: "calendar", state: "connected" };
}

async function handlePanelApiRequest(req, res, opts = {}) {
  const requestUrl = new URL(req.url || "/", "http://panel.local");
  const path = requestUrl.pathname;
  const endpoint = path.startsWith("/api/panel/") ? path.slice("/api/panel/".length) : "";
  const onboardingEndpoint = endpoint === "onboarding" || endpoint.startsWith("onboarding/");
  const humanTaskNextEndpoint = endpoint === HUMAN_TASK_NEXT_ENDPOINT;
  const humanTaskAnswerEndpoint = endpoint === HUMAN_TASK_ANSWER_ENDPOINT;
  if (!ENDPOINTS.has(endpoint) && endpoint !== "control-center" && endpoint !== "commands"
    && !onboardingEndpoint && !humanTaskNextEndpoint && !humanTaskAnswerEndpoint
    && endpoint !== MONEY_PRINTER_OPPORTUNITY_ENDPOINT && endpoint !== MONEY_PRINTER_WORKROOM_ENDPOINT
    && endpoint !== MONEY_PRINTER_FIND_WORK_ENDPOINT) {
    sendJson(res, 404, { error: "not_found" });
    return;
  }

  const session = cookieValue(req.headers.cookie, "__Host-lm_panel_session") || cookieValue(req.headers.cookie, "lm_panel_session");
  const nowMs = opts.nowMs == null ? Date.now() : opts.nowMs;
  let scope;
  if (opts.sessionScopeImpl) scope = await opts.sessionScopeImpl(session, opts);
  else if (opts.sessionUidImpl) { const uid = await opts.sessionUidImpl(session, opts); scope = uid ? { uid, chatId: String(opts.sessionChatId || "legacy") } : null; }
  else scope = await sessionScope(session, {
    supaUrl: opts.supaUrl,
    supaKey: opts.supaKey,
    fetchImpl: opts.fetchImpl,
    now: () => new Date(nowMs),
  });
  if (!scope) {
    sendJson(res, 401, { error: "unauthorized" });
    return;
  }
  const renewedCookie = panelScopeCookie(scope);
  if (renewedCookie && typeof res.setHeader === "function") res.setHeader("Set-Cookie", renewedCookie);
  const commandStore = opts.commandStore || createSupabaseCommandStore(opts);
  if (!opts.sessionScopeImpl && !await commandStore.assertCurrentScope(scope)) {
    sendJson(res, 401, { error: "unauthorized" });
    return;
  }
  if (endpoint === MONEY_PRINTER_OPPORTUNITY_ENDPOINT) {
    if (req.method !== "POST") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "POST" }); return; }
    if (!/^application\/json(?:;|$)/i.test(String(req.headers["content-type"] || ""))) { sendJson(res, 415, { error: "json_required" }); return; }
    const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
    if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) { sendJson(res, 403, { error: "origin_rejected" }); return; }
    if (!timingEqual(req.headers["x-lm-csrf"], scope.csrf || csrfToken(session))) { sendJson(res, 403, { error: "csrf_rejected" }); return; }
    const key = String(req.headers["idempotency-key"] || "");
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) { sendJson(res, 400, { error: "idempotency_required" }); return; }
    let claimedReceipt = null;
    try {
      const body = await readJson(req);
      if (!body || typeof body !== "object" || Array.isArray(body)
        || Object.keys(body).length !== 5
        || Object.keys(body).some((name) => !["source_url", "title", "goal_statement", "value_minor", "currency"].includes(name))) {
        throw workroomError("invalid_opportunity", 400);
      }
      const receipt = await claimMutationReceipt(scope, key, "money-printer.opportunity.create", body, commandStore);
      if (receipt.replay) { sendJson(res, 200, receipt.replay); return; }
      claimedReceipt = receipt.claimed ? receipt : null;
      const result = await createMoneyPrinterOpportunity(scope, body, opts);
      await commandStore.finishReceipt(scope, key, { requestHash: receipt.requestHash, commandType: "money-printer.opportunity.create", status: "succeeded", result });
      claimedReceipt = null;
      sendJson(res, 200, result);
    } catch (error) {
      if (claimedReceipt) {
        try { await commandStore.finishReceipt(scope, key, { requestHash: claimedReceipt.requestHash, commandType: "money-printer.opportunity.create", status: "failed", result: null }); } catch { /* pending keeps retries blocked */ }
      }
      const status = error && error.status || 502;
      const errorCode = ["idempotency_conflict", "idempotency_in_progress", "idempotency_failed"].includes(error && error.message)
        ? error.message : error && ["invalid_json", "body_too_large"].includes(error.message) ? error.message : status === 400 ? "invalid_opportunity" : "opportunity_unavailable";
      sendJson(res, status, { error: errorCode });
    }
    return;
  }
  if (endpoint === MONEY_PRINTER_FIND_WORK_ENDPOINT) {
    if (req.method !== "POST") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "POST" }); return; }
    if (!/^application\/json(?:;|$)/i.test(String(req.headers["content-type"] || ""))) { sendJson(res, 415, { error: "json_required" }); return; }
    const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
    if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) { sendJson(res, 403, { error: "origin_rejected" }); return; }
    if (!timingEqual(req.headers["x-lm-csrf"], scope.csrf || csrfToken(session))) { sendJson(res, 403, { error: "csrf_rejected" }); return; }
    const key = String(req.headers["idempotency-key"] || "");
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) { sendJson(res, 400, { error: "idempotency_required" }); return; }
    let claimedReceipt = null;
    try {
      const body = await readJson(req);
      if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 0) {
        throw workroomError("invalid_find_work", 400);
      }
      const receipt = await claimMutationReceipt(scope, key, "money-printer.find-work.create", body, commandStore);
      if (receipt.replay) { sendJson(res, 200, receipt.replay); return; }
      claimedReceipt = receipt.claimed ? receipt : null;
      const result = await createMoneyPrinterFindWork(scope, opts);
      await commandStore.finishReceipt(scope, key, { requestHash: receipt.requestHash, commandType: "money-printer.find-work.create", status: "succeeded", result });
      claimedReceipt = null;
      sendJson(res, 200, result);
    } catch (error) {
      if (claimedReceipt) {
        try { await commandStore.finishReceipt(scope, key, { requestHash: claimedReceipt.requestHash, commandType: "money-printer.find-work.create", status: "failed", result: null }); } catch { /* pending keeps retries blocked */ }
      }
      const status = error && error.status || 502;
      const errorCode = ["idempotency_conflict", "idempotency_in_progress", "idempotency_failed"].includes(error && error.message)
        ? error.message : error && ["invalid_json", "body_too_large"].includes(error.message) ? error.message : status === 400 ? "invalid_find_work" : "find_work_unavailable";
      sendJson(res, status, { error: errorCode });
    }
    return;
  }
  if (endpoint === MONEY_PRINTER_WORKROOM_ENDPOINT) {
    if (req.method !== "GET") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET" }); return; }
    const opportunityId = requestUrl.searchParams.get("opportunity_id");
    if (!/^[0-9a-f]{64}$/.test(String(opportunityId || ""))) { sendJson(res, 400, { error: "invalid_opportunity_id" }); return; }
    try {
      sendJson(res, 200, await workroom(scope, opportunityId, opts));
    } catch (error) {
      const status = error && error.status || 502;
      sendJson(res, status, { error: status === 404 ? "not_found" : "workroom_unavailable" });
    }
    return;
  }
  if (humanTaskNextEndpoint || humanTaskAnswerEndpoint) {
    const humanTaskStore = opts.humanTaskStore || opts.runtimeStore;
    if (humanTaskNextEndpoint) {
      if (req.method !== "GET") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET" }); return; }
      try {
        sendJson(res, 200, { task: await readNextHumanTask(scope, humanTaskStore, opts.browserHandoffReader) });
      } catch (error) {
        const failure = humanTaskErrorResponse(error);
        sendJson(res, failure.status, failure.body);
      }
      return;
    }
    if (req.method !== "POST") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "POST" }); return; }
    if (!/^application\/json(?:;|$)/i.test(String(req.headers["content-type"] || ""))) { sendJson(res, 415, { error: "json_required" }); return; }
    const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
    if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) { sendJson(res, 403, { error: "origin_rejected" }); return; }
    if (!timingEqual(req.headers["x-lm-csrf"], scope.csrf || csrfToken(session))) { sendJson(res, 403, { error: "csrf_rejected" }); return; }
    const key = String(req.headers["idempotency-key"] || "");
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) { sendJson(res, 400, { error: "idempotency_required" }); return; }
    let claimedReceipt = null;
    try {
      const body = await readJson(req);
      if (!body || typeof body !== "object" || Array.isArray(body)
        || Object.keys(body).some((name) => !["task_id", "version", "answer_ref", "answer"].includes(name))) {
        throw humanTaskError("invalid_json", 400);
      }
      const answerRef = typeof body.answer_ref === "string"
        ? body.answer_ref
        : ["approve", "request_changes"].includes(body.answer)
          ? `vault-answer://${scope.uid}/${body.answer}` : null;
      if (!answerRef) throw humanTaskError("invalid_json", 400);
      const receipt = await claimMutationReceipt(scope, key, "money-printer.human-task.answer", body, commandStore);
      if (receipt.replay) { sendJson(res, 200, receipt.replay); return; }
      claimedReceipt = receipt.claimed ? receipt : null;
      if (typeof body.answer === "string" && typeof opts.completeBrowserHandoff === "function") {
        const handoff = typeof opts.browserHandoffReader === "function"
          ? await opts.browserHandoffReader(scope.uid) : null;
        if (handoff && handoff.expired === false) await opts.completeBrowserHandoff(scope.uid, body.answer);
        else if (body.answer === "approve") throw new Error("browser handoff unavailable");
      }
      const result = await answerHumanTask({
        scope,
        taskId: body.task_id,
        version: body.version,
        answerRef,
      }, humanTaskStore);
      await commandStore.finishReceipt(scope, key, { requestHash: receipt.requestHash, commandType: "money-printer.human-task.answer", status: "succeeded", result });
      claimedReceipt = null;
      sendJson(res, 200, result);
    } catch (error) {
      if (claimedReceipt) {
        try { await commandStore.finishReceipt(scope, key, { requestHash: claimedReceipt.requestHash, commandType: "money-printer.human-task.answer", status: "failed", result: null }); } catch { /* pending keeps retries blocked */ }
      }
      const failure = humanTaskErrorResponse(error);
      sendJson(res, failure.status, failure.body);
    }
    return;
  }
  if (onboardingEndpoint) {
    if (commandStore.assertCurrentScope && !await commandStore.assertCurrentScope(scope)) { sendJson(res, 401, { error: "unauthorized" }); return; }
    if (req.method === "GET") {
      if (endpoint !== "onboarding") { sendJson(res, 404, { error: "not_found" }); return; }
      try {
        await refreshCalendar(scope, commandStore, opts);
        const state = await (commandStore.readOnboardingState || (() => { throw onboardingError("onboarding_unavailable", 502); }))(scope);
        const body = onboardingResponse(state, opts, scope);
        if (body.step === "dashboard") await addNextEvent(body, scope, opts);
        sendJson(res, 200, body);
      } catch (error) { sendJson(res, error.status || 502, { error: ["payment_unavailable", "unauthorized", "calendar_unavailable"].includes(error.message) ? error.message : "onboarding_unavailable" }); }
      return;
    }
    if (req.method !== "POST") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET, POST" }); return; }
    const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
    if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) { sendJson(res, 403, { error: "origin_rejected" }); return; }
    if (!/^application\/json(?:;|$)/i.test(String(req.headers["content-type"] || ""))) { sendJson(res, 415, { error: "json_required" }); return; }
    if (!timingEqual(req.headers["x-lm-csrf"], scope.csrf || csrfToken(session))) { sendJson(res, 403, { error: "csrf_rejected" }); return; }
    const key = String(req.headers["idempotency-key"] || "");
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) { sendJson(res, 400, { error: "idempotency_required" }); return; }
    let claimedReceipt = false, parsed;
    try {
      const pathAction = endpoint.slice("onboarding/".length).replace(/\//g, ".");
      parsed = onboardingMutation(await readJson(req), pathAction);
      const receipt = await claimOnboardingReceipt(scope, key, parsed, commandStore);
      if (receipt.replay) { sendJson(res, 200, receipt.replay); return; }
      claimedReceipt = receipt.claimed;
      const providerStatus = await readCalendarStatus(scope, opts);
      const transition = commandStore.mutateOnboardingWithCalendar;
      if (typeof transition !== "function") throw onboardingError("onboarding_unavailable", 502);
      const state = await transition(scope, providerStatus, parsed.action, parsed.payload);
      const body = onboardingResponse(state, opts, scope);
      await commandStore.finishReceipt(scope, key, { status: "succeeded", result: body });
      claimedReceipt = false;
      sendJson(res, 200, body);
    } catch (error) {
      if (claimedReceipt) {
        try { await commandStore.finishReceipt(scope, key, { status: "failed", result: null }); } catch { /* leave the receipt pending; retries remain blocked */ }
      }
      const known = new Set(["unauthorized", "calendar_unavailable", "invalid_json", "body_too_large", "onboarding_conflict", "invalid_name", "invalid_home_address", "invalid_phone", "invalid_action", "payment_unavailable", "idempotency_required", "idempotency_conflict", "idempotency_in_progress", "idempotency_failed"]);
      const status = error.status || (known.has(error.message) ? (error.message === "unauthorized" ? 401 : error.message === "calendar_unavailable" ? 502 : error.message === "body_too_large" ? 413 : error.message === "onboarding_conflict" || error.message.startsWith("idempotency_") ? 409 : error.message === "payment_unavailable" ? 503 : 400) : 502);
      sendJson(res, status, { error: known.has(error.message) ? error.message : "onboarding_unavailable" });
    }
    return;
  }
  if (endpoint === "commands") {
    if (req.method !== "POST") { sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "POST" }); return; }
    if (!/^application\/json(?:;|$)/i.test(String(req.headers["content-type"] || ""))) { sendJson(res, 415, { error: "json_required" }); return; }
    const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
    if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) { sendJson(res, 403, { error: "origin_rejected" }); return; }
    const expectedCsrf = scope.csrf || csrfToken(session);
    if (!timingEqual(req.headers["x-lm-csrf"], expectedCsrf)) { sendJson(res, 403, { error: "csrf_rejected" }); return; }
    const key = String(req.headers["idempotency-key"] || "");
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) { sendJson(res, 400, { error: "idempotency_required" }); return; }
    try {
      const command = validateCommand(await readJson(req));
      const execute = opts.executeCommandImpl || executeUserCommand;
      const store = commandStore;
      const providerOpts = { ...opts, composioKey: opts.composioKey || process.env.COMPOSIO_API_KEY };
      const result = await execute(scope, command, { ...providerOpts, store, idempotencyKey: key, composioAuthConfig: opts.composioAuthConfig || process.env.COMPOSIO_GCAL_AUTH_CONFIG, startCalendarConnection: opts.startCalendarConnection || ((value) => composioCalendarStart(value, providerOpts)), disconnectCalendar: opts.disconnectCalendar || ((value) => composioCalendarDisconnect(value, providerOpts)) });
      sendJson(res, 200, result);
    } catch (error) { sendJson(res, error.status || 502, { error: error.message === "invalid_action" ? "invalid_action" : "command_failed" }); }
    return;
  }
  if (req.method !== "GET") {
    sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET" });
    return;
  }

  if (endpoint === "control-center") {
    const store = commandStore;
    const model = await buildControlCenter(scope, { ...opts, store, nowMs, calendarStatus: opts.calendarStatus || ((value) => composioCalendarStatus(value, { ...opts, composioKey: opts.composioKey || process.env.COMPOSIO_API_KEY })) });
    sendPanelSection(res, endpoint, { ...model, csrf: scope.csrf || csrfToken(session) }, opts);
    return;
  }
  if (endpoint === "money-printer") {
    sendJson(res, 200, await moneyPrinter(scope, { ...opts, nowMs }));
    return;
  }
  const readers = { timeline, scores, ledger, gates, settings };
  try {
    const candidate = await readers[endpoint](scope.uid, { ...opts, nowMs, scope });
    sendPanelSection(res, endpoint, candidate, opts);
  } catch (error) {
    if (endpoint === "scores" && error.scoreUnavailableReason) {
      sendJson(res, 503, { error: "score_data_unavailable", reason: error.scoreUnavailableReason });
      return;
    }
    throw error;
  }
}

module.exports = {
  CALL_MINUTES_BEFORE,
  todayBounds,
  aggregateCosts,
  createSupabaseCommandStore, readJson, composioCalendarStatus,
  composioCalendarDisconnect,
  composioCalendarStart,
  handleTelegramOAuthCallback,
  handlePanelOAuthCallback,
  handlePanelApiRequest,
};
