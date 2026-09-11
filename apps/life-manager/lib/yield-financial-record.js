"use strict";

const { financialRecordId } = require("./financial-record-contract.js");

function atomic(value, label) {
  const text = String(value);
  if (!/^-?\d+(?:\.\d{1,6})?$/.test(text)) throw new Error(`${label} invalid`);
  const negative = text.startsWith("-");
  const [whole, fraction = ""] = text.replace("-", "").split(".");
  const result = BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"));
  return negative ? -result : result;
}

function yieldResultToFinancialRecord(result, {
  subjectId, occurredAt = new Date().toISOString(), recordedAt = occurredAt,
} = {}) {
  if (result?.kind !== "yield" || result?.action !== "refill" || result?.status !== "0x1"
    || result?.basis_known !== true || !/^0x[0-9a-f]{64}$/i.test(String(result?.tx || ""))) {
    return null;
  }
  const received = atomic(result.refilled_usdc, "Yield received amount");
  const basis = atomic(result.basis_before_usdc, "Yield cost basis");
  const realized = atomic(result.realized_yield_usdc, "Yield realized result");
  if (received < 0n || basis < 0n || realized !== received - basis || realized === 0n) {
    if (realized === 0n && realized === received - basis) return null;
    throw new Error("Yield realized accounting identity invalid");
  }
  const amount = Number(realized < 0n ? -realized : realized);
  if (!Number.isSafeInteger(amount)) throw new Error("Yield realized amount exceeds safe range");
  const tx = String(result.tx).toLowerCase();
  const key = `yield-realized:v1:${tx}`;
  const timestamp = new Date(occurredAt).toISOString();
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: realized > 0n ? "business_revenue" : "business_cost",
    direction: realized > 0n ? "credit" : "debit", amount_minor: amount, currency: "USDC",
    occurred_at: timestamp, recorded_at: new Date(recordedAt).toISOString(), idempotency_key: key,
    source: { provider: "yield_beefy", source_type: "wallet", external_ref: tx },
    verification: { status: "verified", observed_at: timestamp,
      evidence_refs: [`eip155://8453/tx/${tx.slice(2)}`] },
  };
}

module.exports = { yieldResultToFinancialRecord };
