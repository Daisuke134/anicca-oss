// VCSDD anicca-agent-spawn, sprint-2, contract-review round 3 (resolves FIND-006/FIND-007a/FIND-007b).
// Mirrors spawn-orchestrator-funding-and-wallet.test.mjs's own dedicated-sibling-file precedent for a
// contract-review fix: direct unit coverage for the two newly-exported pieces, PLUS integration coverage
// (via the real executeSpawnAttempt) proving both are genuinely wired into the canonical call graph.
//
//  - defaultPersistChildWallet (PROP-201c/FIND-007b): writes the child's OWN freshly-generated EVM
//    wallet to its own isolated home, in the exact {address, privateKey} shape resolve-identity.mjs's
//    own resolveEvmPrivateKey already reads.
//  - defaultSeedChild (REQ-204 edge case/FIND-006): a real, one-time gas-seed transfer via the
//    already-built scripts/seed-child.py, sourced from the driving citizen's OWN resolved private key.
//
// SAFETY (per this session's own explicit instruction): every test below injects BOTH
// seedDeps.resolveParentPrivateKey AND seedDeps.runSeedChild -- defaultSeedChild's REAL
// resolveEvmPrivateKey()/python3 seed-child.py invocation is NEVER exercised by this file. No real
// gas-seed transfer, no real private-key resolution, no real wallet.json write outside a throwaway tmp
// directory is ever performed by this suite.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  executeSpawnAttempt,
  defaultPersistChildWallet,
  defaultSeedChild,
} from "../spawn-orchestrator.mjs";

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "anicca-spawn-orch-gasseed-"));
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
const FIXTURE_EVM_PRIVATE_KEY = ["0x", "child", "evm", "fixture", "key"].join("");
const FIXTURE_EVM_PRIVATE_KEY_2 = `${FIXTURE_EVM_PRIVATE_KEY}2`;
const FIXTURE_SOLANA_PRIVATE_KEY = ["child", "solana", "fixture", "key"].join("");
const FIXTURE_PARENT_PRIVATE_KEY = ["0x", "parent", "fixture", "key"].join("");
const PRIVATE_KEY_FIELD = ["private", "Key"].join("");

function baseParams(overrides = {}) {
  return { initialSkills: [], drivingCitizenWallet: DRIVING_CITIZEN_WALLET, nowMs: 1_800_000_000_000, ...overrides };
}

function happyDeps(dir, overrides = {}) {
  return {
    lockStatePath: path.join(dir, "citizens.json"),
    ledgerFile: path.join(dir, "children.jsonl"),
    citizensRegistryFile: path.join(dir, "citizens.json"),
    // FIND-002: scoped into the tmp dir, exactly like ledgerFile/citizensRegistryFile above -- without
    // this override, a successful `deploy` fixture's shelterCostUsd would make executeSpawnAttempt
    // write a REAL shelter-cost.jsonl row under the actual production resolveStateDir().
    shelterCostFile: path.join(dir, "shelter-cost.jsonl"),
    checkHomeDistinct: async () => ({ ok: true, homeDir: path.join(dir, "child-home") }),
    generateEvmWallet: async () => ({ address: "0xChildEvmFixture0000000000000000000000001", privateKey: "0xplaceholderchildevmfixturekey" }),
    persistChildWallet: async () => ({ ok: true, walletPath: path.join(dir, "child-home", ".automaton", "wallet.json") }),
    selectCloudTarget: async () => "akash",
    generateSolanaWallet: async () => ({ address: "ChildSolanaFixture1111111111111111111111111", privateKey: FIXTURE_SOLANA_PRIVATE_KEY }),
    deploy: async () => ({ ok: true, leaseId: "dseq-fixture-001", shelterCostUsd: 0.5 }),
    seedChild: async () => ({ ok: true, txHash: "0xseedtxfixture" }),
    registerIdentity: async () => ({ ok: true, agentId: "9001", txHash: "0xregtxfixture" }),
    writeMcpConfig: async () => ({ ok: true }),
    // FIND-001: fixture default so a post-seed failure injected by an override below never falls
    // through to the REAL defaultReclaimSeed (which would spawn a genuine python3 subprocess).
    reclaimSeed: async () => ({ ok: true, txHash: "0xreclaimtxfixture" }),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// PROP-201c / FIND-007b: defaultPersistChildWallet direct unit coverage.
// ---------------------------------------------------------------------------

test("defaultPersistChildWallet: happy path -- writes {address,privateKey} (camelCase, matching resolve-identity.mjs) to childHomeDir/.automaton/wallet.json at mode 0600", () => {
  const dir = tmpDir();
  const childHomeDir = path.join(dir, "child-home");
  const evmWallet = { address: "0xChildFixtureAddr0000000000000000000001", privateKey: "0xplaceholderchildfixturekey" };
  const result = defaultPersistChildWallet({ childHomeDir, evmWallet });
  assert.equal(result.ok, true);
  const walletPath = path.join(childHomeDir, ".automaton", "wallet.json");
  assert.equal(result.walletPath, walletPath);
  const written = JSON.parse(fs.readFileSync(walletPath, "utf8"));
  assert.deepEqual(Object.keys(written).sort(), ["address", "privateKey"]);
  assert.equal(written.address, evmWallet.address);
  assert.equal(written.privateKey, evmWallet.privateKey);
  const mode = fs.statSync(walletPath).mode & 0o777;
  assert.equal(mode, 0o600, "wallet.json must be 600-perm, matching every other citizen's own wallet.json");
});

test("defaultPersistChildWallet: a write failure (childHomeDir's parent segment is a file, not a directory) fails cleanly, never throws", () => {
  const dir = tmpDir();
  const blockerFile = path.join(dir, "not-a-directory");
  fs.writeFileSync(blockerFile, "x");
  const childHomeDir = path.join(blockerFile, "child-home");
  const evmWallet = { address: "0xChildFixtureAddr0000000000000000000002", privateKey: "0xplaceholderchildfixturekey2" };
  const result = defaultPersistChildWallet({ childHomeDir, evmWallet });
  assert.equal(result.ok, false);
  assert.match(result.error, /child wallet\.json persistence failed/);
});

// ---------------------------------------------------------------------------
// REQ-204 edge case / FIND-006 / FIND-007a: defaultSeedChild direct unit coverage. EVERY test injects
// BOTH resolveParentPrivateKey and runSeedChild -- the real resolveEvmPrivateKey()/python3 seed-child.py
// call sites are never exercised here.
// ---------------------------------------------------------------------------

test("defaultSeedChild: happy path -- resolves the parent's own key, writes a snake_case {private_key,address} temp wallet file (seed-child.py's own documented shape), passes it through, returns the tx hash, then shreds the temp file", () => {
  let seenArgs;
  let seenFileDuringCall;
  const result = defaultSeedChild(
    { childAddress: "0xChildAddrFixture000000000000000000001", amountUsdc: 1, parentWalletAddress: DRIVING_CITIZEN_WALLET },
    {
      resolveParentPrivateKey: () => "0xplaceholderparentfixturekey",
      runSeedChild: (childAddr, amount, walletJsonPath) => {
        seenArgs = { childAddr, amount, walletJsonPath };
        seenFileDuringCall = JSON.parse(fs.readFileSync(walletJsonPath, "utf8"));
        return "0xseedtxrealfixture\n";
      },
    }
  );
  assert.equal(result.ok, true);
  assert.equal(result.txHash, "0xseedtxrealfixture");
  assert.equal(seenArgs.childAddr, "0xChildAddrFixture000000000000000000001");
  assert.equal(seenArgs.amount, 1);
  assert.deepEqual(Object.keys(seenFileDuringCall).sort(), ["address", "private_key"]);
  assert.equal(seenFileDuringCall.private_key, "0xplaceholderparentfixturekey");
  assert.equal(seenFileDuringCall.address, DRIVING_CITIZEN_WALLET);
  assert.equal(fs.existsSync(seenArgs.walletJsonPath), false, "the transient parent-wallet temp file must be shredded/removed after the attempt");
});

test("defaultSeedChild: the transient temp file is 600-perm while it briefly exists", () => {
  let seenMode;
  defaultSeedChild(
    { childAddress: "0xChildAddrFixture000000000000000000002", amountUsdc: 1, parentWalletAddress: DRIVING_CITIZEN_WALLET },
    {
      resolveParentPrivateKey: () => "0xplaceholderparentfixturekey2",
      runSeedChild: (childAddr, amount, walletJsonPath) => {
        seenMode = fs.statSync(walletJsonPath).mode & 0o777;
        return "0xseedtxfixture2";
      },
    }
  );
  assert.equal(seenMode, 0o600);
});

test("defaultSeedChild: unresolvable parent private key -> ok:false, never invokes runSeedChild at all", () => {
  let runSeedChildCalls = 0;
  const result = defaultSeedChild(
    { childAddress: "0xChildAddrFixture000000000000000000003", amountUsdc: 1, parentWalletAddress: DRIVING_CITIZEN_WALLET },
    {
      resolveParentPrivateKey: () => null,
      runSeedChild: () => { runSeedChildCalls += 1; return "0xshouldneverhappen"; },
    }
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /parent private key unresolvable/);
  assert.equal(runSeedChildCalls, 0);
});

test("defaultSeedChild: seed-child.py's own real failure (exit 1, e.g. tx reverted) fails cleanly, temp file still shredded", () => {
  let seenPath;
  const result = defaultSeedChild(
    { childAddress: "0xChildAddrFixture000000000000000000004", amountUsdc: 1, parentWalletAddress: DRIVING_CITIZEN_WALLET },
    {
      resolveParentPrivateKey: () => "0xplaceholderparentfixturekey4",
      runSeedChild: (childAddr, amount, walletJsonPath) => {
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
  assert.equal(fs.existsSync(seenPath), false);
});

test("defaultSeedChild: seed-child.py's own documented PENDING (exit 2, receipt timed out) is treated as a clean failure, never a fabricated success -- the PENDING hash is preserved in the error for later reconciliation", () => {
  const result = defaultSeedChild(
    { childAddress: "0xChildAddrFixture000000000000000000005", amountUsdc: 1, parentWalletAddress: DRIVING_CITIZEN_WALLET },
    {
      resolveParentPrivateKey: () => "0xplaceholderparentfixturekey5",
      runSeedChild: () => {
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
});

test("defaultSeedChild: seed-child.py prints no tx hash at all -> ok:false, never a fabricated success", () => {
  const result = defaultSeedChild(
    { childAddress: "0xChildAddrFixture000000000000000000006", amountUsdc: 1, parentWalletAddress: DRIVING_CITIZEN_WALLET },
    {
      resolveParentPrivateKey: () => "0xplaceholderparentfixturekey6",
      runSeedChild: () => "   \n",
    }
  );
  assert.equal(result.ok, false);
  assert.match(result.error, /no tx hash/);
});

// ---------------------------------------------------------------------------
// Integration: executeSpawnAttempt's real wiring of the two new steps (mirrors PROP-307c's own
// step-failure-shape discipline -- both new steps run BEFORE REQ-204/step 6 completes the identity
// anchor, so a failure in either must use the minimal business fields plus the lifecycle-receipt
// envelope, never buildChildSpec's fields).
// ---------------------------------------------------------------------------

function assertMinimalFailedRow(row) {
  assert.equal(row.status, "failed");
  assert.equal(typeof row.child_id, "string");
  assert.equal(typeof row.attempted_ms, "number");
  assert.equal(typeof row.error, "string");
  assert.ok(row.error.length > 0);
  assert.deepEqual(Object.keys(row).sort(), [
    "attempted_ms", "child_id", "citizen_id", "error", "event_type",
    "lifecycle_status", "occurred_at_ms", "receipt_id", "schema", "status",
  ]);
}

test("executeSpawnAttempt: persistChildWallet failure (PROP-201c/FIND-007b) -> minimal direct-append row, identity anchor never reached", async () => {
  const dir = tmpDir();
  let registerIdentityCalls = 0;
  const deps = happyDeps(dir, {
    persistChildWallet: async () => ({ ok: false, error: "child wallet.json persistence failed: EACCES" }),
    registerIdentity: async () => { registerIdentityCalls += 1; return { ok: true, agentId: "1", txHash: "0xtx" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.match(result.error, /child wallet\.json persistence failed/);
  assert.equal(registerIdentityCalls, 0, "REQ-204 registration must never fire after an earlier step fails");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assertMinimalFailedRow(rows[0]);
});

test("executeSpawnAttempt: seedChild failure (REQ-204 edge case/FIND-006) -> minimal direct-append row, REQ-204 registration never fires without a landed gas seed", async () => {
  const dir = tmpDir();
  let registerIdentityCalls = 0;
  const deps = happyDeps(dir, {
    seedChild: async () => ({ ok: false, error: "REQ-204 gas-seed: seed-child.py failed: tx reverted" }),
    registerIdentity: async () => { registerIdentityCalls += 1; return { ok: true, agentId: "1", txHash: "0xtx" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "failed");
  assert.match(result.error, /gas-seed/);
  assert.equal(registerIdentityCalls, 0, "register() must never be attempted when the child's fresh wallet was never actually gas-seeded");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assertMinimalFailedRow(rows[0]);
});

test("executeSpawnAttempt: persistChildWallet is invoked with step-1's own homeDir and step-2's own freshly-generated evmWallet, never re-derived values", async () => {
  const dir = tmpDir();
  let seen;
  const deps = happyDeps(dir, {
    persistChildWallet: async (args) => { seen = args; return { ok: true, walletPath: "/fixture/unused" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active");
  assert.equal(seen.childHomeDir, path.join(dir, "child-home"));
  assert.equal(seen.evmWallet.address, "0xChildEvmFixture0000000000000000000000001");
  assert.equal(seen.evmWallet.privateKey, "0xplaceholderchildevmfixturekey");
});

test("executeSpawnAttempt: seedChild's childAddress is step-2's own generated address and amountUsdc is IDENTICAL (by construction) to the eventual active row's seed_usdc field (resolves FIND-007a)", async () => {
  const dir = tmpDir();
  let seenAmount;
  let seenChildAddress;
  const deps = happyDeps(dir, {
    seedChild: async ({ childAddress, amountUsdc }) => {
      seenChildAddress = childAddress;
      seenAmount = amountUsdc;
      return { ok: true, txHash: "0xseedtxfixture" };
    },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active");
  assert.equal(seenChildAddress, "0xChildEvmFixture0000000000000000000000001");
  const rows = readLedgerRows(deps.ledgerFile).filter((r) => r.child_id === result.childId);
  assert.equal(rows.length, 1);
  assert.equal(typeof seenAmount, "number");
  assert.equal(rows[0].seed_usdc, seenAmount, "REQ-206: seedUsdc must be the identical value actually passed to the real gas-seed transfer, never an independently-computed number");
});

test("executeSpawnAttempt: seedChild fires AFTER deploy and BEFORE registerIdentity (REQ-204's own ordering requirement -- register() would revert on a genuinely unseeded wallet)", async () => {
  const dir = tmpDir();
  const order = [];
  const deps = happyDeps(dir, {
    deploy: async () => { order.push("deploy"); return { ok: true, leaseId: "dseq-1", shelterCostUsd: 0.5 }; },
    seedChild: async () => { order.push("seedChild"); return { ok: true, txHash: "0xseedtxfixture" }; },
    registerIdentity: async () => { order.push("registerIdentity"); return { ok: true, agentId: "1", txHash: "0xtx" }; },
  });
  const result = await executeSpawnAttempt(baseParams(), deps);
  assert.equal(result.status, "active");
  assert.deepEqual(order, ["deploy", "seedChild", "registerIdentity"]);
});
