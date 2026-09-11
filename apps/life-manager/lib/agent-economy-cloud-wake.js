"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const os = require("node:os");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const { persistWakeEconomicRecords, persistTaskMarketRevenue } = require("./agent-economy-economic-records.js");

const execFileAsync = promisify(execFile);
const CLOUD_AGENT_ECONOMY_SLOTS = Object.freeze([
  "earn/taskmarket",
]);

async function decideCitizenComputeRoute({ freeModel = "free/glm-4.7",
  frontierModel = "anthropic/claude-sonnet-4-6", expectedRecipient, spendRequest } = {}) {
  const [{ normalizeModelSelection }, { authorizeEarnedSpend }] = await Promise.all([
    import("../../../runtime/compute-proxy/model-map.mjs"),
    import("../../../skills/agent-economy/lib/treasury-policy.mjs"),
  ]);
  const selectFree = () => {
    const configured = normalizeModelSelection(freeModel, frontierModel);
    return configured.tier === "free"
      ? configured : normalizeModelSelection("nvidia/gpt-oss-120b", frontierModel);
  };
  if (!spendRequest) {
    const selected = selectFree();
    return Object.freeze({ route: "bootstrap_free", ...selected, authorization: "not_requested" });
  }
  if (!expectedRecipient || String(spendRequest.recipient || "").toLowerCase()
    !== String(expectedRecipient).toLowerCase()) {
    const selected = selectFree();
    return Object.freeze({ route: "bootstrap_free", ...selected,
      authorization: "citizen-wallet-mismatch" });
  }
  const authorization = authorizeEarnedSpend(spendRequest);
  if (authorization.allowed === true) {
    const selected = normalizeModelSelection(frontierModel, frontierModel);
    return Object.freeze({ route: "citizen_x402", ...selected, authorization: "ok" });
  }
  const selected = selectFree();
  return Object.freeze({ route: "bootstrap_free", ...selected,
    authorization: String(authorization.reason || "denied") });
}

function safeSegment(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

async function lastWake(ledgerPath) {
  const lines = (await fs.promises.readFile(ledgerPath, "utf8")).trim().split("\n").filter(Boolean);
  const row = JSON.parse(lines.at(-1));
  return Object.freeze({
    wake_id: String(row.wake_id || ""),
    kind: String(row.kind || "unknown"),
    slot: row.slot == null ? null : String(row.slot),
    profitable: row.profitable === true,
  });
}

async function writeEphemeralWallet(tmpRoot, wallet) {
  const rootStat = await fs.promises.lstat(tmpRoot);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error("Agent Economy signer tmpfs unavailable");
  }
  const signerDir = await fs.promises.mkdtemp(path.join(tmpRoot, "lm-ae-signer-"));
  await fs.promises.chmod(signerDir, 0o700);
  const walletPath = path.join(signerDir, "wallet.json");
  await fs.promises.writeFile(walletPath, JSON.stringify(wallet), { flag: "wx", mode: 0o600 });
  return { signerDir, walletPath };
}

function createAgentEconomyCloudWakeRunner(options = {}) {
  const repoRoot = path.resolve(options.repoRoot || path.join(__dirname, "../../.."));
  const dataDir = path.resolve(String(options.dataDir || ""));
  const signerTmpRoot = path.resolve(options.signerTmpRoot
    || (fs.existsSync("/dev/shm") ? "/dev/shm" : os.tmpdir()));
  const run = options.execFile || execFileAsync;
  const removeDir = options.removeDir || fs.promises.rm;
  if (!options.citizenStore || typeof options.citizenStore.readSigner !== "function"
    || !dataDir || dataDir === path.parse(dataDir).root) {
    throw new Error("Agent Economy Cloud wake boundary unavailable");
  }
  return async function runSharedWake(identity) {
    const signer = await options.citizenStore.readSigner(identity);
    const privateKey = String(signer && signer.privateKey || "");
    if (!/^(?:0x)?[0-9a-f]{64}$/i.test(privateKey)) throw new Error("Agent Economy signer unavailable");
    const instanceHome = path.join(dataDir, "tenants", safeSegment(identity.tenant_id), "agent-economy");
    await fs.promises.mkdir(path.join(instanceHome, "state"), { recursive: true, mode: 0o700 });
    const { signerDir, walletPath } = await writeEphemeralWallet(signerTmpRoot, {
      address: identity.wallet.address,
      privateKey: privateKey.startsWith("0x") ? privateKey : `0x${privateKey}`,
    });
    try {
      const parentEnv = options.env || process.env;
      const computeRoute = await (options.decideComputeRoute || decideCitizenComputeRoute)({
        freeModel: parentEnv.ANICCA_FREE_MODEL || "free/glm-4.7",
        frontierModel: parentEnv.ANICCA_FRONTIER_MODEL || "anthropic/claude-sonnet-4-6",
        expectedRecipient: identity.wallet.address,
        spendRequest: typeof options.computeSpendRequest === "function"
          ? await options.computeSpendRequest(identity) : options.computeSpendRequest,
      });
      const entrypoint = path.join(repoRoot, "runtime/loop/index.mjs");
      const childEnv = {
      PATH: parentEnv.PATH,
      LANG: parentEnv.LANG,
      LC_ALL: parentEnv.LC_ALL,
      TMPDIR: parentEnv.TMPDIR,
      NODE_ENV: parentEnv.NODE_ENV,
      HOME: instanceHome,
      ANICCA_HOME: instanceHome,
      ANICCA_INSTANCE: identity.instance_id,
      ANICCA_WALLET_ADDRESS: identity.wallet.address,
      ANICCA_EVM_WALLET_PATH: walletPath,
      ANICCA_SINGLE_WAKE: "1",
      ANICCA_SLOT_ALLOWLIST: CLOUD_AGENT_ECONOMY_SLOTS.join(","),
      ANICCA_STRICT_SLOT_ALLOWLIST: "1",
      ANICCA_MODEL: computeRoute.model,
      SLEEP_BASE_S: "0",
      SLEEP_ERROR_S: "0",
      LEDGER_PUBLISH_ENABLED: "0",
      };
      for (const key of [
        "OPENAI_BASE_URL", "ANICCA_BRAIN", "ANICCA_FREE_MODEL",
        "ANICCA_LEAN_MODEL", "ANICCA_FUNDED_MODEL", "BASE_RPC_URL", "USDC_ADDRESS",
      ]) {
        if (parentEnv[key]) childEnv[key] = parentEnv[key];
      }
      await run(process.execPath, [entrypoint], {
        cwd: repoRoot,
        env: childEnv,
        timeout: 10 * 60 * 1000,
        maxBuffer: 1024 * 1024,
      });
      const wake = await lastWake(path.join(instanceHome, "state", "ledger.jsonl"));
      await persistWakeEconomicRecords({ instanceHome, wakeId: wake.wake_id,
        subjectId: identity.tenant_id, financialStore: options.financialStore,
        recordedAt: options.now ? options.now() : new Date().toISOString() });
      await (options.persistTaskMarketRevenue || persistTaskMarketRevenue)({ identity,
        financialStore: options.financialStore,
        recordedAt: options.now ? options.now() : new Date().toISOString(),
        fetchImpl: options.fetchImpl, selfWallets: options.selfWallets || [] });
      return Object.freeze({ ...wake, compute_route: computeRoute });
    } catch {
      const error = new Error("Agent Economy shared wake failed");
      error.unknownEffect = true;
      throw error;
    } finally {
      try {
        await removeDir(signerDir, { recursive: true, force: true });
      } catch {
        const error = new Error("Agent Economy signer cleanup failed");
        error.unknownEffect = true;
        throw error;
      }
    }
  };
}

module.exports = { CLOUD_AGENT_ECONOMY_SLOTS, createAgentEconomyCloudWakeRunner, decideCitizenComputeRoute };
