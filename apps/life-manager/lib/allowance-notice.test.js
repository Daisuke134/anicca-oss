"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { noticeCopy, deliverAllowanceNotice } = require("./allowance-notice.js");

function harness(sendResult = { ok: true, result: { message_id: 901 } }) {
  const calls = [];
  const responses = {
    claim_lm_managed_allowance_notice: { kind: "eighty", claimToken: "claim-1", periodStart: "2026-09-01",
      resetAt: "2026-10-01", actionKey: "event-1", used: 24, limit: 30, paid: false },
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
  assert.match(h.calls[1][1], /残り6回/);
  assert.match(h.calls[1][1], /24\/30/);
});

test("notice copy is plan-aware and never asks an active Plus user to subscribe again", () => {
  const free = noticeCopy({ kind: "exhausted", used: 30, limit: 30, paid: false, resetAt: "2026-10-01" });
  const paid = noticeCopy({ kind: "exhausted", used: 500, limit: 500, paid: true, resetAt: "2026-10-01" });
  assert.match(free, /無料利用枠 30\/30/);
  assert.match(free, /\/subscribe/);
  assert.match(paid, /Plus利用枠 500\/500/);
  assert.doesNotMatch(paid, /\/subscribe|無料/);
});

test("ambiguous delivery keeps the claim; explicit rejection releases it", async () => {
  const unknown = harness({ ok: true, result: {} });
  assert.equal((await deliverAllowanceNotice(USER, unknown.deps)).status, "delivery_unknown");
  assert.deepEqual(unknown.calls.map((x) => x[0]), ["claim_lm_managed_allowance_notice", "send",
    "mark_lm_managed_allowance_notice_unknown"]);
  assert.equal(unknown.calls[2][1].p_period_start, "2026-09-01");
  const rejected = harness({ ok: false });
  assert.equal((await deliverAllowanceNotice(USER, rejected.deps)).status, "send_failed");
  assert.deepEqual(rejected.calls.map((x) => x[0]), ["claim_lm_managed_allowance_notice", "send", "release_lm_managed_allowance_notice"]);
  assert.equal(rejected.calls[2][1].p_period_start, "2026-09-01");
});
