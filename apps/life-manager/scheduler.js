// scheduler.js — the cloud wake loop. Every 60s: find Life Manager users due for a T-10/T-5 min wake
// and place a Telnyx+Gemini-Charon call whose audio bridges back to THIS service's /ws.
//
// Source of truth:
//   lm_users (Supabase)        — registry: who has a phone + paid + a connected gcal
//   Composio connected_account — the actual Google Calendar OAuth (keyed by the SAME uid)
//   lm_wake_log (Supabase)     — dedup: one call per (uid, event start), survives restarts
"use strict";

const crypto = require("crypto");
const { fetchUpcomingEvents } = require("./lib/events.js");
const { schedulerCohortFilter, isCallablePhone } = require("./lib/user-selector.js");
const { DEFAULTS: RUNTIME_DEFAULTS, readRuntimePreferences } = require("./lib/runtime-preferences.js");
const { shouldWake, departureMs, resolveDeparture, isHelperBlock } = require("./lib/wake-filter.js");
const { mentalUserOnce, resolveSleepTarget } = require("./lib/mental-runtime.js");
const { careUserOnce } = require("./lib/care-daily-runtime.js");
const { dietUserOnce } = require("./lib/diet-runtime.js");
const { dietNudgeOnce } = require("./lib/diet-nudge.js");
const { preceptsUserOnce } = require("./lib/precepts-runtime.js");
const { preceptsMirrorOnce } = require("./lib/precepts-mirror.js");
const { relationsUserOnce } = require("./lib/relations-runtime.js");
const { readMentalSendState, recordMentalSend } = require("./lib/mental-send-log.js");

// 12c: TROUGH_AFTER_MS (30 min) plus margin — how far back the tick looks for ended events.
const MENTAL_LOOKBACK_MS = 35 * 60000;
const { placeCall } = require("./lib/dial.js");
const { encodeWakeClientState } = require("./lib/telnyx-webhook.js");
const { recordTelnyxWakeReceipt } = require("./lib/telnyx-receipt.js");
const {
  WAKE_MISS_REASONS, recordWakeMiss, claimWakeMissNotice, wakeMissNotice,
} = require("./lib/wake-miss.js");
const { putEvents, getEvents } = require("./lib/event-cache.js");
const { runOrgan } = require("./lib/organ-run.js");
const { fillTravel, directionsMinutes, directionsRoute } = require("./lib/travel.js");
const { formatTravelAutofillMessage } = require("./lib/i18n.js");
const { askTick } = require("./lib/ask.js");
const { onboardNudgeAll } = require("./lib/telegram-onboard.js");
const { sendMessage } = require("./lib/telegram.js");
const { langForPhone } = require("./lib/call-language.js");
const { recordDailyComposioPoll } = require("./lib/ledger.js");
const { schedulerPollInterval } = require("./lib/composio-budget.js");
const {
  processLocationLateNotice, getLiveLocation,
} = require("./lib/late-notice.js");
const { travelReminderOnce } = require("./lib/travel-reminder.js");
const {
  reserveManagedAction, completeManagedAction, releaseManagedAction,
  reserveVoiceAllowance, acceptVoiceAllowance, releaseVoiceAllowance,
} = require("./lib/managed-allowance.js");
const { deliverAllowanceNotice } = require("./lib/allowance-notice.js");
const {
  DISCOVERY_WEEK_MS, listDiscoveryUsers, runDiscoveryForUser,
} = require("./lib/feature-discovery.js");

// HMAC over the per-call context so the persistent /ws bridge can prove a connection was minted by
// THIS scheduler (not a stranger draining the Gemini budget) AND that the prompt context wasn't
// tampered in transit. server.js recomputes the same MAC and rejects on mismatch.
function signCtx(parts) {
  const secret = process.env.LM_CALL_SECRET || "";
  return crypto.createHmac("sha256", secret).update(parts.join("\n")).digest("base64url");
}

const TICK_MS = 60 * 1000;
// Reminder routing/Telegram work is independent of the call and of the other organs. Keep a
// bounded default for a provider that never settles; tests and operators may inject a smaller
// value without changing the global organ budget.
const REMINDER_TIMEOUT_MS = Number(process.env.LIFE_REMINDER_TIMEOUT_MS) || 35_000;

function withTimeout(work, timeoutMs, label) {
  const duration = Number(timeoutMs);
  const operation = Promise.resolve().then(work);
  if (!Number.isFinite(duration) || duration <= 0) return operation;
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timeout ${duration}ms`)), duration);
  });
  return Promise.race([operation, deadline]).finally(() => clearTimeout(timer));
}

// Escalating wake calls: ring at T-10 (firm) and T-5 (harsh) before EACH event — TWO calls only
// (Dais 2026-06-25: "just call me 10 min before and 5 min before, that's it"), so the user actually
// gets up / leaves. Each (event, level) fires once (deduped).
const WAKE_LEVELS = [
  { min: 10, urgency: "firm" },
  { min: 5, urgency: "harsh" },
];
// How late after DEPARTURE a wake call may still be STARTED. Past this the call has nothing left to
// achieve — the user is already late and the late-notice organ owns that territory — so the catch-up
// below stops here instead of ringing someone about a departure they can no longer make.
const LATE_CUTOFF_MIN = -15;

const SUPA = () => ({ url: process.env.SUPABASE_URL, key: process.env.SUPABASE_SERVICE_ROLE_KEY });

// isHelperBlock now lives in lib/wake-filter.js (shared with the importance filter + leave anchor).

async function supaUsers() {
  const { url, key } = SUPA();
  if (!url || !key) return [];
  const base = `${url}/rest/v1/lm_users?${schedulerCohortFilter()}`;
  const cols = "uid,name,phone,paid,calendar_provider,home_address,gmail_account_id,email,telegram_chat_id,call_language";
  const hdr = { apikey: key, Authorization: `Bearer ${key}` };
  // FAIL-SAFE: try WITH wake_policy; if the column is missing (PostgREST 400) fall back to the base
  // columns rather than returning [] — a missing column must NOT silently disable wakes fleet-wide.
  let r = await fetch(`${base}&select=${cols},wake_policy`, { headers: hdr });
  if (!r.ok) r = await fetch(`${base}&select=${cols}`, { headers: hdr }); // wake_policy → undefined → travel-only
  if (!r.ok) return [];
  const users = await r.json().catch(() => []);
  if (!Array.isArray(users) || users.length === 0) return [];
  const ids = users.map(u => u.uid).filter(Boolean).join(",");
  // call_time_zone rides along because it is the ONLY per-user timezone column that exists anywhere
  // in this schema (lm_users has none). The diet organ resolves each tenant's lunch window from it;
  // without it every tenant would be asked on Tokyo's clock, and the organ chooses silence over that.
  const prefsResponse = await fetch(`${url}/rest/v1/lm_panel_preferences?uid=in.(${encodeURIComponent(ids)})&select=uid,call_enabled,notifications_enabled,daily_automation_enabled,call_time_zone`, { headers: hdr });
  if (!prefsResponse.ok) return users.map(u => ({ ...u, call_enabled: false, notifications_enabled: false, daily_automation_enabled: false }));
  const preferenceRows = await prefsResponse.json().catch(() => null);
  if (!Array.isArray(preferenceRows)) return users.map(u => ({ ...u, call_enabled: false, notifications_enabled: false, daily_automation_enabled: false }));
  const byUid = new Map(preferenceRows.map(row => [row.uid, row]));
  return users.map(u => ({ ...RUNTIME_DEFAULTS, ...u, ...(byUid.get(u.uid) || {}) }));
}

// Claims this (uid,event_key) atomically and returns the CLAIM TOKEN identifying it — a truthy
// string, so every caller's `if (!fresh) continue` gate is unchanged. Falsy means someone already
// called: relies on the unique(uid,event_key) constraint, so a duplicate insert 409s.
//
// The token exists because a release must be able to prove it owns the row it deletes. See
// releaseWake below and migrations/2026-08-01-lm-wake-log-claim-token.sql for the double-call this
// closes.
async function claimWake(uid, eventKey, opts = {}) {
  const { url, key } = SUPA();
  if (!url || !key) return false;
  const f = opts.fetchImpl || fetch;
  const headers = { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=minimal" };
  const claimToken = crypto.randomUUID();
  const insert = (body) => f(`${url}/rest/v1/lm_wake_log`, { method: "POST", headers, body: JSON.stringify(body) });

  const r = await insert({ uid, event_key: eventKey, claim_token: claimToken });
  if (r.status === 201) return claimToken; // 201 = inserted (first time); 409 = already called
  // FAIL-SAFE, same posture as supaUsers' wake_policy fallback: deploy order is not atomic, and if
  // the code ships before the column exists PostgREST 400s on the unknown column — which would
  // silence EVERY wake call fleet-wide. Retry without it and fall back to the pre-token behaviour: a
  // claim with no identity (`true`), which releaseWake then deletes unconditionally, exactly as it
  // did before. Degraded, and honest about being degraded.
  if (r.status === 400) {
    const retry = await insert({ uid, event_key: eventKey });
    if (retry.status === 201) {
      console.error("[scheduler] lm_wake_log.claim_token missing — claimed without one (run migrations/2026-08-01-lm-wake-log-claim-token.sql)");
      return true;
    }
    return false;
  }
  return false;
}

// Release a claim when placeCall failed, so a LATER tick retries while the event is still in its
// window (claim→dial→unclaim-on-failure — mirrors unclaimTravel in lib/travel.js). Without this, a
// dial failure (e.g. Telnyx balance too low) permanently burns the (uid,event,level) slot: the row
// stays in lm_wake_log forever and claimWake 409s on every future tick even after the fix lands.
//
// CONDITIONAL ON THE CLAIM TOKEN. forEachUserSafe's per-user timeout abandons a tick without
// aborting its work, so a hung placeCall outlives its own tick and reaches this line LATE — by which
// time a later tick may have claimed the same key and successfully rung the user. Deleting by
// (uid,event_key) alone would erase that success, and the tick after it would re-claim and ring the
// user a SECOND time. Filtering on the token means a stale release matches nothing.
//
// With no token (a row claimed before the column existed, or the degraded path above) the delete
// stays unconditional: `claim_token=eq.<x>` never matches NULL, and refusing to release those rows
// would strand them forever — the exact bug this function was written to fix.
async function releaseWake(uid, eventKey, claimToken, opts = {}) {
  const { url, key } = SUPA();
  if (!url || !key) return;
  const f = opts.fetchImpl || fetch;
  let filter = `uid=eq.${encodeURIComponent(uid)}&event_key=eq.${encodeURIComponent(eventKey)}`;
  if (typeof claimToken === "string" && claimToken) filter += `&claim_token=eq.${encodeURIComponent(claimToken)}`;
  await f(`${url}/rest/v1/lm_wake_log?${filter}`, {
    method: "DELETE",
    headers: { apikey: key, Authorization: `Bearer ${key}`, Prefer: "return=minimal" },
  }).catch(() => {});
}

// 1b: releaseWake above is what makes a failed call INVISIBLE — it deletes the only row that said we
// were about to ring. lm_wake_miss is the counter-ledger: every wake we owed and did not deliver
// leaves a reasoned row that /status reads back. Best-effort by contract, because a ledger outage
// must never cost the user the retry (spec §3 row 1b, §5.4「沈黙で失敗しない」).
async function recordWakeMissRow(uid, miss) {
  const { url, key } = SUPA();
  return recordWakeMiss(uid, miss, { supaUrl: url, supaKey: key });
}

async function claimWakeMissNoticeRow(uid, eventKey, nowMs) {
  const { url, key } = SUPA();
  return claimWakeMissNotice(uid, eventKey, { supaUrl: url, supaKey: key, nowMs });
}

// Record the miss AND tell the user about it — §5.4「沈黙で失敗しない」: a row nobody reads still
// leaves the user to discover the failure themselves, which is the exact experience 1b exists to end.
// The notice is claimed through notified_at so a dial that keeps failing on every 60s tick produces
// one message, not a stream. Entirely best-effort: this is bookkeeping, and the caller's retry path
// must never depend on it (spec §3 row 1b).
async function noteWakeMiss(u, miss, deps, nowMs) {
  try {
    await (deps.recordWakeMiss || recordWakeMissRow)(u.uid, { ...miss, nowMs });
  } catch (e) {
    console.error(`[wake-miss] record uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`);
    return;
  }
  const telegramToken = deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN;
  // Same gate as every other Telegram leg: a user who turned notifications off still gets the truth,
  // in /status, rather than a message they asked not to receive.
  if (u.notifications_enabled === false || !telegramToken || !u.telegram_chat_id) return;
  try {
    const claimed = await (deps.claimWakeMissNotice || claimWakeMissNoticeRow)(u.uid, miss.eventKey, nowMs);
    if (!claimed) return; // an earlier tick already told them
    const text = wakeMissNotice(claimed, { lang: langForUser(u), timeZone: u.call_time_zone || u.time_zone || null });
    if (text) await (deps.sendMessage || sendMessage)(telegramToken, u.telegram_chat_id, text);
  } catch (e) {
    console.error(`[wake-miss] notify uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`);
  }
}

// Did this (event, level) ever get claimed? A claim is written immediately before the dial, so its
// ABSENCE once departure has passed means nothing was ever attempted for that event.
async function wakeWasClaimed(uid, eventKey) {
  const { url, key } = SUPA();
  if (!url || !key) return false;
  const response = await fetch(
    `${url}/rest/v1/lm_wake_log?uid=eq.${encodeURIComponent(uid)}&event_key=eq.${encodeURIComponent(eventKey)}&select=event_key&limit=1`,
    { headers: { apikey: key, Authorization: `Bearer ${key}` } },
  ).catch(() => null);
  if (!response || !response.ok) return true; // unknown ≠ missed: never accuse the loop on a read failure
  const rows = await response.json().catch(() => null);
  return Array.isArray(rows) && rows.length > 0;
}

// Low-balance alert (issue#10 root cause: pre-event calls silently never fire when the Telnyx
// balance drops below the $0.50 preflight in lib/dial.js). Ping the admin's Telegram so the balance
// gets topped up instead of the gap going unnoticed. isLowBalanceError/shouldAlertLowBalance are pure
// (matches the testCallAllowed(uid, nowMs) pattern in server.js) so the throttle is unit-testable
// without stubbing fetch/Date. Best-effort like dunningNotify in server.js — NEVER throws.
const LOW_BALANCE_ALERT_COOLDOWN_MS = 6 * 60 * 60 * 1000; // at most 1 alert per 6h
let lastLowBalanceAlertMs = 0;

function isLowBalanceError(errorMsg) {
  return /balance too low/i.test(String(errorMsg || ""));
}

function shouldAlertLowBalance(errorMsg, nowMs, lastAlertMs) {
  return isLowBalanceError(errorMsg) && nowMs - lastAlertMs >= LOW_BALANCE_ALERT_COOLDOWN_MS;
}

async function maybeAlertLowBalance(errorMsg, nowMs = Date.now()) {
  if (!shouldAlertLowBalance(errorMsg, nowMs, lastLowBalanceAlertMs)) return;
  lastLowBalanceAlertMs = nowMs;
  const token = process.env.LM_TELEGRAM_BOT_TOKEN;
  const chatId = process.env.LM_ADMIN_TELEGRAM_CHAT_ID;
  if (!token || !chatId) {
    console.error(`[scheduler] LOW BALANCE (no LM_ADMIN_TELEGRAM_CHAT_ID configured): ${errorMsg}`);
    return;
  }
  await sendMessage(token, chatId, `⚠️ Telnyx balance too low — Life Manager wake calls are NOT firing.\n${errorMsg}`)
    .catch((e) => console.error("[scheduler] low-balance alert send failed", e && e.message));
}

// Resolve the call language for a user row: their EXPLICIT choice (lm_users.call_language, set via the
// /lm toggle) wins; otherwise fall back to the phone country. So a US phone can choose Japanese and a
// Japanese phone can choose English (Dais 2026-06-22).
function langForUser(u) {
  const c = u && u.call_language;
  return c === "ja" || c === "en" ? c : langForPhone(u && u.phone);
}

function buildStreamUrl(ev, urgency, lang, name) {
  const base = (process.env.PUBLIC_WSS || "").replace(/\/$/, "");
  const summary = ev.summary || "";
  const dateTime = ev.startIso || "";
  const location = ev.location || "";
  const urg = urgency || "gentle";
  const lg = lang === "ja" ? "ja" : "en";
  const nm = String(name || "").replace(/[\r\n]/g, " ").slice(0, 60); // address the user by name on the call
  const wakeUid = String(ev.wakeUid || "");
  const wakeEventKey = String(ev.wakeEventKey || "");
  const voicePeriodStart = String(ev.voicePeriodStart || "");
  const voiceReservationToken = String(ev.voiceReservationToken || "");
  const voiceAllowedSeconds = String(ev.voiceAllowedSeconds || "");
  const sig = signCtx([summary, dateTime, location, urg, lg, nm, wakeUid, wakeEventKey,
    voicePeriodStart, voiceReservationToken, voiceAllowedSeconds]);
  const qs = new URLSearchParams({ summary, dateTime, location, urgency: urg, lang: lg, name: nm,
    wakeUid, wakeEventKey, sig });
  if (voicePeriodStart && voiceReservationToken && voiceAllowedSeconds) {
    qs.set("voicePeriodStart", voicePeriodStart);
    qs.set("voiceReservationToken", voiceReservationToken);
    qs.set("voiceAllowedSeconds", voiceAllowedSeconds);
  }
  return `${base}/ws?${qs.toString()}`;
}

// LM-30 runs inside the durable 60s wake tick. A non-expired Telegram live location is the sole gate;
// lm_late_notice_log atomically deduplicates one action per calendar event across restarts.
async function lateNoticeUserOnce(u, nowMs, deps = {}) {
  const now = nowMs !== undefined ? nowMs : Date.now();
  const configuredSupa = SUPA();
  const supaUrl = deps.supaUrl !== undefined ? deps.supaUrl : configuredSupa.url;
  const supaKey = deps.supaKey !== undefined ? deps.supaKey : configuredSupa.key;
  if (!u || !u.uid || !supaUrl || !supaKey) return;
  const dbOpts = { supaUrl, supaKey, nowMs: now, fetchImpl: deps.fetchImpl };
  const location = deps.location !== undefined
    ? deps.location
    : await (deps.getLiveLocation || getLiveLocation)(u.uid, now, dbOpts);
  const events = deps.events || await (deps.fetchUpcomingEvents || fetchUpcomingEvents)(u.uid, {
    nowMs: now, horizonH: 6, apiKey: deps.apiKey || process.env.COMPOSIO_API_KEY,
    calendar: deps.calendar, gmailAccountId: u.gmail_account_id,
  });
  return processLocationLateNotice({
    user: u, location, events, nowMs: now,
    mapsKey: deps.mapsKey || process.env.LIFE_MAPS_KEY || process.env.GOOGLE_API_KEY,
    telegramToken: deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN,
    supaUrl, supaKey, fetchImpl: deps.fetchImpl,
  }, {
    routeMinutes: deps.routeMinutes || directionsMinutes,
    getLateDraft: deps.getLateDraft,
    lateApprovalStore: deps.lateApprovalStore,
    resolveLateRecipients: deps.resolveLateRecipients,
    recipientResolverDeps: deps.recipientResolverDeps,
    createLateDraft: deps.createLateDraft,
    sendMessage: deps.sendMessage || sendMessage,
    supaUrl, supaKey, fetchImpl: deps.fetchImpl,
  });
}

// ── Per-user single-invocation functions (extracted for Inngest fan-out) ─────
// Each function takes a single user row `u` and performs the loop body for THAT user only.
// The existing tick/travelTick/askTickAll still call these in a for-loop so the in-process
// LIFE_RUN_LOOPS path continues to work unchanged.

// MEN-c wiring. The trigger rule needs a shape the calendar does not hand over directly, so the two
// judgements are made here and stated plainly: an event with a place or people is "important", and one
// that runs 90 minutes or longer is "intense". Location stays "unknown" rather than pretending we can
// tell standing still from travelling — "unknown" never suppresses, "moving" would.
const MENTAL_SEEDS = Object.freeze([
  "I am enough exactly as I am",
  "I choose peace over worry",
  "I release what I cannot control",
  "Today I choose to be kind to myself",
]);

// H2/H4: the diet and precepts triggers only ask whether an event is IN PROGRESS, so they need
// starts and ends and nothing else. An event with no end is given the same 1h assumption mentalDeps
// makes — a missing end must not read as a zero-length event that suppresses nothing.
function inProgressEvents(events) {
  return (Array.isArray(events) ? events : []).map((event) => {
    const startMs = Number(event.startMs);
    const endMs = Number.isFinite(event.endMs) ? Number(event.endMs) : startMs + 60 * 60000;
    return { startMs, endMs };
  }).filter((event) => Number.isFinite(event.startMs) && event.endMs > event.startMs);
}

// The kill switch, spelled the way an operator under pressure actually spells it. `LM_DIET_ENABLED=0`
// was the only accepted form, so `=false` and `=off` silently LEFT THE ORGAN ON — the exact moment
// an off switch must not be pedantic is the moment someone is reaching for it.
const ORGAN_OFF = /^(0|false|off|no)$/i;
function dietEnabled() {
  return !ORGAN_OFF.test(String(process.env.LM_DIET_ENABLED || "").trim());
}

// H4 ORG-precepts: same switch, same spellings, same default-ON. Defaulting a gate to OFF is how an
// organ ships and then quietly never runs, which is indistinguishable from not having shipped it.
function preceptsEnabled() {
  return !ORGAN_OFF.test(String(process.env.LM_PRECEPTS_ENABLED || "").trim());
}

function relationsEnabled() {
  return !ORGAN_OFF.test(String(process.env.LM_RELATIONS_ENABLED || "").trim());
}

function mentalDeps(u, events, deps = {}) {
  const supa = SUPA();
  const shaped = (Array.isArray(events) ? events : []).map((event) => {
    const startMs = Number(event.startMs);
    const endMs = Number.isFinite(event.endMs) ? Number(event.endMs) : startMs + 60 * 60000;
    return {
      startMs,
      endMs,
      important: Boolean(event.location) || Boolean(event.attendees && event.attendees.length),
      intense: endMs - startMs >= 90 * 60000,
    };
  }).filter((event) => Number.isFinite(event.startMs) && event.endMs > event.startMs);

  return {
    telegramToken: deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN,
    seeds: deps.mentalSeeds || MENTAL_SEEDS,
    sleepTargetMs: Number.isFinite(deps.sleepTargetMs)
      ? deps.sleepTargetMs
      : resolveSleepTarget(process.env.LM_MENTAL_SLEEP_TARGET || "23:30", Date.now(), Number(process.env.LM_MENTAL_UTC_OFFSET_HOURS || 9)),
    fetchUpcomingEvents: async () => shaped,
    getLocationState: deps.getLocationState || (async () => "unknown"),
    sendMessage: deps.sendMessage || sendMessage,
    readSendState: deps.readMentalState || (async (uid, nowMs) => readMentalSendState(uid, nowMs, supa)),
    recordSend: deps.recordMentalSend || ((uid, trigger, messageId) => recordMentalSend(uid, trigger, messageId, supa)),
  };
}

// readMentalSendState / recordMentalSend moved to lib/mental-send-log.js (imported above) — H4 ⑤
// makes lm_mental_send_log a SHARED budget, and a budget only one module can reach is not a budget.
// MENTAL's semantics are unchanged: it still calls them non-strict, so an unreadable log still reads
// as "nothing sent yet" for this organ. Precepts calls them strict and stays silent instead — see
// lib/mental-send-log.js for why the two callers differ on purpose.
//
// ONE DELIBERATE DIFFERENCE FROM THE CODE THAT USED TO LIVE HERE, named rather than smuggled: the
// shared module runs the base URL through supaBase(), which strips a trailing slash. The inline
// versions interpolated `${supa.url}/rest/v1/...` raw, so a SUPABASE_URL configured with a trailing
// slash produced `//rest/v1/lm_mental_send_log` — a 404 that reads as "nothing sent yet" and quietly
// unbounds the 3/day cap. That is a fix, not a refactor, and it applies to MENTAL too.

// wakeCallOnce — the DEADLINE-CRITICAL half. It owns the calendar fetch (and publishes it for the
// organ/reminder ticks), then does nothing but decide and place the call. Everything that can be late
// lives in organsUserOnce or reminderUserOnce on its own timer, so no organ can spend this user's budget
// before the dial
// (spec §3.1: late+mental sat in front of the dial inside one shared 90s budget).
async function wakeCallOnce(u, nowMs, deps = {}) {
  if (u && u.daily_automation_enabled === false) return;
  const now = nowMs !== undefined ? nowMs : Date.now();
  // LM-7: calendar polling is represented once per UTC day/user. The helper checks today's row
  // in Supabase on every tick; no in-memory counter is used, so restarts preserve aggregation.
  //
  // DETACHED ON PURPOSE. This is 1-2 Supabase round trips with no timeout and no AbortController,
  // and it is pure accounting — nothing below reads its result. Awaited, it was the same failure
  // this whole split exists to end ("something slow sits in front of the dial"), surviving in
  // miniature: a slow store spent the user's entire wake budget and the phone never rang. Tightening
  // that budget from 90s to 20s made the exposure worse, not better. So the write still happens; it
  // just no longer gates the call. The .catch is mandatory rather than tidy — an un-caught detached
  // rejection is an unhandled rejection, which can take the whole scheduler process down — and it
  // LOGS, because a silently swallowed ledger failure is the invisibility spec §1.2 is about.
  Promise.resolve()
    .then(() => (deps.recordDailyPoll || recordDailyComposioPoll)(u.uid, { nowMs: now }))
    .catch((e) => console.error(`[wake] daily poll ledger uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`));
  let events;
  try {
    // 6h horizon: a long-travel event AND its [Travel] block must both be visible at the moment we
    // wake 15 min before DEPARTURE, which can be hours before the event itself.
    // 12c: fetch once with a lookback wide enough for the MENTAL trough (an intense block that
    // already ENDED within TROUGH_AFTER_MS). Every non-MENTAL consumer below stays on the
    // strict-future slice, so wake/late behavior is unchanged.
    events = await (deps.fetchUpcomingEvents || fetchUpcomingEvents)(u.uid, {
      nowMs: now, horizonH: 6, lookbackMs: MENTAL_LOOKBACK_MS,
      apiKey: deps.apiKey || process.env.COMPOSIO_API_KEY,
      calendar: deps.calendar, gmailAccountId: u.gmail_account_id,
    });
  } catch {
    return;
  }
  const futureEvents = (events || []).filter((e) => Number(e.startMs) >= now);
  // The organ tick reads this instead of fetching. Publishing the RAW events (not futureEvents) is
  // deliberate: the MENTAL organ needs the lookback slice that futureEvents throws away.
  (deps.putEvents || putEvents)(u.uid, events, now);
  // #69 importance filter: only wake for events the user must TRAVEL to (per their wake_policy),
  // and anchor the 10/5 levels to DEPARTURE (leave time), not the event start — so a 30-min-travel
  // event is called before they must leave. resolveDeparture uses the [Travel] block if present, else
  // computes the leave time inline (never-late even before the 30-min travel loop inserts the block).
  const mapsKey = deps.mapsKey || process.env.LIFE_MAPS_KEY || process.env.GOOGLE_API_KEY;
  // spec §5.2.1: `=== true`, not `!== false`. The phone is opt-IN now, and three different shapes all
  // mean "expressed no preference" — no row, a SQL NULL column, and an unmerged undefined. `!== false`
  // dialled all three. A malformed/missing phone is an independent hard gate. This is also the LAST
  // gate on the Inngest per-user path, which reaches wakeCallOnce through wakeUserOnce and never passes
  // wakeTick's filter.
  if (u.paid === true && u.call_enabled === true && isCallablePhone(u.phone)) {
    for (const ev of futureEvents.filter((e) => shouldWake(e, u.home_address, u.wake_policy))) {
      const managedActionKey = String(ev.id || `${ev.startMs || ev.startIso}:${ev.summary || ""}`);
      const allowanceReserve = deps.reserveManagedAction || (deps.placeCall ? undefined : reserveManagedAction);
      const allowanceRelease = deps.releaseManagedAction || (deps.placeCall ? undefined : releaseManagedAction);
      const voiceReserve = deps.reserveVoiceAllowance || (deps.placeCall ? undefined : reserveVoiceAllowance);
      const voiceAccept = deps.acceptVoiceAllowance || (deps.placeCall ? undefined : acceptVoiceAllowance);
      const voiceRelease = deps.releaseVoiceAllowance || (deps.placeCall ? undefined : releaseVoiceAllowance);
      let allowanceReserved = false;
      let allowanceReservation = null;
      const allowanceState = {};
      const { url: allowanceUrl, key: allowanceKey } = SUPA();
      if ((deps.directionsRoute || deps.directionsMinutes) && typeof allowanceReserve === "function") {
        const allowance = await allowanceReserve(u.uid, managedActionKey, allowanceUrl, allowanceKey);
        if (!allowance || allowance.allowed !== true) continue;
        allowanceState.receipt = allowance.reservationToken ? allowance : null;
      }
      const depMs = await resolveDeparture(ev, futureEvents, {
        home: u.home_address, mapsKey, nowMs: now, bufferMin: 5,
        directionsFn: deps.directionsMinutes || directionsMinutes,
        routeFn: deps.directionsRoute || (deps.directionsMinutes ? undefined : directionsRoute), uid: u.uid,
        timezone: u.call_time_zone || u.time_zone,
        routeOptions: {
          supaUrl: allowanceUrl, supaKey: allowanceKey, _allowanceState: allowanceState,
          _reserveManagedAction: allowanceReserve, _releaseManagedAction: allowanceRelease,
        },
      });
      allowanceReservation = allowanceState.receipt || null;
      allowanceReserved = Boolean(allowanceReservation);
      const mins = (depMs - now) / 60000;
      // A level is DUE once its threshold has passed, not only while the tick sits inside a ~2-min
      // window: this tick is not periodic (the organs above share the per-user timeout, and a redeploy
      // restarts the loop), so a window-bound level was lost FOREVER whenever a tick landed outside it.
      // Ordered most urgent first, because a tick that catches up on both levels must place ONE call —
      // the one that still matches how little time is left — never T-10 and T-5 back to back.
      const due = WAKE_LEVELS
        .filter((lvl) => mins <= lvl.min + 0.5 && mins > LATE_CUTOFF_MIN)
        .sort((a, b) => a.min - b.min);
      if (!due.length && allowanceReserved && typeof allowanceRelease === "function") {
        await allowanceRelease(u.uid, managedActionKey, allowanceUrl, allowanceKey,
          { reservation: allowanceReservation });
        allowanceReserved = false;
        allowanceReservation = null;
      }
      if (due.length && !allowanceReserved && typeof allowanceReserve === "function") {
        const allowance = await allowanceReserve(u.uid, managedActionKey, allowanceUrl, allowanceKey);
        if (!allowance || allowance.allowed !== true) continue;
        allowanceReservation = allowance.reservationToken ? allowance : null;
        allowanceReserved = Boolean(allowanceReservation);
      }
      // 1b: the moment departure crosses the cutoff, this event can never ring again. If the finest
      // level was never even claimed, nothing was ever attempted — the exact failure that looked like
      // a non-event in lm_wake_log. Record it once, in the two ticks just past the cutoff: later ticks
      // belong to the late-notice organ, and re-recording would keep restamping an old failure as new.
      if (mins <= LATE_CUTOFF_MIN && mins > LATE_CUTOFF_MIN - 2) {
        const finest = WAKE_LEVELS.reduce((a, b) => (a.min <= b.min ? a : b));
        const finestKey = `${u.uid}|${ev.startIso}|${finest.min}`;
        try {
          const everClaimed = await (deps.wakeWasClaimed || wakeWasClaimed)(u.uid, finestKey);
          if (!everClaimed) {
            await noteWakeMiss(u, {
              eventKey: `${u.uid}|${ev.startIso}|departure`,
              eventStartIso: ev.startIso,
              dueAtIso: new Date(depMs).toISOString(),
              reason: WAKE_MISS_REASONS.NO_CALL_BEFORE_DEPARTURE,
              eventSummary: ev.summary,
            }, deps, now);
            console.error(`[scheduler] wake never rang uid=${u.uid.slice(0, 12)} departure passed`);
          }
        } catch (e) {
          console.error(`[wake-miss] uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`);
        }
      }
      for (const lvl of due) {
        const eventKey = `${u.uid}|${ev.startIso}|${lvl.min}`;
        if (lvl !== due[0]) {
          await (deps.claimWake || claimWake)(u.uid, eventKey);
          continue;
        }
        const voice = typeof voiceReserve === "function"
          ? await voiceReserve(u.uid, eventKey, allowanceUrl, allowanceKey)
          : { allowed: true, allowedSeconds: 120, periodStart: "test", reservationToken: "test" };
        if (!voice || voice.allowed !== true || !voice.reservationToken || voice.allowedSeconds < 1) {
          if (allowanceReserved && typeof allowanceRelease === "function") {
            await allowanceRelease(u.uid, managedActionKey, allowanceUrl, allowanceKey,
              { reservation: allowanceReservation });
          }
          continue;
        }
        // `fresh` is the CLAIM TOKEN (a truthy string) when this tick won the claim — the gate below
        // is unchanged because falsy still means "someone already called". It is carried all the way
        // to releaseWake so a release that arrives late can only delete ITS OWN claim.
        const fresh = await (deps.claimWake || claimWake)(u.uid, eventKey);
        if (!fresh) {
          if (typeof voiceRelease === "function") await voiceRelease(u.uid, eventKey, allowanceUrl, allowanceKey,
            { reservation: voice });
          continue;
        }
        if (typeof voiceAccept === "function") {
          const accepted = await voiceAccept(u.uid, eventKey, allowanceUrl, allowanceKey, { reservation: voice });
          if (!accepted || accepted.allowed !== true) {
            await (deps.releaseWake || releaseWake)(u.uid, eventKey, fresh);
            if (typeof voiceRelease === "function") await voiceRelease(u.uid, eventKey, allowanceUrl, allowanceKey,
              { reservation: voice });
            if (allowanceReserved && typeof allowanceRelease === "function") {
              await allowanceRelease(u.uid, managedActionKey, allowanceUrl, allowanceKey,
                { reservation: allowanceReservation });
            }
            continue;
          }
        }
        const streamUrl = buildStreamUrl({ ...ev, wakeUid: u.uid, wakeEventKey: eventKey,
          voicePeriodStart: voice.periodStart, voiceReservationToken: voice.reservationToken,
          voiceAllowedSeconds: voice.allowedSeconds }, lvl.urgency, langForUser(u), u.name);
        let res;
        try {
          res = await (deps.placeCall || placeCall)({
            to: u.phone,
            streamUrl,
            timeLimitSeconds: voice.allowedSeconds,
            clientState: encodeWakeClientState({
              wakeUid: u.uid, wakeEventKey: eventKey, wakeClaimToken: fresh,
              managedActionKey: allowanceReservation ? managedActionKey : undefined,
              managedPeriodStart: allowanceReservation && allowanceReservation.periodStart,
              managedReservationToken: allowanceReservation && allowanceReservation.reservationToken,
            }),
          });
        } catch (e) {
          res = { ok: false, error: String((e && e.message) || e) };
        }
        if (res.ok) {
          let receipt;
          try {
            const { url: supaUrl, key: supaKey } = SUPA();
            receipt = await (deps.recordTelnyxWakeReceipt || recordTelnyxWakeReceipt)({
              uid: u.uid,
              eventKey,
              claimToken: fresh,
              callControlId: res.ccid,
              callSessionId: res.callSessionId ?? null,
              callLegId: res.callLegId ?? null,
              webhookEventId: null,
              amdResult: null,
            }, { supaUrl, supaKey, fetchImpl: deps.fetchImpl });
          } catch {
            receipt = null;
          }
          if (receipt && receipt.ok === true && receipt.matched === 1) {
            console.log(`[scheduler] WAKE T-${lvl.min} uid=${u.uid.slice(0, 12)} ccid=${res.ccid}`);
          } else {
            console.error("[scheduler] accepted wake requires Telnyx receipt reconciliation");
          }
        } else {
          console.error(`[scheduler] dial failed T-${lvl.min} uid=${u.uid.slice(0, 12)}: ${res.error}`);
          // 1b: record BEFORE releasing, because releasing is what erases the evidence. Wrapped so a
          // ledger outage can never skip the release below — the retry outranks the bookkeeping.
          await noteWakeMiss(u, {
            eventKey,
            eventStartIso: ev.startIso,
            dueAtIso: new Date(depMs - lvl.min * 60000).toISOString(),
            levelMin: lvl.min,
            reason: WAKE_MISS_REASONS.DIAL_FAILED,
            detail: res.error,
            eventSummary: ev.summary,
          }, deps, now);
          // Don't burn the retry: release the claim so the next 60s tick tries again while the event
          // is still in its window (the claim-before-dial order stays intact as the dedup guard).
          // Released with the token this tick claimed with: this line can run LONG after its own
          // tick was abandoned (the per-user timeout does not abort placeCall), and by then a later
          // tick may have claimed the same key and actually rung the user. An untargeted delete
          // would erase that success and the next tick would ring them a second time.
          await (deps.releaseWake || releaseWake)(u.uid, eventKey, fresh);
          if (typeof voiceRelease === "function") {
            await voiceRelease(u.uid, eventKey, allowanceUrl, allowanceKey, { reservation: voice });
          }
          if (typeof allowanceRelease === "function") {
            const { url: allowanceUrl, key: allowanceKey } = SUPA();
            await allowanceRelease(u.uid, managedActionKey, allowanceUrl, allowanceKey,
              { reservation: allowanceReservation });
          }
          await (deps.alertLowBalance || maybeAlertLowBalance)(res.error);
        }
      }
    }
  }
}

// The deadline-critical Telegram reminder has its own fixed tick. It reads the same raw lookback
// calendar shape as the old organ path, but its route/live-location/claim/send body remains one
// invocation path so a slow/degraded organ tick cannot suppress the reminder or duplicate it.
async function reminderUserOnce(u, nowMs, deps = {}) {
  if (!u || u.daily_automation_enabled === false || u.notifications_enabled === false) return;
  const now = nowMs !== undefined ? nowMs : Date.now();
  const log = deps.log || console.log;
  let events = Array.isArray(deps.events) ? deps.events : (deps.getEvents || getEvents)(u.uid, now);
  if (events == null) {
    try {
      events = await (deps.fetchUpcomingEvents || fetchUpcomingEvents)(u.uid, {
        nowMs: now, horizonH: 6, lookbackMs: MENTAL_LOOKBACK_MS,
        apiKey: deps.apiKey || process.env.COMPOSIO_API_KEY,
        calendar: deps.calendar, gmailAccountId: u.gmail_account_id,
      });
      (deps.putEvents || putEvents)(u.uid, events, now);
    } catch (e) {
      console.error(`[scheduler] reminder calendar fetch uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`);
      return;
    }
  }
  const reminderTimeoutMs = deps.reminderTimeoutMs !== undefined
    ? deps.reminderTimeoutMs
    : (deps.travelReminderTimeoutMs !== undefined ? deps.travelReminderTimeoutMs : REMINDER_TIMEOUT_MS);
  const reminderResult = await runOrgan({
    label: "organ:travel-reminder", uid: u.uid, log,
    run: () => withTimeout(async () => {
      const configuredSupa = SUPA();
      const supaUrl = deps.supaUrl !== undefined ? deps.supaUrl : configuredSupa.url;
      const supaKey = deps.supaKey !== undefined ? deps.supaKey : configuredSupa.key;
      const liveLocation = deps.liveLocation !== undefined
        ? deps.liveLocation
        : await (deps.getLiveLocation || getLiveLocation)(u.uid, now, {
          supaUrl, supaKey, nowMs: now, fetchImpl: deps.fetchImpl,
        });
      return (deps.travelReminder || travelReminderOnce)(u, now, {
        events,
        liveLocation,
        home: deps.home !== undefined ? deps.home : u.home_address,
        mapsKey: deps.mapsKey || process.env.LIFE_MAPS_KEY || process.env.GOOGLE_API_KEY,
        timezone: deps.timezone || u.call_time_zone || u.time_zone,
        telegramToken: deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN,
        supaUrl, supaKey,
        apiKey: deps.apiKey || process.env.COMPOSIO_API_KEY,
        directionsRoute: deps.directionsRoute,
        claimTravel: deps.claimTravel,
        unclaimTravel: deps.unclaimTravel,
        reserveManagedAction: deps.reserveManagedAction || (deps.travelReminder ? undefined : reserveManagedAction),
        completeManagedAction: deps.completeManagedAction || (deps.travelReminder ? undefined : completeManagedAction),
        releaseManagedAction: deps.releaseManagedAction || (deps.travelReminder ? undefined : releaseManagedAction),
        sendMessage: deps.sendMessage || sendMessage,
        log,
      });
    }, reminderTimeoutMs, "travel reminder"),
  });
  const noticeFn = deps.allowanceNotice || (deps.travelReminder ? null : deliverAllowanceNotice);
  if (typeof noticeFn === "function") {
    try {
      await noticeFn(u, {
        supaUrl: deps.supaUrl !== undefined ? deps.supaUrl : SUPA().url,
        supaKey: deps.supaKey !== undefined ? deps.supaKey : SUPA().key,
        telegramToken: deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN,
        sendMessage: deps.sendMessage || sendMessage,
        fetchImpl: deps.fetchImpl,
      });
    } catch { /* notice failure must not change the deadline-critical reminder result */ }
  }
  return reminderResult;
}

// organsUserOnce — everything that is NOT the wake call or deadline-critical Telegram reminder.
// Runs on its own timer with the original 90s per-user budget, so a slow care/diet/mental/late organ
// delays only its siblings. Each organ is wrapped in runOrgan, which both preserves the old
// swallow-and-continue contract and records the elapsed ms that used to be missing when
// `tenant timeout` fired (spec §3 row 1c done receipt).
async function organsUserOnce(u, nowMs, deps = {}) {
  if (u && u.daily_automation_enabled === false) return;
  const now = nowMs !== undefined ? nowMs : Date.now();
  const log = deps.log || console.log;

  // Read what the wake tick already fetched. A miss (first tick after a restart, or a wake tick that
  // failed) falls back to a real fetch: the organs still run, and the cost is bounded to that case.
  let events = (deps.getEvents || getEvents)(u.uid, now);
  if (events == null) {
    try {
      events = await (deps.fetchUpcomingEvents || fetchUpcomingEvents)(u.uid, {
        nowMs: now, horizonH: 6, lookbackMs: MENTAL_LOOKBACK_MS,
        apiKey: deps.apiKey || process.env.COMPOSIO_API_KEY,
        calendar: deps.calendar, gmailAccountId: u.gmail_account_id,
      });
      (deps.putEvents || putEvents)(u.uid, events, now);
    } catch (e) {
      // This return silently stands down EVERY organ for this user this tick. The whole point of
      // this work is that failures stop being invisible, so say which user and why before leaving.
      console.error(`[scheduler] organ calendar fetch uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`);
      return;
    }
  }
  const futureEvents = (events || []).filter((e) => Number(e.startMs) >= now);

  // Every runOrgan label is namespaced `organ:<name>` so the stopwatch receipt can never be confused
  // with the organ's OWN outcome line. Both are `[…] uid=…`, the receipt prints on EVERY tick (an
  // organ that decided to stay silent still took time) while the outcome line prints only when
  // something happened — so a bare `[precepts]` label buries the one line that matters under the
  // ones that don't, and anything reading the first match gets the stopwatch. `grep '\[organ:'` is
  // now the timing view and `grep '\[precepts\]'` is still the behaviour view.
  if (u.notifications_enabled !== false) {
    const late = await runOrgan({
      label: "organ:late", uid: u.uid, log,
      run: () => (deps.lateNotice || lateNoticeUserOnce)(u, now, { events: futureEvents }),
    });
    // The Telegram leg is otherwise unauditable: name the message that was actually delivered.
    if (late && late.telegramMessageId !== undefined) {
      log(`[late] uid=${String(u.uid).slice(0, 12)} decision=${late.decision} sent=${!!late.sent} tg_message_id=${late.telegramMessageId}`);
    }
  }

  // MEN-c: the MENTAL organ rides the same 60s tick. It stays silent unless the day itself says now.
  const mental = await runOrgan({
    label: "organ:mental", uid: u.uid, log,
    run: () => (deps.mental || mentalUserOnce)(u, now, mentalDeps(u, events, deps)),
  });
  if (mental && mental.delivered) {
    log(`[mental] uid=${String(u.uid).slice(0, 12)} trigger=${mental.trigger} tg_message_id=${mental.telegramMessageId}`);
  }

  // 11a/11b: the PHYSICAL organ rides the same 60s tick. careUserOnce holds a durable daily claim
  // in lm_care_scan_log, so despite the 60s cadence there is ONE real scan per user per UTC day —
  // every other tick costs a single row lookup. Isolated exactly like MENTAL above: a care failure
  // must never break wake calls. It runs LAST — after the wake evaluation — because the wake dial
  // has a ~2-min catch window and a slow Places/Supabase chain must never delay it; care has no
  // deadline (any tick today may claim the scan). Still runs for call-disabled users: skipping the
  // wake loop must not skip the PHYSICAL organ. With LM_BOOKING_ENABLED absent this path detects and
  // records candidates only; with the gate ON, careUserOnce also runs the 11c booking leg and the 11d
  // 事後報告 (steel session, Telegram, calendar) inside this same call. The executor holds its own
  // deadline below USER_TICK_TIMEOUT_MS, so a tick abandoned here can never leave the single steel
  // session held open behind it.
  const care = await runOrgan({
    label: "organ:care", uid: u.uid, log,
    run: () => (deps.care || careUserOnce)(u, now, { apiKey: deps.apiKey, calendar: deps.calendar }),
  });
  if (care && care.status && care.status !== "already_scanned") {
    log(`[care] uid=${String(u.uid).slice(0, 12)} status=${care.status}`
      + `${care.category ? ` category=${care.category}` : ""}`
      + `${care.selectedProviderId ? ` selected=${care.selectedProviderId}` : ""}`
      + `${care.chainError ? ` chain_err=${care.chainError}` : ""}`);
  }
  // H2 ORG-diet: the diet organ rides the same 60s tick, LAST — after the time-critical wake dial and
  // after care, because a Places lookup must never sit in front of a call. Both legs hold their own
  // durable claim in lm_diet_log (UNIQUE (uid, day, kind)), so the 120-minute question window and the
  // 30-minute nudge window each produce at most one message however many ticks pass through them.
  //
  // The gate defaults ON (spec H2), and `notifications_enabled` is honoured exactly as the late/mental
  // siblings honour it — the diet organ is nothing but unsolicited messages, so the one switch a user
  // has for "stop messaging me" must cover it. The two legs get SEPARATE try/catch blocks: they share
  // a table but not a fate, and a Places outage in the nudge must not cost the day its question.
  if (dietEnabled() && u.notifications_enabled !== false) {
    let nudgeSentThisTick = false;
    // getLocationState is passed to BOTH diet legs exactly as mentalDeps passes it to MENTAL: one
    // injected provider, one behaviour. Nothing supplies it in production today, so both organs
    // read the constant "unknown" — the gate is wired, not live, and both files say so.
    const nudge = await runOrgan({
      label: "organ:diet-nudge", uid: u.uid, log,
      run: () => (deps.dietNudge || dietNudgeOnce)(u, now, {
        calendarEvents: events, getLocationState: deps.getLocationState,
      }),
    });
    if (nudge && nudge.status === "nudged") {
      nudgeSentThisTick = true;
      log(`[diet-nudge] uid=${String(u.uid).slice(0, 12)} samples=${nudge.sampleCount}`
        + ` fast=${nudge.fastCount} venue=${nudge.venue ? "yes" : "none"} tg_message_id=${nudge.telegramMessageId}`);
    }
    // The two windows overlap (nudge 11:15-11:45, question 11:30-13:30), so on some ticks BOTH legs
    // fire and the user gets two unsolicited messages zero seconds apart — the sermon the thresholds
    // exist to prevent, delivered in one breath. The nudge keeps the tick because it is the
    // time-critical one (it must land before lunch is decided); the question waits for the next tick,
    // which costs 60 seconds out of its own 120-minute window, and only on a day a nudge went out.
    if (!nudgeSentThisTick) {
      // NOT futureEvents: "is the user mid-meeting right now" is a question only the event that has
      // ALREADY started can answer, and futureEvents drops exactly those. The MENTAL lookback the
      // fetch above already carries is what makes the in-progress event visible here.
      const diet = await runOrgan({
        label: "organ:diet", uid: u.uid, log,
        run: () => (deps.diet || dietUserOnce)(u, now, {
          events: inProgressEvents(events), getLocationState: deps.getLocationState,
        }),
      });
      if (diet && (diet.status === "asked" || diet.status === "send_failed")) {
        log(`[diet] uid=${String(u.uid).slice(0, 12)} status=${diet.status} day=${diet.day}`);
      }
    }
  }
  // H4 ORG-precepts: the bedtime organ rides the same 60s tick, after DIET. Both of its legs hold a
  // durable claim in lm_precepts_log (UNIQUE (uid, day, kind)), so the 30-minute bedtime window
  // produces at most one message however many ticks pass through it, and both legs read and write
  // MENTAL's lm_mental_send_log so the evening budget is shared rather than doubled (H4 ⑤).
  //
  // The gate defaults ON (spec H4), `notifications_enabled` is honoured exactly as every other
  // unsolicited-message organ honours it, and the two legs get SEPARATE try/catch blocks: they share
  // a table but not a fate, and a calendar-history outage in the mirror must not cost the night its
  // question.
  if (preceptsEnabled() && u.notifications_enabled !== false) {
    let mirrorSentThisTick = false;
    // The mirror is evaluated FIRST because it is the rarer and more time-boxed of the two (one
    // Sunday, one window). Its own send records to lm_mental_send_log, which starts the 2h spacing
    // that suppresses the question on every later tick — but not on THIS one, because both legs
    // read their budget before either wrote. mirrorSentThisTick closes that gap: two unsolicited
    // messages zero seconds apart is the sermon these thresholds exist to prevent.
    const mirror = await runOrgan({
      label: "organ:precepts-mirror", uid: u.uid, log,
      run: () => (deps.preceptsMirror || preceptsMirrorOnce)(u, now, {
        events: inProgressEvents(events), getLocationState: deps.getLocationState,
        apiKey: deps.apiKey, calendar: deps.calendar, gmailAccountId: u.gmail_account_id,
      }),
    });
    if (mirror && mirror.status === "mirrored") {
      mirrorSentThisTick = true;
      // budgeted says whether lm_mental_send_log actually recorded this send. A false here means a
      // delivered message the SHARED 3/day cap cannot see, so it is printed rather than left in a
      // returned object nobody reads — the organ has already stood itself down for the day.
      log(`[precepts-mirror] uid=${String(u.uid).slice(0, 12)} day=${mirror.day}`
        + ` answers=${mirror.answerCount} pattern=${mirror.pattern} tg_message_id=${mirror.telegramMessageId}`
        + ` budgeted=${mirror.budgeted === true}`);
    }
    if (!mirrorSentThisTick) {
      // NOT futureEvents: "is the user mid-something right now" is a question only the event that
      // has ALREADY started can answer, and futureEvents drops exactly those.
      const precepts = await runOrgan({
        label: "organ:precepts", uid: u.uid, log,
        run: () => (deps.precepts || preceptsUserOnce)(u, now, {
          events: inProgressEvents(events), getLocationState: deps.getLocationState,
        }),
      });
      if (precepts && (precepts.status === "asked" || precepts.status === "send_failed")) {
        // Same reason as the mirror's line: budgeted=false is a delivered message the shared cap
        // never counted, and a silent cap drift is exactly what H4 ⑤ exists to prevent.
        log(`[precepts] uid=${String(u.uid).slice(0, 12)} status=${precepts.status} day=${precepts.day}`
          + ` budgeted=${precepts.budgeted === true}`);
      }
    }
  }
  // H5 ORG-relations: one source-honest Calendar cadence scan in the early evening. The runtime
  // owns durable scan/attempt claims and the shared MENTAL budget; this wrapper only isolates it.
  if (relationsEnabled() && u.notifications_enabled !== false) {
    const relations = await runOrgan({
      label: "organ:relations", uid: u.uid, log,
      run: () => (deps.relations || relationsUserOnce)(u, now, {
        events: inProgressEvents(events),
        getLocationState: deps.getLocationState,
        apiKey: deps.apiKey,
        calendar: deps.calendar,
        gmailAccountId: u.gmail_account_id,
      }),
    });
    if (relations && relations.status === "suggested") {
      log(`[relations] uid=${String(u.uid).slice(0, 12)} status=${relations.status}`
        + ` tg_message_id=${relations.telegramMessageId} budgeted=${relations.budgeted === true}`);
    }
  }
}

// wakeUserOnce — kept as the composition of both halves. The Inngest per-user path
// (inngest/functions.js makeWakeUserHandler) and the 1a/1b test suites call this name, and a
// per-user Inngest run has no sibling users to protect, so running both halves there is correct.
async function wakeUserOnce(u, nowMs, deps = {}) {
  const wakeTimeoutMs = deps.wakeTimeoutMs !== undefined
    ? deps.wakeTimeoutMs
    : (deps.callTimeoutMs !== undefined ? deps.callTimeoutMs : WAKE_USER_TIMEOUT_MS);
  try {
    await withTimeout(() => (deps.wakeCall || wakeCallOnce)(u, nowMs, deps), wakeTimeoutMs, "wake call");
  } catch (e) {
    // The call is deadline-critical and remains first, but a failure in its calendar/claim path
    // must not strand the Telegram and daily organs for this user. Do not print event text here.
    console.error(`[wake] call organ uid=${String(u && u.uid || "?").slice(0, 12)} err ${e && e.message}`);
  }
  const reminderTimeoutMs = deps.reminderTimeoutMs !== undefined
    ? deps.reminderTimeoutMs
    : (deps.travelReminderTimeoutMs !== undefined ? deps.travelReminderTimeoutMs : REMINDER_TIMEOUT_MS);
  try {
    await withTimeout(() => reminderUserOnce(u, nowMs, deps), reminderTimeoutMs, "reminder");
  } catch (e) {
    console.error(`[wake] reminder organ uid=${String(u && u.uid || "?").slice(0, 12)} err ${e && e.message}`);
  }
  await organsUserOnce(u, nowMs, deps);
}

// forEachUserSafe: process each tenant in ISOLATION (HARD-4). A throw/rejection while handling one user is
// caught + logged per-uid so it NEVER prevents the remaining tenants from being processed this tick. This
// mirrors the production Inngest model (each user is a separate function run); it hardens the in-process
// (LIFE_RUN_LOOPS) path to the same one-tenant-failure-can't-break-others guarantee.
const USER_TICK_TIMEOUT_MS = Number(process.env.LIFE_USER_TICK_TIMEOUT_MS) || 90000;
async function forEachUserSafe(users, label, fn, timeoutMs = USER_TICK_TIMEOUT_MS) {
  for (const u of (users || [])) {
    const uid = (u && u.uid ? String(u.uid) : "?").slice(0, 12);
    try {
      // FIND-002: a per-user TIMEOUT so a HANG (not just a throw) in one tenant's upstream (dial/Gemini with
      // no AbortController) cannot stall the others. The abandoned op may still finish — idempotent via C-H1.
      let timer;
      const guard = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`tenant timeout ${timeoutMs}ms`)), timeoutMs); });
      try { await Promise.race([Promise.resolve(fn(u)), guard]); }
      finally { clearTimeout(timer); }
    } catch (e) {
      console.error(`[${label}] uid=${uid} err ${e && e.message}`);
    }
  }
}

// tick/travelTick/askTickAll accept optional injected deps (listUsers + the per-user fn) so a test can drive
// the REAL public loop with a throwing tenant and prove it routes through forEachUserSafe (FIND-001) — a
// future revert to a raw for-loop would then fail the test, not pass silently.
async function tick(deps = {}) {
  const listUsers = deps.listUsers || supaUsers;
  // The organ half only. The dial moved to wakeTick above, on its own timer and its own deadline.
  // `deps.wake` stays accepted as the legacy name for THIS tick's per-user fn — the isolation and
  // runtime-gate suites inject it to prove tick() still routes every tenant through forEachUserSafe,
  // and that assertion is about the tick's fan-out, not about which half it now drives.
  const organs = deps.organs || deps.wake || organsUserOnce;
  const users = await listUsers();
  const now = deps.now !== undefined ? deps.now : Date.now();
  // No `call_enabled` filter here (spec §3 row 1e). It was inherited from the days when the dial ran
  // inside this tick; the dial now has its own loop and applies that filter itself. Keeping a copy
  // here meant a user who never gave a phone number got NO organs at all — care, diet, mental,
  // precepts and relations have nothing to do with a phone, and §5.3 promises that user the same
  // product over Telegram. `daily_automation_enabled` is the real opt-out and still applies.
  await forEachUserSafe(users.filter(u => u.daily_automation_enabled !== false), "scheduler", (u) => organs(u, now));
}

// The wake call gets its own timer and its own budget. 20 seconds is sized to what wakeCallOnce
// actually does — one calendar fetch, one departure resolve, one dial — where 90s was sized for the
// care organ's browser work. A user who blows 20s here still cannot delay the next user's dial.
const WAKE_USER_TIMEOUT_MS = Number(process.env.LIFE_WAKE_USER_TIMEOUT_MS) || 20000;

async function wakeTick(deps = {}) {
  const listUsers = deps.listUsers || supaUsers;
  const wake = deps.wake || wakeCallOnce;
  const users = await listUsers();
  const now = deps.now !== undefined ? deps.now : Date.now();
  await forEachUserSafe(
    // `call_enabled === true` plus strict E.164 phone (spec §5.2.1): the phone is an extra someone opts into,
    // not the default. Missing/malformed values must never enter the dial path.
    // `daily_automation_enabled !== false` keeps its opt-OUT sense — that switch means "run nothing
    // for me", and it is not the thing §5.2.1 flipped.
    users.filter(u => u.daily_automation_enabled !== false && u.paid === true
      && u.call_enabled === true && isCallablePhone(u.phone)),
    "wake", (u) => wake(u, now), WAKE_USER_TIMEOUT_MS,
  );
}

// The T-5 Telegram reminder is deadline-critical but does not require a phone or call opt-in. Keep
// it off the degradable organ tick: each enabled tenant starts together, and one route/Telegram
// stall is abandoned independently without delaying another tenant's deadline.
async function reminderTick(deps = {}) {
  const listUsers = deps.listUsers || supaUsers;
  const reminder = deps.reminder || reminderUserOnce;
  const users = await listUsers();
  const now = deps.now !== undefined ? deps.now : Date.now();
  const timeoutMs = deps.reminderTimeoutMs !== undefined
    ? deps.reminderTimeoutMs
    : (deps.travelReminderTimeoutMs !== undefined ? deps.travelReminderTimeoutMs : REMINDER_TIMEOUT_MS);
  const eligible = (Array.isArray(users) ? users : [])
    .filter(u => u && u.daily_automation_enabled !== false && u.notifications_enabled !== false);
  await Promise.all(eligible.map(async (u) => {
    try {
      await withTimeout(() => reminder(u, now, deps), timeoutMs, "reminder");
    } catch (e) {
      console.error(`[reminder] uid=${String(u.uid).slice(0, 12)} err ${e && e.message}`);
    }
  }));
}

// Fixed 60s, deliberately NOT schedulerPollInterval(): the Composio budget degradation that slows the
// organ tick to 5 minutes must not slow the dial (that defect is spec row #1d, tracked separately).
// The wake tick owns the calendar fetch, so this loop's call volume equals the old combined tick's.
function startWakeLoop() {
  console.log(`[wake] started — dedicated tick every ${TICK_MS / 1000}s, ${WAKE_USER_TIMEOUT_MS / 1000}s per user, wakes at T-${WAKE_LEVELS.map((l) => l.min).join("/")}min`);
  let timer;
  const run = async () => {
    try { await wakeTick(); } catch (e) { console.error("[wake] tick err", e.message); }
    timer = setTimeout(run, TICK_MS);
  };
  run();
  return { close: () => clearTimeout(timer) };
}

// Fixed 60s, deliberately independent from schedulerPollInterval(): Composio budget degradation must
// never turn the deadline-critical Telegram reminder into a 5-minute organ. Reserve the next start
// before awaiting this tick so slow provider work cannot move the wall-clock boundary.
function startReminderLoop(options = {}) {
  console.log(`[reminder] started — dedicated tick every ${TICK_MS / 1000}s, ${REMINDER_TIMEOUT_MS / 1000}s per tenant`);
  const opts = options || {};
  const runReminderTick = opts.reminderTick || opts.tick || reminderTick;
  const schedule = opts.setTimeout || setTimeout;
  const cancel = opts.clearTimeout || clearTimeout;
  let timer;
  let closed = false;
  const run = async () => {
    if (closed) return;
    timer = schedule(run, TICK_MS);
    try { await runReminderTick(); } catch (e) { console.error("[reminder] tick err", e.message); }
  };
  run();
  return {
    close: () => {
      closed = true;
      if (timer !== undefined) cancel(timer);
    },
  };
}

function startScheduler() {
  if (!process.env.PUBLIC_WSS) {
    console.warn("[scheduler] PUBLIC_WSS not set — calls would have no media bridge URL; loop still runs but won't dial");
  }
  console.log(`[scheduler] started — organ tick every ${TICK_MS / 1000}s (wake runs on its own loop)`);
  let timer;
  const run = async () => {
    try { await tick(); } catch (e) { console.error("[scheduler] tick err", e.message); }
    const intervalMs = await schedulerPollInterval().catch(() => TICK_MS);
    timer = setTimeout(run, intervalMs);
  };
  run();
  return { close: () => clearTimeout(timer) };
}

// ── Travel auto-fill (every 30 min) — keep today+7d filled with [Travel] blocks ─────────────────
const TRAVEL_TICK_MS = 30 * 60 * 1000;

async function travelUserOnce(u, deps = {}) {
  if (u && u.daily_automation_enabled === false) return;
  const apiKey = deps.apiKey || process.env.COMPOSIO_API_KEY;
  const mapsKey = deps.mapsKey || process.env.LIFE_MAPS_KEY || process.env.GOOGLE_API_KEY;
  const geminiKey = deps.geminiKey || process.env.GEMINI_API_KEY; // agentic resolve of room-name / unroutable locations
  if (!apiKey || !mapsKey) return;
  const configuredSupa = SUPA();
  const supaUrl = deps.supaUrl !== undefined ? deps.supaUrl : configuredSupa.url;
  const supaKey = deps.supaKey !== undefined ? deps.supaKey : configuredSupa.key;
  try {
    const r = await (deps.fillTravel || fillTravel)(u.uid, {
      apiKey, mapsKey, geminiKey, home: u.home_address,
      timezone: u.call_time_zone,
      nowMs: deps.nowMs === undefined ? Date.now() : deps.nowMs,
      calendar: deps.calendar, supaUrl, supaKey,
      _directionsMinutes: deps.directionsMinutes,
      _reserveManagedAction: deps.reserveManagedAction || (deps.fillTravel ? undefined : reserveManagedAction),
      _completeManagedAction: deps.completeManagedAction || (deps.fillTravel ? undefined : completeManagedAction),
      _releaseManagedAction: deps.releaseManagedAction || (deps.fillTravel ? undefined : releaseManagedAction),
      gmailAccountId: u.gmail_account_id,
    });
    if (r.inserted) console.log(`[travel] uid=${u.uid.slice(0, 12)} inserted=${r.inserted} checked=${r.checked}`);
    const telegramToken = deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN;
    if (u.notifications_enabled !== false && telegramToken && u.telegram_chat_id) {
      for (const report of r.outboundReports || []) {
        try {
          await (deps.sendMessage || sendMessage)(telegramToken, u.telegram_chat_id,
            formatTravelAutofillMessage(report, deps.nowMs === undefined ? Date.now() : deps.nowMs));
        } catch (error) {
          console.error(`[travel] uid=${u.uid.slice(0, 12)} report send failed: ${error && error.message}`);
        }
      }
    }
    return r;
  } catch (e) {
    console.error(`[travel] uid=${u.uid.slice(0, 12)} err ${e.message}`);
  }
}

async function travelTick(deps = {}) {
  const apiKey = process.env.COMPOSIO_API_KEY;
  const mapsKey = process.env.LIFE_MAPS_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey || !mapsKey) return;
  const listUsers = deps.listUsers || supaUsers;
  const travel = deps.travel || travelUserOnce;
  const users = await listUsers();
  await forEachUserSafe(users.filter(u => u.daily_automation_enabled !== false), "travel", travel);
}
function startTravelLoop() {
  console.log(`[travel] started — every ${TRAVEL_TICK_MS / 60000}min, horizon 7d`);
  const run = () => travelTick().catch((e) => console.error("[travel] tick err", e.message));
  run();
  return setInterval(run, TRAVEL_TICK_MS);
}

// ── Ask/reply loop (every 20 min) — ask the user about events missing a location (Telegram or our-domain
// email via Resend); replies arrive on webhooks (/telegram, /inbound-email), not polled here ──
const ASK_TICK_MS = 20 * 60 * 1000;

async function askUserOnce(u) {
  if (u && (u.daily_automation_enabled === false || u.notifications_enabled === false)) return;
  const composioKey = process.env.COMPOSIO_API_KEY;
  const resendKey = process.env.RESEND_API_KEY;                            // our-domain email send
  const mapsKey = process.env.LIFE_MAPS_KEY || process.env.GOOGLE_API_KEY; // Places grounding
  const geminiKey = process.env.GEMINI_API_KEY;                            // agentic resolve/read
  const telegramToken = process.env.LM_TELEGRAM_BOT_TOKEN;                 // Telegram ask channel
  const { url: supaUrl, key: supaKey } = SUPA();
  if (!composioKey || !supaUrl || !geminiKey) return;
  // A user is reachable for asks via Telegram OR their email (captured at sign-in) — need at least one.
  if (!u.telegram_chat_id && !u.email) return;
  try {
    const r = await askTick(u.uid, {
      composioKey, userEmail: u.email, resendKey,
      supaUrl, supaKey, mapsKey, geminiKey, home: u.home_address,
      telegramChatId: u.telegram_chat_id, telegramToken,
      gmailAccountId: u.gmail_account_id,
      unipileToken: process.env.UNIPILE_TOKEN,
      unipileDsn: process.env.UNIPILE_DSN,
    });
    if (r.autofilled || r.asked || r.resolved)
      console.log(`[ask] uid=${u.uid.slice(0, 12)} autofilled=${r.autofilled} asked=${r.asked} resolved=${r.resolved} via=${u.telegram_chat_id ? "tg" : "email"}`);
  } catch (e) { console.error(`[ask] uid=${u.uid.slice(0, 12)} err ${e.message}`); }
}

async function askTickAll(deps = {}) {
  const composioKey = process.env.COMPOSIO_API_KEY;
  const { url: supaUrl } = SUPA();
  const geminiKey = process.env.GEMINI_API_KEY;
  if (!composioKey || !supaUrl || !geminiKey) return;
  const listUsers = deps.listUsers || supaUsers;
  const ask = deps.ask || askUserOnce;
  const users = await listUsers();
  await forEachUserSafe(users.filter(u => u.daily_automation_enabled !== false && u.notifications_enabled !== false), "ask", ask);
}
function startAskLoop() {
  console.log(`[ask] started — every ${ASK_TICK_MS / 60000}min`);
  const run = () => askTickAll().catch((e) => console.error("[ask] tick err", e.message));
  run();
  return setInterval(run, ASK_TICK_MS);
}

// ── Interactive Telegram onboarding nudge (every 2 min) — guide linked users to their next step ────
const ONBOARD_TICK_MS = 2 * 60 * 1000;
async function onboardTick() {
  const token = process.env.LM_TELEGRAM_BOT_TOKEN;
  const base = process.env.PUBLIC_BASE || "https://aniccaai.com";
  const { url: supaUrl, key: supaKey } = SUPA();
  if (!token || !supaUrl) return;
  const sent = await onboardNudgeAll({
    token, base, supaUrl, supaKey,
    composioKey: process.env.COMPOSIO_API_KEY,
    geminiKey: process.env.GEMINI_API_KEY,
    uidSecret: process.env.LM_UID_SECRET,
    gmailBase: process.env.LIFE_CALL_PUBLIC_BASE || process.env.PUBLIC_WSS || base,
    gmailConfigured: Boolean(process.env.LM_UID_SECRET && process.env.UNIPILE_DSN &&
      process.env.UNIPILE_TOKEN && process.env.UNIPILE_NOTIFY_SECRET),
  });
  if (sent) console.log(`[onboard] nudged ${sent} Telegram user(s) to their next step`);
}
function startOnboardLoop() {
  console.log(`[onboard] started — every ${ONBOARD_TICK_MS / 60000}min (interactive Telegram guidance)`);
  const run = () => onboardTick().catch((e) => console.error("[onboard] tick err", e.message));
  run();
  return setInterval(run, ONBOARD_TICK_MS);
}

// ── Context-gate feature discovery (weekly) ────────────────────────────────
// The per-user last_discovery_at gate is durable, so process restarts do not
// increase frequency. Each run re-reads live-location freshness before send.
async function discoveryTick(deps = {}) {
  const { url: supaUrl, key: supaKey } = SUPA();
  const token = process.env.LM_TELEGRAM_BOT_TOKEN;
  if (!token || !supaUrl || !supaKey) return;
  const dbOpts = { supaUrl, supaKey, fetchImpl: deps.fetchImpl };
  const listUsers = deps.listUsers || (() => listDiscoveryUsers(dbOpts));
  const discover = deps.discover || ((user, nowMs) => runDiscoveryForUser(user, nowMs, {
    ...dbOpts, token,
  }));
  const users = await listUsers();
  const now = deps.now !== undefined ? deps.now : Date.now();
  await forEachUserSafe(users.filter(user => user.notifications_enabled !== false), "discovery", (user) => discover(user, now));
}

function startDiscoveryLoop() {
  console.log("[discovery] started — weekly, one locked gate per eligible Telegram user");
  const run = () => discoveryTick().catch((error) =>
    console.error("[discovery] tick err", error && error.message));
  run();
  return setInterval(run, DISCOVERY_WEEK_MS);
}

// listPaidUsers: public alias for supaUsers — used by Inngest sweep functions.
const listPaidUsers = supaUsers;

// getUserByUid: re-fetches a single user row by uid for Inngest per-user functions.
// Inngest sweepers fan-out only { uid } (PII-safe); the per-user handler calls this
// to get the full row (phone, home_address, etc.) before invoking the scheduler fn.
// Uses the same column set as supaUsers to keep behaviour identical.
async function getUserByUid(uid) {
  const { url, key } = SUPA();
  if (!url || !key || !uid) return null;
  const cols = "uid,name,phone,paid,calendar_provider,home_address,gmail_account_id,email,telegram_chat_id,call_language";
  const base = `${url}/rest/v1/lm_users?uid=eq.${encodeURIComponent(uid)}&${schedulerCohortFilter()}`;
  const hdr = { apikey: key, Authorization: `Bearer ${key}` };
  let r = await fetch(`${base}&select=${cols},wake_policy`, { headers: hdr });
  if (!r.ok) r = await fetch(`${base}&select=${cols}`, { headers: hdr });
  if (!r.ok) return null;
  const rows = await r.json().catch(() => []);
  const user = Array.isArray(rows) && rows[0] ? rows[0] : null;
  if (!user) return null;
  const prefs = await readRuntimePreferences(uid, { supaUrl: url, supaKey: key, fetchImpl: fetch });
  return prefs ? { ...user, ...prefs } : { ...user, call_enabled: false, notifications_enabled: false, daily_automation_enabled: false };
}

module.exports = {
  startScheduler, startWakeLoop, startReminderLoop, startTravelLoop, startAskLoop, startOnboardLoop, startDiscoveryLoop,
  tick, wakeTick, reminderTick, travelTick, askTickAll, onboardTick, discoveryTick,
  // the wake loop's own per-user budget — exported so a revert to the shared 90s is test-caught
  WAKE_USER_TIMEOUT_MS,
  // per-user single-invocation functions (for Inngest fan-out + testing)
  wakeUserOnce, reminderUserOnce, travelUserOnce, askUserOnce,
  // the two halves of the old wakeUserOnce — separate timers drive them (spec §3.1 method A)
  wakeCallOnce, organsUserOnce,
  REMINDER_TIMEOUT_MS,
  lateNoticeUserOnce,
  // per-tenant isolation wrapper (HARD-4): one tenant's failure can't break the others' tick
  forEachUserSafe,
  // wake escalation levels (Dais: T-10 firm + T-5 harsh only) — exported so a revert is test-caught
  WAKE_LEVELS,
  // how late a missed level may still be caught up (past it, the late-notice organ owns the event)
  LATE_CUTOFF_MIN,
  // paid-user listing (for Inngest sweep fan-out)
  listPaidUsers,
  // per-uid re-fetch for Inngest per-user functions (PII: sweepers send only uid)
  getUserByUid,
  // utilities used by server.js and tests
  isHelperBlock, buildStreamUrl, langForPhone, langForUser,
  // wake claim ledger (C-H1 dedup) — claim before dial, release on dial failure so a retry can fire
  claimWake, releaseWake,
  // low-balance admin alert (issue#10): pure decision fns + the side-effecting sender
  isLowBalanceError, shouldAlertLowBalance, maybeAlertLowBalance, LOW_BALANCE_ALERT_COOLDOWN_MS,
};
