// lib/telegram.js — Life Manager Telegram bot helpers (raw Bot API, no SDK dependency).
//
// The caller supplies a tenant-scoped bot token. Webhook updates land on life-call POST /telegram.
"use strict";

const { createHash } = require("node:crypto");

const TG = (token) => `https://api.telegram.org/bot${token}`;
const TELEGRAM_SEND_TIMEOUT_MS = 20_000;

function hashChatId(chatId) {
  const value = String(chatId == null ? "" : chatId).trim();
  if (!value) throw new Error("Telegram chat id is required");
  return createHash("sha256").update(value, "utf8").digest("hex");
}

async function tgCall(token, method, body, requestOptions = {}) {
  let response;
  try {
    response = await fetch(`${TG(token)}/${method}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      ...(Number.isSafeInteger(requestOptions.timeoutMs) && requestOptions.timeoutMs > 0
        ? { signal: AbortSignal.timeout(requestOptions.timeoutMs) } : {}),
    });
  } catch { return { ok: false, delivery_unknown: true }; }
  try {
    const result = await response.json();
    return result && typeof result === "object" && !Array.isArray(result) && typeof result.ok === "boolean"
      ? result : { ok: false, delivery_unknown: true };
  } catch { return { ok: false, delivery_unknown: true }; }
}

const sendMessage = (token, chatId, text, extra) =>
  tgCall(token, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true, ...(extra || {}) }, { timeoutMs: TELEGRAM_SEND_TIMEOUT_MS });

const editMessageText = (token, chatId, messageId, text, extra) =>
  tgCall(token, "editMessageText", {
    chat_id: chatId, message_id: messageId, text, parse_mode: "HTML", disable_web_page_preview: true,
    ...(extra || {}),
  });

async function sendPhoto(token, chatId, bytes, caption) {
  try {
    const form = new FormData();
    form.append("chat_id", String(chatId));
    if (caption) form.append("caption", String(caption));
    form.append("photo", new Blob([bytes], { type: "image/png" }), "cloud-browser-receipt.png");
    const response = await fetch(`${TG(token)}/sendPhoto`, {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(TELEGRAM_SEND_TIMEOUT_MS),
    });
    const result = await response.json();
    return result && typeof result === "object" && !Array.isArray(result) && typeof result.ok === "boolean"
      ? result : { ok: false, delivery_unknown: true };
  } catch {
    return { ok: false, delivery_unknown: true };
  }
}

const getMe = (token) => tgCall(token, "getMe");

// Register the webhook with a secret token Telegram echoes back in a header we verify on each update.
const setWebhook = (token, url, secret) =>
  tgCall(token, "setWebhook", { url, secret_token: secret, allowed_updates: ["message", "edited_message", "callback_query"] });

const answerCallbackQuery = (token, id, text) =>
  tgCall(token, "answerCallbackQuery", { callback_query_id: id, ...(text ? { text } : {}) });

function isPanelCommand(text) {
  return /^\/panel(?:@[A-Za-z0-9_]+)?(?:\s|$)/i.test(String(text || "").trim());
}

// Pull the meaningful bits out of a Telegram update. Message fields remain backward-compatible.
function parseUpdate(update) {
  const q = update && update.callback_query;
  if (q && q.message && q.message.chat) {
    return {
      kind: "callback",
      chatId: String(q.message.chat.id),
      userId: q.from ? String(q.from.id) : "",
      data: String(q.data || ""),
      callbackQueryId: String(q.id || ""),
      ...(q.message.message_id == null ? {} : { messageId: String(q.message.message_id) }),
      // CB-1: handlers edit the tapped message into its answered state, which needs the original
      // text. Absent stays absent — an empty string would make markAnswered rewrite the message to "".
      ...(q.message.text == null ? {} : { messageText: String(q.message.text) }),
    };
  }
  const edited = update && update.edited_message;
  const m = edited || (update && update.message);
  if (!m || !m.chat) return null;
  const loc = m.location;
  if (loc && Number.isFinite(loc.latitude) && Number.isFinite(loc.longitude) &&
      Number.isInteger(loc.live_period) && loc.live_period > 0) {
    return {
      kind: "location",
      chatId: String(m.chat.id),
      userId: m.from ? String(m.from.id) : "",
      messageId: String(m.message_id || ""),
      latitude: loc.latitude,
      longitude: loc.longitude,
      observedAtMs: Number(m.edit_date || m.date || 0) * 1000,
      expiresAtMs: (Number(m.date || 0) + loc.live_period) * 1000,
    };
  }
  return {
    kind: "message",
    chatId: String(m.chat.id),
    userId: m.from ? String(m.from.id) : "",
    ...(m.message_id == null ? {} : { messageId: String(m.message_id) }),
    text: (m.text || "").trim(),
    // Exact command boundary: payloads may follow whitespace (or an optional @bot suffix), but
    // punctuation/prefix lookalikes such as "/start-foo" and "/start?" must never open onboarding.
    isStart: /^\/start(?:@[A-Za-z0-9_]+)?(?:\s|$)/i.test((m.text || "").trim()),
    firstName: m.from ? String(m.from.first_name || "") : "",
    lastName: m.from ? String(m.from.last_name || "") : "",
    languageCode: m.from ? String(m.from.language_code || "") : "",
  };
}

function isPanelDeepLink(text) {
  return /^\/start(?:@[A-Za-z0-9_]+)?\s+panel$/i.test(String(text || "").trim());
}

async function routeCallbackData(data, handlers = {}, log = console.log) {
  const prefix = String(data || "").split(":", 1)[0];
  if (prefix === "ask" && typeof handlers.ask === "function") return handlers.ask(data);
  if (prefix === "gmail" && typeof handlers.gmail === "function") return handlers.gmail(data);
  if (prefix === "calendar" && typeof handlers.calendar === "function") return handlers.calendar(data);
  if (prefix === "discovery" && typeof handlers.discovery === "function") return handlers.discovery(data);
  if (prefix === "payout" && typeof handlers.payout === "function") return handlers.payout(data);
  if (prefix === "diet" && typeof handlers.diet === "function") return handlers.diet(data);
  if (prefix === "precepts" && typeof handlers.precepts === "function") return handlers.precepts(data);
  if (prefix === "late" && typeof handlers.late === "function") return handlers.late(data);
  if (prefix === "economy" && typeof handlers.economy === "function") return handlers.economy(data);
  log(`[telegram] ignoring unknown callback prefix: ${String(data || "").slice(0, 40)}`);
  return { ignored: true };
}

// The onboarding deep link: Telegram can't host Google OAuth or Stripe, so /start hands the user to
// the web /lm flow, carrying the chat id so the web side can store it on the lm_users row.
function onboardLink(chatId, base) {
  const root = (base || "https://aniccaai.com").replace(/\/$/, "");
  return `${root}/lm?tg=${encodeURIComponent(chatId)}`;
}

// Keep /start in Telegram. The only external hop is Google's consent URL.
function startReply(options = {}) {
  let calendarUrl;
  try {
    const value = String(options.calendarUrl || "");
    if (value.trim() !== value || !/^https:\/\//i.test(value)) throw new Error("invalid calendar URL");
    const parsed = new URL(value);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || !parsed.origin || parsed.origin === "null") {
      throw new Error("invalid calendar URL");
    }
    const host = parsed.hostname.toLowerCase();
    if (host !== "accounts.google.com" && host !== "connect.composio.dev" && !host.endsWith(".connect.composio.dev")) {
      throw new Error("invalid calendar URL");
    }
    calendarUrl = parsed.toString();
  } catch {
    throw new Error("calendar URL is unavailable");
  }
  const ja = /^ja(?:-|$)/i.test(String(options.languageCode || ""));
  return {
    text: ja
      ? "👋 <b>ライフマネージャー</b>\n\n次の予定を確認して、移動時間を予定に入れ、出発前に乗換案内を送ります。\n\n最初にGoogle Calendarをつなぎます。期限が切れたら、/start で新しいリンクを出せます。"
      : "👋 <b>Life Manager</b>\n\nI check your next event, reserve travel time, and send directions before you leave.\n\nFirst, connect Google Calendar. If the link expires, send /start for a new one.",
    extra: {
      reply_markup: {
        inline_keyboard: [[{ text: ja ? "Google Calendarをつなぐ" : "Connect Google Calendar", url: calendarUrl }]],
      },
    },
  };
}

module.exports = {
  tgCall,
  sendMessage,
  editMessageText,
  sendPhoto,
  getMe,
  setWebhook,
  answerCallbackQuery,
  hashChatId,
  isPanelCommand,
  isPanelDeepLink,
  parseUpdate,
  routeCallbackData,
  onboardLink,
  startReply,
};
