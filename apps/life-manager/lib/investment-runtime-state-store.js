"use strict";

const crypto = require("node:crypto");

const UID = /^[A-Za-z0-9._-]{1,200}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const FILES = new Set([
  "control.json", "risk-day.json", "receipts.jsonl", "live-owned-position.json",
  "telegram-outbox.sqlite3", "telegram-latest.json",
]);
const MAX_BUNDLE_BYTES = 2 * 1024 * 1024;

function invalid() { throw new Error("investment runtime state invalid"); }

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function normalizeBundle(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  if (Object.keys(value).sort().join(",") !== "account_binding,exported_at,files,schema_version") invalid();
  if (value.schema_version !== 1 || !Number.isFinite(Date.parse(value.exported_at))) invalid();
  const binding = value.account_binding;
  if (!binding || Object.keys(binding).sort().join(",") !== "account_id_hash,endpoint,provider"
    || binding.provider !== "alpaca" || binding.endpoint !== "live"
    || !DIGEST.test(binding.account_id_hash)) invalid();
  if (!value.files || typeof value.files !== "object" || Array.isArray(value.files)) invalid();
  for (const [name, encoded] of Object.entries(value.files)) {
    if (!FILES.has(name) || typeof encoded !== "string" || !BASE64.test(encoded)) invalid();
  }
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
