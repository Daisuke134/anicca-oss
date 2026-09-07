// lib/telegram-onboard.js — LM-6 minimal-question Telegram onboarding.
"use strict";

const { sendMessage, onboardLink } = require("./telegram.js");
const { signedGmailConnectUrl } = require("./gmail-onboard.js");
const { backfillCalendarContext } = require("./context-graph.js");
const { mailAvailable } = require("./mail-availability.js");
const { compActive } = require("./comp-window.js");

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
  if (!row || row.calendar_provider !== "composio_gcal") return "calendar";
  const storedStage = String(row.tg_onboard_stage || "").toLowerCase();
  if (storedStage === "done") return "done";
  if ((storedStage === "calendar" || storedStage === "home")
    && (typeof row.home_address !== "string" || !row.home_address.trim())) return "home";
  if (storedStage === "phone" && !row.phone) return "phone";
  if (storedStage === "call" && row.phone) return "call";
  if (coreReady(row)) return "done";
  // Optional phone/call questions run only while the server-owned stage says so. Legacy rows never
  // reopen an onboarding paywall or optional integration.
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

const NATIVE_STAGES = new Set(["home", "phone", "call"]);
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

function stageMessage(stage, chatId, base, gmailConnectUrl, profileName, languageCode) {
  const ja = /^ja(?:-|$)/i.test(String(languageCode || ""));
  let link = onboardLink(chatId, base);
  if (profileName) link += `&name=${encodeURIComponent(profileName)}`;
  const urlButton = (text, url = link) => ({ reply_markup: { inline_keyboard: [[{ text, url }]] } });
  switch (stage) {
    case "calendar":
      return { text: "👋 <b>Welcome to Life Manager!</b>\n\nConnect your Google Calendar (10 sec). Tap below, sign in, then come back here.", extra: urlButton("📅 Connect Calendar") };
    case "home":
      return { text: "自宅の住所を教えてください。ここを普段の出発地点として、次の予定に間に合う出発時刻を計算します。", extra: undefined };
    case "phone":
      return { text: ja
        ? "電話通知も使えます（任意）。出発時刻の10分前と5分前に電話します。\n\n使う場合は電話番号（例：<code>090-1234-5678</code>）を送ってください。使わない場合は「スキップ」と送ってください。"
        : "Phone alerts are optional. I can call 10 and 5 minutes before you need to leave.\n\nSend your number (for example <code>+81 90-1234-5678</code>) or reply “skip”.", extra: undefined };
    case "call":
      return { text: ja
        ? "この番号への電話通知をオンにしますか？「はい」または「スキップ」と送ってください。"
        : "Turn on phone alerts for this number? Reply “yes” or “skip”.", extra: undefined };
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
      return { text: ja
        ? "準備完了です。予定に合わせて移動時間を確保し、出発前にTelegramで経路を知らせます。"
        : "You're ready. I'll reserve travel time for your schedule and send the route in Telegram before you need to leave.", extra: undefined };
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
  const message = stageMessage(stage, chatId, base, opts.gmailConnectUrl, effective.name, opts.languageCode);
  await (opts.sendMessage || sendMessage)(token, chatId, message.text, message.extra);
  return stage;
}

// payout_destination rides along so the webhook can see a pending wallet-address intake (13d-a)
// without a second round trip on every typed message.
const SEL = "uid,name,telegram_chat_id,tg_onboard_stage,calendar_provider,gmail_account_id,gmail_skipped,email,phone,home_address,payout_destination";
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

async function completeTelegramHome(uid, chatId, homeAddress, supaUrl, supaKey) {
  const response = await fetch(`${String(supaUrl || "").replace(/\/$/, "")}/rest/v1/rpc/complete_lm_telegram_home`, {
    method: "POST",
    headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ p_uid: uid, p_chat_id: String(chatId), p_home_address: homeAddress }),
  });
  if (!response.ok) throw new Error("onboarding_transition_failed");
  const value = await response.json().catch(() => false);
  const completed = Array.isArray(value) ? value[0] === true : value === true;
  if (!completed) throw new Error("onboarding_transition_failed");
  return true;
}

async function backfillIfCalendarCompleted(row, opts = {}) {
  if (!row || row.tg_onboard_stage !== "calendar" || computeStage(row) === "calendar") return false;
  const backfill = opts.backfillCalendarContext || backfillCalendarContext;
  try {
    await backfill(row.uid, {
      composioKey: opts.composioKey, geminiKey: opts.geminiKey,
      supaUrl: opts.supaUrl, supaKey: opts.supaKey, gmailAccountId: row.gmail_account_id,
    });
    return true;
  } catch {
    return false;
  }
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
    await (opts.completeTelegramHome || completeTelegramHome)(row.uid, chatId, homeAddress, opts.supaUrl, opts.supaKey);
    const message = stageMessage("phone", chatId, opts.base, "", "", opts.languageCode);
    await (opts.sendMessage || sendMessage)(opts.token, chatId, message.text, message.extra);
    return "home";
  }
  if (stage === "phone") {
    const skip = /^(?:skip|スキップ|使わない|不要)$/i.test(String(text || "").trim());
    const transition = opts.transitionOnboarding || transitionOnboarding;
    if (skip) {
      await transition(row.uid, chatId, "phone.skip", {}, opts.supaUrl, opts.supaKey);
      const message = stageMessage("done", chatId, opts.base, "", "", opts.languageCode);
      await (opts.sendMessage || sendMessage)(opts.token, chatId, message.text, message.extra);
      return "phone-skip";
    }
    const phone = normalizePhone(text);
    if (!phone) {
      await (opts.sendMessage || sendMessage)(opts.token, chatId, /^ja(?:-|$)/i.test(String(opts.languageCode || ""))
        ? "電話番号を確認できませんでした。例：<code>090-1234-5678</code>。使わない場合は「スキップ」と送ってください。"
        : "I couldn't read that phone number. Try <code>+81 90-1234-5678</code>, or reply “skip”.");
      return "bad-phone";
    }
    await transition(row.uid, chatId, "phone.save", { phone }, opts.supaUrl, opts.supaKey);
    const message = stageMessage("call", chatId, opts.base, "", "", opts.languageCode);
    await (opts.sendMessage || sendMessage)(opts.token, chatId, message.text, message.extra);
    return "phone";
  }
  if (stage === "call") {
    const answer = String(text || "").trim();
    const enable = /^(?:yes|on|はい|使う|有効)$/i.test(answer);
    const skip = /^(?:skip|no|off|スキップ|いいえ|使わない|不要)$/i.test(answer);
    if (!enable && !skip) {
      const message = stageMessage("call", chatId, opts.base, "", "", opts.languageCode);
      await (opts.sendMessage || sendMessage)(opts.token, chatId, message.text, message.extra);
      return "bad-call-choice";
    }
    await (opts.transitionOnboarding || transitionOnboarding)(row.uid, chatId, enable ? "call.enable" : "call.skip", {}, opts.supaUrl, opts.supaKey);
    const message = stageMessage("done", chatId, opts.base, "", "", opts.languageCode);
    await (opts.sendMessage || sendMessage)(opts.token, chatId, message.text, message.extra);
    return enable ? "call-enable" : "call-skip";
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
  saveField, completeTelegramHome, backfillIfCalendarCompleted, onboardNudgeAll, NUDGE_COOLDOWN_MS,
};
