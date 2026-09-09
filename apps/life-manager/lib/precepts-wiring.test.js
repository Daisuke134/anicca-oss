"use strict";
// H4 ORG-precepts — the wiring. The 12c lesson, applied a fourth time: a finished module nobody calls
// changes nothing in production. These tests pin that the 60s tick really runs the precepts organ,
// that it runs LAST, that it is isolated in both directions, that the mirror and the question can
// never land in the same tick, and that LM_PRECEPTS_ENABLED is a real off switch.
// Run: node --test lib/precepts-wiring.test.js

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
process.env.SUPABASE_URL = process.env.SUPABASE_URL || "https://db.example";
process.env.SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "service";
const scheduler = require("../scheduler.js");

const NOW = Date.parse("2026-07-27T14:10:00Z"); // 23:10 JST
const USER = {
  uid: "u-precepts-wire", telegram_chat_id: "1", phone: "+819012345678", home_address: "東京都新宿区",
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
    precepts: async () => ({ status: "suppressed", reason: "outside-bedtime-window" }),
    preceptsMirror: async () => ({ status: "suppressed", reason: "not-sunday" }),
    placeCall: async () => ({ ok: true }),
    claimWake: async () => false,
    ...overrides,
  };
}

test("the 60s tick runs the precepts question and the weekly mirror for the user", async () => {
  const seen = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    precepts: async (u, nowMs) => { seen.push({ leg: "question", uid: u.uid, nowMs }); return { status: "suppressed" }; },
    preceptsMirror: async (u, nowMs) => { seen.push({ leg: "mirror", uid: u.uid, nowMs }); return { status: "suppressed" }; },
  }));
  assert.equal(seen.length, 2, `both precepts legs must run, got ${JSON.stringify(seen)}`);
  assert.ok(seen.every((s) => s.uid === USER.uid && s.nowMs === NOW));
});

test("the mirror is evaluated BEFORE the question", async () => {
  const order = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    precepts: async () => { order.push("question"); return { status: "suppressed" }; },
    preceptsMirror: async () => { order.push("mirror"); return { status: "suppressed" }; },
  }));
  assert.deepEqual(order, ["mirror", "question"]);
});

test("a mirror and a question never land in the same tick — the question waits for the next one", async () => {
  // Both legs read their MENTAL budget before either wrote, so the 2h spacing cannot separate them
  // WITHIN a tick. Two unsolicited messages zero seconds apart is the sermon the thresholds exist
  // to prevent, delivered in one breath.
  let questionRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    preceptsMirror: async () => ({ status: "mirrored", day: "2026-07-26", answerCount: 3, pattern: "weekday", telegramMessageId: 9 }),
    precepts: async () => { questionRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(questionRan, 0, "the question must be deferred when a mirror already went out this tick");
  await scheduler.wakeUserOnce(USER, NOW + 60000, deps({
    precepts: async () => { questionRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(questionRan, 1, "the deferral costs 60 seconds, not the night");
});

test("a mirror that was SUPPRESSED or FAILED does not cost the question its tick", async () => {
  for (const outcome of [{ status: "suppressed", reason: "not-sunday" }, { status: "send_failed" }, { status: "skipped" }]) {
    let questionRan = 0;
    await scheduler.wakeUserOnce(USER, NOW, deps({
      preceptsMirror: async () => outcome,
      precepts: async () => { questionRan += 1; return { status: "suppressed" }; },
    }));
    assert.equal(questionRan, 1, `only a DELIVERED mirror defers the question, got ${JSON.stringify(outcome)}`);
  }
});

test("a precepts throw is isolated: late, mental, care, diet and the wake claim all still run", async () => {
  let lateRan = 0; let mentalRan = 0; let careRan = 0; let dietRan = 0; const wakeClaims = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    lateNotice: async () => { lateRan += 1; return null; },
    mental: async () => { mentalRan += 1; return null; },
    care: async () => { careRan += 1; return { status: "abstained" }; },
    diet: async () => { dietRan += 1; return { status: "suppressed" }; },
    precepts: async () => { throw new Error("supabase down"); },
    preceptsMirror: async () => { throw new Error("calendar down"); },
    claimWake: async (_uid, key) => { wakeClaims.push(key); return false; },
  }));
  assert.equal(lateRan, 1);
  assert.equal(mentalRan, 1);
  assert.equal(careRan, 1);
  assert.equal(dietRan, 1);
  assert.ok(wakeClaims.length > 0, "the wake path still reached its claim after the precepts throws");
});

test("the two precepts legs are isolated from EACH OTHER — a mirror throw must not eat the question", async () => {
  let questionRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    preceptsMirror: async () => { throw new Error("calendar exploded"); },
    precepts: async () => { questionRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(questionRan, 1);
});

test("a diet throw does not stop the precepts organ (isolation is mutual)", async () => {
  let preceptsRan = 0;
  await scheduler.wakeUserOnce(USER, NOW, deps({
    diet: async () => { throw new Error("diet exploded"); },
    dietNudge: async () => { throw new Error("places exploded"); },
    precepts: async () => { preceptsRan += 1; return { status: "suppressed" }; },
  }));
  assert.equal(preceptsRan, 1);
});

test("precepts runs AFTER the wake evaluation, care and diet — nothing may delay a wake call", async () => {
  const order = [];
  await scheduler.wakeUserOnce(USER, NOW, deps({
    claimWake: async (_uid, key) => { order.push(`wake:${key}`); return false; },
    care: async () => { order.push("care"); return { status: "abstained" }; },
    dietNudge: async () => { order.push("diet"); return { status: "suppressed" }; },
    preceptsMirror: async () => { order.push("precepts"); return { status: "suppressed" }; },
  }));
  const wakeIdx = order.findIndex((x) => x.startsWith("wake:"));
  assert.ok(wakeIdx >= 0 && wakeIdx < order.indexOf("care"), `wake first, got ${JSON.stringify(order)}`);
  assert.ok(order.indexOf("care") < order.indexOf("diet"), `care before diet, got ${JSON.stringify(order)}`);
  assert.ok(order.indexOf("diet") < order.indexOf("precepts"), `diet before precepts, got ${JSON.stringify(order)}`);
});

test("precepts still runs for a call-disabled user — skipping the wake loop must not skip the organ", async () => {
  let ran = 0;
  await scheduler.wakeUserOnce({ ...USER, call_enabled: false }, NOW, deps({
    claimWake: async () => { throw new Error("wake loop must be skipped for call-disabled users"); },
    precepts: async () => { ran += 1; return { status: "suppressed" }; },
  }));
  assert.equal(ran, 1);
});

test("the kill switch answers to 0/false/off/no in any case", async () => {
  const previous = process.env.LM_PRECEPTS_ENABLED;
  try {
    for (const value of ["0", "false", "FALSE", "off", "Off", "no", "NO", " off "]) {
      process.env.LM_PRECEPTS_ENABLED = value;
      let ran = 0;
      await scheduler.wakeUserOnce(USER, NOW, deps({
        precepts: async () => { ran += 1; return { status: "suppressed" }; },
        preceptsMirror: async () => { ran += 1; return { status: "suppressed" }; },
      }));
      assert.equal(ran, 0, `LM_PRECEPTS_ENABLED=${JSON.stringify(value)} must turn the organ off`);
    }
    for (const value of ["1", "true", "on", ""]) {
      process.env.LM_PRECEPTS_ENABLED = value;
      let ran = 0;
      await scheduler.wakeUserOnce(USER, NOW, deps({
        precepts: async () => { ran += 1; return { status: "suppressed" }; },
        preceptsMirror: async () => { ran += 1; return { status: "suppressed" }; },
      }));
      assert.equal(ran, 2, `LM_PRECEPTS_ENABLED=${JSON.stringify(value)} is not an off switch`);
    }
  } finally {
    if (previous === undefined) delete process.env.LM_PRECEPTS_ENABLED; else process.env.LM_PRECEPTS_ENABLED = previous;
  }
});

test("the gate defaults ON — an unset env var must not silently disable the organ", async () => {
  const previous = process.env.LM_PRECEPTS_ENABLED;
  delete process.env.LM_PRECEPTS_ENABLED;
  try {
    let ran = 0;
    await scheduler.wakeUserOnce(USER, NOW, deps({ precepts: async () => { ran += 1; return { status: "suppressed" }; } }));
    assert.equal(ran, 1);
  } finally {
    if (previous !== undefined) process.env.LM_PRECEPTS_ENABLED = previous;
  }
});

test("the two organ gates are independent — turning diet off must not turn precepts off", async () => {
  const before = { diet: process.env.LM_DIET_ENABLED, precepts: process.env.LM_PRECEPTS_ENABLED };
  try {
    process.env.LM_DIET_ENABLED = "0";
    delete process.env.LM_PRECEPTS_ENABLED;
    let dietRan = 0; let preceptsRan = 0;
    await scheduler.wakeUserOnce(USER, NOW, deps({
      diet: async () => { dietRan += 1; return { status: "suppressed" }; },
      dietNudge: async () => { dietRan += 1; return { status: "suppressed" }; },
      precepts: async () => { preceptsRan += 1; return { status: "suppressed" }; },
      preceptsMirror: async () => { preceptsRan += 1; return { status: "suppressed" }; },
    }));
    assert.equal(dietRan, 0);
    assert.equal(preceptsRan, 2);
  } finally {
    for (const [key, value] of [["LM_DIET_ENABLED", before.diet], ["LM_PRECEPTS_ENABLED", before.precepts]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test("a notifications-off user never gets a precepts message, like every other organ", async () => {
  let ran = 0;
  await scheduler.wakeUserOnce({ ...USER, notifications_enabled: false }, NOW, deps({
    precepts: async () => { ran += 1; return { status: "suppressed" }; },
    preceptsMirror: async () => { ran += 1; return { status: "suppressed" }; },
  }));
  assert.equal(ran, 0);
});

test("production default wires the real modules (not a stub that can silently vanish)", () => {
  const src = fs.readFileSync(path.join(__dirname, "../scheduler.js"), "utf8");
  assert.match(src, /require\("\.\/lib\/precepts-runtime\.js"\)/, "scheduler must import the precepts runtime");
  assert.match(src, /require\("\.\/lib\/precepts-mirror\.js"\)/, "scheduler must import the precepts mirror");
  assert.match(src, /deps\.precepts \|\| preceptsUserOnce/);
  assert.match(src, /deps\.preceptsMirror \|\| preceptsMirrorOnce/);
  // The shared MENTAL budget lives in one module now; a scheduler-local copy would be a second budget.
  assert.match(src, /require\("\.\/lib\/mental-send-log\.js"\)/);
  assert.doesNotMatch(src, /async function readMentalSendState\(/,
    "the budget reader must not be re-declared inside the scheduler");
});

test("the webhook router knows the precepts prefix", () => {
  const { routeCallbackData } = require("./telegram.js");
  return Promise.all([
    routeCallbackData("precepts:answer:harsh:2026-07-27:9", { precepts: async () => ({ handled: true }) })
      .then((r) => assert.deepEqual(r, { handled: true })),
    // An unrouted prefix must not fall through into another organ's handler.
    routeCallbackData("precepts:answer:harsh:2026-07-27:9", { diet: async () => ({ handled: true }) }, () => {})
      .then((r) => assert.deepEqual(r, { ignored: true })),
  ]);
});

test("the scheduler's own log line says whether the shared cap actually recorded the send", async () => {
  // budgeted:false means the message went out and lm_mental_send_log never saw it. If that only
  // lives in a returned object nobody prints, the cap drifts in silence.
  const lines = [];
  const originalLog = console.log;
  console.log = (line) => lines.push(String(line));
  try {
    await scheduler.wakeUserOnce(USER, NOW, deps({
      precepts: async () => ({ status: "asked", day: "2026-07-27", budgeted: false, telegramMessageId: 7 }),
    }));
    await scheduler.wakeUserOnce(USER, NOW, deps({
      preceptsMirror: async () => ({
        status: "mirrored", day: "2026-07-26", answerCount: 3, pattern: "weekday",
        telegramMessageId: 9, budgeted: false,
      }),
    }));
  } finally {
    console.log = originalLog;
  }
  const question = lines.find((l) => l.startsWith("[precepts] "));
  const mirror = lines.find((l) => l.startsWith("[precepts-mirror] "));
  assert.ok(question, `the question outcome must be logged, got ${JSON.stringify(lines)}`);
  assert.match(question, /budgeted=false/);
  assert.ok(mirror, `the mirror outcome must be logged, got ${JSON.stringify(lines)}`);
  assert.match(mirror, /budgeted=false/);
});

test("server.js really mounts the precepts callback handler", () => {
  const src = fs.readFileSync(path.join(__dirname, "../server.js"), "utf8");
  assert.match(src, /require\("\.\/lib\/precepts-runtime\.js"\)/);
  assert.match(src, /precepts: async \(data\) => \{/);
  assert.match(src, /handlePreceptsCallback\(data, \{/);
});
