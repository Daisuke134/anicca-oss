"use strict";

const { financialRecordId } = require("./financial-record-contract.js");

function earningsIncomeToFinancialRecord(row, { subjectId, recordedAt } = {}) {
  if (row?.kind !== "financial_external_income" || row?.meta?.external !== true
    || row?.meta?.finalized !== true || !/^0x[0-9a-f]{64}$/i.test(String(row?.tx_hash || ""))) {
    throw new Error("verified external earnings row required");
  }
  const atomic = String(row?.meta?.usdc_atomic ?? row?.amount_atomic ?? "");
  const amount = Number(atomic);
  if (!/^\d+$/.test(atomic) || !Number.isSafeInteger(amount) || amount <= 0) {
    throw new Error("external earnings USDC amount invalid");
  }
  const key = `earnings-financial:v1:${row.entry_key}`;
  const occurred = new Date(row.occurred_at).toISOString();
  const recorded = new Date(recordedAt || row.occurred_at).toISOString();
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: "business_revenue", direction: "credit",
    amount_minor: amount, currency: "USDC", occurred_at: occurred, recorded_at: recorded,
    idempotency_key: key,
    source: { provider: String(row.source), source_type: "wallet", external_ref: String(row.tx_hash).toLowerCase() },
    verification: { status: "verified", observed_at: recorded,
      evidence_refs: [`eip155://8453/tx/${String(row.tx_hash).slice(2).toLowerCase()}`] },
  };
}

function createFinancialEarningsWriter({ store, subjectId, now } = {}) {
  if (!store || typeof store.append !== "function") throw new Error("FinancialRecord store required");
  return async (row) => {
    const write = await store.append(earningsIncomeToFinancialRecord(row, {
      subjectId, recordedAt: now ? now() : row.occurred_at,
    }));
    return { ok: true, duplicate: write.created === false, entry_key: row.entry_key };
  };
}

function polymarketEarningToFinancialRecord(row, { subjectId, receipts, recordedAt } = {}) {
  const kind = {
    financial_external_income: ["business_revenue", "credit"],
    financial_realized_loss: ["business_cost", "debit"],
    financial_fee: ["fee", "debit"],
  }[row?.kind];
  const tx = String(row?.tx_hash || "").toLowerCase();
  const receipt = receipts?.[tx];
  const receiptTx = String(receipt?.transactionHash || "").toLowerCase();
  if (!kind || row?.source !== "polymarket_cycle" || receipt?.status !== "0x1"
    || !/^0x[0-9a-f]{64}$/.test(tx) || !/^0x[0-9a-f]{64}$/.test(receiptTx)
    || receiptTx !== tx) {
    throw new Error("verified Polymarket settlement row required");
  }
  const amount = Number(row.amount_minor);
  if (!Number.isSafeInteger(amount) || amount <= 0) throw new Error("Polymarket amount invalid");
  const key = `earnings-financial:v1:${row.entry_key}`;
  const recorded = new Date(recordedAt || row.occurred_at).toISOString();
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: kind[0], direction: kind[1],
    amount_minor: amount, currency: "USD",
    occurred_at: new Date(row.occurred_at).toISOString(), recorded_at: recorded,
    idempotency_key: key,
    source: { provider: "polymarket", source_type: "wallet", external_ref: tx },
    verification: { status: "verified", observed_at: recorded,
      evidence_refs: [`eip155://137/tx/${receiptTx.slice(2)}`] },
  };
}

function createPolymarketFinancialWriter({ store, subjectId, receipts, now } = {}) {
  if (!store || typeof store.append !== "function") throw new Error("FinancialRecord store required");
  return async (row) => {
    const record = polymarketEarningToFinancialRecord(row, {
      subjectId, receipts, recordedAt: now ? now() : row.occurred_at,
    });
    const write = await store.append(record);
    return { ok: true, duplicate: write.created === false, entry_key: row.entry_key };
  };
}

module.exports = {
  earningsIncomeToFinancialRecord,
  createFinancialEarningsWriter,
  polymarketEarningToFinancialRecord,
  createPolymarketFinancialWriter,
};
