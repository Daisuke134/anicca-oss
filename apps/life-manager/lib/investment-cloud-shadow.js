"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const { exportState, importState } = require("../scripts/investment-cutover-state.js");
const { readInvestmentCoreArtifact } = require("./investment-core-artifact.js");

const execute = promisify(execFile);
const CORE = path.resolve(__dirname, "../investment-core/run.py");
const AGENT = path.resolve(__dirname, "../scripts/cloud-investment-agent-runner.js");

function accountHash(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

function fiveMinuteSlot(value = new Date()) {
  const milliseconds = value instanceof Date ? value.getTime() : Date.parse(value);
  if (!Number.isFinite(milliseconds)) throw new Error("investment shadow time invalid");
  return new Date(Math.floor(milliseconds / 300000) * 300000).toISOString();
}

function makeInvestmentCloudWake(deps) {
  const workerId = deps.workerId || `investment-shadow-${process.pid}`;
  return async (now = new Date()) => {
    const owners = await deps.stateStore.listRunnable(1);
    if (!owners.length) return { status: "no_tenant", effect_permission: "none" };
    const owner = owners[0];
    if (owner.deployment !== "cloud" || !["shadow", "live"].includes(owner.mode)) {
      throw new Error("investment cloud owner invalid");
    }
    deps.secretProvider.assertTenant(owner.uid);
    const sealed = await deps.runtimeStore.read(owner.uid);
    if (!sealed) throw new Error("investment cloud runtime state missing");
    const slot = fiveMinuteSlot(now);
    const artifact = readInvestmentCoreArtifact();
    const capability = `investment.${owner.mode}`;
    const effectClass = owner.mode === "live" ? "money" : "none";
    const lineage = owner.mode === "shadow" ? `${owner.uid}\n${slot}\n${artifact.digest}`
      : `${owner.uid}\nlive\n${slot}\n${artifact.digest}`;
    const jobId = crypto.createHash("sha256").update(lineage).digest("hex");
    await deps.jobs.enqueueJob({ jobId, tenantId: owner.uid, loopId: "investment.cloud",
      capability, effectClass, effectKey: owner.mode === "live" ? jobId : null, maxAttempts: 3,
      inputRefs: { investment_state_ref: `investment-state://${owner.uid}`,
        runtime_state_ref: `investment-runtime-state://${owner.uid}`,
        core_artifact_ref: artifact.ref, schedule_slot_ref: `schedule-slot://${slot}` } });
    const claimed = await deps.jobs.claimJobs({ workerId, capabilities: [capability],
      tenantId: owner.uid, limit: 1, leaseSeconds: 300 });
    if (!claimed.length) return { status: "already_processed", effect_permission: "none" };
    const job = claimed[0];
    const refs = job.input_refs || {};
    const claimedSlot = String(refs.schedule_slot_ref || "").replace(/^schedule-slot:\/\//, "");
    const claimedLineage = owner.mode === "shadow" ? `${owner.uid}\n${claimedSlot}\n${artifact.digest}`
      : `${owner.uid}\nlive\n${claimedSlot}\n${artifact.digest}`;
    const claimedJobId = crypto.createHash("sha256").update(claimedLineage).digest("hex");
    if (job.job_id !== claimedJobId || fiveMinuteSlot(claimedSlot) !== claimedSlot
      || job.tenant_id !== owner.uid || job.loop_id !== "investment.cloud"
      || job.capability !== capability || job.effect_class !== effectClass
      || job.effect_key !== (owner.mode === "live" ? job.job_id : null)
      || refs.investment_state_ref !== `investment-state://${owner.uid}`
      || refs.runtime_state_ref !== `investment-runtime-state://${owner.uid}`
      || refs.core_artifact_ref !== artifact.ref) {
      throw new Error("investment cloud shadow claimed job invalid");
    }
    const telegramChatId = await deps.readChatId(owner.uid);
    const result = await deps.executeInvestment({ tenantId: owner.uid, mode: owner.mode, sealed,
      secretProvider: deps.secretProvider, telegramChatId,
      stateRoot: deps.stateRoot,
      persist: (uid, next) => deps.runtimeStore.upsert(uid, next.bundle) });
    const receipt = { deployment: "cloud", mode: owner.mode,
      effect_permission: owner.mode === "live" ? "money" : "none",
      broker_effect: result.effect, order_calls: result.effect === "none" ? 0 : 1,
      message_calls: 1, decision: result.decision || null,
      telegram_message_id: String(result.telegram_message_id), observed_at: claimedSlot,
      core_artifact_ref: artifact.ref, runtime_state_digest: sealed.digest };
    await deps.jobs.completeJob({ tenantId: owner.uid, jobId: job.job_id, attempt: job.attempt, workerId, receipt });
    return { status: "completed", receipt };
  };
}

function makeInvestmentCloudShadowWake(deps) {
  return makeInvestmentCloudWake({ ...deps,
    executeInvestment: deps.executeInvestment || deps.executeShadow });
}

async function defaultReadAccountId({ alpacaCli, apiKey, apiSecret }) {
  const result = await execute(alpacaCli, ["account", "get", "--quiet", "--jq", ".id"], {
    env: { ...process.env, ALPACA_API_KEY: apiKey, ALPACA_SECRET_KEY: apiSecret, ALPACA_LIVE_TRADE: "true" },
    timeout: 30_000, maxBuffer: 64 * 1024,
  });
  const value = String(result.stdout || "").trim().replace(/^"|"$/g, "");
  if (!value || value.length > 500) throw new Error("investment cloud account id invalid");
  return value;
}

async function defaultRunCore({ stateDir, credentialsFile, env }) {
  const result = await execute(process.env.LM_INVESTMENT_PYTHON || "python3", [CORE], {
    env, cwd: path.dirname(CORE), timeout: 240_000, maxBuffer: 1024 * 1024,
  });
  const lines = String(result.stdout || "").trim().split("\n").filter(Boolean);
  try { return JSON.parse(lines.at(-1)); } catch { throw new Error("investment cloud core result invalid"); }
}

async function runInvestmentCloud(input) {
  const tenantId = String(input && input.tenantId || "").trim();
  const mode = String(input && input.mode || "shadow");
  if (!tenantId || !input.sealed || !input.secretProvider || typeof input.secretProvider.get !== "function"
    || typeof input.persist !== "function" || !["shadow", "live"].includes(mode)) {
    throw new Error("investment cloud input invalid");
  }
  const [apiKey, apiSecret, telegramToken] = await Promise.all([
    input.secretProvider.get(tenantId, "secret://alpaca/api-key"),
    input.secretProvider.get(tenantId, "secret://alpaca/api-secret"),
    input.secretProvider.get(tenantId, "secret://telegram/bot-token"),
  ]);
  const telegramChatId = String(input.telegramChatId || "").trim();
  if (![apiKey, apiSecret, telegramToken, telegramChatId].every((value) => String(value || "").trim())) {
    throw new Error("investment cloud shadow secret unavailable");
  }
  const stateRoot = path.resolve(String(input.stateRoot || ""));
  if (!input.stateRoot || stateRoot === path.parse(stateRoot).root) throw new Error("investment cloud durable state root invalid");
  fs.mkdirSync(stateRoot, { recursive: true, mode: 0o700 });
  fs.chmodSync(stateRoot, 0o700);
  const stateDir = path.join(stateRoot, crypto.createHash("sha256").update(tenantId).digest("hex").slice(0, 32));
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  fs.chmodSync(stateDir, 0o700);
  const privateDir = fs.mkdtempSync(path.join(os.tmpdir(), "investment-cloud-credential-"));
  fs.chmodSync(privateDir, 0o700);
  const credentialsFile = path.join(privateDir, "credentials.json");
  const markerPath = path.join(stateDir, ".cutover.json");
  let accountId;
  try {
    const alpacaCli = input.alpacaCli || process.env.ALPACA_CLI || "/app/.bin/alpaca";
    accountId = await (input.readAccountId || defaultReadAccountId)({ alpacaCli, apiKey, apiSecret });
    if (accountHash(accountId) !== input.sealed.bundle.account_binding.account_id_hash) {
      throw new Error("investment cloud account binding mismatch");
    }
    if (fs.existsSync(markerPath)) {
      const marker = JSON.parse(fs.readFileSync(markerPath, "utf8"));
      if (marker.account_id_hash !== input.sealed.bundle.account_binding.account_id_hash
        || marker.source_release_sha !== input.sealed.bundle.cutover.source_release_sha) {
        throw new Error("investment cloud durable state binding mismatch");
      }
    } else {
      importState({ stateDir, sealed: input.sealed });
      fs.writeFileSync(markerPath, `${JSON.stringify({
        account_id_hash: input.sealed.bundle.account_binding.account_id_hash,
        source_release_sha: input.sealed.bundle.cutover.source_release_sha,
      })}\n`, { mode: 0o600, flag: "wx" });
    }
    const credentials = { credentials: [{ service: "app.alpaca.markets",
      live_endpoint: "https://api.alpaca.markets/v2", live_api_key: apiKey, live_api_secret: apiSecret }] };
    fs.writeFileSync(credentialsFile, `${JSON.stringify(credentials)}\n`, { mode: 0o600 });
    fs.chmodSync(credentialsFile, 0o600);
    const env = { ...process.env,
      LIFE_MANAGER_INVESTMENT_MODE: mode, LIFE_MANAGER_INVESTMENT_DEPLOYMENT: "cloud",
      LIFE_MANAGER_INVESTMENT_AGENT_RUNNER: AGENT,
      ALPACA_CLI: alpacaCli,
      LM_TELEGRAM_BOT_TOKEN: telegramToken, TELEGRAM_CHAT_ID: telegramChatId,
    };
    const prefix = mode === "live" ? "ALPACA_INVESTMENT_LIVE" : "ALPACA_INVESTMENT_SHADOW";
    env[`${prefix}_STATE_DIR`] = stateDir;
    env[`${prefix}_CREDENTIALS_FILE`] = credentialsFile;
    const result = await (input.runCore || defaultRunCore)({ stateDir, credentialsFile, env });
    if (!result || result.mode !== mode || result.deployment !== "cloud"
      || (mode === "shadow" && result.effect !== "none") || !result.telegram_message_id) {
      throw new Error("investment cloud result invalid");
    }
    return result;
  } finally {
    if (accountId && accountHash(accountId) === input.sealed.bundle.account_binding.account_id_hash) {
      const next = exportState({ stateDir, accountId, cutover: input.sealed.bundle.cutover });
      await input.persist(tenantId, next);
    }
    fs.rmSync(privateDir, { recursive: true, force: true });
  }
}

async function runInvestmentCloudShadow(input) {
  return runInvestmentCloud({ ...input, mode: "shadow" });
}

module.exports = { accountHash, fiveMinuteSlot, makeInvestmentCloudWake,
  makeInvestmentCloudShadowWake, runInvestmentCloud, runInvestmentCloudShadow };
