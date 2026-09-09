"use strict";
// H2 ORG-diet — the wiring. The 12c lesson, applied a third time: a finished module nobody calls
// changes nothing in production. These tests pin that the 60s tick really runs the diet organ, that
// it runs LAST (after the time-critical wake dial and after care), that it is isolated in both
// directions, and that the LM_DIET_ENABLED gate is a real off switch.
// Run: node --test lib/diet-scan-wiring.test.js

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
process.env.SUPABASE_URL = process.env.SUPABASE_URL || "https://db.example";
process.env.SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "service";
const scheduler = require("../scheduler.js");

const NOW = Date.parse("2026-07-27T03:00:00Z");
const USER = {
  uid: "u-diet-wire", telegram_chat_id: "1", phone: "+819012345678", home_address: "東京都新宿区",
  wake_policy: "all-events", call_enabled: true, notifications_enabled: true, paid: true,
};
const WAKEABLE = { summary: "stand-up", location: null, startMs: NOW + 10 * 60000, endMs: NOW + 40 * 60000, startIso: "s", endIso: "e" };

function deps(overrides = {}) {
  return {
    recordDailyPoll: async () => true,
    fetchUpcomingEvents: async () => [WAKEABLE],
    lateNotice: async () => null,
    mental: async () => null,
    care: async () => ({ status: "abstained" }),
    diet: async () => ({ status: "suppressed", reason: "outside-lunch-window" }),
    dietNudge: async () => ({ status: "suppressed", reason: "outside-nudge-window" }),
    placeCall: async () => ({ ok: true }),
    claimWake: async () => false,
    ...overrides,
  };
}

test("the 60s tick runs the diet question and the diet nudge for the user", async () => {
  const seen = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    diet: async (u, nowMs) => { seen.push({ leg: "question", uid: u.uid, nowMs }); return { status: "suppressed" }; },
    dietNudge: async (u, nowMs) => { seen.push({ leg: "nudge", uid: u.uid, nowMs }); return { status: "suppressed" }; },
  }));
  assert.equal(seen.length, 2, `both diet legs must run, got ${JSON.stringify(seen)}`);
  assert.ok(seen.every((s) => s.uid === USER.uid && s.nowMs === NOW));
});

test("the nudge is evaluated BEFORE the question — 11:15 must not be spent claiming the 11:30 ask", async () => {
  const order = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    diet: async () => { order.push("question"); return { status: "suppressed" }; },
    dietNudge: async () => { order.push("nudge"); return { status: "suppressed" }; },
  }));
  assert.deepEqual(order, ["nudge", "question"]);
});

test("a diet throw is isolated: late, mental, care, and the wake claim all still run", async () => {
  let lateRan = 0; let mentalRan = 0; let careRan = 0; const wakeClaims = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    lateNotice: async () => { lateRan += 1; return null; },
    mental: async () => { mentalRan += 1; return null; },
    care: async () => { careRan += 1; return { status: "abstained" }; },
    diet: async () => { throw new Error("supabase down"); },
    dietNudge: async () => { throw new Error("places down"); },
    claimWake: async (_uid, key) => { wakeClaims.push(key); return false; },
  }));
  assert.equal(lateRan, 1);
  assert.equal(mentalRan, 1);
  assert.equal(careRan, 1);
  assert.ok(wakeClaims.length > 0, "the wake path still reached its claim after the diet throws");
});

test("the two diet legs are isolated from EACH OTHER — a nudge throw must not eat the question", async () => {
  let questionRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    dietNudge: async () => { throw new Error("places exploded"); },
    diet: async () => { questionRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(questionRan, 1);
});

test("a care throw does not stop the diet organ (isolation is mutual)", async () => {
  let dietRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    care: async () => { throw new Error("care exploded"); },
    diet: async () => { dietRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(dietRan, 1);
});

test("diet runs AFTER the wake evaluation — a slow Places call can never delay a wake call", async () => {
  const order = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    claimWake: async (_uid, key) => { order.push(`wake:${key}`); return false; },
    care: async () => { order.push("care"); return { status: "abstained" }; },
    dietNudge: async () => { order.push("diet"); return { status: "suppressed" }; },
  }));
  const wakeIdx = order.findIndex((x) => x.startsWith("wake:"));
  assert.ok(wakeIdx >= 0 && wakeIdx < order.indexOf("care"), `wake first, got ${JSON.stringify(order)}`);
  assert.ok(order.indexOf("care") < order.indexOf("diet"), `care before diet, got ${JSON.stringify(order)}`);
});

test("diet still runs for a call-disabled user — skipping the wake loop must not skip the organ", async () => {
  let dietRan = 0;
  await scheduler.wakeUserOnce({ ...USER, call_enabled: false }, NOW, deps({
    claimWake: async () => { throw new Error("wake loop must be skipped for call-disabled users"); },
    diet: async () => { dietRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(dietRan, 1);
});

test("a nudge and a question never land in the same tick — the question waits for the next one", async () => {
  // Two unsolicited messages 0 seconds apart is exactly the sermon the thresholds exist to prevent.
  let questionRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    dietNudge: async () => ({ status: "nudged", day: "2026-07-27", sampleCount: 4, fastCount: 2, telegramMessageId: 9 }),
    diet: async () => { questionRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(questionRan, 0, "the question leg must be deferred when a nudge already went out this tick");
  // The very next tick, with no nudge, asks as usual — the deferral costs 60 seconds, not the day.
  await scheduler.wakeUserOnce(USER, NOW + 60000, deps({
    diet: async () => { questionRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(questionRan, 1);
});

test("a nudge that was SUPPRESSED or FAILED does not cost the question its tick", async () => {
  for (const nudgeOutcome of [{ status: "suppressed", reason: "nudge-cooldown" }, { status: "send_failed" }, { status: "skipped" }]) {
    let questionRan = 0;
    await scheduler.wakeUserOnce(USER, NOW, deps({
      dietNudge: async () => nudgeOutcome,
      diet: async () => { questionRan += 1; return { status: "suppressed" }; },
    }));
    assert.equal(questionRan, 1, `only a DELIVERED nudge defers the question, got ${JSON.stringify(nudgeOutcome)}`);
  }
});

test("the kill switch answers to 0/false/off/no in any case", async () => {
  const previous = process.env.LM_DIET_ENABLED;
  try {
    for (const value of ["0", "false", "FALSE", "off", "Off", "no", "NO", " off "]) {
      process.env.LM_DIET_ENABLED = value;
      let ran = 0;
      await scheduler.wakeUserOnce(USER, NOW, deps({
        diet: async () => { ran += 1; return { status: "suppressed" }; },
        dietNudge: async () => { ran += 1; return { status: "suppressed" }; },
      }));
      assert.equal(ran, 0, `LM_DIET_ENABLED=${JSON.stringify(value)} must turn the organ off`);
    }
    for (const value of ["1", "true", "on", ""]) {
      process.env.LM_DIET_ENABLED = value;
      let ran = 0;
      await scheduler.wakeUserOnce(USER, NOW, deps({
        diet: async () => { ran += 1; return { status: "suppressed" }; },
        dietNudge: async () => { ran += 1; return { status: "suppressed" }; },
      }));
      assert.equal(ran, 2, `LM_DIET_ENABLED=${JSON.stringify(value)} is not an off switch`);
    }
  } finally {
    if (previous === undefined) delete process.env.LM_DIET_ENABLED; else process.env.LM_DIET_ENABLED = previous;
  }
});

test("LM_DIET_ENABLED=0 is a real off switch: neither leg is called", async () => {
  const previous = process.env.LM_DIET_ENABLED;
  process.env.LM_DIET_ENABLED = "0";
  try {
    let ran = 0;
    await scheduler.wakeUserOnce(USER, NOW, deps({
      diet: async () => { ran += 1; return { status: "suppressed" }; },
      dietNudge: async () => { ran += 1; return { status: "suppressed" }; },
    }));
    assert.equal(ran, 0);
  } finally {
    if (previous === undefined) delete process.env.LM_DIET_ENABLED; else process.env.LM_DIET_ENABLED = previous;
  }
});

test("the gate defaults ON — an unset env var must not silently disable the organ", async () => {
  const previous = process.env.LM_DIET_ENABLED;
  delete process.env.LM_DIET_ENABLED;
  try {
    let ran = 0;
    await scheduler.wakeUserOnce(USER, NOW, deps({ diet: async () => { ran += 1; return { status: "suppressed" }; } }));
    assert.equal(ran, 1);
  } finally {
    if (previous !== undefined) process.env.LM_DIET_ENABLED = previous;
  }
});

test("a notifications-off user never gets a diet message, like the late/mental siblings", async () => {
  let ran = 0;
  await scheduler.wakeUserOnce({ ...USER, notifications_enabled: false }, NOW, deps({
    diet: async () => { ran += 1; return { status: "suppressed" }; },
    dietNudge: async () => { ran += 1; return { status: "suppressed" }; },
  }));
  assert.equal(ran, 0);
});

test("production default wires the real modules (not a stub that can silently vanish)", () => {
  const src = fs.readFileSync(path.join(__dirname, "../scheduler.js"), "utf8");
  assert.match(src, /require\("\.\/lib\/diet-runtime\.js"\)/, "scheduler must import the diet runtime");
  assert.match(src, /require\("\.\/lib\/diet-nudge\.js"\)/, "scheduler must import the diet nudge");
  assert.match(src, /deps\.diet \|\| dietUserOnce/);
  assert.match(src, /deps\.dietNudge \|\| dietNudgeOnce/);
});
