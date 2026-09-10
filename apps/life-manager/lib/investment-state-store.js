"use strict";

const LIFECYCLES = new Set(["setup_required", "in_review", "approved", "active", "rejected", "action_required"]);
const DEPLOYMENTS = new Set(["local", "cloud"]);
const MODES = new Set(["paper", "shadow", "live"]);
const UID = /^[A-Za-z0-9._-]{1,200}$/;
const DIGEST = /^[a-f0-9]{64}$/;
const SECRET_REF = /^secret:\/\/alpaca\/[a-z0-9][a-z0-9._/-]{0,190}$/i;
const RECEIPT_REF = /^(?:provider-receipt|object):\/\/[a-z0-9._~:/?#@!$&'()*+,;=%-]{1,950}$/i;
const ROW_KEYS = Object.freeze([
  "alpaca_api_key_ref", "alpaca_api_secret_ref", "core_digest", "deployment", "killed",
  "lifecycle", "mode", "paused", "receipt_refs", "uid",
]);

function invalid() { throw new Error("investment state invalid"); }

function normalizeInvestmentState(value, expectedUid) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  if (Object.keys(value).sort().join(",") !== [...ROW_KEYS].sort().join(",")) invalid();
  if (!UID.test(value.uid) || value.uid !== expectedUid) invalid();
  if (!LIFECYCLES.has(value.lifecycle) || !DEPLOYMENTS.has(value.deployment) || !MODES.has(value.mode)) invalid();
  if (typeof value.paused !== "boolean" || typeof value.killed !== "boolean") invalid();
  if (value.core_digest !== null && !DIGEST.test(value.core_digest)) invalid();
  if (!Array.isArray(value.receipt_refs) || value.receipt_refs.length > 100
    || value.receipt_refs.some((ref) => typeof ref !== "string" || !RECEIPT_REF.test(ref))) invalid();
  if (value.alpaca_api_key_ref !== null && !SECRET_REF.test(value.alpaca_api_key_ref)) invalid();
  if (value.alpaca_api_secret_ref !== null && !SECRET_REF.test(value.alpaca_api_secret_ref)) invalid();
  return Object.freeze({ ...value, receipt_refs: Object.freeze([...value.receipt_refs]) });
}

function createInvestmentStateStore({ query } = {}) {
  if (typeof query !== "function") throw new Error("investment state store unavailable");
  return Object.freeze({
    async listRunnable(limit = 1) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 50) invalid();
      const result = await query(`
        SELECT ${ROW_KEYS.join(", ")}
        FROM public.lm_investment_states
        WHERE deployment = 'cloud' AND mode IN ('paper', 'shadow', 'live')
          AND paused = false AND killed = false
        ORDER BY uid
        LIMIT $1
      `, [limit]).catch(() => null);
      const rows = result && result.rows;
      if (!Array.isArray(rows) || rows.length > limit) throw new Error("investment state store unavailable");
      return Object.freeze(rows.map((row) => normalizeInvestmentState(row, row.uid)));
    },
    async read(uid) {
      if (!UID.test(String(uid || ""))) invalid();
      const result = await query(`
        SELECT ${ROW_KEYS.join(", ")}
        FROM public.lm_investment_states
        WHERE uid = $1
        LIMIT 1
      `, [uid]).catch(() => null);
      const rows = result && result.rows;
      if (!Array.isArray(rows) || rows.length > 1) throw new Error("investment state store unavailable");
      return rows.length === 0 ? Object.freeze({ lifecycle: "setup_required" }) : normalizeInvestmentState(rows[0], uid);
    },

    async upsert(uid, state) {
      if (!UID.test(String(uid || ""))) invalid();
      const body = normalizeInvestmentState({ ...state, uid }, uid);
      const result = await query(`
        INSERT INTO public.lm_investment_states (
          uid, lifecycle, deployment, mode, paused, killed, core_digest, receipt_refs,
          alpaca_api_key_ref, alpaca_api_secret_ref
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
        ON CONFLICT (uid) DO UPDATE SET
          lifecycle = EXCLUDED.lifecycle, deployment = EXCLUDED.deployment, mode = EXCLUDED.mode,
          paused = EXCLUDED.paused, killed = EXCLUDED.killed, core_digest = EXCLUDED.core_digest,
          receipt_refs = EXCLUDED.receipt_refs, alpaca_api_key_ref = EXCLUDED.alpaca_api_key_ref,
          alpaca_api_secret_ref = EXCLUDED.alpaca_api_secret_ref, updated_at = clock_timestamp()
        RETURNING ${ROW_KEYS.join(", ")}
      `, [
        body.uid, body.lifecycle, body.deployment, body.mode, body.paused, body.killed,
        body.core_digest, JSON.stringify(body.receipt_refs), body.alpaca_api_key_ref, body.alpaca_api_secret_ref,
      ]).catch(() => null);
      const rows = result && result.rows;
      if (!Array.isArray(rows) || rows.length !== 1) throw new Error("investment state store unavailable");
      return normalizeInvestmentState(rows[0], uid);
    },
  });
}

module.exports = { createInvestmentStateStore, normalizeInvestmentState };
