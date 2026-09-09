#!/usr/bin/env node
"use strict";

const {
  lstatSync,
  readFileSync,
  statSync,
} = require("node:fs");
const { execFile } = require("node:child_process");
const { homedir } = require("node:os");
const { join } = require("node:path");
const { promisify } = require("node:util");
const { deriveAddress } = require("../lib/agent-wallet.js");
const { settleBaseUsdc } = require("../lib/base-usdc-payout.js");
const { readUsdcBalance } = require("../lib/base-usdc-balance.js");
const { usdMicrosFromDecimal } = require("../lib/financial-report-snapshot.js");
const { readFinancialCostTotals } = require("../lib/financial-report-runtime.js");
const { runPayout } = require("../lib/payout-runtime.js");

const DEFAULT_AGENT_WALLET = "0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad";
const DEFAULT_RPC_URL = "https://mainnet.base.org";
const DEFAULT_WALLET_PATH = join(homedir(), ".cloak", "life-manager-agent-wallet.json");
const DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8406";
const DEFAULT_FACILITATOR_START = join(
  __dirname, "..", "..", "..", "services", "facilitator", "start.sh",
);
const MAX_WALLET_BYTES = 4096;

function parseArgs(argv) {
  const args = Array.isArray(argv) ? argv : [];
  const index = args.indexOf("--uid");
  const uid = index >= 0 ? String(args[index + 1] || "").trim() : "";
  if (!uid || uid.startsWith("--")) throw new Error("--uid <tenant-uid> is required");
  return { uid };
}

async function readProtectedWallet(path = DEFAULT_WALLET_PATH) {
  const linkStat = lstatSync(path);
  if (linkStat.isSymbolicLink() || !linkStat.isFile()) {
    throw new Error("protected wallet path must be a regular file, never a symlink");
  }
  const fileStat = statSync(path);
  if ((fileStat.mode & 0o777) !== 0o600) {
    throw new Error("protected wallet file must have mode 0600");
  }
  if (fileStat.size <= 0 || fileStat.size > MAX_WALLET_BYTES) {
    throw new Error("protected wallet file has an invalid bounded size");
  }
  let value;
  try {
    value = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    throw new Error("protected wallet file is not valid JSON");
  }
  const address = String(value && value.address || "");
  const privateKey = String(value && value.privateKey || "");
  if (!address || !privateKey || deriveAddress(privateKey) !== address) {
    throw new Error("protected wallet private key does not derive the stored address");
  }
  return { address, privateKey };
}

async function facilitatorProfile(facilitatorUrl, fetchImpl) {
  try {
    const health = await fetchImpl(`${facilitatorUrl}/health`);
    if (!health || !health.ok) return { live: false, mainnet: false };
    const supported = await fetchImpl(`${facilitatorUrl}/supported`);
    if (!supported || !supported.ok) return { live: true, mainnet: false };
    const body = await supported.json().catch(() => ({}));
    const mainnet = Array.isArray(body.kinds) && body.kinds.some((kind) => (
      kind && kind.x402Version === 2
      && kind.scheme === "exact"
      && kind.network === "eip155:8453"
    ));
    return { live: true, mainnet };
  } catch {
    return { live: false, mainnet: false };
  }
}

async function ensureMainnetFacilitator(opts = {}) {
  const facilitatorUrl = String(opts.facilitatorUrl || DEFAULT_FACILITATOR_URL).replace(/\/$/, "");
  const url = new URL(facilitatorUrl);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") {
    throw new Error("payout facilitator must be the dedicated loopback service");
  }
  const fetchImpl = opts.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== "function") throw new Error("facilitator gate needs fetch");
  const before = await facilitatorProfile(facilitatorUrl, fetchImpl);
  if (before.live) {
    if (!before.mainnet) {
      throw new Error("live payout facilitator does not advertise x402 v2 exact on Base mainnet (8453)");
    }
    return { ok: true, started: false, network: "eip155:8453" };
  }

  const run = opts.execFileImpl || promisify(execFile);
  const startScript = opts.startScript || DEFAULT_FACILITATOR_START;
  await run("/bin/bash", [startScript], {
    env: {
      ...process.env,
      GIG_CHAIN: "base",
      PORT: url.port || "8406",
    },
  });
  const after = await facilitatorProfile(facilitatorUrl, fetchImpl);
  if (!after.live || !after.mainnet) {
    throw new Error("payout facilitator failed Base mainnet (8453) readiness verification");
  }
  return { ok: true, started: true, network: "eip155:8453" };
}

function publicResult(result) {
  const safe = {};
  const keys = [
    "status",
    "reason",
    "amountAtomic",
    "verifiedSurplusMinor",
    "reserveAtomic",
    "txHash",
    "payoutId",
    "notificationSent",
  ];
  for (const key of keys) {
    if (result && Object.prototype.hasOwnProperty.call(result, key)) safe[key] = result[key];
  }
  return safe;
}

async function main(argv = process.argv.slice(2), env = process.env, deps = {}) {
  const { uid } = parseArgs(argv);
  const walletAddress = env.AGENT_WALLET_ADDRESS || DEFAULT_AGENT_WALLET;
  const rpcUrl = env.BASE_RPC_URL || DEFAULT_RPC_URL;
  const walletPath = env.LM_AGENT_WALLET_PATH || DEFAULT_WALLET_PATH;
  const execute = deps.runPayout || runPayout;
  const balanceReader = deps.readUsdcBalance || readUsdcBalance;
  const protectedReader = deps.readProtectedWallet || readProtectedWallet;
  const operatingCostReader = deps.readOperatingCostMinor || (async (uid) => {
    const totals = await readFinancialCostTotals(uid, {
      period_start: "1970-01-01T00:00:00.000Z",
      period_end: new Date().toISOString(),
    }, {
      supaUrl: env.SUPABASE_URL,
      supaKey: env.SUPABASE_SERVICE_ROLE_KEY,
      fetchImpl,
    });
    const micros = usdMicrosFromDecimal(totals.all_time_est_usd);
    return Number((micros + 9_999n) / 10_000n);
  });
  const fetchImpl = deps.fetchImpl || globalThis.fetch;

  const result = await execute({
    uid,
    walletAddress,
    reserveAtomic: env.LM_PAYOUT_RESERVE_USDC_ATOMIC || undefined,
    maxPayoutAtomic: env.LM_PAYOUT_MAX_USDC_ATOMIC || undefined,
    facilitatorUrl: env.LM_PAYOUT_FACILITATOR_URL || DEFAULT_FACILITATOR_URL,
    rpcUrl,
  }, {
    supaUrl: env.SUPABASE_URL,
    supaKey: env.SUPABASE_SERVICE_ROLE_KEY,
    telegramToken: env.LM_TELEGRAM_BOT_TOKEN,
    fetchImpl,
    readBalance: (address) => balanceReader(address, { rpcUrl, fetchImpl }),
    readOperatingCostMinor: (uid) => operatingCostReader(uid),
    readPrivateWallet: () => protectedReader(walletPath),
    settle: deps.settle || (async (settlementRequest, settlementDeps) => {
      await (deps.ensureMainnetFacilitator || ensureMainnetFacilitator)({
        facilitatorUrl: settlementRequest.facilitatorUrl,
        startScript: env.LM_PAYOUT_FACILITATOR_START || DEFAULT_FACILITATOR_START,
        fetchImpl,
        execFileImpl: deps.execFileImpl,
      });
      return settleBaseUsdc(settlementRequest, settlementDeps);
    }),
  });
  const safe = publicResult(result);
  const stdout = deps.stdout || process.stdout;
  stdout.write(`${JSON.stringify(safe)}\n`);
  return safe;
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`[payout] ${error && error.message ? error.message : "failed"}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  DEFAULT_AGENT_WALLET,
  DEFAULT_RPC_URL,
  DEFAULT_WALLET_PATH,
  DEFAULT_FACILITATOR_URL,
  DEFAULT_FACILITATOR_START,
  parseArgs,
  readProtectedWallet,
  readUsdcBalance,
  ensureMainnetFacilitator,
  publicResult,
  main,
};
