"use strict";
// The 60s tick is NOT periodic: the same tick runs the late/mental/care/diet organs under a per-user
// timeout, and a redeploy restarts the process — so a tick can land minutes after a wake threshold.
// A window-bound gate ("fire only inside T-min ±~2min") lost that call FOREVER. These tests pin the
// catch-up contract instead: a passed threshold still rings, at most ONE dial per event per tick, the
// superseded coarser level is CLAIMED so it cannot ring late, and past LATE_CUTOFF_MIN nobody is rung
// at all (that territory belongs to the late-notice organ).
// Run: node --test test/wake-catchup.test.js
const { test } = require("node:test");
const assert = require("node:assert");

process.env.LM_CALL_SECRET = "unit_secret";
process.env.PUBLIC_WSS = "wss://life-call.invalid";

const { wakeUserOnce, LATE_CUTOFF_MIN } = require("../scheduler.js");

const MINUTE = 60_000;
const EVENT_START_ISO = "2026-08-05T14:00:00+09:00";
const EVENT_START_MS = Date.parse(EVENT_START_ISO);
const TRAVEL_MIN = 35; // + the 5-min buffer resolveDeparture adds → departure = start − 40 min
const DEPARTURE_MS = EVENT_START_MS - 40 * MINUTE;
const TEST_PHONE = "+99900000000";

const USER = {
  paid: true,
  uid: "catchup-user",
  name: "Catchup User",
  phone: TEST_PHONE,
  home_address: "東京都渋谷区",
  call_language: "ja",
  daily_automation_enabled: true,
  call_enabled: true,
  notifications_enabled: false, // silences the late/diet/precepts/relations legs — this file is wakes only
};

const EVENT = {
  id: "catchup-event",
  summary: "新宿で打ち合わせ",
  location: "新宿",
  startMs: EVENT_START_MS,
  startIso: EVENT_START_ISO,
  endMs: EVENT_START_MS + 60 * MINUTE,
};

// Deps are injected exactly the way test/daily-journey-contract.test.js injects them: the real
// wakeUserOnce runs, only its I/O edges are controlled.
function harness({ dial } = {}) {
  const held = new Set();
  const claimed = [];
  const dialed = [];
  const released = [];
  const deps = {
    recordDailyPoll: async () => true,
    fetchUpcomingEvents: async () => [{ ...EVENT }],
    mental: async () => null,
    care: async () => ({ status: "already_scanned" }),
    mapsKey: "catchup-maps-key",
    directionsMinutes: async () => TRAVEL_MIN,
    claimWake: async (_uid, key) => {
      claimed.push(key);
      if (held.has(key)) return false;
      held.add(key);
      return true;
    },
    placeCall: async ({ streamUrl }) => {
      const params = new URL(streamUrl, "https://life-manager.invalid").searchParams;
      dialed.push({ urgency: params.get("urgency"), level: params.get("wakeEventKey").split("|").at(-1) });
      return dial ? dial(dialed.length) : { ok: true, ccid: `catchup-call-${dialed.length}` };
    },
    releaseWake: async (_uid, key) => { released.push(key); held.delete(key); },
    alertLowBalance: async () => {},
  };
  return { deps, held, claimed, dialed, released };
}

test("a tick 5 minutes AFTER the T-5 threshold still places exactly one call", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS, h.deps); // T-5 passed 5 minutes ago; nothing rang yet
  assert.deepEqual(h.dialed, [{ urgency: "harsh", level: "5" }], "the missed threshold still rings, once");
});

test("an on-time sequence still rings T-10 first and T-5 on a later tick", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS - 15 * MINUTE, h.deps);
  await wakeUserOnce(USER, DEPARTURE_MS - 10 * MINUTE, h.deps);
  await wakeUserOnce(USER, DEPARTURE_MS - 10 * MINUTE, h.deps); // a repeat tick must not re-ring
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.deepEqual(h.dialed, [
    { urgency: "firm", level: "10" },
    { urgency: "harsh", level: "5" },
  ], "two calls, in escalating order");
  assert.equal(h.held.size, 2, "exactly the two (event, level) claims are held");
});

test("one tick with both levels due places ONE call and claims the coarser level without ringing", async () => {
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS - 4 * MINUTE, h.deps); // T-10 passed, T-5 due, same tick
  assert.deepEqual(h.dialed, [{ urgency: "harsh", level: "5" }], "only the most urgent level rings");
  assert.deepEqual([...h.held].map((k) => k.split("|").at(-1)).sort(), ["10", "5"],
    "the superseded T-10 is claimed so a later tick cannot resurrect it");
  await wakeUserOnce(USER, DEPARTURE_MS - 3 * MINUTE, h.deps);
  assert.equal(h.dialed.length, 1, "the superseded level stays silent on every later tick");
});

test("a departure more than LATE_CUTOFF_MIN minutes past places no call at all", async () => {
  assert.equal(LATE_CUTOFF_MIN, -15, "the cutoff is the named constant, not a magic number");
  const h = harness();
  await wakeUserOnce(USER, DEPARTURE_MS + 16 * MINUTE, h.deps); // event itself is still in the future
  assert.deepEqual(h.dialed, [], "past the cutoff the late-notice organ owns this, not a wake call");
  assert.deepEqual(h.claimed, [], "and no claim is burned on the way out");
});

test("a dial failure releases its claim so the next tick retries", async () => {
  const h = harness({ dial: (n) => (n === 1 ? { ok: false, error: "balance too low" } : { ok: true, ccid: `retry-${n}` }) });
  await wakeUserOnce(USER, DEPARTURE_MS - 5 * MINUTE, h.deps);
  assert.deepEqual(h.released.map((k) => k.split("|").at(-1)), ["5"], "the failed level's claim is released");
  await wakeUserOnce(USER, DEPARTURE_MS - 4 * MINUTE, h.deps);
  assert.deepEqual(h.dialed, [
    { urgency: "harsh", level: "5" },
    { urgency: "harsh", level: "5" },
  ], "the next tick retries the same level");
});
