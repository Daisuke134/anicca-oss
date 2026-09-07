"use strict";

const ERROR = "cfo_recommendation_invalid:decision";
const KINDS = new Set(["increase", "hold", "repair", "stop-review"]);
function fail() { throw new Error(ERROR); }
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function iso(value) { return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value)); }
function freeze(value, seen = new WeakSet()) { if (value === null || typeof value !== "object" || seen.has(value)) return value; seen.add(value); Object.values(value).forEach((child) => freeze(child, seen)); return Object.freeze(value); }

/** Pure recommendation: it never calls an executor or changes a budget. */
function decideCfoRecommendation(input = {}) {
  try {
    if (!plain(input) || !iso(input.observedAt) || !plain(input.profit) || !plain(input.reconciliation)
      || !plain(input.guardian)) fail();
    if (!['complete', 'partial'].includes(input.profit.status) || input.profit.contribution_profit !== null
      && typeof input.profit.contribution_profit !== 'string' || input.profit.roi !== null
      && typeof input.profit.roi !== 'string' || !Array.isArray(input.profit.coverage_exceptions)
      || !['complete', 'partial', 'incomplete_fleet_read', 'provider_fleet_join_unknown'].includes(input.reconciliation.reconciliation_status)
      || !['suppress', 'suggest'].includes(input.guardian.decision)) fail();
    const exceptions = new Set(input.profit.coverage_exceptions);
    const unknownEvidence = input.profit.status !== "complete" || input.profit.contribution_profit === null
      || input.profit.roi === null || input.reconciliation.reconciliation_status !== "complete";
    let kind = "hold", reason = "verified_stable_state";
    if (unknownEvidence) { kind = "repair"; reason = "evidence_incomplete_before_allocation"; }
    else if (input.guardian.decision === "suggest") { kind = "hold"; reason = "spending_guardian_suggestion_pending_owner_action"; }
    else if (String(input.profit.contribution_profit).startsWith("-")) { kind = "stop-review"; reason = "verified_negative_contribution_profit"; }
    else if (!String(input.profit.roi).startsWith("-")) { kind = "increase"; reason = "verified_positive_contribution_profit_and_roi"; }
    exceptions.add(reason);
    return freeze({ schemaVersion: 1, observedAt: input.observedAt, kind, reason, execute: false, ownerActionRequired: kind !== "hold", coverageExceptions: [...exceptions].sort() });
  } catch { throw new Error(ERROR); }
}

module.exports = { KINDS, decideCfoRecommendation };
