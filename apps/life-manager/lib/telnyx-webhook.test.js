"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  encodeWakeClientState, encodeTestCallClientState, decodeCallClientState, decodeWakeClientState,
} = require("./telnyx-webhook.js");

test("a wake client_state decodes as kind=wake", () => {
  const encoded = encodeWakeClientState({ wakeUid: "lm_abc", wakeEventKey: "lm_abc|2026-08-02T09:00:00+09:00|10" });
  assert.deepEqual(decodeCallClientState(encoded), {
    kind: "wake", wakeUid: "lm_abc", wakeEventKey: "lm_abc|2026-08-02T09:00:00+09:00|10",
  });
});

test("a wake claim token round-trips without changing the legacy fields", () => {
  const encoded = encodeWakeClientState({
    wakeUid: "lm_abc",
    wakeEventKey: "event-key",
    wakeClaimToken: "claim-token-exact",
  });
  assert.deepEqual(decodeWakeClientState(encoded), {
    wakeUid: "lm_abc",
    wakeEventKey: "event-key",
    wakeClaimToken: "claim-token-exact",
  });
  assert.deepEqual(decodeCallClientState(encoded), {
    kind: "wake",
    wakeUid: "lm_abc",
    wakeEventKey: "event-key",
    wakeClaimToken: "claim-token-exact",
  });
});

test("a managed action key round-trips only when explicitly supplied", () => {
  const state = decodeCallClientState(encodeWakeClientState({
    wakeUid: "lm_abc", wakeEventKey: "wake-key", wakeClaimToken: "claim", managedActionKey: "calendar-event-1",
    managedPeriodStart: "2026-09-01", managedReservationToken: "11111111-1111-4111-8111-111111111111",
  }));
  assert.equal(state.kind, "wake");
  assert.equal(state.managedActionKey, "calendar-event-1");
  assert.equal(state.managedPeriodStart, "2026-09-01");
  assert.equal(state.managedReservationToken, "11111111-1111-4111-8111-111111111111");
  assert.equal(decodeCallClientState(encodeWakeClientState({ wakeUid: "lm_abc", wakeEventKey: "wake-key" })).managedActionKey, undefined);
});

test("a two-field wake blob keeps its exact legacy bytes and decoded shape", () => {
  const legacyBytes = Buffer.from(JSON.stringify({
    wakeUid: "lm_abc",
    wakeEventKey: "event-key",
  }), "utf8").toString("base64");
  assert.equal(encodeWakeClientState({ wakeUid: "lm_abc", wakeEventKey: "event-key" }), legacyBytes);
  assert.deepEqual(decodeWakeClientState(legacyBytes), {
    wakeUid: "lm_abc",
    wakeEventKey: "event-key",
  });
});

test("invalid wake claim tokens are omitted instead of guessed", () => {
  const base = { wakeUid: "lm_abc", wakeEventKey: "event-key" };
  const invalidTokens = [null, 42, "", " \t\n", "x".repeat(513)];
  for (const wakeClaimToken of invalidTokens) {
    const encoded = Buffer.from(JSON.stringify({ ...base, wakeClaimToken }), "utf8").toString("base64");
    assert.deepEqual(decodeWakeClientState(encoded), base, `decoder accepted ${String(wakeClaimToken)}`);
    assert.deepEqual(decodeWakeClientState(encodeWakeClientState({ ...base, wakeClaimToken })), base,
      `encoder retained ${String(wakeClaimToken)}`);
  }
  assert.deepEqual(decodeWakeClientState(Buffer.from(JSON.stringify(base), "utf8").toString("base64")), base);
});

test("a test-call client_state decodes as kind=test", () => {
  const encoded = encodeTestCallClientState({ testUid: "lm_abc" });
  assert.deepEqual(decodeCallClientState(encoded), { kind: "test", testUid: "lm_abc" });
});

test("a test-call client_state is NOT mistaken for a wake row", () => {
  // Mistaking one for the other would send an amd_result PATCH at an lm_wake_log row that does not
  // exist, and every test call would report matched=0 — the same log line that means a real wake row
  // went missing. The two must stay distinguishable or the matched=0 alarm stops meaning anything.
  assert.equal(decodeWakeClientState(encodeTestCallClientState({ testUid: "lm_abc" })), null);
});

test("empty and unreadable client_state stay null", () => {
  assert.equal(decodeCallClientState(""), null);
  assert.equal(decodeCallClientState("not-base64-json"), null);
  assert.equal(decodeCallClientState(Buffer.from(JSON.stringify({ other: 1 }), "utf8").toString("base64")), null);
});

test("encodeTestCallClientState requires a uid", () => {
  // An empty string is what dial.js checks for before it puts client_state on the dial body. Encoding
  // `{}` into a decodable-but-anonymous blob would hand the webhook a test call it cannot name.
  assert.equal(encodeTestCallClientState({}), "");
  assert.equal(encodeTestCallClientState(), "");
});
