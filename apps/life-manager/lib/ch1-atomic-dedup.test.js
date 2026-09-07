// C-H1 (HARD-1): atomic claim helpers for ask + travel + wake dedup (race-safe).
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const { claimAsk, unclaimAsk } = require("./ask.js");
const { claimTravel, unclaimTravel } = require("./travel.js");
process.env.LM_CALL_SECRET = process.env.LM_CALL_SECRET || "unit_secret"; // scheduler.js require-time no-op guard
// claimWake/releaseWake read SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY from process.env directly
// (unlike claimTravel/claimAsk, which take supaUrl/supaKey as explicit args) — must be set for the
// SUPA() guard in scheduler.js to proceed past its "not configured → no-op" early return.
process.env.SUPABASE_URL = process.env.SUPABASE_URL || "http://s";
process.env.SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "k";
const {
  claimWake, releaseWake, isLowBalanceError, shouldAlertLowBalance, maybeAlertLowBalance, LOW_BALANCE_ALERT_COOLDOWN_MS,
} = require("../scheduler.js");

// Stub global.fetch to return a sequence of HTTP statuses, recording each call.
function stubFetch(statuses) {
  const calls = [];
  const orig = global.fetch;
  let i = 0;
  global.fetch = async (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET", body: (opts && opts.body) || "" });
    const status = statuses[Math.min(i++, statuses.length - 1)];
    return { status, ok: status >= 200 && status < 300, json: async () => [] };
  };
  return { calls, restore: () => { global.fetch = orig; } };
}

test("claimAsk: 201 → claimed (true); 409 → already asked (false); POSTs to lm_ask_log", async () => {
  let s = stubFetch([201]);
  assert.strictEqual(await claimAsk("u1", "e1", "http://s", "k"), true);
  assert.match(s.calls[0].url, /lm_ask_log/);
  assert.strictEqual(s.calls[0].method, "POST");
  assert.ok(s.calls[0].body.includes('"event_id":"e1"'));
  s.restore();
  s = stubFetch([409]);
  assert.strictEqual(await claimAsk("u1", "e1", "http://s", "k"), false, "409 → not claimed");
  s.restore();
});

test("claimAsk: any non-201 (500/network) → false (do NOT proceed to send on an unknown write)", async () => {
  let s = stubFetch([500]);
  assert.strictEqual(await claimAsk("u1", "e1", "http://s", "k"), false);
  s.restore();
});

test("claimAsk: no supa configured → true (best-effort, don't block the ask)", async () => {
  assert.strictEqual(await claimAsk("u1", "e1", "", ""), true);
});

test("claimTravel: 201 → true, 409 → false; POSTs lm_travel_log with the leg", async () => {
  let s = stubFetch([201]);
  assert.strictEqual(await claimTravel("u1", "k1", "go", "http://s", "k"), true);
  assert.match(s.calls[0].url, /lm_travel_log/);
  assert.ok(s.calls[0].body.includes('"leg":"go"'));
  assert.ok(s.calls[0].body.includes('"event_key":"k1"'));
  s.restore();
  s = stubFetch([409]);
  assert.strictEqual(await claimTravel("u1", "k1", "go", "http://s", "k"), false);
  s.restore();
});

test("claimTravel: go and return are SEPARATE legs (so one event gets both blocks, not deduped together)", async () => {
  const s = stubFetch([201, 201]);
  assert.strictEqual(await claimTravel("u1", "k1", "go", "http://s", "k"), true);
  assert.strictEqual(await claimTravel("u1", "k1", "return", "http://s", "k"), true);
  assert.ok(s.calls[0].body.includes('"leg":"go"'));
  assert.ok(s.calls[1].body.includes('"leg":"return"'));
  s.restore();
});

test("claimTravel: no supa → true (in-memory gcal dedup still guards obvious dups)", async () => {
  assert.strictEqual(await claimTravel("u1", "k1", "go", "", ""), true);
});

test("unclaimAsk DELETEs the exact (uid,event_id) row; unclaimTravel verifies 2xx DELETE", async () => {
  let s = stubFetch([200]);
  await unclaimAsk("u1", "e1", "http://s", "k");
  assert.strictEqual(s.calls[0].method, "DELETE");
  assert.match(s.calls[0].url, /lm_ask_log\?uid=eq\.u1&event_id=eq\.e1/);
  s.restore();
  s = stubFetch([200]);
  assert.strictEqual(await unclaimTravel("u1", "k1", "return", "http://s", "k"), true);
  assert.strictEqual(s.calls[0].method, "DELETE");
  assert.match(s.calls[0].url, /lm_travel_log\?uid=eq\.u1&event_key=eq\.k1&leg=eq\.return/);
  s.restore();
  s = stubFetch([204]);
  assert.strictEqual(await unclaimTravel("u1", "k1", "return", "http://s", "k"), true);
  s.restore();
  s = stubFetch([500]);
  assert.strictEqual(await unclaimTravel("u1", "k1", "return", "http://s", "k"), false);
  s.restore();
  const orig = global.fetch;
  global.fetch = async () => { throw new Error("network unavailable"); };
  try {
    assert.strictEqual(await unclaimTravel("u1", "k1", "return", "http://s", "k"), false);
  } finally {
    global.fetch = orig;
  }
  assert.strictEqual(await unclaimTravel("u1", "k1", "return", "", ""), false);
});

// ── INTEGRATION: fillTravel actually uses the claim ledger (catches FIND-001 evKey collision) ──────
const { fillTravel } = require("./travel.js");
function makeFakeCalendar(eventRows) {
  const created = [];
  return {
    _created: created,
    async listEventsRaw() { return eventRows; },
    async createEvent(_uid, payload) { created.push({ ...payload }); return { successful: true }; },
  };
}
function rawEvId(id, summary, location, startIso, endIso) {
  return { id, summary, location, start: { dateTime: startIso }, end: { dateTime: endIso } };
}
// Simulate lm_travel_log UNIQUE(uid,event_key,leg): a (uid,event_key,leg) POST 201s once then 409s.
function stubClaimLedger() {
  const seen = new Set();
  const orig = global.fetch;
  global.fetch = async (url, opts) => {
    if (/lm_travel_log/.test(url) && opts && opts.method === "POST") {
      const b = JSON.parse(opts.body);
      const key = `${b.uid}|${b.event_key}|${b.leg}`;
      if (seen.has(key)) return { status: 409, ok: false, json: async () => [] };
      seen.add(key);
      return { status: 201, ok: true, json: async () => [] };
    }
    return { status: 200, ok: true, json: async () => [] }; // DELETE / other
  };
  return { seen, restore: () => { global.fetch = orig; } };
}

test("[INTEGRATION][FIND-001] two same-startMs events with DIFFERENT ids BOTH get [Travel] blocks (evKey uses id, not startMs)", async () => {
  const s = stubClaimLedger();
  const start = "2026-06-20T14:00:00+09:00", end = "2026-06-20T15:00:00+09:00";
  const cal = makeFakeCalendar([
    rawEvId("evA", "Dentist", "Shibuya Hikarie, Tokyo", start, end),
    rawEvId("evB", "Investor meet", "Roppongi Hills, Tokyo", start, end),
  ]);
  await fillTravel("uidX", {
    apiKey: "x", mapsKey: "x", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"),
    calendar: cal, supaUrl: "http://s", supaKey: "k", _directionsMinutes: async () => 30,
  });
  const blocks = cal._created.filter((b) => /\[Travel\]/.test(b.summary || ""));
  // With evKey=id, evA and evB claim distinct keys → BOTH produce blocks. With the FIND-001 bug
  // (evKey=startMs) evB's claims would 409 and its block would be silently dropped.
  const distinctDest = new Set(blocks.map((b) => b.location));
  assert.ok(blocks.length >= 2, `both same-start events got blocks, got ${blocks.length}`);
  assert.ok(distinctDest.size >= 2, `blocks for BOTH venues exist (no evKey collision), got ${[...distinctDest]}`);
  s.restore();
});

test("[ALLOWANCE] exhausted tenant keeps Calendar read but performs zero route or Calendar write", async () => {
  const start = "2026-06-20T14:00:00+09:00", end = "2026-06-20T15:00:00+09:00";
  const cal = makeFakeCalendar([rawEvId("allowance-event", "Dentist", "Shibuya, Tokyo", start, end)]);
  let routes = 0, reserves = 0, completes = 0, geminiCalls = 0;
  const result = await fillTravel("allowance-user", {
    apiKey: "x", mapsKey: "x", geminiKey: "gemini", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"), calendar: cal,
    supaUrl: "http://s", supaKey: "k",
    _reserveManagedAction: async () => { reserves += 1; return { allowed: false }; },
    _completeManagedAction: async () => { completes += 1; return { allowed: true }; },
    _directionsMinutes: async () => { routes += 1; return 30; },
    _agentResolveLocation: async () => { geminiCalls += 1; return { kind: "filled", location: "x" }; },
  });
  assert.equal(result.checked, 1, "Calendar event was still read");
  assert.equal(reserves, 1);
  assert.equal(routes, 0);
  assert.equal(completes, 0);
  assert.equal(geminiCalls, 0, "no Gemini/Search grounding after allowance exhaustion");
  assert.equal(cal._created.length, 0);
});

test("[ALLOWANCE] negative route cache suppresses Gemini/Search fallback during its TTL", async () => {
  const start = "2026-06-20T14:00:00+09:00", end = "2026-06-20T15:00:00+09:00";
  const cal = makeFakeCalendar([rawEvId("negative-event", "Unknown room", "Room L1", start, end)]);
  let geminiCalls = 0, reserves = 0;
  await fillTravel("negative-user", {
    apiKey: "x", mapsKey: "x", geminiKey: "gemini", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"), calendar: cal,
    supaUrl: "http://s", supaKey: "k",
    _routeCache: { getByEvent: async () => ({ hit: true, value: null, failureClass: "no_route" }) },
    _reserveManagedAction: async () => { reserves += 1; return { allowed: true }; },
    _agentResolveLocation: async () => { geminiCalls += 1; return null; },
  });
  assert.equal(reserves, 0, "negative cache is read before allowance/provider work");
  assert.equal(geminiCalls, 0, "negative TTL also fences Gemini/Search grounding");
  assert.equal(cal._created.length, 0);
});

test("[INTEGRATION] re-running fillTravel does NOT double-create — 2nd run's claims all 409", async () => {
  const s = stubClaimLedger();
  const start = "2026-06-20T14:00:00+09:00", end = "2026-06-20T15:00:00+09:00";
  const rows = [rawEvId("evZ", "Dentist", "Shibuya Hikarie, Tokyo", start, end)];
  const base = {
    apiKey: "x", mapsKey: "x", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"), supaUrl: "http://s", supaKey: "k",
    _directionsMinutes: async () => 30,
  };
  const cal1 = makeFakeCalendar(rows);
  await fillTravel("uidY", { ...base, calendar: cal1 });
  assert.ok(cal1._created.length >= 1, "1st run creates block(s)");
  const cal2 = makeFakeCalendar(rows); // same claim ledger (stub seen-set persists)
  await fillTravel("uidY", { ...base, calendar: cal2 });
  const blocks2 = cal2._created.filter((b) => /\[Travel\]/.test(b.summary || ""));
  assert.strictEqual(blocks2.length, 0, "2nd run: claims 409 → no double-create");
  s.restore();
});

test("[INTEGRATION][16T] an unrelated helper 26 minutes before an event does not suppress its outbound block", async () => {
  const s = stubClaimLedger();
  const target = rawEvId("ev-16t-unrelated", "Physical appointment", "Shibuya Hikarie, Tokyo",
    "2026-06-20T14:00:00+09:00", "2026-06-20T15:00:00+09:00");
  const unrelated = rawEvId("helper-16t-unrelated", "[Travel] 🚆 unrelated", "Old venue",
    "2026-06-20T13:00:00+09:00", "2026-06-20T13:34:00+09:00");
  const cal = makeFakeCalendar([unrelated, target]);
  await fillTravel("uid-16t-unrelated", {
    apiKey: "x", mapsKey: "x", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"), calendar: cal,
    supaUrl: "http://s", supaKey: "k", _directionsMinutes: async () => 30,
  });
  const outbound = cal._created.find((block) => block.location === target.location);
  assert.ok(outbound, "the target event still receives an outbound block");
  s.restore();
});

test("[INTEGRATION][16T] a helper ending in the current-event window still dedupes outbound creation", async () => {
  const s = stubClaimLedger();
  const target = rawEvId("ev-16t-current", "Physical appointment", "Shibuya Hikarie, Tokyo",
    "2026-06-20T14:00:00+09:00", "2026-06-20T15:00:00+09:00");
  const current = rawEvId("helper-16t-current", "[Travel] 🚆 current", " shibuya  hikarie, tokyo ",
    "2026-06-20T13:00:00+09:00", "2026-06-20T13:59:00+09:00");
  const cal = makeFakeCalendar([current, target]);
  await fillTravel("uid-16t-current", {
    apiKey: "x", mapsKey: "x", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"), calendar: cal,
    supaUrl: "http://s", supaKey: "k", _directionsMinutes: async () => 30,
  });
  assert.equal(cal._created.some((block) => block.location === target.location), false,
    "the current-event helper suppresses a duplicate outbound block");
  s.restore();
});

test("[INTEGRATION][16T] a near-time helper for another location does not suppress event B", async () => {
  const s = stubClaimLedger();
  const target = rawEvId("ev-16t-other-location", "Physical appointment B", "Shibuya Hikarie, Tokyo",
    "2026-06-20T14:00:00+09:00", "2026-06-20T15:00:00+09:00");
  const otherLocation = rawEvId("helper-16t-other-location", "[Travel] 🚆 event A", "Roppongi Hills, Tokyo",
    "2026-06-20T13:00:00+09:00", "2026-06-20T13:59:00+09:00");
  const cal = makeFakeCalendar([otherLocation, target]);
  await fillTravel("uid-16t-other-location", {
    apiKey: "x", mapsKey: "x", home: "Setagaya, Tokyo",
    nowMs: Date.parse("2026-06-20T08:00:00+09:00"), calendar: cal,
    supaUrl: "http://s", supaKey: "k", _directionsMinutes: async () => 30,
  });
  assert.ok(cal._created.some((block) => block.location === target.location),
    "a helper for event A cannot suppress event B's outbound block");
  s.restore();
});

// ── issue#10: releaseWake — a dial failure must not permanently burn the (uid,event,level) slot ──
// 201 now hands back the CLAIM TOKEN rather than a bare `true` (see test/wake-claim-token.test.js
// for why: a release must prove it owns the row it deletes, or it can erase a later tick's
// successful claim and the user gets a second phone call). The property this test has always
// existed to pin is unchanged and is asserted harder — fresh is truthy AND now carries an identity
// that reaches the row, duplicate is still falsy, which is what every caller's `if (!fresh)` reads.
test("claimWake: 201 → a claim token (fresh), 409 → falsy (already called); POSTs lm_wake_log", async () => {
  let s = stubFetch([201]);
  const token = await claimWake("u1", "u1|2026-07-17T09:00:00+09:00|10");
  assert.strictEqual(typeof token, "string");
  assert.ok(token.length > 0, "a fresh claim is truthy — every caller gates the dial on that");
  assert.match(s.calls[0].url, /lm_wake_log/);
  assert.strictEqual(s.calls[0].method, "POST");
  assert.strictEqual(JSON.parse(s.calls[0].body).claim_token, token, "the token is written to the row");
  s.restore();
  s = stubFetch([409]);
  assert.ok(!(await claimWake("u1", "u1|2026-07-17T09:00:00+09:00|10")), "409 → not claimed");
  s.restore();
});

test("releaseWake DELETEs the exact (uid,event_key) row (mirrors unclaimTravel)", async () => {
  const s = stubFetch([200]);
  await releaseWake("u1", "u1|2026-07-17T09:00:00+09:00|10");
  assert.strictEqual(s.calls[0].method, "DELETE");
  assert.match(s.calls[0].url, /lm_wake_log\?uid=eq\.u1&event_key=eq\.u1%7C2026-07-17T09%3A00%3A00%2B09%3A00%7C10/);
  s.restore();
});

test("[INTEGRATION] claim→dial-fail→release→re-claim: a failed dial is retryable on the next tick", async () => {
  const eventKey = "u1|2026-07-17T09:00:00+09:00|10";
  // 1st tick: claim succeeds (201) — simulates claimWake before a placeCall that then fails.
  let s = stubFetch([201]);
  const token = await claimWake("u1", eventKey);
  assert.ok(token, "1st claim succeeds");
  s.restore();
  // dial failed → release the claim (as wakeUserOnce now does on !res.ok), carrying the token it
  // claimed with so a release arriving late cannot delete a different tick's successful claim.
  s = stubFetch([200]);
  await releaseWake("u1", eventKey, token);
  assert.strictEqual(s.calls[0].method, "DELETE", "release issues a DELETE");
  assert.match(s.calls[0].url, /claim_token=eq\./, "and it names the claim it is releasing");
  s.restore();
  // 2nd tick (after balance topped up): claim succeeds again because the row was released — this is
  // the exact bug this fix closes (without releaseWake, this 2nd claim would 409 forever).
  s = stubFetch([201]);
  assert.ok(await claimWake("u1", eventKey), "released claim can be re-claimed next tick");
  s.restore();
});

// ── issue#10: low-balance admin Telegram alert, throttled to 1/6h ────────────────────────────────
test("isLowBalanceError matches the exact dial.js message, ignores unrelated dial errors", () => {
  assert.strictEqual(isLowBalanceError("telnyx balance too low ($0.43)"), true);
  assert.strictEqual(isLowBalanceError("telnyx env missing (API/CONN/FROM)"), false);
  assert.strictEqual(isLowBalanceError("no call_control_id"), false);
  assert.strictEqual(isLowBalanceError(""), false);
});

test("shouldAlertLowBalance: fires once, then throttled for LOW_BALANCE_ALERT_COOLDOWN_MS", () => {
  const msg = "telnyx balance too low ($0.43)";
  const lastAlert = 10_000; // an arbitrary prior alert time (NOT 0 — 0 is the "never alerted" sentinel
  // in scheduler.js's lastLowBalanceAlertMs; testing the boundary against a real prior alert timestamp
  // isolates the throttle-window math from that sentinel).
  assert.strictEqual(
    shouldAlertLowBalance(msg, lastAlert + 1000, lastAlert), false, "1s after the last alert — inside the 6h cooldown"
  );
  assert.strictEqual(
    shouldAlertLowBalance(msg, lastAlert + LOW_BALANCE_ALERT_COOLDOWN_MS - 1, lastAlert), false, "1ms before cooldown elapses"
  );
  assert.strictEqual(
    shouldAlertLowBalance(msg, lastAlert + LOW_BALANCE_ALERT_COOLDOWN_MS, lastAlert), true, "exactly at cooldown boundary fires again"
  );
  assert.strictEqual(
    shouldAlertLowBalance(msg, lastAlert + LOW_BALANCE_ALERT_COOLDOWN_MS, lastAlert) &&
    !shouldAlertLowBalance("no call_control_id", lastAlert + LOW_BALANCE_ALERT_COOLDOWN_MS, lastAlert),
    true, "non-balance error never alerts even past the cooldown"
  );
  // The real "never alerted yet" case: module state starts lastLowBalanceAlertMs=0, and Date.now()
  // in production is always astronomically larger than LOW_BALANCE_ALERT_COOLDOWN_MS past 0.
  assert.strictEqual(
    shouldAlertLowBalance(msg, LOW_BALANCE_ALERT_COOLDOWN_MS + 1, 0), true, "first-ever alert (sentinel lastAlert=0) fires once enough time has passed"
  );
});

// NOTE: lastLowBalanceAlertMs is module-level state in scheduler.js (in-memory throttle, single
// process — by design, see maybeAlertLowBalance). It persists ACROSS tests in this file, so each
// test below uses a `nowMs` far (>>6h) from any previous test's `nowMs` to guarantee it lands on the
// intended branch regardless of run order, rather than accidentally passing because of the throttle.
test("maybeAlertLowBalance: no LM_ADMIN_TELEGRAM_CHAT_ID configured → logs, does not throw, does not fetch", async () => {
  const origToken = process.env.LM_TELEGRAM_BOT_TOKEN, origChat = process.env.LM_ADMIN_TELEGRAM_CHAT_ID;
  delete process.env.LM_TELEGRAM_BOT_TOKEN; delete process.env.LM_ADMIN_TELEGRAM_CHAT_ID;
  const s = stubFetch([200]);
  await assert.doesNotReject(maybeAlertLowBalance("telnyx balance too low ($0.10)", 1000 * 3600 * 1000));
  assert.strictEqual(s.calls.length, 0, "no admin chat configured → never calls the Telegram API");
  s.restore();
  if (origToken !== undefined) process.env.LM_TELEGRAM_BOT_TOKEN = origToken;
  if (origChat !== undefined) process.env.LM_ADMIN_TELEGRAM_CHAT_ID = origChat;
});

test("maybeAlertLowBalance: with admin chat configured, sends via the Telegram Bot API and respects the throttle", async () => {
  const origToken = process.env.LM_TELEGRAM_BOT_TOKEN, origChat = process.env.LM_ADMIN_TELEGRAM_CHAT_ID;
  process.env.LM_TELEGRAM_BOT_TOKEN = "test_token";
  process.env.LM_ADMIN_TELEGRAM_CHAT_ID = "12345";
  const s = stubFetch([200, 200]);
  const t0 = 2000 * 3600 * 1000; // far past the previous test's nowMs → guaranteed to fire
  await maybeAlertLowBalance("telnyx balance too low ($0.10)", t0);
  assert.strictEqual(s.calls.length, 1, "first low-balance failure sends an alert");
  assert.match(s.calls[0].url, /api\.telegram\.org\/bottest_token\/sendMessage/);
  assert.ok(s.calls[0].body.includes('"chat_id":"12345"'));
  await maybeAlertLowBalance("telnyx balance too low ($0.05)", t0 + 1000); // 1s later — still throttled
  assert.strictEqual(s.calls.length, 1, "a second failure inside the 6h window does not send another alert");
  s.restore();
  if (origToken !== undefined) process.env.LM_TELEGRAM_BOT_TOKEN = origToken; else delete process.env.LM_TELEGRAM_BOT_TOKEN;
  if (origChat !== undefined) process.env.LM_ADMIN_TELEGRAM_CHAT_ID = origChat; else delete process.env.LM_ADMIN_TELEGRAM_CHAT_ID;
});
