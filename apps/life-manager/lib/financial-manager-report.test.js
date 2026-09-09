"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { financialRecordId } = require("../../../runtime/contracts/common-record.cjs");
const {
  buildFinancialManagerReport, renderFinancialManagerTelegram,
} = require("./financial-manager-report.js");

function record(key, { kind, amount, occurredAt, provider = "stripe", scope = "business" }) {
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId("tenant-1", key), subject_id: "tenant-1",
    scope, kind,
    direction: kind === "asset_balance" ? "snapshot" : (
      ["business_revenue", "payout"].includes(kind) ? "credit" : "debit"
    ),
    amount_minor: amount, currency: "JPY", occurred_at: occurredAt,
    recorded_at: occurredAt, idempotency_key: key,
    source: {
      provider, source_type: scope === "personal" ? "moneytree" : "payment_processor",
      external_ref: key,
    },
    verification: {
      status: "verified", observed_at: occurredAt,
      evidence_refs: [`${provider}://receipt/${key}`],
    },
  };
}

test("Financial Manager folds daily, seven-day, monthly and provider views into one report", () => {
  const { report } = buildFinancialManagerReport([
    record("today-revenue", { kind: "business_revenue", amount: 1000, occurredAt: "2026-09-07T01:00:00Z" }),
    record("yesterday-cost", { kind: "business_cost", amount: 200, occurredAt: "2026-09-06T01:00:00Z" }),
    record("older-revenue", { kind: "business_revenue", amount: 300, occurredAt: "2026-09-01T01:00:00Z", provider: "gumroad" }),
    record("previous-month", { kind: "business_revenue", amount: 9000, occurredAt: "2026-08-31T14:59:59Z" }),
  ], "2026-09-07");

  assert.deepEqual(report.business.today.revenue, [{ currency: "JPY", amountMinor: 1000 }]);
  assert.deepEqual(report.business.last7Days, {
    period: { start: "2026-09-01", end: "2026-09-07" },
    revenue: [{ currency: "JPY", amountMinor: 1300 }],
    costs: [{ currency: "JPY", amountMinor: 200 }],
    profit: [{ currency: "JPY", amountMinor: 1100 }], payouts: [],
  });
  assert.deepEqual(report.business.revenue, [{ currency: "JPY", amountMinor: 1300 }]);
  assert.deepEqual(report.business.byProvider.map((item) => item.provider), ["gumroad", "stripe"]);
  const text = renderFinancialManagerTelegram(report);
  assert.match(text, /事業（今日）/);
  assert.match(text, /事業（直近7日）/);
  assert.match(text, /事業（2026-09）/);
  assert.match(text, /収益内訳（今月・プロバイダー別）/);
  assert.doesNotMatch(text, /9,000/);
  assert.doesNotMatch(text, /確認済みデータなし/);
});

test("balance-only report omits empty business and liability noise", () => {
  const { report } = buildFinancialManagerReport([
    record("balance", {
      kind: "asset_balance", amount: 5000, occurredAt: "2026-09-07T01:00:00Z",
      provider: "moneytree", scope: "personal",
    }),
  ], "2026-09-07");
  const text = renderFinancialManagerTelegram(report);
  assert.match(text, /個人資産/);
  assert.doesNotMatch(text, /事業（/);
  assert.doesNotMatch(text, /負債/);
  assert.doesNotMatch(text, /確認済みデータなし/);
});

test("Financial Manager uses the tenant timezone at a non-JST day boundary", () => {
  const rows = [record("los-angeles-day", {
    kind: "business_revenue", amount: 500,
    // 23:30 on 2026-08-31 in Los Angeles, but already Sep 1 in Tokyo.
    occurredAt: "2026-09-01T06:30:00.000Z",
  })];
  const { report } = buildFinancialManagerReport(rows, "2026-08-31", {
    timezone: "America/Los_Angeles",
  });
  assert.deepEqual(report.business.today.revenue, [{ currency: "JPY", amountMinor: 500 }]);
  assert.deepEqual(report.business.revenue, [{ currency: "JPY", amountMinor: 500 }]);
});
