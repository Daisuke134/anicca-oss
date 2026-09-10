"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const test = require("node:test");

const { createAgentEconomyCloudWakeRunner } = require("./agent-economy-cloud-wake.js");

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
  const run = createAgentEconomyCloudWakeRunner({
    dataDir,
    signerTmpRoot,
    repoRoot: path.resolve(__dirname, "../../.."),
    env: { PATH: process.env.PATH, LM_CLOUD_CITIZEN_ENCRYPTION_KEY: "master-secret",
      SUPABASE_SERVICE_ROLE_KEY: "database-secret", LM_RUNTIME_DATABASE_URL: "postgres-secret" },
    citizenStore: { readSigner: async (input) => {
      signerReads += 1;
      assert.equal(input.tenant_id, "tenant/a");
      return { privateKey: PRIVATE_KEY };
    } },
    execFile: async (executable, args, options) => {
      assert.equal(executable, process.execPath);
      assert.equal(args[0], path.resolve(__dirname, "../../../runtime/loop/index.mjs"));
      assert.equal(options.env.ANICCA_SINGLE_WAKE, "1");
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
    },
  });
  const result = await run(identity());
  assert.equal(signerReads, 1);
  assert.deepEqual(result, { wake_id: "wake-1", kind: "acted", slot: "earn", profitable: true });
  assert.doesNotMatch(JSON.stringify(result), new RegExp(PRIVATE_KEY));
  await assert.rejects(() => fs.promises.lstat(ephemeralWalletPath), /ENOENT/);
  assert.match(dataDir, /lm-ae-wake-/);
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
