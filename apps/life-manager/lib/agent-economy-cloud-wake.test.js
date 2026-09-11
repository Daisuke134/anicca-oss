"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const test = require("node:test");

const {
  CLOUD_AGENT_ECONOMY_SLOTS,
  createAgentEconomyCloudWakeRunner,
  decideCitizenComputeRoute,
} = require("./agent-economy-cloud-wake.js");

const PRIVATE_KEY = "11".repeat(32);
const ADDRESS = `0x${"a".repeat(40)}`;

function identity() {
  return { tenant_id: "tenant/a", citizen_id: "primary", instance_id: "cloud",
    wallet: { chain: "eip155:8453", address: ADDRESS } };
}

test("Cloud runner invokes the shared loop for one wake and returns only its safe projection", async () => {
  const dataDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-wake-"));
  const signerTmpRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-signers-"));
  let signerReads = 0;
  let ephemeralWalletPath;
  const financialRecords = [];
  const run = createAgentEconomyCloudWakeRunner({
    dataDir,
    signerTmpRoot,
    repoRoot: path.resolve(__dirname, "../../.."),
    env: { PATH: process.env.PATH, ANICCA_MODEL: "anthropic/claude-opus-hostile",
      LM_CLOUD_CITIZEN_ENCRYPTION_KEY: "master-secret",
      SUPABASE_SERVICE_ROLE_KEY: "database-secret", LM_RUNTIME_DATABASE_URL: "postgres-secret" },
    citizenStore: { readSigner: async (input) => {
      signerReads += 1;
      assert.equal(input.tenant_id, "tenant-a");
      return { privateKey: PRIVATE_KEY };
    } },
    financialStore: { append: async (record) => financialRecords.push(record) },
    persistTaskMarketRevenue: async () => ({ recorded: 0 }),
    now: () => "2026-09-11T05:00:00.000Z",
    execFile: async (executable, args, options) => {
      assert.equal(executable, process.execPath);
      assert.equal(args[0], path.resolve(__dirname, "../../../runtime/loop/index.mjs"));
      assert.equal(options.env.ANICCA_SINGLE_WAKE, "1");
      assert.equal(options.env.ANICCA_SLOT_ALLOWLIST, CLOUD_AGENT_ECONOMY_SLOTS.join(","));
      assert.equal(options.env.ANICCA_STRICT_SLOT_ALLOWLIST, "1");
      assert.equal(options.env.ANICCA_MODEL, "nvidia/gpt-oss-120b");
      assert.deepEqual(CLOUD_AGENT_ECONOMY_SLOTS, ["earn/taskmarket"]);
      assert.equal(options.env.ANICCA_EVM_PRIVATE_KEY, undefined);
      assert.equal(options.env.LM_CLOUD_CITIZEN_ENCRYPTION_KEY, undefined);
      assert.equal(options.env.SUPABASE_SERVICE_ROLE_KEY, undefined);
      assert.equal(options.env.LM_RUNTIME_DATABASE_URL, undefined);
      assert.equal(options.env.ANICCA_WALLET_ADDRESS, ADDRESS);
      ephemeralWalletPath = options.env.ANICCA_EVM_WALLET_PATH;
      const wallet = JSON.parse(await fs.promises.readFile(ephemeralWalletPath, "utf8"));
      assert.deepEqual(wallet, { address: ADDRESS, privateKey: `0x${PRIVATE_KEY}` });
      assert.equal((await fs.promises.stat(ephemeralWalletPath)).mode & 0o777, 0o600);
      assert.equal(ephemeralWalletPath.startsWith(signerTmpRoot), true);
      const ledger = path.join(options.env.ANICCA_HOME, "state", "ledger.jsonl");
      await fs.promises.writeFile(ledger, `${JSON.stringify({ wake_id: "wake-1", kind: "acted", slot: "earn", profitable: true, secret: PRIVATE_KEY })}\n`);
      const earnLedger = path.join(options.env.ANICCA_HOME, "state", "skills", "earn", "earn-ledger.jsonl");
      await fs.promises.mkdir(path.dirname(earnLedger), { recursive: true });
      await fs.promises.writeFile(earnLedger, `${JSON.stringify({ ts: 1789100000, wake: "wake-1",
        source: "taskmarket_work_attempt", cost_usdc: 0.065, payment_receipt_id: "blockrun:paid-1" })}\n`);
    },
  });
  const result = await run({ ...identity(), tenant_id: "tenant-a" });
  assert.equal(signerReads, 1);
  assert.deepEqual(result, { wake_id: "wake-1", kind: "acted", slot: "earn", profitable: true,
    compute_route: { route: "bootstrap_free", model: "nvidia/gpt-oss-120b", tier: "free",
      authorization: "not_requested" } });
  assert.doesNotMatch(JSON.stringify(result), new RegExp(PRIVATE_KEY));
  assert.equal(financialRecords.length, 1);
  assert.deepEqual([financialRecords[0].subject_id, financialRecords[0].kind,
    financialRecords[0].amount_minor], ["tenant-a", "business_cost", 65000]);
  await assert.rejects(() => fs.promises.lstat(ephemeralWalletPath), /ENOENT/);
  assert.match(dataDir, /lm-ae-wake-/);
});

test("compute routing stays free without receipt-backed spend authority and authorizes only capped earned funds", async () => {
  const recipient = `0x${"81".repeat(20)}`;
  const revenueModule = await import("../../../skills/agent-economy/lib/revenue-receipt.mjs");
  const receipt = revenueModule.normalizeRevenueReceipt({
    provider: "x402", payer: `0x${"64".repeat(20)}`, recipient,
    gross: 0.003, fee: 0, refund: 0, asset: "USDC", terminal_state: "settled",
    occurred_at: "2026-09-11T04:00:00.000Z",
    proof: { chain_id: 8453, tx_hash: `0x${"36".repeat(32)}`, log_index: 1, verified: true },
  });
  assert.deepEqual(await decideCitizenComputeRoute({ freeModel: "free/glm-4.7" }), {
    route: "bootstrap_free", model: "nvidia/gpt-oss-120b", tier: "free",
    authorization: "not_requested",
  });
  assert.deepEqual(await decideCitizenComputeRoute({ freeModel: "anthropic/claude-opus-hostile" }), {
    route: "bootstrap_free", model: "nvidia/gpt-oss-120b", tier: "free",
    authorization: "not_requested",
  });
  const paid = await decideCitizenComputeRoute({ freeModel: "free/glm-4.7",
    frontierModel: "anthropic/claude-sonnet-4-6", expectedRecipient: recipient, spendRequest: {
      amountUsdc: 0.001, fundingReceiptIds: [receipt.idempotency_key], revenueReceipts: [receipt],
      recipient, reserveUsdc: 0.001, sessionSpentUsdc: 0, sessionCapUsdc: 0.001,
    } });
  assert.equal(paid.route, "citizen_x402");
  assert.equal(paid.tier, "frontier");
  assert.equal(paid.authorization, "ok");
  const denied = await decideCitizenComputeRoute({ freeModel: "free/glm-4.7",
    frontierModel: "anthropic/claude-sonnet-4-6", expectedRecipient: recipient, spendRequest: {
      amountUsdc: 0.002, fundingReceiptIds: [receipt.idempotency_key], revenueReceipts: [receipt],
      recipient, reserveUsdc: 0.001, sessionSpentUsdc: 0, sessionCapUsdc: 0.001,
    } });
  assert.equal(denied.route, "bootstrap_free");
  assert.equal(denied.authorization, "session-cap");
  const borrowed = await decideCitizenComputeRoute({ freeModel: "free/glm-4.7",
    frontierModel: "anthropic/claude-sonnet-4-6", expectedRecipient: ADDRESS, spendRequest: {
      amountUsdc: 0.001, fundingReceiptIds: [receipt.idempotency_key], revenueReceipts: [receipt],
      recipient, reserveUsdc: 0.001, sessionSpentUsdc: 0, sessionCapUsdc: 0.001,
    } });
  assert.equal(borrowed.route, "bootstrap_free");
  assert.equal(borrowed.authorization, "citizen-wallet-mismatch");
});

test("Cloud runner reaches the paid route only from its own simulated receipt-backed capped request", async () => {
  const dataDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-paid-route-"));
  const revenueModule = await import("../../../skills/agent-economy/lib/revenue-receipt.mjs");
  const receipt = revenueModule.normalizeRevenueReceipt({
    provider: "x402", payer: `0x${"64".repeat(20)}`, recipient: ADDRESS,
    gross: 0.003, fee: 0, refund: 0, asset: "USDC", terminal_state: "settled",
    occurred_at: "2026-09-11T04:00:00.000Z",
    proof: { chain_id: 8453, tx_hash: `0x${"37".repeat(32)}`, log_index: 1, verified: true },
  });
  const run = createAgentEconomyCloudWakeRunner({
    dataDir,
    env: { PATH: process.env.PATH, ANICCA_FRONTIER_MODEL: "anthropic/claude-sonnet-4-6" },
    citizenStore: { readSigner: async () => ({ privateKey: PRIVATE_KEY }) },
    computeSpendRequest: async (boundIdentity) => {
      assert.equal(boundIdentity.wallet.address, ADDRESS);
      return { amountUsdc: 0.001, fundingReceiptIds: [receipt.idempotency_key],
        revenueReceipts: [receipt], recipient: ADDRESS, reserveUsdc: 0.001,
        sessionSpentUsdc: 0, sessionCapUsdc: 0.001 };
    },
    persistTaskMarketRevenue: async () => ({ recorded: 0 }),
    execFile: async (_executable, _args, options) => {
      assert.equal(options.env.ANICCA_MODEL, "anthropic/claude-sonnet-4-6");
      await fs.promises.writeFile(path.join(options.env.ANICCA_HOME, "state", "ledger.jsonl"),
        `${JSON.stringify({ wake_id: "wake-paid", kind: "idle", profitable: false })}\n`);
    },
  });
  const result = await run(identity());
  assert.deepEqual(result.compute_route, { route: "citizen_x402",
    model: "anthropic/claude-sonnet-4-6", tier: "frontier", authorization: "ok" });
});

test("Cloud runner maps ambiguous tenant names to distinct durable homes", async () => {
  const dataDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-tenant-map-"));
  const homes = [];
  const run = createAgentEconomyCloudWakeRunner({
    dataDir,
    citizenStore: { readSigner: async () => ({ privateKey: PRIVATE_KEY }) },
    execFile: async (_executable, _args, options) => {
      homes.push(options.env.ANICCA_HOME);
      await fs.promises.writeFile(path.join(options.env.ANICCA_HOME, "state", "ledger.jsonl"),
        `${JSON.stringify({ wake_id: "wake", kind: "idle", profitable: false })}\n`);
    },
  });
  await run(identity());
  await run({ ...identity(), tenant_id: "tenant_2Fa" });
  assert.equal(new Set(homes).size, 2);
  assert.equal(path.basename(homes[0]), "agent-economy");
  assert.equal(path.basename(path.dirname(homes[0])), crypto.createHash("sha256").update("tenant/a").digest("hex"));
});

test("Cloud runner redacts child failure and signer material", async () => {
  const dataDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-wake-fail-"));
  const run = createAgentEconomyCloudWakeRunner({
    dataDir,
    citizenStore: { readSigner: async () => ({ privateKey: PRIVATE_KEY }) },
    execFile: async () => { throw new Error(`child leaked ${PRIVATE_KEY}`); },
  });
  await assert.rejects(() => run(identity()), (error) => {
    assert.equal(error.message, "Agent Economy shared wake failed");
    assert.equal(error.unknownEffect, true);
    assert.doesNotMatch(error.message, new RegExp(PRIVATE_KEY));
    return true;
  });
});

test("compute-routing failure removes the ephemeral signer before returning", async () => {
  const dataDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-route-fail-"));
  const signerTmpRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-route-signers-"));
  const run = createAgentEconomyCloudWakeRunner({
    dataDir, signerTmpRoot,
    citizenStore: { readSigner: async () => ({ privateKey: PRIVATE_KEY }) },
    decideComputeRoute: async () => { throw new Error(`route leaked ${PRIVATE_KEY}`); },
    execFile: async () => { assert.fail("child must not run after routing failure"); },
  });
  await assert.rejects(() => run(identity()), (error) => {
    assert.equal(error.message, "Agent Economy shared wake failed");
    assert.doesNotMatch(error.message, new RegExp(PRIVATE_KEY));
    return true;
  });
  assert.deepEqual(await fs.promises.readdir(signerTmpRoot), []);
});

test("signer cleanup failure remains a sanitized unknown money effect", async () => {
  const dataDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "lm-ae-cleanup-fail-"));
  const run = createAgentEconomyCloudWakeRunner({
    dataDir,
    citizenStore: { readSigner: async () => ({ privateKey: PRIVATE_KEY }) },
    execFile: async (_executable, _args, options) => {
      await fs.promises.writeFile(path.join(options.env.ANICCA_HOME, "state", "ledger.jsonl"),
        `${JSON.stringify({ wake_id: "wake", kind: "idle", profitable: false })}\n`);
    },
    removeDir: async () => { throw new Error(`filesystem leaked ${PRIVATE_KEY}`); },
  });
  await assert.rejects(() => run(identity()), (error) => {
    assert.equal(error.message, "Agent Economy signer cleanup failed");
    assert.equal(error.unknownEffect, true);
    assert.doesNotMatch(error.message, new RegExp(PRIVATE_KEY));
    return true;
  });
});
