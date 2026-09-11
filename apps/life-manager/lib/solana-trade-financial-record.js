"use strict";

const { financialRecordId } = require("./financial-record-contract.js");

const SOLANA_SIGNATURE = /^[1-9A-HJ-NP-Za-km-z]{60,100}$/;

function usdcAtomic(value) {
  const text = String(value);
  if (!/^-?\d+(?:\.\d{1,6})?$/.test(text)) throw new Error("Solana trade USDC delta invalid");
  const negative = text.startsWith("-");
  const [whole, fraction = ""] = text.replace("-", "").split(".");
  const atomic = BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"));
  return negative ? -atomic : atomic;
}

function solanaTradeToFinancialRecord(result, {
  subjectId, occurredAt = new Date().toISOString(), recordedAt = new Date().toISOString(),
} = {}) {
  if (result?.status !== "recorded" && result?.status !== "duplicate") {
    throw new Error("verified Solana trade result required");
  }
  const signatures = Array.isArray(result.signatures) ? [...new Set(result.signatures)] : [];
  if (!signatures.length || signatures.some((sig) => !SOLANA_SIGNATURE.test(sig))) {
    throw new Error("Solana trade signatures invalid");
  }
  const atomic = usdcAtomic(result.net_usdc);
  if (atomic === 0n) return null;
  const amount = Number(atomic < 0n ? -atomic : atomic);
  if (!Number.isSafeInteger(amount)) throw new Error("Solana trade amount exceeds safe range");
  const key = `solana-trade:v1:${signatures.at(-1)}`;
  const recorded = new Date(recordedAt).toISOString();
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: atomic > 0n ? "business_revenue" : "business_cost",
    direction: atomic > 0n ? "credit" : "debit", amount_minor: amount, currency: "USDC",
    occurred_at: new Date(occurredAt).toISOString(), recorded_at: recorded,
    idempotency_key: key,
    source: { provider: "solana_trade", source_type: "wallet", external_ref: signatures.at(-1) },
    verification: { status: "verified", observed_at: recorded,
      evidence_refs: signatures.map((sig) => `solana://mainnet/tx/${sig}`) },
  };
}

function createSolanaTradeFinancialWriter({ store, subjectId, now = () => new Date().toISOString() }) {
  if (!store || typeof store.append !== "function") throw new Error("FinancialRecord store required");
  return async (result) => {
    const timestamp = result.occurred_at || now();
    const record = solanaTradeToFinancialRecord(result, {
      subjectId, occurredAt: timestamp, recordedAt: timestamp,
    });
    if (!record) return { ok: true, skipped: "zero-net" };
    const write = await store.append(record);
    return { ok: true, duplicate: write.created === false, record_id: record.record_id };
  };
}

module.exports = { usdcAtomic, solanaTradeToFinancialRecord, createSolanaTradeFinancialWriter };
