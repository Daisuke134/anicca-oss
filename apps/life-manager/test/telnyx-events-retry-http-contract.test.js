// test/telnyx-events-retry-http-contract.test.js — spec §3 row 2a: WHICH HTTP STATUS /telnyx-events
// answers a detection with, driven through the REAL server.js over real HTTP.
//
// This is a status-code contract, and it can only be tested from the outside. The unit tests around
// applyAmdDetection already prove what gets written; none of them can see the one thing that decides
// whether a lost write is recoverable — the number we hand back to Telnyx. Telnyx reads any 2xx as
// "you have it now, I am done" and only redelivers when it does NOT get one
// (developers.telnyx.com/development/api-fundamentals/webhooks/receiving-webhooks: "All response
// codes outside this range... will indicate to Telnyx that you did not receive the webhook"; up to 3
// primary + 3 failover attempts with exponential backoff). So a 200 sent while Supabase is down is
// not a cosmetic mislabel: it is us destroying the only remaining copy of that detection.
//
// The fake fetch THROWS on any host or path it was not told about, so "the handler answered without
// reaching Supabase" cannot pass unnoticed — it would surface as an unexpected-fetch failure rather
// than as a green test.
"use strict";

const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const crypto = require("node:crypto");
const { encodeWakeClientState, encodeTestCallClientState } = require("../lib/telnyx-webhook.js");

const TEST_PHONE = "+99900000000";

const WAKE_UID = "lm_fixture_uid";
const WAKE_EVENT_KEY = "fixture-event-key";
const wakeClientState = encodeWakeClientState({ wakeUid: WAKE_UID, wakeEventKey: WAKE_EVENT_KEY });
const testClientState = encodeTestCallClientState({ testUid: WAKE_UID });
const CLAIM_UID = "claim-bound-user";
const CLAIM_EVENT_KEY = "claim-bound-event";
const CLAIM_TOKEN = "claim-bound-token";
const claimClientState = encodeWakeClientState({
  wakeUid: CLAIM_UID, wakeEventKey: CLAIM_EVENT_KEY, wakeClaimToken: CLAIM_TOKEN,
});
const managedClaimClientState = encodeWakeClientState({
  wakeUid: CLAIM_UID, wakeEventKey: CLAIM_EVENT_KEY, wakeClaimToken: CLAIM_TOKEN,
  managedActionKey: "calendar-event-1",
});

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

// Telnyx signs `${timestamp}|${rawBody}` with ed25519 and publishes the public key as raw base64 —
// the same shape createTelnyxPublicKey() accepts, so this fixture exercises the production verifier
// instead of a bypass. Without a valid signature the route 403s and every assertion below would be
// measuring the signature check rather than the retry contract.
function telnyxKeypair() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
  const spki = publicKey.export({ format: "der", type: "spki" });
  return { privateKey, publicKeyBase64: spki.subarray(spki.length - 32).toString("base64") };
}

let keys;
let productionServer;
let origin;
let originalCreateServer;
let originalFetch;
// What the two upstreams do for the POST currently in flight. Reset on every postSignedAmdEvent so a
// case that says nothing about Supabase gets the honest default instead of the previous case's outage.
let supabaseHandler;
let telnyxHandler;
const upstreamCalls = [];

before(async () => {
  keys = telnyxKeypair();
  process.env.LM_UID_SECRET = "fixture-uid-secret";
  process.env.LM_CALL_SECRET = "fixture-call-secret";
  process.env.SUPABASE_URL = "https://fixture.supabase.co";
  process.env.SUPABASE_SERVICE_ROLE_KEY = "fixture-service-role";
  process.env.PUBLIC_WSS = "wss://life-call-fixture.up.railway.app";
  process.env.TELNYX_API_KEY = "fixture-telnyx-key";
  process.env.TELNYX_CONNECTION_ID = "fixture-connection";
  process.env.TELNYX_PHONE_NUMBER = TEST_PHONE;
  process.env.TELNYX_PUBLIC_KEY = keys.publicKeyBase64;
  process.env.LM_AMD = "on";
  process.env.LIFE_RUN_LOOPS = "false";

  originalCreateServer = http.createServer;
  originalFetch = global.fetch;
  http.createServer = (handler) => {
    productionServer = originalCreateServer(handler);
    return productionServer;
  };

  global.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const method = String(init.method || "GET").toUpperCase();
    upstreamCalls.push({ url: String(input), method, body: init.body || "" });
    if (url.hostname === "fixture.supabase.co") {
      if ((url.pathname === "/rest/v1/lm_wake_log" && method === "PATCH") ||
        (url.pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt" && method === "POST") ||
        (url.pathname === "/rest/v1/rpc/complete_lm_managed_action" && method === "POST") ||
        (url.pathname === "/rest/v1/rpc/release_lm_managed_action" && method === "POST")) {
        return supabaseHandler(String(input), init);
      }
      throw new Error(`unexpected supabase ${method} ${url.pathname}`);
    }
    if (url.hostname === "api.telnyx.com") {
      if (/^\/v2\/calls\/.+\/actions\/hangup$/.test(url.pathname) && method === "POST") {
        return telnyxHandler(String(input), init);
      }
      throw new Error(`unexpected telnyx ${method} ${url.pathname}`);
    }
    throw new Error(`unexpected fetch ${method} ${url}`);
  };

  const serverPath = require.resolve("../server.js");
  delete require.cache[serverPath];
  require(serverPath);
  assert.ok(productionServer, "the production HTTP server must be captured");
  await new Promise((resolve) => productionServer.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${productionServer.address().port}`;
});

after(async () => {
  global.fetch = originalFetch;
  http.createServer = originalCreateServer;
  if (productionServer) await new Promise((resolve) => productionServer.close(resolve));
});

function post(path, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const raw = Buffer.from(body, "utf8");
    const request = http.request(`${origin}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", "content-length": raw.length, ...headers },
    }, (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { text += chunk; });
      res.on("end", () => resolve({ status: res.statusCode, text }));
    });
    request.on("error", reject);
    request.end(raw);
  });
}

// One detection webhook exactly as Telnyx sends it, signed with the fixture key. `supabase` and
// `telnyx` are what the two upstreams answer with for this delivery; both default to working, so a
// case only has to describe the failure it is about.
async function postSignedAmdEvent({
  clientState, result, supabase, telnyx,
  eventId = "fixture-webhook-id", callControlId = "v2:fixture-ccid",
  callSessionId, callLegId, signatureValue,
  eventType = "call.machine.detection.ended",
} = {}) {
  supabaseHandler = supabase || (() => response(200, [{ event_key: WAKE_EVENT_KEY }]));
  telnyxHandler = telnyx || (() => response(200, { data: { result: "ok" } }));
  const payload = { result, call_control_id: callControlId, client_state: clientState };
  if (callSessionId !== undefined) payload.call_session_id = callSessionId;
  if (callLegId !== undefined) payload.call_leg_id = callLegId;
  const data = { event_type: eventType, payload };
  if (eventId !== undefined) data.id = eventId;
  const body = JSON.stringify({
    data,
  });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = crypto.sign(
    null,
    Buffer.concat([Buffer.from(`${timestamp}|`, "utf8"), Buffer.from(body, "utf8")]),
    keys.privateKey,
  );
  return post("/telnyx-events", body, {
    "telnyx-signature-ed25519": signatureValue || signature.toString("base64"),
    "telnyx-timestamp": timestamp,
  });
}

function pathOf(call) {
  return new URL(call.url).pathname;
}

function claimReceiptBody({
  uid = CLAIM_UID, eventKey = CLAIM_EVENT_KEY, claimToken = CLAIM_TOKEN,
  callControlId = "v2:claim-control-id", callSessionId = "claim-session-id",
  callLegId = "claim-leg-id", webhookEventId = "claim-webhook-id", amdResult = "machine",
} = {}) {
  return {
    p_uid: uid,
    p_event_key: eventKey,
    p_claim_token: claimToken,
    p_telnyx_call_control_id: callControlId,
    p_telnyx_call_session_id: callSessionId,
    p_telnyx_call_leg_id: callLegId,
    p_telnyx_webhook_event_id: webhookEventId,
    p_amd_result: amdResult,
  };
}

test("a signed call.hangup writes one exact wake receipt without an outbound call", async () => {
  upstreamCalls.length = 0;
  const rpcBodies = [];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname !== "/rest/v1/rpc/record_lm_wake_telnyx_receipt") {
      throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
    }
    rpcBodies.push(JSON.parse(init.body));
    return response(200, 1);
  };
  const res = await postSignedAmdEvent({
    eventType: "call.hangup", clientState: claimClientState, result: undefined,
    eventId: "hangup-webhook-id", callControlId: "hangup-control-id",
    callSessionId: "hangup-session-id", callLegId: "hangup-leg-id", supabase,
  });
  assert.equal(res.status, 200);
  assert.deepEqual(rpcBodies, [claimReceiptBody({
    callControlId: "hangup-control-id", callSessionId: "hangup-session-id",
    callLegId: "hangup-leg-id", webhookEventId: "hangup-webhook-id", amdResult: null,
  })]);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/rest/v1/lm_wake_log").length, 0);
  assert.equal(upstreamCalls.filter((call) => pathOf(call).endsWith("/actions/hangup")).length, 0);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
});

test("call.hangup receipt failure returns 5xx and matched zero stays effect-free 200", async () => {
  for (const [supabase, expectedStatus] of [
    [() => response(503, { secret: "provider-body" }), 503],
    [() => { throw new Error("provider-network-error"); }, 503],
    [() => response(200, 0), 200],
  ]) {
    upstreamCalls.length = 0;
    let rpcCalls = 0;
    const guardedSupabase = (url, init) => {
      rpcCalls += 1;
      const parsed = new URL(url);
      assert.equal(parsed.pathname, "/rest/v1/rpc/record_lm_wake_telnyx_receipt", "wrong receipt route");
      return supabase(url, init);
    };
    const res = await postSignedAmdEvent({
      eventType: "call.hangup", clientState: claimClientState, result: undefined,
      eventId: "hangup-webhook-id", callControlId: "hangup-control-id",
      callSessionId: "hangup-session-id", callLegId: "hangup-leg-id", supabase: guardedSupabase,
    });
    assert.equal(res.status >= 500 && res.status < 600, expectedStatus === 503);
    assert.equal(res.status, expectedStatus);
    assert.equal(rpcCalls, 1);
    assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/rest/v1/lm_wake_log").length, 0);
    assert.equal(upstreamCalls.filter((call) => pathOf(call).endsWith("/actions/hangup")).length, 0);
  }
});

test("exact call.hangup replay preserves receipt identity and creates no call effect", async () => {
  upstreamCalls.length = 0;
  const rpcBodies = [];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    assert.equal(parsed.pathname, "/rest/v1/rpc/record_lm_wake_telnyx_receipt");
    rpcBodies.push(JSON.parse(init.body));
    return response(200, 1);
  };
  const input = {
    eventType: "call.hangup", clientState: claimClientState, result: undefined,
    eventId: "hangup-replay-id", callControlId: "hangup-control-id",
    callSessionId: "hangup-session-id", callLegId: "hangup-leg-id", supabase,
  };
  assert.equal((await postSignedAmdEvent(input)).status, 200);
  assert.equal((await postSignedAmdEvent(input)).status, 200);
  assert.deepEqual(rpcBodies, [
    claimReceiptBody({ callControlId: "hangup-control-id", callSessionId: "hangup-session-id", callLegId: "hangup-leg-id", webhookEventId: "hangup-replay-id", amdResult: null }),
    claimReceiptBody({ callControlId: "hangup-control-id", callSessionId: "hangup-session-id", callLegId: "hangup-leg-id", webhookEventId: "hangup-replay-id", amdResult: null }),
  ]);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
});

test("call.hangup missing event or call-control identity returns 5xx without receipt fetch", async () => {
  for (const input of [
    { eventId: null, callControlId: "hangup-control-id" },
    { eventId: "hangup-webhook-id", callControlId: null },
  ]) {
    upstreamCalls.length = 0;
    let fetches = 0;
    const supabase = () => { fetches += 1; throw new Error("receipt must not fetch"); };
    const res = await postSignedAmdEvent({
      eventType: "call.hangup", clientState: claimClientState, result: undefined,
      callSessionId: "hangup-session-id", callLegId: "hangup-leg-id", supabase, ...input,
    });
    assert.equal(res.status >= 500 && res.status < 600, true);
    assert.equal(fetches, 0);
    assert.equal(upstreamCalls.filter((call) => pathOf(call).includes("/rest/v1/")).length, 0);
  }
});

test("non-terminal, test-call, legacy, and unreadable hangup states do not write a wake receipt", async () => {
  const states = [
    { eventType: "call.initiated", clientState: claimClientState },
    { eventType: "call.answered", clientState: claimClientState },
    { eventType: "call.hangup", clientState: testClientState },
    { eventType: "call.hangup", clientState: wakeClientState },
    { eventType: "call.hangup", clientState: Buffer.from(JSON.stringify({ other: "foreign" }), "utf8").toString("base64") },
  ];
  for (const state of states) {
    upstreamCalls.length = 0;
    let fetches = 0;
    const supabase = () => { fetches += 1; throw new Error("unexpected wake receipt"); };
    const res = await postSignedAmdEvent({
      ...state, result: undefined, eventId: "ignored-webhook-id", callControlId: "ignored-control-id", supabase,
    });
    assert.equal(res.status, 200);
    assert.equal(fetches, 0);
    assert.equal(upstreamCalls.filter((call) => pathOf(call).includes("/rest/v1/")).length, 0);
    assert.equal(upstreamCalls.filter((call) => pathOf(call).endsWith("/actions/hangup")).length, 0);
  }
});

test("a signed claim-bound machine event writes one exact receipt before the existing hangup", async () => {
  upstreamCalls.length = 0;
  const rpcBodies = [];
  const sentinels = [
    "v2:claim-control-id", "claim-session-id", "claim-leg-id", "claim-webhook-id", CLAIM_TOKEN,
    "+99900000000", CLAIM_EVENT_KEY, "provider-secret-hangup",
  ];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname !== "/rest/v1/rpc/record_lm_wake_telnyx_receipt") {
      throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
    }
    rpcBodies.push(JSON.parse(init.body));
    return response(200, 1);
  };
  const logs = [];
  const originalLog = console.log;
  const originalError = console.error;
  console.log = (...args) => logs.push(args.join(" "));
  console.error = (...args) => logs.push(args.join(" "));
  let res;
  try {
    res = await postSignedAmdEvent({
      clientState: claimClientState,
      result: " machine ",
      eventId: "claim-webhook-id",
      callControlId: "v2:claim-control-id",
      callSessionId: "claim-session-id",
      callLegId: "claim-leg-id",
      supabase,
      telnyx: () => response(503, { provider_secret_body: "provider-secret-hangup" }),
    });
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }
  assert.equal(res.status, 200);
  assert.equal(res.text, "recorded");
  assert.deepEqual(rpcBodies, [claimReceiptBody()]);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/rest/v1/rpc/record_lm_wake_telnyx_receipt").length, 1);
  assert.equal(upstreamCalls.filter((call) => pathOf(call).endsWith("/actions/hangup")).length, 1);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0,
    "receipt wiring must not add outbound call creation");
  assert.ok(
    upstreamCalls.findIndex((call) => pathOf(call) === "/rest/v1/rpc/record_lm_wake_telnyx_receipt")
      < upstreamCalls.findIndex((call) => pathOf(call).endsWith("/actions/hangup")),
    "the durable receipt must be written before the existing machine hangup",
  );
  assert.ok(logs.length >= 1);
  assert.ok(logs.every((line) => sentinels.every((sentinel) => !line.includes(sentinel))),
    `provider identity/client state leaked into logs: ${JSON.stringify(logs)}`);
});

test("a claim-bound human writes answered_at only after receipt matched=1", async () => {
  upstreamCalls.length = 0;
  const order = [];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt") {
      order.push("receipt");
      assert.deepEqual(JSON.parse(init.body), claimReceiptBody({ amdResult: "human" }));
      return response(200, 1);
    }
    if (parsed.pathname === "/rest/v1/lm_wake_log" && init.method === "PATCH") {
      order.push("answered");
      return response(200, [{ event_key: CLAIM_EVENT_KEY }]);
    }
    throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
  };
  const res = await postSignedAmdEvent({
    clientState: claimClientState, result: "human", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", callSessionId: "claim-session-id", callLegId: "claim-leg-id", supabase,
  });
  assert.equal(res.status, 200);
  assert.equal(res.text, "answered");
  assert.deepEqual(order, ["receipt", "answered"], "record-first is the human answered_at fence");
});

test("a human-confirmed call completes the same managed event allowance after wake receipt", async () => {
  upstreamCalls.length = 0;
  const order = [];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt") {
      order.push("receipt"); return response(200, 1);
    }
    if (parsed.pathname === "/rest/v1/lm_wake_log") {
      order.push("answered"); return response(200, [{ event_key: CLAIM_EVENT_KEY }]);
    }
    if (parsed.pathname === "/rest/v1/rpc/complete_lm_managed_action") {
      order.push("allowance");
      assert.deepEqual(JSON.parse(init.body), { p_uid: CLAIM_UID, p_action_key: "calendar-event-1" });
      return response(200, { allowed: true, used: 1, limit: 30, periodStart: "2026-09-01", resetAt: "2026-10-01", notify: false });
    }
    throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
  };
  const res = await postSignedAmdEvent({
    clientState: managedClaimClientState, result: "human", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", callSessionId: "claim-session-id", callLegId: "claim-leg-id", supabase,
  });
  assert.equal(res.status, 200);
  assert.deepEqual(order, ["receipt", "answered", "allowance"]);
});

test("managed allowance receipt failure returns 5xx for replay without creating another call", async () => {
  upstreamCalls.length = 0;
  const supabase = (url, init) => {
    const pathname = new URL(url).pathname;
    if (pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt") return response(200, 1);
    if (pathname === "/rest/v1/lm_wake_log") return response(200, [{ event_key: CLAIM_EVENT_KEY }]);
    if (pathname === "/rest/v1/rpc/complete_lm_managed_action") return response(503, {});
    throw new Error(`unexpected supabase write ${init.method} ${pathname}`);
  };
  const res = await postSignedAmdEvent({
    clientState: managedClaimClientState, result: "human", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", supabase,
  });
  assert.equal(res.status, 503);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
});

test("a claim-bound answered_at failure is bounded and still answers 200 after the receipt", async () => {
  upstreamCalls.length = 0;
  const order = [];
  const logs = [];
  const forbidden = [
    CLAIM_UID, "v2:claim-control-id", "claim-session-id", "claim-leg-id", "claim-webhook-id",
    CLAIM_TOKEN, "+99900000000", "raw-answered-secret-sentinel",
  ];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt") {
      order.push("receipt");
      assert.deepEqual(JSON.parse(init.body), claimReceiptBody({ amdResult: "human" }));
      return response(200, 1);
    }
    if (parsed.pathname === "/rest/v1/lm_wake_log" && init.method === "PATCH") {
      order.push("answered");
      throw new Error("raw-answered-secret-sentinel");
    }
    throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
  };
  const originalLog = console.log;
  const originalError = console.error;
  console.log = (...args) => logs.push(args.join(" "));
  console.error = (...args) => logs.push(args.join(" "));
  let res;
  try {
    res = await postSignedAmdEvent({
      clientState: claimClientState, result: "human", eventId: "claim-webhook-id",
      callControlId: "v2:claim-control-id", callSessionId: "claim-session-id",
      callLegId: "claim-leg-id", supabase,
    });
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }
  assert.equal(res.status, 200, "answered_at remains best-effort after receipt acceptance");
  assert.equal(res.text, "answered_at unchanged");
  assert.deepEqual(order, ["receipt", "answered"], "receipt precedes the failing answered_at PATCH");
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
  assert.ok(logs.some((line) => line.includes("network_error")), `bounded error was not logged: ${JSON.stringify(logs)}`);
  assert.ok(logs.every((line) => forbidden.every((sentinel) => !line.includes(sentinel))),
    `answered_at failure leaked sensitive data: ${JSON.stringify(logs)}`);
});

test("a claim-bound human with receipt matched=0 writes no answered_at and answers 200", async () => {
  upstreamCalls.length = 0;
  let rpcCalls = 0;
  let patchCalls = 0;
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt") { rpcCalls += 1; return response(200, 0); }
    if (parsed.pathname === "/rest/v1/lm_wake_log" && init.method === "PATCH") { patchCalls += 1; return response(200, []); }
    throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
  };
  const res = await postSignedAmdEvent({
    clientState: claimClientState, result: "human", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", supabase,
  });
  assert.equal(res.status, 200);
  assert.equal(res.text, "answered_at unchanged");
  assert.equal(rpcCalls, 1);
  assert.equal(patchCalls, 0, "matched zero cannot latch answered_at");
});

test("claim-bound receipt HTTP failure and thrown fetch return 5xx after the existing machine hangup attempt", async () => {
  for (const [supabase, forbidden] of [
    [() => response(503, { provider_secret_body: "must-not-escape" }), "must-not-escape"],
    [() => { throw new Error("raw-provider-error-must-not-escape"); }, "raw-provider-error-must-not-escape"],
  ]) {
    upstreamCalls.length = 0;
    const logs = [];
    const originalError = console.error;
    const originalLog = console.log;
    console.error = (...args) => logs.push(args.join(" "));
    console.log = (...args) => logs.push(args.join(" "));
    let res;
    try {
      res = await postSignedAmdEvent({
        clientState: claimClientState, result: "machine", eventId: "claim-webhook-id",
        callControlId: "v2:claim-control-id", supabase,
      });
    } finally {
      console.error = originalError;
      console.log = originalLog;
    }
    assert.equal(res.status >= 500 && res.status < 600, true);
    assert.equal(upstreamCalls.filter((call) => pathOf(call).endsWith("/actions/hangup")).length, 1);
    assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
    assert.ok(logs.every((line) => !line.includes(forbidden)), `raw provider failure leaked: ${JSON.stringify(logs)}`);
  }
});

test("missing claim-bound event or call-control identity returns 5xx without a receipt fetch or guessed ID", async () => {
  for (const input of [
    { eventId: null, callControlId: "v2:claim-control-id" },
    { eventId: "claim-webhook-id", callControlId: null },
  ]) {
    upstreamCalls.length = 0;
    let rpcCalls = 0;
    const supabase = () => { rpcCalls += 1; throw new Error("receipt must not fetch"); };
    const res = await postSignedAmdEvent({
      clientState: claimClientState, result: "machine", supabase, ...input,
    });
    assert.equal(res.status >= 500 && res.status < 600, true);
    assert.equal(rpcCalls, 0);
    assert.equal(upstreamCalls.filter((call) => pathOf(call).includes("/rest/v1/rpc/")).length, 0);
    assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
  }
});

test("an exact signed replay preserves the receipt identity and creates no outbound call", async () => {
  upstreamCalls.length = 0;
  const rpcBodies = [];
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname !== "/rest/v1/rpc/record_lm_wake_telnyx_receipt") {
      throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
    }
    rpcBodies.push(JSON.parse(init.body));
    return response(200, 1);
  };
  const first = await postSignedAmdEvent({
    clientState: claimClientState, result: "machine", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", callSessionId: "claim-session-id", callLegId: "claim-leg-id", supabase,
  });
  const second = await postSignedAmdEvent({
    clientState: claimClientState, result: "machine", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", callSessionId: "claim-session-id", callLegId: "claim-leg-id", supabase,
  });
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.deepEqual(rpcBodies, [claimReceiptBody(), claimReceiptBody()]);
  assert.equal(upstreamCalls.filter((call) => pathOf(call) === "/v2/calls").length, 0);
});

test("an invalid Telnyx signature performs zero upstream fetches for claim-bound receipt work", async () => {
  upstreamCalls.length = 0;
  const res = await postSignedAmdEvent({
    clientState: claimClientState, result: "machine", eventId: "claim-webhook-id",
    callControlId: "v2:claim-control-id", signatureValue: "invalid-signature",
  });
  assert.equal(res.status, 403);
  assert.equal(upstreamCalls.length, 0);
});

test("a legacy two-field wake state still uses one PATCH and never the receipt RPC", async () => {
  upstreamCalls.length = 0;
  let patchCalls = 0;
  let rpcCalls = 0;
  const supabase = (url, init) => {
    const parsed = new URL(url);
    if (parsed.pathname === "/rest/v1/lm_wake_log" && init.method === "PATCH") {
      patchCalls += 1;
      return response(200, [{ event_key: WAKE_EVENT_KEY }]);
    }
    if (parsed.pathname === "/rest/v1/rpc/record_lm_wake_telnyx_receipt") { rpcCalls += 1; return response(200, 1); }
    throw new Error(`unexpected supabase write ${init.method} ${parsed.pathname}`);
  };
  const res = await postSignedAmdEvent({ clientState: wakeClientState, result: "machine", supabase });
  assert.equal(res.status, 200);
  assert.equal(patchCalls, 1);
  assert.equal(rpcCalls, 0);
});

// Supabase answered, and answered badly. Nothing about that says the detection is unrecordable — it
// says this second was a bad second. Telnyx is still holding the event and will bring it back with
// exponential backoff, but only if we decline it now; a 200 here spends our last retry on an outage.
test("a failed amd_result write asks Telnyx to send it again", async () => {
  const res = await postSignedAmdEvent({
    clientState: wakeClientState,
    result: "machine",
    supabase: () => response(500, {}),
  });
  assert.equal(res.status >= 500 && res.status < 600, true, `expected 5xx, got ${res.status}`);
});

// The request never reached Supabase at all (DNS, refused connection, TLS). Strictly less evidence of
// a permanent problem than the 500 above, so it must not be answered more finally than the 500 is.
test("a thrown Supabase write also asks for a retry", async () => {
  const res = await postSignedAmdEvent({
    clientState: wakeClientState,
    result: "machine",
    supabase: () => { throw new Error("ECONNREFUSED"); },
  });
  assert.equal(res.status >= 500 && res.status < 600, true, `expected 5xx, got ${res.status}`);
});

// The write LANDED and correctly changed nothing: there is no lm_wake_log row for this uid+event_key.
// Redelivery cannot conjure one, so asking for six of them would burn the retry budget, delay the
// failover URL, and bury the real outages above under noise that can never resolve.
test("a write that matched no row is NOT retried", async () => {
  const res = await postSignedAmdEvent({
    clientState: wakeClientState,
    result: "machine",
    supabase: () => response(200, []),
  });
  assert.equal(res.status, 200);
});

// The whole point of returning 5xx is that 200 keeps meaning "recorded". If the happy path drifted
// into 5xx, every wake call would be delivered six times and written six times.
test("a recorded detection still answers 200", async () => {
  const res = await postSignedAmdEvent({
    clientState: wakeClientState,
    result: "machine",
    supabase: () => response(200, [{ event_key: WAKE_EVENT_KEY }]),
  });
  assert.equal(res.status, 200);
});

// A test call has no lm_wake_log row by design (spec §3 row 2d), so its only side effect is the
// hangup — and a hangup is not redeliverable. By the time the first backoff expires the call has
// already run to the carrier's recording limit and there is nobody left to cut off. Retrying would
// re-spend delivery attempts on an action that can no longer happen.
test("a test call answers 200 even when the hangup fails", async () => {
  const res = await postSignedAmdEvent({
    clientState: testClientState,
    result: "machine",
    telnyx: () => response(422, {}),
  });
  assert.equal(res.status, 200);
});

// The 5xx above is only correct if reprocessing is harmless, because Telnyx redelivers the SAME
// payload — same data.id, same client_state, only meta.attempt moves
// (developers.telnyx.com/docs/voice/programmable-voice/voice-api-webhooks). Today it IS harmless, by
// two properties of lib/late-notice.js that nothing in this file's names would protect: amd_result
// PATCHes with no answered_at filter (last observation wins, every detection recorded) and
// answered_at PATCHes with `&answered_at=is.null` (a latch — the first human proof wins and a later
// delivery cannot rewrite the moment the user picked up). This is a characterization test: it is not
// driving a change, it is standing guard over the two properties the retry now depends on, so that
// removing either one fails HERE instead of quietly double-writing production rows.
test("a redelivered detection records once more and never rewrites answered_at", async () => {
  const patches = [];
  const supabase = (url, init) => {
    patches.push({ url, body: JSON.parse(init.body) });
    // Delivery 1 finds Supabase down (its amd_result write is patch #1); everything after is healthy,
    // which is exactly the sequence the 5xx exists to make possible.
    if (patches.length === 1) return response(500, {});
    return response(200, [{ event_key: WAKE_EVENT_KEY }]);
  };
  const first = await postSignedAmdEvent({ clientState: wakeClientState, result: "human", supabase });
  const second = await postSignedAmdEvent({ clientState: wakeClientState, result: "human", supabase });

  assert.equal(first.status >= 500 && first.status < 600, true, `expected 5xx, got ${first.status}`);
  assert.equal(second.status, 200);
  assert.equal(second.text, "answered");

  // Each delivery writes both columns: amd_result first, then answered_at for a human. Only the
  // second delivery's amd_result actually lands, so a detection lost to an outage ends up recorded
  // exactly once — the whole point of declining the first one.
  const amdPatches = patches.filter((p) => "amd_result" in p.body);
  const answeredPatches = patches.filter((p) => "answered_at" in p.body);
  assert.equal(amdPatches.length, 2);
  assert.equal(answeredPatches.length, 2);

  // THE LOAD-BEARING PAIR. Every answered_at write must carry the is.null latch, or the redelivery we
  // just asked for would overwrite the real pickup time with the retry's clock — the user "answered"
  // minutes after they actually did, and every downstream ETA computed from it is wrong.
  for (const patch of answeredPatches) {
    assert.match(patch.url, /answered_at=is\.null/,
      "answered_at must be latched so a resent event cannot rewrite the first human proof");
  }
  // And amd_result must NOT be latched, for the opposite reason: it is the record of what AMD said,
  // so a retry has to be able to land it. Filtering it would make the redelivery match zero rows and
  // turn every recovered outage into a permanent NULL.
  for (const patch of amdPatches) {
    assert.equal(/answered_at=is\.null/.test(patch.url), false,
      "amd_result must stay unfiltered so a resent event can still be recorded");
  }
});
