"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildFinancialReportJob,
  executeFinancialReportJob,
  appendCloudFinancialRecords,
  enqueueFinancialReportJobs,
  runCloudFinancialManagerReport,
} = require("./report-job-adapter.js");
const { buildFinancialManagerReport } = require("./financial-manager-report.js");

const NOW_MS = Date.parse("2026-08-02T11:05:00.000Z");
const WALLET = "0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad";

function memoryFinancialStore() {
  const rows = [];
  return {
    async append(record) {
      const existing = rows.find((item) => item.record_id === record.record_id);
      if (existing) return { created: false, record: existing };
      rows.push(record);
      return { created: true, record };
    },
    async read({ subjectId }) { return rows.filter((row) => row.subject_id === subjectId); },
    rows,
  };
}

function reportDeps(calls) {
  return {
    secretProvider: {
      async get(tenantId, ref) {
        calls.push({ kind: "secret", tenantId, ref });
        return "telegram-token-value";
      },
    },
    readTenant: async () => ({
      uid: "tenant-a",
      telegram_chat_id: "private-chat-id",
      agent_wallet_address: WALLET,
      notifications_enabled: true,
      call_time_zone: "Asia/Tokyo",
    }),
    readReceipt: async () => null,
    readLedger: async () => [{
      entry_key: "income-1",
      wallet_address: WALLET,
      kind: "financial_external_income",
      amount_minor: 100,
      currency: "USD",
      occurred_at: "2026-08-02T10:00:00.000Z",
      source: "x402_sale",
    }],
    readCosts: async () => [{
      id: 1,
      ts: "2026-08-02T10:30:00.000Z",
      kind: "model",
      est_usd: "0.25",
    }],
    readBalance: async () => "42000000",
    financialStore: memoryFinancialStore(),
    claimReceipt: async () => ({ claimed: true }),
    markReceiptSent: async () => true,
    markReceiptFailed: async () => true,
    sendTelegram: async (token, chatId, body) => {
      calls.push({ kind: "send", token, chatId, body });
      return {
        ok: true,
        result: {
          message_id: 987,
          date: Math.floor(NOW_MS / 1000),
        },
      };
    },
  };
}

test("financial report jobs carry only immutable refs and reject raw Telegram secrets", () => {
  const job = buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "daily",
    nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });

  assert.equal(job.tenant_id, "tenant-a");
  assert.equal(job.loop_id, "financial.report");
  assert.equal(job.capability, "report.financial.telegram");
  assert.equal(job.effect_class, "message");
  assert.match(job.effect_key, /^telegram:financial:[0-9a-f]{64}$/);
  assert.deepEqual(job.input_refs, {
    financial_report_ref: "financial-report://tenant-a/daily/2026-08-02T11%3A05%3A00.000Z",
    telegram_token_ref: "secret://telegram/bot-token",
  });
  assert.doesNotMatch(JSON.stringify(job), /telegram-token-value|private-chat-id/);

  assert.throws(() => buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "daily",
    nowMs: NOW_MS,
    telegramTokenRef: "123456:raw-secret",
  }), /secret reference/i);
});

test("financial report refs preserve case-sensitive tenant identities", async () => {
  const job = buildFinancialReportJob({
    tenantId: "Tenant-A",
    kind: "daily",
    nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  assert.match(job.input_refs.financial_report_ref, /Tenant-A/);
  await assert.doesNotReject(() => executeFinancialReportJob(job, {
    secretProvider: { get: async () => "unused" },
    runReport: async () => ({ status: "skipped", report_kind: "daily" }),
  }));
});

test("default and force-all enqueue exactly one consolidated daily Financial Manager job", async () => {
  const queued = [];
  const enqueueJob = async (job) => { queued.push(job); return { created: true }; };
  const env = { LM_TELEGRAM_TOKEN_REF: "secret://telegram/bot-token" };
  const defaults = await enqueueFinancialReportJobs(["enqueue", "--uid", "tenant-a"], env, {
    nowMs: NOW_MS, enqueueJob, stdout: { write() {} },
  });
  const all = await enqueueFinancialReportJobs(["enqueue", "--uid", "tenant-a", "--force", "all"], env, {
    nowMs: NOW_MS, enqueueJob, stdout: { write() {} },
  });
  assert.equal(defaults.length, 1);
  assert.equal(all.length, 1);
  assert.equal(queued.length, 2);
  assert.equal(queued[0].inputRefs.financial_report_ref.includes("/daily/"), true);
  assert.equal(queued[0].inputRefs.financial_report_ref.includes("force=true"), false);
  assert.equal(queued[1].inputRefs.financial_report_ref.includes("/daily/"), true);
  assert.equal(queued[1].inputRefs.financial_report_ref.includes("force=true"), true);
});

test("unforced consolidated cloud Financial Manager reports remain quiet before the scheduled window", async () => {
  const calls = [];
  const result = await runCloudFinancialManagerReport({
    uid: "tenant-a", kind: "daily", nowMs: Date.parse("2026-08-02T00:00:00.000Z"), force: false,
  }, reportDeps(calls));
  assert.deepEqual(result, { status: "skipped", reason: "not_due", report_kind: "daily" });
  assert.equal(calls.filter((call) => call.kind === "send").length, 0);
});

test("adapter routes cloud input through the shared Financial Manager body and emits a safe effect receipt", async () => {
  const calls = [];
  const job = buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "daily",
    nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const receipt = await executeFinancialReportJob(job, reportDeps(calls));
  const send = calls.find((call) => call.kind === "send");

  assert.deepEqual(calls[0], {
    kind: "secret",
    tenantId: "tenant-a",
    ref: "secret://telegram/bot-token",
  });
  assert.match(send.body, /^💰 Financial Manager/);
  assert.match(send.body, /事業（今日）\n収益：USD 1\.00/);
  assert.match(send.body, /個人資産/);
  assert.match(receipt.receipt.snapshot_hash, /^[0-9a-f]{64}$/);
  assert.deepEqual({
    chat_id_hash: receipt.receipt.chat_id_hash,
    message_id: receipt.receipt.message_id,
    snapshot_hash: receipt.receipt.snapshot_hash,
    sent_at: receipt.receipt.sent_at,
    source_freshness: receipt.receipt.source_freshness,
  }, {
    chat_id_hash: "954c1bb22e1272793a5e52f5b972719c2b9c3f05d89ef7cde70f127430865132",
    message_id: 987,
    snapshot_hash: receipt.receipt.snapshot_hash,
    sent_at: "2026-08-02T11:05:00.000Z",
    source_freshness: {
      report_cutoff_at: "2026-08-02T11:05:00.000Z",
      earnings_latest_at: "2026-08-02T10:00:00.000Z",
      costs_latest_at: "2026-08-02T10:30:00.000Z",
      balance_observed_at: "2026-08-02T11:05:00.000Z",
    },
  });
  assert.doesNotMatch(JSON.stringify(receipt.receipt), /telegram-token-value|private-chat-id/);
});

test("adapter rejects cross-tenant jobs before resolving a secret or sending", async () => {
  const calls = [];
  const job = buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "weekly",
    nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const tampered = { ...job, tenant_id: "tenant-b" };

  await assert.rejects(
    executeFinancialReportJob(tampered, reportDeps(calls)),
    /tenant scope mismatch/i,
  );
  assert.deepEqual(calls, []);
});

test("an error after Telegram dispatch is classified as an unknown external effect", async () => {
  const job = buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "daily",
    nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const deps = reportDeps([]);
  deps.sendTelegram = async () => {
    throw new Error("connection ended after request write");
  };

  await assert.rejects(
    executeFinancialReportJob(job, deps),
    (error) => error.unknownEffect === true,
  );
});

test("a first zero Base balance is a verified snapshot rather than silently stale data", async () => {
  const calls = [];
  const job = buildFinancialReportJob({
    tenantId: "tenant-a", kind: "daily", nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const deps = reportDeps(calls);
  deps.readLedger = async () => [];
  deps.readCosts = async () => [];
  deps.readBalance = async () => "0";
  const execution = await executeFinancialReportJob(job, deps);
  assert.equal(execution.result.status, "sent");
  assert.equal(calls.filter((call) => call.kind === "send").length, 1);
});

test("cloud Financial Manager rejects a wallet ledger row outside its tenant before Telegram", async () => {
  const calls = [];
  const job = buildFinancialReportJob({
    tenantId: "tenant-a", kind: "daily", nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const deps = reportDeps(calls);
  deps.readLedger = async () => [{
    entry_key: "cross-tenant", wallet_address: "0x0000000000000000000000000000000000000000",
    kind: "financial_external_income", amount_minor: 1, currency: "USD",
    occurred_at: "2026-08-02T10:00:00.000Z", source: "x402_sale",
  }];
  await assert.rejects(executeFinancialReportJob(job, deps), /wallet ledger tenant scope mismatch/i);
  assert.equal(calls.filter((call) => call.kind === "send").length, 0);
});

test("cloud Financial Manager projects valid atomic USD earnings as normalized USDC FinancialRecords", async () => {
  const store = memoryFinancialStore();
  const result = await appendCloudFinancialRecords({
    store, subjectId: "tenant-a", walletAddress: WALLET, costRows: [], balanceAtomic: "0",
    observedAt: "2026-08-02T11:05:00.000Z",
    ledgerRows: [{
      entry_key: "atomic-income", wallet_address: WALLET,
      kind: "financial_external_income", amount_atomic: "12345", amount_decimals: 4,
      currency: "USD", occurred_at: "2026-08-02T10:00:00.000Z", source: "x402_sale",
    }],
  });
  assert.equal(result.observed, 2);
  const projected = store.rows.find((row) => row.idempotency_key === "wallet-ledger:atomic-income");
  assert.equal(projected.currency, "USDC");
  assert.equal(projected.amount_minor, 1_234_500);
  assert.equal(projected.kind, "business_revenue");
});

test("cloud FinancialRecord source rows retain stable ids across different worker runs", async () => {
  const store = memoryFinancialStore();
  const source = {
    ledgerRows: [{
      entry_key: "stable-income", wallet_address: WALLET, kind: "financial_external_income",
      amount_minor: 100, currency: "USD", occurred_at: "2026-08-02T10:00:00.000Z",
      source: "x402_sale",
    }],
    costRows: [{ id: 7, ts: "2026-08-02T10:30:00.000Z", est_usd: "0.25" }],
    store, subjectId: "tenant-a", walletAddress: WALLET, balanceAtomic: "1",
  };
  const first = await appendCloudFinancialRecords({ ...source, observedAt: "2026-08-02T11:05:00.000Z" });
  const replay = await appendCloudFinancialRecords({ ...source, observedAt: "2026-08-02T12:05:00.000Z" });
  assert.equal(first.created, 3);
  assert.equal(replay.created, 1, "only the time-varying Base balance snapshot is new");
  assert.equal(store.rows.filter((row) => row.idempotency_key === "wallet-ledger:stable-income").length, 1);
  assert.equal(store.rows.filter((row) => row.idempotency_key === "api-cost:7").length, 1);
});

test("cloud Financial Manager preserves fractional API costs across stable replay", async () => {
  const store = memoryFinancialStore();
  const source = {
    ledgerRows: [], costRows: [{ id: 8, ts: "2026-08-02T10:30:00.000Z", est_usd: "0.003" }],
    store, subjectId: "tenant-a", walletAddress: WALLET, balanceAtomic: "0",
  };
  await appendCloudFinancialRecords({ ...source, observedAt: "2026-08-02T11:05:00.000Z" });
  await appendCloudFinancialRecords({ ...source, observedAt: "2026-08-02T12:05:00.000Z" });
  const projected = store.rows.filter((row) => row.idempotency_key === "api-cost:8");
  assert.equal(projected.length, 1);
  assert.equal(projected[0].amount_minor, 1);
  assert.equal(projected[0].currency, "USD");
  assert.equal(projected[0].verification.status, "unverified");
});

test("a zero Base balance snapshot supersedes a prior positive balance", async () => {
  const store = memoryFinancialStore();
  const common = { store, subjectId: "tenant-a", walletAddress: WALLET, ledgerRows: [], costRows: [] };
  await appendCloudFinancialRecords({ ...common, balanceAtomic: "42000000", observedAt: "2026-08-02T11:05:00.000Z" });
  await appendCloudFinancialRecords({ ...common, balanceAtomic: "0", observedAt: "2026-08-03T11:05:00.000Z" });
  const { report } = buildFinancialManagerReport(await store.read({ subjectId: "tenant-a" }), "2026-08-03");
  assert.deepEqual(report.personal.assets, [{ currency: "USDC", amountMinor: 0 }]);
});

test("a failed receipt claim only duplicates after a complete durable sent receipt", async () => {
  const job = buildFinancialReportJob({
    tenantId: "tenant-a", kind: "daily", nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  for (const proof of [
    { status: "pending" }, { status: "failed" }, null,
  ]) {
    const calls = [];
    const deps = reportDeps(calls);
    let reads = 0;
    deps.readReceipt = async () => (++reads === 1 ? null : proof);
    deps.claimReceipt = async () => ({ claimed: false });
    await assert.rejects(
      executeFinancialReportJob(job, deps),
      (error) => error.unknownEffect === true && /claim_unresolved/.test(error.message),
    );
    assert.equal(calls.filter((call) => call.kind === "send").length, 0);
  }

  const calls = [];
  const deps = reportDeps(calls);
  let reads = 0;
  deps.readReceipt = async () => (++reads === 1 ? null : {
    status: "sent", telegram_message_id: 55, snapshot_hash: "a".repeat(64),
    sent_at: "2026-08-02T11:05:01.000Z", period_end: "2026-08-02T11:05:00.000Z",
  });
  deps.claimReceipt = async () => ({ claimed: false });
  const duplicate = await executeFinancialReportJob(job, deps);
  assert.equal(duplicate.receipt.status, "duplicate");
  assert.equal(duplicate.receipt.message_id, 55);
  assert.equal(calls.filter((call) => call.kind === "send").length, 0);
});

test("a missing Telegram provider receipt is an unknown effect, not a successful job", async () => {
  const job = buildFinancialReportJob({
    tenantId: "tenant-a", kind: "daily", nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const deps = reportDeps([]);
  deps.sendTelegram = async () => ({ ok: true, result: {} });
  await assert.rejects(
    executeFinancialReportJob(job, deps),
    (error) => error.unknownEffect === true && /provider_receipt_missing/.test(error.message),
  );
});

test("a duplicate reconciles the existing real Telegram effect into the runtime receipt", async () => {
  const job = buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "daily",
    nowMs: NOW_MS,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  const execution = await executeFinancialReportJob(job, {
    secretProvider: { get: async () => "unused-token" },
    runReport: async () => ({
      status: "duplicate",
      report_kind: "daily",
      period_key: "2026-08-02",
      telegram_message_id: 123,
      snapshot_hash: "a".repeat(64),
      chat_id_hash: "b".repeat(64),
      sent_at: "2026-08-02T11:05:01.000Z",
      source_freshness: {
        report_cutoff_at: "2026-08-02T11:05:00.000Z",
        earnings_latest_at: null,
        costs_latest_at: null,
        balance_observed_at: null,
      },
    }),
  });

  assert.deepEqual(execution.receipt, {
    schema_version: 1,
    kind: "telegram_financial_report",
    status: "duplicate",
    report_kind: "daily",
    period_key: "2026-08-02",
    chat_id_hash: "b".repeat(64),
    message_id: 123,
    snapshot_hash: "a".repeat(64),
    sent_at: "2026-08-02T11:05:01.000Z",
    source_freshness: {
      report_cutoff_at: "2026-08-02T11:05:00.000Z",
      earnings_latest_at: null,
      costs_latest_at: null,
      balance_observed_at: null,
    },
  });
});

test("a forced runtime job uses the durable job receipt as its one-shot dedupe boundary", async () => {
  const job = buildFinancialReportJob({
    tenantId: "tenant-a",
    kind: "weekly",
    nowMs: NOW_MS,
    force: true,
    telegramTokenRef: "secret://telegram/bot-token",
  });
  let runtimeDeps;
  const execution = await executeFinancialReportJob(job, {
    secretProvider: { get: async () => "token" },
    runReport: async (_request, receivedDeps) => {
      runtimeDeps = receivedDeps;
      return { status: "skipped", report_kind: "weekly", reason: "fixture" };
    },
  });

  assert.equal((await runtimeDeps.readReceipt()), null);
  assert.deepEqual(await runtimeDeps.claimReceipt(), { claimed: true });
  assert.equal(await runtimeDeps.markReceiptSent(), true);
  assert.equal(await runtimeDeps.markReceiptFailed(), true);
  assert.equal(execution.receipt.status, "skipped");
});
