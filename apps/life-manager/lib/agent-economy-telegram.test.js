"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { financialRecordId } = require("./financial-record-contract.js");
const { renderFinancialTransition, deliverFinancialTransition } = require("./agent-economy-telegram.js");

function record(overrides = {}) {
  const subject = "tenant-a";
  const key = "economic:event:1";
  return { schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subject, key), subject_id: subject, scope: "business",
    kind: "business_revenue", direction: "credit", amount_minor: 1200000, currency: "USDC",
    occurred_at: "2026-09-11T04:00:00Z", recorded_at: "2026-09-11T04:00:01Z",
    idempotency_key: key, source: { provider: "taskmarket", source_type: "marketplace", external_ref: "award-1" },
    verification: { status: "verified", observed_at: "2026-09-11T04:00:01Z",
      evidence_refs: ["taskmarket://receipt/abc"] }, ...overrides };
}

test("renders a concise natural-language revenue or cost transition", () => {
  assert.match(renderFinancialTransition(record()), /収益を受け取りました\n\+USDC 1\.20/);
  const cost = record({ kind: "business_cost", direction: "debit", amount_minor: 2000 });
  assert.match(renderFinancialTransition(cost), /費用を支払いました\n-USDC 0\.002/);
});

test("identical replay sends once and returns the stored provider message id", async () => {
  const deliveries = new Map();
  let sends = 0;
  const deliveryStore = {
    lookup: async ({ eventKey }) => deliveries.get(eventKey) || null,
    claim: async () => ({ claimed: true }),
    markDelivered: async (value) => { deliveries.set(value.eventKey, value); },
  };
  const notify = async () => { sends += 1; return { delivered: true, providerMessageId: 7001 }; };
  const first = await deliverFinancialTransition({ record: record(), deliveryStore, notify,
    now: "2026-09-11T04:01:00Z" });
  const second = await deliverFinancialTransition({ record: record(), deliveryStore, notify,
    now: "2026-09-11T04:02:00Z" });
  assert.equal(first.status, "sent");
  assert.deepEqual([second.status, second.reason, second.providerMessageId], ["quiet", "duplicate", "7001"]);
  assert.equal(sends, 1);
});

test("a raced claim reads delivery proof and an unreceipted send never becomes delivered", async () => {
  const raced = await deliverFinancialTransition({ record: record(), deliveryStore: {
    lookup: async () => ({ provider_message_id: 8001 }), claim: async () => ({ claimed: false }),
    markDelivered: async () => { throw new Error("must not mark"); },
  }, notify: async () => { throw new Error("must not send"); } });
  assert.equal(raced.providerMessageId, "8001");

  const failed = await deliverFinancialTransition({ record: record(), deliveryStore: {
    lookup: async () => null, claim: async () => ({ claimed: true }), markDelivered: async () => {},
  }, notify: async () => ({ delivered: true, providerMessageId: null }) });
  assert.deepEqual([failed.status, failed.delivered], ["failed", false]);
});

test("unverified records and balance snapshots never create money transition messages", async () => {
  assert.throws(() => renderFinancialTransition(record({ verification: {
    status: "unverified", observed_at: "2026-09-11T04:00:01Z", evidence_refs: [],
  } })), /verified/i);
  assert.equal(renderFinancialTransition(record({ kind: "asset_balance", direction: "snapshot", scope: "personal" })), null);
});
