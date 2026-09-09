// REQ-307: the spawn attempt's single entry-point function. Pure sequencing and error propagation
// over the already-specified REQ-201-206/301-306 building blocks -- this module makes zero
// eligibility/threshold decisions of its own (that decision is made entirely by this function's own
// caller, before it is ever invoked).
import fs from "node:fs";
import { promises as fsp } from "node:fs";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { privateKeyToAccount } from "viem/accounts";

import { withGigLock } from "../../../economy/gig/lib/lock.mjs";
import { ensureAgentId } from "../../../economy/gig/lib/ensure-agent-id.mjs";
import { isSelfFunded, selfFundedReasons } from "../../../_shared/lib/is-self-funded.mjs";
import { resolveEvmPrivateKey } from "../../../earn/lib/resolve-identity.mjs";
import { CITIZENS_REGISTRY_PATH } from "./registry-path.mjs";
import { resolveStateDir } from "./state-path.js";
import { readChildren, appendCitizenLifecycleReceipt } from "./ledger.js";
import { appendShelterCostEntry } from "./shelter-cost-ledger.js";
import {
  readPendingRegistryAppends,
  queuePendingRegistryAppend,
  resolvePendingRegistryAppend,
  deriveOutstandingRegistryAppends,
} from "./pending-registry-append.js";
import { nextChildId, buildChildSpec } from "./child-spec.js";
import { needsSolanaWallet } from "./needs-solana-wallet.mjs";
import { selectCloudTarget as selectCloudTargetPure } from "./cloud-target.mjs";
import { evaluateAkashFundingGate } from "./akash-funding-gate.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const GENESIS_PATH = path.join(__dirname, "..", "..", "..", "..", "identity", "genesis.md");
const GEN_WALLET_SCRIPT = path.join(__dirname, "..", "scripts", "gen-wallet.sh");
const GEN_SOLANA_WALLET_SCRIPT = path.join(__dirname, "..", "scripts", "gen-solana-wallet.sh");
const DEPLOY_AKASH_SCRIPT = path.join(__dirname, "..", "scripts", "deploy-akash.sh");
const AKT_TREASURY_SCRIPT = path.join(__dirname, "..", "scripts", "akt-treasury.sh");
const SEED_CHILD_SCRIPT = path.join(__dirname, "..", "scripts", "seed-child.py");
const SPAWN_CHILD_CONFIG_PATH = path.join(__dirname, "..", "..", "spawn-child", "config.json");

// REQ-206: fixed at 1 for every child this feature produces -- colony-treasury-funded spawns are,
// by design, top-level, non-lineage children (spawn chaining is out of scope this increment).
const GENERATION = 1;
// REQ-401: the exclusive $0-bootstrap fuel provider every child this feature produces carries.
const FUEL_PROVIDER = "free-model";

function constitutionHash() {
  return crypto.createHash("sha256").update(fs.readFileSync(GENESIS_PATH)).digest("hex");
}

// REQ-304: spawn-child/config.json's own real, already-tuned costAkt/bufferAkt values -- never
// hardcoded here a second time (mirrors akt-treasury.sh's own TREASURY_MINT_UAKT/ACT_BUFFER_UACT
// citing this SAME file's _notes field as their source of truth).
function spawnChildConfig() {
  return JSON.parse(fs.readFileSync(SPAWN_CHILD_CONFIG_PATH, "utf8"));
}

// REQ-206: identical to REQ-204's actual gas-seed transfer amount, never a second, independently
// derived quantity -- reuses the SAME env-override convention this codebase's own existing
// gas-seed-transfer call site already uses.
function seedUsdcAmount() {
  return Number(process.env.ANICCA_SEED_USDC || 1);
}

function defaultLedgerFile() {
  return path.join(resolveStateDir({}), "children.jsonl");
}

// REQ-305 edge case (FIND-003): the durable queue a transient registry-append failure is recorded to,
// and retryPendingRegistryAppends (below) reads from -- SAME state dir as children.jsonl/citizens.json,
// never a second, differently-rooted location.
function defaultPendingRegistryAppendsFile() {
  return path.join(resolveStateDir({}), "pending-registry-appends.jsonl");
}

function errorMessage(e) {
  return (e && e.message) || String(e);
}

// `extra` (FIND-001) carries the gas-seed reclaim outcome (`reclaimed`/`reclaimTxHash`/`reclaimError`)
// and `lease_id` onto a failure row, when-and-only-when the caller actually attempted a reclaim for
// THIS failure -- omitted entirely (never `undefined`-valued keys) when no reclaim ever applied.
function appendMinimalFailure(ledgerFile, { childId, attemptedMs, error, extra = {} }) {
  appendCitizenLifecycleReceipt(ledgerFile, { child_id: childId, status: "failed", attempted_ms: attemptedMs, error: errorMessage(error), ...extra });
}

function appendSpecFailure(ledgerFile, spec, { attemptedMs, error, extra = {} }) {
  appendCitizenLifecycleReceipt(ledgerFile, { ...spec, status: "failed", attempted_ms: attemptedMs, error: errorMessage(error), ...extra });
}

// Every REQ-201/202/203/204/205/306/302/303 call site below shares the exact same shape: run a
// deps-or-default effectful step, and on either a thrown error OR (for the "ok"-shaped results)
// result.ok === false, record a failure row and signal the caller to return early. This helper is
// the single place that shape lives; `onFailure` lets step 7 (REQ-205) redirect the failure row to
// appendSpecFailure's buildChildSpec-based shape instead of the default minimal one, without
// duplicating the try/catch/requireOk logic itself.
// `onFailure` may itself be async (FIND-001's reclaim attempt needs to run and complete BEFORE the
// failure row is written) -- always awaited here so the ledger write is guaranteed to land before this
// step (and therefore executeSpawnAttempt itself) returns; awaiting a synchronous `onFailure`'s plain
// return value is a no-op, so every pre-existing synchronous `onFailure` caller is unaffected.
async function runStep({ ledgerFile, childId, nowMs, run, requireOk = false, defaultErrorMessage, onFailure }) {
  const recordFailure = onFailure || ((error) => appendMinimalFailure(ledgerFile, { childId, attemptedMs: nowMs, error }));
  let result;
  try {
    result = await run();
  } catch (e) {
    const error = errorMessage(e);
    await recordFailure(error);
    return { failed: true, error };
  }
  if (requireOk && (!result || !result.ok)) {
    const error = (result && result.error) || defaultErrorMessage;
    await recordFailure(error);
    return { failed: true, error };
  }
  return { failed: false, value: result };
}

// Money-safety fix (Phase-5 finding: crash-window double-append -- see
// verification/proof-harnesses/finding-money-safety-registry-retry-crash-window.mjs in the
// anicca-agent-spawn feature ledger): retryPendingRegistryAppends' own two-step sequence --
// (1) await append(citizens_registry_file, citizen_record) then (2) resolvePendingRegistryAppend --
// is NOT atomic. A process crash/SIGKILL/OOM-kill between (1) landing and (2) marking the queue entry
// resolved leaves that entry still "pending", so the NEXT wake's retry re-drives the SAME entry and
// calls THIS function a second time for the SAME record. Making the append idempotent by id closes the
// gap at its source: a re-drive becomes a genuine no-op instead of a duplicate row, so
// computeColonySurplusUsd/computePerCitizenSurplusUsd (treasury-gate.mjs) can never double-count that
// citizen's balance into a real-money spawn-eligibility decision. Applies identically to BOTH call
// sites (the first-attempt append at step 9, below, and every retryPendingRegistryAppends re-drive) --
// there is only ever one appendCitizenRecord implementation, never a second, independently-written one.
async function appendCitizenRecord(citizensRegistryFile, record) {
  let records = [];
  try {
    records = JSON.parse(await fsp.readFile(citizensRegistryFile, "utf8"));
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  if (records.some((r) => r && r.id === record.id)) return; // already present -- re-drive is a no-op
  records.push(record);
  await fsp.mkdir(path.dirname(citizensRegistryFile), { recursive: true });
  await fsp.writeFile(citizensRegistryFile, JSON.stringify(records, null, 2));
}

// --- Production default wiring (used only when a caller omits the corresponding `deps` override). ---

async function defaultCheckHomeDistinct({ childId, citizensRegistryFile }) {
  const homeDir = path.join(os.homedir(), ".anicca-children", childId);
  let citizens = [];
  try {
    citizens = JSON.parse(fs.readFileSync(citizensRegistryFile, "utf8"));
  } catch {
    // no registry yet -- nothing to collide with
  }
  const collides = citizens.some(
    (c) =>
      c &&
      typeof c.homeDir === "string" &&
      (c.homeDir === homeDir || homeDir.startsWith(c.homeDir + path.sep) || c.homeDir.startsWith(homeDir + path.sep))
  );
  if (collides) return { ok: false, error: `home collides with an existing citizen: ${homeDir}` };
  return { ok: true, homeDir };
}

// REQ-201's own hard SHALL: verify the generated address is a real keccak256-derived Ethereum
// address, not gen-wallet.sh's own documented sha256 fallback (fired when the host lacks a real
// keccak implementation -- its own comment: "not a real eth addr; smoke-test will warn"). Detected by
// independently re-deriving the address from the SAME private key via viem's privateKeyToAccount -- a
// SECOND, INDEPENDENT keccak implementation from gen-wallet.sh's own openssl+python path (matches
// REQ-201's Acceptance Criteria: "the address independently re-derives ... under a second, independent
// keccak implementation"). A sha256-fallback address structurally cannot match this re-derivation.
export function assertRealEvmAddress({ address, privateKey }) {
  const rederived = privateKeyToAccount(privateKey).address;
  if (rederived.toLowerCase() !== String(address).toLowerCase()) {
    throw new Error(
      `gen-wallet.sh address fails independent keccak re-derivation (got ${address}, expected ${rederived}) -- this is the documented sha256 fallback, not a real Ethereum address (REQ-201)`
    );
  }
}

function defaultGenerateEvmWallet() {
  const out = execFileSync("bash", [GEN_WALLET_SCRIPT], { encoding: "utf8" });
  const { address, private_key: privateKey } = JSON.parse(out);
  assertRealEvmAddress({ address, privateKey });
  return { address, privateKey };
}

function defaultGenerateSolanaWallet() {
  const out = execFileSync("bash", [GEN_SOLANA_WALLET_SCRIPT], { encoding: "utf8" });
  const { address, private_key: privateKey } = JSON.parse(out);
  return { address, privateKey };
}

// PROP-201c (resolves round-3 contract-review FIND-007b): persist the child's own freshly-generated
// EVM wallet to its OWN isolated home, in the EXACT {address, privateKey} shape
// resolve-identity.mjs::resolveEvmPrivateKey already reads (`${effectiveHome}/.automaton/wallet.json`,
// field `privateKey`) -- verified to be the SAME real shape this host's own existing citizens' wallet
// files already use (`~/.blockrun/.automaton/wallet.json`'s own real keys are exactly
// ["address","privateKey"]). Without this write the child has no way to resolve its own signing key on
// any of ITS OWN future wake cycles -- defaultGenerateEvmWallet above only ever returns the pair
// in-memory, it is never otherwise touched to disk.
export function defaultPersistChildWallet({ childHomeDir, evmWallet }) {
  try {
    const automatonDir = path.join(childHomeDir, ".automaton");
    fs.mkdirSync(automatonDir, { recursive: true });
    const walletPath = path.join(automatonDir, "wallet.json");
    fs.writeFileSync(walletPath, JSON.stringify({ address: evmWallet.address, privateKey: evmWallet.privateKey }), { mode: 0o600 });
    fs.chmodSync(walletPath, 0o600); // belt-and-suspenders against a wider process umask
    return { ok: true, walletPath };
  } catch (e) {
    return { ok: false, error: `child wallet.json persistence failed: ${errorMessage(e)}` };
  }
}

// Honest limitation (mirrors this spec's own established honesty precedent for genuinely-missing
// infra): no live NOS/AKT spot-price feed and no Nosana deploy path exist anywhere in this codebase
// yet -- deploy-akash.sh is the only proven cloud-deploy path today, so it is the only target this
// default ever selects, never a fabricated price comparison against a target this repo cannot
// actually deploy to. selectCloudTargetPure's own deterministic rule still governs the decision.
async function defaultSelectCloudTarget() {
  return selectCloudTargetPure({ nosanaAvailable: false, akashAvailable: true, akashPriceUsd: 0 });
}

// REQ-304: real AKT balance query for the configured Akash signing wallet -- mirrors akt-treasury.sh's
// own existing balance() helper verbatim (same `akash query bank balances` shape, uakt -> AKT via
// /1e6), never a second, independently-invented query mechanism. Fails closed to 0 (never throws),
// exactly like evaluateAkashFundingGate's own established fail-closed discipline for its inputs.
function defaultQueryAktBalance() {
  const akashCli = process.env.AKASH_CLI || "akash";
  const keyName = process.env.AKASH_KEY_NAME;
  if (!keyName) return 0;
  try {
    const addr = execFileSync(akashCli, ["keys", "show", keyName, "-a"], { encoding: "utf8" }).trim();
    const out = execFileSync(akashCli, ["query", "bank", "balances", addr, "-o", "json"], { encoding: "utf8" });
    const balances = JSON.parse(out).balances || [];
    const uakt = balances.find((b) => b.denom === "uakt");
    return uakt ? Number(uakt.amount) / 1e6 : 0;
  } catch {
    return 0;
  }
}

// REQ-304's own funding bridge: the ALREADY-BUILT akt-treasury.sh (AKT -> ACT mint, optionally
// preceded by a USDC->AKT swap when TREASURY_SWAP_CMD is configured) -- reused as-is, never
// re-implemented. Fail-soft: evaluateAkashFundingGate's own second pass re-queries the balance
// regardless of whether this attempt actually landed funds.
function defaultAttemptAktBridge() {
  try {
    execFileSync("bash", [AKT_TREASURY_SCRIPT], { encoding: "utf8" });
  } catch {
    // fail-soft -- the second computeSpawnGate pass (evaluateAkashFundingGate's own sequencing)
    // is what actually determines deploy readiness, not this attempt's own outcome.
  }
}

// REQ-304 (Round 2 additions, resolves contract-review round-2 FIND-001/002): before ANY Akash deploy
// attempt, evaluate akash-funding-gate.mjs's own two-pass evaluateAkashFundingGate against
// spawn-child/config.json's real spawn_cost_akt/buffer_akt -- never invoking deploy-akash.sh at all
// when the gate is not ready after both passes. `fundingDeps` is a test/production wiring seam
// (mirrors executeSpawnAttempt's own `deps` convention) for the funding gate's own queryBalanceAkt/
// attemptBridge and for the deploy-akash.sh invocation itself; omitting any key wires the real
// production implementation directly. Exported (unlike the other `default*` steps) so this funding-gate
// wiring is directly unit-testable, mirroring evaluateAkashFundingGate's own test discipline.
// FIND-002/PROP-303c: converts the winning bid's own settled price (deploy-akash.sh's new JSON output,
// `{dseq,priceAmount,priceDenom}`) into USD. AEP-76's own protocol design pegs the escrow denom
// EXACTLY: "uact = 1e-6 USD" (AEP-76 DESIGN.md, cited verbatim at
// docs/superpowers/specs/2026-06-26-B1-akash-provider-services-acceleration.md:21, and reused by this
// SAME script's own SDL pricing/`--deposit` denom, deploy-akash.sh line 44) -- so a `uact`-priced bid
// converts to USD by a FIXED division, never a fetched spot price. AKT (`uakt`) is a DIFFERENT,
// non-pegged token (the bme mint rate AKT->ACT is ~0.66, spawn-child/config.json's own `_notes` field) --
// pricing a `uact` amount against an AKT/USD quote would price the WRONG asset entirely, so this fails
// closed to `null` (never a fabricated number) for any denom other than the one the protocol actually
// pegs to USD. Exported so this pure conversion is directly unit-testable.
export function shelterCostUsdFromSettledPrice({ priceAmount, priceDenom } = {}) {
  if (priceDenom !== "uact") return null;
  const amount = Number(priceAmount);
  return Number.isFinite(amount) && amount >= 0 ? amount / 1e6 : null;
}

export async function defaultDeploy({ target, childId }, fundingDeps = {}) {
  if (target !== "akash") {
    return { ok: false, error: `no deploy path implemented for cloud target "${target}"` };
  }
  const { spawn_cost_akt: costAkt, buffer_akt: bufferAkt } = spawnChildConfig();
  const gate = await evaluateAkashFundingGate({
    queryBalanceAkt: fundingDeps.queryBalanceAkt || (async () => defaultQueryAktBalance()),
    costAkt,
    bufferAkt,
    attemptBridge: fundingDeps.attemptBridge || (async () => defaultAttemptAktBridge()),
  });
  if (!gate.ready) {
    return {
      ok: false,
      error: `REQ-304 funding gate not ready: ${gate.reason} (shortfall ${gate.shortfallAkt} AKT of ${gate.thresholdAkt} AKT threshold)`,
    };
  }
  let stdout;
  try {
    stdout = fundingDeps.runDeployAkash
      ? await fundingDeps.runDeployAkash(childId)
      : execFileSync("bash", [DEPLOY_AKASH_SCRIPT, childId], { encoding: "utf8" }).trim();
  } catch (e) {
    return { ok: false, error: `deploy-akash.sh failed: ${errorMessage(e)}` };
  }
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    return { ok: false, error: "deploy-akash.sh produced invalid output" };
  }
  const { dseq: leaseId, priceAmount, priceDenom } = parsed || {};
  if (!leaseId) return { ok: false, error: "deploy-akash.sh produced no lease id" };
  return { ok: true, leaseId, shelterCostUsd: shelterCostUsdFromSettledPrice({ priceAmount, priceDenom }) };
}

function defaultRunSeedChild(childAddr, amount, walletJsonPath) {
  return execFileSync("python3", [SEED_CHILD_SCRIPT, childAddr, String(amount), walletJsonPath], { encoding: "utf8" });
}

// Mirrors this skill's own retired shell provisioning wrapper's proven `trap 'shred -u "$WALLET_TMP"
// ... || rm -f "$WALLET_TMP"' EXIT` discipline for a transient private-key file: best-effort
// secure-delete, unconditional plain unlink fallback. Never throws (a cleanup failure must never mask
// the real result of the seed attempt).
function shredTempFile(filePath) {
  try {
    execFileSync("shred", ["-u", filePath], { stdio: "ignore" });
  } catch {
    try {
      fs.unlinkSync(filePath);
    } catch {
      // best-effort -- the file is 600-perm under this host's own state dir, not a public path
    }
  }
}

// REQ-204's own Edge Cases text (behavioral-spec.md): "register() reverts for insufficient gas (the
// child's fresh wallet starts at exactly 0 ETH): THE SYSTEM SHALL fund it with a ONE-TIME, minimal gas
// seed transferred from a self-funded citizen's own wallet ... the SAME class of transfer SPEC.md
// Section9.6 already performed and evidenced on-chain". Since REQ-201's freshly-generated child wallet
// ALWAYS starts at 0 ETH, this is the wallet's baseline state on every real spawn attempt -- not a rare
// edge case -- so it must land before step 6 (REQ-204 registration) on every real invocation. Reuses the
// ALREADY-BUILT, already-tested scripts/seed-child.py (a real Base USDC transfer, fail-closed,
// PENDING-on-receipt-timeout honest-ledger discipline) UNMODIFIED, rather than re-implementing the
// transfer in JS (resolves contract-review round-3 FIND-006). `seedDeps` mirrors defaultDeploy's own
// `fundingDeps` seam (resolveParentPrivateKey/runSeedChild); omitting a key wires the real
// resolveEvmPrivateKey()/seed-child.py invocation directly. Exported (like defaultDeploy) so this is
// directly unit-testable in isolation. The parent's private key is resolved via the SAME
// resolve-identity.mjs::resolveEvmPrivateKey() wake-gate.mjs already uses to derive `drivingCitizenWallet`
// itself -- never a second, independently-derived identity mechanism -- and is written ONLY to a
// transient, 600-perm temp file under this host's own resolveStateDir() (never bare /tmp, mirroring
// this skill's own retired shell provisioning wrapper), shredded in a `finally` immediately after the
// transfer attempt completes (mirrors REQ-301's own transient-key-file discipline).
export function defaultSeedChild({ childAddress, amountUsdc, parentWalletAddress }, seedDeps = {}) {
  const resolveParentPrivateKey = seedDeps.resolveParentPrivateKey || (() => resolveEvmPrivateKey({}));
  const runSeedChild = seedDeps.runSeedChild || defaultRunSeedChild;

  const parentPrivateKey = resolveParentPrivateKey();
  if (!parentPrivateKey) {
    return { ok: false, error: "REQ-204 gas-seed: parent private key unresolvable (resolveEvmPrivateKey returned null)" };
  }

  const stateDir = resolveStateDir({});
  fs.mkdirSync(stateDir, { recursive: true });
  const tmpWalletPath = path.join(stateDir, `.tmp-parentwallet-${crypto.randomBytes(6).toString("hex")}.json`);
  // seed-child.py's own documented shape: {"private_key": "0x...", "address": "0x..."} (snake_case) --
  // a DIFFERENT, python-only convention from resolve-identity.mjs's camelCase {address,privateKey};
  // never conflate the two.
  fs.writeFileSync(tmpWalletPath, JSON.stringify({ private_key: parentPrivateKey, address: parentWalletAddress }), { mode: 0o600 });
  fs.chmodSync(tmpWalletPath, 0o600);
  try {
    const out = runSeedChild(childAddress, amountUsdc, tmpWalletPath);
    const txHash = String(out || "").trim();
    if (!txHash) return { ok: false, error: "REQ-204 gas-seed: seed-child.py produced no tx hash" };
    return { ok: true, txHash };
  } catch (e) {
    // seed-child.py's own documented exit 2: broadcast but the receipt timed out -- record the PENDING
    // hash in the failure reason (never silently re-attempt a fresh transfer, per its own
    // no-double-spend discipline) rather than fabricate a success.
    if (e && e.status === 2) {
      const pending = /PENDING\s+(0x[0-9a-fA-F]+)/.exec(String(e.stderr || ""));
      return {
        ok: false,
        error: `REQ-204 gas-seed: tx broadcast but receipt timed out -- ${pending ? pending[0] : "PENDING (hash unavailable)"}`,
      };
    }
    return { ok: false, error: `REQ-204 gas-seed: seed-child.py failed: ${errorMessage(e)}` };
  } finally {
    shredTempFile(tmpWalletPath);
  }
}

// FIND-001 (money-safety gap): a best-effort reclaim of the REQ-204 gas seed, fired ONLY when the
// SAME attempt's own seedStep already landed the transfer and a LATER step then fails -- without this,
// the seeded USDC is permanently stranded in the abandoned child wallet, since the NEXT spawn attempt
// always mints a brand-new child_id/wallet (child-spec.js::nextChildId is monotonic, never revisits an
// old attempt). Reuses scripts/seed-child.py UNMODIFIED, exactly as defaultSeedChild already does, with
// sender and recipient reversed: seed-child.py's own {"private_key","address"} wallet-json argument is
// the SIGNER (the child's own in-memory evmWallet, never re-read from disk), and its destination
// argument is the driving citizen's wallet -- the SAME script working in either direction because it
// only ever reads a sender wallet-json and sends to an arbitrary destination address. `seedDeps` mirrors
// defaultSeedChild's own `runSeedChild` override seam. Exported (like defaultSeedChild) so this is
// directly unit-testable in isolation.
export function defaultReclaimSeed({ childEvmWallet, parentWalletAddress, amountUsdc }, seedDeps = {}) {
  const runSeedChild = seedDeps.runSeedChild || defaultRunSeedChild;

  const stateDir = resolveStateDir({});
  fs.mkdirSync(stateDir, { recursive: true });
  const tmpWalletPath = path.join(stateDir, `.tmp-reclaimwallet-${crypto.randomBytes(6).toString("hex")}.json`);
  fs.writeFileSync(
    tmpWalletPath,
    JSON.stringify({ private_key: childEvmWallet.privateKey, address: childEvmWallet.address }),
    { mode: 0o600 }
  );
  fs.chmodSync(tmpWalletPath, 0o600);
  try {
    const out = runSeedChild(parentWalletAddress, amountUsdc, tmpWalletPath);
    const txHash = String(out || "").trim();
    if (!txHash) return { ok: false, error: "reclaim: seed-child.py produced no tx hash" };
    return { ok: true, txHash };
  } catch (e) {
    if (e && e.status === 2) {
      const pending = /PENDING\s+(0x[0-9a-fA-F]+)/.exec(String(e.stderr || ""));
      return {
        ok: false,
        error: `reclaim: tx broadcast but receipt timed out -- ${pending ? pending[0] : "PENDING (hash unavailable)"}`,
      };
    }
    return { ok: false, error: `reclaim: seed-child.py failed: ${errorMessage(e)}` };
  } finally {
    shredTempFile(tmpWalletPath);
  }
}

// Runs a reclaim attempt and translates its outcome into the extra ledger-row fields FIND-001 requires
// (`reclaimed`/`reclaimTxHash`/`reclaimError`) -- NEVER throws (any error, including a thrown one, is
// swallowed into `reclaimed:false`) and NEVER masks the original failure this attempt is already
// recording, since this is only ever merged alongside that failure's own `error` field, never in place
// of it. `deps.reclaimSeed` mirrors `deps.seedChild`'s own override-seam convention.
async function attemptSeedReclaim({ deps, childEvmWallet, parentWalletAddress, amountUsdc }) {
  try {
    const result = deps.reclaimSeed
      ? await deps.reclaimSeed({ childEvmWallet, parentWalletAddress, amountUsdc })
      : defaultReclaimSeed({ childEvmWallet, parentWalletAddress, amountUsdc }, deps);
    if (result && result.ok) return { reclaimed: true, reclaimTxHash: result.txHash };
    return { reclaimed: false, reclaimError: (result && result.error) || "reclaim: unknown failure" };
  } catch (e) {
    return { reclaimed: false, reclaimError: errorMessage(e) };
  }
}

async function defaultRegisterIdentity({ childPrivateKey, childHomeDir }) {
  const cacheFile = path.join(childHomeDir, ".automaton", "gig-agent-id.json");
  const result = await ensureAgentId({ privateKey: childPrivateKey, cacheFile });
  if (!result.ok) return { ok: false, error: result.reason };
  return { ok: true, agentId: result.agentId };
}

function defaultWriteMcpConfig({ childHomeDir }) {
  const gigStatePath = path.join(childHomeDir, ".anicca-signing", "gig-board", "state", "gigs.json");
  const mcpConfig = {
    mcpServers: {
      "anicca-gig": {
        transport: "stdio",
        command: process.execPath,
        args: [path.join(childHomeDir, "skills", "economy", "gig", "mcp-server.mjs")],
        env: {
          GIG_FACILITATOR_URL: process.env.GIG_FACILITATOR_URL || "http://127.0.0.1:8407",
          GIG_STATE_PATH: gigStatePath,
          GIG_CHAIN: process.env.GIG_CHAIN || "base",
        },
      },
    },
  };
  const mcpPath = path.join(childHomeDir, "mcp.json");
  fs.mkdirSync(path.dirname(mcpPath), { recursive: true });
  fs.writeFileSync(mcpPath, JSON.stringify(mcpConfig, null, 2));
  return { ok: true };
}

/**
 * executeSpawnAttempt -- the single call-graph root for a spawn attempt, run under the "colony-spawn"
 * lock for its entire duration. `deps` is a test/production wiring seam for the 9 genuinely effectful
 * steps this sprint wires (the original 7 -- REQ-203's home check, REQ-201's EVM keygen, REQ-306's
 * cloud-target selection, REQ-202's Solana keygen, REQ-302/303's deploy, REQ-204's ERC-8004
 * registration, REQ-205's mcp.json write -- plus round-3 contract-review's own persistChildWallet
 * (PROP-201c/FIND-007b) and seedChild (REQ-204 edge case/FIND-006)); omitting it wires the real
 * scripts/modules above directly.
 *
 * @returns {Promise<{status: "active"|"failed", childId: string|null, error?: string}>}
 */
export async function executeSpawnAttempt({ initialSkills = [], drivingCitizenWallet, nowMs = Date.now() } = {}, deps = {}) {
  const lockStatePath = deps.lockStatePath || CITIZENS_REGISTRY_PATH;
  const ledgerFile = deps.ledgerFile || defaultLedgerFile();
  const citizensRegistryFile = deps.citizensRegistryFile || CITIZENS_REGISTRY_PATH;

  const runAttempt = async () => {
    const childId = nextChildId(readChildren(ledgerFile), "anicca-c");

    // Step 1 (REQ-203): HOME/ANICCA_HOME distinctness, before any key generation.
    const homeStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () =>
        deps.checkHomeDistinct
          ? deps.checkHomeDistinct({ childId, citizensRegistryFile })
          : defaultCheckHomeDistinct({ childId, citizensRegistryFile }),
      requireOk: true,
      defaultErrorMessage: "home distinctness check failed",
    });
    if (homeStep.failed) return { status: "failed", childId, error: homeStep.error };
    const homeDir = homeStep.value.homeDir;

    // Step 2 (REQ-201): child EVM wallet generation.
    const evmStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () => (deps.generateEvmWallet ? deps.generateEvmWallet() : defaultGenerateEvmWallet()),
    });
    if (evmStep.failed) return { status: "failed", childId, error: evmStep.error };
    const evmWallet = evmStep.value;

    // New step (PROP-201c, resolves round-3 contract-review FIND-007b): persist the child's own EVM
    // wallet to its own isolated home NOW, immediately after generation -- this is the child's own
    // future-wake-cycle identity file (resolve-identity.mjs's own read path), never a second signing
    // path for the REST of THIS attempt (evmWallet.privateKey below stays purely in-memory, unchanged).
    const persistStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () =>
        deps.persistChildWallet
          ? deps.persistChildWallet({ childHomeDir: homeDir, evmWallet })
          : defaultPersistChildWallet({ childHomeDir: homeDir, evmWallet }),
      requireOk: true,
      defaultErrorMessage: "child wallet.json persistence failed",
    });
    if (persistStep.failed) return { status: "failed", childId, error: persistStep.error };

    // Step 3 (REQ-306): a fresh cloud-target selection for THIS attempt.
    const cloudStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () => (deps.selectCloudTarget ? deps.selectCloudTarget() : defaultSelectCloudTarget()),
    });
    if (cloudStep.failed) return { status: "failed", childId, error: cloudStep.error };
    const cloudTarget = cloudStep.value;
    if (cloudTarget === "none") {
      const error = "no cloud target available (neither nosana nor akash)";
      appendMinimalFailure(ledgerFile, { childId, attemptedMs: nowMs, error });
      return { status: "failed", childId, error };
    }

    // Step 4 (REQ-202): conditional Solana wallet generation, fed step 3's own return value directly.
    let solanaWallet = null;
    if (needsSolanaWallet({ initialSkills, deployTarget: cloudTarget })) {
      const solanaStep = await runStep({
        ledgerFile,
        childId,
        nowMs,
        run: () => (deps.generateSolanaWallet ? deps.generateSolanaWallet() : defaultGenerateSolanaWallet()),
      });
      if (solanaStep.failed) return { status: "failed", childId, error: solanaStep.error };
      solanaWallet = solanaStep.value;
    }

    // Step 5 (REQ-302/303): deploy, selected by step 3's own return value.
    const deployStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () =>
        deps.deploy
          ? deps.deploy({ target: cloudTarget, childId, childWallet: evmWallet.address })
          : defaultDeploy({ target: cloudTarget, childId }, deps),
      requireOk: true,
      defaultErrorMessage: "deploy failed",
    });
    if (deployStep.failed) return { status: "failed", childId, error: deployStep.error };
    const { leaseId, shelterCostUsd } = deployStep.value;

    // FIND-002/PROP-303c: append the settled lease cost to the shelter-cost ledger REQ-102's
    // deriveMeasuredShelterCostUsd reads on its NEXT wake evaluation, immediately once it is genuinely
    // observable (right after a successful deploy) -- ONLY when it is a real, finite, positive USD
    // figure. A null/zero reading (the price fetch/denom-mismatch fail-closed path in
    // shelterCostUsdFromSettledPrice) is never appended: deriveMeasuredShelterCostUsd reduces to the
    // LAST-appended entry, so a fabricated `0` row would poison every subsequent wake's
    // spawnThresholdUsd to $0, defeating REQ-102's own safety margin -- skip with a logged warning
    // instead, leaving the provisional MIN_SHELTER_USD default in effect for one more wake.
    if (typeof shelterCostUsd === "number" && Number.isFinite(shelterCostUsd) && shelterCostUsd > 0) {
      try {
        const appendEntry = deps.appendShelterCostEntry || appendShelterCostEntry;
        const shelterCostFile = deps.shelterCostFile || path.join(resolveStateDir({}), "shelter-cost.jsonl");
        appendEntry(shelterCostFile, { ts: nowMs, settledLeaseCostUsd: shelterCostUsd, leaseId });
      } catch (e) {
        console.error(`self/spawn: shelter-cost ledger append failed for ${childId}: ${errorMessage(e)}`);
      }
    } else {
      console.error(`self/spawn: skipping shelter-cost ledger append for ${childId} -- no real settled price available`);
    }

    // New step (REQ-204 Edge Cases, resolves round-3 contract-review FIND-006/FIND-007a): a ONE-TIME
    // minimal gas seed from the driving (self-funded) citizen's OWN wallet. REQ-201's freshly-generated
    // child wallet always starts at 0 ETH, so the register() call in the very next step would revert on
    // every real invocation without this landing first. `seedUsdc` is computed exactly ONCE and reused
    // verbatim for buildChildSpec's own `seedUsdc` field below (REQ-206: the two MUST be identical "by
    // construction, never merely close" -- never two independently-computed reads of seedUsdcAmount()).
    const seedUsdc = seedUsdcAmount();
    const seedStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () =>
        deps.seedChild
          ? deps.seedChild({ childAddress: evmWallet.address, amountUsdc: seedUsdc, parentWalletAddress: drivingCitizenWallet })
          : defaultSeedChild({ childAddress: evmWallet.address, amountUsdc: seedUsdc, parentWalletAddress: drivingCitizenWallet }, deps),
      requireOk: true,
      defaultErrorMessage: "REQ-204 gas-seed transfer failed",
    });
    if (seedStep.failed) return { status: "failed", childId, error: seedStep.error };

    // Step 6 (REQ-204): ERC-8004 registration -- the second half of the identity anchor. The gas seed
    // (seedStep, above) already landed by this point -- a failure here is exactly FIND-001's
    // reclaim-trigger case, so `onFailure` attempts the reclaim BEFORE writing the failure row, and
    // records the outcome (`reclaimed`/`reclaimTxHash`/`reclaimError`) plus `leaseId` alongside it,
    // never in place of the real registerIdentity error.
    const identityStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () =>
        deps.registerIdentity
          ? deps.registerIdentity({ childPrivateKey: evmWallet.privateKey })
          : defaultRegisterIdentity({ childPrivateKey: evmWallet.privateKey, childHomeDir: homeDir }),
      requireOk: true,
      defaultErrorMessage: "identity registration failed",
      onFailure: async (error) => {
        const reclaim = await attemptSeedReclaim({ deps, childEvmWallet: evmWallet, parentWalletAddress: drivingCitizenWallet, amountUsdc: seedUsdc });
        appendMinimalFailure(ledgerFile, { childId, attemptedMs: nowMs, error, extra: { lease_id: leaseId, ...reclaim } });
      },
    });
    if (identityStep.failed) return { status: "failed", childId, error: identityStep.error };
    const identityResult = identityStep.value;

    // The identity anchor is now complete -- every failure from here on uses the buildChildSpec-based
    // recording path (REQ-305), never the minimal direct-append row above.
    let spec;
    try {
      spec = {
        ...buildChildSpec({
          childId,
          parentWallet: drivingCitizenWallet,
          childWallet: evmWallet.address,
          generation: GENERATION,
          seedUsdc,
          constitutionHash: constitutionHash(),
          agentEvmAddress: evmWallet.address,
          agentId: identityResult.agentId,
        }),
        // FIND-002/PROP-303c: buildChildSpec's own schema has no leaseId field (a fixed, validated
        // shape) -- attached here, the minimal-change way, so REQ-402's own documented "operator looks
        // up dseq from this feature's own state" reconciliation mitigation actually has a lease_id to
        // read, on both the final active row (below) and every failure row from here on (which all
        // spread this SAME `spec`).
        lease_id: leaseId,
      };
    } catch (e) {
      // buildChildSpec's own real, untouched assertion fired -- the identity anchor is complete, so
      // this is recorded via a buildChildSpec-shaped row even though buildChildSpec itself never
      // returned one. The gas seed already landed by this point -- FIND-001's reclaim-trigger case.
      const reclaim = await attemptSeedReclaim({ deps, childEvmWallet: evmWallet, parentWalletAddress: drivingCitizenWallet, amountUsdc: seedUsdc });
      appendCitizenLifecycleReceipt(ledgerFile, {
        child_id: childId,
        wallet: evmWallet.address,
        parent_wallet: drivingCitizenWallet,
        generation: GENERATION,
        agent_evm_address: evmWallet.address,
        agent_id: identityResult.agentId,
        lease_id: leaseId,
        status: "failed",
        attempted_ms: nowMs,
        error: errorMessage(e),
        ...reclaim,
      });
      return { status: "failed", childId, error: errorMessage(e) };
    }

    // Step 7 (REQ-205): mcp.json write. The identity anchor is already complete here, so a failure
    // is recorded via appendSpecFailure's buildChildSpec-shaped row, never the minimal one above.
    const mcpStep = await runStep({
      ledgerFile,
      childId,
      nowMs,
      run: () => (deps.writeMcpConfig ? deps.writeMcpConfig() : defaultWriteMcpConfig({ childHomeDir: homeDir, childId })),
      requireOk: true,
      defaultErrorMessage: "mcp.json write failed",
      // The gas seed already landed by this point -- FIND-001's reclaim-trigger case. `spec` already
      // carries `lease_id` (attached above), so appendSpecFailure's `...spec` spread includes it.
      onFailure: async (error) => {
        const reclaim = await attemptSeedReclaim({ deps, childEvmWallet: evmWallet, parentWalletAddress: drivingCitizenWallet, amountUsdc: seedUsdc });
        appendSpecFailure(ledgerFile, spec, { attemptedMs: nowMs, error, extra: reclaim });
      },
    });
    if (mcpStep.failed) return { status: "failed", childId, error: mcpStep.error };

    // Step 9 (REQ-305): ledger append (the "active" row) + citizen-registry append, gated on
    // isSelfFunded(). A failure here (e.g. the ledger file itself cannot be written) is allowed to
    // propagate -- there is no lower-tier ledger left to record it in.
    appendCitizenLifecycleReceipt(ledgerFile, { ...spec, status: "active", attempted_ms: nowMs, active_since: nowMs });

    const citizenRecord = {
      id: childId,
      wallet: { evm: true, ...(solanaWallet ? { solana: true } : {}) },
      walletAddress: { evm: evmWallet.address, ...(solanaWallet ? { solana: solanaWallet.address } : {}) },
      fuel: { provider: FUEL_PROVIDER },
      humanDependencies: [],
      homeDir,
      coLocatedWithCoordinator: false,
    };
    if (isSelfFunded(citizenRecord)) {
      try {
        await appendCitizenRecord(citizensRegistryFile, citizenRecord);
      } catch (e) {
        // FIND-003 (REQ-305 edge case): the ledger's "active" row above already landed genuinely --
        // a TRANSIENT registry-append failure (e.g. ENOTDIR/EACCES/ENOSPC) must never propagate as an
        // uncaught rejection out of this function (its own JSDoc contract only ever resolves
        // {status,childId,error?}). Queue a durable, retryable marker instead of the append itself;
        // retryPendingRegistryAppends (below) is the SOLE place that ever completes it, called by
        // wake-gate.mjs before the NEXT wake's own spawn evaluation runs (REQ-305's own "before any
        // further spawn evaluation runs" mandate) -- never a silent, permanent gap.
        const pendingFile = deps.pendingRegistryAppendsFile || defaultPendingRegistryAppendsFile();
        queuePendingRegistryAppend(pendingFile, {
          childId,
          citizenRecord,
          citizensRegistryFile,
          queuedMs: nowMs,
          error: errorMessage(e),
        });
        console.error(
          `self/spawn: citizens.json append failed transiently for ${childId}, queued for retry on next wake: ${errorMessage(e)}`
        );
      }
    } else {
      console.error(
        `self/spawn: refusing citizens.json append for ${childId} -- fails isSelfFunded(): ${selfFundedReasons(citizenRecord).join(",")}`
      );
    }

    return { status: "active", childId };
  };

  const lockResult = await withGigLock(lockStatePath, "colony-spawn", runAttempt);
  if (lockResult && Object.prototype.hasOwnProperty.call(lockResult, "status")) {
    return lockResult;
  }
  // withGigLock's own lock-rejection shape ({ok:false, reason}) -- never itself a status:"active" claim.
  return { status: "failed", childId: null, error: (lockResult && lockResult.reason) || "lock not acquired" };
}

/**
 * retryPendingRegistryAppends -- REQ-305 edge case (FIND-003): completes any citizens.json append
 * queued by a PRIOR executeSpawnAttempt call whose own step 9 registry-append failed for a transient
 * reason (above). Never re-runs the spawn attempt itself -- the child is already genuinely "active" in
 * the ledger; only the registry append is retried, against the SAME citizens_registry_file the original
 * failed attempt recorded. Intended call site: wake-gate.mjs, before the NEXT wake's own spawn
 * evaluation runs (REQ-305's own "before any further spawn evaluation runs" mandate) -- an entry that
 * fails again here simply remains "pending" for the wake after that, never thrown/escalated.
 *
 * @returns {Promise<Array<{childId: string, retried: boolean, error?: string}>>}
 */
export async function retryPendingRegistryAppends({ pendingRegistryAppendsFile, deps = {} } = {}) {
  const file = pendingRegistryAppendsFile || defaultPendingRegistryAppendsFile();
  const outstanding = deriveOutstandingRegistryAppends(readPendingRegistryAppends(file));
  const append = deps.appendCitizenRecord || appendCitizenRecord;
  const results = [];
  for (const entry of outstanding) {
    try {
      await append(entry.citizens_registry_file, entry.citizen_record);
      resolvePendingRegistryAppend(file, entry.child_id);
      results.push({ childId: entry.child_id, retried: true });
    } catch (e) {
      results.push({ childId: entry.child_id, retried: false, error: errorMessage(e) });
    }
  }
  return results;
}
