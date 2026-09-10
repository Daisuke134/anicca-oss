"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { exportState } = require("../scripts/investment-cutover-state.js");
const { makeInvestmentCloudShadowWake, runInvestmentCloudShadow } = require("./investment-cloud-shadow.js");

function seededBundle() {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), "investment-shadow-source-"));
  fs.writeFileSync(path.join(state, "risk-day.json"), '{"ny_day":"2026-09-10"}\n');
  fs.writeFileSync(path.join(state, "receipts.jsonl"), "");
  return exportState({ stateDir: state, accountId: "account-1", now: "2026-09-10T12:00:00.000Z" });
}

test("one cloud shadow wake verifies account binding, invokes the same core, reports, and persists state", async () => {
  const saved = [];
  const result = await runInvestmentCloudShadow({
    tenantId: "tenant-1", sealed: seededBundle(), secretProvider: { get: async (_tenant, ref) => ({
      "secret://alpaca/api-key": "key", "secret://alpaca/api-secret": "secret",
      "secret://telegram/bot-token": "telegram",
    })[ref] }, telegramChatId: "chat-1", alpacaCli: "/app/.bin/alpaca",
    readAccountId: async () => "account-1",
    runCore: async ({ stateDir, credentialsFile, env }) => {
      assert.equal(fs.statSync(credentialsFile).mode & 0o777, 0o600);
      assert.equal(env.LIFE_MANAGER_INVESTMENT_MODE, "shadow");
      assert.equal(env.LIFE_MANAGER_INVESTMENT_DEPLOYMENT, "cloud");
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
    readAccountId: async () => "other-account", runCore: async () => { ran = true; },
    persist: async () => {} }), /binding/);
  assert.equal(ran, false);
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
