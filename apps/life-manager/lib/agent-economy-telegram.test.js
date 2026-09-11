"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { financialRecordId } = require("./financial-record-contract.js");
const {
  createFinancialTransitionStore,
  createCloudFinancialTransitionStore,
  createPostgresFinancialTransitionDeliveryStore,
  renderFinancialTransition,
  deliverFinancialTransition,
} = require("./agent-economy-telegram.js");

function record(overrides = {}) {
  const subject = "tenant-a";
  const key = "economic:event:1";
  return { schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subject, key), subject_id: subject, scope: "business",
    kind: "business_revenue", direction: "credit", amount_minor: 1200000, currency: "USDC",
    occurred_at: "2026-09-11T04:00:00Z", recorded_at: "2026-09-11T04:00:01Z",
    idempotency_key: key, source: { provider: "taskmarket", source_type: "marketplace", external_ref: "award-1" },
    verification: { status: "verified", observed_at: "2026-09-11T04:00:01Z",
      evidence_refs: ["taskmarket://receipt/abc"] }, ...overrides };
}

test("renders a concise natural-language revenue or cost transition", () => {
  assert.match(renderFinancialTransition(record()), /収益を受け取りました\n\+USDC 1\.20/);
  const cost = record({ kind: "business_cost", direction: "debit", amount_minor: 2000 });
  assert.match(renderFinancialTransition(cost), /費用を支払いました\n-USDC 0\.002/);
});

test("identical replay sends once and returns the stored provider message id", async () => {
  const deliveries = new Map();
  let sends = 0;
  const deliveryStore = {
    lookup: async ({ eventKey }) => deliveries.get(eventKey) || null,
    claim: async () => ({ claimed: true }),
    markDelivered: async (value) => { deliveries.set(value.eventKey, value); },
  };
  const notify = async () => { sends += 1; return { delivered: true, providerMessageId: 7001 }; };
  const first = await deliverFinancialTransition({ record: record(), deliveryStore, notify,
    now: "2026-09-11T04:01:00Z" });
  const second = await deliverFinancialTransition({ record: record(), deliveryStore, notify,
    now: "2026-09-11T04:02:00Z" });
  assert.equal(first.status, "sent");
  assert.deepEqual([second.status, second.reason, second.providerMessageId], ["quiet", "duplicate", "7001"]);
  assert.equal(sends, 1);
});

test("a raced claim reads delivery proof and an unreceipted send never becomes delivered", async () => {
  const raced = await deliverFinancialTransition({ record: record(), deliveryStore: {
    lookup: async () => ({ provider_message_id: 8001 }), claim: async () => ({ claimed: false }),
    markDelivered: async () => { throw new Error("must not mark"); },
  }, notify: async () => { throw new Error("must not send"); } });
  assert.equal(raced.providerMessageId, "8001");

  const failed = await deliverFinancialTransition({ record: record(), deliveryStore: {
    lookup: async () => null, claim: async () => ({ claimed: true }), markDelivered: async () => {},
  }, notify: async () => ({ delivered: true, providerMessageId: null }) });
  assert.deepEqual([failed.status, failed.delivered], ["failed", false]);
});

test("unverified records and balance snapshots never create money transition messages", async () => {
  assert.throws(() => renderFinancialTransition(record({ verification: {
    status: "unverified", observed_at: "2026-09-11T04:00:01Z", evidence_refs: [],
  } })), /verified/i);
  assert.equal(renderFinancialTransition(record({ kind: "asset_balance", direction: "snapshot", scope: "personal" })), null);
});

test("FinancialRecord store retries delivery on duplicate writes without duplicating persistence", async () => {
  let writes = 0;
  let deliveries = 0;
  const raw = record();
  const store = createFinancialTransitionStore({
    store: {
      append: async () => ({ created: writes++ === 0, record: raw }),
      read: async () => [raw],
    },
    deliver: async () => ({ status: deliveries++ === 0 ? "sent" : "quiet" }),
  });
  assert.equal((await store.append(raw)).created, true);
  assert.equal((await store.append(raw)).created, false);
  assert.equal(deliveries, 2);
  assert.deepEqual(await store.read({ subjectId: "tenant-a" }), [raw]);
});

test("FinancialRecord store fails its completion boundary until notification is receipted", async () => {
  const raw = record();
  const store = createFinancialTransitionStore({
    store: { append: async () => ({ created: true, record: raw }), read: async () => [raw] },
    deliver: async () => ({ status: "failed", reason: "telegram_provider_receipt_missing", unknownEffect: true }),
  });
  await assert.rejects(store.append(raw), (error) => (
    error.code === "FINANCIAL_TRANSITION_DELIVERY_FAILED" && error.unknownEffect === true
  ));
});

test("Postgres transition store claims once, persists provider receipt, then resolves replay", async () => {
  const rows = new Map();
  const query = async (sql, values) => {
    const key = `${values[0]}:${values[1]}`;
    if (sql.includes("INSERT INTO")) {
      if (rows.has(key)) return { rows: [] };
      rows.set(key, { status: "pending", record_id: values[2] });
      return { rows: [{ event_key: values[1] }] };
    }
    if (sql.includes("UPDATE public")) {
      const current = rows.get(key);
      if (!current || current.status !== "pending") return { rows: [] };
      rows.set(key, { ...current, status: "sent", provider_message_id: String(values[2]) });
      return { rows: [{ event_key: values[1] }] };
    }
    if (sql.includes("DELETE FROM")) {
      const deleted = rows.delete(key);
      return { rows: deleted ? [{ event_key: values[1] }] : [] };
    }
    const current = rows.get(key);
    return { rows: current?.status === "sent" ? [current] : [] };
  };
  const deliveryStore = createPostgresFinancialTransitionDeliveryStore({ query });
  const input = { eventKey: "agent-economy:financial:abc", record: record(), observedAt: "2026-09-11T04:01:00Z" };
  assert.deepEqual(await deliveryStore.claim(input), { claimed: true });
  assert.deepEqual(await deliveryStore.claim(input), { claimed: false });
  await deliveryStore.markDelivered({ ...input, provider_message_id: "9001", delivered_at: input.observedAt });
  assert.equal((await deliveryStore.lookup(input)).provider_message_id, "9001");
});

test("Cloud store sends a created record and identical replay resolves from Postgres receipt", async () => {
  const receipts = new Map();
  let sends = 0;
  let appends = 0;
  const query = async (sql, values) => {
    const key = `${values[0]}:${values[1]}`;
    if (sql.includes("INSERT INTO")) {
      if (receipts.has(key)) return { rows: [] };
      receipts.set(key, { status: "pending" });
      return { rows: [{ event_key: values[1] }] };
    }
    if (sql.includes("UPDATE public")) {
      receipts.set(key, { status: "sent", provider_message_id: String(values[2]) });
      return { rows: [{ event_key: values[1] }] };
    }
    if (sql.includes("DELETE FROM")) {
      const deleted = receipts.delete(key);
      return { rows: deleted ? [{ event_key: values[1] }] : [] };
    }
    const item = receipts.get(key);
    return { rows: item?.status === "sent" ? [item] : [] };
  };
  const raw = record();
  const store = createCloudFinancialTransitionStore({
    store: {
      append: async () => ({ created: appends++ === 0, record: raw }),
      read: async () => [raw],
    },
    query,
    readTenant: async () => ({ telegram_chat_id: "private", notifications_enabled: true }),
    telegramToken: "token",
    sendTelegram: async () => ({ ok: true, result: { message_id: ++sends } }),
    now: () => new Date("2026-09-11T04:01:00Z"),
  });
  assert.equal((await store.append(raw)).notification.status, "sent");
  assert.equal((await store.append(raw)).notification.reason, "duplicate");
  assert.equal(sends, 1);
});

test("Cloud store releases a known Telegram rejection so replay can retry", async () => {
  const receipts = new Map();
  let sends = 0;
  const query = async (sql, values) => {
    const key = `${values[0]}:${values[1]}`;
    if (sql.includes("INSERT INTO")) {
      if (receipts.has(key)) return { rows: [] };
      receipts.set(key, { status: "pending" });
      return { rows: [{ event_key: values[1] }] };
    }
    if (sql.includes("DELETE FROM")) {
      const deleted = receipts.delete(key);
      return { rows: deleted ? [{ event_key: values[1] }] : [] };
    }
    if (sql.includes("UPDATE public")) return { rows: [{ event_key: values[1] }] };
    return { rows: [] };
  };
  const raw = record();
  const store = createCloudFinancialTransitionStore({
    store: { append: async () => ({ created: false, record: raw }), read: async () => [raw] },
    query, readTenant: async () => ({ telegram_chat_id: "private", notifications_enabled: true }),
    telegramToken: "token", sendTelegram: async () => (++sends === 1
      ? { ok: false, error_code: 400 }
      : { ok: true, result: { message_id: 77 } }),
  });
  await assert.rejects(store.append(raw), /financial transition delivery failed/);
  assert.equal((await store.append(raw)).notification.status, "sent");
  assert.equal(sends, 2);
});

test("Cloud store retains a claim when Telegram says sent without a provider receipt", async () => {
  const receipts = new Map();
  let sends = 0;
  const query = async (sql, values) => {
    const key = `${values[0]}:${values[1]}`;
    if (sql.includes("INSERT INTO")) {
      if (receipts.has(key)) return { rows: [] };
      receipts.set(key, { status: "pending" });
      return { rows: [{ event_key: values[1] }] };
    }
    if (sql.includes("DELETE FROM")) throw new Error("unknown send must retain claim");
    return { rows: [] };
  };
  const raw = record();
  const store = createCloudFinancialTransitionStore({
    store: { append: async () => ({ created: false, record: raw }), read: async () => [raw] },
    query, readTenant: async () => ({ telegram_chat_id: "private", notifications_enabled: true }),
    telegramToken: "token", sendTelegram: async () => (sends++, { ok: true, result: {} }),
  });
  await assert.rejects(store.append(raw), (error) => error.unknownEffect === true);
  await assert.rejects(store.append(raw), (error) => error.unknownEffect === true);
  assert.equal(sends, 1);
});

test("Cloud receipt migration accepts the canonical prefixed FinancialRecord ID", () => {
  const sql = fs.readFileSync(path.join(__dirname,
    "../migrations/2026-09-11-lm-financial-transition-receipts.sql"), "utf8");
  assert.match(sql, /record_id ~ '\^financial:\[0-9a-f\]\{64\}\$'/);
  assert.match(sql, /GRANT SELECT, INSERT, UPDATE, DELETE/);
  assert.match(sql, /FOR DELETE TO service_role USING \(true\)/);
  assert.equal(record().record_id.length, 74);
});
