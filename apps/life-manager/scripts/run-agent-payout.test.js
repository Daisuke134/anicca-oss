"use strict";

const assert = require("node:assert/strict");
const {
  chmodSync,
  mkdtempSync,
  symlinkSync,
  writeFileSync,
} = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const test = require("node:test");

const {
  DEFAULT_AGENT_WALLET,
  DEFAULT_FACILITATOR_START,
  ensureMainnetFacilitator,
  main,
  parseArgs,
  readProtectedWallet,
  readUsdcBalance,
} = require("./run-agent-payout.js");

test("the default facilitator source is owned by this repository", () => {
  assert.match(DEFAULT_FACILITATOR_START, /\/services\/facilitator\/start\.sh$/);
  assert.doesNotMatch(DEFAULT_FACILITATOR_START, /anicca-oss/);
});

const PRIVATE_KEY = `${"0".repeat(63)}1`;
const WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf";

test("--uid is mandatory and parsed without accepting a dangling option", () => {
  assert.deepEqual(parseArgs(["--uid", "u1"]), { uid: "u1" });
  assert.throws(() => parseArgs([]), /--uid/i);
  assert.throws(() => parseArgs(["--uid"]), /--uid/i);
  assert.throws(() => parseArgs(["--uid", "--other"]), /--uid/i);
});

test("protected wallet reader requires a regular 0600 file whose key derives its public address", async () => {
  const dir = mkdtempSync(join(tmpdir(), "lm-payout-wallet-"));
  const path = join(dir, "wallet.json");
  writeFileSync(path, JSON.stringify({ address: WALLET, privateKey: PRIVATE_KEY }), { mode: 0o600 });
  assert.deepEqual(await readProtectedWallet(path), {
    address: WALLET,
    privateKey: PRIVATE_KEY,
  });

  chmodSync(path, 0o644);
  await assert.rejects(() => readProtectedWallet(path), /0600/i);

  chmodSync(path, 0o600);
  const link = join(dir, "wallet-link.json");
  symlinkSync(path, link);
  await assert.rejects(() => readProtectedWallet(link), /symlink|regular/i);

  writeFileSync(path, JSON.stringify({
    address: DEFAULT_AGENT_WALLET,
    privateKey: PRIVATE_KEY,
  }), { mode: 0o600 });
  await assert.rejects(() => readProtectedWallet(path), /derive|match/i);
});

test("Base balance reader performs one canonical USDC balanceOf call and returns exact atomic units", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return {
      ok: true,
      status: 200,
      json: async () => ({ jsonrpc: "2.0", id: 1, result: "0x6acfc0" }),
    };
  };
  const amount = await readUsdcBalance(WALLET, {
    rpcUrl: "https://mainnet.base.org",
    fetchImpl,
  });

  assert.equal(amount, "7000000");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.method, "eth_call");
  assert.equal(calls[0].body.params[0].to.toLowerCase(), "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913");
  assert.equal(calls[0].body.params[0].data, `0x70a08231${WALLET.slice(2).toLowerCase().padStart(64, "0")}`);
  assert.equal(calls[0].body.params[1], "latest");
});

test("facilitator gate accepts only x402 v2 exact on Base mainnet", async () => {
  let starts = 0;
  const fetchImpl = async (url) => {
    if (String(url).endsWith("/health")) return { ok: true, status: 200, json: async () => ({}) };
    return {
      ok: true,
      status: 200,
      json: async () => ({
        kinds: [{ x402Version: 2, scheme: "exact", network: "eip155:8453" }],
      }),
    };
  };
  const result = await ensureMainnetFacilitator({
    facilitatorUrl: "http://127.0.0.1:8406",
    fetchImpl,
    execFileImpl: async () => { starts += 1; },
  });

  assert.deepEqual(result, { ok: true, started: false, network: "eip155:8453" });
  assert.equal(starts, 0);
});

test("an unavailable facilitator is started explicitly as Base mainnet and reverified", async () => {
  let started = false;
  let startCall;
  const fetchImpl = async (url) => {
    if (!started) throw new Error("connection refused");
    if (String(url).endsWith("/health")) return { ok: true, status: 200, json: async () => ({}) };
    return {
      ok: true,
      status: 200,
      json: async () => ({
        kinds: [{ x402Version: 2, scheme: "exact", network: "eip155:8453" }],
      }),
    };
  };
  const result = await ensureMainnetFacilitator({
    facilitatorUrl: "http://127.0.0.1:8406",
    startScript: "/facilitator/start.sh",
    fetchImpl,
    execFileImpl: async (file, args, opts) => {
      startCall = { file, args, env: opts.env };
      started = true;
    },
  });

  assert.deepEqual(result, { ok: true, started: true, network: "eip155:8453" });
  assert.equal(startCall.file, "/bin/bash");
  assert.deepEqual(startCall.args, ["/facilitator/start.sh"]);
  assert.equal(startCall.env.GIG_CHAIN, "base");
  assert.equal(startCall.env.PORT, "8406");
});

test("a live facilitator on the payout port with the wrong chain is refused, never repurposed", async () => {
  let starts = 0;
  const fetchImpl = async (url) => {
    if (String(url).endsWith("/health")) return { ok: true, status: 200, json: async () => ({}) };
    return {
      ok: true,
      status: 200,
      json: async () => ({
        kinds: [{ x402Version: 2, scheme: "exact", network: "eip155:84532" }],
      }),
    };
  };
  await assert.rejects(() => ensureMainnetFacilitator({
    facilitatorUrl: "http://127.0.0.1:8406",
    fetchImpl,
    execFileImpl: async () => { starts += 1; },
  }), /Base mainnet|8453/i);
  assert.equal(starts, 0);
});

test("zero-balance production-shaped main prints one safe no-op and never opens the key", async () => {
  const output = [];
  let keyReads = 0;
  let captured;
  const result = await main(["--uid", "u1"], {
    AGENT_WALLET_ADDRESS: WALLET,
    LM_PAYOUT_RESERVE_USDC_ATOMIC: "46000000",
    LM_PAYOUT_MAX_USDC_ATOMIC: "5000000",
    LM_AGENT_WALLET_PATH: "/protected/wallet.json",
  }, {
    runPayout: async (request, runtimeDeps) => {
      captured = { request, runtimeDeps };
      assert.equal(await runtimeDeps.readBalance(WALLET), "0");
      return {
        status: "noop",
        reason: "no_verified_surplus",
        amountAtomic: "0",
        verifiedSurplusMinor: 0,
        reserveAtomic: "46000000",
      };
    },
    readUsdcBalance: async () => "0",
    readProtectedWallet: async () => {
      keyReads += 1;
      throw new Error("must stay unopened");
    },
    stdout: { write: (text) => output.push(text) },
  });

  assert.equal(result.status, "noop");
  assert.equal(keyReads, 0);
  assert.equal(captured.request.uid, "u1");
  assert.equal(captured.request.walletAddress, WALLET);
  assert.equal(captured.request.reserveAtomic, "46000000");
  assert.equal(captured.request.maxPayoutAtomic, "5000000");
  assert.equal(typeof captured.runtimeDeps.readPrivateWallet, "function");
  assert.deepEqual(JSON.parse(output.join("")), result);
  assert.doesNotMatch(output.join(""), /u1|private|protected|telegram|supabase/i);
});

test("CLI output allowlists payout status and never serializes dependency results or unknown fields", async () => {
  const output = [];
  await main(["--uid", "u1"], { AGENT_WALLET_ADDRESS: WALLET }, {
    runPayout: async () => ({
      status: "transferred",
      amountAtomic: "7000000",
      txHash: `0x${"a".repeat(64)}`,
      payoutId: `tenant-${"b".repeat(64)}`,
      notificationSent: true,
      privateKey: "secret-must-not-print",
      destination: "secret-destination",
    }),
    stdout: { write: (text) => output.push(text) },
  });
  const printed = JSON.parse(output.join(""));

  assert.deepEqual(Object.keys(printed).sort(), [
    "amountAtomic", "notificationSent", "payoutId", "status", "txHash",
  ]);
  assert.doesNotMatch(output.join(""), /secret|privateKey|destination/);
});
