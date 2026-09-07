// Claimed T-5 event/travel Telegram reminder.
"use strict";

const crypto = require("node:crypto");
const { directionsRoute, claimTravel, unclaimTravel, recordTravelTelegramReceipt } = require("./travel.js");
const { isHelperBlock } = require("./wake-filter.js");
const { sendMessage } = require("./telegram.js");
const { computeDoorDepartureMs, computeT5ReminderMs } = require("./travel-timing.js");

const T5_MS = 5 * 60 * 1000;
const CATCH_UP_MS = 15 * 60 * 1000;
const REMINDER_LOOKBACK_MS = CATCH_UP_MS - T5_MS;
const PREVIOUS_EVENT_WINDOW_MS = 90 * 60 * 1000;
const DEFAULT_TIMEZONE = "Asia/Tokyo";
const ALLOWANCE_COPY = "今月の無料利用分を使い切りました。設定とこれまでの情報はそのまま残っています。翌月に無料利用分が戻ります。";

function toMs(value) {
  if (value instanceof Date) value = value.getTime();
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim()) {
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  }
  return null;
}
function startMs(event) { return toMs(event && event.startMs); }
function endMs(event) { return toMs(event && event.endMs); }
function helper(event) { return Boolean(event && isHelperBlock(event.summary || "")); }
function physical(event) {
  const online = event && (event.online === true || event.isOnline === true);
  return Boolean(event && String(event.location || "").trim() && !online);
}

function travelHelper(event) {
  const summary = String(event && event.summary || "");
  return summary.startsWith("[Travel]") || summary.includes("🚆 移動");
}

function normalizeLocation(value) {
  return String(value || "").normalize("NFKC").replace(/[－ー‐‑–—]/g, "-").replace(/\s+/g, "").toLowerCase();
}

// A Travel block's end-window adjacency is not enough on its own: the same block can also fall in
// ANOTHER nearby timed event's [start-2min, start+1min] window (e.g. two back-to-back events with a
// single travel leg between them). If it matches more than one event's window, which one it belongs
// to is genuinely ambiguous, so fail closed to the event's own location rather than misattribute it.
function matchesOtherEventWindow(candidateEnd, events, event, candidate) {
  return events.some((other) => {
    if (!other || other === event || other === candidate) return false;
    if (other.id && (other.id === event.id || (candidate.id && other.id === candidate.id))) return false;
    if (helper(other)) return false;
    const otherStart = startMs(other);
    return otherStart !== null && candidateEnd >= otherStart - 2 * 60000 && candidateEnd <= otherStart + 60000;
  });
}

function matchesOtherEventEnd(candidateStart, events, event, candidate) {
  return otherEventsAtEnd(candidateStart, events, event, candidate).length > 0;
}

function otherEventsAtEnd(candidateStart, events, event, candidate) {
  return events.filter((other) => {
    if (!other || other === event || other === candidate) return false;
    if (other.id && (other.id === event.id || (candidate.id && other.id === candidate.id))) return false;
    if (helper(other)) return false;
    const otherEnd = endMs(other);
    return otherEnd !== null
      && candidateStart >= otherEnd - 60000
      && candidateStart <= otherEnd + 60000;
  });
}

function resolveReminderDestination(event, { events = [], home, targetGoClaimed = false, previousReturnClaims = new Map() } = {}) {
  const fallback = String(event && event.location || "").trim();
  const normalizedHome = normalizeLocation(home);
  const start = startMs(event), end = endMs(event);
  const list = Array.isArray(events) ? events : [];
  const matches = [];
  for (const candidate of list) {
    if (!candidate || candidate === event || (candidate.id && candidate.id === event.id) || !travelHelper(candidate)) continue;
    const candidateStart = startMs(candidate), candidateEnd = endMs(candidate);
    const location = String(candidate.location || "").trim();
    if (!location || (normalizedHome && normalizeLocation(location) === normalizedHome) || start === null || candidateStart === null || candidateEnd === null) continue;
    if (candidateEnd < start - 2 * 60000 || candidateEnd > start + 60000 || candidateStart > start || (end !== null && candidateStart >= end)) continue;
    if (matchesOtherEventWindow(candidateEnd, list, event, candidate)) continue;
    const previousEvents = otherEventsAtEnd(candidateStart, list, event, candidate);
    if (previousEvents.length && (targetGoClaimed !== true
      || previousEvents.some((other) => previousReturnClaims.get(eventKey(other)) !== false))) continue;
    matches.push({ start: candidateStart, location });
  }
  return matches.length === 1 ? matches[0].location : fallback;
}

function reminderEvents(events, nowMs = Date.now()) {
  const now = toMs(nowMs);
  if (now === null) return [];
  return (Array.isArray(events) ? events : [])
    .filter((event) => startMs(event) !== null && startMs(event) >= now - REMINDER_LOOKBACK_MS && !helper(event))
    .sort((a, b) => startMs(a) - startMs(b));
}

function nextReminderEvent(events, nowMs = Date.now()) {
  return reminderEvents(events, nowMs)[0] || null;
}

function freshLive(location, nowMs) {
  if (!location || typeof location !== "object") return null;
  const lat = Number(location.latitude);
  const lon = Number(location.longitude);
  const observed = toMs(location.observedAtMs ?? location.observed_at);
  const expires = toMs(location.expiresAtMs ?? location.expires_at);
  const now = toMs(nowMs);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || observed === null || expires === null || now === null) return null;
  return expires > now && observed <= now ? { lat, lon } : null;
}

function resolveReminderOrigin(event, { events = [], liveLocation, home, nowMs = Date.now() } = {}) {
  const live = freshLive(liveLocation, nowMs);
  if (live) return { kind: "live", value: `geo:${live.lat},${live.lon}` };
  const start = startMs(event);
  let previous = null;
  for (const candidate of Array.isArray(events) ? events : []) {
    if (!candidate || candidate === event || (candidate.id && candidate.id === event.id) || helper(candidate)) continue;
    const location = String(candidate.location || "").trim();
    const end = endMs(candidate);
    if (!location || start === null || end === null || end > start || start - end > PREVIOUS_EVENT_WINDOW_MS) continue;
    if (!previous || end > previous.end) previous = { end, location };
  }
  if (previous) return { kind: "previous", value: previous.location };
  const address = String(home || "").trim();
  return address ? { kind: "home", value: address } : null;
}

function computeDepartureMs(event, route, { bufferMin = 5 } = {}) {
  const start = startMs(event);
  if (start === null || !physical(event)) return start;
  return computeDoorDepartureMs(start, route, { bufferMin }) ?? start;
}

function computeReminderDueAt(event, { departureMs, route, bufferMin = 5 } = {}) {
  const departure = toMs(departureMs) ?? computeDepartureMs(event, route, { bufferMin });
  return computeT5ReminderMs(departure);
}

function isReminderDue(nowMs, dueAt) {
  const now = toMs(nowMs);
  const due = toMs(dueAt);
  return now !== null && due !== null && now >= due && now <= due + CATCH_UP_MS;
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/\"/g, "&quot;").replace(/'/g, "&#39;");
}

function timezone(value) {
  const zone = String(value || "").trim() || DEFAULT_TIMEZONE;
  try { new Intl.DateTimeFormat("en", { timeZone: zone }).format(0); return zone; }
  catch { return DEFAULT_TIMEZONE; }
}
function timeText(value, zone) {
  const ms = toMs(value);
  return ms === null ? null : new Intl.DateTimeFormat("ja-JP", {
    timeZone: timezone(zone), hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(ms));
}
function stopText(stop) {
  if (!stop || typeof stop !== "object" || !String(stop.name || "").trim()) return null;
  const platform = String(stop.platform || "").trim();
  return escapeHtml(String(stop.name).trim()) + (platform ? ` ${escapeHtml(platform)}` : "");
}
function isWalk(step) { return Boolean(step && (step.kind === "walk" || step.mode === "walk")); }
function walkingMinutes(seconds) {
  if ((typeof seconds !== "number" && typeof seconds !== "string") || String(seconds).trim() === "") return null;
  const n = Number(seconds);
  return Number.isFinite(n) && n >= 0 ? Math.ceil(n / 60) : null;
}
function stepText(step, transitIndex, zone) {
  if (!step || typeof step !== "object") return null;
  const from = stopText(step.from), to = stopText(step.to);
  const depart = timeText(step.departAt, zone), arrive = timeText(step.arriveAt, zone);
  if (isWalk(step)) {
    const start = toMs(step.departAt), end = toMs(step.arriveAt);
    const minutes = start !== null && end !== null ? walkingMinutes((end - start) / 1000) : null;
    const path = from && to ? `${from} → ${to}` : "徒歩区間";
    return `🚶 ${path}\n${minutes === null ? "徒歩（所要時間不明）" : `徒歩 ${minutes}分`}`;
  }
  if (!from || !to || !depart || !arrive) return null;
  const service = [step.service, step.trainType, step.headsign].filter((v) => v != null && String(v).trim())
    .map((v) => escapeHtml(String(v).trim())).join("・");
  if (!service) return `${depart} ${from} → ${arrive} ${to}`;
  return transitIndex === 0 ? `${depart} ${from}\n${service} → ${arrive} ${to}`
    : `${depart} ${from}から${service} → ${arrive} ${to}`;
}
// Access/egress summaries fill only absent edge steps, never duplicate a detailed walking leg.
function edgeWalkText(step, seconds, access) {
  const minutes = walkingMinutes(seconds);
  if (!step || isWalk(step) || minutes === null || minutes <= 0) return null;
  const station = stopText(access ? step.from : step.to);
  if (!station) return null;
  return `🚶 ${access ? `出発地 → ${station}` : `${station} → 目的地`}\n徒歩 ${minutes}分`;
}
function fareText(fare) {
  if (!fare || typeof fare !== "object") return null;
  const currency = String(fare.currency || "").trim().toUpperCase();
  const suffix = currency === "JPY" || !currency ? "円" : ` ${escapeHtml(currency)}`;
  const ic = fare.ic === null || fare.ic === undefined || fare.ic === "" ? null : Number(fare.ic);
  const ticket = fare.ticket === null || fare.ticket === undefined || fare.ticket === "" ? null : Number(fare.ticket);
  if (Number.isFinite(ic)) return `IC ${escapeHtml(ic)}${suffix}`;
  if (Number.isFinite(ticket)) return `切符 ${escapeHtml(ticket)}${suffix}`;
  return null;
}

function onlineDetailsUrl(location) {
  const raw = String(location || "").trim().replace(/^(?:オンライン|online)\s*[:：]\s*/iu, "");
  if (!/^https:\/\/[^\s<>"'\\]+$/iu.test(raw)) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

// This formatter emits escaped text, not HTML tags. Bound the encoded form conservatively so one
// claim still corresponds to one sendMessage, without splitting an entity or Unicode code point.
function reminderText(lines) {
  const text = lines.join("\n");
  if (text.length <= 4096) return text;
  const suffix = "\n…（詳細が長いため一部省略）";
  let prefix = "";
  for (const match of text.matchAll(/&(?:amp|lt|gt|quot|#39);|[\s\S]/gu)) {
    if (prefix.length + match[0].length + suffix.length > 4096) break;
    prefix += match[0];
  }
  return prefix + suffix;
}

function formatTravelReminder(event, route, { departureMs, timezone: zone = DEFAULT_TIMEZONE, routeAttempted } = {}) {
  const start = startMs(event);
  if (start === null) return "";
  const online = event.online === true || event.isOnline === true;
  const steps = (Array.isArray(route && route.steps) ? route.steps : []).filter((step) => step && typeof step === "object");
  const walkOnly = steps.length > 0 && steps.every(isWalk);
  const icon = online ? "💻" : !physical(event) ? "🔔" : walkOnly ? "🚶" : "🚆";
  const lines = [`${icon} 次は ${timeText(start, zone)}「${escapeHtml(event.summary || "予定")}」`];
  if (!physical(event)) {
    const url = online && onlineDetailsUrl(event.location);
    if (url) lines.push("", `イベント詳細: ${escapeHtml(url)}`);
    return reminderText(lines);
  }
  const departure = route ? toMs(departureMs) ?? computeDepartureMs(event, route) : null;
  const arrival = timeText(route && route.arrivalAt, zone);
  if (departure !== null) lines.push(`${timeText(departure, zone)} 出発${arrival ? ` → ${arrival} 到着予定` : ""}`);
  else if (arrival) lines.push(`${arrival} 到着予定`);
  lines.push(`目的地: ${escapeHtml(String(event.location || "").trim())}`);
  const attempted = routeAttempted === undefined ? true : routeAttempted === true;
  if (!route) return reminderText(attempted ? lines.concat("", "経路を取得できませんでした。") : lines);
  const rendered = [];
  const access = edgeWalkText(steps[0], route.accessWalkSeconds, true);
  if (access) rendered.push(access);
  let index = 0;
  for (const step of steps) {
    const text = stepText(step, index, zone);
    if (text) rendered.push(text);
    if (!isWalk(step) && text) index += 1;
  }
  const egress = edgeWalkText(steps[steps.length - 1], route.egressWalkSeconds, false);
  if (egress) rendered.push(egress);
  if (rendered.length) lines.push("", ...rendered);
  const facts = [];
  const transfers = route.transferCount === null || route.transferCount === undefined || route.transferCount === ""
    ? null : Number(route.transferCount);
  if (Number.isFinite(transfers)) facts.push(`乗換 ${transfers}回`);
  facts.push(fareText(route.fare));
  const present = facts.filter(Boolean);
  if (present.length) lines.push(present.join(" / "));
  lines.push("", "※ 出口番号は経路元が返した場合だけ表示します。運行情報が変わることがあります。");
  return reminderText(lines);
}

function eventKey(event) {
  return String(event.id || `${startMs(event) === null ? "unknown" : startMs(event)}:${event.summary || ""}`);
}

function postgrestEqValue(value) {
  return encodeURIComponent(String(value));
}

async function readTravelClaim(uid, key, leg, supaUrl, supaKey, fetchImpl = fetch) {
  if (!supaUrl || !supaKey) return false;
  if (fetchImpl === fetch) {
    try { new URL(String(supaUrl)); } catch { return false; }
  }
  const response = await fetchImpl(
    `${supaUrl}/rest/v1/lm_travel_log?uid=eq.${postgrestEqValue(uid)}&event_key=eq.${postgrestEqValue(key)}&leg=eq.${postgrestEqValue(leg)}&select=event_key&limit=1`,
    { headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}` } },
  ).catch(() => null);
  if (!response || response.ok === false
      || (response.ok !== true && (!Number.isInteger(response.status) || response.status < 200 || response.status >= 300))) return null;
  const rows = await response.json().catch(() => null);
  if (!Array.isArray(rows)) return null;
  return rows.some((row) => row && String(row.event_key || "") === String(key));
}

function previousEventsForDestination(event, { events = [], home } = {}) {
  const start = startMs(event), end = endMs(event);
  const normalizedHome = normalizeLocation(home);
  const list = Array.isArray(events) ? events : [];
  const previous = new Map();
  if (start === null) return [];
  for (const candidate of list) {
    if (!candidate || candidate === event || (candidate.id && candidate.id === event.id) || !travelHelper(candidate)) continue;
    const candidateStart = startMs(candidate), candidateEnd = endMs(candidate);
    const location = String(candidate.location || "").trim();
    if (!location || (normalizedHome && normalizeLocation(location) === normalizedHome)
      || candidateStart === null || candidateEnd === null) continue;
    if (candidateEnd < start - 2 * 60000 || candidateEnd > start + 60000 || candidateStart > start
      || (end !== null && candidateStart >= end) || matchesOtherEventWindow(candidateEnd, list, event, candidate)) continue;
    for (const other of otherEventsAtEnd(candidateStart, list, event, candidate)) previous.set(eventKey(other), other);
  }
  return previous.values();
}

function logReconciliation(deps, message) {
  try { (deps.logError || deps.log || console.error)(message); } catch { /* keep loop alive */ }
}

async function travelReminderOnce(user, nowMs = Date.now(), deps = {}) {
  const now = toMs(nowMs), chatId = String(user && user.telegram_chat_id || "").trim();
  if (!user || !user.uid || !chatId || user.notifications_enabled === false || now === null) return { status: "skipped" };
  const token = deps.telegramToken !== undefined ? deps.telegramToken : process.env.LM_TELEGRAM_BOT_TOKEN;
  if (!token) return { status: "skipped", reason: "telegram-unconfigured" };
  const supaUrl = deps.supaUrl !== undefined ? deps.supaUrl : process.env.SUPABASE_URL;
  const supaKey = deps.supaKey !== undefined ? deps.supaKey : process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supaUrl || !supaKey) return { status: "skipped", reason: "travel-ledger-unconfigured" };
  const events = Array.isArray(deps.events) ? deps.events : [];
  const candidates = reminderEvents(events, now);
  if (!candidates.length) return { status: "suppressed", reason: "no-event" };
  const home = deps.home !== undefined ? deps.home : user.home_address;
  const prepareCandidate = async (event) => {
    const key = eventKey(event);
    let allowance = { allowed: true };
    if (physical(event) && typeof deps.reserveManagedAction === "function") {
      allowance = await deps.reserveManagedAction(user.uid, key, supaUrl, supaKey);
      if (!allowance || allowance.allowed !== true) {
        return { event, key, route: null, routeAttempted: false,
          departureMs: startMs(event), dueAt: computeReminderDueAt(event, { departureMs: startMs(event) }),
          allowanceBlocked: true, allowanceNotify: Boolean(allowance && allowance.notify === true) };
      }
    }
    let targetGoClaimed = false;
    const previousReturnClaims = new Map();
    if (physical(event)) {
      try {
        const association = deps.travelLogAssociation !== undefined ? deps.travelLogAssociation : deps.hasTravelGoClaim;
        targetGoClaimed = typeof association === "function"
          ? await association(user.uid, key, "go", supaUrl, supaKey) === true
          : association !== undefined ? association === true
            : await readTravelClaim(user.uid, key, "go", supaUrl, supaKey, deps.fetchImpl) === true;
        if (targetGoClaimed) {
          for (const previous of previousEventsForDestination(event, { events, home })) {
            previousReturnClaims.set(eventKey(previous), await readTravelClaim(
              user.uid, eventKey(previous), "return", supaUrl, supaKey, deps.fetchImpl,
            ));
          }
        }
      } catch { targetGoClaimed = false; }
    }
    const origin = resolveReminderOrigin(event, {
      events, liveLocation: deps.liveLocation,
      home, nowMs: now,
    });
    const routeAttempted = physical(event) && Boolean(origin);
    const destination = resolveReminderDestination(event, {
      events, home,
      targetGoClaimed, previousReturnClaims,
    });
    let route = null;
    if (routeAttempted) {
      try {
        route = await (deps.directionsRoute || directionsRoute)(origin.value, destination, deps.mapsKey,
          startMs(event), now, false, {
            uid: user.uid, timezone: deps.timezone || user.call_time_zone, supaUrl, supaKey,
            eventId: key, purpose: "go",
          });
      } catch { route = null; }
    }
    const departureMs = computeDepartureMs(event, route, { bufferMin: deps.bufferMin });
    const dueAt = computeReminderDueAt(event, { departureMs });
    return { event, key, route, routeAttempted, departureMs, dueAt };
  };
  const prepared = await Promise.all(candidates.map(prepareCandidate));
  const dueCandidates = prepared
    .filter((candidate) => isReminderDue(now, candidate.dueAt))
    .sort((a, b) => (a.dueAt - b.dueAt) || (startMs(a.event) - startMs(b.event)) || a.key.localeCompare(b.key));
  if (!dueCandidates.length) {
    const nextDueAt = prepared.reduce((earliest, candidate) => candidate.dueAt !== null
      && (earliest === null || candidate.dueAt < earliest) ? candidate.dueAt : earliest, null);
    return { status: "suppressed", reason: "not-due", dueAt: nextDueAt };
  }
  let selected = null;
  for (const candidate of dueCandidates) {
    let claimed = false;
    try { claimed = await (deps.claimTravel || claimTravel)(user.uid, candidate.key, "telegram-t5", supaUrl, supaKey); }
    catch { return { status: "suppressed", reason: "claim-failed" }; }
    if (claimed) {
      selected = candidate;
      break;
    }
  }
  if (!selected) return { status: "suppressed", reason: "duplicate" };
  const { event, key, route, routeAttempted, departureMs } = selected;
  if (selected.allowanceBlocked === true && selected.allowanceNotify !== true) {
    await (deps.unclaimTravel || unclaimTravel)(user.uid, key, "telegram-t5", supaUrl, supaKey);
    return { status: "suppressed", reason: "allowance-exhausted" };
  }
  let response = null;
  const message = selected.allowanceBlocked === true ? ALLOWANCE_COPY : formatTravelReminder(event, route, {
    departureMs, timezone: deps.timezone || user.call_time_zone || DEFAULT_TIMEZONE, routeAttempted,
  });
  try { response = await (deps.sendMessage || sendMessage)(token, chatId, message); }
  catch { response = { ok: false, delivery_unknown: true }; }
  const deliveryUnknown = !response || response.delivery_unknown === true || response.deliveryUnknown === true
    || typeof response.ok !== "boolean" || (response.ok === true && !(response.result && typeof response.result === "object"
      && !Array.isArray(response.result) && Number.isInteger(response.result.message_id) && response.result.message_id > 0));
  if (deliveryUnknown) {
    logReconciliation(deps, "[travel-reminder] delivery unknown; reconciliation required");
    return { status: "delivery_unknown" };
  }
  const ok = Boolean(response && response.ok === true);
  const messageId = response && response.result && response.result.message_id;
  if (!ok || !Number.isInteger(messageId) || messageId <= 0) {
    let released = false;
    try { released = await (deps.unclaimTravel || unclaimTravel)(user.uid, key, "telegram-t5", supaUrl, supaKey); } catch { /* retry next tick */ }
    if (released !== true) logReconciliation(deps, "[travel-reminder] reconciliation required");
    if (typeof deps.releaseManagedAction === "function") {
      await deps.releaseManagedAction(user.uid, key, supaUrl, supaKey);
    }
    return { status: "send_failed", eventKey: key };
  }
  let receipt;
  try {
    receipt = await (deps.recordTravelTelegramReceipt || recordTravelTelegramReceipt)(
      user.uid, key, "telegram-t5", messageId, supaUrl, supaKey, { fetchImpl: deps.fetchImpl },
    );
  } catch {
    receipt = null;
  }
  if (!receipt || receipt.ok !== true || receipt.matched !== 1) {
    logReconciliation(deps, "[travel-reminder] delivery receipt reconciliation required");
    return { status: "delivery_unknown" };
  }
  if (selected.allowanceBlocked !== true && typeof deps.completeManagedAction === "function") {
    const completed = await deps.completeManagedAction(user.uid, key, supaUrl, supaKey);
    if (!completed || completed.allowed !== true) {
      logReconciliation(deps, "[travel-reminder] allowance receipt reconciliation required");
    }
  }
  const provider = route && route.provider ? String(route.provider) : "none";
  (deps.log || console.log)(`[travel-reminder] uid=${String(user.uid).slice(0, 12)} event_key_hash=${crypto.createHash("sha256").update(key).digest("hex")} provider=${provider} tg_message_id=${messageId}`);
  return { status: "sent", eventKey: key, provider, telegramMessageId: messageId };
}

module.exports = {
  T5_MS, CATCH_UP_MS, isReminderDue, nextReminderEvent, resolveReminderOrigin,
  resolveReminderDestination, computeDepartureMs, computeReminderDueAt, formatTravelReminder, travelReminderOnce, escapeHtml,
  ALLOWANCE_COPY,
};
