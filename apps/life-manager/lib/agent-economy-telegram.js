"use strict";

const { projectFinancialRecord } = require("./financial-record-contract.js");

function amount(record) {
  const digits = record.currency === "USDC" ? 6 : record.currency === "JPY" ? 0 : 2;
  const scale = 10n ** BigInt(digits);
  const units = BigInt(record.amount_minor);
  const whole = units / scale;
  const fraction = digits ? `.${String(units % scale).padStart(digits, "0").replace(/0+$/, "").padEnd(2, "0")}` : "";
  return record.currency === "JPY" ? `¥${whole}${fraction}` : `${record.currency} ${whole}${fraction}`;
}

function renderFinancialTransition(raw) {
  const record = projectFinancialRecord(raw);
  if (record.verification.status !== "verified") throw new Error("verified FinancialRecord required");
  const labels = {
    business_revenue: ["収益を受け取りました", "+"],
    business_cost: ["費用を支払いました", "-"],
    fee: ["手数料を支払いました", "-"],
    payout: ["入金を確認しました", "+"],
  };
  const label = labels[record.kind];
  if (!label) return null;
  return [
    "💰 Agent Economy",
    label[0],
    `${label[1]}${amount(record)}`,
    `提供元：${record.source.provider}`,
    `時刻：${record.occurred_at}`,
  ].join("\n");
}

async function deliverFinancialTransition({ record: raw, deliveryStore, notify, now = new Date() } = {}) {
  const record = projectFinancialRecord(raw);
  const message = renderFinancialTransition(record);
  if (!message) return { status: "quiet", reason: "non_transition_record", delivered: false };
  if (!deliveryStore || typeof deliveryStore.lookup !== "function"
    || typeof deliveryStore.claim !== "function" || typeof deliveryStore.markDelivered !== "function") {
    throw new Error("Agent Economy Telegram delivery store required");
  }
  const eventKey = `agent-economy:financial:${record.record_id}`;
  const existing = await deliveryStore.lookup({ eventKey, record });
  if (existing) return { status: "quiet", reason: "duplicate", delivered: false,
    providerMessageId: String(existing.provider_message_id) };
  const claim = await deliveryStore.claim({ eventKey, record, observedAt: new Date(now).toISOString() });
  if (!claim || claim.claimed !== true) {
    const raced = await deliveryStore.lookup({ eventKey, record });
    if (raced?.provider_message_id) return { status: "quiet", reason: "duplicate", delivered: false,
      providerMessageId: String(raced.provider_message_id) };
    return { status: "failed", reason: "delivery_claim_unresolved", delivered: false, unknownEffect: true };
  }
  if (typeof notify !== "function") throw new Error("Agent Economy Telegram notifier required");
  const sent = await notify({ eventKey, message, observedAt: new Date(now).toISOString() });
  if (!sent || sent.delivered !== true || !String(sent.providerMessageId || "").trim()) {
    return { status: "failed", reason: "telegram_provider_receipt_missing", delivered: false };
  }
  await deliveryStore.markDelivered({ eventKey, record, provider_message_id: String(sent.providerMessageId),
    delivered_at: new Date(now).toISOString() });
  return { status: "sent", reason: null, delivered: true,
    providerMessageId: String(sent.providerMessageId), eventKey };
}

module.exports = { renderFinancialTransition, deliverFinancialTransition };
