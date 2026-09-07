"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createJsonlFinancialRecordStore } = require("./financial-record-store.js");
const { ingestFinancialRecords } = require("./financial-manager-ingest.js");
const { createMoneytreeObservationStore } = require("./moneytree-observation-store.js");
const { MONEYTREE_OBSERVATION } = require("./moneytree-local-adapter.js");

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
  assert.equal(transactionCalls, 1);
});

test("authenticated Moneytree reads are verified only after immutable evidence is stored", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-financial-ingest-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(root, "records") });
  const provenance = (tool, digest) => ({
    provider: "moneytree", mcp_server: "codex_apps", tool,
    retrieved_at: "2026-09-07T02:00:00.000Z", payload_sha256: digest.repeat(64),
  });
  const observed = (records, observation) => {
    Object.defineProperty(records, MONEYTREE_OBSERVATION, { value: observation });
    return records;
  };
  let observedAt = "2026-09-07T02:00:00.000Z";
  const evidenceStore = createMoneytreeObservationStore({ directoryPath: path.join(root, "evidence") });
  const readAccounts = async () => observed([{
      id: "moneytree:a1", source: "moneytree", source_ref: `moneytree:${"a".repeat(64)}`,
      name: "Moneytree account", kind: "bank", balance_jpy: 5000,
      observed_at: observedAt,
    }], { ...provenance("moneytree.show-accounts", "a"), retrieved_at: observedAt });
  const readTransactions = async () => observed([{
    id: "moneytree:t1", source_ref: `moneytree:${"b".repeat(64)}`,
    account_id: "moneytree:a1", amount_jpy: -100, occurred_at: "2026-09-06T00:00:00.000Z",
    merchant: "Shop", category: "Food",
  }], { ...provenance("moneytree.show-transactions", "b"), retrieved_at: observedAt });
  const result = await ingestFinancialRecords({
    store, subjectId: "tenant-1", now: new Date(observedAt),
    moneytreeEvidenceStore: evidenceStore,
    readMoneytreeAccounts: readAccounts, readMoneytreeTransactions: readTransactions,
  });
  assert.equal(result.sources.moneytree, "observed_verified");
  const first = await store.read({ subjectId: "tenant-1" });
  const balance = first.find((record) => record.kind === "asset_balance");
  const transaction = first.find((record) => record.kind === "personal_expense");
  assert.equal(balance.verification.status, "verified");
  assert.match(balance.verification.evidence_refs[0], /^moneytree-observation:\/\/sha256\/[a-f0-9]{64}$/);
  assert.equal(transaction.verification.status, "unverified");
  assert.deepEqual(transaction.verification.evidence_refs, []);
  assert.equal(fs.readdirSync(path.join(root, "evidence")).length, 1);

  observedAt = "2026-09-07T03:00:00.000Z";
  const replay = await ingestFinancialRecords({
    store, subjectId: "tenant-1", now: new Date(observedAt),
    moneytreeEvidenceStore: evidenceStore,
    readMoneytreeAccounts: readAccounts, readMoneytreeTransactions: readTransactions,
  });
  assert.equal(replay.sources.moneytree, "observed_verified");
  assert.equal(replay.created, 1);
  assert.equal((await store.read({ subjectId: "tenant-1" })).length, 3);
});

test("Moneytree snapshot helper preserves its two-array return contract", async () => {
  const { readMoneytreeSnapshot } = require("./financial-manager-ingest.js");
  const result = await readMoneytreeSnapshot(async () => ["account"], async () => ["transaction"], {});
  assert.deepEqual(result, [["account"], ["transaction"]]);
});

test("Moneytree snapshot starts only one app-server read at a time", async () => {
  const { readMoneytreeSnapshot } = require("./financial-manager-ingest.js");
  let active = 0;
  let maximum = 0;
  const read = (value) => async () => {
    active += 1;
    maximum = Math.max(maximum, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    return [value];
  };
  assert.deepEqual(await readMoneytreeSnapshot(read("account"), read("transaction"), {}), [
    ["account"], ["transaction"],
  ]);
  assert.equal(maximum, 1);
});
