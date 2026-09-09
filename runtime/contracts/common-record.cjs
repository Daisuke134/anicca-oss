"use strict";

const crypto = require("node:crypto");
const {
  projectFinancialRecord,
  financialRecordId,
} = require("../../apps/life-manager/lib/financial-record-contract.js");

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const EFFECT_CLASSES = new Set([
  "none", "publish", "message", "money", "application", "trade", "account_mutation",
]);
const RECEIPT_OUTCOMES = new Set(["verified", "failed", "uncertain", "reconciled"]);
const EVIDENCE_REF = /^[a-z][a-z0-9+.-]*:\/\/[A-Za-z0-9._:/-]{1,512}$/;

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

module.exports = {
  projectJob, projectReceipt, projectFinancialRecord, financialRecordId,
};
