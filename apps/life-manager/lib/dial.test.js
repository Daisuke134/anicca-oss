"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { amdDialOptions, placeCall } = require("./dial.js");
const { encodeTestCallClientState, decodeCallClientState } = require("./telnyx-webhook.js");

const WAKE_URL = "wss://life-call-production.up.railway.app/ws?summary=x&wakeUid=lm_abc&wakeEventKey=k1";
const TEST_URL = "wss://life-call-production.up.railway.app/ws?summary=x&wakeUid=&wakeEventKey=";

const CALL_URL = "wss://life-call-production.up.railway.app/ws?summary=x";

function jsonResponse(payload, ok = true, status = ok ? 200 : 500) {
  return { ok, status, async json() { return payload; } };
}

async function withDialTransport(callPayload, run) {
  const savedEnv = {
    TELNYX_API_KEY: process.env.TELNYX_API_KEY,
    TELNYX_CONNECTION_ID: process.env.TELNYX_CONNECTION_ID,
    TELNYX_PHONE_NUMBER: process.env.TELNYX_PHONE_NUMBER,
  };
  const savedFetch = global.fetch;
  const requests = [];
  process.env.TELNYX_API_KEY = "test-api-key";
  process.env.TELNYX_CONNECTION_ID = "test-connection";
  process.env.TELNYX_PHONE_NUMBER = "+99900000000";
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/balance")) return jsonResponse({ data: { balance: "1.00" } });
    assert.equal(url, "https://api.telnyx.com/v2/calls");
    if (callPayload instanceof Error) throw callPayload;
    if (callPayload && callPayload.__status) return jsonResponse(callPayload.body || {}, false, callPayload.__status);
    return jsonResponse(callPayload);
  };
  try {
    const result = await run(requests);
    assert.equal(requests.length, 2, "placeCall must keep the existing balance plus one dial request");
    return result;
  } finally {
    global.fetch = savedFetch;
    for (const [key, value] of Object.entries(savedEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("a wake stream url still derives its client_state from the url", () => {
  // The wake path is the one that already works in production; the test-call fix must not move it.
  const opts = amdDialOptions(WAKE_URL, { LM_AMD: "on" });
  assert.deepEqual(decodeCallClientState(opts.client_state), { kind: "wake", wakeUid: "lm_abc", wakeEventKey: "k1" });
});

test("placeCall distinguishes response loss from an explicit provider rejection", async () => {
  const unknown = await withDialTransport(new Error("socket reset"), () => placeCall({
    to: "+99900000000", streamUrl: CALL_URL,
  }));
  assert.equal(unknown.ok, false);
  assert.equal(unknown.deliveryUnknown, true);
  const serverError = await withDialTransport({ __status: 503 }, () => placeCall({
    to: "+99900000000", streamUrl: CALL_URL,
  }));
  assert.equal(serverError.deliveryUnknown, true);
  const rejection = await withDialTransport({ __status: 422 }, () => placeCall({
    to: "+99900000000", streamUrl: CALL_URL,
  }));
  assert.equal(rejection.deliveryUnknown, false);
});

test("an explicit client_state wins over the url", () => {
  const clientState = encodeTestCallClientState({ testUid: "lm_abc" });
  const opts = amdDialOptions(TEST_URL, { LM_AMD: "on" }, { clientState });
  assert.equal(opts.answering_machine_detection, "detect");
  assert.deepEqual(decodeCallClientState(opts.client_state), { kind: "test", testUid: "lm_abc" });
});

test("without either, AMD still runs but no client_state is sent", () => {
  // A dial body carrying client_state:"" would make Telnyx echo an empty state back and the webhook
  // would have to tell "the caller chose not to say" apart from "the caller said nothing decodable".
  const opts = amdDialOptions(TEST_URL, { LM_AMD: "on" });
  assert.equal(opts.answering_machine_detection, "detect");
  assert.equal("client_state" in opts, false);
});

test("LM_AMD=off disables AMD even with an explicit client_state", () => {
  // The kill switch stays absolute: no AMD means no detection webhook, so a client_state on the dial
  // body would only be a promise of a callback that is never coming.
  const opts = amdDialOptions(TEST_URL, { LM_AMD: "off" }, { clientState: encodeTestCallClientState({ testUid: "lm_abc" }) });
  assert.deepEqual(opts, {});
});

test("LM_AMD=off keeps the lifecycle webhook for a voice allowance owner", () => {
  const { encodeWakeClientState } = require("./telnyx-webhook.js");
  const clientState = encodeWakeClientState({ wakeUid: "lm_abc", wakeEventKey: "call-a",
    wakeClaimToken: "claim", voicePeriodStart: "2026-09-01",
    voiceReservationToken: "11111111-1111-4111-8111-111111111111", voiceAllowedSeconds: 120 });
  const opts = amdDialOptions(WAKE_URL, { LM_AMD: "off" }, { clientState });
  assert.equal(opts.answering_machine_detection, undefined);
  assert.equal(opts.client_state, clientState);
  assert.match(opts.webhook_url, /\/telnyx-events$/);
});

test("placeCall returns all exact Telnyx call identities from one successful response", async () => {
  const ids = {
    call_control_id: "v2:opaque/control+id",
    call_session_id: "session exact/opaque",
    call_leg_id: "leg:opaque+id",
  };
  const result = await withDialTransport({ data: ids }, () => placeCall({
    to: "+99900000000", streamUrl: CALL_URL,
  }));
  assert.deepEqual(result, {
    ok: true,
    ccid: ids.call_control_id,
    callSessionId: ids.call_session_id,
    callLegId: ids.call_leg_id,
  });
});

test("placeCall sends the reserved connected-second ceiling to Telnyx", async () => {
  await withDialTransport({ data: { call_control_id: "voice-budget-call" } }, (requests) => placeCall({
    to: "+99900000000", streamUrl: CALL_URL, timeLimitSeconds: 37,
  }).then((result) => {
    assert.equal(result.ok, true);
    assert.equal(JSON.parse(requests[1].options.body).time_limit_secs, 37);
  }));
});

test("placeCall maps absent or invalid optional Telnyx identities to null", async () => {
  const responses = [
    { data: { call_control_id: "control-only" } },
    { data: { call_control_id: "control-empty", call_session_id: "", call_leg_id: " \t" } },
    { data: { call_control_id: "control-types", call_session_id: 7, call_leg_id: null } },
  ];
  for (const payload of responses) {
    const result = await withDialTransport(payload, () => placeCall({
      to: "+99900000000", streamUrl: CALL_URL,
    }));
    assert.equal(result.ok, true);
    assert.equal(result.callSessionId, null);
    assert.equal(result.callLegId, null);
  }
});

test("placeCall rejects blank, non-string, and oversized mandatory call-control IDs", async () => {
  const invalidIds = ["", " \t", 7, "x".repeat(513)];
  for (const call_control_id of invalidIds) {
    const result = await withDialTransport({ data: { call_control_id } }, () => placeCall({
      to: "+99900000000", streamUrl: CALL_URL,
    }));
    assert.equal(result.ok, false, `accepted ${String(call_control_id)}`);
    assert.equal("ccid" in result, false);
    assert.equal(result.error, "no call_control_id");
    assert.equal(result.deliveryUnknown, true);
  }
});
