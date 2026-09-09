"use strict";

const { EXCLUDED_KINDS, normaliseEntry, usdMicrosForEntry } = require("./earnings-ledger.js");
const { usdMicrosFromDecimal } = require("./financial-money.js");
const { computePayout } = require("./payout-policy.js");

const MICROS_PER_MINOR = 10_000n;
const MAX_SAFE_BIGINT = BigInt(Number.MAX_SAFE_INTEGER);
const FORMATTERS = new Map();

function fail(message) {
  throw new Error(message);
}

function validZone(timezone) {
  const zone = String(timezone || "UTC");
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone }).format(0);
    return zone;
  } catch {
    fail(`unknown timezone ${zone}`);
  }
}

function zoneFormatter(timezone) {
  if (!FORMATTERS.has(timezone)) {
    FORMATTERS.set(timezone, new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }));
  }
  return FORMATTERS.get(timezone);
}

function zonedParts(ms, timezone) {
  return Object.fromEntries(zoneFormatter(timezone).formatToParts(new Date(ms))
    .filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
}

function dateKey(ms, timezone) {
  const part = zonedParts(ms, timezone);
  return `${part.year}-${part.month}-${part.day}`;
}

function zonedMidnightMs(key, timezone) {
  const [year, month, day] = key.split("-").map(Number);
  const wallUtc = Date.UTC(year, month - 1, day);
  let instant = wallUtc;
  for (let pass = 0; pass < 2; pass += 1) {
    const part = zonedParts(instant, timezone);
    const represented = Date.UTC(
      Number(part.year),
      Number(part.month) - 1,
      Number(part.day),
      Number(part.hour),
      Number(part.minute),
      Number(part.second),
    );
    instant = wallUtc - (represented - instant);
  }
  return instant;
}

function addCalendarDays(key, days) {
  const [year, month, day] = key.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

function isoWeekKey(mondayKey) {
  const [year, month, day] = mondayKey.split("-").map(Number);
  const monday = new Date(Date.UTC(year, month - 1, day));
  const thursday = new Date(monday.getTime() + 3 * 86400000);
  const weekYear = thursday.getUTCFullYear();
  const firstThursday = new Date(Date.UTC(weekYear, 0, 4));
  const firstDay = firstThursday.getUTCDay() || 7;
  const firstMonday = new Date(firstThursday.getTime() - (firstDay - 1) * 86400000);
  const week = Math.floor((monday - firstMonday) / (7 * 86400000)) + 1;
  return `${weekYear}-W${String(week).padStart(2, "0")}`;
}

function periodBounds({ kind, nowMs, timezone = "UTC" } = {}) {
  if (kind !== "daily" && kind !== "weekly") fail(`unknown financial report kind ${kind}`);
  const now = Number(nowMs);
  if (!Number.isFinite(now)) fail("financial report nowMs must be an instant");
  const zone = validZone(timezone);
  const today = dateKey(now, zone);
  let startKey = today;
  let periodKey = today;
  if (kind === "weekly") {
    const [year, month, day] = today.split("-").map(Number);
    const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay() || 7;
    startKey = addCalendarDays(today, -(weekday - 1));
    periodKey = isoWeekKey(startKey);
  }
  return {
    period_key: periodKey,
    period_start: new Date(zonedMidnightMs(startKey, zone)).toISOString(),
    period_end: new Date(now).toISOString(),
  };
}

function nonNegativeAtomic(value) {
  const raw = typeof value === "bigint"
    ? value.toString()
    : String(value == null ? "" : value).trim();
  if (!/^\d+$/.test(raw)) fail("a measured Base USDC balance is required");
  return raw;
}

function railName(source) {
  if (source === "x402_sale") return "SELL";
  if (source === "x402_work") return "WORK";
  if (source === "taskmarket_work") return "WORK";
  if (source === "polymarket") return "CAPITAL";
  return "UNCLASSIFIED";
}

function emptyMoney() {
  return { gross: 0n, loss: 0n, fee: 0n, transfer: 0n };
}

function aggregateEarnings(rows, { walletAddress, startMs, endMs }) {
  const totals = emptyMoney();
  const rails = new Map();
  let excluded = 0;
  for (const candidate of Array.isArray(rows) ? rows : []) {
    const row = normaliseEntry(candidate);
    if (row.wallet_address !== walletAddress) fail("an earnings row belongs to another wallet");
    if (row.currency !== "USD") fail("financial reports require USD ledger rows");
    const occurredAt = Date.parse(row.occurred_at);
    if (occurredAt < startMs || occurredAt >= endMs) continue;
    if (EXCLUDED_KINDS.has(row.kind)) {
      excluded += 1;
      continue;
    }
    const amount = usdMicrosForEntry(row);
    const rail = railName(row.source);
    const item = rails.get(rail) || emptyMoney();
    if (row.kind === "financial_external_income") {
      totals.gross += amount;
      item.gross += amount;
    }
    if (row.kind === "financial_realized_loss") {
      totals.loss += amount;
      item.loss += amount;
    }
    if (row.kind === "financial_fee") {
      totals.fee += amount;
      item.fee += amount;
    }
    if (row.kind === "financial_user_transfer") {
      totals.transfer += amount;
      item.transfer += amount;
    }
    rails.set(rail, item);
  }
  return { totals, rails, excluded };
}

function aggregateCosts(rows, { startMs, endMs }) {
  let total = 0n;
  for (const row of Array.isArray(rows) ? rows : []) {
    const at = Date.parse(String(row && row.ts));
    if (!Number.isFinite(at) || at < startMs || at >= endMs) continue;
    total += usdMicrosFromDecimal(row.est_usd == null ? "0" : row.est_usd);
  }
  return total;
}

function allTimeCostMicros(rows, endMs) {
  return aggregateCosts(rows, { startMs: Number.NEGATIVE_INFINITY, endMs });
}

function safeBps(numerator, denominator) {
  const value = (numerator * 10_000n) / denominator;
  if (value > MAX_SAFE_BIGINT) fail("self-funded ratio is outside the supported range");
  return Number(value);
}

function buildFinancialSnapshot(input = {}) {
  const {
    kind,
    nowMs,
    timezone = "UTC",
    walletAddress,
    earningsRows = [],
    costRows = [],
    allTimeEarningsRows = earningsRows,
    allTimeCostRows = costRows,
  } = input;
  const balance = nonNegativeAtomic(input.onchainUsdcAtomic);
  const bounds = periodBounds({ kind, nowMs, timezone });
  const startMs = Date.parse(bounds.period_start);
  const endMs = Date.parse(bounds.period_end);
  const period = aggregateEarnings(earningsRows, { walletAddress, startMs, endMs });
  const apiCost = aggregateCosts(costRows, { startMs, endMs });
  const verifiedNet = period.totals.gross - period.totals.loss - period.totals.fee;
  const operatingNet = verifiedNet - apiCost;
  const allCostMicros = allTimeCostMicros(allTimeCostRows, endMs);
  const allCostMinor = (allCostMicros + MICROS_PER_MINOR - 1n) / MICROS_PER_MINOR;
  const payout = computePayout({
    rows: allTimeEarningsRows,
    walletAddress,
    onchainUsdcAtomic: balance,
    reserveAtomic: input.reserveAtomic,
    maxPayoutAtomic: input.maxPayoutAtomic,
    operatingCostMinor: allCostMinor,
  });

  let selfFundedBps = null;
  let selfFundedStatus = "operating_cost_unmeasured";
  if (verifiedNet <= 0n) {
    selfFundedBps = 0;
    selfFundedStatus = "non_positive_net";
  } else if (apiCost > 0n) {
    selfFundedBps = safeBps(verifiedNet, apiCost);
    selfFundedStatus = "measured";
  }

  let stopReason = "running";
  if (operatingNet < 0n) stopReason = "negative_net";
  else if (period.totals.gross === 0n) stopReason = "no_external_income";
  else if (payout.reason === "reserve_floor") stopReason = "reserve_floor";

  const railPnl = [...period.rails.entries()]
    .map(([rail, item]) => ({
      rail,
      gross_usd_micros: item.gross.toString(),
      realized_loss_usd_micros: item.loss.toString(),
      financial_fee_usd_micros: item.fee.toString(),
      user_transfer_usd_micros: item.transfer.toString(),
      net_usd_micros: (item.gross - item.loss - item.fee).toString(),
    }))
    .sort((a, b) => a.rail.localeCompare(b.rail));

  return Object.freeze({
    schema_version: 1,
    kind,
    timezone: validZone(timezone),
    ...bounds,
    wallet_address: walletAddress,
    gross_usd_micros: period.totals.gross.toString(),
    realized_loss_usd_micros: period.totals.loss.toString(),
    financial_fee_usd_micros: period.totals.fee.toString(),
    api_cost_usd_micros: apiCost.toString(),
    user_transfer_usd_micros: period.totals.transfer.toString(),
    operating_net_usd_micros: operatingNet.toString(),
    balance_usdc_atomic: balance,
    distributable_usdc_atomic: payout.amountAtomic,
    payout_reason: payout.reason,
    self_funded_bps: selfFundedBps,
    self_funded_status: selfFundedStatus,
    stop_reason: stopReason,
    excluded_rows: period.excluded,
    rail_pnl: Object.freeze(railPnl),
  });
}

module.exports = {
  MICROS_PER_MINOR,
  periodBounds,
  usdMicrosFromDecimal,
  buildFinancialSnapshot,
};
