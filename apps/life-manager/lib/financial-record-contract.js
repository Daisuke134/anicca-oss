"use strict";

const crypto = require("node:crypto");

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SCOPES = new Set(["personal", "business"]);
const KINDS = new Set([
  "asset_balance", "liability_balance", "personal_income", "personal_expense",
  "business_revenue", "business_cost", "payout", "fee", "tax", "transfer",
]);
const DIRECTIONS = new Set(["credit", "debit", "snapshot"]);
const SOURCE_TYPES = new Set([
  "moneytree", "marketplace", "payment_processor", "app_store", "wallet", "manual",
]);
const STATUSES = new Set(["verified", "unverified", "stale"]);
const EVIDENCE_REF = /^[a-z][a-z0-9+.-]*:\/\/[A-Za-z0-9._:/-]{1,512}$/;

function invalid(label) { throw new Error(`FinancialRecord ${label} invalid`); }
function id(value, label) {
  if (typeof value !== "string" || !ID.test(value)) invalid(label);
  return value;
}
function exactKeys(value, keys, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).length !== keys.length
    || keys.some((key) => !Object.hasOwn(value, key))) invalid(label);
}
function timestamp(value, label) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))
    || !/[zZ]|[+-]\d\d:\d\d$/.test(value)) invalid(label);
  return new Date(value).toISOString();
}
function financialRecordId(subjectId, idempotencyKey) {
  const subject = id(subjectId, "subject_id");
  if (typeof idempotencyKey !== "string" || idempotencyKey.length < 1
    || idempotencyKey.length > 1024) invalid("idempotency_key");
  return `financial:${crypto.createHash("sha256").update(`${subject}\n${idempotencyKey}`).digest("hex")}`;
}
function projectFinancialRecord(value) {
  exactKeys(value, [
    "schema_version", "record_type", "record_id", "subject_id", "scope", "kind",
    "direction", "amount_minor", "currency", "occurred_at", "recorded_at",
    "idempotency_key", "source", "verification",
  ], "record");
  if (value.schema_version !== 1 || value.record_type !== "financial_record") invalid("version");
  if (!SCOPES.has(value.scope) || !KINDS.has(value.kind) || !DIRECTIONS.has(value.direction)) invalid("classification");
  if (!Number.isSafeInteger(value.amount_minor) || value.amount_minor < 0) invalid("amount_minor");
  if (typeof value.currency !== "string" || !/^[A-Z][A-Z0-9]{2,9}$/.test(value.currency)) invalid("currency");
  if (typeof value.idempotency_key !== "string" || value.idempotency_key.length < 1
    || value.idempotency_key.length > 1024) invalid("idempotency_key");
  if (value.record_id !== financialRecordId(value.subject_id, value.idempotency_key)) invalid("record_id");
  exactKeys(value.source, ["provider", "source_type", "external_ref"], "source");
  exactKeys(value.verification, ["status", "observed_at", "evidence_refs"], "verification");
  if (!SOURCE_TYPES.has(value.source.source_type)
    || (value.source.external_ref !== null
      && (typeof value.source.external_ref !== "string" || value.source.external_ref.length > 1024))) invalid("source");
  if (!STATUSES.has(value.verification.status)
    || !Array.isArray(value.verification.evidence_refs)
    || value.verification.evidence_refs.length > 32
    || value.verification.evidence_refs.some((ref) => typeof ref !== "string" || !EVIDENCE_REF.test(ref))
    || (value.verification.status === "verified" && value.verification.evidence_refs.length === 0)) invalid("verification evidence");
  if (new Set(["asset_balance", "liability_balance", "personal_income", "personal_expense"]).has(value.kind)
    && value.scope !== "personal") invalid("scope");
  if (new Set(["business_revenue", "business_cost", "payout"]).has(value.kind)
    && value.scope !== "business") invalid("scope");
  if (value.source.source_type === "moneytree" && value.scope !== "personal") invalid("scope");
  const direction = {
    asset_balance: "snapshot", liability_balance: "snapshot", personal_income: "credit",
    business_revenue: "credit", payout: "credit", personal_expense: "debit",
    business_cost: "debit", fee: "debit", tax: "debit",
  }[value.kind];
  if (direction && value.direction !== direction) invalid("direction");
  if (value.kind === "transfer" && !["credit", "debit"].includes(value.direction)) invalid("direction");
  return Object.freeze({
    ...value,
    record_id: id(value.record_id, "record_id"),
    subject_id: id(value.subject_id, "subject_id"),
    occurred_at: timestamp(value.occurred_at, "occurred_at"),
    recorded_at: timestamp(value.recorded_at, "recorded_at"),
    source: Object.freeze({ ...value.source, provider: id(value.source.provider, "provider") }),
    verification: Object.freeze({
      ...value.verification,
      observed_at: timestamp(value.verification.observed_at, "verification observed_at"),
      evidence_refs: Object.freeze([...value.verification.evidence_refs]),
    }),
  });
}

module.exports = { projectFinancialRecord, financialRecordId };
