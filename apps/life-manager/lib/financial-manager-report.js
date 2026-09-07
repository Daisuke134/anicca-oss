"use strict";

const crypto = require("node:crypto");
const { projectFinancialRecord } = require("../../../runtime/contracts/common-record.cjs");

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonical(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function newestBalances(records) {
  const latest = new Map();
  for (const record of records) {
    if (!["asset_balance", "liability_balance"].includes(record.kind)) continue;
    const key = `${record.source.provider}\n${record.source.external_ref}`;
    const current = latest.get(key);
    if (!current || current.occurred_at < record.occurred_at
      || (current.occurred_at === record.occurred_at
        && current.recorded_at < record.recorded_at)) latest.set(key, record);
  }
  return [...latest.values()];
}

function add(map, currency, value) {
  const next = (map.get(currency) || 0) + value;
  if (!Number.isSafeInteger(next)) throw new Error("Financial Manager total exceeds safe units");
  map.set(currency, next);
}

function sortedAmounts(map) {
  return [...map].sort(([left], [right]) => left.localeCompare(right))
    .map(([currency, amountMinor]) => ({ currency, amountMinor }));
}

function buildFinancialManagerReport(rawRecords, reportingDate) {
  const records = rawRecords.map(projectFinancialRecord);
  const verified = records.filter((record) => record.verification.status === "verified");
  const month = reportingDate.slice(0, 7);
  const [year, monthNumber] = month.split("-").map(Number);
  const monthStart = Date.parse(`${month}-01T00:00:00+09:00`);
  const nextYear = monthNumber === 12 ? year + 1 : year;
  const nextMonth = monthNumber === 12 ? 1 : monthNumber + 1;
  const monthEnd = Date.parse(
    `${String(nextYear).padStart(4, "0")}-${String(nextMonth).padStart(2, "0")}-01T00:00:00+09:00`,
  );
  const currentBusiness = verified.filter((record) => (
    record.scope === "business"
    && Date.parse(record.occurred_at) >= monthStart
    && Date.parse(record.occurred_at) < monthEnd
  ));
  const balances = newestBalances(verified);
  const assets = new Map();
  const liabilities = new Map();
  for (const record of balances) {
    add(record.kind === "asset_balance" ? assets : liabilities, record.currency, record.amount_minor);
  }
  const revenue = new Map();
  const costs = new Map();
  const payouts = new Map();
  for (const record of currentBusiness) {
    if (record.kind === "business_revenue") add(revenue, record.currency, record.amount_minor);
    if (["business_cost", "fee", "tax"].includes(record.kind)) {
      add(costs, record.currency, record.amount_minor);
    }
    if (record.kind === "payout") add(payouts, record.currency, record.amount_minor);
  }
  const included = [...balances, ...currentBusiness];
  const providers = [...new Set(included.map((record) => record.source.provider))].sort();
  const netWorth = new Map(assets);
  for (const [currency, amount] of liabilities) add(netWorth, currency, -amount);
  const net = new Map(revenue);
  for (const [currency, amount] of costs) add(net, currency, -amount);
  const report = {
    schemaVersion: 1,
    reportingDate,
    verifiedRecordCount: included.length,
    excludedRecordCount: records.length - verified.length,
    personal: {
      assets: sortedAmounts(assets), liabilities: sortedAmounts(liabilities),
      netWorth: sortedAmounts(netWorth),
    },
    business: {
      period: month,
      revenue: sortedAmounts(revenue), costs: sortedAmounts(costs),
      profit: sortedAmounts(net), payouts: sortedAmounts(payouts),
    },
    providers,
  };
  const digestReport = { ...report };
  delete digestReport.verifiedRecordCount;
  delete digestReport.excludedRecordCount;
  return {
    report,
    digest: crypto.createHash("sha256").update(canonical(digestReport)).digest("hex"),
  };
}

function money({ currency, amountMinor }) {
  const digits = new Map([
    ["JPY", 0], ["USD", 2], ["EUR", 2], ["GBP", 2], ["USDC", 6], ["USDT", 6],
  ]).get(currency);
  if (digits === undefined) return `${currency} minor ${amountMinor}`;
  const negative = amountMinor < 0;
  const units = BigInt(Math.abs(amountMinor));
  const scale = 10n ** BigInt(digits);
  const whole = (units / scale).toLocaleString("ja-JP");
  const rawFraction = String(units % scale).padStart(digits, "0");
  const fraction = digits === 0 ? "" : `.${rawFraction.replace(/0+$/, "").padEnd(2, "0")}`;
  const formatted = `${negative ? "-" : ""}${whole}${fraction}`;
  return currency === "JPY" ? `¥${formatted}` : `${currency} ${formatted}`;
}

function rows(label, values) {
  return values.length ? values.map((value) => `${label}：${money(value)}`).join("\n") : `${label}：確認済みデータなし`;
}

function renderFinancialManagerTelegram(report) {
  return [
    "💰 Financial Manager",
    "",
    "個人資産",
    rows("資産", report.personal.assets),
    rows("負債", report.personal.liabilities),
    rows("純資産", report.personal.netWorth),
    "",
    "事業",
    rows("事業収益", report.business.revenue),
    rows("事業コスト", report.business.costs),
    rows("事業利益", report.business.profit),
    rows("入金移動（収益に重複計上しない）", report.business.payouts),
    "",
    `集計期間：${report.business.period}`,
    `確認済み記録：${report.verifiedRecordCount}件`,
    `未確認のため合計から除外：${report.excludedRecordCount}件`,
    `根拠プロバイダー：${report.providers.join("、") || "なし"}`,
  ].join("\n");
}

module.exports = { buildFinancialManagerReport, renderFinancialManagerTelegram };
