"use strict";

const ERROR = "cfo_spending_guardian_invalid:decision";
const DAY = 86400000;
const COOLDOWN_DAYS = 7;
const EXCLUDED = /^(?:transfer|card repayment|card_repayment|refund|返金|振替|カード返済)$/i;

function fail() { throw new Error(ERROR); }
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function isoDate(value) { return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(`${value}T00:00:00Z`)); }
function integer(value) { return Number.isSafeInteger(value) && value >= 0; }
function freeze(value, seen = new WeakSet()) { if (value === null || typeof value !== "object" || seen.has(value)) return value; seen.add(value); Object.values(value).forEach((child) => freeze(child, seen)); return Object.freeze(value); }
function daysBetween(start, end) { return Math.floor((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / DAY); }
function validTransaction(value) {
  return plain(value) && isoDate(value.bookingDate) && integer(Math.abs(Number(value.amountMinor)))
    && ["inflow", "outflow", "neutral"].includes(value.flow)
    && value.verificationStatus === "provider_reported"
    && (value.category === null || typeof value.category === "string");
}
function suppress(reason, observedAt) { return freeze({ decision: "suppress", reason, observedAt, suggestion: null, receipt: { decision: "suppress", reason, observedAt } }); }

/**
 * Deterministic, read-only spending advice. It never labels an unknown row,
 * transfer, repayment, refund, category, budget, or cash floor as actionable.
 */
function decideSpendingGuardian(input = {}) {
  try {
    if (!plain(input) || !Array.isArray(input.transactions) || !plain(input.budgets)
      || !plain(input.protectedCash) || !Array.isArray(input.history) || !isoDate(input.reportingDate)) fail();
    const observedAt = input.observedAt || `${input.reportingDate}T00:00:00Z`;
    if (typeof observedAt !== "string" || !Number.isFinite(Date.parse(observedAt))) fail();
    if (input.transactions.some((row) => !validTransaction(row))) fail();
    if (input.protectedCash.status !== "verified" && input.protectedCash.status !== "unknown") fail();
    if (input.protectedCash.status === "verified" && (!integer(input.protectedCash.amountMinor) || !integer(input.protectedCash.floorMinor))) fail();
    if (input.protectedCash.status === "unknown" && (input.protectedCash.amountMinor !== null || input.protectedCash.floorMinor !== null)) fail();
    for (const [category, budget] of Object.entries(input.budgets)) {
      if (!category || !plain(budget) || !integer(budget.amountMinor) || !isoDate(budget.periodStart) || !isoDate(budget.periodEnd) || budget.periodStart >= budget.periodEnd || budget.evidenceStatus !== "owner_approved") fail();
    }
    for (const row of input.history) if (!plain(row) || typeof row.category !== "string" || !isoDate(row.suggestedAt)) fail();

    const candidates = new Map();
    for (const row of input.transactions) {
      if (row.flow !== "outflow" || row.category === null || EXCLUDED.test(row.category)) continue;
      const budget = input.budgets[row.category];
      if (!budget || input.reportingDate < budget.periodStart || input.reportingDate >= budget.periodEnd) continue;
      if (row.bookingDate < budget.periodStart || row.bookingDate >= budget.periodEnd) continue;
      const entry = candidates.get(row.category) || { spend: 0, latest: row.bookingDate, count: 0 };
      entry.spend += Math.abs(row.amountMinor); entry.count += 1; if (row.bookingDate > entry.latest) entry.latest = row.bookingDate; candidates.set(row.category, entry);
    }
    if (Object.keys(input.budgets).length === 0) return suppress("budget_unknown", observedAt);
    if ([...candidates.values()].length === 0) return suppress("no_verified_actionable_outgoing", observedAt);
    const ranked = [...candidates.entries()].map(([category, entry]) => {
      const budget = input.budgets[category], overage = entry.spend - budget.amountMinor;
      return { category, ...entry, budget: budget.amountMinor, overage };
    }).filter((entry) => entry.overage > 0).sort((a, b) => b.overage - a.overage || b.latest.localeCompare(a.latest) || a.category.localeCompare(b.category));
    if (!ranked.length) return suppress("no_material_budget_impact", observedAt);
    const top = ranked[0], previous = input.history.find((row) => row.category === top.category);
    if (previous && daysBetween(previous.suggestedAt, input.reportingDate) < COOLDOWN_DAYS) return suppress("cooldown_active", observedAt);
    if (input.protectedCash.status !== "verified") return suppress("protected_cash_unknown", observedAt);
    const suggestedLimit = Math.max(0, top.budget - (top.spend - Math.abs(input.transactions.filter((row) => row.category === top.category && row.bookingDate === input.reportingDate && row.flow === "outflow").reduce((sum, row) => sum + Math.abs(row.amountMinor), 0))));
    return freeze({ decision: "suggest", reason: "material_verified_budget_overage", observedAt, suggestion: { category: top.category, verifiedMonthSpendMinor: top.spend, approvedBudgetMinor: top.budget, overageMinor: top.overage, suggestedLimitMinor: suggestedLimit, transactionCount: top.count }, receipt: { decision: "suggest", category: top.category, observedAt, cooldownDays: COOLDOWN_DAYS } });
  } catch { throw new Error(ERROR); }
}

module.exports = { COOLDOWN_DAYS, decideSpendingGuardian };
