"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createAgentEconomyControlStore, economyReply } = require("./agent-economy-control.js");

test("status is a tenant-scoped projection of the citizen, latest job, and immutable receipt", async () => {
  const calls = [];
  const store = createAgentEconomyControlStore({ query: async (sql, params) => {
    calls.push({ sql, params });
    return { rows: [{
      wallet_address: `0x${"a".repeat(40)}`, agent_economy_paused_at: null,
      job_id: "j2", job_status: "queued",
      input_refs: { cycle_ref: "agent-economy-cycle://tenant-a/2" },
      available_at: "2026-09-11T00:10:00.000Z", receipt_outcome: "completed",
      receipt: { kind: "agent_economy_wake", cycle: 1, wake: { profitable: false } },
    }] };
  } });
  const snapshot = await store.read("tenant-a");
  assert.deepEqual(calls[0].params, ["tenant-a"]);
  assert.match(calls[0].sql, /lm_runtime_job_receipts/);
  assert.equal(snapshot.latestJob.cycle, 2);
  assert.equal(snapshot.latestReceipt.profitable, false);
  assert.equal(economyReply(snapshot).extra.reply_markup.inline_keyboard[0][0].callback_data, "economy:pause");
});

test("pause is idempotent and tenant scoped", async () => {
  const calls = [];
  const store = createAgentEconomyControlStore({ query: async (sql, params) => {
    calls.push({ sql, params });
    return { rows: [{ agent_economy_paused_at: "2026-09-11T00:00:00.000Z" }] };
  } });
  assert.deepEqual(await store.pause("tenant-a"), { pausedAt: "2026-09-11T00:00:00.000Z" });
  assert.deepEqual(calls[0].params, ["tenant-a"]);
  assert.match(calls[0].sql, /COALESCE\(agent_economy_paused_at/);
});

test("the production delta migration adds the durable pause field idempotently", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-09-11-lm-agent-economy-control.sql"), "utf8");
  assert.match(sql, /ADD COLUMN IF NOT EXISTS agent_economy_paused_at timestamptz/);
});

test("an unavailable store is not misreported as incomplete setup", () => {
  assert.match(economyReply(null).text, /unavailable/);
  assert.equal(economyReply(null).extra, undefined);
});
