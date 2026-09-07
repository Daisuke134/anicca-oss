// lib/telegram-onboard.js — LM-6 minimal-question Telegram onboarding.
"use strict";

const { sendMessage, onboardLink } = require("./telegram.js");
const { signedGmailConnectUrl } = require("./gmail-onboard.js");
const { backfillCalendarContext } = require("./context-graph.js");
const { mailAvailable } = require("./mail-availability.js");
const { compActive } = require("./comp-window.js");
const { paymentLink } = require("./payment-link.js");
const { claimTravel, unclaimTravel } = require("./travel.js");

// PURE: calendar/pay are taps, phone is the only typed question, and Gmail is skippable after pay.
// COMP WINDOW: while LM_COMP_UNTIL is in the future an unpaid row passes the paywall and continues to
// the next stage. This is a READ-TIME override only — the paid column is never written here, so the
// Stripe webhook (lib/billing.js) remains its single writer and the comp expires by itself.
// `opts` ({env, now}) exists so tests can pin the clock; production calls computeStage(row).
function coreReady(row) {
  return Boolean(row && row.calendar_provider === "composio_gcal"
    && typeof row.home_address === "string" && row.home_address.trim()
    && row.notifications_enabled === true);
}

function computeStage(row, opts = {}) {
  if (coreReady(row)) return "done";
  if (!row || row.calendar_provider !== "composio_gcal") return "calendar";
  const storedStage = String(row.tg_onboard_stage || "").toLowerCase();
  if ((storedStage === "calendar" || storedStage === "home")
    && (typeof row.home_address !== "string" || !row.home_address.trim())) return "home";
  // The panel state machine owns the canonical paid/core-ready terminal state. The legacy loop must
  // not reopen phone or Gmail for a paid user who intentionally skipped a phone, nor can it rewrite
  // a server-owned `done` marker after a browser resume.
  if (row.paid === true && coreReady(row)) return "done";
  if (!row.phone) return "phone";
  if (row.paid !== true && !compActive(opts.env || process.env, opts.now)) return "pay";
  if (!row.gmail_account_id && row.gmail_skipped !== true) return "gmail";
  return "done";
}

function telegramProfileName(from) {
  if (!from) return "";
  return [from.first_name, from.last_name].map((part) => String(part || "").trim()).filter(Boolean).join(" ").slice(0, 120);
}

function applyTelegramProfileName(row, from) {
  const current = row ? { ...row } : {};
  if (!current.name) {
    const name = telegramProfileName(from);
    if (name) current.name = name;
  }
  return current;
}

const NATIVE_STAGES = new Set(["home", "phone"]);
const isNativeStage = (stage) => NATIVE_STAGES.has(stage);

function normalizePhone(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  const international = raw.startsWith("+");
  const digits = (international ? raw.slice(1) : raw).replace(/[()\s.-]/g, "");
  if (!/^\d+$/.test(digits)) return null;
  const value = international ? `+${digits}` : digits.startsWith("0") ? `+81${digits.slice(1)}` : "";
  return /^\+[1-9]\d{7,14}$/.test(value) ? value : null;
}

function stageMessage(stage, chatId, base, gmailConnectUrl, profileName) {
  let link = onboardLink(chatId, base);
  if (profileName) link += `&name=${encodeURIComponent(profileName)}`;
  const urlButton = (text, url = link) => ({ reply_markup: { inline_keyboard: [[{ text, url }]] } });
  switch (stage) {
    case "calendar":
      return { text: "👋 <b>Welcome to Life Manager!</b>\n\nConnect your Google Calendar (10 sec). Tap below, sign in, then come back here.", extra: urlButton("📅 Connect Calendar") };
    case "home":
      return { text: "自宅の住所を教えてください。ここを普段の出発地点として、次の予定に間に合う出発時刻を計算します。", extra: undefined };
    case "phone":
      return { text: "✅ <b>Calendar connected!</b>\n\nWhat's your phone number? Japanese numbers can be <code>090-1234-5678</code> or international <code>+81 90-1234-5678</code> — I'll call you before events.", extra: undefined };
    case "pay":
      return { text: "✅ <b>Phone saved!</b>\n\nSubscribe ($20/mo) and I'll take it from here.", extra: urlButton("⭐ Subscribe") };
    case "gmail": {
      const connectUrl = gmailConnectUrl || `${link}&gmail=connect`;
      return {
        text: "Optional: connect Gmail so I can use relevant email context. You can skip this and every Gmail-independent feature will still work.",
        extra: { reply_markup: { inline_keyboard: [[
          { text: "✉️ Connect Gmail", url: connectUrl }, { text: "Skip", callback_data: "gmail:skip" },
        ]] } },
      };
    }
    case "done":
      return { text: "🎉 <b>You're all set!</b>\n\nI'll now manage your schedule — I call you before you must leave, fill in travel time, and only ask when I genuinely can't find a location. Talk soon.", extra: undefined };
    default:
      return { text: "Tap below to continue setting up Life Manager.", extra: urlButton("Open Life Manager") };
  }
}

async function sendStage(token, chatId, row, base, opts = {}) {
  const effective = applyTelegramProfileName(row, opts.profile || null);
  let stage = computeStage(effective);
  if (stage === "gmail") {
    const available = await (opts.mailAvailable || mailAvailable)(effective, opts.mailOptions || {});
    if (!available) {
      await (opts.saveField || saveField)(effective.uid, { gmail_skipped: true }, opts.supaUrl, opts.supaKey);
      await (opts.sendMessage || sendMessage)(token, chatId,
        "✉️ Gmail integration is currently being prepared, so I skipped that optional step. Your Calendar and all Gmail-independent features are ready.");
      return "done";
    }
  }
  const message = stageMessage(stage, chatId, base, opts.gmailConnectUrl, effective.name);
  await (opts.sendMessage || sendMessage)(token, chatId, message.text, message.extra);
  return stage;
}

// payout_destination rides along so the webhook can see a pending wallet-address intake (13d-a)
// without a second round trip on every typed message.
const SEL = "uid,name,telegram_chat_id,tg_onboard_stage,calendar_provider,gmail_account_id,gmail_skipped,email,phone,paid,trial_expires_at,home_address,payout_destination";
async function saveField(uid, patch, supaUrl, supaKey) {
  await fetch(`${supaUrl}/rest/v1/lm_users?uid=eq.${encodeURIComponent(uid)}`, {
    method: "PATCH",
    headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}`, "Content-Type": "application/json", Prefer: "return=minimal" },
    body: JSON.stringify({ ...patch, updated_at: new Date().toISOString() }),
  }).catch(() => {});
}
// NO TIMEZONE HERE, ON PURPOSE (H4). The only per-user zone in this schema is
// lm_panel_preferences.call_time_zone; SEL is a lm_users projection, so every webhook handler that
// asks "what day is it for this person" gets whatever env fallback its organ carries — which is not
// necessarily the offset the SENDING side used. PostgREST could embed it (lm_panel_preferences.uid
// REFERENCES lm_users.uid, so `select=...,lm_panel_preferences(call_time_zone)` would resolve), and
// that is deliberately NOT done: it adds a join and a nested array to a query on the hot path of
// every callback and every typed message, for eight call sites of which one wanted it. The precepts
// organ instead carries its resolved offset inside the callback data, which is strictly better than
// a join — it makes the ASK MOMENT the source of truth rather than re-deriving an answer that can
// drift. Any future handler with the same need should carry it the same way.
async function rowByChatId(chatId, supaUrl, supaKey) {
  const response = await fetch(`${supaUrl}/rest/v1/lm_users?telegram_chat_id=eq.${encodeURIComponent(chatId)}&select=${SEL}&limit=1`,
    { headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}` } });
  const data = await response.json().catch(() => []);
  return Array.isArray(data) && data[0] ? data[0] : null;
}
// notifications_enabled lives in lm_panel_preferences, not lm_users, so it is joined here the same way
// scheduler.supaUsers does it: ONE batched `uid=in.(...)` query, because this feeds a 2-minute loop and
// a per-user round trip would multiply the cost by the size of the fleet. A missing preferences row or
// failed preferences read is not proof of explicit ON, so this path fails CLOSED (no unsolicited nudge).
async function linkedRows(supaUrl, supaKey, opts = {}) {
  const f = opts.fetchImpl || fetch;
  const headers = { apikey: supaKey, Authorization: `Bearer ${supaKey}` };
  const response = await f(`${supaUrl}/rest/v1/lm_users?telegram_chat_id=not.is.null&select=${SEL}`, { headers });
  const data = await response.json().catch(() => []);
  const rows = Array.isArray(data) ? data : [];
  if (rows.length === 0) return rows;
  const ids = rows.map(row => row.uid).filter(Boolean).join(",");
  const prefsResponse = ids
    ? await f(`${supaUrl}/rest/v1/lm_panel_preferences?uid=in.(${encodeURIComponent(ids)})&select=uid,notifications_enabled`, { headers }).catch(() => null)
    : null;
  if (!prefsResponse || !prefsResponse.ok) return rows.map(row => ({ ...row, notifications_enabled: false }));
  const prefRows = await prefsResponse.json().catch(() => null);
  if (!Array.isArray(prefRows)) return rows.map(row => ({ ...row, notifications_enabled: false }));
  const byUid = new Map(prefRows.map(row => [row.uid, row]));
  return rows.map(row => ({ notifications_enabled: false, ...row, ...(byUid.get(row.uid) || {}) }));
}
async function setStage(uid, stage, supaUrl, supaKey) {
  await fetch(`${supaUrl}/rest/v1/lm_users?uid=eq.${encodeURIComponent(uid)}`, {
    method: "PATCH",
    headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}`, "Content-Type": "application/json", Prefer: "return=minimal" },
    body: JSON.stringify({ tg_onboard_stage: stage }),
  }).catch(() => {});
}

async function transitionOnboarding(uid, chatId, action, payload, supaUrl, supaKey) {
  const response = await fetch(`${String(supaUrl || "").replace(/\/$/, "")}/rest/v1/rpc/lm_panel_onboarding_transition`, {
    method: "POST",
    headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ p_uid: uid, p_chat_id: String(chatId), p_action: action, p_payload: payload || {} }),
  });
  if (!response.ok) throw new Error("onboarding_transition_failed");
  return response.json().catch(() => ({}));
}

async function backfillIfCalendarCompleted(row, opts = {}) {
  if (!row || row.tg_onboard_stage !== "calendar" || computeStage(row) === "calendar") return false;
  const backfill = opts.backfillCalendarContext || backfillCalendarContext;
  await backfill(row.uid, {
    composioKey: opts.composioKey, geminiKey: opts.geminiKey,
    supaUrl: opts.supaUrl, supaKey: opts.supaKey, gmailAccountId: row.gmail_account_id,
  });
  return true;
}

async function handleOnboardingText(chatId, text, row, opts) {
  if (row && row.tg_onboard_stage === "done") return "done";
  const stage = computeStage(row);
  await backfillIfCalendarCompleted(row, opts);
  if (stage === "home") {
    const homeAddress = String(text || "").trim();
    if (!homeAddress || homeAddress.length > 240) {
      await (opts.sendMessage || sendMessage)(opts.token, chatId, "自宅の住所を240文字以内で教えてください。");
      return "bad-home";
    }
    const transition = opts.transitionOnboarding || transitionOnboarding;
    await transition(row.uid, chatId, "home.save", { home_address: homeAddress }, opts.supaUrl, opts.supaKey);
    await transition(row.uid, chatId, "notifications.enable", {}, opts.supaUrl, opts.supaKey);
    const message = stageMessage("phone", chatId, opts.base);
    await (opts.sendMessage || sendMessage)(opts.token, chatId, message.text, message.extra);
    return "home";
  }
  if (stage === "phone") {
    const phone = normalizePhone(text);
    if (!phone) {
      await sendMessage(opts.token, chatId, "That doesn't look like a phone number. Try <code>090-1234-5678</code> or international <code>+81 90-1234-5678</code>.");
      return "bad-phone";
    }
    await saveField(row.uid, { phone }, opts.supaUrl, opts.supaKey);
    const next = computeStage({ ...row, phone });
    const message = stageMessage(next, chatId, opts.base);
    await sendMessage(opts.token, chatId, message.text, message.extra);
    await setStage(row.uid, next, opts.supaUrl, opts.supaKey);
    return "phone";
  }
  if (stage === "done") return "done";
  await sendStage(opts.token, chatId, row, opts.base, opts);
  if (row) await setStage(row.uid, stage, opts.supaUrl, opts.supaKey);
  return "guidance";
}

async function handleGmailCallback(data, row, opts = {}) {
  if (data !== "gmail:skip") return { ok: false, ignored: true };
  if (!row || !row.uid) return { ok: false, reason: "unlinked_chat" };
  if (row.tg_onboard_stage === "done") return { ok: true, stage: "done" };
  const save = opts.saveField || saveField;
  const persistStage = opts.setStage || setStage;
  const send = opts.sendMessage || sendMessage;
  await save(row.uid, { gmail_skipped: true }, opts.supaUrl, opts.supaKey);
  const stage = computeStage({ ...row, gmail_skipped: true });
  const message = stageMessage(stage, opts.chatId || row.telegram_chat_id, opts.base);
  await send(opts.token, opts.chatId || row.telegram_chat_id, message.text, message.extra);
  await persistStage(row.uid, stage, opts.supaUrl, opts.supaKey);
  return { ok: true, stage };
}

// This loop ticks every 2 minutes over every linked row. The same-stage guard alone is not enough:
// a user finishing steps in the browser changes stage several times in a few minutes and gets a
// Telegram message for each one. One nudge per user per 30 minutes.
const NUDGE_COOLDOWN_MS = 30 * 60 * 1000;
// TRADEOFF (accepted): the cooldown lives in memory, not in the database. A restart forgets it, so a
// deploy can cost a user ONE extra nudge — bounded and harmless. The durable alternative is a new
// lm_users column plus a write on every tick for every linked user, which buys nothing while a single
// process owns this loop (see lib/maybe-start-loops.js: exactly one writer). If the loop is ever
// sharded across processes, move this to a column next to last_discovery_at.
const nudgeSentAt = new Map();

function escapeHtml(value) {
  return String(value).replace(/[&<>\"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;",
  }[character]));
}

function trustedCheckoutLink(opts, uid) {
  try {
    const link = typeof opts.paymentLink === "function"
      ? opts.paymentLink(opts, { uid })
      : typeof opts.link === "function" ? opts.link(opts, { uid }) : paymentLink(opts, { uid });
    const url = new URL(String(link || ""));
    if (url.protocol !== "https:" || url.hostname !== "buy.stripe.com"
      || url.username || url.password || url.pathname.length <= 1) return "";
    url.searchParams.set("client_reference_id", String(uid));
    return escapeHtml(url.toString());
  } catch { return ""; }
}

async function onboardNudgeAll(opts) {
  if (!opts.token || !opts.supaUrl || !opts.supaKey) return 0;
  const list = opts.linkedRows || linkedRows;
  const announce = opts.sendStage || sendStage;
  const persistStage = opts.setStage || setStage;
  const backfill = opts.backfillCalendarContext || backfillCalendarContext;
  const now = typeof opts.now === "number" ? opts.now : Date.now();
  const cooldown = opts.nudgeStore || nudgeSentAt;
  for (const [uid, at] of cooldown) if (now - at >= NUDGE_COOLDOWN_MS) cooldown.delete(uid); // bounded
  const rows = await list(opts.supaUrl, opts.supaKey, opts);
  let sent = 0;
  for (const row of rows) {
    // Honour the panel notifications switch, like the ask and discovery loops already do.
    if (row.notifications_enabled === false) continue;
    const expiresAt = Date.parse(String(row.trial_expires_at || ""));
    const expired = Number.isFinite(expiresAt) && expiresAt <= now && row.paid !== true && coreReady(row);
    if (expired && row.telegram_chat_id) {
      const eventKey = String(row.trial_expires_at);
      const claim = opts.claimTravel || claimTravel;
      const unclaim = opts.unclaimTravel || unclaimTravel;
      let claimed = false;
      try { claimed = await claim(row.uid, eventKey, "trial-upgrade", opts.supaUrl, opts.supaKey); } catch { claimed = false; }
      if (!claimed) continue;
      const link = trustedCheckoutLink(opts, row.uid);
      let result = { ok: false };
      if (link) {
        try {
          result = await (opts.sendMessage || sendMessage)(opts.token, row.telegram_chat_id,
            `無料期間が終了しました。\n\n<a href="${link}">月額プランを確認する</a>`);
        } catch { result = { ok: false, delivery_unknown: true }; }
      }
      const deliveryUnknown = !result || typeof result !== "object" || Array.isArray(result)
        || result.delivery_unknown === true || result.deliveryUnknown === true
        || typeof result.ok !== "boolean" || (result.ok === true && !(result.result && typeof result.result === "object"
          && !Array.isArray(result.result) && Number.isInteger(result.result.message_id) && result.result.message_id > 0));
      if (deliveryUnknown) {
        try { (opts.logError || opts.log || console.error)("[onboard] trial-upgrade reconciliation required"); } catch { /* keep loop alive */ }
        continue;
      }
      const messageId = result && result.ok === true && result.result
        && Number.isInteger(result.result.message_id) && result.result.message_id > 0;
      if (!messageId) {
        let released = false;
        try { released = await unclaim(row.uid, eventKey, "trial-upgrade", opts.supaUrl, opts.supaKey); } catch { /* retry next tick */ }
        if (released !== true) {
          try { (opts.logError || opts.log || console.error)("[onboard] trial-upgrade reconciliation required"); } catch { /* keep loop alive */ }
        }
      } else sent++;
      continue;
    }
    const lastNudge = cooldown.get(row.uid);
    if (typeof lastNudge === "number" && now - lastNudge < NUDGE_COOLDOWN_MS) continue;
    let stage = computeStage(row);
    if (stage === "done" && coreReady(row)) continue;
    if (stage === "gmail") {
      const available = await (opts.mailAvailable || mailAvailable)(row, opts.mailOptions || {});
      if (!available) {
        await (opts.saveField || saveField)(row.uid, { gmail_skipped: true }, opts.supaUrl, opts.supaKey);
        await (opts.sendMessage || sendMessage)(opts.token, row.telegram_chat_id,
          "✉️ Gmail integration is currently being prepared, so I skipped that optional step. Your Calendar and all Gmail-independent features are ready.");
        stage = "done";
        await persistStage(row.uid, stage, opts.supaUrl, opts.supaKey);
        cooldown.set(row.uid, now);
        sent++;
        continue;
      }
    }
    if (stage === row.tg_onboard_stage) continue;
    await backfillIfCalendarCompleted(row, { ...opts, backfillCalendarContext: backfill });
    const gmailConnectUrl = opts.gmailConfigured === false
      ? "" : signedGmailConnectUrl(row.uid, opts.gmailBase, opts.uidSecret);
    await announce(opts.token, row.telegram_chat_id, row, opts.base, { gmailConnectUrl });
    await persistStage(row.uid, stage, opts.supaUrl, opts.supaKey);
    cooldown.set(row.uid, now);
    sent++;
  }
  return sent;
}

module.exports = {
  computeStage, telegramProfileName, applyTelegramProfileName, stageMessage, sendStage, isNativeStage,
  normalizePhone, handleOnboardingText, handleGmailCallback, rowByChatId, linkedRows, setStage, transitionOnboarding,
  saveField, backfillIfCalendarCompleted, onboardNudgeAll, NUDGE_COOLDOWN_MS,
};
