"use strict";
// spec 2026-08-01-lm-daily-organ-design.md §1.2「計測の穴」+ §3 row 1b.
//
// 1a made a passed threshold still ring. 1b makes the calls that STILL do not ring leave a trace.
// Two ways a wake can fail to reach the user, both of which are invisible today:
//   1. the dial fails — releaseWake then DELETEs the claim, erasing the evidence with it;
//   2. departure passes LATE_CUTOFF_MIN having never been claimed — nothing was ever attempted.
// These tests pin that each writes ONE reasoned row, that a healthy call writes none, and that a
// ledger outage can never stop the retry the user actually needs.
//
// Run: node --test test/wake-miss-record.test.js
const { test } = require("node:test");
const assert = require("node:assert");

process.env.LM_CALL_SECRET = "unit_secret";
process.env.PUBLIC_WSS = "wss://life-call.invalid";

const { wakeUserOnce, LATE_CUTOFF_MIN } = require("../scheduler.js");
const { WAKE_MISS_REASONS } = require("../lib/wake-miss.js");

const MINUTE = 60_000;
const EVENT_START_ISO = "2026-08-05T14:00:00+09:00";
const EVENT_START_MS = Date.parse(EVENT_START_ISO);
const TRAVEL_MIN = 35; // + resolveDeparture's 5-min buffer → departure = start − 40 min
const DEPARTURE_MS = EVENT_START_MS - 40 * MINUTE;
const TEST_PHONE = "+99900000000";

const USER = {
  paid: true,
  uid: "miss-user",
  name: "Miss User",
  phone: TEST_PHONE,
  home_address: "東京都渋谷区",
  call_language: "ja",
  daily_automation_enabled: true,
  call_enabled: true,
  notifications_enabled: false, // wakes only: silences the late/mental/care legs
};

const EVENT = {
  id: "miss-event",
  summary: "新宿で打ち合わせ",
  location: "新宿",
  startMs: EVENT_START_MS,
  startIso: EVENT_START_ISO,
  endMs: EVENT_START_MS + 60 * MINUTE,
};

function harness({ dial, recordThrows = false } = {}) {
  const held = new Set();
  const dialed = [];
  const released = [];
  const missed = [];
  const deps = {
    recordDailyPoll: async () => true,
    fetchUpcomingEvents: async () => [{ ...EVENT }],
    mental: async () => null,
    care: async () => ({ status: "already_scanned" }),
    mapsKey: "miss-maps-key",
    directionsMinutes: async () => TRAVEL_MIN,
    claimWake: async (_uid, key) => {
      if (held.has(key)) return false;
      held.add(key);
      return true;
    },
    placeCall: async () => (dial ? dial(dialed.push({}) ) : (dialed.push({}), { ok: true, ccid: "c1" })),
    releaseWake: async (_uid, key) => { released.push(key); held.delete(key); },
    alertLowBalance: async () => {},
    wakeWasClaimed: async (_uid, key) => held.has(key),
    recordWakeMiss: async (uid, miss) => {
      if (recordThrows) throw new Error("ledger down");
      missed.push({ uid, ...miss });
      return { ok: true };
    },
  };
  return { deps, held, dialed, released, missed };
}

test("a dial failure is recorded with its reason, not just erased with the claim", async () => {
  const h = harness({ dial: () => ({ ok: false, error: "balance too low" }) });
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);

  assert.equal(h.released.length, 1, "the claim is still released so the next tick retries");
  assert.equal(h.missed.length, 1, "and the failure now exists as a row");
  const row = h.missed[0];
  assert.equal(row.uid, USER.uid);
  assert.equal(row.reason, WAKE_MISS_REASONS.DIAL_FAILED);
  assert.match(row.detail, /balance too low/, "the operator reads WHY without opening a log");
  assert.equal(row.levelMin, 5);
  assert.equal(row.eventStartIso, EVENT_START_ISO);
  assert.equal(Date.parse(row.dueAtIso), DEPARTURE_MS - 5 * MINUTE,
    "due_at is when the call was owed — the clock time /status shows the user");
  assert.equal(row.eventKey, `${USER.uid}|${EVENT_START_ISO}|5`,
    "keyed per (event, level) so a repeat refreshes one row instead of duplicating");
});

test("a call that goes through records no miss", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 1);
  assert.deepEqual(h.missed, [], "success must never leave a failure row behind");
});

test("a departure that passes with nothing ever claimed records one no-call row", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS - LATE_CUTOFF_MIN * MINUTE + MINUTE, h.deps); // ~1 min past cutoff

  assert.deepEqual(h.dialed, [], "past the cutoff the late-notice organ owns this, not a wake call");
  assert.equal(h.missed.length, 1, "silence is the thing being recorded");
  const row = h.missed[0];
  assert.equal(row.reason, WAKE_MISS_REASONS.NO_CALL_BEFORE_DEPARTURE);
  assert.equal(row.eventKey, `${USER.uid}|${EVENT_START_ISO}|departure`,
    "its own key, so it never overwrites a per-level dial failure");
  assert.equal(Date.parse(row.dueAtIso), DEPARTURE_MS, "the departure time the user was owed a call before");
});

test("a departure that passes AFTER a real call records nothing", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps); // rings for real
  h.missed.length = 0;
  await wakeUserOnce(USER, DEPARTURE_MS - LATE_CUTOFF_MIN * MINUTE + MINUTE, h.deps);
  assert.deepEqual(h.missed, [], "a call that happened is not a missed call");
});

test("the no-call row is written near the cutoff, not on every tick for the rest of the day", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS - LATE_CUTOFF_MIN * MINUTE + 30 * MINUTE, h.deps);
  assert.deepEqual(h.missed, [], "long past the cutoff this is the late organ's business, not a new record");
});

test("a ledger outage never stops the retry the user actually needs", async () => {
  const h = harness({ dial: () => ({ ok: false, error: "balance too low" }), recordThrows: true });
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.released.length, 1, "recording is best-effort; releasing the claim is not");
});

// §5.4「沈黙で失敗しない」+ §6: recording is not enough — the user must be TOLD, once. These pin the
// notice leg: it fires on the first miss, stays quiet on every retry tick, and never fires for a user
// who turned Telegram notifications off (that user still sees it in /status).
const NOTIFIED_USER = { ...USER, notifications_enabled: true, telegram_chat_id: "12345", call_time_zone: "Asia/Tokyo" };

function notifyHarness({ dial, claimed = true } = {}) {
  const base = harness({ dial });
  const sent = [];
  const claims = [];
  base.deps.lateNotice = async () => null;
  base.deps.telegramToken = "tg-token";
  base.deps.sendMessage = async (token, chatId, text) => { sent.push({ token, chatId, text }); return { ok: true }; };
  base.deps.claimWakeMissNotice = async (uid, eventKey) => {
    claims.push({ uid, eventKey });
    return claimed && claims.length === 1
      ? { uid, event_key: eventKey, reason: WAKE_MISS_REASONS.DIAL_FAILED, detail: "balance too low", due_at: new Date(DEPARTURE_MS - 5 * MINUTE).toISOString() }
      : null;
  };
  return { ...base, sent, claims };
}

test("a missed call tells the user once, and stays quiet while the dial keeps failing", async () => {
  const h = notifyHarness({ dial: () => ({ ok: false, error: "balance too low" }) });
  await wakeUserOnce(NOTIFIED_USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.sent.length, 1, "the user hears about it without having to ask");
  assert.equal(h.sent[0].chatId, "12345");
  assert.match(h.sent[0].text, /呼び出しに失敗/, "in the user's language");
  assert.match(h.sent[0].text, /balance too low/);

  await wakeUserOnce(NOTIFIED_USER, DEPARTURE_MS - 4 * MINUTE, h.deps);
  assert.equal(h.sent.length, 1, "the retry tick records again but must not message again");
});

test("a user who turned notifications off is not messaged (the row and /status still carry it)", async () => {
  const h = notifyHarness({ dial: () => ({ ok: false, error: "balance too low" }) });
  await wakeUserOnce({ ...NOTIFIED_USER, notifications_enabled: false }, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.missed.length, 1, "the failure is still recorded");
  assert.deepEqual(h.sent, [], "but an opted-out user is not messaged");
});

test("a Telegram failure never costs the user the retry", async () => {
  const h = notifyHarness({ dial: () => ({ ok: false, error: "balance too low" }) });
  h.deps.sendMessage = async () => { throw new Error("telegram 500"); };
  await wakeUserOnce(NOTIFIED_USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.released.length, 1, "the claim is still released so the next tick retries");
});
