"use strict";
// spec 2026-08-01-lm-daily-organ-design.md §3 row 1c — the done receipt.
//
// The measured failure (§3.1): late and mental ran BEFORE the dial inside one 90s per-user budget, so
// two slow organs abandoned the user before a call was ever attempted. These tests pin the property
// that fixes it — the dial does not run the organs at all — and pin the constraint that made method A
// worth choosing: the split must not double Composio calls.
//
// Run: node --test test/wake-loop-isolation.test.js
const { test } = require("node:test");
const assert = require("node:assert");

process.env.LM_CALL_SECRET = "unit_secret";
process.env.PUBLIC_WSS = "wss://life-call.invalid";

const {
  wakeCallOnce, wakeUserOnce, reminderUserOnce, organsUserOnce, reminderTick, startReminderLoop,
  WAKE_USER_TIMEOUT_MS, REMINDER_TIMEOUT_MS, forEachUserSafe,
} = require("../scheduler.js");
const { travelReminderOnce } = require("../lib/travel-reminder.js");
const { clearEvents, getEvents } = require("../lib/event-cache.js");

const MINUTE = 60_000;
const EVENT_START_ISO = "2026-08-05T14:00:00+09:00";
const EVENT_START_MS = Date.parse(EVENT_START_ISO);
const TRAVEL_MIN = 35; // + resolveDeparture's 5-min buffer → departure = start − 40 min
const DEPARTURE_MS = EVENT_START_MS - 40 * MINUTE;
const TEST_PHONE = "+99900000000";

const USER = {
  paid: true,
  uid: "iso-user",
  name: "Iso User",
  phone: TEST_PHONE,
  home_address: "東京都渋谷区",
  call_language: "ja",
  daily_automation_enabled: true,
  call_enabled: true,
  notifications_enabled: false,
};

const EVENT = {
  id: "iso-event",
  summary: "新宿で打ち合わせ",
  location: "新宿",
  startMs: EVENT_START_MS,
  startIso: EVENT_START_ISO,
  endMs: EVENT_START_MS + 60 * MINUTE,
};

function deps({ slowOrganMs = 0, fetches } = {}) {
  const dialed = [];
  const held = new Set();
  const stall = async () => { if (slowOrganMs) await new Promise((r) => setTimeout(r, slowOrganMs)); return null; };
  return {
    dialed,
    deps: {
      recordDailyPoll: async () => true,
      fetchUpcomingEvents: async () => { if (fetches) fetches.push(1); return [{ ...EVENT }]; },
      directionsMinutes: async () => TRAVEL_MIN,
      mapsKey: "iso-maps-key",
      claimWake: async (_uid, key) => { if (held.has(key)) return false; held.add(key); return true; },
      placeCall: async () => { dialed.push(Date.now()); return { ok: true, ccid: "iso-1" }; },
      releaseWake: async () => {},
      alertLowBalance: async () => {},
      recordWakeMiss: async () => ({ ok: true }),
      wakeWasClaimed: async (_uid, key) => held.has(key),
      // every organ stalls
      lateNotice: stall, mental: stall, care: stall, diet: stall, dietNudge: stall,
      preceptsMirror: stall, precepts: stall, relations: stall,
      log: () => {},
    },
  };
}

test("the dial half does not run a single organ — a stalled organ cannot reach it", async () => {
  clearEvents();
  const h = deps({ slowOrganMs: 5000 });
  const started = Date.now();
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  const elapsed = Date.now() - started;
  assert.equal(h.dialed.length, 1, "the call is placed");
  assert.ok(elapsed < 1000, `the dial path must not wait on organs (took ${elapsed}ms)`);
});

test("monthly allowance exhaustion performs zero inline route and zero Telnyx call", async () => {
  clearEvents();
  const h = deps();
  let routes = 0, reserves = 0;
  h.deps.reserveManagedAction = async (_uid, actionKey) => {
    reserves += 1;
    assert.equal(actionKey, EVENT.id);
    return { allowed: false, notify: true };
  };
  h.deps.directionsMinutes = async () => { routes += 1; return TRAVEL_MIN; };
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(reserves, 1);
  assert.equal(routes, 0);
  assert.equal(h.dialed.length, 0);
});

test("voice allowance exhaustion performs zero Telnyx call and releases its pending event action", async () => {
  clearEvents();
  const h = deps();
  let actionReleases = 0;
  h.deps.reserveManagedAction = async () => ({ allowed: true, periodStart: "2026-09-01",
    reservationToken: "11111111-1111-4111-8111-111111111111" });
  h.deps.reserveVoiceAllowance = async () => ({ allowed: false, usedSeconds: 3600, limitSeconds: 3600,
    allowedSeconds: 0, periodStart: "2026-09-01", resetAt: "2026-10-01" });
  h.deps.releaseManagedAction = async () => { actionReleases += 1; };
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 0);
  assert.equal(actionReleases, 1);
});

test("a voice reservation becomes the signed stream context and Telnyx time limit", async () => {
  clearEvents();
  const h = deps();
  let dial;
  h.deps.reserveVoiceAllowance = async () => ({ allowed: true, usedSeconds: 3563, limitSeconds: 3600,
    allowedSeconds: 37, periodStart: "2026-09-01", resetAt: "2026-10-01",
    reservationToken: "11111111-1111-4111-8111-111111111111" });
  h.deps.placeCall = async (input) => { dial = input; return { ok: true, ccid: "voice-budget" }; };
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  const stream = new URL(dial.streamUrl);
  assert.equal(dial.timeLimitSeconds, 37);
  assert.equal(stream.searchParams.get("voiceAllowedSeconds"), "37");
  assert.equal(stream.searchParams.get("voicePeriodStart"), "2026-09-01");
  assert.equal(stream.searchParams.get("voiceReservationToken"), "11111111-1111-4111-8111-111111111111");
});

test("a voice reservation that cannot become durable accepted state never dials", async () => {
  clearEvents();
  const h = deps();
  let voiceReleases = 0, wakeReleases = 0;
  h.deps.reserveVoiceAllowance = async () => ({ allowed: true, usedSeconds: 0, limitSeconds: 3600,
    allowedSeconds: 120, periodStart: "2026-09-01", resetAt: "2026-10-01",
    reservationToken: "11111111-1111-4111-8111-111111111111" });
  h.deps.acceptVoiceAllowance = async () => null;
  h.deps.releaseVoiceAllowance = async () => { voiceReleases += 1; };
  h.deps.releaseWake = async () => { wakeReleases += 1; };
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 0);
  assert.equal(voiceReleases, 1);
  assert.equal(wakeReleases, 1);
});

test("transport-unknown dial retains wake and accepted voice ownership for webhook reconciliation", async () => {
  clearEvents();
  const h = deps();
  let wakeReleases = 0, voiceReleases = 0;
  h.deps.reserveVoiceAllowance = async () => ({ allowed: true, usedSeconds: 0, limitSeconds: 3600,
    allowedSeconds: 120, periodStart: "2026-09-01", resetAt: "2026-10-01",
    reservationToken: "11111111-1111-4111-8111-111111111111" });
  h.deps.acceptVoiceAllowance = async () => ({ allowed: true, usedSeconds: 0, limitSeconds: 3600,
    allowedSeconds: 120, periodStart: "2026-09-01", resetAt: "2026-10-01" });
  h.deps.placeCall = async () => ({ ok: false, error: "delivery unknown", deliveryUnknown: true });
  h.deps.releaseWake = async () => { wakeReleases += 1; };
  h.deps.releaseVoiceAllowance = async () => { voiceReleases += 1; };
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(wakeReleases, 0);
  assert.equal(voiceReleases, 0);
});

test("a hung bookkeeping write cannot hold the dial — the daily poll ledger is not awaited", async () => {
  // Same failure class as the test above, in miniature and easier to miss: recordDailyComposioPoll
  // is 1-2 Supabase round trips with no timeout and no AbortController, and it sat AWAITED in front
  // of the dial. It records that a calendar poll happened today — accounting, not a precondition —
  // so a slow store must cost the ledger, never the phone call. Narrowing the budget from 90s to 20s
  // made this worse, not better: the same stall now abandons the user 4.5x sooner.
  clearEvents();
  const h = deps();
  h.deps.recordDailyPoll = () => new Promise(() => {}); // never resolves, never rejects
  const started = Date.now();
  await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  const elapsed = Date.now() - started;
  assert.equal(h.dialed.length, 1, "the call is still placed");
  assert.ok(elapsed < 1000, `the dial path must not wait on bookkeeping (took ${elapsed}ms)`);
});

test("a failing daily-poll ledger is logged, not swallowed, and never crashes the dial", async () => {
  // Fire-and-forget without a .catch turns a Supabase blip into an unhandled rejection that can take
  // the whole scheduler process down — a worse outcome than the blocking call it replaced. And a
  // .catch that swallows silently recreates the invisibility this whole spec exists to end.
  clearEvents();
  const h = deps();
  h.deps.recordDailyPoll = async () => { throw new Error("supabase 503"); };
  const errors = [];
  const originalError = console.error;
  console.error = (...args) => errors.push(args.join(" "));
  try {
    await wakeCallOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
    await new Promise((r) => setImmediate(r)); // let the detached rejection settle
  } finally {
    console.error = originalError;
  }
  assert.equal(h.dialed.length, 1, "the call is still placed");
  assert.ok(
    errors.some((line) => /supabase 503/.test(line)),
    `the ledger failure is reported, not hidden (saw ${JSON.stringify(errors)})`,
  );
});

test("the wake loop's per-user budget is its own, and far below the organ budget", () => {
  assert.equal(WAKE_USER_TIMEOUT_MS, 20000);
  assert.ok(WAKE_USER_TIMEOUT_MS < 90000, "the shared 90s budget was sized for the care organ, not the dial");
});

test("the reminder loop's default per-tenant budget is 35 seconds", () => {
  assert.equal(REMINDER_TIMEOUT_MS, 35_000);
});

test("the dial publishes the calendar so the organ tick does not fetch it again", async () => {
  clearEvents();
  const fetches = [];
  const h = deps({ fetches });
  const now = DEPARTURE_MS - 5 * MINUTE;
  await wakeCallOnce(USER, now, h.deps);
  assert.equal(fetches.length, 1, "the wake half fetches once");
  assert.ok(getEvents(USER.uid, now), "and publishes what it fetched");

  await organsUserOnce(USER, now, h.deps);
  assert.equal(fetches.length, 1, "the organ half reuses it — the split must not double Composio calls");
});

test("the organ half still fetches when nothing was published (first tick after a restart)", async () => {
  clearEvents();
  const fetches = [];
  const h = deps({ fetches });
  await organsUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(fetches.length, 1, "a cache miss falls back to a real fetch rather than skipping the organs");
});

test("one user blowing the wake budget does not stop the next user's dial", async () => {
  const order = [];
  await forEachUserSafe(
    [{ uid: "slow" }, { uid: "fast" }],
    "wake",
    async (u) => {
      if (u.uid === "slow") await new Promise((r) => setTimeout(r, 200));
      order.push(u.uid);
    },
    50, // a 50ms budget stands in for the real 20s one
  );
  assert.deepEqual(order, ["fast"], "the slow user is abandoned; the fast user is still served");
});

// spec §3 row 1e. The organ tick inherited `call_enabled !== false` from the days when the dial lived
// inside it. Now that the dial has its own loop (which filters on `call_enabled` itself), that copy
// only does harm: care/diet/mental/precepts/relations have nothing to do with a phone, and spec §5.3
// promises the phone-less user the same product over Telegram. The organ tick's own care comment says
// so — "Still runs for call-disabled users" — while the filter above it said otherwise.
const { tick, wakeTick } = require("../scheduler.js");

test("the organ tick serves a user who gave no phone number", async () => {
  const served = [];
  await tick({
    listUsers: async () => [
      { uid: "has-phone", paid: true, phone: TEST_PHONE, daily_automation_enabled: true, call_enabled: true },
      { uid: "no-phone", daily_automation_enabled: true, call_enabled: false },
    ],
    organs: async (u) => { served.push(u.uid); },
    now: 0,
  });
  assert.deepEqual(served, ["has-phone", "no-phone"],
    "organs are not a phone feature — spec §5.3 promises this user the same product over Telegram");
});

test("a paid phone-less tenant reaches non-call organs while the call effect stays at zero", async () => {
  const served = [], dialled = [];
  const user = { uid: "paid-no-phone", paid: true, calendar_provider: "composio_gcal", phone: null, daily_automation_enabled: true, call_enabled: false };
  await tick({ listUsers: async () => [user], organs: async (u) => served.push(u.uid), now: 0 });
  await wakeTick({ listUsers: async () => [user], wake: async () => dialled.push(user.uid), now: 0 });
  assert.deepEqual(served, ["paid-no-phone"]);
  assert.deepEqual(dialled, [], "missing phone/call consent cannot create a call effect");
});

test("the organ tick still respects the one switch that means 'run nothing for me'", async () => {
  const served = [];
  await tick({
    listUsers: async () => [{ uid: "opted-out", daily_automation_enabled: false, call_enabled: true }],
    organs: async (u) => { served.push(u.uid); },
    now: 0,
  });
  assert.deepEqual(served, [], "daily_automation_enabled=false is the real opt-out and still holds");
});

test("the wake tick keeps its own call_enabled filter — dialing a user with no phone is nonsense", async () => {
  const dialled = [];
  await wakeTick({
    listUsers: async () => [
      { uid: "has-phone", paid: true, phone: TEST_PHONE, daily_automation_enabled: true, call_enabled: true },
      { uid: "no-phone", daily_automation_enabled: true, call_enabled: false },
      { uid: "malformed-phone", phone: "090-1234-5678", daily_automation_enabled: true, call_enabled: true },
    ],
    wake: async (u) => { dialled.push(u.uid); },
    now: 0,
  });
  assert.deepEqual(dialled, ["has-phone"], "the filter belongs to the dial, and stays there");
});

test("the direct/Inngest call organ rejects malformed or missing phone independently of consent", async () => {
  for (const phone of [null, undefined, "090-1234-5678", " +819012345678", 819012345678]) {
    clearEvents();
    const h = deps();
    await wakeCallOnce({ ...USER, phone, call_enabled: true }, DEPARTURE_MS - 5 * MINUTE, h.deps);
    assert.equal(h.dialed.length, 0, `phone=${String(phone)} must not claim/place a call`);
  }
});

// spec §5.2.1 / §5.3 — the phone is opt-IN now, not opt-out.
//
// The `!== false` idiom encodes "on unless refused", which is the exact opposite of what the spec
// now says: a user who has expressed no preference gets no call. Three shapes all mean "expressed no
// preference" and every one of them used to dial: no preference row at all, a row whose column is
// SQL NULL, and (before RUNTIME_DEFAULTS flips) a merged default of true. Only `=== true` refuses
// all three while still honouring the person who deliberately switched calls on.
test("a user who never asked for calls is not dialled; an explicit opt-in still is", async () => {
  const dialled = [];
  await wakeTick({
    listUsers: async () => [
      { uid: "opted-in", paid: true, phone: TEST_PHONE, daily_automation_enabled: true, call_enabled: true },
      { uid: "no-preference-row", daily_automation_enabled: true },
      { uid: "null-column", daily_automation_enabled: true, call_enabled: null },
      { uid: "opted-out", daily_automation_enabled: true, call_enabled: false },
    ],
    wake: async (u) => { dialled.push(u.uid); },
    now: 0,
  });
  assert.deepEqual(dialled, ["opted-in"],
    "silence is not consent to be phoned — §5.2.1 makes the phone an extra, and Telegram the default");
});

test("normal scheduled calls are a paid feature even when a free tenant opted in", async () => {
  const dialled = [];
  await wakeTick({
    listUsers: async () => [
      { uid: "free-opted-in", paid: false, phone: TEST_PHONE, daily_automation_enabled: true, call_enabled: true },
      { uid: "paid-opted-in", paid: true, phone: TEST_PHONE, daily_automation_enabled: true, call_enabled: true },
    ],
    wake: async (u) => { dialled.push(u.uid); },
    now: 0,
  });
  assert.deepEqual(dialled, ["paid-opted-in"]);
});

// The tick filter is not the only door. wakeUserOnce (the Inngest per-user path) calls wakeCallOnce
// directly and never passes through wakeTick's filter, so the dial half must refuse for itself.
test("the dial half itself refuses a user who never asked for calls", async () => {
  clearEvents();
  const h = deps();
  await wakeCallOnce({ ...USER, call_enabled: undefined }, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 0, "the Inngest per-user path bypasses wakeTick — this gate is the last one");

  const optedIn = deps();
  await wakeCallOnce({ ...USER, call_enabled: true }, DEPARTURE_MS - 5 * MINUTE, optedIn.deps);
  assert.equal(optedIn.dialed.length, 1, "and the person who switched calls on is still called");
});

// Task 4: the T-5 Telegram reminder is an organ, not part of the deadline-critical dial. Keep the
// fixtures injected so these tests exercise scheduler composition without touching Telegram,
// Supabase, or a real route provider.
function reminderUser(overrides = {}) {
  return {
    ...USER,
    uid: "iso-reminder",
    telegram_chat_id: "chat-iso",
    notifications_enabled: true,
    ...overrides,
  };
}

test("a due call and the reminder both run from one raw calendar fetch", async () => {
  clearEvents();
  const fetches = [];
  const h = deps({ fetches });
  const order = [];
  const seen = [];
  h.deps.travelReminder = async (u, nowMs, options) => {
    order.push("reminder");
    seen.push({ uid: u.uid, nowMs, events: options.events });
    return { status: "sent", telegramMessageId: 701 };
  };
  h.deps.lateNotice = async () => { order.push("late"); return null; };
  await wakeUserOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 1, "the due T-5 call is placed");
  assert.equal(fetches.length, 1, "wake and reminder reuse one raw calendar fetch");
  assert.equal(seen.length, 1, "wakeUserOnce invokes the reminder once");
  assert.equal(seen[0].events[0].id, EVENT.id, "the reminder receives the fetched event");
  assert.deepEqual(order, ["reminder", "late"], "later organs continue after the reminder");
});

test("a reminder throw or delay cannot suppress the call that ran first", async () => {
  clearEvents();
  const h = deps();
  let callAt = null;
  h.deps.placeCall = async () => { callAt = Date.now(); h.dialed.push(callAt); return { ok: true, ccid: "iso-call-first" }; };
  h.deps.travelReminder = async () => {
    await new Promise((resolve) => setTimeout(resolve, 40));
    throw new Error("reminder route failed");
  };
  await wakeCallOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps);
  await reminderUserOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 1, "the call survives the reminder failure");
  assert.ok(callAt, "the call completed before the reminder settled");
});

test("a call failure still invokes the reminder exactly once before later organs", async () => {
  clearEvents();
  const h = deps();
  let reminders = 0;
  h.deps.wakeCall = async () => { throw new Error("call path exploded"); };
  h.deps.travelReminder = async () => { reminders += 1; return { status: "suppressed" }; };
  await wakeUserOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(reminders, 1, "a call throw still reaches the reminder exactly once");
});

test("call-disabled users still receive the fixed reminder", async () => {
  clearEvents();
  const h = deps();
  let reminders = 0;
  h.deps.travelReminder = async () => { reminders += 1; return { status: "suppressed" }; };
  await reminderUserOnce(reminderUser({ call_enabled: false }), DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(reminders, 1, "call-disabled users still get Telegram organs");
});

test("notifications-disabled users get no reminder send or event-bearing reminder log", async () => {
  clearEvents();
  const h = deps();
  const logs = [];
  let reminders = 0;
  let sends = 0;
  h.deps.log = (line) => logs.push(String(line));
  h.deps.travelReminder = async () => { reminders += 1; return { status: "sent" }; };
  h.deps.sendMessage = async () => { sends += 1; return { ok: true, result: { message_id: 702 } }; };
  const consoleLines = [];
  const originalLog = console.log;
  const originalError = console.error;
  console.log = (...args) => consoleLines.push(args.join(" "));
  console.error = (...args) => consoleLines.push(args.join(" "));
  try {
    await reminderUserOnce(reminderUser({ call_enabled: true, notifications_enabled: false }), DEPARTURE_MS - 5 * MINUTE, h.deps);
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }
  assert.equal(reminders, 0, "the reminder gate is notifications_enabled");
  assert.equal(sends, 0, "notifications-off performs no Telegram send");
  assert.ok(logs.every((line) => !line.includes(EVENT.summary) && !line.includes(EVENT.location)),
    `event text must not leak into disabled-organ logs: ${JSON.stringify(logs)}`);
  assert.ok(consoleLines.every((line) => !line.includes(EVENT.summary) && !line.includes(EVENT.location)),
    `event text must not leak into scheduler console logs: ${JSON.stringify(consoleLines)}`);
});

test("the reminder receives the raw lookback event, including an online event already started", async () => {
  clearEvents();
  const h = deps();
  const now = EVENT_START_MS + 60 * 1000;
  const online = {
    ...EVENT,
    id: "iso-online-past",
    summary: "online private event",
    location: "https://meet.example/room",
    online: true,
    startMs: now - 60 * 1000,
    startIso: new Date(now - 60 * 1000).toISOString(),
  };
  h.deps.fetchUpcomingEvents = async () => [online];
  let received = null;
  h.deps.travelReminder = async (_u, _nowMs, options) => {
    received = options.events;
    return { status: "suppressed" };
  };
  await reminderUserOnce(reminderUser({ call_enabled: false }), now, h.deps);
  assert.equal(received.length, 1, "the raw event list reaches the reminder");
  assert.equal(received[0].online, true);
  assert.ok(received[0].startMs < now, "lookback event is not replaced by futureEvents");
});

test("a reminder hang in one tenant does not stop the next tenant", async () => {
  clearEvents();
  const h = deps();
  h.deps.reminderTimeoutMs = 30;
  let nextRan = 0;
  const users = [reminderUser({ uid: "iso-stuck" }), reminderUser({ uid: "iso-next" })];
  await reminderTick({
    listUsers: async () => users,
    now: Date.now(),
    reminder: (u, now, tickDeps) => reminderUserOnce(u, now, {
      ...tickDeps,
      travelReminder: async () => {
        if (u.uid === "iso-stuck") return new Promise(() => {});
        nextRan += 1;
        return { status: "suppressed" };
      },
    }),
    reminderTimeoutMs: 30,
  });
  assert.equal(nextRan, 1, "the next tenant is still served after the first reminder hangs");
});

test("a never-resolving call is bounded and wakeUserOnce runs the reminder exactly once", async () => {
  clearEvents();
  const h = deps();
  let callStarted = false;
  let reminderRan = 0;
  h.deps.wakeTimeoutMs = 30;
  h.deps.wakeCall = async () => { callStarted = true; return new Promise(() => {}); };
  h.deps.travelReminder = async () => { reminderRan += 1; return { status: "suppressed" }; };
  const sentinel = Symbol("wake-timeout");
  const result = await Promise.race([
    wakeUserOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps),
    new Promise((resolve) => setTimeout(() => resolve(sentinel), 100)),
  ]);
  assert.notStrictEqual(result, sentinel, "wakeUserOnce must not await a hung call forever");
  assert.equal(callStarted, true, "the call path remains first");
  assert.equal(reminderRan, 1, "a call timeout still reaches the reminder exactly once");
});

test("a never-resolving reminder is bounded and the late organ still runs", async () => {
  clearEvents();
  const h = deps();
  let lateRan = 0;
  h.deps.reminderTimeoutMs = 30;
  h.deps.travelReminder = async () => new Promise(() => {});
  h.deps.lateNotice = async () => { lateRan += 1; return null; };
  const sentinel = Symbol("reminder-timeout");
  const result = await Promise.race([
    reminderUserOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps),
    new Promise((resolve) => setTimeout(() => resolve(sentinel), 100)),
  ]);
  assert.notStrictEqual(result, sentinel, "a hung reminder must not hold this user's organ tick");
  await organsUserOnce(reminderUser(), DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.equal(lateRan, 1, "the late organ runs independently after reminder timeout");
});

test("the scheduler leaves one canonical travel-reminder receipt with hash/provider/message id", async () => {
  clearEvents();
  const h = deps();
  const now = EVENT_START_MS - 5 * MINUTE;
  const online = {
    ...EVENT,
    id: "iso-receipt",
    location: "https://meet.example/room",
    online: true,
    startMs: now + 5 * MINUTE,
    startIso: new Date(now + 5 * MINUTE).toISOString(),
  };
  const lines = [];
  h.deps.getEvents = () => [online];
  h.deps.liveLocation = null;
  h.deps.telegramToken = "token";
  h.deps.supaUrl = "supa";
  h.deps.supaKey = "key";
  h.deps.claimTravel = async () => true;
  h.deps.sendMessage = async () => ({ ok: true, result: { message_id: 703 } });
  const receiptArgs = [];
  h.deps.recordTravelTelegramReceipt = async (...args) => {
    receiptArgs.push(args);
    return { ok: true, matched: 1 };
  };
  h.deps.travelReminder = (user, reminderNow, forwarded) => travelReminderOnce(user, reminderNow, {
    ...forwarded,
    recordTravelTelegramReceipt: h.deps.recordTravelTelegramReceipt,
  });
  h.deps.log = (line) => lines.push(String(line));
  await reminderUserOnce(reminderUser(), now, h.deps);
  assert.equal(receiptArgs.length, 1);
  assert.deepEqual(receiptArgs[0].slice(0, 6), ["iso-reminder", "iso-receipt", "telegram-t5", 703, "supa", "key"]);
  const receipts = lines.filter((line) => line.startsWith("[travel-reminder] "));
  assert.equal(receipts.length, 1, `one canonical receipt expected, got ${JSON.stringify(receipts)}`);
  assert.match(receipts[0], /event_key_hash=[0-9a-f]{64}/);
  assert.match(receipts[0], /provider=none/);
  assert.match(receipts[0], /tg_message_id=703/);
});

test("the fixed reminder tick serves notification tenants concurrently and ignores phone opt-in", async () => {
  const started = [];
  const released = [];
  const users = [
    reminderUser({ uid: "iso-reminder-slow", call_enabled: false, daily_automation_enabled: true }),
    reminderUser({ uid: "iso-reminder-fast", call_enabled: false, daily_automation_enabled: true }),
    reminderUser({ uid: "iso-reminder-off", call_enabled: true, daily_automation_enabled: false }),
    reminderUser({ uid: "iso-reminder-muted", call_enabled: true, notifications_enabled: false }),
  ];
  const result = reminderTick({
    listUsers: async () => users,
    now: 0,
    reminder: async (u) => {
      started.push(u.uid);
      if (u.uid.endsWith("slow")) await new Promise((resolve) => setTimeout(resolve, 30));
      released.push(u.uid);
    },
    reminderTimeoutMs: 100,
  });
  await result;
  assert.deepEqual(started.sort(), ["iso-reminder-fast", "iso-reminder-slow"]);
  assert.ok(released.includes("iso-reminder-fast"), "a fast tenant is not held behind a slow tenant");
});

test("the degradable organ tick no longer owns the travel reminder", async () => {
  clearEvents();
  const h = deps();
  let reminders = 0;
  h.deps.getEvents = () => [];
  h.deps.travelReminder = async () => { reminders += 1; return { status: "sent" }; };
  h.deps.mental = async () => null;
  h.deps.care = async () => null;
  h.deps.diet = async () => null;
  h.deps.dietNudge = async () => null;
  h.deps.preceptsMirror = async () => null;
  h.deps.precepts = async () => null;
  h.deps.relations = async () => null;
  await organsUserOnce(reminderUser(), Date.now(), h.deps);
  assert.equal(reminders, 0, "the reminder has one fixed-tick owner, not the degradable organ tick");
});

test("the reminder loop reserves the next 60s start before a slow tick and close prevents future work", async () => {
  const previousSupaUrl = process.env.SUPABASE_URL;
  const previousSupaKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  process.env.SUPABASE_URL = "";
  process.env.SUPABASE_SERVICE_ROLE_KEY = "";
  const scheduled = [];
  const cleared = [];
  let starts = 0;
  let resolveCurrent;
  let loop;
  try {
    loop = startReminderLoop({
      reminderTick: async () => {
        starts += 1;
        await new Promise((resolve) => { resolveCurrent = resolve; });
      },
      setTimeout: (fn, ms) => {
        const handle = { fn, ms };
        scheduled.push(handle);
        return handle;
      },
      clearTimeout: (handle) => cleared.push(handle),
    });
    assert.equal(starts, 1, "the current tick starts immediately");
    assert.equal(scheduled.length, 1, "the next start is reserved while the current tick is unresolved");
    assert.equal(scheduled[0].ms, 60_000, "the reserved boundary is fixed at 60 seconds");

    loop.close();
    assert.deepEqual(cleared, [scheduled[0]], "close clears the reserved future timer");
    scheduled[0].fn();
    await Promise.resolve();
    assert.equal(starts, 1, "a callback racing with close cannot start future work");
  } finally {
    if (loop) loop.close();
    if (resolveCurrent) resolveCurrent();
    if (previousSupaUrl === undefined) delete process.env.SUPABASE_URL;
    else process.env.SUPABASE_URL = previousSupaUrl;
    if (previousSupaKey === undefined) delete process.env.SUPABASE_SERVICE_ROLE_KEY;
    else process.env.SUPABASE_SERVICE_ROLE_KEY = previousSupaKey;
  }
});
