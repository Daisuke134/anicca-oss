"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { financialRecordId } = require("../../../runtime/contracts/common-record.cjs");
const { runFinancialManager } = require("./financial-manager-runtime.js");

function record(subjectId, key, amount = 12500) {
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: "business_revenue", direction: "credit",
    amount_minor: amount, currency: "JPY", occurred_at: "2026-09-07T00:00:00.000Z",
    recorded_at: "2026-09-07T00:01:00.000Z", idempotency_key: key,
    source: { provider: "stripe", source_type: "payment_processor", external_ref: key },
    verification: {
      status: "verified", observed_at: "2026-09-07T00:01:00.000Z",
      evidence_refs: [`stripe://payment/${key}`],
    },
  };
}

function store(rows) {
  return { async read({ subjectId }) { return rows.filter((row) => row.subject_id === subjectId); } };
}

function receiptFence() {
  const delivered = new Map();
  return {
    lookup: async ({ digest }) => delivered.get(digest) || null,
    claim: async () => ({ claimed: true }),
    markDelivered: async ({ digest, delivery }) => delivered.set(digest, delivery),
  };
}

test("local and cloud hosts render identical FinancialRecord reports and replay sends zero messages", async () => {
  const rows = [record("tenant-a", "stripe:payment:1")];
  const localMessages = [];
  const cloudMessages = [];
  const common = {
    subjectId: "tenant-a", reportingDate: "2026-09-07", now: "2026-09-07T06:00:00.000Z",
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
  };
  const local = await runFinancialManager({
    ...common, store: store(rows), deliveryStore: receiptFence(),
    notify: async ({ message }) => {
      localMessages.push(message);
      return { delivered: true, providerMessageId: "local-1" };
    },
  });
  const cloudFence = receiptFence();
  const cloudOptions = {
    ...common, store: store(rows), deliveryStore: cloudFence,
    notify: async ({ message }) => {
      cloudMessages.push(message);
      return { delivered: true, providerMessageId: "cloud-1" };
    },
  };
  const cloud = await runFinancialManager(cloudOptions);
  const replay = await runFinancialManager(cloudOptions);

  assert.equal(local.status, "sent");
  assert.equal(cloud.status, "sent");
  assert.equal(local.digest, cloud.digest);
  assert.deepEqual(localMessages, cloudMessages);
  assert.equal(replay.status, "quiet");
  assert.equal(replay.reason, "unchanged");
  assert.equal(cloudMessages.length, 1);
});

test("empty or unverified records are quiet and a tenant cannot read another tenant's records", async () => {
  const messages = [];
  const result = await runFinancialManager({
    subjectId: "tenant-a", reportingDate: "2026-09-07", now: "2026-09-07T06:00:00.000Z",
    store: store([record("tenant-b", "other"), {
      ...record("tenant-a", "unverified"),
      verification: { status: "unverified", observed_at: "2026-09-07T00:01:00.000Z", evidence_refs: [] },
    }]),
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    notify: async () => { messages.push("sent"); return { delivered: true, providerMessageId: "unused" }; },
  });
  assert.equal(result.status, "quiet");
  assert.equal(result.reason, "no_verified_financial_records");
  assert.equal(result.recordCount, 1);
  assert.deepEqual(messages, []);
});

test("a missing provider receipt fails closed after the shared render", async () => {
  const result = await runFinancialManager({
    subjectId: "tenant-a", reportingDate: "2026-09-07", now: "2026-09-07T06:00:00.000Z",
    store: store([record("tenant-a", "stripe:payment:1")]),
    ingest: async () => ({ observed: 0, created: 0, sources: {} }),
    notify: async () => ({ delivered: true, providerMessageId: null }),
  });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "telegram_provider_receipt_missing");
});
