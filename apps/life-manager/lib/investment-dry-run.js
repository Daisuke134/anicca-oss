"use strict";

const crypto = require("node:crypto");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const runtimeJobs = require("./runtime-job-store.js");
const { createInvestmentStateStore } = require("./investment-state-store.js");
const { createInvestmentRuntimeStateStore } = require("./investment-runtime-state-store.js");
const { makeInvestmentCloudWake, runInvestmentCloud } = require("./investment-cloud-shadow.js");
const { createSecretProvider } = require("./secret-provider.js");
const { sendMessage } = require("./telegram.js");
const { readInvestmentCoreArtifact, runInvestmentParityCore } = require("./investment-core-artifact.js");

const FIXTURE_PATH = path.resolve(__dirname, "fixtures/investment-preapproval-replay.json");
const PARITY_PATH = path.resolve(__dirname, "fixtures/investment-parity-expected.json");
const CAPABILITY = "investment.dry-run";
let pool;

function createCloudInvestmentSecretProvider(env = process.env) {
  const tenantId = String(env.LM_RUNTIME_TENANT_ID || "").trim();
  const bindings = new Map([
    ["secret://alpaca/api-key", "LM_ALPACA_API_KEY"],
    ["secret://alpaca/api-secret", "LM_ALPACA_API_SECRET"],
    ["secret://telegram/bot-token", "LM_TELEGRAM_BOT_TOKEN"],
  ]);
  const provider = createSecretProvider({ mode: "cloud", vault: {
    async get(requestTenantId, ref) {
      if (!tenantId || requestTenantId !== tenantId) throw new Error("investment cloud secret tenant scope mismatch");
      const name = bindings.get(ref);
      const value = name && String(env[name] || "").trim();
      if (!value) throw new Error("investment cloud secret unavailable");
      return value;
    },
    async health() {
      return { ok: Boolean(tenantId && [...bindings.values()].every((name) => String(env[name] || "").trim())) };
    },
  } });
  return Object.freeze({
    get: provider.get,
    health: provider.health,
    assertTenant(requestTenantId) {
      if (!tenantId || requestTenantId !== tenantId) throw new Error("investment cloud secret tenant scope mismatch");
      return true;
    },
  });
}

function createSupabaseInvestmentChatReader({ env = process.env, fetchImpl = fetch } = {}) {
  const base = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = String(env.SUPABASE_SERVICE_ROLE_KEY || "").trim();
  if (!base || !key) throw new Error("investment cloud Telegram directory unavailable");
  return async (uid) => {
    const tenant = String(uid || "").trim();
    if (!tenant) throw new Error("investment cloud Telegram target unavailable");
    const response = await fetchImpl(`${base}/rest/v1/lm_users?uid=eq.${encodeURIComponent(tenant)}`
      + "&select=uid,telegram_chat_id&limit=2", {
      headers: { apikey: key, Authorization: `Bearer ${key}` },
    });
    if (!response.ok) throw new Error("investment cloud Telegram directory unavailable");
    const rows = await response.json();
    const row = Array.isArray(rows) && rows.length === 1 ? rows[0] : null;
    const value = row && row.uid === tenant && String(row.telegram_chat_id || "").trim();
    if (!value) throw new Error("investment cloud Telegram target unavailable");
    return value;
  };
}

async function readInvestmentCloudWiring(opts = {}) {
  const env = opts.env || process.env;
  const artifact = (opts.readCoreArtifact || readInvestmentCoreArtifact)();
  const secretProvider = opts.secretProvider || createCloudInvestmentSecretProvider(env);
  const health = await secretProvider.health();
  const parity = JSON.parse(fs.readFileSync(PARITY_PATH, "utf8"));
  const shadow = env.LM_INVESTMENT_CLOUD_SHADOW_ENABLED === "true";
  const live = env.LM_INVESTMENT_CLOUD_LIVE_ENABLED === "true";
  const conflict = live && shadow;
  const dryRun = env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED === "true";
  const durableRoot = String(env.LM_INVESTMENT_CLOUD_STATE_ROOT || "").trim();
  const durableStateRootBound = durableRoot.startsWith("/") && durableRoot !== "/";
  return Object.freeze({
    status: conflict ? "invalid_schedule_conflict"
      : live ? (durableStateRootBound ? "live_enabled" : "invalid_live_config")
      : shadow ? (durableStateRootBound ? "shadow_enabled" : "invalid_shadow_config")
      : dryRun ? "invalid_schedule_enabled" : "wired_disabled",
    deployment: "cloud",
    schedule_enabled: live || shadow || dryRun,
    broker_mutation_enabled: live,
    core_digest: parity.core_digest,
    core_artifact_digest: artifact.digest,
    core_artifact_ref: artifact.ref,
    queue: "life-manager-runtime-jobs",
    durable_receipts: true,
    durable_state_root_bound: durableStateRootBound,
    secret_provider: health.provider,
    secret_provider_ok: health.ok,
    telegram_transport: "life-manager-telegram",
    telegram_transport_wired: typeof (opts.telegramTransport || sendMessage) === "function",
    telegram_transport_enabled: live || shadow,
  });
}

function fiveMinuteSlot(value = new Date()) {
  const milliseconds = value instanceof Date ? value.getTime() : Date.parse(value);
  if (!Number.isFinite(milliseconds)) throw new Error("investment dry-run time invalid");
  return new Date(Math.floor(milliseconds / 300000) * 300000).toISOString();
}

function productionDependencies() {
  const connectionString = String(process.env.LM_RUNTIME_DATABASE_URL || process.env.LM_FEEDBACK_DATABASE_URL || "").trim();
  if (!connectionString) throw new Error("investment dry-run database unavailable");
  if (!pool) pool = new (require("pg").Pool)({ connectionString, max: 2 });
  const query = pool.query.bind(pool);
  const readChatId = createSupabaseInvestmentChatReader();
  return {
    stateStore: createInvestmentStateStore({ query }),
    runtimeStore: createInvestmentRuntimeStateStore({ query }),
    stateRoot: String(process.env.LM_INVESTMENT_CLOUD_STATE_ROOT || "").trim(),
    secretProvider: createCloudInvestmentSecretProvider(),
    telegramTransport: sendMessage,
    jobs: {
      enqueueJob: (job) => runtimeJobs.enqueueJob(job, { query }),
      claimJobs: (input) => runtimeJobs.claimJobs(input, { query }),
      completeJob: (input) => runtimeJobs.completeJob(input, { query }),
    },
    readChatId,
  };
}

function makeInvestmentDryRun(stateStore, jobs, opts = {}) {
  const getEnv = opts.getEnv || (() => process.env);
  const workerId = opts.workerId || `investment-cloud-${process.pid}`;
  return async (now = new Date()) => {
    if (getEnv().LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED !== "true") {
      return { status: "disabled", effect_permission: "none" };
    }
    const states = await stateStore.listRunnable(1);
    if (!states.length) return { status: "no_tenant", effect_permission: "none" };
    const owner = states[0];
    const slot = fiveMinuteSlot(now);
    const artifact = (opts.readCoreArtifact || readInvestmentCoreArtifact)();
    if (!opts.secretProvider || typeof opts.secretProvider.health !== "function"
      || typeof opts.secretProvider.assertTenant !== "function") {
      throw new Error("investment cloud secret provider unavailable");
    }
    opts.secretProvider.assertTenant(owner.uid);
    const secretHealth = await opts.secretProvider.health();
    const secretRefsBound = owner.alpaca_api_key_ref === "secret://alpaca/api-key"
      && owner.alpaca_api_secret_ref === "secret://alpaca/api-secret";
    const fixtureText = opts.fixture ? JSON.stringify(opts.fixture) : fs.readFileSync(FIXTURE_PATH, "utf8");
    const fixture = opts.fixture || JSON.parse(fixtureText);
    const fixtureDigest = opts.fixtureDigest || crypto.createHash("sha256").update(fixtureText).digest("hex");
    const jobId = crypto.createHash("sha256").update(`${owner.uid}\n${slot}\n${fixtureDigest}\n${artifact.digest}`).digest("hex");
    await jobs.enqueueJob({ jobId, tenantId: owner.uid, loopId: "investment.cloud",
      capability: CAPABILITY, effectClass: "none", effectKey: null, maxAttempts: 3,
      inputRefs: { investment_state_ref: `investment-state://${owner.uid}`,
        fixture_ref: `fixture://alpaca/preapproval-replay/${fixtureDigest}`,
        core_artifact_ref: artifact.ref,
        schedule_slot_ref: `schedule-slot://${slot}` } });
    const claimed = await jobs.claimJobs({ workerId, capabilities: [CAPABILITY], tenantId: owner.uid,
      limit: 1, leaseSeconds: 180 });
    if (!claimed.length) return { status: "already_processed", effect_permission: "none" };
    const job = claimed[0];
    const refs = job.input_refs;
    const fixtureRef = `fixture://alpaca/preapproval-replay/${fixtureDigest}`;
    const claimedSlot = refs && typeof refs.schedule_slot_ref === "string"
      ? refs.schedule_slot_ref.replace(/^schedule-slot:\/\//, "") : "";
    const expectedJobId = crypto.createHash("sha256")
      .update(`${owner.uid}\n${claimedSlot}\n${fixtureDigest}\n${artifact.digest}`).digest("hex");
    if (!refs || refs.fixture_ref !== fixtureRef
      || refs.investment_state_ref !== `investment-state://${owner.uid}`
      || refs.core_artifact_ref !== artifact.ref
      || fiveMinuteSlot(claimedSlot) !== claimedSlot || job.job_id !== expectedJobId
      || job.tenant_id !== owner.uid || job.loop_id !== "investment.cloud"
      || job.capability !== CAPABILITY || job.effect_class !== "none" || job.effect_key !== null) {
      throw new Error("investment dry-run claimed job invalid");
    }
    const parity = await (opts.runCore || runInvestmentParityCore)(fixture);
    const expectedParity = opts.expectedParity || (opts.fixture ? parity
      : JSON.parse(fs.readFileSync(PARITY_PATH, "utf8")));
    try { assert.deepStrictEqual(parity, expectedParity); }
    catch { throw new Error("investment local/cloud parity mismatch"); }
    if (owner.core_digest !== null && owner.core_digest !== parity.core_digest) {
      throw new Error("investment cloud core digest mismatch");
    }
    const receipt = Object.freeze({ effect_permission: "none", broker_calls: 0, message_calls: 0,
      deployment: "cloud", mode: owner.mode, decision: parity.decision, gate: parity.risk.gate,
      reason: parity.report.reason, cash: parity.report.cash, equity: parity.report.equity,
      core_digest: parity.core_digest, core_artifact_digest: artifact.digest,
      core_artifact_ref: artifact.ref, idempotency_key: parity.idempotency_key,
      secret_provider: secretHealth.provider, secret_provider_ok: secretHealth.ok,
      secret_refs_bound: secretRefsBound, telegram_transport: "life-manager-telegram",
      telegram_transport_wired: typeof opts.telegramTransport === "function",
      telegram_transport_enabled: false, fixture_ref: fixtureRef,
      observed_at: claimedSlot });
    await jobs.completeJob({ tenantId: owner.uid, jobId: job.job_id, attempt: job.attempt,
      workerId, receipt });
    return { status: "completed", receipt };
  };
}

async function runInvestmentDryRun(now) {
  const live = process.env.LM_INVESTMENT_CLOUD_LIVE_ENABLED === "true";
  const shadow = process.env.LM_INVESTMENT_CLOUD_SHADOW_ENABLED === "true";
  if (live || shadow) {
    if (process.env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED === "true" || (live && shadow)) {
      throw new Error("investment cloud schedules conflict");
    }
    const dependencies = productionDependencies();
    return makeInvestmentCloudWake({ ...dependencies,
      executeInvestment: runInvestmentCloud })(now);
  }
  if (process.env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED !== "true") {
    return { status: "disabled", effect_permission: "none" };
  }
  const { stateStore, jobs, secretProvider, telegramTransport } = productionDependencies();
  return makeInvestmentDryRun(stateStore, jobs, { secretProvider, telegramTransport })(now);
}

function startInvestmentDryRunLoop(opts = {}) {
  const runOnce = opts.runOnce || runInvestmentDryRun;
  const setTimer = opts.setTimer || setInterval;
  const onError = opts.onError || ((error) => console.error(`[investment-dry-run] ${error.message}`));
  const run = () => Promise.resolve(runOnce()).catch(onError);
  run();
  return setTimer(run, 300000);
}

module.exports = { CAPABILITY, fiveMinuteSlot, makeInvestmentDryRun,
  createCloudInvestmentSecretProvider, createSupabaseInvestmentChatReader, readInvestmentCloudWiring,
  runInvestmentDryRun, startInvestmentDryRunLoop };
