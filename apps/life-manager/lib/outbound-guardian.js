"use strict";

const { sendMessage, sendPhoto } = require("./telegram.js");

const SAFE_WAKE_ID = /^[A-Za-z0-9][A-Za-z0-9:._-]{2,159}$/;
const REPORT_TARGET = /^-?[0-9]{5,20}$/;
const REPORT_FAILURE = "Telegram report delivery failed";
const PHOTO_FAILURE = "Telegram photo delivery failed";

function parseTelegramMessageId(response) {
  let value = response;
  if (typeof response === "string") {
    try { value = JSON.parse(response); } catch { value = null; }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Telegram delivery needs a positive message ID");
  }
  const apiResponse = value.ok === true && value.result && typeof value.result === "object"
    && !Array.isArray(value.result);
  if (Object.hasOwn(value, "ok") && !apiResponse) {
    throw new Error("Telegram delivery needs a positive message ID");
  }
  const raw = apiResponse ? value.result.message_id : value.messageId;
  if (apiResponse && !Number.isSafeInteger(raw)) {
    throw new Error("Telegram delivery needs a positive message ID");
  }
  const numeric = Number(raw);
  if (!Number.isSafeInteger(numeric) || numeric <= 0) {
    throw new Error("Telegram delivery needs a positive message ID");
  }
  return String(numeric);
}

function telegramToken(options) {
  const token = options.telegramToken === undefined
    ? process.env.LM_TELEGRAM_BOT_TOKEN : options.telegramToken;
  if (typeof token !== "string" || !token.trim()) throw new Error("Telegram token is required");
  return token.trim();
}

async function notifyTelegram(message, options = {}) {
  const target = String(options.telegramTarget || "").trim();
  if (!target) throw new Error("Telegram target is required");
  try {
    const response = await (options.sendMessage || sendMessage)(telegramToken(options), target, message);
    return { messageId: parseTelegramMessageId(response) };
  } catch {
    throw new Error("Telegram delivery failed");
  }
}

async function notifyTelegramReport(message, options = {}) {
  try {
    const target = options.telegramTarget;
    const idempotencyKey = options.idempotencyKey;
    if (
      typeof message !== "string" || !message || message.length > 4_096
      || typeof target !== "string" || !REPORT_TARGET.test(target)
      || typeof idempotencyKey !== "string" || !SAFE_WAKE_ID.test(idempotencyKey)
    ) throw new Error(REPORT_FAILURE);
    const response = await (options.sendMessage || sendMessage)(telegramToken(options), target, message);
    return { messageId: parseTelegramMessageId(response) };
  } catch {
    throw new Error(REPORT_FAILURE);
  }
}

async function notifyTelegramPhoto(bytes, options = {}) {
  try {
    const target = options.telegramTarget;
    const idempotencyKey = options.idempotencyKey;
    if (
      !Buffer.isBuffer(bytes)
      || typeof target !== "string" || !REPORT_TARGET.test(target)
      || typeof idempotencyKey !== "string" || !SAFE_WAKE_ID.test(idempotencyKey)
    ) throw new Error("Telegram photo delivery invalid");
    const response = await (options.sendPhoto || sendPhoto)(
      telegramToken(options), target, bytes, String(options.caption || ""),
    );
    return { messageId: parseTelegramMessageId(response) };
  } catch (error) {
    if (error && error.message === "Telegram photo delivery invalid") throw error;
    throw new Error(PHOTO_FAILURE);
  }
}

module.exports = {
  parseTelegramMessageId,
  notifyTelegram,
  notifyTelegramReport,
  notifyTelegramPhoto,
};
