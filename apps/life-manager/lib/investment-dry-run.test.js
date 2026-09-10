"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { makeInvestmentDryRun, fiveMinuteSlot, runInvestmentDryRun,
  startInvestmentDryRunLoop, createCloudInvestmentSecretProvider,
  createSupabaseInvestmentChatReader, readInvestmentCloudWiring } = require("./investment-dry-run.js");
const { readInvestmentCoreArtifact, runInvestmentParityCore } = require("./investment-core-artifact.js");

const state = { uid: "owner-1", lifecycle: "in_review", deployment: "cloud", mode: "paper",
  paused: false, killed: false, core_digest: null, receipt_refs: [],
  alpaca_api_key_ref: null, alpaca_api_secret_ref: null };
const cloudSecrets = () => createCloudInvestmentSecretProvider({
  LM_RUNTIME_TENANT_ID: "owner-1", LM_ALPACA_API_KEY: "key",
  LM_ALPACA_API_SECRET: "secret", LM_TELEGRAM_BOT_TOKEN: "telegram",
});

test("production fixture is packaged inside the Railway app root with the sealed digest", () => {
  const bytes = fs.readFileSync(path.join(__dirname, "fixtures/investment-preapproval-replay.json"));
  assert.equal(crypto.createHash("sha256").update(bytes).digest("hex"),
    "a123789d7306f1551dc8e8637fc15e2af732f756b57170fe2a1928aeb2375592");
});

test("cloud invokes the committed Python core and reports its content-addressed artifact", async () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/investment-preapproval-replay.json")));
  const artifact = readInvestmentCoreArtifact();
  const result = await runInvestmentParityCore(fixture);
  assert.match(artifact.digest, /^[a-f0-9]{64}$/);
  assert.equal(artifact.ref, `investment-core://sha256/${artifact.digest}`);
  assert.equal(result.core_digest, "85ac90e0027acd0c57ac0a81ef01db1a02c8a2b5b59ae50774fe2a40ec89f259");
});

test("Railway app-root artifact is byte-identical to every committed core source", () => {
  const sourceRoot = path.resolve(__dirname, "../../../skills/alpaca-investment");
  const appRoot = path.resolve(__dirname, "../investment-core");
  for (const file of readInvestmentCoreArtifact().files) {
    const source = file.name === "telegram.py"
      ? path.resolve(__dirname, "../../../skills/_shared/telegram.py")
      : file.name === "telegram_outbox.py"
        ? path.resolve(__dirname, "../../../skills/_shared/marketplace-core/scripts/telegram_outbox.py")
        : path.join(sourceRoot, file.name);
    assert.deepEqual(fs.readFileSync(path.join(appRoot, file.name)),
      fs.readFileSync(source), file.name);
  }
});

test("cloud secret adapter is tenant-scoped and returns no value in health readback", async () => {
  const provider = createCloudInvestmentSecretProvider({
    LM_RUNTIME_TENANT_ID: "owner-1", LM_ALPACA_API_KEY: "secret-value",
    LM_ALPACA_API_SECRET: "api-secret", LM_TELEGRAM_BOT_TOKEN: "telegram",
  });
  assert.deepEqual(await provider.health(), { ok: true, mode: "cloud", provider: "vault" });
  assert.equal(await provider.get("owner-1", "secret://alpaca/api-key"), "secret-value");
  await assert.rejects(provider.get("owner-2", "secret://alpaca/api-key"), /tenant scope/);
  assert.equal(JSON.stringify(await provider.health()).includes("secret-value"), false);
});

test("cloud reads the exact tenant Telegram target from the Supabase user directory", async () => {
  let request;
  const readChatId = createSupabaseInvestmentChatReader({
    env: { SUPABASE_URL: "https://directory.example/", SUPABASE_SERVICE_ROLE_KEY: "role-key" },
    fetchImpl: async (url, options) => {
      request = { url, options };
      return { ok: true, json: async () => [{ uid: "owner-1", telegram_chat_id: "42" }] };
    },
  });
  assert.equal(await readChatId("owner-1"), "42");
  assert.match(request.url, /lm_users\?uid=eq\.owner-1&select=uid,telegram_chat_id&limit=2$/);
  assert.equal(request.options.headers.apikey, "role-key");
  assert.equal(request.options.headers.Authorization, "Bearer role-key");
});

test("cloud fails closed on an ambiguous or foreign Telegram directory row", async () => {
  const make = (rows) => createSupabaseInvestmentChatReader({
    env: { SUPABASE_URL: "https://directory.example", SUPABASE_SERVICE_ROLE_KEY: "role-key" },
    fetchImpl: async () => ({ ok: true, json: async () => rows }),
  });
  await assert.rejects(make([])("owner-1"), /target unavailable/);
  await assert.rejects(make([{ uid: "owner-2", telegram_chat_id: "42" }])("owner-1"), /target unavailable/);
  await assert.rejects(make([{ uid: "owner-1", telegram_chat_id: "42" },
    { uid: "owner-1", telegram_chat_id: "43" }])("owner-1"), /target unavailable/);
});

test("disabled cloud readback proves host wiring without broker or Telegram effects", async () => {
  const result = await readInvestmentCloudWiring({ env: { LM_RUNTIME_TENANT_ID: "owner-1" } });
  assert.equal(result.status, "wired_disabled");
  assert.equal(result.schedule_enabled, false);
  assert.equal(result.broker_mutation_enabled, false);
  assert.equal(result.telegram_transport_enabled, false);
  assert.equal(result.telegram_transport_wired, true);
  assert.equal(result.durable_receipts, true);
  assert.equal(result.core_digest, "85ac90e0027acd0c57ac0a81ef01db1a02c8a2b5b59ae50774fe2a40ec89f259");
  assert.match(result.core_artifact_digest, /^[a-f0-9]{64}$/);
  assert.equal(result.secret_provider_ok, false);
});

test("cloud readback never calls an enabled schedule disabled", async () => {
  const result = await readInvestmentCloudWiring({
    env: { LM_RUNTIME_TENANT_ID: "owner-1", LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED: "true" },
  });
  assert.equal(result.status, "invalid_schedule_enabled");
  assert.equal(result.schedule_enabled, true);
});

test("cloud shadow readback reports real observation/reporting but no broker mutation", async () => {
  const result = await readInvestmentCloudWiring({
    env: { LM_RUNTIME_TENANT_ID: "owner-1", LM_INVESTMENT_CLOUD_SHADOW_ENABLED: "true",
      LM_INVESTMENT_CLOUD_STATE_ROOT: "/data/investment" },
    secretProvider: cloudSecrets(),
  });
  assert.equal(result.status, "shadow_enabled");
  assert.equal(result.schedule_enabled, true);
  assert.equal(result.broker_mutation_enabled, false);
  assert.equal(result.telegram_transport_enabled, true);
  assert.equal(result.durable_state_root_bound, true);
  assert.equal(result.secret_provider_ok, true);
});

test("cloud shadow flag fails readback without a durable volume path", async () => {
  const result = await readInvestmentCloudWiring({
    env: { LM_RUNTIME_TENANT_ID: "owner-1", LM_INVESTMENT_CLOUD_SHADOW_ENABLED: "true" },
    secretProvider: cloudSecrets(),
  });
  assert.equal(result.status, "invalid_shadow_config");
  assert.equal(result.durable_state_root_bound, false);
});

test("enabled worker rejects a foreign tenant before queue enqueue", async () => {
  let enqueued = false;
  const run = makeInvestmentDryRun({ listRunnable: async () => [{ ...state, uid: "owner-2" }] }, {
    enqueueJob: async () => { enqueued = true; },
  }, {
    getEnv: () => ({ LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED: "true" }),
    secretProvider: cloudSecrets(),
  });
  await assert.rejects(run(new Date("2026-09-06T12:00:00Z")), /tenant scope/);
  assert.equal(enqueued, false);
});

test("a Local/Cloud parity mismatch fails closed before the durable receipt", async () => {
  let completed = false;
  let enqueued;
  const jobs = { enqueueJob: async (job) => (enqueued = job, { created: true, job }),
    claimJobs: async () => [{ job_id: enqueued.jobId, tenant_id: "owner-1",
      loop_id: enqueued.loopId, capability: enqueued.capability, effect_class: "none",
      effect_key: null, attempt: 1, input_refs: enqueued.inputRefs }],
    completeJob: async () => { completed = true; } };
  const fixture = { no_trade: { approved: false, candidate_ref: "NO_TRADE",
    gate: "model_no_trade", reason: "No edge", observed_at: "2026-09-06T00:00:00Z" },
    observation: { account: { cash: "100000.00", equity: "100000.00" } } };
  const run = makeInvestmentDryRun({ listRunnable: async () => [state] }, jobs, {
    getEnv: () => ({ LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED: "true" }), fixture,
    fixtureDigest: "d".repeat(64), expectedParity: {}, workerId: "worker-1",
    secretProvider: cloudSecrets() });
  await assert.rejects(() => run(new Date("2026-09-06T12:00:00Z")), /parity mismatch/);
  assert.equal(completed, false);
});

test("in-process owner starts immediately and repeats every five minutes", async () => {
  let runs = 0;
  let timer;
  const handle = startInvestmentDryRunLoop({ runOnce: async () => { runs += 1; },
    setTimer: (fn, ms) => (timer = { fn, ms }) });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(runs, 1);
  assert.equal(timer.ms, 300000);
  await timer.fn();
  assert.equal(runs, 2);
  assert.equal(handle, timer);
});

test("investment dry-run is disabled by default", async () => {
  let touched = false;
  const run = makeInvestmentDryRun({ listRunnable: async () => { touched = true; } }, {}, { getEnv: () => ({}) });
  assert.deepEqual(await run(new Date()), { status: "disabled", effect_permission: "none" });
  assert.equal(touched, false);
});

test("production entrypoint stays disabled without constructing database dependencies", async () => {
  const previous = process.env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED;
  delete process.env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED;
  try {
    assert.deepEqual(await runInvestmentDryRun(new Date()), { status: "disabled", effect_permission: "none" });
  } finally {
    if (previous === undefined) delete process.env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED;
    else process.env.LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED = previous;
  }
});

test("enabled dry-run writes one effect-none receipt and replay creates no duplicate effect", async () => {
  const completed = [];
  let claimed = false;
  let enqueued;
  const jobs = {
    enqueueJob: async (job) => (enqueued = job, { created: !claimed, job }),
    claimJobs: async () => claimed ? [] : (claimed = true, [{ job_id: enqueued.jobId,
      tenant_id: "owner-1", loop_id: enqueued.loopId, capability: enqueued.capability,
      effect_class: enqueued.effectClass, effect_key: enqueued.effectKey,
      attempt: 1, input_refs: enqueued.inputRefs }]),
    completeJob: async (value) => completed.push(value),
  };
  const run = makeInvestmentDryRun({ listRunnable: async () => [state] }, jobs, {
    getEnv: () => ({ LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED: "true" }),
    fixture: { no_trade: { approved: false, candidate_ref: "NO_TRADE", gate: "model_no_trade", reason: "No edge", observed_at: "2026-09-06T00:00:00Z" },
      observation: { account: { cash: "100000.00", equity: "100000.00" } } },
    fixtureDigest: "a".repeat(64), workerId: "worker-1",
    secretProvider: cloudSecrets(),
    telegramTransport: async () => { throw new Error("disabled transport must not send"); },
  });
  const now = new Date("2026-09-06T12:07:59Z");
  const first = await run(now);
  const replay = await run(now);
  assert.equal(first.receipt.effect_permission, "none");
  assert.equal(first.receipt.broker_calls, 0);
  assert.equal(first.receipt.message_calls, 0);
  assert.equal(first.receipt.decision, "NO_TRADE");
  assert.match(first.receipt.core_digest, /^[a-f0-9]{64}$/);
  assert.match(first.receipt.core_digest, /^[a-f0-9]{64}$/);
  assert.equal(first.receipt.core_artifact_ref,
    `investment-core://sha256/${first.receipt.core_artifact_digest}`);
  assert.equal(first.receipt.secret_provider, "vault");
  assert.equal(first.receipt.secret_provider_ok, true);
  assert.equal(first.receipt.secret_refs_bound, false);
  assert.equal(first.receipt.telegram_transport, "life-manager-telegram");
  assert.equal(first.receipt.telegram_transport_wired, true);
  assert.equal(first.receipt.telegram_transport_enabled, false);
  assert.equal(completed.length, 1);
  assert.deepEqual(replay, { status: "already_processed", effect_permission: "none" });
});

test("five-minute slot is stable", () => {
  assert.equal(fiveMinuteSlot(new Date("2026-09-06T12:09:59Z")), "2026-09-06T12:05:00.000Z");
});

test("an older claimed slot completes with its own immutable lineage", async () => {
  const digest = "b".repeat(64);
  const artifact = readInvestmentCoreArtifact();
  const oldSlot = "2026-09-06T12:00:00.000Z";
  const oldId = crypto.createHash("sha256").update(`owner-1\n${oldSlot}\n${digest}\n${artifact.digest}`).digest("hex");
  let completion;
  const jobs = {
    enqueueJob: async (job) => ({ created: true, job }),
    claimJobs: async () => [{ job_id: oldId, tenant_id: "owner-1", loop_id: "investment.cloud",
      capability: "investment.dry-run", effect_class: "none", effect_key: null, attempt: 1,
      input_refs: { investment_state_ref: "investment-state://owner-1",
        fixture_ref: `fixture://alpaca/preapproval-replay/${digest}`,
        core_artifact_ref: artifact.ref,
        schedule_slot_ref: `schedule-slot://${oldSlot}` } }],
    completeJob: async (value) => { completion = value; },
  };
  const run = makeInvestmentDryRun({ listRunnable: async () => [state] }, jobs, {
    getEnv: () => ({ LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED: "true" }), fixtureDigest: digest,
    fixture: { no_trade: { approved: false, candidate_ref: "NO_TRADE", gate: "model_no_trade", reason: "No edge", observed_at: "2026-09-06T00:00:00Z" },
      observation: { account: { cash: "1.00", equity: "1.00" } } }, workerId: "worker-1",
    secretProvider: cloudSecrets(),
  });
  const result = await run(new Date("2026-09-06T12:05:00Z"));
  assert.equal(result.receipt.observed_at, oldSlot);
  assert.equal(completion.jobId, oldId);
});

test("claimed job with foreign state lineage fails closed before completion", async () => {
  let completed = false;
  const digest = "c".repeat(64);
  const slot = "2026-09-06T12:00:00.000Z";
  const id = crypto.createHash("sha256").update(`owner-1\n${slot}\n${digest}`).digest("hex");
  const jobs = {
    enqueueJob: async (job) => ({ created: true, job }),
    claimJobs: async () => [{ job_id: id, tenant_id: "owner-1", loop_id: "investment.cloud",
      capability: "investment.dry-run", effect_class: "none", effect_key: null, attempt: 1,
      input_refs: { investment_state_ref: "investment-state://foreign",
        fixture_ref: `fixture://alpaca/preapproval-replay/${digest}`,
        schedule_slot_ref: `schedule-slot://${slot}` } }],
    completeJob: async () => { completed = true; },
  };
  const run = makeInvestmentDryRun({ listRunnable: async () => [state] }, jobs, {
    getEnv: () => ({ LM_INVESTMENT_CLOUD_DRY_RUN_ENABLED: "true" }), fixtureDigest: digest,
    fixture: { no_trade: { approved: false, candidate_ref: "NO_TRADE", gate: "model_no_trade", reason: "No edge", observed_at: "2026-09-06T00:00:00Z" },
      observation: { account: { cash: "1.00", equity: "1.00" } } }, workerId: "worker-1",
    secretProvider: cloudSecrets(),
  });
  await assert.rejects(() => run(new Date("2026-09-06T12:05:00Z")), /claimed job invalid/);
  assert.equal(completed, false);
});
