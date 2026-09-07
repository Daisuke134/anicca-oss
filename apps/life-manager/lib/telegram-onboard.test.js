// telegram-onboard.test.js — LM-6 minimal-question onboarding stage machine.
// Run: node --test apps/life-call/lib/telegram-onboard.test.js
"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const {
  computeStage, stageMessage, isNativeStage, normalizePhone, telegramProfileName,
  applyTelegramProfileName, handleOnboardingText, handleGmailCallback, onboardNudgeAll, backfillIfCalendarCompleted,
  linkedRows, completeTelegramHome,
  NUDGE_COOLDOWN_MS,
} = require("./telegram-onboard.js");
const { startReply, tgCall } = require("./telegram.js");

const full = {
  uid: "u1", telegram_chat_id: "1", name: "Dais", calendar_provider: "composio_gcal",
  phone: "+81", paid: true, home_address: "Tokyo home", notifications_enabled: true,
  gmail_account_id: "gmail-1", gmail_skipped: false,
};

test("null row → calendar (name is never a blocking typed stage)", () => assert.equal(computeStage(null), "calendar"));
test("no calendar → calendar even when name is absent", () => assert.equal(computeStage({ ...full, name: null, calendar_provider: null }), "calendar"));
test("canonical phone stage asks the optional phone question", () => assert.equal(computeStage({ ...full, phone: null, paid: false, tg_onboard_stage: "phone" }), "phone"));
test("saved phone advances only to explicit call consent", () => assert.equal(computeStage({ ...full, paid: false, tg_onboard_stage: "call" }), "call"));
test("paid without Gmail decision → done (Gmail is not a core prerequisite)", () => assert.equal(computeStage({ ...full, gmail_account_id: null, gmail_skipped: false }), "done"));
test("Gmail connected → done", () => assert.equal(computeStage(full), "done"));
test("Gmail skipped → done", () => assert.equal(computeStage({ ...full, gmail_account_id: null, gmail_skipped: true }), "done"));
test("server-owned done stage never regresses into optional phone or Gmail", () => {
  assert.equal(computeStage({ ...full, tg_onboard_stage: "done", phone: null, paid: true, gmail_account_id: null, gmail_skipped: false }), "done");
});
test("done rows never reopen onboarding when optional fields are absent", () => {
  const incomplete = { ...full, tg_onboard_stage: "done", paid: false, home_address: null, phone: null, gmail_account_id: null, gmail_skipped: false };
  assert.equal(computeStage(incomplete), "done");
  const coreReadyUnpaid = { ...full, tg_onboard_stage: "done", paid: false, phone: null, gmail_account_id: null, gmail_skipped: false };
  withCompUntil(past(), () => assert.equal(computeStage(coreReadyUnpaid), "done"));
  withCompUntil(future(), () => assert.equal(computeStage(coreReadyUnpaid), "done"));
  assert.equal(computeStage({ ...coreReadyUnpaid, notifications_enabled: false }), "done");
});
test("order is strict: canonical phone then call consent then done", () => {
  assert.equal(computeStage({ ...full, phone: null, paid: false, tg_onboard_stage: "phone" }), "phone");
  assert.equal(computeStage({ ...full, paid: false, tg_onboard_stage: "call" }), "call");
  assert.equal(computeStage({ ...full, phone: null, paid: false, tg_onboard_stage: "done" }), "done");
});

// ── Demo comp window (LM_COMP_UNTIL) ──────────────────────────────────────────
// A stranger who scans the demo QR must not hit a $20 wall mid-onboarding. The comp is a READ-TIME
// override with an expiry; lm_users.paid is never written, so Stripe stays the single writer.
function withCompUntil(value, fn) {
  const previous = process.env.LM_COMP_UNTIL;
  process.env.LM_COMP_UNTIL = value;
  try { return fn(); } finally {
    if (previous === undefined) delete process.env.LM_COMP_UNTIL; else process.env.LM_COMP_UNTIL = previous;
  }
}
// Async variant: a sync try/finally would restore the env before the awaited body ever runs.
async function withCompUntilAsync(value, fn) {
  const previous = process.env.LM_COMP_UNTIL;
  process.env.LM_COMP_UNTIL = value;
  try { return await fn(); } finally {
    if (previous === undefined) delete process.env.LM_COMP_UNTIL; else process.env.LM_COMP_UNTIL = previous;
  }
}
const future = () => new Date(Date.now() + 3600000).toISOString();
const past = () => new Date(Date.now() - 1000).toISOString();

test("legacy comp setting cannot reopen a paywall", () => {
  withCompUntil(future(), () => {
    assert.equal(computeStage({ ...full, paid: false }), "done");
    assert.equal(computeStage({ ...full, paid: false, gmail_account_id: null, gmail_skipped: false }), "done");
  });
});

test("legacy comp setting does not skip Calendar or canonical phone", () => {
  withCompUntil(future(), () => {
    assert.equal(computeStage({ ...full, paid: false, calendar_provider: null }), "calendar");
    assert.equal(computeStage({ ...full, paid: false, phone: null, tg_onboard_stage: "phone" }), "phone");
  });
});

test("expired or invalid comp never creates an onboarding pay gate", () => {
  withCompUntil(past(), () => assert.equal(computeStage({ ...full, paid: false }), "done"));
  withCompUntil("whenever", () => assert.equal(computeStage({ ...full, paid: false }), "done"));
  withCompUntil("", () => assert.equal(computeStage({ ...full, paid: false }), "done"));
});

test("injected comp time cannot change onboarding into a paywall", () => {
  const until = "2026-07-27T12:00:00.000Z";
  const env = { LM_COMP_UNTIL: until };
  assert.equal(computeStage({ ...full, paid: false }, { env, now: Date.parse(until) - 1 }), "done");
  assert.equal(computeStage({ ...full, paid: false }, { env, now: Date.parse(until) }), "done");
});

test("comp NEVER writes lm_users.paid — Stripe stays the single writer", async () => {
  const patches = [], stages = [];
  await withCompUntilAsync(future(), async () => {
    await onboardNudgeAll({
      token: "t", base: "https://x", supaUrl: "s", supaKey: "k", nudgeStore: new Map(),
      linkedRows: async () => [{ ...full, paid: false, gmail_account_id: null, gmail_skipped: true, tg_onboard_stage: "pay" }],
      sendStage: async () => {},
      saveField: async (_uid, patch) => patches.push(patch),
      setStage: async (_uid, stage) => stages.push(stage),
    });
  });
  assert.deepEqual(stages, []);
  for (const patch of patches) assert.equal(Object.prototype.hasOwnProperty.call(patch, "paid"), false);
});

test("telegram-onboard.js contains no write of the paid column", () => {
  const src = require("node:fs").readFileSync(require("node:path").join(__dirname, "telegram-onboard.js"), "utf8");
  assert.equal(/paid\s*:/.test(src), false, "the comp must stay a read-time override");
});

// ── Nudge discipline ──────────────────────────────────────────────────────────
// The loop ticks every 2 minutes over every linked row. Without a cooldown a user mid-web-flow gets
// re-prompted the moment a stage changes, and the loop ignored the notifications toggle its siblings
// (ask/discovery) already honour.
const nudgeRow = (over = {}) => ({ ...full, phone: null, paid: false, tg_onboard_stage: "calendar", ...over });

// ── Trial upgrade (durable, separate from onboarding stage drift) ────────────
const TRIAL_NOW = Date.parse("2026-08-31T12:00:00.000Z");
const expiredTrialRow = {
  ...full, uid: "trial-user", telegram_chat_id: "42", paid: false, tg_onboard_stage: "done",
  trial_expires_at: "2026-08-31T11:59:00.000Z",
};

function trialRun(over = {}, overrides = {}) {
  const order = [], sent = [], unclaims = [], errors = [];
  const row = { ...expiredTrialRow, ...over };
  const opts = {
    token: "t", base: "https://panel.example", supaUrl: "https://supa.example", supaKey: "k", now: TRIAL_NOW,
    nudgeStore: new Map(), linkedRows: async () => [row], sendStage: async () => { throw new Error("ordinary nudge must not send"); },
    setStage: async () => { throw new Error("trial upgrade must not rewrite stage"); },
    backfillCalendarContext: async () => {},
    claimTravel: async (...args) => { order.push(["claim", ...args]); return overrides.claimed !== false; },
    unclaimTravel: async (...args) => {
      order.push(["unclaim", ...args]);
      unclaims.push(args);
      return overrides.unclaimResult === undefined ? true : overrides.unclaimResult;
    },
    logError: (...args) => errors.push(args.join(" ")),
    paymentLink: overrides.paymentLink || (() => "https://buy.stripe.com/test_life_manager?client_reference_id=trial-user"),
    sendMessage: async (...args) => {
      order.push(["send", ...args]);
      const result = overrides.result === undefined ? { ok: true, result: { message_id: 901 } } : overrides.result;
      args.result = result.result;
      sent.push(args);
      return result;
    },
  };
  return { opts, order, sent, unclaims, errors };
}

test("legacy pay rows do not reopen ordinary pay nudges", () => {
  const base = nudgeRow({ tg_onboard_stage: "pay", paid: false, trial_expires_at: "2026-08-31T12:01:00.000Z" });
  assert.equal(computeStage(base, { now: TRIAL_NOW, env: {} }), "done");
  assert.equal(computeStage({ ...base, trial_expires_at: "2026-08-31T12:00:00.000Z" }, { now: TRIAL_NOW, env: {} }), "done");
});

test("linkedRows selects trial_expires_at with the existing user projection", async () => {
  const urls = [];
  const fetchImpl = async (url) => {
    urls.push(String(url));
    if (String(url).includes("lm_panel_preferences")) return { ok: true, json: async () => [] };
    return { ok: true, json: async () => [] };
  };
  await linkedRows("https://supa.example", "k", { fetchImpl });
  assert.match(urls[0], /select=[^&]*trial_expires_at/);
});

test("expired trial claims before send and keeps the claim after Telegram receipt", async () => {
  const h = trialRun();
  const count = await onboardNudgeAll(h.opts);
  assert.equal(count, 1);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "send"]);
  assert.equal(h.sent[0][1], "42");
  assert.match(h.sent[0][2], /^無料期間が終了しました。/);
  assert.match(h.sent[0][2], /<a href="https:\/\/buy\.stripe\.com\//);
  assert.equal(h.sent[0].result.message_id, 901);
  assert.equal(h.unclaims.length, 0);
});

test("exact expiry timestamp enters the upgrade branch and sends once", async () => {
  const h = trialRun({ trial_expires_at: new Date(TRIAL_NOW).toISOString() });
  assert.equal(await onboardNudgeAll(h.opts), 1);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "send"]);
  assert.equal(h.sent.length, 1);
  assert.match(h.sent[0][2], /<a href="https:\/\/buy\.stripe\.com\//);
  assert.equal(h.sent[0].result.message_id, 901);
  assert.equal(h.unclaims.length, 0);
});

test("trial upgrade send throw keeps the claim and replay sends zero", async () => {
  const h = trialRun();
  let attempts = 0;
  h.opts.claimTravel = async (...args) => {
    h.order.push(["claim", ...args]);
    attempts++;
    return attempts === 1;
  };
  h.opts.sendMessage = async (...args) => {
    h.order.push(["send", ...args]);
    h.sent.push(args);
    throw new Error("telegram unavailable");
  };
  assert.equal(await onboardNudgeAll(h.opts), 0);
  assert.equal(await onboardNudgeAll({ ...h.opts, nudgeStore: new Map() }), 0);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "send", "claim"]);
  assert.equal(h.unclaims.length, 0);
  assert.deepEqual(h.errors, ["[onboard] trial-upgrade reconciliation required"]);
});

test("trial upgrade delivery_unknown keeps the claim and does not resend", async () => {
  const h = trialRun({});
  let attempts = 0;
  h.opts.claimTravel = async (...args) => {
    h.order.push(["claim", ...args]);
    attempts++;
    return attempts === 1;
  };
  h.opts.sendMessage = async (...args) => {
    h.order.push(["send", ...args]);
    h.sent.push(args);
    return { ok: false, delivery_unknown: true };
  };
  assert.equal(await onboardNudgeAll(h.opts), 0);
  assert.equal(await onboardNudgeAll({ ...h.opts, nudgeStore: new Map() }), 0);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "send", "claim"]);
  assert.equal(h.unclaims.length, 0);
  assert.deepEqual(h.errors, ["[onboard] trial-upgrade reconciliation required"]);
});

test("trial upgrade ambiguous Telegram receipt keeps the claim and replay sends zero", async () => {
  const h = trialRun();
  let attempts = 0;
  h.opts.claimTravel = async (...args) => {
    h.order.push(["claim", ...args]);
    attempts++;
    return attempts === 1;
  };
  h.opts.sendMessage = async (...args) => {
    h.order.push(["send", ...args]);
    h.sent.push(args);
    return { error: "upstream reset" };
  };
  assert.equal(await onboardNudgeAll(h.opts), 0);
  assert.equal(await onboardNudgeAll({ ...h.opts, nudgeStore: new Map() }), 0);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "send", "claim"]);
  assert.equal(h.unclaims.length, 0);
  assert.deepEqual(h.errors, ["[onboard] trial-upgrade reconciliation required"]);
});

test("a duplicate trial claim sends zero additional messages", async () => {
  let attempts = 0;
  const h = trialRun();
  h.opts.claimTravel = async (...args) => {
    h.order.push(["claim", ...args]);
    attempts++;
    return attempts === 1;
  };
  assert.equal(await onboardNudgeAll(h.opts), 1);
  assert.equal(await onboardNudgeAll({ ...h.opts, nudgeStore: new Map() }), 0);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "send", "claim"]);
  assert.equal(h.sent.length, 1);
});

test("explicit rejection releases, while receipt-less success retains the claim", async () => {
  for (const result of [{ ok: false }, { ok: true, result: {} }]) {
    const h = trialRun({}, { result });
    assert.equal(await onboardNudgeAll(h.opts), 0);
    const explicitReject = result.ok === false;
    assert.deepEqual(h.order.map((entry) => entry[0]), explicitReject ? ["claim", "send", "unclaim"] : ["claim", "send"]);
    assert.equal(h.unclaims.length, explicitReject ? 1 : 0);
    assert.deepEqual(h.errors, explicitReject ? [] : ["[onboard] trial-upgrade reconciliation required"]);
    if (explicitReject) assert.deepEqual(h.unclaims[0].slice(0, 3), ["trial-user", expiredTrialRow.trial_expires_at, "trial-upgrade"]);
  }
});

test("only a positive integer Telegram message_id keeps the claim; ambiguous receipts retain it", async () => {
  for (const messageId of [-1, 0, true, {}, "901", 1.5, undefined]) {
    const h = trialRun({}, { result: { ok: true, result: { message_id: messageId } } });
    assert.equal(await onboardNudgeAll(h.opts), 0, `message_id=${String(messageId)}`);
    assert.equal(h.unclaims.length, 0, `message_id=${String(messageId)}`);
    assert.deepEqual(h.errors, ["[onboard] trial-upgrade reconciliation required"], `message_id=${String(messageId)}`);
  }
  const h = trialRun({}, { result: { ok: true, result: { message_id: 901 } } });
  assert.equal(await onboardNudgeAll(h.opts), 1);
  assert.equal(h.unclaims.length, 0);
});

test("verified release permits a later retry, while failed release stays claimed", async () => {
  const retry = trialRun();
  let retrySends = 0;
  retry.opts.sendMessage = async (...args) => {
    retry.order.push(["send", ...args]);
    retry.sent.push(args);
    retrySends++;
    return retrySends === 1 ? { ok: false } : { ok: true, result: { message_id: 902 } };
  };
  assert.equal(await onboardNudgeAll(retry.opts), 0);
  assert.equal(await onboardNudgeAll({ ...retry.opts, nudgeStore: new Map() }), 1);
  assert.equal(retry.sent.length, 2);
  assert.equal(retry.unclaims.length, 1);
  assert.deepEqual(retry.errors, []);

  let attempts = 0;
  const stuck = trialRun({}, { result: { ok: false }, unclaimResult: false });
  stuck.opts.claimTravel = async (...args) => {
    stuck.order.push(["claim", ...args]);
    attempts++;
    return attempts === 1;
  };
  assert.equal(await onboardNudgeAll(stuck.opts), 0);
  assert.equal(await onboardNudgeAll({ ...stuck.opts, nudgeStore: new Map() }), 0);
  assert.deepEqual(stuck.order.map((entry) => entry[0]), ["claim", "send", "unclaim", "claim"]);
  assert.equal(stuck.sent.length, 1);
  assert.deepEqual(stuck.errors, ["[onboard] trial-upgrade reconciliation required"]);
});

test("missing trusted checkout releases the claim without sending", async () => {
  const h = trialRun({}, { paymentLink: () => "" });
  assert.equal(await onboardNudgeAll(h.opts), 0);
  assert.deepEqual(h.order.map((entry) => entry[0]), ["claim", "unclaim"]);
  assert.equal(h.sent.length, 0);
});

test("active, paid, incomplete, notifications-off, and Telegram-unbound rows send no upgrade", async () => {
  const cases = [
    { name: "active", row: { trial_expires_at: "2026-08-31T12:01:00.000Z" } },
    { name: "paid", row: { paid: true } },
    { name: "incomplete", row: { home_address: null, phone: null, tg_onboard_stage: "phone" } },
    { name: "notifications-off", row: { notifications_enabled: false } },
    { name: "Telegram-unbound", row: { telegram_chat_id: null } },
  ];
  for (const item of cases) {
    const h = trialRun(item.row);
    assert.equal(await onboardNudgeAll(h.opts), 0, item.name);
    assert.equal(h.sent.length, 0, item.name);
    assert.equal(h.order.length, 0, item.name);
  }
});

test("core-ready legacy phone/pay rows remain done and do not emit optional-stage nudges", async () => {
  for (const row of [
    { ...full, paid: false, tg_onboard_stage: "calendar", phone: null, trial_expires_at: "2026-08-31T12:01:00.000Z" },
    { ...full, paid: false, tg_onboard_stage: "", phone: null, trial_expires_at: "2026-08-31T12:01:00.000Z" },
    { ...full, paid: false, tg_onboard_stage: "unknown-stage", phone: null, trial_expires_at: "2026-08-31T12:01:00.000Z" },
    { ...full, paid: false, tg_onboard_stage: "phone", phone: "+81", trial_expires_at: "2026-08-31T12:01:00.000Z" },
    { ...full, paid: false, tg_onboard_stage: "pay", phone: null, trial_expires_at: "2026-08-31T12:01:00.000Z" },
    { ...full, paid: false, tg_onboard_stage: "phone", phone: "+81", trial_expires_at: "2026-08-31T11:59:00.000Z" },
    { ...full, paid: false, tg_onboard_stage: "pay", phone: null, trial_expires_at: "2026-08-31T11:59:00.000Z" },
  ]) {
    assert.equal(computeStage(row, { now: TRIAL_NOW, env: {} }), "done");
    const stages = [];
    const messages = [];
    const expired = Date.parse(row.trial_expires_at) <= TRIAL_NOW;
    const h = trialRun(row);
    h.opts.sendStage = async (_token, _chat, stageRow) => stages.push(computeStage(stageRow));
    h.opts.sendMessage = async (...args) => { messages.push(args[2]); return { ok: true, result: { message_id: 903 } }; };
    assert.equal(await onboardNudgeAll({ ...h.opts, now: TRIAL_NOW }), expired ? 1 : 0);
    assert.deepEqual(stages, []);
    assert.equal(messages.some((text) => /Phone saved|Subscribe/i.test(text)), false);
  }
});

test("core-ready call stage asks for explicit opt-in and never falls through to pay", async () => {
  for (const [trialExpiresAt, expectedCount] of [
    ["2026-08-31T12:01:00.000Z", 0],
    ["2026-08-31T11:59:00.000Z", 1],
  ]) {
    const row = { ...full, uid: "call-stage", paid: false, phone: "+81", tg_onboard_stage: "call", trial_expires_at: trialExpiresAt };
    const h = trialRun(row);
    const ordinary = [];
    h.opts.sendStage = async (...args) => ordinary.push(args);
    assert.equal(computeStage(row, { now: TRIAL_NOW, env: {} }), "call");
    assert.equal(await onboardNudgeAll({ ...h.opts, now: TRIAL_NOW }), expectedCount);
    assert.equal(ordinary.length, 0);
    assert.equal(h.sent.some((args) => /Phone saved|Subscribe/i.test(args[2])), false);
  }
});

test("notifications_enabled=false gets nothing", async () => {
  const calls = [];
  const sent = await onboardNudgeAll({
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k", nudgeStore: new Map(),
    linkedRows: async () => [nudgeRow({ notifications_enabled: false })],
    sendStage: async () => calls.push("send"),
    setStage: async () => calls.push("stage"),
    backfillCalendarContext: async () => calls.push("context"),
  });
  assert.equal(sent, 0);
  assert.deepEqual(calls, []);
});

test("notifications_enabled true/undefined still nudges (direct row compatibility)", async () => {
  for (const value of [true, undefined]) {
    const sent = await onboardNudgeAll({
      token: "t", base: "https://x", supaUrl: "s", supaKey: "k", nudgeStore: new Map(),
      linkedRows: async () => [nudgeRow({ home_address: null, notifications_enabled: value })],
      sendStage: async () => {}, setStage: async () => {}, backfillCalendarContext: async () => {},
    });
    assert.equal(sent, 1, `notifications_enabled=${value}`);
  }
});

test("same-stage suppression still works (no cooldown entry is even created)", async () => {
  const store = new Map();
  const sent = await onboardNudgeAll({
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k", nudgeStore: store,
    linkedRows: async () => [nudgeRow({ tg_onboard_stage: "phone" })], // computeStage → phone
    sendStage: async () => { throw new Error("must not send"); },
    setStage: async () => { throw new Error("must not persist"); },
  });
  assert.equal(sent, 0);
  assert.equal(store.size, 0);
});

test("a stage CHANGE inside the 30-min cooldown still waits, then fires once elapsed", async () => {
  const store = new Map();
  const stages = [];
  const t0 = Date.parse("2026-07-27T00:00:00.000Z");
  const run = (row, now) => onboardNudgeAll({
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k", nudgeStore: store, now,
    linkedRows: async () => [row], sendStage: async () => {},
    setStage: async (_uid, stage) => stages.push(stage), backfillCalendarContext: async () => {},
  });
  assert.equal(await run(nudgeRow({ home_address: null, tg_onboard_stage: "calendar" }), t0), 1); // calendar → home
  assert.deepEqual(stages, ["home"]);
  // stage really changed (phone → pay) but only 2 minutes have passed → hold
  assert.equal(await run(nudgeRow({ home_address: null, phone: "+81", tg_onboard_stage: "phone" }), t0 + 2 * 60000), 0);
  assert.equal(await run(nudgeRow({ home_address: null, phone: "+81", tg_onboard_stage: "phone" }), t0 + NUDGE_COOLDOWN_MS - 1), 0);
  assert.deepEqual(stages, ["home"]);
  // cooldown elapsed → the pending change is finally announced
  assert.equal(await run(nudgeRow({ home_address: null, phone: "+81", tg_onboard_stage: "phone" }), t0 + NUDGE_COOLDOWN_MS), 1);
  assert.deepEqual(stages, ["home", "done"]);
});

test("the cooldown is 30 minutes and is per-uid, not global", async () => {
  assert.equal(NUDGE_COOLDOWN_MS, 30 * 60 * 1000);
  const store = new Map();
  const t0 = Date.parse("2026-07-27T00:00:00.000Z");
  const rows = [nudgeRow({ uid: "a", home_address: null }), nudgeRow({ uid: "b", home_address: null })];
  const sent = await onboardNudgeAll({
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k", nudgeStore: store, now: t0,
    linkedRows: async () => rows, sendStage: async () => {}, setStage: async () => {},
    backfillCalendarContext: async () => {},
  });
  assert.equal(sent, 2);
  assert.deepEqual([...store.keys()].sort(), ["a", "b"]);
});

test("linkedRows joins notifications_enabled from lm_panel_preferences in ONE batched query", async () => {
  const { linkedRows } = require("./telegram-onboard.js");
  const urls = [];
  const fetchImpl = async (url) => {
    urls.push(String(url));
    if (String(url).includes("lm_panel_preferences")) {
      return { ok: true, json: async () => [{ uid: "a", notifications_enabled: false }] };
    }
    return { ok: true, json: async () => [{ uid: "a" }, { uid: "b" }] };
  };
  const rows = await linkedRows("https://supa.test", "key", { fetchImpl });
  assert.equal(urls.length, 2, "one users query + one batched preferences query");
  assert.equal(rows.find(r => r.uid === "a").notifications_enabled, false);
  assert.equal(rows.find(r => r.uid === "b").notifications_enabled, false); // no preferences row → not proven enabled
});

test("Telegram /start stays in chat and exposes only Google consent", () => {
  const reply = startReply({ calendarUrl: "https://accounts.google.com/o/oauth2/auth?state=opaque", languageCode: "ja" });
  const buttons = reply.extra.reply_markup.inline_keyboard;
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].length, 1);
  const button = buttons[0][0];
  assert.equal(Object.hasOwn(button, "web_app"), false);
  const url = new URL(button.url);
  assert.equal(url.protocol, "https:");
  assert.equal(url.hostname, "accounts.google.com");
  assert.equal(url.searchParams.get("state"), "opaque");
  assert.equal(url.hash, "");
  assert.match(reply.text, /^👋 <b>ライフマネージャー<\/b>/);
  assert.match(reply.text, /乗換案内/);
  assert.doesNotMatch(reply.text, /料金|カード|Stripe|trial|プラン/i);
});

test("Telegram /start rejects unsafe or non-consent Calendar URLs", () => {
  for (const calendarUrl of [undefined, "", "http://accounts.google.com/x", "accounts.google.com", " https://accounts.google.com/x", "https://user:pass@accounts.google.com/x", "https://evil.example/x", "https://accounts.google.com.evil.example/x"]) {
    assert.throws(() => startReply({ calendarUrl }), /calendar URL is unavailable/);
  }
  assert.doesNotThrow(() => startReply({ calendarUrl: "https://connect.composio.dev/link/opaque" }));
});

test("Telegram /start localizes from Telegram language without another question", () => {
  const text = startReply({ calendarUrl: "https://accounts.google.com/x", languageCode: "en-US" }).text;
  assert.match(text, /^👋 <b>Life Manager<\/b>/);
  assert.doesNotMatch(text, /ライフマネージャー/);
});

test("Telegram transport failure returns a delivery_unknown marker without provider error text", async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => { throw new Error("provider-secret-detail"); };
  try {
    const result = await tgCall("token", "sendMessage", { chat_id: "42", text: "hello" });
    assert.equal(result.ok, false);
    assert.equal(result.delivery_unknown, true);
    assert.equal(Object.hasOwn(result, "error"), false);
  } finally {
    global.fetch = originalFetch;
  }
});

test("Telegram JSON without a boolean ok is delivery_unknown", async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ error: "upstream reset" }) });
  try {
    assert.deepEqual(await tgCall("token", "sendMessage", { chat_id: "42", text: "hello" }), { ok: false, delivery_unknown: true });
  } finally {
    global.fetch = originalFetch;
  }
});

test("unreadable Telegram JSON is delivery_unknown while explicit rejection stays definitive", async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: true, status: 200, json: async () => { throw new Error("secret-body"); } });
  try {
    const unknown = await tgCall("token", "sendMessage", { chat_id: "42", text: "hello" });
    assert.deepEqual(unknown, { ok: false, delivery_unknown: true });
  } finally {
    global.fetch = originalFetch;
  }
  global.fetch = async () => ({ ok: false, status: 400, json: async () => ({ ok: false, description: "rejected" }) });
  try {
    assert.deepEqual(await tgCall("token", "sendMessage", { chat_id: "42", text: "hello" }), { ok: false, description: "rejected" });
  } finally {
    global.fetch = originalFetch;
  }
});

test("telegramProfileName: derives name from first_name + last_name", () => {
  assert.equal(telegramProfileName({ first_name: " Dais ", last_name: " Tanaka " }), "Dais Tanaka");
  assert.equal(telegramProfileName({ first_name: "Dais" }), "Dais");
  assert.equal(telegramProfileName(null), "");
});
test("applyTelegramProfileName: fills missing name without overwriting an existing name", () => {
  assert.deepEqual(applyTelegramProfileName(null, { first_name: "Dais", last_name: "Tanaka" }), { name: "Dais Tanaka" });
  assert.equal(applyTelegramProfileName({ name: "Existing" }, { first_name: "Dais" }).name, "Existing");
  assert.equal(computeStage(applyTelegramProfileName(null, { first_name: "Dais" })), "calendar");
});

test("home, phone, and call choice are the NATIVE typed stages", () => {
  assert.ok(isNativeStage("home"));
  assert.ok(isNativeStage("phone"));
  assert.ok(isNativeStage("call"));
  for (const stage of ["name", "calendar", "pay", "gmail", "done"]) assert.ok(!isNativeStage(stage));
});

test("Calendar carries its web button; Gmail compatibility carries connect + skip buttons", () => {
  assert.equal(stageMessage("calendar", "9", "https://aniccaai.com").extra.reply_markup.inline_keyboard[0][0].url, "https://aniccaai.com/lm?tg=9");
  const buttons = stageMessage("gmail", "9", "https://aniccaai.com", "https://life.example/gmail-connect").extra.reply_markup.inline_keyboard[0];
  assert.equal(buttons[0].url, "https://life.example/gmail-connect");
  assert.equal(buttons[1].callback_data, "gmail:skip");
});

test("phone is optional, call needs explicit opt-in, and Gmail never claims connection", () => {
  assert.match(stageMessage("phone", "1", "x").text, /optional/i);
  assert.match(stageMessage("call", "1", "x").text, /yes.*skip/i);
  assert.match(stageMessage("gmail", "1", "x").text, /Gmail/i);
  assert.doesNotMatch(stageMessage("gmail", "1", "x").text, /connected!/i);
});

test("Gmail skip persists gmail_skipped=true and advances to done", async () => {
  const saved = [], stages = [], sent = [];
  const result = await handleGmailCallback("gmail:skip", full, {
    token: "t", chatId: "1", base: "https://x", saveField: async (_uid, patch) => saved.push(patch),
    setStage: async (_uid, stage) => stages.push(stage), sendMessage: async (_t, _c, text) => sent.push(text),
  });
  assert.deepEqual(result, { ok: true, stage: "done" });
  assert.deepEqual(saved, [{ gmail_skipped: true }]);
  assert.deepEqual(stages, ["done"]);
  assert.match(sent[0], /ready/i);
});

test("Gmail compatibility stage is not reopened by normal onboarding", async () => {
  await withCompUntilAsync(future(), async () => {
    const saved = [], stages = [], announced = [];
    // Keep this row outside the core-ready terminal guard: the optional Gmail fallback is
    // still exercised for legacy/incomplete rows, while a core-ready stored `gmail` stage is done.
    const row = { ...full, paid: false, home_address: null, gmail_account_id: null, gmail_skipped: false, tg_onboard_stage: "gmail" };
    const sent = await onboardNudgeAll({ token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
      nudgeStore: new Map(), // isolated per test: the real store is module-level and 30-min sticky
      linkedRows: async () => [row], mailAvailable: async () => false,
      saveField: async (_uid, patch) => saved.push(patch),
      sendStage: async (_token, _chat, stageRow) => announced.push(computeStage(stageRow)),
      setStage: async (_uid, stage) => stages.push(stage) });
    assert.equal(sent, 1);
    assert.deepEqual(saved, []);
    assert.deepEqual(stages, ["done"]);
    assert.deepEqual(announced, ["done"]);
  });
});

test("canonical done rows are not rewritten by the legacy onboarding nudge", async () => {
  const calls = [];
  const row = { ...full, tg_onboard_stage: "done", phone: null, paid: true };
  const sent = await onboardNudgeAll({ token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
    nudgeStore: new Map(), linkedRows: async () => [row], sendStage: async () => calls.push("send"),
    setStage: async () => calls.push("stage"), backfillCalendarContext: async () => calls.push("context") });
  assert.equal(sent, 0);
  assert.deepEqual(calls, []);
});

test("rowByChatId-shaped paid phone-less done rows are webhook no-ops without joined preferences", async () => {
  const row = { uid: "u-done", telegram_chat_id: "100", tg_onboard_stage: "done", calendar_provider: "composio_gcal", paid: true, phone: null, home_address: "Tokyo home" };
  const effects = [];
  const opts = {
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
    saveField: async () => effects.push("save"), setStage: async () => effects.push("stage"),
    sendMessage: async () => effects.push("send"), backfillCalendarContext: async () => effects.push("context"),
  };
  assert.equal(await handleOnboardingText("100", ["+81", "90", "1234", "5678"].join(""), row, opts), "done");
  assert.deepEqual(await handleGmailCallback("gmail:skip", row, { ...opts, chatId: "100" }), { ok: true, stage: "done" });
  assert.deepEqual(effects, []);
});

test("calendar completion triggers best-effort context backfill once before asking for home", async () => {
  const calls = [];
  const row = { ...full, home_address: null, phone: null, paid: false, tg_onboard_stage: "calendar" };
  const sent = await onboardNudgeAll({ token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
    nudgeStore: new Map(), // isolated per test: the real store is module-level and 30-min sticky
    linkedRows: async () => [row], sendStage: async () => calls.push("send"),
    setStage: async (_uid, stage) => calls.push(`stage:${stage}`),
    backfillCalendarContext: async (uid) => calls.push(`context:${uid}`),
  });
  assert.equal(sent, 1);
  assert.deepEqual(calls, ["context:u1", "send", "stage:home"]);
});

test("calendar completion asks for the exact home address", () => {
  assert.equal(computeStage({ ...full, home_address: null, tg_onboard_stage: "calendar" }), "home");
  const message = stageMessage("home", "1", "https://x");
  assert.match(message.text, /自宅の住所/);
  assert.doesNotMatch(message.text, /料金|カード|Stripe/);
});

test("home text is tenant-scoped and atomically enables notifications before advancing", async () => {
  const completions = [], messages = [];
  const row = { ...full, home_address: null, phone: null, paid: false, tg_onboard_stage: "calendar" };
  const result = await handleOnboardingText("1", " 東京都千代田区1-1 ", row, {
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
    backfillCalendarContext: async () => {},
    completeTelegramHome: async (...args) => completions.push(args),
    sendMessage: async (_token, _chat, text) => messages.push(text),
  });
  assert.equal(result, "home");
  assert.deepEqual(completions, [["u1", "1", "東京都千代田区1-1", "s", "k"]]);
  assert.equal(messages.length, 1);
});

test("blank or overlong home text performs no mutation", async () => {
  for (const input of [" ", "a".repeat(241)]) {
    const completions = [], messages = [];
    const row = { ...full, home_address: null, phone: null, paid: false, tg_onboard_stage: "calendar" };
    assert.equal(await handleOnboardingText("1", input, row, {
      token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
      backfillCalendarContext: async () => {},
      completeTelegramHome: async (...args) => completions.push(args),
      sendMessage: async (_token, _chat, text) => messages.push(text),
    }), "bad-home");
    assert.deepEqual(completions, []);
    assert.equal(messages.length, 1);
  }
});

test("home completion rejects a tenant/stage conflict returned as RPC false", async () => {
  const originalFetch = global.fetch;
  let sends = 0;
  global.fetch = async () => ({ ok: true, json: async () => false });
  try {
    await assert.rejects(
      completeTelegramHome("u1", "1", "東京都千代田区1-1", "https://supa.example", "service"),
      /onboarding_transition_failed/,
    );
    await assert.rejects(handleOnboardingText("1", "東京都千代田区1-1", {
      ...full, home_address: null, phone: null, paid: false, tg_onboard_stage: "calendar",
    }, {
      token: "t", base: "https://x", supaUrl: "https://supa.example", supaKey: "service",
      backfillCalendarContext: async () => {}, completeTelegramHome,
      sendMessage: async () => { sends++; },
    }), /onboarding_transition_failed/);
    assert.equal(sends, 0);
  } finally {
    global.fetch = originalFetch;
  }
});

test("calendar completion hook also runs on immediate /start or text resume", async () => {
  const calls = [];
  const row = { ...full, home_address: null, phone: null, paid: false, tg_onboard_stage: "calendar" };
  assert.equal(await backfillIfCalendarCompleted(row, {
    backfillCalendarContext: async (uid) => calls.push(uid),
  }), true);
  assert.deepEqual(calls, ["u1"]);
  assert.equal(await backfillIfCalendarCompleted({ ...row, tg_onboard_stage: "phone" }, {
    backfillCalendarContext: async () => calls.push("unexpected"),
  }), false);
});

test("best-effort Calendar context failure never loses the following home input", async () => {
  const completions = [];
  const row = { ...full, home_address: null, phone: null, paid: false, tg_onboard_stage: "calendar" };
  assert.equal(await handleOnboardingText("1", "東京都千代田区1-1", row, {
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k",
    backfillCalendarContext: async () => { throw new Error("temporary provider error"); },
    completeTelegramHome: async (...args) => completions.push(args),
    sendMessage: async () => ({ ok: true }),
  }), "home");
  assert.equal(completions.length, 1);
});

test("optional phone can be skipped without storing a number or enabling calls", async () => {
  const transitions = [], messages = [];
  const row = { ...full, phone: null, tg_onboard_stage: "phone" };
  assert.equal(await handleOnboardingText("1", "スキップ", row, {
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k", languageCode: "ja",
    transitionOnboarding: async (...args) => transitions.push(args),
    sendMessage: async (_token, _chat, text) => messages.push(text),
  }), "phone-skip");
  assert.deepEqual(transitions, [["u1", "1", "phone.skip", {}, "s", "k"]]);
  assert.equal(messages.length, 1);
});

test("saving a phone asks for separate call opt-in", async () => {
  const transitions = [], messages = [];
  const row = { ...full, phone: null, tg_onboard_stage: "phone" };
  assert.equal(await handleOnboardingText("1", "090-1234-5678", row, {
    token: "t", base: "https://x", supaUrl: "s", supaKey: "k", languageCode: "ja",
    transitionOnboarding: async (...args) => transitions.push(args),
    sendMessage: async (_token, _chat, text) => messages.push(text),
  }), "phone");
  assert.deepEqual(transitions, [["u1", "1", "phone.save", { phone: ["+81", "90", "1234", "5678"].join("") }, "s", "k"]]);
  assert.match(messages[0], /オンにしますか/);
});

test("call alerts remain off unless the user explicitly opts in", async () => {
  for (const [answer, action, result] of [["はい", "call.enable", "call-enable"], ["スキップ", "call.skip", "call-skip"]]) {
    const transitions = [], messages = [];
    const row = { ...full, phone: "+819012345678", tg_onboard_stage: "call" };
    assert.equal(await handleOnboardingText("1", answer, row, {
      token: "t", base: "https://x", supaUrl: "s", supaKey: "k", languageCode: "ja",
      transitionOnboarding: async (...args) => transitions.push(args),
      sendMessage: async (_token, _chat, text) => messages.push(text),
    }), result);
    assert.deepEqual(transitions, [["u1", "1", action, {}, "s", "k"]]);
    assert.equal(messages.length, 1);
  }
});

test("normalizePhone: valid forms", () => {
  assert.equal(normalizePhone("+810000000000"), "+810000000000");
  assert.equal(normalizePhone("090-1234-5678"), ["+81", "90", "1234", "5678"].join(""));
  assert.equal(normalizePhone("08012345678"), ["+81", "80", "1234", "5678"].join(""));
  assert.equal(normalizePhone("+44 (20) 7946-0958"), "+442079460958");
  assert.equal(normalizePhone("+81 90-1234-5678"), ["+81", "90", "1234", "5678"].join(""));
  assert.equal(normalizePhone("9012345678"), null);
});
test("normalizePhone: junk → null", () => {
  assert.equal(normalizePhone("hello"), null);
  assert.equal(normalizePhone("123"), null);
  assert.equal(normalizePhone(""), null);
});

test("stageMessage phone copy is localized, optional, and gives a concrete example", () => {
  const ja = stageMessage("phone", "1", "https://panel.example", "", "", "ja");
  const en = stageMessage("phone", "1", "https://panel.example", "", "", "en");
  assert.match(ja.text, /任意/);
  assert.match(ja.text, /090-1234-5678/);
  assert.match(en.text, /optional/i);
  assert.match(en.text, /\+81[ -]?90-1234-5678/);
});
