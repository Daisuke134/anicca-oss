"use strict";

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const EFFECT_CLASSES = new Set([
  "none", "publish", "message", "money", "application", "trade", "account_mutation",
]);

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

module.exports = { projectJob };
