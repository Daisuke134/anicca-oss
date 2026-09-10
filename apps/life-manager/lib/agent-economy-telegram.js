"use strict";

const { projectFinancialRecord } = require("./financial-record-contract.js");
const { sendMessage } = require("./telegram.js");

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
    const unknownEffect = Boolean(sent?.unknownEffect || sent?.delivered === true);
    if (!unknownEffect && typeof deliveryStore.release === "function") {
      await deliveryStore.release({ eventKey, record });
    }
    return { status: "failed", reason: "telegram_provider_receipt_missing", delivered: false,
      unknownEffect };
  }
  await deliveryStore.markDelivered({ eventKey, record, provider_message_id: String(sent.providerMessageId),
    delivered_at: new Date(now).toISOString() });
  return { status: "sent", reason: null, delivered: true,
    providerMessageId: String(sent.providerMessageId), eventKey };
}

async function deliverFinancialTransitionWithOutbox({ record: raw, notify, now = new Date() } = {}) {
  const record = projectFinancialRecord(raw);
  const message = renderFinancialTransition(record);
  if (!message) return { status: "quiet", reason: "non_transition_record", delivered: false };
  if (typeof notify !== "function") throw new Error("financial transition outbox notifier required");
  const eventKey = `agent-economy:financial:${record.record_id}`;
  const result = await notify({ eventKey, message, observedAt: new Date(now).toISOString() });
  const providerMessageId = String(result?.provider_message_id || "").trim();
  if (result?.delivery !== "delivered" || !providerMessageId) {
    return { status: "failed", reason: "telegram_provider_receipt_missing", delivered: false,
      unknownEffect: ["sending", "delivery_uncertain"].includes(result?.delivery)
        || result?.delivery === "delivered" };
  }
  return { status: Number(result.attempted) === 0 ? "quiet" : "sent",
    reason: Number(result.attempted) === 0 ? "duplicate" : null,
    delivered: Number(result.attempted) !== 0, providerMessageId, eventKey };
}

function createFinancialTransitionStore({ store, deliver } = {}) {
  if (!store || typeof store.append !== "function" || typeof store.read !== "function") {
    throw new Error("FinancialRecord store required");
  }
  if (typeof deliver !== "function") throw new Error("financial transition delivery required");
  return Object.freeze({
    async append(record) {
      const write = await store.append(record);
      const notification = await deliver(write.record);
      if (notification?.status === "failed") {
        const error = new Error(`financial transition delivery failed: ${notification.reason || "unknown"}`);
        error.code = "FINANCIAL_TRANSITION_DELIVERY_FAILED";
        error.unknownEffect = Boolean(notification.unknownEffect);
        throw error;
      }
      return { ...write, notification };
    },
    read: (input) => store.read(input),
  });
}

function createPostgresFinancialTransitionDeliveryStore({ query } = {}) {
  if (typeof query !== "function") throw new Error("financial transition Postgres query required");
  return Object.freeze({
    async lookup({ eventKey, record }) {
      const rows = (await query(`
        SELECT telegram_message_id AS provider_message_id
        FROM public.lm_financial_transition_receipts
        WHERE subject_id = $1 AND event_key = $2 AND status = 'sent'
        LIMIT 1
      `, [record.subject_id, eventKey])).rows;
      if (rows.length > 1) throw new Error("financial transition receipt lookup failed");
      return rows[0] || null;
    },
    async claim({ eventKey, record, observedAt }) {
      const rows = (await query(`
        INSERT INTO public.lm_financial_transition_receipts
          (subject_id, event_key, record_id, status, created_at, updated_at)
        VALUES ($1, $2, $3, 'pending', $4::timestamptz, $4::timestamptz)
        ON CONFLICT DO NOTHING
        RETURNING event_key
      `, [record.subject_id, eventKey, record.record_id, observedAt])).rows;
      return { claimed: rows.length === 1 };
    },
    async markDelivered({ eventKey, record, provider_message_id, delivered_at }) {
      const rows = (await query(`
        UPDATE public.lm_financial_transition_receipts
        SET status = 'sent', telegram_message_id = $3::bigint,
            sent_at = $4::timestamptz, updated_at = $4::timestamptz
        WHERE subject_id = $1 AND event_key = $2 AND status = 'pending'
        RETURNING event_key
      `, [record.subject_id, eventKey, provider_message_id, delivered_at])).rows;
      if (rows.length !== 1) throw new Error("financial transition delivery lost its claim");
      return true;
    },
    async release({ eventKey, record }) {
      const rows = (await query(`
        DELETE FROM public.lm_financial_transition_receipts
        WHERE subject_id = $1 AND event_key = $2 AND status = 'pending'
        RETURNING event_key
      `, [record.subject_id, eventKey])).rows;
      if (rows.length !== 1) throw new Error("financial transition release lost its claim");
      return true;
    },
  });
}

function createCloudFinancialTransitionStore({ store, query, readTenant, telegramToken, sendTelegram = sendMessage,
  now = () => new Date() } = {}) {
  if (typeof readTenant !== "function") throw new Error("financial transition tenant reader required");
  const deliveryStore = createPostgresFinancialTransitionDeliveryStore({ query });
  return createFinancialTransitionStore({ store, deliver: async (record) => {
    const tenant = await readTenant(record.subject_id);
    if (!tenant || tenant.notifications_enabled === false) {
      return { status: "quiet", reason: "notifications_disabled", delivered: false };
    }
    if (!String(tenant.telegram_chat_id || "").trim()) {
      return { status: "quiet", reason: "telegram_unbound", delivered: false };
    }
    return deliverFinancialTransition({ record, deliveryStore, now: now(), notify: async ({ message }) => {
      const result = await sendTelegram(telegramToken, String(tenant.telegram_chat_id), message);
      return { delivered: Boolean(result?.ok), providerMessageId: result?.result?.message_id,
        unknownEffect: Boolean(result?.delivery_unknown || (result?.ok && !result?.result?.message_id)) };
    } });
  } });
}

module.exports = {
  createCloudFinancialTransitionStore,
  createFinancialTransitionStore,
  createPostgresFinancialTransitionDeliveryStore,
  renderFinancialTransition,
  deliverFinancialTransition,
  deliverFinancialTransitionWithOutbox,
};
