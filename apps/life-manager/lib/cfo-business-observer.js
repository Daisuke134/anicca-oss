"use strict";

const BUSINESS = Object.freeze([
  ["life_manager_saas", "Life Manager"], ["anicca_ios", "Anicca iOS"], ["writer_agent", "Writer Agent"],
  ["affiliate_agent", "Affiliate Agent"], ["gig_work", "Gig Work"], ["x402_services", "x402 Services"],
  ["job_income", "Employment Income"], ["capafy_marketplace", "Capafy Marketplace"],
  ["proprietary_investing", "Proprietary Investing"],
]);
const SOURCE_MAP = Object.freeze({ x402_sale: "x402_services", polymarket_cycle: "proprietary_investing", taskmarket_work: "life_manager_saas", x402_work: "life_manager_saas", ugig_work: "life_manager_saas" });
const BUSINESS_IDS = new Set(BUSINESS.map(([id]) => id));
const ERROR = "cfo_business_observer_invalid:read";

function iso(value) { return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value)); }
function decimal(value) {
  const negative = value < 0n, absolute = negative ? -value : value;
  const fraction = String(absolute % 100000000n).padStart(8, "0").replace(/0+$/, "");
  return `${negative ? "-" : ""}${absolute / 100000000n}${fraction ? `.${fraction}` : ".00"}`;
}
function micros(row) {
  if (row.amount_minor != null) {
    if (!Number.isSafeInteger(row.amount_minor) || row.amount_minor < 0) return null;
    return BigInt(row.amount_minor) * 1000000n;
  }
  if (row.amount_atomic != null) {
    if (typeof row.amount_atomic !== "string" || !/^\d+$/.test(row.amount_atomic) || row.currency !== "USD") return null;
    const decimals = Number(row.amount_decimals);
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 6) return null;
    return BigInt(row.amount_atomic) * 100000000n / (10n ** BigInt(decimals + 2));
  }
  return null;
}
function emptyBusiness(id, label) {
  return { financialUnitId: id, label, providerReceiptStatus: "unknown", providerReceiptCount: 0, activity: [], landedCashStatus: "unknown", costStatus: "unknown", contributionProfit: null, roi: null };
}
function safeRows(rows) {
  const map = new Map(BUSINESS.map(([id, label]) => [id, emptyBusiness(id, label)]));
  const exceptions = new Set(["landed_cash_unknown", "business_cost_unknown", "capital_unknown", "profit_disabled_until_reconciliation", "roi_disabled_until_reconciliation", "fleet_join_unknown", "api_cost_attribution_unknown"]);
  for (const row of Array.isArray(rows) ? rows : []) {
    const id = SOURCE_MAP[row.source];
    if (!id) { exceptions.add("unmapped_provider_source"); continue; }
    const value = micros(row);
    if (value === null || row.currency !== "USD" || !iso(row.occurred_at)) { exceptions.add("provider_amount_unknown"); continue; }
    const business = map.get(id);
    const kind = row.kind === "financial_realized_loss" ? "realized_pnl" : row.kind === "financial_fee" ? "fee" : "external_income";
    business.providerReceiptStatus = "observed";
    business.providerReceiptCount += 1;
    business.activity.push({ kind, currency: "USD", amountDecimal: decimal(kind === "realized_pnl" ? -value : value), evidenceStatus: "verified_append_only_ledger" });
  }
  return { businesses: [...map.values()], exceptions: [...exceptions].sort() };
}

async function observeCfoBusiness({ supaUrl, supaKey, fetchImpl = globalThis.fetch, observedAt } = {}) {
  if (typeof supaUrl !== "string" || typeof supaKey !== "string" || typeof fetchImpl !== "function" || !iso(observedAt)) throw new Error(ERROR);
  const endpoint = `${supaUrl.replace(/\/$/, "")}/rest/v1/lm_agent_earnings?select=source,kind,currency,amount_minor,amount_atomic,amount_decimals,occurred_at&order=occurred_at.asc&limit=1000`;
  try {
    const response = await fetchImpl(endpoint, { headers: { apikey: supaKey, Authorization: `Bearer ${supaKey}` } });
    if (!response || !response.ok) throw new Error(ERROR);
    const rows = await response.json();
    if (!Array.isArray(rows)) throw new Error(ERROR);
    const result = safeRows(rows);
    if (rows.length === 1000) result.exceptions.push("provider_receipt_page_partial");
    return Object.freeze({ schemaVersion: 1, observedAt, status: "partial", evidenceStatus: "partial", businesses: result.businesses, exceptions: [...new Set(result.exceptions)].sort() });
  } catch { return Object.freeze({ schemaVersion: 1, observedAt, status: "partial", evidenceStatus: "unknown", businesses: BUSINESS.map(([id, label]) => emptyBusiness(id, label)), exceptions: ["provider_ledger_unavailable", "landed_cash_unknown", "business_cost_unknown", "profit_disabled_until_reconciliation", "roi_disabled_until_reconciliation"].sort() }); }
}

module.exports = { BUSINESS, observeCfoBusiness };
