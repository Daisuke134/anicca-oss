"use strict";

const { createHash, randomBytes } = require("node:crypto");

const MODES = new Set([
  "seed-two-tenant-contexts",
  "verify-two-tenant-contexts",
  "verify-provider-context",
  "verify-expired-handoff",
]);
const TERMINAL_STATUSES = new Set(["completed", "possibly_completed", "handoff_required", "failed"]);
const OUTPUT_KEYS = [
  "mode",
  "tenant_count",
  "origin",
  "context_hashes",
  "job_ids",
  "steel_session_ids",
  "telegram_evidence_ids",
  "released",
];
const REQUIRED_ENV = [
  "BROWSER_AUTH_PRODUCTION_ORIGIN",
  "BROWSER_AUTH_TENANT_A_UID",
  "LM_BROWSER_SESSION_KEY",
  "LM_FEEDBACK_DATABASE_URL",
  "LM_TELEGRAM_BOT_TOKEN",
  "BROWSER_AUTH_TELEGRAM_CHAT_ID",
];

class ConfigurationError extends Error {}

function requiredEnvironment(env, mode) {
  const names = mode === "verify-provider-context"
    ? REQUIRED_ENV
    : [...REQUIRED_ENV, "BROWSER_AUTH_TENANT_B_UID"];
  const missing = names.filter((name) => !String(env && env[name] || "").trim());
  if (missing.length) {
    throw new ConfigurationError(`Missing required environment variables: ${missing.join(", ")}`);
  }
}

function originOf(value) {
  let parsed;
  try { parsed = new URL(String(value || "")); } catch { throw new ConfigurationError("BROWSER_AUTH_PRODUCTION_ORIGIN must be a public HTTPS origin"); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.origin === "null") {
    throw new ConfigurationError("BROWSER_AUTH_PRODUCTION_ORIGIN must be a public HTTPS origin");
  }
  return parsed.origin;
}

function markerHash() {
  // The raw marker is intentionally ephemeral: only this digest crosses a module boundary.
  return createHash("sha256").update(randomBytes(32)).digest("hex");
}

function dependency(deps, group, method) {
  if (!deps || !deps[group] || typeof deps[group][method] !== "function") {
    throw new Error("browser auth production dependency unavailable");
  }
  return deps[group][method].bind(deps[group]);
}

function boundedId(value, kind) {
  const id = String(value == null ? "" : value).trim();
  if (!id || id.length > 200) throw new Error(`browser auth ${kind} unavailable`);
  return id;
}

function isMarkerHash(value) {
  return /^[a-f0-9]{64}$/.test(String(value || ""));
}

function terminalEvidence(row, expected) {
  if (!row || row.id !== expected.jobId || row.uid !== expected.uid || !TERMINAL_STATUSES.has(row.status)) {
    throw new Error("browser auth terminal durable job unavailable");
  }
  const receipt = row.receipt;
  const provider = receipt && receipt.provider_receipt;
  if (!receipt || typeof receipt !== "object" || !provider || typeof provider !== "object") {
    throw new Error("browser auth terminal provider receipt unavailable");
  }
  if (row.auth_marker_hash !== expected.markerHash || receipt.auth_marker_hash !== expected.markerHash) {
    throw new Error("browser auth terminal marker hash is not tenant-bound or isolated");
  }
  if (!isMarkerHash(row.auth_marker_hash) || !isMarkerHash(receipt.auth_marker_hash)) {
    throw new Error("browser auth terminal marker hash unavailable");
  }
  const sessionId = boundedId(receipt.session_id, "Steel session id");
  const evidenceId = boundedId(receipt.evidence_message_id, "Telegram evidence id");

  if (expected.mode === "verify-provider-context" || expected.mode === "verify-two-tenant-contexts") {
    if (receipt.steel_released !== true) throw new Error("browser auth terminal Steel release unavailable");
    if (row.status !== "completed"
      || provider.status !== "authenticated"
      || provider.confirmed !== true
      || provider.handoff_required !== false
      || provider.handoff_reason != null) {
      throw new Error("browser auth authenticated provider readback unavailable");
    }
  }
  if (expected.mode === "verify-expired-handoff") {
    if (row.status !== "handoff_required"
      || provider.handoff_required !== true
      || provider.handoff_reason !== "login") {
      throw new Error("browser auth structured provider login handoff unavailable");
    }
    if (receipt.steel_released !== false) throw new Error("browser auth handoff Steel session was not held");
  }
  return { sessionId, evidenceId, markerHash: row.auth_marker_hash, released: receipt.steel_released === true };
}

async function runBrowserAuthProductionE2E({ mode, env = process.env, deps } = {}) {
  if (!MODES.has(mode)) throw new ConfigurationError("Unknown browser auth production E2E mode");
  requiredEnvironment(env, mode);
  const origin = originOf(env.BROWSER_AUTH_PRODUCTION_ORIGIN);
  const tenants = [
    { tenant: "tenant-a", uid: String(env.BROWSER_AUTH_TENANT_A_UID), principalKind: "agent_owned" },
  ];
  if (mode !== "verify-provider-context") {
    tenants.push({
      tenant: "tenant-b",
      uid: String(env.BROWSER_AUTH_TENANT_B_UID),
      principalKind: "agent_owned",
    });
  }
  if (tenants.length === 2 && tenants[0].uid === tenants[1].uid) {
    throw new ConfigurationError("BROWSER_AUTH_TENANT_A_UID and BROWSER_AUTH_TENANT_B_UID must differ");
  }
  const enqueue = dependency(deps, "durableQueue", "enqueue");
  const read = dependency(deps, "durableQueue", "read");
  const execute = dependency(deps, "executor", "run");
  const result = {
    mode,
    tenant_count: tenants.length,
    origin,
    context_hashes: [],
    job_ids: [],
    steel_session_ids: [],
    telegram_evidence_ids: [],
    released: false,
  };
  let allReleased = true;

  const generatedMarkerHashes = new Set();
  for (const tenant of tenants) {
    let hash;
    do { hash = markerHash(); } while (generatedMarkerHashes.has(hash));
    generatedMarkerHashes.add(hash);
    const queued = await enqueue({ ...tenant, origin, markerHash: hash, mode });
    const jobId = boundedId(queued && queued.id, "job id");
    if (!queued || queued.auth_marker_hash !== hash) {
      throw new Error("browser auth durable marker hash unavailable");
    }
    const execution = await execute({ jobId });
    if (execution && execution.trace_id != null && String(execution.trace_id) !== jobId) {
      throw new Error("browser auth executor claimed an unexpected job");
    }
    const terminal = await read({ id: jobId });
    const evidence = terminalEvidence(terminal, { ...tenant, mode, markerHash: hash, jobId });
    result.context_hashes.push(evidence.markerHash);
    result.job_ids.push(jobId);
    result.steel_session_ids.push(evidence.sessionId);
    result.telegram_evidence_ids.push(evidence.evidenceId);
    allReleased = allReleased && evidence.released;
  }
  if (new Set(result.context_hashes).size !== tenants.length) {
    throw new Error("browser auth tenant marker hashes are not isolated");
  }
  result.released = allReleased;
  return Object.freeze(result);
}

function makeProductionDeps(env, boundaries = {}) {
  const {
    enqueueBrowserJob,
    claimBrowserJobById,
    readBrowserJob,
  } = require("../lib/browser-job-store.js");
  const { runNextBrowserJob } = require("../lib/browser-job-runtime.js");
  const storeOptions = typeof boundaries.query === "function" ? { query: boundaries.query } : {};
  let sequence = 0;
  const uidFor = (tenant) => tenant === "tenant-a"
    ? String(env.BROWSER_AUTH_TENANT_A_UID)
    : String(env.BROWSER_AUTH_TENANT_B_UID);

  return {
    durableQueue: {
      async enqueue({ tenant, markerHash, origin }) {
        const suffix = `${Date.now()}-${++sequence}`;
        const queued = await enqueueBrowserJob({
          uid: uidFor(tenant),
          chatId: String(env.BROWSER_AUTH_TELEGRAM_CHAT_ID),
          messageId: `browser-auth-e2e-${tenant}-${suffix}`,
          updateId: `browser-auth-e2e-${tenant}-${suffix}`,
          rawPrompt: `Verify the existing authenticated session at ${origin}.`,
          classification: {
            locale: "en",
            goal: `Read the current authenticated provider page at ${origin}; do not submit any action.`,
            actionKind: "browser_auth_continuity_readback",
            requiresLogin: true,
            principalKind: "agent_owned",
            authMarkerHash: markerHash,
          },
        }, storeOptions);
        return {
          id: queued && queued.job && queued.job.id,
          auth_marker_hash: queued && queued.job && queued.job.auth_marker_hash,
        };
      },
      async read({ id }) {
        return readBrowserJob(id, storeOptions);
      },
    },
    executor: {
      async run({ jobId }) {
        return runNextBrowserJob({
          ...storeOptions,
          ...(boundaries.driver ? { driver: boundaries.driver } : {}),
          ...(boundaries.sendMessage ? { sendMessage: boundaries.sendMessage } : {}),
          ...(boundaries.sendPhoto ? { sendPhoto: boundaries.sendPhoto } : {}),
          telegramToken: env.LM_TELEGRAM_BOT_TOKEN,
          claimJob: () => claimBrowserJobById(jobId, storeOptions),
        });
      },
    },
  };
}

function cliError(error) {
  return error instanceof ConfigurationError ? error.message : "browser auth production E2E failed";
}

if (require.main === module) {
  const mode = process.argv[2];
  runBrowserAuthProductionE2E({ mode, env: process.env, deps: makeProductionDeps(process.env) })
    .then((result) => process.stdout.write(`${JSON.stringify(result)}\n`))
    .catch((error) => {
      process.stderr.write(`${cliError(error)}\n`);
      process.exitCode = 1;
    });
}

module.exports = { runBrowserAuthProductionE2E, makeProductionDeps, OUTPUT_KEYS };
