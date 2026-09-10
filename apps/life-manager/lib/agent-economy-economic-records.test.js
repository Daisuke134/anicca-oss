"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { persistWakeEconomicRecords } = require("./agent-economy-economic-records.js");

test("persists only receipt-backed TaskMarket cost for the current wake", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-ae-economic-"));
  const ledger = path.join(home, "state", "skills", "earn", "earn-ledger.jsonl");
  fs.mkdirSync(path.dirname(ledger), { recursive: true });
  fs.writeFileSync(ledger, [
    { ts: 1789100000, wake: "wanted", source: "taskmarket_work_attempt", cost_usdc: 0.065, payment_receipt_id: "blockrun:paid-1" },
    { ts: 1789100001, wake: "wanted", source: "taskmarket_work_attempt", cost_usdc: 0.1 },
    { ts: 1789100002, wake: "other", source: "taskmarket_work_attempt", cost_usdc: 0.2, payment_receipt_id: "blockrun:paid-2" },
  ].map(JSON.stringify).join("\n") + "\n");
  const appended = [];
  const records = await persistWakeEconomicRecords({ instanceHome: home, wakeId: "wanted",
    subjectId: "tenant-a", financialStore: { append: async (row) => appended.push(row) },
    recordedAt: "2026-09-11T05:00:00.000Z" });
  assert.equal(records.length, 1);
  assert.equal(appended.length, 1);
  assert.deepEqual([records[0].kind, records[0].direction, records[0].amount_minor, records[0].currency],
    ["business_cost", "debit", 65000, "USDC"]);
  assert.equal(records[0].verification.status, "verified");
  assert.match(records[0].verification.evidence_refs[0], /^x402:\/\/receipt\/[0-9a-f]{64}$/);
});

test("ignores a missing ledger", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-ae-economic-empty-"));
  let calls = 0;
  const records = await persistWakeEconomicRecords({ instanceHome: home, wakeId: "wake",
    subjectId: "tenant-a", financialStore: { append: async () => { calls += 1; } },
    recordedAt: "2026-09-11T05:00:00.000Z" });
  assert.deepEqual(records, []);
  assert.equal(calls, 0);
});
