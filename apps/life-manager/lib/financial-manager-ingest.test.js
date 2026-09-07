"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createJsonlFinancialRecordStore } = require("./financial-record-store.js");
const { ingestFinancialRecords } = require("./financial-manager-ingest.js");

test("ingestion projects real provider receipts and appends through the common store", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-financial-ingest-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(root, "records") });
  const revenueModule = await import("../../../skills/agent-economy/lib/revenue-receipt.mjs");
  const receipt = revenueModule.normalizeRevenueReceipt({
    provider: "x402", payer: "buyer", recipient: "seller",
    gross: "1.25", fee: "0.05", refund: "0", asset: "USDC",
    terminal_state: "settled", occurred_at: "2026-09-07T01:00:00Z",
    proof: { chain_id: 8453, tx_hash: `0x${"a".repeat(64)}`, log_index: 1, verified: true },
  });
  const result = await ingestFinancialRecords({
    store, subjectId: "tenant-1", now: new Date("2026-09-07T02:00:00Z"),
    readMoneytreeAccounts: async () => [],
    readMoneytreeTransactions: async () => [],
    readAgentReceipts: async () => [receipt],
    readMarketplaceReceipts: async () => [],
    projectMarketplaceReceipts: async () => [],
  });

  assert.deepEqual(result, {
    observed: 2, created: 2,
    sources: { moneytree: "observed_unverified", agentEconomy: "observed_verified", marketplace: "empty" },
  });
  const records = await store.read({ subjectId: "tenant-1" });
  assert.deepEqual(new Set(records.map((row) => row.kind)), new Set(["business_revenue", "fee"]));
  assert.equal((await ingestFinancialRecords({
    store, subjectId: "tenant-1", now: new Date("2026-09-07T03:00:00Z"),
    readMoneytreeAccounts: async () => [], readMoneytreeTransactions: async () => [],
    readAgentReceipts: async () => [receipt], readMarketplaceReceipts: async () => [],
    projectMarketplaceReceipts: async () => [],
  })).created, 0);
});

test("a configured missing journal is unavailable instead of empty revenue", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-financial-ingest-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(root, "records") });
  const result = await ingestFinancialRecords({
    store, subjectId: "tenant-1", now: new Date("2026-09-07T02:00:00Z"),
    readMoneytreeAccounts: async () => [], readMoneytreeTransactions: async () => [],
    agentReceiptPaths: [path.join(root, "missing-revenue-receipts.jsonl")],
  });

  assert.deepEqual(result.sources, {
    moneytree: "observed_unverified", agentEconomy: "unavailable",
    marketplace: "not_configured",
  });
});

test("Moneytree read retries one transient connector startup failure", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-financial-ingest-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(root, "records") });
  let accountCalls = 0;
  let transactionCalls = 0;
  const result = await ingestFinancialRecords({
    store, subjectId: "tenant-1", now: new Date("2026-09-07T02:00:00Z"),
    readMoneytreeAccounts: async () => {
      accountCalls += 1;
      if (accountCalls === 1) throw new Error("connector startup timeout");
      return [];
    },
    readMoneytreeTransactions: async () => { transactionCalls += 1; return []; },
  });

  assert.equal(result.sources.moneytree, "observed_unverified");
  assert.equal(accountCalls, 2);
  assert.equal(transactionCalls, 2);
});
