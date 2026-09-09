"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  WAKE_LEVELS, lateNoticeUserOnce, travelUserOnce, wakeUserOnce,
} = require("../scheduler.js");
const { createInMemoryLateApprovalStore } = require("../lib/late-approval.js");

const MINUTE = 60_000;
const EVENT_START_ISO = "2026-07-23T14:00:00+09:00";
const EVENT_START_MS = Date.parse(EVENT_START_ISO);
const TRAVEL_NOW_MS = Date.parse("2026-07-22T12:00:00+09:00");
const DEPARTURE_MS = Date.parse("2026-07-23T13:20:00+09:00");
const ALLOWANCE_DEPS = Object.freeze({
  reserveManagedAction: async () => ({
    allowed: true,
    used: 0,
    limit: 30,
    periodStart: "2026-07-01",
    resetAt: "2026-08-01T00:00:00.000Z",
    reservationToken: "controlled-reservation",
  }),
  completeManagedAction: async () => ({ allowed: true }),
  releaseManagedAction: async () => ({ allowed: true }),
});

function calendarEvent({ id, summary, location, startIso, endIso, attendees = [] }) {
  return {
    kind: "calendar#event",
    etag: `\"${id}-etag\"`,
    id,
    status: "confirmed",
    htmlLink: `https://calendar.google.test/event?eid=${id}`,
    created: "2026-07-22T03:00:00.000Z",
    updated: "2026-07-22T03:00:00.000Z",
    summary,
    location,
    creator: { email: "owner@controlled.invalid", self: true },
    organizer: { email: "owner@controlled.invalid", self: true },
    start: { dateTime: startIso, timeZone: "Asia/Tokyo" },
    end: { dateTime: endIso, timeZone: "Asia/Tokyo" },
    iCalUID: `${id}@controlled.invalid`,
    sequence: 0,
    attendees,
  };
}

class JourneyCalendar {
  constructor(rows) {
    this.rows = rows;
    this.acceptCreates = false;
    this.createAttempts = [];
    this.listRequests = [];
  }

  async listEventsRaw(uid, opts = {}) {
    assert.equal(uid, "controlled-user");
    this.listRequests.push({ ...opts });
    const min = Date.parse(opts.timeMin || "");
    const max = Date.parse(opts.timeMax || "");
    return this.rows
      .filter((row) => {
        const start = Date.parse(row.start.dateTime);
        const end = Date.parse(row.end.dateTime);
        return (!Number.isFinite(min) || end > min) && (!Number.isFinite(max) || start < max);
      })
      .sort((a, b) => Date.parse(a.start.dateTime) - Date.parse(b.start.dateTime));
  }

  async createEvent(uid, payload) {
    assert.equal(uid, "controlled-user");
    this.createAttempts.push({ ...payload });
    if (!this.acceptCreates) return { successful: false, error: "provider_rejected" };

    const startMs = Date.parse(`${payload.start_datetime}Z`);
    const durationMs = (payload.event_duration_hour * 60 + payload.event_duration_minutes) * MINUTE;
    const row = calendarEvent({
      id: `provider-travel-${this.createAttempts.length}`,
      summary: payload.summary,
      location: payload.location,
      startIso: new Date(startMs).toISOString(),
      endIso: new Date(startMs + durationMs).toISOString(),
    });
    this.rows.push(row);
    return { successful: true, data: row };
  }
}

test("CORE 8e drives the production DAILY journey with provider-ordered reporting and durable dedup", async () => {
  const prior = calendarEvent({
    id: "controlled-prior",
    summary: "自宅での予定",
    location: "自宅",
    startIso: "2026-07-23T12:00:00+09:00",
    endIso: "2026-07-23T13:00:00+09:00",
  });
  const commitment = calendarEvent({
    id: "controlled-commitment",
    summary: "新宿で打ち合わせ",
    location: "新宿",
    startIso: EVENT_START_ISO,
    endIso: "2026-07-23T15:00:00+09:00",
    attendees: [{ email: "recipient@controlled.invalid", responseStatus: "accepted" }],
  });
  const calendar = new JourneyCalendar([prior, commitment]);
  const telegramMessages = [];
  const telegramExtras = [];
  const user = {
    uid: "controlled-user",
    name: "Controlled User",
    phone: "+810000000000",
    home_address: null,
    telegram_chat_id: "controlled-chat",
    wake_policy: "all-events",
    call_language: "ja",
    daily_automation_enabled: true,
    notifications_enabled: true,
    call_enabled: true,
    paid: true,
  };
  const sendMessage = async (_token, _chatId, text, extra) => {
    telegramMessages.push(text);
    telegramExtras.push(extra);
    return { ok: true, result: { message_id: telegramMessages.length } };
  };
  const travelDeps = {
    ...ALLOWANCE_DEPS,
    nowMs: TRAVEL_NOW_MS,
    apiKey: "controlled-composio-key",
    mapsKey: "controlled-maps-key",
    calendar,
    directionsMinutes: async () => 35,
    telegramToken: "controlled-telegram-token",
    sendMessage,
  };

  await travelUserOnce(user, travelDeps);
  assert.equal(calendar.listRequests.length, 1, "the production travel path observes the calendar");
  assert.equal(calendar.createAttempts.length, 1, "the provider sees one create attempt");
  assert.equal(calendar.rows.filter((row) => row.summary.startsWith("[Travel]")).length, 0,
    "a rejected provider write creates no block");
  assert.equal(telegramMessages.length, 0, "provider rejection cannot emit a success report");

  calendar.acceptCreates = true;
  await travelUserOnce(user, travelDeps);
  await travelUserOnce(user, travelDeps);
  const travelBlocks = calendar.rows.filter((row) => row.summary.startsWith("[Travel]"));
  assert.equal(travelBlocks.length, 1, "accepted travel autofill creates exactly one block");
  assert.equal(calendar.createAttempts.length, 2, "a repeated tick does not write a duplicate");
  assert.equal(Date.parse(travelBlocks[0].start.dateTime), DEPARTURE_MS);
  assert.equal(Date.parse(travelBlocks[0].end.dateTime), EVENT_START_MS);
  assert.deepEqual(telegramMessages, [
    "📅 明日14:00「新宿で打ち合わせ」を確認しました。自宅からの移動時間40分をカレンダーに入れておきました。13:20発です。",
  ], "the exact §9.11 travel report is sent once, after calendar acceptance");

  const wakeClaims = new Set();
  const dialed = [];
  let dailyPolls = 0;
  let wakeLateChecks = 0;
  const wakeDeps = {
    calendar,
    mapsKey: "controlled-maps-key",
    recordDailyPoll: async () => { dailyPolls++; return true; },
    lateNotice: async () => { wakeLateChecks++; return { decision: "location_missing" }; },
    claimWake: async (_uid, key) => {
      if (wakeClaims.has(key)) return false;
      wakeClaims.add(key);
      return true;
    },
    placeCall: async ({ streamUrl }) => {
      const params = new URL(streamUrl, "https://controlled.invalid").searchParams;
      dialed.push({ urgency: params.get("urgency"), eventKey: params.get("wakeEventKey") });
      return { ok: true, ccid: `controlled-call-${dialed.length}` };
    },
    releaseWake: async () => assert.fail("successful dials retain their claims"),
    alertLowBalance: async () => assert.fail("successful dials do not alert"),
  };

  await wakeUserOnce(user, DEPARTURE_MS - 15 * MINUTE, wakeDeps);
  await wakeUserOnce(user, DEPARTURE_MS - 10 * MINUTE, wakeDeps);
  await wakeUserOnce(user, DEPARTURE_MS - 10 * MINUTE, wakeDeps);
  await wakeUserOnce(user, DEPARTURE_MS - 5 * MINUTE, wakeDeps);
  await wakeUserOnce(user, DEPARTURE_MS - 5 * MINUTE, wakeDeps);

  assert.deepEqual(WAKE_LEVELS.map((level) => level.min), [10, 5], "T-15 is not configured");
  assert.deepEqual(dialed.map((call) => call.urgency), ["firm", "harsh"]);
  assert.deepEqual(dialed.map((call) => call.eventKey.split("|").at(-1)), ["10", "5"]);
  assert.equal(wakeClaims.size, 2, "repeated wake ticks retain exactly two claims");
  assert.equal(dailyPolls, 5, "every production wake invocation records its poll");
  assert.equal(wakeLateChecks, 5, "the same wake invocation retains the late-decision hook");

  const decisionNowMs = Date.parse("2026-07-23T13:10:00+09:00");
  const location = {
    latitude: 35.0,
    longitude: 139.0,
    expires_at: "2026-07-23T16:00:00+09:00",
  };
  let routeMinutes = 20;
  const emails = [];
  const lateApprovalStore = createInMemoryLateApprovalStore({ nowMs: decisionNowMs });
  const lateDeps = {
    supaUrl: "https://controlled-db.invalid",
    supaKey: "controlled-service-key",
    calendar,
    location,
    mapsKey: "controlled-maps-key",
    telegramToken: "controlled-telegram-token",
    routeMinutes: async () => routeMinutes,
    lateApprovalStore,
    sendLateNotice: async () => { throw new Error("tick must not send mail before approval"); },
    sendMessage,
  };

  const onTime = await lateNoticeUserOnce(user, decisionNowMs, lateDeps);
  assert.equal(onTime.decision, "on_time");
  assert.equal(emails.length, 0, "on-time performs zero email I/O");
  assert.equal(telegramMessages.length, 1, "on-time performs zero late Telegram I/O");

  routeMinutes = 60;
  const late = await lateNoticeUserOnce(user, decisionNowMs, lateDeps);
  const repeatedLate = await lateNoticeUserOnce(user, decisionNowMs, lateDeps);
  assert.equal(late.decision, "late");
  assert.equal(late.sent, false, "the late tick never sends before a Telegram decision");
  assert.equal(late.approvalRequired, true);
  assert.equal(late.draft.status, "awaiting_decision");
  assert.equal(repeatedLate.draft.duplicate, true, "repeated ticks reuse the durable draft");
  assert.equal(emails.length, 0, "the late tick performs zero mail I/O");
  assert.equal(telegramMessages.length, 2, "the late tick sends one approval card after the travel report");
  assert.match(telegramMessages[1], /⚠️ 遅刻連絡の確認/);
  assert.deepEqual(
    telegramExtras[1].reply_markup.inline_keyboard[0].map((button) => button.text),
    ["送る", "送らない"],
    "the approval card exposes exactly two decisions",
  );
});

test("CORE 8e preserves accepted travel results and continues reports when one Telegram send fails", async () => {
  const first = calendarEvent({
    id: "controlled-first",
    summary: "最初の予定",
    location: "会場A",
    startIso: "2026-07-24T14:00:00+09:00",
    endIso: "2026-07-24T15:00:00+09:00",
  });
  const second = calendarEvent({
    id: "controlled-second",
    summary: "次の予定",
    location: "会場B",
    startIso: "2026-07-24T15:30:00+09:00",
    endIso: "2026-07-24T16:30:00+09:00",
  });
  const existingReturn = calendarEvent({
    id: "controlled-existing-return",
    summary: "[Travel] 🚆 会場B→自宅",
    location: "自宅",
    startIso: "2026-07-24T16:30:00+09:00",
    endIso: "2026-07-24T17:00:00+09:00",
  });
  const calendar = new JourneyCalendar([first, second, existingReturn]);
  calendar.acceptCreates = true;
  const attempts = [];
  const result = await travelUserOnce({
    uid: "controlled-user",
    home_address: "自宅",
    telegram_chat_id: "controlled-chat",
    notifications_enabled: true,
    daily_automation_enabled: true,
  }, {
    ...ALLOWANCE_DEPS,
    nowMs: TRAVEL_NOW_MS,
    apiKey: "controlled-composio-key",
    mapsKey: "controlled-maps-key",
    calendar,
    directionsMinutes: async () => 20,
    telegramToken: "controlled-telegram-token",
    sendMessage: async (_token, _chatId, text) => {
      attempts.push(text);
      if (attempts.length === 1) throw new Error("controlled Telegram outage");
      return { ok: true, result: { message_id: 2 } };
    },
  });

  assert.equal(result.inserted, 2, "provider-accepted calendar results survive report I/O failure");
  assert.equal(calendar.rows.filter((row) => row.summary.startsWith("[Travel]")).length, 3,
    "both accepted outbound blocks remain plus the pre-existing return block");
  assert.equal(attempts.length, 2, "one report failure cannot suppress a later accepted report");
});

test("CORE fix R1: verified user timezone wins when event timeZone is absent, invalid user timezone falls back to event timezone", async () => {
  const newYorkEvent = calendarEvent({
    id: "controlled-new-york-event",
    summary: "New York meeting",
    location: "New York venue",
    startIso: "2026-07-24T14:00:00-04:00",
    endIso: "2026-07-24T15:00:00-04:00",
  });
  delete newYorkEvent.start.timeZone;
  delete newYorkEvent.end.timeZone;
  const newYorkCalendar = new JourneyCalendar([newYorkEvent]);
  newYorkCalendar.acceptCreates = true;
  const newYorkCalls = [];
  await travelUserOnce({
    uid: "controlled-user",
    home_address: "New York home",
    call_time_zone: "America/New_York",
    daily_automation_enabled: true,
    notifications_enabled: false,
  }, {
    ...ALLOWANCE_DEPS,
    nowMs: Date.parse("2026-07-23T12:00:00-04:00"),
    apiKey: "controlled-composio-key",
    mapsKey: "controlled-maps-key",
    calendar: newYorkCalendar,
    directionsMinutes: async (...args) => { newYorkCalls.push(args); return 20; },
  });
  assert.ok(newYorkCalls.length >= 2, "outbound and return route calls should use the production travel path");
  assert.equal(newYorkCalls[0][6].timezone, "America/New_York");
  assert.equal(newYorkCalls.at(-1)[6].timezone, "America/New_York");

  const conflictingEvent = calendarEvent({
    id: "controlled-conflicting-event-timezone",
    summary: "Tokyo-labelled event for New York user",
    location: "Conflicting venue",
    startIso: "2026-07-24T14:00:00+09:00",
    endIso: "2026-07-24T15:00:00+09:00",
  });
  const conflictingCalendar = new JourneyCalendar([conflictingEvent]);
  conflictingCalendar.acceptCreates = true;
  const conflictingCalls = [];
  await travelUserOnce({
    uid: "controlled-user",
    home_address: "New York home",
    call_time_zone: "America/New_York",
    daily_automation_enabled: true,
    notifications_enabled: false,
  }, {
    ...ALLOWANCE_DEPS,
    nowMs: Date.parse("2026-07-23T12:00:00-04:00"),
    apiKey: "controlled-composio-key",
    mapsKey: "controlled-maps-key",
    calendar: conflictingCalendar,
    directionsMinutes: async (...args) => { conflictingCalls.push(args); return 20; },
  });
  assert.equal(conflictingCalls[0][6].timezone, "America/New_York");

  const eventTimezone = calendarEvent({
    id: "controlled-event-timezone",
    summary: "Tokyo meeting",
    location: "Tokyo venue",
    startIso: "2026-07-25T14:00:00+09:00",
    endIso: "2026-07-25T15:00:00+09:00",
  });
  const eventTimezoneCalendar = new JourneyCalendar([eventTimezone]);
  eventTimezoneCalendar.acceptCreates = true;
  const fallbackCalls = [];
  await travelUserOnce({
    uid: "controlled-user",
    home_address: "Tokyo home",
    call_time_zone: "Mars/Olympus",
    daily_automation_enabled: true,
    notifications_enabled: false,
  }, {
    ...ALLOWANCE_DEPS,
    nowMs: Date.parse("2026-07-23T12:00:00+09:00"),
    apiKey: "controlled-composio-key",
    mapsKey: "controlled-maps-key",
    calendar: eventTimezoneCalendar,
    directionsMinutes: async (...args) => { fallbackCalls.push(args); return 20; },
  });
  assert.ok(fallbackCalls.length >= 2);
  assert.equal(fallbackCalls[0][6].timezone, "Asia/Tokyo");
});
