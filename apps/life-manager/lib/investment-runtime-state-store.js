"use strict";

const crypto = require("node:crypto");

const UID = /^[A-Za-z0-9._-]{1,200}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const FILES = new Set([
  "control.json", "risk-day.json", "receipts.jsonl", "live-owned-position.json",
  "telegram-outbox.sqlite3", "telegram-latest.json",
]);
const REQUIRED_FILES = ["control.json", "risk-day.json", "receipts.jsonl", "telegram-outbox.sqlite3"];
const MAX_BUNDLE_BYTES = 2 * 1024 * 1024;

function invalid() { throw new Error("investment runtime state invalid"); }

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function decoded(files, name) {
  const bytes = Buffer.from(files[name], "base64");
  if (bytes.length === 0 && name !== "receipts.jsonl") invalid();
  return bytes;
}

function validateFileSemantics(files) {
  let control;
  let risk;
  try {
    control = JSON.parse(decoded(files, "control.json").toString("utf8"));
    risk = JSON.parse(decoded(files, "risk-day.json").toString("utf8"));
  } catch { invalid(); }
  if (!control || typeof control.paused !== "boolean" || typeof control.killed !== "boolean"
    || !Number.isInteger(control.revision) || control.revision < 1 || (control.killed && !control.paused)) invalid();
  const riskKeys = ["ny_day", "baseline_equity", "baseline_observed_at", "baseline_bank_cash_flow",
    "baseline_trade_activity_ids", "baseline_trades_clean", "crypto_cash_flow", "transfers"];
  if (!risk || riskKeys.some((key) => !Object.hasOwn(risk, key))
    || !Array.isArray(risk.baseline_trade_activity_ids) || typeof risk.transfers !== "object") invalid();
  const receiptText = decoded(files, "receipts.jsonl").toString("utf8").trim();
  if (receiptText) {
    try {
      for (const line of receiptText.split("\n")) {
        const row = JSON.parse(line);
        if (!row || typeof row !== "object" || Array.isArray(row)) invalid();
      }
    } catch { invalid(); }
  }
  if (!decoded(files, "telegram-outbox.sqlite3").subarray(0, 16).equals(Buffer.from("SQLite format 3\0"))) invalid();
  if (/"(?:api_key|api_secret|live_api_key|live_api_secret|token|password)"\s*:/i.test(
    `${decoded(files, "control.json")}\n${decoded(files, "risk-day.json")}\n${receiptText}`)) invalid();
}

function normalizeBundle(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  if (Object.keys(value).sort().join(",") !== "account_binding,cutover,exported_at,files,schema_version") invalid();
  if (value.schema_version !== 1 || !Number.isFinite(Date.parse(value.exported_at))) invalid();
  const binding = value.account_binding;
  if (!binding || Object.keys(binding).sort().join(",") !== "account_id_hash,endpoint,provider"
    || binding.provider !== "alpaca" || binding.endpoint !== "live"
    || !DIGEST.test(binding.account_id_hash)) invalid();
  if (!value.files || typeof value.files !== "object" || Array.isArray(value.files)) invalid();
  for (const [name, encoded] of Object.entries(value.files)) {
    if (!FILES.has(name) || typeof encoded !== "string" || !BASE64.test(encoded)) invalid();
  }
  if (REQUIRED_FILES.some((name) => !Object.hasOwn(value.files, name))) invalid();
  validateFileSemantics(value.files);
  const cutover = value.cutover;
  if (!cutover || Object.keys(cutover).sort().join(",") !== "broker_reconciled_at,local_stopped_at,queues_drained_at,source_release_sha,status"
    || cutover.status !== "ready" || !/^[a-f0-9]{40}$/.test(cutover.source_release_sha)) invalid();
  const instants = [cutover.local_stopped_at, cutover.queues_drained_at, cutover.broker_reconciled_at].map(Date.parse);
  if (instants.some((instant) => !Number.isFinite(instant)) || instants[0] > instants[1] || instants[1] > instants[2]) invalid();
  const clone = JSON.parse(JSON.stringify(value));
  if (Buffer.byteLength(canonicalJson(clone)) > MAX_BUNDLE_BYTES) invalid();
  return Object.freeze(clone);
}

function sealRuntimeBundle(value) {
  const bundle = normalizeBundle(value);
  const digest = crypto.createHash("sha256").update(canonicalJson(bundle)).digest("hex");
  return Object.freeze({ bundle, digest });
}

function validateRow(row, uid) {
  if (!row || row.uid !== uid || !DIGEST.test(row.bundle_digest)) invalid();
  const sealed = sealRuntimeBundle(row.bundle);
  if (sealed.digest !== row.bundle_digest) invalid();
  return sealed;
}

function createInvestmentRuntimeStateStore({ query } = {}) {
  if (typeof query !== "function") throw new Error("investment runtime state store unavailable");
  return Object.freeze({
    async read(uid) {
      if (!UID.test(String(uid || ""))) invalid();
      const result = await query(`SELECT uid, bundle, bundle_digest FROM public.lm_investment_runtime_states WHERE uid = $1 LIMIT 1`, [uid]).catch(() => null);
      if (!result || !Array.isArray(result.rows) || result.rows.length > 1) throw new Error("investment runtime state store unavailable");
      return result.rows.length ? validateRow(result.rows[0], uid) : null;
    },
    async upsert(uid, value) {
      if (!UID.test(String(uid || ""))) invalid();
      const sealed = sealRuntimeBundle(value);
      const result = await query(`
        INSERT INTO public.lm_investment_runtime_states (uid, bundle, bundle_digest)
        VALUES ($1, $2::jsonb, $3)
        ON CONFLICT (uid) DO UPDATE SET bundle = EXCLUDED.bundle,
          bundle_digest = EXCLUDED.bundle_digest, updated_at = clock_timestamp()
        RETURNING uid, bundle, bundle_digest
      `, [uid, JSON.stringify(sealed.bundle), sealed.digest]).catch(() => null);
      if (!result || !Array.isArray(result.rows) || result.rows.length !== 1) throw new Error("investment runtime state store unavailable");
      return validateRow(result.rows[0], uid);
    },
  });
}

module.exports = { createInvestmentRuntimeStateStore, normalizeBundle, sealRuntimeBundle };
