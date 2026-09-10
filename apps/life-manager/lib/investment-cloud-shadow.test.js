"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { exportState } = require("../scripts/investment-cutover-state.js");
const { makeInvestmentCloudWake, makeInvestmentCloudShadowWake,
  runInvestmentCloud, runInvestmentCloudShadow } = require("./investment-cloud-shadow.js");

function seededBundle() {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), "investment-shadow-source-"));
  fs.writeFileSync(path.join(state, "risk-day.json"), JSON.stringify({ ny_day: "2026-09-10", baseline_equity: "66",
    baseline_observed_at: "2026-09-10T12:00:00Z", baseline_bank_cash_flow: "0", baseline_trade_activity_ids: [],
    baseline_trades_clean: true, crypto_cash_flow: "0", transfers: {} }));
  fs.writeFileSync(path.join(state, "receipts.jsonl"), "");
  fs.writeFileSync(path.join(state, "telegram-outbox.sqlite3"), Buffer.from("SQLite format 3\0fixture"));
  return exportState({ stateDir: state, accountId: "account-1",
    cutover: { status: "ready", local_stopped_at: "2026-09-10T11:57:00Z",
      queues_drained_at: "2026-09-10T11:58:00Z", broker_reconciled_at: "2026-09-10T11:59:00Z",
      source_release_sha: "a".repeat(40) }, now: "2026-09-10T12:00:00.000Z" });
}

test("one cloud shadow wake verifies account binding, invokes the same core, reports, and persists state", async () => {
  const saved = [];
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), "investment-shadow-volume-"));
  const result = await runInvestmentCloudShadow({
    tenantId: "tenant-1", sealed: seededBundle(), secretProvider: { get: async (_tenant, ref) => ({
      "secret://alpaca/api-key": "key", "secret://alpaca/api-secret": "secret",
      "secret://telegram/bot-token": "telegram",
    })[ref] }, telegramChatId: "chat-1", alpacaCli: "/app/.bin/alpaca",
    readAccountId: async () => "account-1", stateRoot,
    runCore: async ({ stateDir, credentialsFile, env }) => {
      assert.equal(fs.statSync(credentialsFile).mode & 0o777, 0o600);
      assert.equal(env.LIFE_MANAGER_INVESTMENT_MODE, "shadow");
      assert.equal(env.LIFE_MANAGER_INVESTMENT_DEPLOYMENT, "cloud");
      assert.equal(env.ALPACA_INVESTMENT_STATE_DIR, undefined);
      assert.equal(env.ALPACA_INVESTMENT_SHADOW_STATE_DIR, stateDir);
      assert.equal(env.LM_TELEGRAM_BOT_TOKEN, "telegram");
      fs.writeFileSync(path.join(stateDir, "telegram-latest.json"), '{"message_id":"42"}\n');
      return { status: "allocated", mode: "shadow", deployment: "cloud", effect: "none", telegram_message_id: "42" };
    },
    persist: async (tenantId, bundle) => saved.push({ tenantId, bundle }),
  });
  assert.equal(result.effect, "none");
  assert.equal(result.telegram_message_id, "42");
  assert.equal(saved.length, 1);
  assert.equal(saved[0].tenantId, "tenant-1");
  assert.equal(Object.hasOwn(saved[0].bundle.bundle.files, "telegram-latest.json"), true);
  assert.equal(JSON.stringify(saved).includes('"secret"'), false);
});

test("account mismatch fails before core execution", async () => {
  let ran = false;
  await assert.rejects(runInvestmentCloudShadow({ tenantId: "tenant-1", sealed: seededBundle(),
    secretProvider: { get: async () => "value" }, telegramChatId: "chat",
    stateRoot: fs.mkdtempSync(path.join(os.tmpdir(), "investment-shadow-volume-")),
    readAccountId: async () => "other-account", runCore: async () => { ran = true; },
    persist: async () => {} }), /binding/);
  assert.equal(ran, false);
});

test("durable volume keeps outbox/receipt state across a send-window crash and rejects stale re-import", async () => {
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), "investment-shadow-crash-volume-"));
  const input = { tenantId: "tenant-1", sealed: seededBundle(),
    secretProvider: { get: async () => "value" }, telegramChatId: "chat", stateRoot,
    readAccountId: async () => "account-1", persist: async () => {} };
  await assert.rejects(runInvestmentCloudShadow({ ...input, runCore: async ({ stateDir }) => {
    fs.appendFileSync(path.join(stateDir, "receipts.jsonl"), '{"status":"delivery_uncertain"}\n');
    throw new Error("simulated process loss after send began");
  } }), /simulated/);
  let preserved = false;
  const result = await runInvestmentCloudShadow({ ...input, runCore: async ({ stateDir }) => {
    preserved = fs.readFileSync(path.join(stateDir, "receipts.jsonl"), "utf8").includes("delivery_uncertain");
    return { status: "allocated", mode: "shadow", deployment: "cloud", effect: "none", telegram_message_id: "43" };
  } });
  assert.equal(preserved, true);
  assert.equal(result.telegram_message_id, "43");
});

test("durable five-minute job makes restart replay produce zero extra shadow wakes", async () => {
  const owner = { uid: "tenant-1", deployment: "cloud", mode: "shadow", paused: false, killed: false };
  let enqueued;
  let claimed = false;
  let executions = 0;
  const completed = [];
  const wake = makeInvestmentCloudShadowWake({
    stateStore: { listRunnable: async () => [owner] },
    runtimeStore: { read: async () => seededBundle(), upsert: async () => {} },
    jobs: {
      enqueueJob: async (job) => (enqueued = job, { created: !claimed, job }),
      claimJobs: async () => claimed ? [] : (claimed = true, [{ job_id: enqueued.jobId,
        tenant_id: enqueued.tenantId, loop_id: enqueued.loopId, capability: enqueued.capability,
        effect_class: enqueued.effectClass, effect_key: null, attempt: 1, input_refs: enqueued.inputRefs }]),
      completeJob: async (row) => completed.push(row),
    },
    secretProvider: { assertTenant: () => true }, readChatId: async () => "chat-1",
    stateRoot: "/durable/investment",
    executeShadow: async () => (executions += 1, { status: "allocated", mode: "shadow",
      deployment: "cloud", effect: "none", telegram_message_id: "42", decision: "NO_TRADE" }),
  });
  const now = new Date("2026-09-10T12:07:00Z");
  assert.equal((await wake(now)).status, "completed");
  assert.deepEqual(await wake(now), { status: "already_processed", effect_permission: "none" });
  assert.equal(executions, 1);
  assert.equal(completed.length, 1);
  assert.equal(completed[0].receipt.order_calls, 0);
  assert.equal(completed[0].receipt.message_calls, 1);
});

test("an older claimed shadow slot completes with its own immutable lineage", async () => {
  const owner = { uid: "tenant-1", deployment: "cloud", mode: "shadow" };
  const artifact = require("./investment-core-artifact.js").readInvestmentCoreArtifact();
  const oldSlot = "2026-09-10T12:00:00.000Z";
  const oldId = require("node:crypto").createHash("sha256").update(`tenant-1\n${oldSlot}\n${artifact.digest}`).digest("hex");
  let completion;
  const wake = makeInvestmentCloudShadowWake({ stateStore: { listRunnable: async () => [owner] },
    runtimeStore: { read: async () => seededBundle(), upsert: async () => {} },
    jobs: { enqueueJob: async () => {}, claimJobs: async () => [{ job_id: oldId,
      tenant_id: "tenant-1", loop_id: "investment.cloud", capability: "investment.shadow",
      effect_class: "none", effect_key: null, attempt: 1,
      input_refs: { investment_state_ref: "investment-state://tenant-1",
        runtime_state_ref: "investment-runtime-state://tenant-1", core_artifact_ref: artifact.ref,
        schedule_slot_ref: `schedule-slot://${oldSlot}` } }], completeJob: async (value) => { completion = value; } },
    secretProvider: { assertTenant: () => true }, readChatId: async () => "chat",
    stateRoot: "/durable/investment",
    executeShadow: async () => ({ mode: "shadow", deployment: "cloud", effect: "none", telegram_message_id: "9" }),
  });
  assert.equal((await wake(new Date("2026-09-10T12:05:00Z"))).receipt.observed_at, oldSlot);
  assert.equal(completion.jobId, oldId);
});

test("one Cloud live wake owns a money-class job and persists the shared core result", async () => {
  const owner = { uid: "tenant-1", deployment: "cloud", mode: "live" };
  let enqueued;
  let completion;
  const wake = makeInvestmentCloudWake({ stateStore: { listRunnable: async () => [owner] },
    runtimeStore: { read: async () => seededBundle(), upsert: async () => {} },
    jobs: { enqueueJob: async (job) => { enqueued = job; }, claimJobs: async () => [{
      job_id: enqueued.jobId, tenant_id: "tenant-1", loop_id: "investment.cloud",
      capability: "investment.live", effect_class: "money", effect_key: enqueued.jobId,
      attempt: 1, input_refs: enqueued.inputRefs }],
      completeJob: async (value) => { completion = value; } },
    secretProvider: { assertTenant: () => true }, readChatId: async () => "chat",
    stateRoot: "/durable/investment", executeInvestment: async ({ mode, wakeId }) => ({
      mode, deployment: "cloud", effect: "e".repeat(64), observed_wake_id: wakeId,
      telegram_message_id: "live-message", decision: "position://BTCUSD" }) });
  const result = await wake(new Date("2026-09-10T12:07:00Z"));
  assert.equal(enqueued.capability, "investment.live");
  assert.equal(enqueued.effectClass, "money");
  assert.equal(enqueued.effectKey, enqueued.jobId);
  assert.equal(result.receipt.effect_permission, "money");
  assert.equal(result.receipt.order_calls, 1);
  assert.equal(result.receipt.observed_at, "2026-09-10T12:05:00.000Z");
  assert.equal(completion.receipt.telegram_message_id, "live-message");
});

test("Cloud live executor passes only live state variables to the shared Python core", async () => {
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), "investment-live-volume-"));
  const saved = [];
  const result = await runInvestmentCloud({ tenantId: "tenant-1", mode: "live",
    wakeId: "2026-09-10T12:05:00.000Z",
    sealed: seededBundle(), secretProvider: { get: async (_tenant, ref) => ({
      "secret://alpaca/api-key": "key", "secret://alpaca/api-secret": "secret",
      "secret://telegram/bot-token": "telegram" })[ref] }, telegramChatId: "chat",
    alpacaCli: "/app/.bin/alpaca", readAccountId: async () => "account-1", stateRoot,
    runCore: async ({ stateDir, env }) => {
      assert.equal(env.LIFE_MANAGER_INVESTMENT_MODE, "live");
      assert.equal(env.LIFE_MANAGER_INVESTMENT_DEPLOYMENT, "cloud");
      assert.equal(env.LIFE_MANAGER_INVESTMENT_WAKE_ID, "2026-09-10T12:05:00.000Z");
      assert.equal(env.ALPACA_INVESTMENT_LIVE_STATE_DIR, stateDir);
      assert.equal(env.ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE.endsWith("credentials.json"), true);
      assert.equal(env.ALPACA_INVESTMENT_SHADOW_STATE_DIR, undefined);
      return { status: "allocated", mode: "live", deployment: "cloud", effect: "none",
        telegram_message_id: "live-message" };
    }, persist: async (tenantId, bundle) => saved.push({ tenantId, bundle }) });
  assert.equal(result.mode, "live");
  assert.equal(saved.length, 1);
});
