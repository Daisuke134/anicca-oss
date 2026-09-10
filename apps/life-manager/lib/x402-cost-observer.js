"use strict";

const crypto = require("node:crypto");
const { financialRecordId } = require("./financial-record-contract.js");

function header(headers, name) {
  if (headers && typeof headers.get === "function") return headers.get(name);
  const match = Object.entries(headers || {}).find(([key]) => key.toLowerCase() === name.toLowerCase());
  return match ? match[1] : null;
}

function decodeRequired(value) {
  try {
    const parsed = JSON.parse(Buffer.from(String(value), "base64").toString("utf8"));
    const accepted = Array.isArray(parsed.accepts) ? parsed.accepts[0] : null;
    const amount = String(accepted?.amount || accepted?.maxAmountRequired || "");
    if (!/^\d+$/.test(amount) || BigInt(amount) <= 0n || accepted?.network !== "eip155:8453") return null;
    if (String(accepted?.asset || "").toLowerCase() !== "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913") return null;
    return { amount, provider: new URL(parsed.resource?.url).hostname.replace(/[^a-z0-9._:-]/gi, "_") };
  } catch { return null; }
}

function paymentKey(url, init) {
  let model = "";
  try { model = String(JSON.parse(String(init?.body || "{}")).model || ""); } catch {}
  return crypto.createHash("sha256").update(`${url}\n${model}`).digest("hex");
}

function createX402CostObserver({ store, subjectId, now = () => new Date().toISOString() }) {
  if (!store || typeof store.append !== "function") throw new Error("FinancialRecord store required");
  const requirements = new Map();
  return Object.freeze({
    async observe(url, init, response) {
      const key = paymentKey(url, init);
      if (response?.status === 402) {
        let paymentRequired = header(response.headers, "payment-required");
        if (!paymentRequired && typeof response.clone === "function") {
          try {
            const body = await response.clone().json();
            if (body?.x402 || Array.isArray(body?.accepts)) {
              paymentRequired = Buffer.from(JSON.stringify(body)).toString("base64");
            }
          } catch {}
        }
        const required = decodeRequired(paymentRequired);
        if (required) requirements.set(key, required);
        return { recorded: false };
      }
      const signature = header(init?.headers, "payment-signature");
      const receipt = header(response?.headers, "payment-response")
        || header(response?.headers, "x-payment-response");
      const required = requirements.get(key);
      if (!response?.ok || !signature || !receipt || !required) return { recorded: false };
      const amount = Number(required.amount);
      if (!Number.isSafeInteger(amount)) throw new Error("x402 cost exceeds safe USDC units");
      const digest = crypto.createHash("sha256").update(receipt).digest("hex");
      const idempotencyKey = `x402-cost:v1:${digest}`;
      const timestamp = now();
      const record = {
        schema_version: 1, record_type: "financial_record",
        record_id: financialRecordId(subjectId, idempotencyKey), subject_id: subjectId,
        scope: "business", kind: "business_cost", direction: "debit",
        amount_minor: amount, currency: "USDC", occurred_at: timestamp, recorded_at: timestamp,
        idempotency_key: idempotencyKey,
        source: { provider: required.provider || "x402", source_type: "payment_processor", external_ref: digest },
        verification: { status: "verified", observed_at: timestamp,
          evidence_refs: [`x402://receipt/${digest}`] },
      };
      const write = await store.append(record);
      return { recorded: true, duplicate: write.created === false, record_id: record.record_id };
    },
  });
}

module.exports = { decodeRequired, createX402CostObserver };
