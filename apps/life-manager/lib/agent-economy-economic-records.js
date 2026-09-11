"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const { financialRecordId } = require("./financial-record-contract.js");
const { main: recordTaskMarketWork } = require("../scripts/record-taskmarket-work.js");

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

function taskMarketFinancialRecords(row, subjectId, recordedAt) {
  const gross = Number(row?.meta?.gross_atomic);
  const fee = Number(row?.meta?.platform_fee_atomic);
  if (!Number.isSafeInteger(gross) || gross <= 0 || !Number.isSafeInteger(fee) || fee < 0
    || gross - fee !== Number(row?.amount_atomic)) throw new Error("TaskMarket financial amount invalid");
  const base = {
    schema_version: 1, record_type: "financial_record", subject_id: subjectId,
    scope: "business", currency: "USDC", occurred_at: new Date(row.occurred_at).toISOString(),
    recorded_at: new Date(recordedAt).toISOString(),
    source: { provider: "taskmarket", source_type: "marketplace", external_ref: row.tx_hash },
    verification: { status: "verified", observed_at: new Date(recordedAt).toISOString(),
      evidence_refs: [`eip155://8453/tx/${String(row.tx_hash).slice(2)}`] },
  };
  const components = [["gross", "business_revenue", "credit", gross], ["fee", "fee", "debit", fee]];
  return components.filter(([, , , amount]) => amount > 0).map(([component, kind, direction, amount_minor]) => {
    const idempotency_key = `taskmarket-financial:v1:${row.entry_key}:${component}`;
    return { ...base, record_id: financialRecordId(subjectId, idempotency_key), idempotency_key,
      kind, direction, amount_minor };
  });
}

async function persistTaskMarketRevenue({ identity, financialStore, recordedAt, fetchImpl = globalThis.fetch,
  selfWallets = [], recordTaskMarket = recordTaskMarketWork }) {
  if (!financialStore || typeof financialStore.append !== "function") return { recorded: 0 };
  const configuredSelfWallets = selfWallets.length ? selfWallets
    : (await import("../../../skills/earn/x402-sell/lib/self-wallets.mjs")).SELF_WALLETS;
  let storeFailure = null;
  try {
    return await recordTaskMarket({
      workerAddress: identity.wallet.address,
      selfWallets: [...new Set([...configuredSelfWallets, identity.wallet.address])],
      fetchImpl,
      recordEntry: async (row) => {
        try {
          const records = taskMarketFinancialRecords(row, identity.tenant_id, recordedAt);
          const writes = await Promise.all(records.map((record) => financialStore.append(record)));
          return { ok: true, duplicate: writes.every((write) => write.created === false) };
        } catch (error) {
          storeFailure = error;
          throw error;
        }
      },
      now: () => new Date(recordedAt),
      writeOutput: () => {},
    }, []);
  } catch (error) {
    if (storeFailure) throw storeFailure;
    return { recorded: 0, scan_error: true };
  }
}

module.exports = { persistWakeEconomicRecords, persistTaskMarketRevenue, taskMarketFinancialRecords };
