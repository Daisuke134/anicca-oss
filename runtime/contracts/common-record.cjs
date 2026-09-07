"use strict";

const crypto = require("node:crypto");

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const EFFECT_CLASSES = new Set([
  "none", "publish", "message", "money", "application", "trade", "account_mutation",
]);
const RECEIPT_OUTCOMES = new Set(["verified", "failed", "uncertain", "reconciled"]);
const EVIDENCE_REF = /^[a-z][a-z0-9+.-]*:\/\/[A-Za-z0-9._:/-]{1,512}$/;
const FINANCIAL_SCOPES = new Set(["personal", "business"]);
const FINANCIAL_KINDS = new Set([
  "asset_balance", "liability_balance", "personal_income", "personal_expense",
  "business_revenue", "business_cost", "payout", "fee", "tax", "transfer",
]);
const FINANCIAL_DIRECTIONS = new Set(["credit", "debit", "snapshot"]);
const FINANCIAL_SOURCE_TYPES = new Set([
  "moneytree", "marketplace", "payment_processor", "app_store", "wallet", "manual",
]);
const FINANCIAL_STATUSES = new Set(["verified", "unverified", "stale"]);

function invalid(label) {
  throw new Error(`common Job ${label} invalid`);
}

function id(value, label) {
  if (typeof value !== "string" || !ID.test(value)) invalid(label);
  return value;
}

function codePointLength(value) {
  return [...value].length;
}

function projectJob(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid("record");
  const effectClass = value.effect_class;
  if (!EFFECT_CLASSES.has(effectClass)) invalid("effect_class");
  const effectKey = value.effect_key;
  if ((effectClass === "none" && effectKey !== null)
    || (effectClass !== "none" && (typeof effectKey !== "string" || codePointLength(effectKey) < 1 || codePointLength(effectKey) > 500))) {
    invalid("effect_key");
  }
  if (!value.input_refs || typeof value.input_refs !== "object" || Array.isArray(value.input_refs)) {
    invalid("input_refs");
  }
  const inputRefs = {};
  for (const [key, raw] of Object.entries(value.input_refs)) {
    if (!/_refs?$/.test(key)) invalid("input_refs");
    const items = Array.isArray(raw) ? raw : [raw];
    if (items.length < 1 || items.some((item) => typeof item !== "string" || codePointLength(item) < 1 || codePointLength(item) > 1000)) {
      invalid("input_refs");
    }
    inputRefs[key] = Array.isArray(raw) ? [...raw] : raw;
  }
  if (!Number.isSafeInteger(value.max_attempts) || value.max_attempts < 1 || value.max_attempts > 20) {
    invalid("max_attempts");
  }
  return {
    schema_version: 1,
    record_type: "job",
    job_id: id(value.job_id, "job_id"),
    tenant_id: id(value.tenant_id, "tenant_id"),
    loop_id: id(value.loop_id, "loop_id"),
    capability: id(value.capability, "capability"),
    effect_class: effectClass,
    effect_key: effectKey,
    input_refs: inputRefs,
    max_attempts: value.max_attempts,
  };
}

function projectReceipt(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid("Receipt record");
  if (!RECEIPT_OUTCOMES.has(value.outcome)) invalid("Receipt outcome");
  if (typeof value.payload_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.payload_sha256)) {
    invalid("Receipt payload_sha256");
  }
  if (typeof value.recorded_at !== "string" || !Number.isFinite(Date.parse(value.recorded_at))) {
    invalid("Receipt recorded_at");
  }
  if (value.external_ref !== null && (typeof value.external_ref !== "string" || codePointLength(value.external_ref) > 1024)) {
    invalid("Receipt external_ref");
  }
  if (!Array.isArray(value.evidence_refs) || value.evidence_refs.length > 32
    || value.evidence_refs.some((ref) => typeof ref !== "string" || !EVIDENCE_REF.test(ref))) {
    invalid("Receipt evidence_refs");
  }
  if (value.outcome === "verified" && (!value.external_ref || value.evidence_refs.length < 1)) {
    invalid("Receipt verification evidence");
  }
  return {
    schema_version: 1,
    record_type: "receipt",
    receipt_id: id(value.receipt_id, "Receipt receipt_id"),
    effect_id: id(value.effect_id, "Receipt effect_id"),
    loop_id: id(value.loop_id, "Receipt loop_id"),
    run_id: id(value.run_id, "Receipt run_id"),
    outcome: value.outcome,
    provider: id(value.provider, "Receipt provider"),
    recorded_at: value.recorded_at,
    external_ref: value.external_ref,
    payload_sha256: value.payload_sha256,
    evidence_refs: [...value.evidence_refs],
  };
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
  const subject = id(subjectId, "FinancialRecord subject_id");
  if (typeof idempotencyKey !== "string" || idempotencyKey.length < 1
    || idempotencyKey.length > 1024) invalid("FinancialRecord idempotency_key");
  return `financial:${crypto.createHash("sha256")
    .update(`${subject}\n${idempotencyKey}`)
    .digest("hex")}`;
}

function projectFinancialRecord(value) {
  const keys = [
    "schema_version", "record_type", "record_id", "subject_id", "scope", "kind",
    "direction", "amount_minor", "currency", "occurred_at", "recorded_at",
    "idempotency_key", "source", "verification",
  ];
  exactKeys(value, keys, "FinancialRecord");
  if (value.schema_version !== 1 || value.record_type !== "financial_record") {
    invalid("FinancialRecord version");
  }
  if (!FINANCIAL_SCOPES.has(value.scope) || !FINANCIAL_KINDS.has(value.kind)
    || !FINANCIAL_DIRECTIONS.has(value.direction)) invalid("FinancialRecord classification");
  if (!Number.isSafeInteger(value.amount_minor) || value.amount_minor < 0) {
    invalid("FinancialRecord amount_minor");
  }
  if (typeof value.currency !== "string" || !/^[A-Z][A-Z0-9]{2,9}$/.test(value.currency)) {
    invalid("FinancialRecord currency");
  }
  if (typeof value.idempotency_key !== "string" || value.idempotency_key.length < 1
    || value.idempotency_key.length > 1024) invalid("FinancialRecord idempotency_key");
  if (value.record_id !== financialRecordId(value.subject_id, value.idempotency_key)) {
    invalid("FinancialRecord record_id");
  }
  exactKeys(value.source, ["provider", "source_type", "external_ref"], "FinancialRecord source");
  exactKeys(
    value.verification,
    ["status", "observed_at", "evidence_refs"],
    "FinancialRecord verification",
  );
  if (!FINANCIAL_SOURCE_TYPES.has(value.source.source_type)
    || (value.source.external_ref !== null
      && (typeof value.source.external_ref !== "string" || value.source.external_ref.length > 1024))) {
    invalid("FinancialRecord source");
  }
  if (!FINANCIAL_STATUSES.has(value.verification.status)
    || !Array.isArray(value.verification.evidence_refs)
    || value.verification.evidence_refs.length > 32
    || value.verification.evidence_refs.some((ref) => (
      typeof ref !== "string" || !EVIDENCE_REF.test(ref)
    ))
    || (value.verification.status === "verified"
      && value.verification.evidence_refs.length === 0)) {
    invalid("FinancialRecord verification evidence");
  }
  const personal = new Set([
    "asset_balance", "liability_balance", "personal_income", "personal_expense",
  ]);
  if (personal.has(value.kind) && value.scope !== "personal") invalid("FinancialRecord scope");
  if (new Set(["business_revenue", "business_cost", "payout"]).has(value.kind)
    && value.scope !== "business") invalid("FinancialRecord scope");
  if (value.source.source_type === "moneytree" && value.scope !== "personal") {
    invalid("FinancialRecord scope");
  }
  const expectedDirection = {
    asset_balance: "snapshot", liability_balance: "snapshot",
    personal_income: "credit", business_revenue: "credit", payout: "credit",
    personal_expense: "debit", business_cost: "debit", fee: "debit", tax: "debit",
  }[value.kind];
  if (expectedDirection && value.direction !== expectedDirection) {
    invalid("FinancialRecord direction");
  }
  if (value.kind === "transfer" && !["credit", "debit"].includes(value.direction)) {
    invalid("FinancialRecord direction");
  }
  return Object.freeze({
    ...value,
    record_id: id(value.record_id, "FinancialRecord record_id"),
    subject_id: id(value.subject_id, "FinancialRecord subject_id"),
    occurred_at: timestamp(value.occurred_at, "FinancialRecord occurred_at"),
    recorded_at: timestamp(value.recorded_at, "FinancialRecord recorded_at"),
    source: Object.freeze({
      ...value.source,
      provider: id(value.source.provider, "FinancialRecord provider"),
    }),
    verification: Object.freeze({
      ...value.verification,
      observed_at: timestamp(
        value.verification.observed_at,
        "FinancialRecord verification observed_at",
      ),
      evidence_refs: Object.freeze([...value.verification.evidence_refs]),
    }),
  });
}

module.exports = {
  projectJob, projectReceipt, projectFinancialRecord, financialRecordId,
};
