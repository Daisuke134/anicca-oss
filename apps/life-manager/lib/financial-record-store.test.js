"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createJsonlFinancialRecordStore,
  createPostgresFinancialRecordStore,
} = require("./financial-record-store.js");
const { financialRecordId } = require("../../../runtime/contracts/common-record.cjs");
const MIGRATION = fs.readFileSync(path.join(
  __dirname, "../migrations/2026-09-07-lm-financial-records.sql",
), "utf8");

function record(overrides = {}) {
  const value = {
    schema_version: 1,
    record_type: "financial_record",
    record_id: null,
    subject_id: "tenant-a",
    scope: "business",
    kind: "business_revenue",
    direction: "credit",
    amount_minor: 12500,
    currency: "JPY",
    occurred_at: "2026-09-07T00:00:00.000Z",
    recorded_at: "2026-09-07T00:01:00.000Z",
    idempotency_key: "stripe:payment:1:revenue",
    source: {
      provider: "stripe",
      source_type: "payment_processor",
      external_ref: "payment-1",
    },
    verification: {
      status: "verified",
      observed_at: "2026-09-07T00:01:00.000Z",
      evidence_refs: ["stripe://payment/payment-1"],
    },
    ...overrides,
  };
  if (overrides.record_id === undefined) {
    value.record_id = financialRecordId(value.subject_id, value.idempotency_key);
  }
  return value;
}

test("JSONL store appends immutable tenant records once and reads only that tenant", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-financial-record-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const directoryPath = path.join(root, "records");
  const store = createJsonlFinancialRecordStore({ directoryPath });

  assert.deepEqual(await store.append(record()), { created: true, record: record() });
  assert.deepEqual(await store.append(record()), { created: false, record: record() });
  assert.deepEqual(await store.read({ subjectId: "tenant-a" }), [record()]);
  assert.deepEqual(await store.read({ subjectId: "tenant-b" }), []);
  const shards = fs.readdirSync(directoryPath);
  assert.equal(shards.length, 1);
  assert.equal(fs.statSync(path.join(directoryPath, shards[0])).mode & 0o777, 0o600);

  await assert.rejects(
    store.append(record({ record_id: "stripe-payment-2", amount_minor: 999 })),
    /record_id invalid/i,
  );
});

test("Postgres store uses one tenant-scoped immutable append/read boundary", async () => {
  const calls = [];
  const query = async (sql, params) => {
    calls.push({ sql, params });
    if (/INSERT INTO public\.lm_financial_records/i.test(sql)) {
      return { rows: [{ record: record() }] };
    }
    return { rows: [{ record: record() }] };
  };
  const store = createPostgresFinancialRecordStore({ query });

  assert.deepEqual(await store.append(record()), { created: true, record: record() });
  assert.deepEqual(await store.read({
    subjectId: "tenant-a", scope: "business", since: "2026-09-01T00:00:00Z",
  }), [record()]);
  assert.match(calls[0].sql, /ON CONFLICT DO NOTHING/i);
  assert.deepEqual(calls[1].params, ["tenant-a", "business", "2026-09-01T00:00:00.000Z"]);
  assert.match(calls[1].sql, /subject_id = \$1/i);
});

test("store rejects unverified claims disguised as verified records", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-financial-record-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = createJsonlFinancialRecordStore({ directoryPath: path.join(root, "records") });

  await assert.rejects(
    store.append(record({ verification: {
      status: "verified",
      observed_at: "2026-09-07T00:01:00.000Z",
      evidence_refs: [],
    } })),
    /verification evidence/i,
  );
  await assert.rejects(
    store.append(record({
      kind: "transfer", scope: "personal", direction: "snapshot",
      verification: {
        status: "unverified",
        observed_at: "2026-09-07T00:01:00.000Z",
        evidence_refs: [],
      },
    })),
    /direction/i,
  );
});

test("cloud table is immutable, tenant-indexed and private to the service role", () => {
  assert.match(MIGRATION, /PRIMARY KEY \(subject_id, record_id\)/i);
  assert.match(MIGRATION, /UNIQUE \(subject_id, idempotency_key\)/i);
  assert.match(MIGRATION, /record->>'occurred_at'\)::timestamptz = occurred_at/i);
  assert.match(MIGRATION, /ENABLE ROW LEVEL SECURITY/i);
  assert.match(MIGRATION, /REVOKE ALL[^;]+FROM PUBLIC, anon, authenticated/i);
  assert.match(MIGRATION, /GRANT SELECT, INSERT[^;]+TO service_role/i);
  assert.match(MIGRATION, /FOR SELECT TO service_role USING \(true\)/i);
  assert.match(MIGRATION, /FOR INSERT TO service_role WITH CHECK \(true\)/i);
  assert.match(MIGRATION, /BEFORE UPDATE OR DELETE/i);
});
