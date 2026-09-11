"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { persistWakeEconomicRecords, persistTaskMarketRevenue, taskMarketFinancialRecords } = require("./agent-economy-economic-records.js");

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

test("projects verified TaskMarket gross and fee without net double counting", () => {
  const records = taskMarketFinancialRecords({ entry_key: "taskmarket:task:tx:1:income",
    amount_atomic: "4750000", occurred_at: "2026-09-11T04:00:00.000Z", tx_hash: `0x${"a".repeat(64)}`,
    meta: { gross_atomic: "5000000", platform_fee_atomic: "250000" } }, "tenant-a", "2026-09-11T05:00:00.000Z");
  assert.deepEqual(records.map((row) => [row.kind, row.direction, row.amount_minor]), [
    ["business_revenue", "credit", 5000000], ["fee", "debit", 250000],
  ]);
  assert.equal(new Set(records.map((row) => row.idempotency_key)).size, 2);
});

test("reuses the official TaskMarket verifier and writes its records to the shared store", async () => {
  const appended = [];
  const identity = { tenant_id: "tenant-a", wallet: { address: `0x${"b".repeat(40)}` } };
  const result = await persistTaskMarketRevenue({ identity,
    financialStore: { append: async (record) => { appended.push(record); return { created: true, record }; } },
    recordedAt: "2026-09-11T05:00:00.000Z", selfWallets: [`0x${"c".repeat(40)}`],
    recordTaskMarket: async (deps) => {
      assert.deepEqual(deps.selfWallets, [`0x${"c".repeat(40)}`, identity.wallet.address]);
      await deps.recordEntry({ entry_key: "taskmarket:task:tx:1:income", amount_atomic: "4750000",
        occurred_at: "2026-09-11T04:00:00.000Z", tx_hash: `0x${"d".repeat(64)}`,
        meta: { gross_atomic: "5000000", platform_fee_atomic: "250000" } });
      return { recorded: 1 };
    } });
  assert.equal(result.recorded, 1);
  assert.deepEqual(appended.map((row) => row.kind), ["business_revenue", "fee"]);
});

test("official TaskMarket readback plus finalized Base transfer reaches the shared store", async () => {
  const worker = `0x${"b".repeat(40)}`;
  const requester = `0x${"e".repeat(40)}`;
  const task = `0x${"a".repeat(64)}`;
  const tx = `0x${"d".repeat(64)}`;
  const topic = (address) => `0x${address.slice(2).padStart(64, "0")}`;
  const award = { workerAddress: worker, workerAgentId: null, rank: 1,
    grossAmount: "5000000", workerPayment: "4750000", platformFee: "250000",
    settlementTxHash: tx, settledAt: "2026-09-11T04:00:00.000Z" };
  const appended = [];
  const fetchImpl = async (url, init = {}) => {
    if (String(url).includes("/api/submissions/mine")) {
      return { ok: true, status: 200, text: async () => JSON.stringify([{ taskId: task }]) };
    }
    if (String(url).includes(`/api/tasks/${task}`)) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ id: task, requester,
        status: "completed", selfAward: false, awardCount: 1, awards: [award] }) };
    }
    const request = JSON.parse(init.body);
    const result = request.method === "eth_chainId" ? "0x2105"
      : request.method === "eth_getBlockByNumber" ? { number: "0x100" }
        : { status: "0x1", transactionHash: tx, blockNumber: "0xff", logs: [{
          address: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", topics: [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            topic(requester), topic(worker),
          ], data: `0x${BigInt(4750000).toString(16)}`,
        }] };
    return { ok: true, json: async () => ({ result }) };
  };
  const result = await persistTaskMarketRevenue({ identity: { tenant_id: "tenant-a", wallet: { address: worker } },
    financialStore: { append: async (record) => { appended.push(record); return { created: true, record }; } },
    recordedAt: "2026-09-11T05:00:00.000Z", selfWallets: [`0x${"c".repeat(40)}`], fetchImpl });
  assert.equal(result.recorded, 1);
  assert.deepEqual(appended.map((row) => [row.kind, row.amount_minor]), [
    ["business_revenue", 5000000], ["fee", 250000],
  ]);
});
