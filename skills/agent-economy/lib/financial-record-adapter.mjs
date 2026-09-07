import { createHash } from "node:crypto";
import commonRecord from "../../../runtime/contracts/common-record.cjs";

import { isNormalizedRevenueReceipt } from "./revenue-receipt.mjs";

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DECIMALS = new Map([["JPY", 0], ["USD", 2], ["EUR", 2], ["GBP", 2], ["USDC", 6], ["USDT", 6]]);
const POSITIVE_TERMINALS = new Set(["settled", "paid", "received", "completed"]);
const REFUND_TERMINALS = new Set(["refunded", "charged_back", "chargeback", "reversed"]);
const { financialRecordId } = commonRecord;

function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

function commonId(value, label) {
  if (typeof value !== "string" || !ID.test(value)) throw new Error(`${label} is not a common ID`);
  return value;
}

function minor(value, asset) {
  const decimals = DECIMALS.get(asset);
  if (decimals === undefined) throw new Error(`unsupported FinancialRecord asset: ${asset}`);
  const text = String(value);
  if (!/^\d+(?:\.\d+)?$/.test(text)) throw new Error("revenue amount is not non-negative decimal");
  const [whole, fraction = ""] = text.split(".");
  if (fraction.length > decimals) throw new Error("revenue amount exceeds asset precision");
  const units = BigInt(`${whole}${fraction.padEnd(decimals, "0")}`);
  if (units > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("revenue amount exceeds safe minor units");
  return Number(units);
}

function exactAmount(receipt, component) {
  const exact = receipt[`${component}_decimal`];
  if (typeof exact === "string") return exact;
  const legacy = receipt[component];
  if (typeof legacy !== "number" || !Number.isFinite(legacy) || legacy < 0) {
    throw new Error(`legacy revenue ${component} is not safely representable`);
  }
  const text = String(legacy);
  const units = minor(text, receipt.asset);
  const decimals = DECIMALS.get(receipt.asset);
  if (decimals > 0 && units > (2 ** 52) - 1) {
    throw new Error(`legacy revenue ${component} cannot distinguish adjacent minor units`);
  }
  return text;
}

function proof(receipt) {
  if (receipt.proof.provider_receipt_id) {
    return {
      externalRef: receipt.proof.provider_receipt_id,
      evidenceRef: `${receipt.provider}://receipt/${hash(receipt.proof.provider_receipt_id)}`,
    };
  }
  const externalRef = `${receipt.proof.chain_id}:${receipt.proof.tx_hash}:${receipt.proof.log_index}`;
  return {
    externalRef,
    evidenceRef: `eip155://${receipt.proof.chain_id}/tx/${receipt.proof.tx_hash.slice(2)}/${receipt.proof.log_index}`,
  };
}

export function revenueReceiptToFinancialRecords(receipt, { subjectId, recordedAt = receipt?.occurred_at } = {}) {
  if (!isNormalizedRevenueReceipt(receipt)) throw new Error("normalized verified revenue receipt required");
  const subject = commonId(subjectId, "FinancialRecord subject_id");
  const recorded = new Date(recordedAt).toISOString();
  const evidence = proof(receipt);
  const scoped = hash(`${subject}\n${receipt.idempotency_key}`);
  let components;
  const gross = exactAmount(receipt, "gross");
  const fee = exactAmount(receipt, "fee");
  const refund = exactAmount(receipt, "refund");
  if (POSITIVE_TERMINALS.has(receipt.terminal_state)) {
    components = [
      ["gross", "business_revenue", "credit", gross],
      ["fee", "fee", "debit", fee],
      ["refund", "business_cost", "debit", refund],
    ];
  } else if (REFUND_TERMINALS.has(receipt.terminal_state)) {
    if (minor(refund, receipt.asset) === 0) throw new Error("refund terminal requires a positive refund amount");
    components = [["refund", "business_cost", "debit", refund]];
  } else {
    return [];
  }
  return components.filter(([, , , amount]) => minor(amount, receipt.asset) > 0).map(([component, kind, direction, amount]) => {
    const idempotencyKey = `revenue-financial:v1:${hash(`${scoped}\n${component}`)}`;
    return {
    schema_version: 1,
    record_type: "financial_record",
    record_id: financialRecordId(subject, idempotencyKey),
    subject_id: subject,
    scope: "business",
    kind,
    direction,
    amount_minor: minor(amount, receipt.asset),
    currency: receipt.asset,
    occurred_at: receipt.occurred_at,
    recorded_at: recorded,
    idempotency_key: idempotencyKey,
    source: { provider: commonId(receipt.provider, "FinancialRecord provider"), source_type: receipt.proof.tx_hash ? "wallet" : "payment_processor", external_ref: evidence.externalRef },
    verification: { status: "verified", observed_at: recorded, evidence_refs: [evidence.evidenceRef] },
    };
  });
}
