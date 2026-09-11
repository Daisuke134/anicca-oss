"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { financialRecordId } = require("../../../runtime/contracts/common-record.cjs");
const { createJsonlFinancialRecordStore } = require("../lib/financial-record-store.js");
const { agentReceiptPathsFromEnv, runHourlyCfo } = require("./cfo-hourly-local.js");

function revenue(overrides = {}) {
  const subjectId = overrides.subject_id || "dais-local";
  const key = overrides.idempotency_key || "stripe:payment:1";
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: "business_revenue", direction: "credit",
    amount_minor: 12500, currency: "JPY",
    occurred_at: "2026-09-07T00:00:00.000Z",
    recorded_at: "2026-09-07T00:01:00.000Z",
    idempotency_key: key,
    source: { provider: "stripe", source_type: "payment_processor", external_ref: "payment-1" },
    verification: {
      status: "verified", observed_at: "2026-09-07T00:01:00.000Z",
      evidence_refs: ["stripe://payment/payment-1"],
    },
    ...overrides,
  };
}

test("CFO reports verified records once and stays quiet on exact replay", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({
    directoryPath: path.join(stateDir, "financial-records"),
  });
  await store.append(revenue());
  const deliveries = [];
  const options = {
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    now: () => new Date("2026-09-07T06:00:00.000Z"),
    notify: async (input) => {
      deliveries.push(input);
      return { delivery: "delivered", provider_message_id: "telegram-1" };
    },
  };

  const first = await runHourlyCfo(options);
  assert.equal(first.status, "sent");
  assert.equal(first.recordCount, 1);
  assert.equal(deliveries.length, 1);
  assert.match(deliveries[0].message, /事業（今日）\n収益：¥12,500/);
  assert.match(deliveries[0].message, /事業（直近7日）\n収益：¥12,500/);
  assert.match(deliveries[0].message, /事業（2026-09）\n収益：¥12,500/);
  assert.match(deliveries[0].message, /収益内訳（今月・プロバイダー別）\nstripe\n収益：¥12,500/);
  assert.match(deliveries[0].message, /根拠プロバイダー：stripe/);
  assert.doesNotMatch(deliveries[0].message, /個人資産/);
  await store.append(revenue({
    idempotency_key: "stripe:unverified:2",
    record_id: financialRecordId("dais-local", "stripe:unverified:2"),
    verification: {
      status: "unverified", observed_at: "2026-09-07T01:01:00.000Z",
      evidence_refs: [],
    },
  }));
  assert.equal((await runHourlyCfo(options)).status, "quiet");
  assert.equal(deliveries.length, 1);
});

test("CFO sends at most one consolidated snapshot per local reporting day", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-daily-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(stateDir, "financial-records") });
  await store.append(revenue());
  const deliveries = [];
  const base = {
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    notify: async (input) => (deliveries.push(input),
      { delivery: "delivered", provider_message_id: String(deliveries.length) }),
  };
  assert.equal((await runHourlyCfo({ ...base, now: () => new Date("2026-09-07T06:00:00Z") })).status, "sent");
  await store.append(revenue({
    idempotency_key: "stripe:payment:2",
    record_id: financialRecordId("dais-local", "stripe:payment:2"),
    source: { provider: "stripe", source_type: "payment_processor", external_ref: "payment-2" },
  }));
  assert.equal((await runHourlyCfo({ ...base, now: () => new Date("2026-09-07T10:00:00Z") })).status, "quiet");
  assert.equal((await runHourlyCfo({ ...base, now: () => new Date("2026-09-08T06:00:00Z") })).status, "sent");
  assert.equal(deliveries.length, 2);
  assert.equal(deliveries[0].eventKey, "cfo:dais-local:2026-09-07");
  assert.equal(deliveries[1].eventKey, "cfo:dais-local:2026-09-08");
});

test("CFO recognizes the previous snapshot format and does not resend on release day", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-legacy-snapshot-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(stateDir, "financial-records") });
  await store.append(revenue());
  fs.writeFileSync(path.join(stateDir, "last-delivered-snapshot.json"), JSON.stringify({
    schemaVersion: 1, digest: "a".repeat(64), report: { reportingDate: "2026-09-07" },
  }));
  let sends = 0;
  const result = await runHourlyCfo({
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    now: () => new Date("2026-09-07T10:00:00Z"),
    notify: async () => { sends += 1; return { delivery: "delivered", provider_message_id: "new" }; },
  });
  assert.equal(result.status, "quiet");
  assert.equal(sends, 0);
});

test("CFO freezes and retries the first same-day snapshot after a pre-send failure", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-pending-snapshot-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(stateDir, "financial-records") });
  await store.append(revenue());
  const messages = [];
  let attempt = 0;
  const base = {
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    notify: async ({ message }) => {
      messages.push(message);
      attempt += 1;
      return attempt === 1
        ? { delivery: "pending", provider_message_id: null, attempted: 0 }
        : { delivery: "delivered", provider_message_id: "recovered", attempted: 1 };
    },
  };
  assert.equal((await runHourlyCfo({ ...base, now: () => new Date("2026-09-07T06:00:00Z") })).status, "failed");
  await store.append(revenue({
    idempotency_key: "stripe:payment:after-failure",
    record_id: financialRecordId("dais-local", "stripe:payment:after-failure"),
    amount_minor: 99999,
    source: { provider: "stripe", source_type: "payment_processor", external_ref: "after-failure" },
  }));
  assert.equal((await runHourlyCfo({ ...base, now: () => new Date("2026-09-07T10:00:00Z") })).status, "sent");
  assert.equal(messages.length, 2);
  assert.equal(messages[1], messages[0]);
});

test("CFO suppresses no-data and unverified-only Telegram noise", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({
    directoryPath: path.join(stateDir, "financial-records"),
  });
  await store.append(revenue({
    verification: {
      status: "unverified", observed_at: "2026-09-07T00:01:00.000Z",
      evidence_refs: [],
    },
  }));
  let called = false;
  const result = await runHourlyCfo({
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    now: () => new Date("2026-09-07T06:00:00.000Z"),
    notify: async () => { called = true; },
  });

  assert.deepEqual(result, {
    status: "quiet", reason: "no_verified_financial_records",
    reportingDate: "2026-09-07", recordCount: 1, delivered: false,
    ingestion: { observed: 0, created: 0, sources: {} },
  });
  assert.equal(called, false);
});

test("CFO excludes old business receipts and does not report historical data as this month", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({
    directoryPath: path.join(stateDir, "financial-records"),
  });
  await store.append(revenue({ occurred_at: "2026-08-31T14:59:59.000Z" }));
  let called = false;
  const result = await runHourlyCfo({
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    now: () => new Date("2026-09-07T06:00:00.000Z"),
    notify: async () => { called = true; },
  });
  assert.equal(result.status, "quiet");
  assert.equal(result.reason, "no_verified_financial_records");
  assert.equal(called, false);
});

test("CFO fails closed when the shared outbox cannot prove delivery", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({
    directoryPath: path.join(stateDir, "financial-records"),
  });
  await store.append(revenue());

  const result = await runHourlyCfo({
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    now: () => new Date("2026-09-07T06:00:00.000Z"),
    notify: async () => ({ delivery: "delivery_uncertain", provider_message_id: null }),
  });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "telegram_delivery_uncertain");
});

test("CFO does not send a stale partial report when a source is unavailable", async (t) => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cfo-"));
  t.after(() => fs.rmSync(stateDir, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({
    directoryPath: path.join(stateDir, "financial-records"),
  });
  await store.append(revenue());
  let called = false;
  const result = await runHourlyCfo({
    stateDir, subjectId: "dais-local", store,
    ingest: async () => ({
      observed: 0, created: 0,
      sources: { moneytree: "unavailable", agentEconomy: "observed_verified" },
    }),
    now: () => new Date("2026-09-07T06:00:00.000Z"),
    notify: async () => { called = true; },
  });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "financial_source_unavailable");
  assert.deepEqual(result.unavailableSources, ["moneytree"]);
  assert.equal(called, false);
});

test("CFO defaults Agent Economy to its portable Life Manager state and accepts overrides", () => {
  assert.deepEqual(agentReceiptPathsFromEnv({}), [
    path.join(os.homedir(), ".local/state/life-manager/agent-economy/revenue-receipts.jsonl"),
  ]);
  assert.deepEqual(agentReceiptPathsFromEnv({
    LM_AGENT_ECONOMY_STATE_ROOT: "/agent-state",
  }), ["/agent-state/revenue-receipts.jsonl"]);
  assert.deepEqual(agentReceiptPathsFromEnv({
    REVENUE_RECEIPT_JOURNAL: "/state/revenue.jsonl",
  }), ["/state/revenue.jsonl"]);
});
