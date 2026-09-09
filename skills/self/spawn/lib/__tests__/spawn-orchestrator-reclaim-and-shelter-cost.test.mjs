// VCSDD anicca-agent-spawn, sprint-2, Phase 3 implementation review findings FIND-001/FIND-002.
//
//  - FIND-001 (money-safety gap): defaultReclaimSeed -- a best-effort reclaim of the REQ-204 gas seed
//    when it landed but a LATER step in the same attempt then fails, wired via `deps.reclaimSeed` at
//    the three failure sites that can only be reached AFTER a successful seedStep (registerIdentity,
//    buildChildSpec's own throw, mcp.json write).
//  - FIND-002 (unwired requirement): PROP-303c's shelter-cost-ledger wiring -- defaultDeploy's real
//    shelterCostUsd, appendShelterCostEntry's real call site in executeSpawnAttempt, and leaseId
//    surviving onto the final ledger row.
//
// SAFETY: every test below injects deps.reclaimSeed/deps.runSeedChild fixtures where relevant -- the
// real subprocess-calling defaultReclaimSeed/defaultSeedChild python3 path is only exercised by the
// small, dedicated, seedDeps-injected unit tests in the first section (mirroring
// spawn-orchestrator-gas-seed-and-key-persistence.test.mjs's own established discipline: EVERY direct
// defaultReclaimSeed unit test injects seedDeps.runSeedChild, never the real python3 seed-child.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  executeSpawnAttempt,
  defaultReclaimSeed,
} from "../spawn-orchestrator.mjs";
import { readShelterCostEntries } from "../shelter-cost-ledger.js";

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "anicca-spawn-orch-reclaim-"));
}

function readLedgerRows(file) {
  if (!fs.existsSync(file)) return [];
  return fs
    .readFileSync(file, "utf8")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => JSON.parse(l));
}

const DRIVING_CITIZEN_WALLET = "0xParentWalletFixtureAbc0000000000000001";
const CHILD_EVM_WALLET = { address: "0xChildEvmFixture0000000000000000000000001", privateKey: "0xplaceholderchildevmfixturekey" };

function baseParams(overrides = {}) {
  return { initialSkills: [], drivingCitizenWallet: DRIVING_CITIZEN_WALLET, nowMs: 1_800_000_000_000, ...overrides };
}

function happyDeps(dir, overrides = {}) {
  return {
    lockStatePath: path.join(dir, "citizens.json"),
    ledgerFile: path.join(dir, "children.jsonl"),
    citizensRegistryFile: path.join(dir, "citizens.json"),
    shelterCostFile: path.join(dir, "shelter-cost.jsonl"),
    checkHomeDistinct: async () => ({ ok: true, homeDir: path.join(dir, "child-home") }),
    generateEvmWallet: async () => ({ ...CHILD_EVM_WALLET }),
    persistChildWallet: async () => ({ ok: true, walletPath: path.join(dir, "child-home", ".automaton", "wallet.json") }),
    selectCloudTarget: async () => "akash",
    generateSolanaWallet: async () => ({ address: "ChildSolanaFixture1111111111111111111111111", privateKey: "childsolanafixturekey" }),
    deploy: async () => ({ ok: true, leaseId: "dseq-fixture-001", shelterCostUsd: 0.5 }),
    seedChild: async () => ({ ok: true, txHash: "0xseedtxfixture" }),
    registerIdentity: async () => ({ ok: true, agentId: "9001", txHash: "0xregtxfixture" }),
    writeMcpConfig: async () => ({ ok: true }),
    reclaimSeed: async () => ({ ok: true, txHash: "0xreclaimtxfixture" }),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// FIND-001: defaultReclaimSeed direct unit coverage. EVERY test injects seedDeps.runSeedChild -- the
// real python3 seed-child.py invocation is never exercised here.
// ---------------------------------------------------------------------------

test("defaultReclaimSeed: happy path -- signs with the CHILD's own in-memory key, sends to the parent's address, writes a snake_case {private_key,address} temp wallet file, returns the tx hash, then shreds the temp file", () => {
  let seenArgs;
  let seenFileDuringCall;
  const result = defaultReclaimSeed(
    { childEvmWallet: CHILD_EVM_WALLET, parentWalletAddress: DRIVING_CITIZEN_WALLET, amountUsdc: 1 },
    {
      runSeedChild: (destAddr, amount, walletJsonPath) => {
        seenArgs = { destAddr, amount, walletJsonPath };
        seenFileDuringCall = JSON.parse(fs.readFileSync(walletJsonPath, "utf8"));
        return "0xreclaimtxrealfixture\n";
      },
    }
  );
  assert.equal(result.ok, true);
  assert.equal(result.txHash, "0xreclaimtxrealfixture");
  assert.equal(seenArgs.destAddr, DRIVING_CITIZEN_WALLET, "the reclaim's destination must be the driving citizen's own wallet, never the child itself");
  assert.equal(seenArgs.amount, 1);
  assert.deepEqual(Object.keys(seenFileDuringCall).sort(), ["address", "private_key"]);
  assert.equal(seenFileDuringCall.private_key, CHILD_EVM_WALLET.privateKey, "the SIGNER must be the child's own key, never the parent's");
  assert.equal(seenFileDuringCall.address, CHILD_EVM_WALLET.address);
  assert.equal(fs.existsSync(seenArgs.walletJsonPath), false, "the transient child-wallet temp file must be shredded/removed after the attempt");
});

test("defaultReclaimSeed: the transient temp file is 600-perm while it briefly exists", () => {
  let seenMode;
  defaultReclaimSeed(
    { childEvmWallet: CHILD_EVM_WALLET, parentWalletAddress: DRIVING_CITIZEN_WALLET, amountUsdc: 1 },
    {
      runSeedChild: (destAddr, amount, walletJsonPath) => {
        seenMode = fs.statSync(walletJsonPath).mode & 0o777;
        return "0xreclaimtxfixture2";
      },
    }
  );
  assert.equal(seenMode, 0o600);
});

test("defaultReclaimSeed: seed-child.py's own real failure (exit 1, e.g. tx reverted) fails cleanly, temp file still shredded (d)", () => {
  let seenPath;
  const result = defaultReclaimSeed(
    { childEvmWallet: CHILD_EVM_WALLET, parentWalletAddress: DRIVING_CITIZEN_WALLET, amountUsdc: 1 },
    {
      runSeedChild: (destAddr, amount, walletJsonPath) => {
        seenPath = walletJsonPath;
        const e = new Error("Command failed");
        e.status = 1;
        e.stderr = "seed-child: tx 0xdeadbeef reverted\n";
        throw e;
      },
    }
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /seed-child\.py failed/);
  assert.equal(fs.existsSync(seenPath), false, "(d) the temp wallet file is shredded after the reclaim attempt regardless of outcome");
});

test("defaultReclaimSeed: seed-child.py's own documented PENDING (exit 2, receipt timed out) is a clean failure, never a fabricated success, temp file still shredded", () => {
  let seenPath;
  const result = defaultReclaimSeed(
    { childEvmWallet: CHILD_EVM_WALLET, parentWalletAddress: DRIVING_CITIZEN_WALLET, amountUsdc: 1 },
    {
      runSeedChild: (destAddr, amount, walletJsonPath) => {
        seenPath = walletJsonPath;
        const e = new Error("Command failed");
        e.status = 2;
        e.stderr = "PENDING 0xabc123def4560000000000000000000000000000000000000000000000000001\n";
        throw e;
      },
    }
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /receipt timed out/);
  assert.match(result.error, /PENDING 0xabc123def4560000000000000000000000000000000000000000000000000001/);
  assert.equal(fs.existsSync(seenPath), false);
});

test("defaultReclaimSeed: seed-child.py prints no tx hash at all -> ok:false, never a fabricated success", () => {
  const result = defaultReclaimSeed(
    { childEvmWallet: CHILD_EVM_WALLET, parentWalletAddress: DRIVING_CITIZEN_WALLET, amountUsdc: 1 },
    { runSeedChild: () => "   \n" }
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /no tx hash/);
});

// ---------------------------------------------------------------------------
// FIND-001: executeSpawnAttempt's real wiring of the reclaim at the three post-seedStep failure sites.
// ---------------------------------------------------------------------------

test("FIND-001(a): identityStep (step 6) fails AFTER a successful seedStep -> a real reclaim fires with the child's own credentials and the parent as recipient, and the failure row records reclaimed:true", async () => {
  const dir = tmpDir();
  let seenReclaimArgs;
  const deps = happyDeps(dir, {
    registerIdentity: async () => ({ ok: false, error: "Registered event could not be decoded" }),
    reclaimSeed: async (args) => { seenReclaimArgs = args; return { ok: true, txHash: "0xreclaimedtx" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.ok(seenReclaimArgs, "deps.reclaimSeed must actually be invoked");
  assert.equal(seenReclaimArgs.childEvmWallet.address, CHILD_EVM_WALLET.address);
  assert.equal(seenReclaimArgs.childEvmWallet.privateKey, CHILD_EVM_WALLET.privateKey);
  assert.equal(seenReclaimArgs.parentWalletAddress, DRIVING_CITIZEN_WALLET);
  assert.equal(typeof seenReclaimArgs.amountUsdc, "number", "amountUsdc must be the SAME seedUsdc already computed for the seed transfer, never a re-derived/hardcoded number");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].reclaimed, true);
  assert.equal(rows[0].reclaimTxHash, "0xreclaimedtx");
  assert.equal(rows[0].lease_id, "dseq-fixture-001");
  assert.match(rows[0].error, /Registered event could not be decoded/, "the ORIGINAL failure's error must never be masked/replaced by the reclaim");
});

test("FIND-001(b): seedStep itself fails -> reclaim is correctly SKIPPED (no reclaimed/reclaimTxHash/reclaimError/lease_id fields), deps.reclaimSeed is never invoked", async () => {
  const dir = tmpDir();
  let reclaimCalls = 0;
  const deps = happyDeps(dir, {
    seedChild: async () => ({ ok: false, error: "REQ-204 gas-seed: seed-child.py failed: tx reverted" }),
    reclaimSeed: async () => { reclaimCalls += 1; return { ok: true, txHash: "0xshouldneverhappen" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.equal(reclaimCalls, 0, "reclaim must never be attempted when the gas seed itself never landed");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assert.deepEqual(Object.keys(rows[0]).sort(), [
    "attempted_ms", "child_id", "citizen_id", "error", "event_type",
    "lifecycle_status", "occurred_at_ms", "receipt_id", "schema", "status",
  ], "no reclaim/lease_id fields when no reclaim was ever attempted");
});

test("FIND-001(c): a reclaim that itself fails is swallowed and recorded as reclaimed:false, never masking the original failure", async () => {
  const dir = tmpDir();
  const deps = happyDeps(dir, {
    registerIdentity: async () => ({ ok: false, error: "Registered event could not be decoded" }),
    reclaimSeed: async () => ({ ok: false, error: "reclaim: seed-child.py failed: tx reverted" }),
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.match(result.error, /Registered event could not be decoded/, "executeSpawnAttempt's own returned error must still be the ORIGINAL failure, never the reclaim's");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].reclaimed, false);
  assert.equal(rows[0].reclaimError, "reclaim: seed-child.py failed: tx reverted");
  assert.equal(rows[0].reclaimTxHash, undefined);
  assert.match(rows[0].error, /Registered event could not be decoded/);
});

test("FIND-001(c) variant: a reclaim that THROWS (rather than returning ok:false) is also swallowed, never propagates/crashes the attempt", async () => {
  const dir = tmpDir();
  const deps = happyDeps(dir, {
    registerIdentity: async () => ({ ok: false, error: "Registered event could not be decoded" }),
    reclaimSeed: async () => { throw new Error("reclaim: unexpected network error"); },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.match(result.error, /Registered event could not be decoded/);
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows[0].reclaimed, false);
  assert.equal(rows[0].reclaimError, "reclaim: unexpected network error");
});

test("FIND-001: buildChildSpec's own real distinct-wallet assertion (step 8) fires AFTER a successful seedStep -> reclaim also fires on this path", async () => {
  const dir = tmpDir();
  let reclaimCalls = 0;
  const deps = happyDeps(dir, {
    generateEvmWallet: async () => ({ address: DRIVING_CITIZEN_WALLET, privateKey: "0xk" }),
    reclaimSeed: async () => { reclaimCalls += 1; return { ok: true, txHash: "0xreclaimed8" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.match(result.error, /distinct from parent wallet/);
  assert.equal(reclaimCalls, 1);
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows[0].reclaimed, true);
  assert.equal(rows[0].reclaimTxHash, "0xreclaimed8");
  assert.equal(rows[0].lease_id, "dseq-fixture-001");
});

test("FIND-001: mcp.json write failure (step 7) fires AFTER a successful seedStep -> reclaim also fires on this path, buildChildSpec's own fields still present", async () => {
  const dir = tmpDir();
  let reclaimCalls = 0;
  const deps = happyDeps(dir, {
    writeMcpConfig: async () => { throw new Error("GIG_STATE_PATH collided with an existing citizen"); },
    reclaimSeed: async () => { reclaimCalls += 1; return { ok: true, txHash: "0xreclaimed7" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.equal(reclaimCalls, 1);
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows[0].reclaimed, true);
  assert.equal(rows[0].reclaimTxHash, "0xreclaimed7");
  assert.equal(rows[0].lease_id, "dseq-fixture-001");
  assert.equal(typeof rows[0].wallet, "string", "the buildChildSpec-shaped row's own fields must still be present alongside the reclaim fields");
});

// ---------------------------------------------------------------------------
// FIND-002/PROP-303c: executeSpawnAttempt's real shelter-cost-ledger + leaseId wiring.
// ---------------------------------------------------------------------------

test("FIND-002: a successful deploy with a real shelterCostUsd genuinely appends to the shelter-cost ledger file wake-gate.mjs reads, with the expected row shape", async () => {
  const dir = tmpDir();
  const deps = happyDeps(dir, { deploy: async () => ({ ok: true, leaseId: "dseq-shelter-001", shelterCostUsd: 0.01 }) });
  const nowMs = 1_800_000_000_000;
  const result = await executeSpawnAttempt(baseParams({ nowMs }), deps);
  assert.equal(result.status, "active");
  const entries = readShelterCostEntries(deps.shelterCostFile);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].settledLeaseCostUsd, 0.01);
  assert.equal(entries[0].ts, nowMs);
  assert.equal(entries[0].leaseId, "dseq-shelter-001");
});

test("FIND-002: a deploy with shelterCostUsd:null (price unavailable) never appends a fabricated row -- the shelter-cost ledger stays empty", async () => {
  const dir = tmpDir();
  const deps = happyDeps(dir, { deploy: async () => ({ ok: true, leaseId: "dseq-noprice-001", shelterCostUsd: null }) });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active");
  assert.equal(fs.existsSync(deps.shelterCostFile), false, "no row, no file at all -- appendShelterCostEntry must never be called");
});

test("FIND-002: a deploy with shelterCostUsd:0 never appends -- a fabricated $0 row would poison deriveMeasuredShelterCostUsd's next reading", async () => {
  const dir = tmpDir();
  const deps = happyDeps(dir, { deploy: async () => ({ ok: true, leaseId: "dseq-zero-001", shelterCostUsd: 0 }) });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active");
  assert.equal(fs.existsSync(deps.shelterCostFile), false);
});

test("FIND-002: leaseId survives onto the final active ledger row", async () => {
  const dir = tmpDir();
  const deps = happyDeps(dir, { deploy: async () => ({ ok: true, leaseId: "dseq-persisted-001", shelterCostUsd: 0.02 }) });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "active");
  assert.equal(rows[0].lease_id, "dseq-persisted-001");
});

test("FIND-002: a shelter-cost ledger append failure (e.g. unwritable path) is caught and logged, never crashes/fails the spawn attempt itself", async () => {
  const dir = tmpDir();
  const blockerFile = path.join(dir, "not-a-directory");
  fs.writeFileSync(blockerFile, "x");
  const deps = happyDeps(dir, {
    deploy: async () => ({ ok: true, leaseId: "dseq-badpath-001", shelterCostUsd: 0.02 }),
    shelterCostFile: path.join(blockerFile, "shelter-cost.jsonl"),
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active", "a shelter-cost ledger write failure must never fail the spawn attempt itself -- it is pure colony accounting, not part of the identity/ledger critical path");
});
