"use strict";

const { createHash } = require("node:crypto");
const { isVerifiedRollingEventCoverage } = require("./rolling-event-coverage.js");
const { isVerifiedLumaDateInventory } = require("./luma-date-inventory.js");
const { isVerifiedEventProviderDateInventory } = require("./event-provider-date-inventory.js");
const { isVerifiedConnectorCalendarSync } = require("./connector-calendar-sync.js");
const { isVerifiedEventGoalSerendipity } = require("./event-goal-serendipity.js");
const {
  notifyTelegramReport,
  notifyTelegramPhoto,
  parseTelegramMessageId,
} = require("./outbound-guardian.js");
const { hashChatId } = require("./telegram.js");

const TENANT = /^[a-z0-9][a-z0-9._-]{0,199}$/;
const NEW_EVENT_KEYS = Object.freeze(["calendarSync", "dateInventory", "eventRef", "goalDecision", "selection"]);
const PRIORITY_CLASSES = Object.freeze(["yc_hackathon", "open_talk", "ai", "crypto", "startup", "other"]);
const TALK_STATES = Object.freeze(["not_open", "application_ready", "submitted", "provider_verified", "accepted", "rejected", "human_action_required"]);
const UNSAFE = /\{\{|\}\}|\b(?:TODO|TBD|password|cookie|guest[_ -]?key|api[_ -]?key|access[_ -]?token)\b/i;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function invalid() { throw new Error("Connector coverage Telegram invalid"); }

function uncertain(message) {
  const error = new Error(message);
  error.unknownEffect = true;
  return error;
}

function displayText(value) {
  return String(value).replace(/[<>]/g, "");
}

function safeText(value, max) {
  const result = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  if (!result || result.length > max || UNSAFE.test(result)) invalid();
  return result;
}

function googleCalendarUrl(value) {
  let url;
  try { url = new URL(String(value == null ? "" : value)); } catch { invalid(); }
  if (
    url.protocol !== "https:"
    || !["calendar.google.com", "www.google.com"].includes(url.hostname)
    || !url.pathname.startsWith("/calendar/") || url.username || url.password
  ) invalid();
  return url.toString();
}

function jaDate(date) {
  const [year, month, day] = String(date).split("-").map(Number);
  if (!year || !month || !day) invalid();
  return `${year}年${month}月${day}日`;
}

function shortDate(date) {
  const [, month, day] = String(date).split("-").map(Number);
  return `${month}/${day}`;
}

function localDate(instant, timeZone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(instant)).filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function jaTimeRange(startAt, endAt, timeZone) {
  const format = (value) => new Intl.DateTimeFormat("ja-JP", {
    timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(value));
  return `${format(startAt)}〜${format(endAt)}`;
}

function jaInstant(value, timeZone) {
  const instant = String(value == null ? "" : value).trim();
  if (!Number.isFinite(Date.parse(instant)) || !/[zZ]|[+-]\d\d:\d\d$/.test(instant)) invalid();
  const parts = Object.fromEntries(new Intl.DateTimeFormat("ja-JP", {
    timeZone, year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(instant)).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${parts.year}/${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`;
}

function normalizeNewEvents(coverage, rows) {
  if (!Array.isArray(rows)) invalid();
  const coveredDates = new Set(coverage.days.filter((day) => day.status === "covered_new").map((day) => day.date));
  const seenRefs = new Set();
  const seenDates = new Set();
  const result = rows.map((row) => {
    if (
      !row || typeof row !== "object" || Array.isArray(row)
      || Object.keys(row).sort().join(",") !== [...NEW_EVENT_KEYS].sort().join(",")
      || !(isVerifiedLumaDateInventory(row.dateInventory) || isVerifiedEventProviderDateInventory(row.dateInventory))
      || !isVerifiedConnectorCalendarSync(row.calendarSync)
      || (isVerifiedLumaDateInventory(row.dateInventory) && !isVerifiedEventGoalSerendipity(row.goalDecision))
    ) invalid();
    const eventRef = String(row.eventRef == null ? "" : row.eventRef).trim();
    const selection = row.selection;
    if (!selection || typeof selection !== "object" || Array.isArray(selection)
      || Object.keys(selection).sort().join(",") !== "application_deadline_at,preference_reason,priority_class,talk_state"
      || !PRIORITY_CLASSES.includes(selection.priority_class)
      || !TALK_STATES.includes(selection.talk_state)
      || (selection.application_deadline_at != null && !Number.isFinite(Date.parse(selection.application_deadline_at)))) invalid();
    if (
      !eventRef || seenRefs.has(eventRef)
      || row.calendarSync.event_ref !== eventRef
      || row.calendarSync.canonical_event_url == null
      || (row.goalDecision && row.dateInventory.inventory_snapshot_id !== row.goalDecision.inventory_snapshot_id)
    ) invalid();
    const event = row.dateInventory.days.flatMap((day) => day.events)
      .find((candidate) => candidate.event_ref === eventRef);
    const goal = row.goalDecision && row.goalDecision.ranked_events.find((candidate) => candidate.event_ref === eventRef);
    if (!event || (isVerifiedLumaDateInventory(row.dateInventory) && !goal) || event.canonical_url !== row.calendarSync.canonical_event_url) invalid();
    const date = localDate(event.starts_at, coverage.timezone);
    const coverageDay = coverage.days.find((day) => day.date === date);
    if (
      !coverageDay || coverageDay.status !== "covered_new"
      || !coverageDay.evidence_refs.includes(row.calendarSync.registration_receipt_ref)
      || !coverageDay.evidence_refs.includes(row.calendarSync.calendar_event_ref)
    ) invalid();
    seenRefs.add(eventRef);
    seenDates.add(date);
    return Object.freeze({
      date,
      title: safeText(event.title, 160),
      venue: safeText(event.venue_name || event.venue_address, 160),
      starts_at: event.starts_at,
      ends_at: event.ends_at,
      reason: safeText(goal ? goal.goal_reason : "Calendarの空き枠に適合した登録", 240),
      priority_class: selection.priority_class,
      preference_reason: safeText(selection.preference_reason, 500),
      talk_state: selection.talk_state,
      application_deadline_at: selection.application_deadline_at,
      event_url: event.canonical_url,
      calendar_url: googleCalendarUrl(row.calendarSync.calendar_event_url),
    });
  });
  if (seenDates.size !== coveredDates.size || [...coveredDates].some((date) => !seenDates.has(date))) invalid();
  return result.sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at));
}

function buildConnectorCoverageTelegramMessage(input = {}) {
  const coverage = input.coverage;
  if (
    !isVerifiedRollingEventCoverage(coverage)
    || !Number.isInteger(coverage.horizon_days) || coverage.horizon_days < 1
  ) invalid();
  const horizonDays = coverage.horizon_days;
  const calendar = googleCalendarUrl(input.calendarCoverageUrl);
  const newEvents = normalizeNewEvents(coverage, input.newEvents);
  const rejectionCounts = input.rejectionCounts == null ? null : input.rejectionCounts;
  if (rejectionCounts != null && (
    !rejectionCounts || typeof rejectionCounts !== "object" || Array.isArray(rejectionCounts)
    || Object.keys(rejectionCounts).sort().join(",") !== "other,unknown,weak"
    || Object.values(rejectionCounts).some((value) => !Number.isInteger(value) || value < 0 || value > 100_000)
  )) invalid();
  const lines = [
    coverage.counts.open === 0
      ? `✅ 今後${horizonDays}日のevent予定を確認しました。`
      : `🔌 Connector ${horizonDays}日予約状況`,
    "",
    `確認期間: ${jaDate(coverage.window_start_date)}〜${jaDate(coverage.window_end_date)}`,
    `対象日: ${horizonDays}日`,
    `既存の対面予定: ${coverage.counts.covered_existing}日`,
    `Connectorが新しく予約: ${coverage.counts.covered_new}日`,
    `固定予定で追加不可: ${coverage.counts.unavailable}日`,
    `未処理の空き: ${coverage.counts.open}日`,
  ];
  if (newEvents.length > 0) {
    lines.push("", "今回予約し、登録証拠とCalendar登録を照合したevent:");
    for (const [index, event] of newEvents.entries()) {
      lines.push(
        `${index + 1}. ${shortDate(event.date)} ${displayText(event.title)}`,
        `   ${jaTimeRange(event.starts_at, event.ends_at, coverage.timezone)} / ${displayText(event.venue)}`,
        `   理由: ${displayText(event.reason)}`,
        `   優先度: ${event.priority_class}`,
        `   選定理由: ${displayText(event.preference_reason)}`,
        `   LT: ${event.talk_state}`,
        ...(event.application_deadline_at == null ? [] : [`   LT申請締切: ${jaInstant(event.application_deadline_at, coverage.timezone)}`]),
        "   イベントページ:",
        `   ${event.event_url}`,
        "   Calendar:",
        `   ${event.calendar_url}`,
      );
    }
  }
  if (coverage.counts.open > 0) {
    const dates = coverage.days.filter((day) => day.status === "open").map((day) => shortDate(day.date));
    lines.push(
      "",
      `空いている日: ${dates.join("、")}`,
      "この日々は終了扱いにしていません。予約が成立するまで探索と申込みを続けています。",
    );
    if (rejectionCounts) lines.push(
      `品質基準で自動申請しなかった候補: weak ${rejectionCounts.weak}件 / unknown ${rejectionCounts.unknown}件 / other ${rejectionCounts.other}件`,
      "適格候補が0件でも失敗ではありません。品質を落とさず次の探索を続けます。",
    );
  } else if (coverage.counts.covered_new === 0) {
    lines.push("", "予約できる空き枠が残っていないため、二重予約を作りませんでした。");
  } else {
    lines.push("", `${horizonDays}日間の未処理日はありません。既存のCalendar予定と重なる予約もありません。`);
  }
  lines.push("", `${horizonDays}日のCalendarを開く:`, calendar);
  const message = lines.join("\n");
  if (message.length > 4_096 || /runner|bounded|none:/i.test(message)) invalid();
  return message;
}

async function deliverConnectorCoverageTelegram(input = {}, dependencies = {}) {
  const tenant = String(input.tenantId == null ? "" : input.tenantId).trim();
  const target = String(input.telegramTarget == null ? "" : input.telegramTarget).trim();
  if (!TENANT.test(tenant) || !target || input.coverage?.tenant_id !== tenant) invalid();
  const message = buildConnectorCoverageTelegramMessage(input);
  const observedAtMs = Date.parse((dependencies.observedAt || (() => new Date().toISOString()))());
  if (!Number.isFinite(observedAtMs)) invalid();
  const observedAt = new Date(observedAtMs).toISOString();
  let photoInput = null;
  if (Array.isArray(input.newEvents) && input.newEvents.length > 0) {
    const evidence = input.registrationEvidence;
    const row = input.newEvents.length === 1 ? input.newEvents[0] : null;
    const event = row && row.dateInventory && row.dateInventory.days
      .flatMap((day) => day.events).find((candidate) => candidate.event_ref === row.eventRef);
    const bytes = evidence && evidence.bytes;
    const digest = Buffer.isBuffer(bytes) ? createHash("sha256").update(bytes).digest("hex") : "";
    if (
      !evidence || !event || evidence.event_ref !== row.eventRef
      || evidence.canonical_url !== event.canonical_url
      || !Buffer.isBuffer(bytes) || bytes.length < 5_000
      || !bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)
      || evidence.artifact_sha256 !== digest
      || evidence.artifact_ref !== `object://sha256/${digest}`
    ) invalid();
    photoInput = { bytes, digest, caption: `✅ 登録済み証拠: ${safeText(event.title, 160)}\n${event.canonical_url}` };
  }
  const send = dependencies.send || notifyTelegramReport;
  let response;
  try {
    response = await send(message, {
      telegramTarget: target,
      idempotencyKey: `connector-coverage:${input.coverage.coverage_snapshot_id}`,
    });
  } catch (error) {
    if (error && error.unknownEffect === true) throw error;
    throw uncertain("Connector coverage Telegram delivery uncertain");
  }
  let providerId;
  try { providerId = parseTelegramMessageId(response || {}); }
  catch {
    const error = new Error("Connector coverage Telegram needs a positive message ID");
    error.unknownEffect = true;
    throw error;
  }
  let photo = null;
  if (photoInput) {
    const sendPhoto = dependencies.sendPhoto || notifyTelegramPhoto;
    let photoResponse;
    try {
      photoResponse = await sendPhoto(photoInput.bytes, {
        telegramTarget: target,
        caption: photoInput.caption,
        idempotencyKey: `connector-coverage-photo:${input.coverage.coverage_snapshot_id}`,
      });
    } catch (error) {
      if (error && error.unknownEffect === true) throw error;
      throw uncertain("Connector coverage Telegram photo delivery uncertain");
    }
    let photoProviderId;
    try { photoProviderId = parseTelegramMessageId(photoResponse || {}); }
    catch {
      const error = new Error("Connector coverage Telegram photo needs a positive message ID");
      error.unknownEffect = true;
      throw error;
    }
    photo = { photo_provider_id: photoProviderId, artifact_sha256: photoInput.digest };
  }
  return Object.freeze({
    kind: "connector_coverage_telegram_delivery",
    provider_id: providerId,
    ...(photo || {}),
    observed_at: observedAt,
    tenant_id: tenant,
    chat_id_sha256: hashChatId(target),
    coverage_snapshot_id: input.coverage.coverage_snapshot_id,
  });
}

module.exports = {
  buildConnectorCoverageTelegramMessage,
  deliverConnectorCoverageTelegram,
};
