"use strict";

const crypto = require("node:crypto");
const { projectFinancialRecord } = require("./financial-record-contract.js");

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

function summarizeBusiness(records) {
  const revenue = new Map();
  const costs = new Map();
  const payouts = new Map();
  for (const record of records) {
    if (record.kind === "business_revenue") add(revenue, record.currency, record.amount_minor);
    if (["business_cost", "fee", "tax"].includes(record.kind)) {
      add(costs, record.currency, record.amount_minor);
    }
    if (record.kind === "payout") add(payouts, record.currency, record.amount_minor);
  }
  const profit = new Map(revenue);
  for (const [currency, amount] of costs) add(profit, currency, -amount);
  return {
    revenue: sortedAmounts(revenue), costs: sortedAmounts(costs),
    profit: sortedAmounts(profit), payouts: sortedAmounts(payouts),
  };
}

function inRange(records, start, end) {
  return records.filter((record) => {
    const occurred = Date.parse(record.occurred_at);
    return occurred >= start && occurred < end;
  });
}

function addDays(key, days) {
  const [year, month, day] = key.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

function zonedMidnight(key, timezone) {
  const [year, month, day] = key.split("-").map(Number);
  const wallUtc = Date.UTC(year, month - 1, day);
  let instant = wallUtc;
  for (let pass = 0; pass < 2; pass += 1) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en", {
      timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date(instant)).filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]));
    const represented = Date.UTC(
      Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour), Number(parts.minute), Number(parts.second),
    );
    instant = wallUtc - (represented - instant);
  }
  return instant;
}

function buildFinancialManagerReport(rawRecords, reportingDate, { timezone = "Asia/Tokyo" } = {}) {
  const records = rawRecords.map(projectFinancialRecord);
  const verified = records.filter((record) => record.verification.status === "verified");
  const month = reportingDate.slice(0, 7);
  const [year, monthNumber] = month.split("-").map(Number);
  const monthStart = zonedMidnight(`${month}-01`, timezone);
  const nextYear = monthNumber === 12 ? year + 1 : year;
  const nextMonth = monthNumber === 12 ? 1 : monthNumber + 1;
  const monthEnd = zonedMidnight(
    `${String(nextYear).padStart(4, "0")}-${String(nextMonth).padStart(2, "0")}-01`, timezone,
  );
  const verifiedBusiness = verified.filter((record) => record.scope === "business");
  const currentBusiness = inRange(verifiedBusiness, monthStart, monthEnd);
  const dayStart = zonedMidnight(reportingDate, timezone);
  const dayEnd = zonedMidnight(addDays(reportingDate, 1), timezone);
  const sevenDayStartLabel = addDays(reportingDate, -6);
  const sevenDayStart = zonedMidnight(sevenDayStartLabel, timezone);
  const balances = newestBalances(verified);
  const assets = new Map();
  const liabilities = new Map();
  for (const record of balances) {
    add(record.kind === "asset_balance" ? assets : liabilities, record.currency, record.amount_minor);
  }
  const included = [...balances, ...currentBusiness];
  const providers = [...new Set(included.map((record) => record.source.provider))].sort();
  const netWorth = new Map(assets);
  for (const [currency, amount] of liabilities) add(netWorth, currency, -amount);
  const monthSummary = summarizeBusiness(currentBusiness);
  const byProvider = [...new Set(currentBusiness.map((record) => record.source.provider))]
    .sort()
    .map((provider) => ({
      provider,
      ...summarizeBusiness(currentBusiness.filter((record) => record.source.provider === provider)),
    }));
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
      ...monthSummary,
      today: { period: reportingDate, ...summarizeBusiness(inRange(verifiedBusiness, dayStart, dayEnd)) },
      last7Days: {
        period: { start: sevenDayStartLabel, end: reportingDate },
        ...summarizeBusiness(inRange(verifiedBusiness, sevenDayStart, dayEnd)),
      },
      byProvider,
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

function hasAmounts(summary) {
  return ["revenue", "costs", "profit", "payouts"].some((key) => summary[key].length > 0);
}

function businessLines(label, summary) {
  if (!hasAmounts(summary)) return [];
  return [
    label,
    ...(summary.revenue.length ? [rows("収益", summary.revenue)] : []),
    ...(summary.costs.length ? [rows("コスト", summary.costs)] : []),
    ...(summary.profit.length ? [rows("利益", summary.profit)] : []),
    ...(summary.payouts.length ? [rows("入金移動（収益に重複計上しない）", summary.payouts)] : []),
  ];
}

function renderFinancialManagerTelegram(report) {
  const lines = ["💰 Financial Manager"];
  if (report.personal.assets.length || report.personal.liabilities.length) {
    lines.push(
      "", "個人資産",
      ...(report.personal.assets.length ? [rows("資産", report.personal.assets)] : []),
      ...(report.personal.liabilities.length ? [rows("負債", report.personal.liabilities)] : []),
      ...(report.personal.netWorth.length ? [rows("純資産", report.personal.netWorth)] : []),
    );
  }
  lines.push(...businessLines("\n事業（今日）", report.business.today));
  lines.push(...businessLines("\n事業（直近7日）", report.business.last7Days));
  lines.push(...businessLines(`\n事業（${report.business.period}）`, report.business));
  const revenueProviders = report.business.byProvider.filter((item) => item.revenue.length);
  if (revenueProviders.length) {
    lines.push("\n収益内訳（今月・プロバイダー別）");
    for (const item of revenueProviders) {
      lines.push(item.provider, rows("収益", item.revenue));
    }
  }
  lines.push(
    "",
    `確認済み記録：${report.verifiedRecordCount}件`,
    `未確認のため合計から除外：${report.excludedRecordCount}件`,
    `根拠プロバイダー：${report.providers.join("、") || "なし"}`,
  );
  return lines.join("\n");
}

module.exports = { buildFinancialManagerReport, renderFinancialManagerTelegram };
