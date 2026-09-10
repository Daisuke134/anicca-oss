"use strict";

const { parseTelegramMessageId } = require("./outbound-guardian.js");
const { hashChatId, sendPhoto } = require("./telegram.js");

const TENANT = /^[a-z0-9][a-z0-9._-]{0,127}$/i;
const ARTIFACT_REF = /^object:\/\/sha256\/[0-9a-f]{64}$/;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const PLACEHOLDER = /\{\{|\}\}|<placeholder>|TODO|TBD/i;
const CONFIRMATION_RECEIPT_REF = /^gmail-message:\/\/[a-z0-9._-]+\/[0-9a-f]{64}$/i;
const PRIORITY_CLASSES = Object.freeze(["yc_hackathon", "open_talk", "ai", "crypto", "startup", "other"]);
const TALK_STATES = Object.freeze(["not_open", "application_ready", "submitted", "provider_verified", "accepted", "rejected", "human_action_required"]);

function text(value, label, max = 500) {
  const result = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  if (!result || result.length > max || PLACEHOLDER.test(result)) {
    throw new Error(`Connector Telegram ${label} placeholder or value invalid`);
  }
  return result;
}

function instant(value, label) {
  const raw = String(value == null ? "" : value).trim();
  const parsed = Date.parse(raw);
  if (!Number.isFinite(parsed) || !/[zZ]|[+-]\d\d:\d\d$/.test(raw)) {
    throw new Error(`Connector Telegram ${label} time invalid`);
  }
  return parsed;
}

function parts(milliseconds) {
  const result = {};
  for (const part of new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(milliseconds))) {
    if (part.type !== "literal") result[part.type] = part.value;
  }
  return result;
}

function eventUrl(value) {
  let url;
  try { url = new URL(String(value || "")); } catch { throw new Error("Connector Telegram event URL invalid"); }
  if (
    url.protocol !== "https:"
    || !/^(?:www\.)?(?:luma\.com|lu\.ma)$/i.test(url.hostname)
    || !/^\/[A-Za-z0-9_-]+\/?$/.test(url.pathname)
    || url.username
    || url.password
  ) throw new Error("Connector Telegram event URL invalid");
  url.hash = "";
  url.search = "";
  return `https://${url.hostname.toLowerCase()}${url.pathname.replace(/\/$/, "")}`;
}

function providerLabel(canonicalEventUrl) {
  const hostname = new URL(canonicalEventUrl).hostname.toLowerCase();
  if (hostname === "luma.com" || hostname === "lu.ma") return "Luma";
  throw new Error("Connector Telegram provider unresolved");
}

function confirmationReceiptRef(value) {
  const result = String(value == null ? "" : value).trim();
  if (!CONFIRMATION_RECEIPT_REF.test(result)) {
    throw new Error("Connector Telegram confirmation receipt invalid");
  }
  return result;
}

function calendarUrl(value) {
  let url;
  try { url = new URL(String(value || "")); } catch { throw new Error("Connector Telegram Calendar URL invalid"); }
  if (
    url.protocol !== "https:"
    || !/^(?:www\.)?google\.com$/i.test(url.hostname)
    || url.pathname !== "/calendar/event"
    || !url.searchParams.get("eid")
    || url.username
    || url.password
  ) throw new Error("Connector Telegram Calendar URL invalid");
  return url.toString();
}

function buildConnectorTicketCaption(input = {}) {
  const title = text(input.eventTitle, "event title", 300);
  const venue = text(input.venue, "venue", 500);
  const identity = text(input.registrationIdentity, "registration identity", 100);
  const reason = text(input.selectionReason, "selection reason", 500);
  const priorityClass = text(input.priorityClass, "priority class", 40);
  const talkState = text(input.talkState, "talk state", 40);
  if (!PRIORITY_CLASSES.includes(priorityClass) || !TALK_STATES.includes(talkState)) {
    throw new Error("Connector Telegram selection metadata invalid");
  }
  const deadlineMs = input.applicationDeadlineAt == null ? null : instant(input.applicationDeadlineAt, "application deadline");
  const startMs = instant(input.startsAt, "start");
  const endMs = instant(input.endsAt, "end");
  if (endMs <= startMs) throw new Error("Connector Telegram event time invalid");
  const start = parts(startMs);
  const end = parts(endMs);
  const date = `${start.year}年${Number(start.month)}月${Number(start.day)}日（${start.weekday}）`;
  const when = `${date}${start.hour}:${start.minute}〜${end.hour}:${end.minute}`;
  const event = eventUrl(input.eventUrl);
  const calendar = calendarUrl(input.calendarUrl);
  const provider = providerLabel(event);
  confirmationReceiptRef(input.confirmationReceiptRef);
  const caption = [
    "🎟️ イベント参加の申込みが完了しました。",
    "",
    `イベント: ${title}`,
    `日時: ${when}`,
    `場所: ${venue}`,
    `申込者: ${identity}`,
    "",
    "このイベントを選んだ理由:",
    reason,
    `優先度: ${priorityClass}`,
    `LT: ${talkState}`,
    ...(deadlineMs == null ? [] : [(() => {
      const deadline = parts(deadlineMs);
      return `LT申請締切: ${deadline.year}年${Number(deadline.month)}月${Number(deadline.day)}日 ${deadline.hour}:${deadline.minute}`;
    })()]),
    "",
    `✅ ${provider}の確認メールを受信済み`,
    "✅ Google Calendarへ登録済み",
    "📱 当日の公式QRを添付しました",
    "",
    "イベントページ:",
    event,
    "",
    "カレンダー:",
    calendar,
  ].join("\n");
  if (caption.length > 1024) throw new Error("Connector Telegram caption too long");
  return caption;
}

function telegramToken(options) {
  const token = options.telegramToken === undefined
    ? process.env.LM_TELEGRAM_BOT_TOKEN : options.telegramToken;
  if (typeof token !== "string" || !token.trim()) throw new Error("Telegram token is required");
  return token.trim();
}

async function sendTelegramMedia(targetValue, bytes, caption, options = {}) {
  const target = String(targetValue == null ? "" : targetValue).trim();
  if (!target || target.length > 200) throw new Error("Telegram target invalid");
  if (!Buffer.isBuffer(bytes) || bytes.length < 5_000) throw new Error("Telegram PNG invalid");
  const token = telegramToken(options);
  try {
    const response = await (options.sendPhoto || sendPhoto)(token, target, bytes, caption);
    return { messageId: parseTelegramMessageId(response) };
  } catch {
    const error = new Error("Telegram media delivery failed");
    error.unknownEffect = true;
    throw error;
  }
}

async function deliverConnectorTicket(input = {}, dependencies = {}) {
  const tenant = String(input.tenantId == null ? "" : input.tenantId).trim();
  if (!TENANT.test(tenant)) throw new Error("Connector Telegram tenant invalid");
  const target = String(input.telegramTarget == null ? "" : input.telegramTarget).trim();
  if (!target || target.length > 200) throw new Error("Telegram target invalid");
  const chatIdSha256 = hashChatId(target);
  const verifiedEventUrl = eventUrl(input.eventUrl);
  const artifactRef = String(input.artifactRef == null ? "" : input.artifactRef).trim();
  if (!ARTIFACT_REF.test(artifactRef)) throw new Error("Connector Telegram artifact ref invalid");
  if (typeof dependencies.readArtifact !== "function") {
    throw new Error("Connector Telegram artifact reader unavailable");
  }
  const caption = buildConnectorTicketCaption(input);
  const observedAtMs = Date.parse((dependencies.observedAt || (() => new Date().toISOString()))());
  if (!Number.isFinite(observedAtMs)) throw new Error("Connector Telegram observed time invalid");
  const observedAt = new Date(observedAtMs).toISOString();
  const bytes = await dependencies.readArtifact(tenant, artifactRef);
  if (
    !Buffer.isBuffer(bytes)
    || bytes.length < 5_000
    || !bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)
  ) throw new Error("Connector Telegram PNG invalid");
  const send = dependencies.sendMedia || sendTelegramMedia;
  let response;
  try {
    response = await send(target, bytes, caption);
  } catch (error) {
    if (error && error.unknownEffect === true) throw error;
    const uncertain = new Error("Telegram delivery uncertain");
    uncertain.unknownEffect = true;
    throw uncertain;
  }
  let messageId;
  try { messageId = parseTelegramMessageId(response); } catch {
    const error = new Error("Telegram delivery needs a positive message ID");
    error.unknownEffect = true;
    throw error;
  }
  return Object.freeze({
    kind: "telegram_delivery",
    provider_id: messageId,
    observed_at: observedAt,
    tenant_id: tenant,
    chat_id_sha256: chatIdSha256,
    artifact_ref: artifactRef,
    event_url: verifiedEventUrl,
  });
}

module.exports = {
  buildConnectorTicketCaption,
  deliverConnectorTicket,
  sendTelegramMedia,
};
