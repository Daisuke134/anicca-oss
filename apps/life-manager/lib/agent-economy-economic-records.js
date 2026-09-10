"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const { financialRecordId } = require("./financial-record-contract.js");

function readJsonl(file) {
  try {
    return fs.readFileSync(file, "utf8").split("\n").filter(Boolean).map(JSON.parse);
  } catch (error) {
    if (error && error.code === "ENOENT") return [];
    throw error;
  }
}

function taskmarketCost(row, subjectId, recordedAt) {
  if (row?.source !== "taskmarket_work_attempt" || !row.payment_receipt_id
    || !(Number(row.cost_usdc) > 0)) return null;
  const amountMinor = Math.round(Number(row.cost_usdc) * 1_000_000);
  if (!Number.isSafeInteger(amountMinor) || amountMinor <= 0) return null;
  const key = `agent-economy-cost:v1:taskmarket:${row.payment_receipt_id}`;
  const occurredAt = new Date(Number(row.ts) * 1000).toISOString();
  const evidenceDigest = crypto.createHash("sha256").update(String(row.payment_receipt_id)).digest("hex");
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, key), subject_id: subjectId,
    scope: "business", kind: "business_cost", direction: "debit",
    amount_minor: amountMinor, currency: "USDC", occurred_at: occurredAt,
    recorded_at: new Date(recordedAt).toISOString(), idempotency_key: key,
    source: { provider: "blockrun", source_type: "payment_processor", external_ref: String(row.payment_receipt_id) },
    verification: { status: "verified", observed_at: new Date(recordedAt).toISOString(), evidence_refs: [`x402://receipt/${evidenceDigest}`] },
  };
}

async function persistWakeEconomicRecords({ instanceHome, wakeId, subjectId, financialStore, recordedAt }) {
  if (!financialStore || typeof financialStore.append !== "function") return [];
  const ledger = path.join(instanceHome, "state", "skills", "earn", "earn-ledger.jsonl");
  const records = readJsonl(ledger)
    .filter((row) => row?.wake === wakeId)
    .map((row) => taskmarketCost(row, subjectId, recordedAt))
    .filter(Boolean);
  for (const record of records) await financialStore.append(record);
  return records;
}

module.exports = { persistWakeEconomicRecords };
