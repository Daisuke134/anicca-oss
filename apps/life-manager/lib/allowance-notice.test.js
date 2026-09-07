"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { COPY, deliverAllowanceNotice } = require("./allowance-notice.js");

function harness(sendResult = { ok: true, result: { message_id: 901 } }) {
  const calls = [];
  const responses = {
    claim_lm_managed_allowance_notice: { kind: "eighty", claimToken: "claim-1", periodStart: "2026-09-01", actionKey: "event-1" },
    record_lm_managed_allowance_notice: true,
    release_lm_managed_allowance_notice: true,
  };
  return {
    calls,
    deps: {
      telegramToken: "token", supaUrl: "https://db.example", supaKey: "key",
      sendMessage: async (_token, _chat, text) => { calls.push(["send", text]); return sendResult; },
      fetchImpl: async (url, init) => {
        const name = String(url).split("/").pop(); calls.push([name, JSON.parse(init.body)]);
        return { ok: true, json: async () => responses[name] };
      },
    },
  };
}

const USER = { uid: "tenant-a", telegram_chat_id: "chat-a", notifications_enabled: true };

test("80% notice is claimed, sent once, and recorded with the provider message id", async () => {
  const h = harness();
  assert.deepEqual(await deliverAllowanceNotice(USER, h.deps), { status: "sent", kind: "eighty", telegramMessageId: 901 });
  assert.deepEqual(h.calls.map((x) => x[0]), ["claim_lm_managed_allowance_notice", "send", "record_lm_managed_allowance_notice"]);
  assert.match(h.calls[1][1], /残り20%/);
  assert.equal(COPY.exhausted.includes("/subscribe"), true);
});

test("ambiguous delivery keeps the claim; explicit rejection releases it", async () => {
  const unknown = harness({ ok: true, result: {} });
  assert.equal((await deliverAllowanceNotice(USER, unknown.deps)).status, "delivery_unknown");
  assert.deepEqual(unknown.calls.map((x) => x[0]), ["claim_lm_managed_allowance_notice", "send"]);
  const rejected = harness({ ok: false });
  assert.equal((await deliverAllowanceNotice(USER, rejected.deps)).status, "send_failed");
  assert.deepEqual(rejected.calls.map((x) => x[0]), ["claim_lm_managed_allowance_notice", "send", "release_lm_managed_allowance_notice"]);
});
