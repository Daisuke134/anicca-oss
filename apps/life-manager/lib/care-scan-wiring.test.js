// care-scan-wiring.test.js — 11a/11b runtime wiring: care-detector + the 11b chain existed, were
// tested, and were called by NOTHING in production (the same unreachable-rule disease 12c had).
// These tests pin the cure: the 60s tick itself runs the PHYSICAL scan for every user, and a care
// failure is isolated exactly like MENTAL — it can never break the late/mental/wake paths.
// Run: node --test lib/care-scan-wiring.test.js
"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
process.env.SUPABASE_URL = process.env.SUPABASE_URL || "https://db.example";
process.env.SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "service";
const scheduler = require("../scheduler.js");

const NOW = Date.parse("2026-07-26T00:00:00Z");
const USER = {
  uid: "u-care-wire", telegram_chat_id: "1", phone: "+819012345678", home_address: "東京都新宿区",
  wake_policy: "all-events", call_enabled: true, notifications_enabled: true, paid: true,
};
// starts in exactly the T-10 window (no location → resolveDeparture returns event start)
const WAKEABLE = { summary: "stand-up", location: null, startMs: NOW + 10 * 60000, endMs: NOW + 40 * 60000, startIso: "s", endIso: "e" };

function deps(overrides = {}) {
  return {
    recordDailyPoll: async () => true,
    fetchUpcomingEvents: async () => [WAKEABLE],
    lateNotice: async () => null,
    mental: async () => null,
    care: async () => ({ status: "abstained" }),
    placeCall: async () => ({ ok: true }),
    claimWake: async () => false, // observed, then declined — no dial in tests
    ...overrides,
  };
}

test("the 60s tick runs the PHYSICAL care scan for the user", async () => {
  let seen = null;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    care: async (u, nowMs) => { seen = { uid: u.uid, nowMs }; return { status: "abstained" }; },
  }));
  assert.ok(seen, "careUserOnce was called by the tick");
  assert.equal(seen.uid, USER.uid);
  assert.equal(seen.nowMs, NOW);
});

test("a care throw is isolated: late, mental, and wake claims all still run", async () => {
  let lateRan = 0; let mentalRan = 0; const wakeClaims = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    lateNotice: async () => { lateRan += 1; return null; },
    mental: async () => { mentalRan += 1; return null; },
    care: async () => { throw new Error("supabase down"); },
    claimWake: async (_uid, key) => { wakeClaims.push(key); return false; },
  }));
  assert.equal(lateRan, 1, "late notice survived the care throw");
  assert.equal(mentalRan, 1, "mental survived the care throw");
  assert.ok(wakeClaims.length > 0, "the wake path still reached its claim after the care throw");
});

test("a mental throw does not stop the care scan (isolation is mutual)", async () => {
  let careRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    mental: async () => { throw new Error("mental exploded"); },
    care: async () => { careRan += 1; return { status: "abstained" }; },
  }));
  assert.equal(careRan, 1);
});

// 🟡 Finding 5: care ran BEFORE the wake evaluation, so a slow Places/Supabase chain could delay a
// wake dial past its ~2-min catch window. The wake loop is the time-critical path — care rides last.
test("care runs AFTER the wake evaluation — a slow care chain can never delay a wake call", async () => {
  const order = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    claimWake: async (_uid, key) => { order.push(`wake:${key}`); return false; },
    care: async () => { order.push("care"); return { status: "abstained" }; },
  }));
  const wakeIdx = order.findIndex((x) => x.startsWith("wake:"));
  const careIdx = order.indexOf("care");
  assert.ok(wakeIdx >= 0, "the wake claim fired on this tick");
  assert.ok(careIdx >= 0, "the care scan fired on this tick");
  assert.ok(wakeIdx < careIdx, `wake claim must fire before care's work, got order=${JSON.stringify(order)}`);
});

test("care still runs for a call-disabled user — skipping the wake loop must not skip the PHYSICAL organ", async () => {
  let careRan = 0;
  await scheduler.wakeUserOnce({ ...USER, call_enabled: false }, NOW, deps({
    claimWake: async () => { throw new Error("wake loop must be skipped for call-disabled users"); },
    care: async () => { careRan += 1; return { status: "abstained" }; },
  }));
  assert.equal(careRan, 1);
});

test("production default wires the real careUserOnce (not a stub that can silently vanish)", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const src = fs.readFileSync(path.join(__dirname, "../scheduler.js"), "utf8");
  assert.match(src, /require\("\.\/lib\/care-daily-runtime\.js"\)/, "scheduler must import the care runtime");
  assert.match(src, /deps\.care \|\| careUserOnce/, "the tick must default to the real careUserOnce");
});
