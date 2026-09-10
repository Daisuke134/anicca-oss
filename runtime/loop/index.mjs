/**
 * index.mjs — Anicca ReAct automaton loop entry point.
 *
 * REQ-001: Single-wake lifecycle (context → THINK → parse → execute → persist → sleep)
 * REQ-002: Survival-tier model selection
 * REQ-003: Earn skill execution + isProfitable classification via WAKE_ID
 * REQ-004: Private-key isolation
 * REQ-005: Loop detect / idle guard
 * REQ-006: Graceful shutdown on SIGTERM
 * REQ-007: Ledger immutability (append-only)
 * REQ-008: Cloud portability (no macOS-specific code)
 * REQ-009: Config via env / .env file
 * REQ-011: Pluggable brain backend
 *
 * Start via: node runtime/loop/index.mjs
 * Or:        ./start-local.sh node runtime/loop/index.mjs
 */

import { promises as fs } from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

import { readDotenvFile } from './dotenv.mjs';
import { loadConfig } from './config.mjs';
import { selectTier } from './tier.mjs';
import { fetchUsdcBalance } from './balance.mjs';
import { fetchNetWorth, resolveInstanceWallets } from '../../skills/earn/lib/net-worth.mjs';
import { assembleContext } from './context.mjs';
import { selfEval } from './self-eval.mjs';
import { think } from './brain.mjs';
import { parseToolCall } from './parse-tool-call.mjs';
import { resolveSkillPath } from './run-skill.mjs';
import { isEarnSlot, earnStrategyFor } from './earn-slot.mjs';
import { isLooping } from './loop-detect.mjs';
import { formatRecord } from './ledger-record.mjs';
import { appendLedgerLine, readLedgerLines } from './ledger.mjs';
import { classifyEarnResult, defaultEarnLedgerPath } from './earn-detect.mjs';
import { summarizeSkillResult } from './result-summary.mjs';
import { redactPrivateKeyPatterns } from './env-filter.mjs';
import { liveSlotNames } from './prompt.mjs';
import { classifyLayer, capFailureDetail } from './harness-health.mjs';
import {
  filterCatalog,
  DEFAULT_BOOTSTRAP_RESERVE_USDC,
  hasOpenRiskPositionOfYield,
  hasOpenRiskPositionOfHlTrade,
} from './catalog-gate.mjs';
import {
  isEarnActionSlot,
  assembleAlwaysActMenu,
  isMarketRiskFree,
  noRealizedAction,
  isRejectableSleepOrOffMenu,
  nextRerouteState,
  buildMustActReinforcement,
  buildAlwaysActLedgerFields,
} from './always-act-router.mjs';
import { publishLedgerCycle } from './ledger-publish.mjs';

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOOP_REPO_ROOT = path.resolve(__dirname, '..', '..');
const SKILLS_ROOT = process.env.LIFE_MANAGER_SKILLS_ROOT || path.join(LOOP_REPO_ROOT, 'skills');

// Inline ULID generator (no npm dependency — uses crypto.randomUUID as entropy source)
function ulid() {
  const now = Date.now();
  const ts = now.toString(36).toUpperCase().padStart(10, '0').slice(-10);
  const rand = randomUUID().replace(/-/g, '').toUpperCase().slice(0, 16);
  return ts + rand;
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

const ANICCA_HOME = process.env.ANICCA_HOME;
if (!ANICCA_HOME) {
  process.stderr.write('[loop] FATAL: ANICCA_HOME is not set. Set it to the absolute path of the agent home directory.\n');
  process.exit(1);
}

const dotenvText = await readDotenvFile(ANICCA_HOME);
const config = loadConfig(process.env, dotenvText);

const LEDGER_PATH = path.join(ANICCA_HOME, 'state', 'ledger.jsonl');
// anicca-harness-tooluse-health R6: a NEW side-channel path, never read by context.mjs/prompt.mjs
// (INV-NO-PROMPT-REGRESSION) — reuses the EXISTING appendLedgerLine primitive, never a new writer.
const HARNESS_FAILURES_PATH = path.join(ANICCA_HOME, 'state', 'harness-failures.jsonl');
const GENESIS_PATH = path.join(ANICCA_HOME, 'identity', 'genesis.md');
// franklin-ledger-push (P2) iter1 redesign: throttle/cursor state for ledger-publish.mjs —
// deliberately in ANICCA_HOME (data). The DEDICATED publish clone (never the shared checkout
// below) defaults to $ANICCA_HOME/state/.ledger-publish-repo, derived by ledger-publish.mjs itself
// from this marker path's directory — see ledger-publish.mjs's publishRepoDir default.
const LEDGER_PUBLISH_MARKER_PATH = path.join(ANICCA_HOME, 'state', '.ledger-publish-marker');
// This file itself lives at <repo>/runtime/loop/index.mjs — two dirs up is the SHARED checkout's
// repo root. ledger-publish.mjs reads ONLY `git remote get-url origin` from it (never writes to
// it, never checks out/commits/pushes against it — FIND-001/002's fix) to resolve where its own
// DEDICATED clone should point.

// Read genesis prompt (missing = warn + empty string)
let genesisPrompt = '';
try {
  genesisPrompt = await fs.readFile(GENESIS_PATH, 'utf8');
} catch {
  process.stderr.write(`[loop] WARNING: genesis.md not found at ${GENESIS_PATH}\n`);
}

// Load isProfitable from earn skill.
// Canonical path: $repo_root/skills/earn/lib/ledger.mjs
// repo_root = dirname(dirname(dirname(this file))) since this file is at runtime/loop/index.mjs
let isProfitable;
{
  // The canonical export lives in the immutable repository-owned skill tree. ANICCA_HOME contains
  // mutable instance data only and is never searched for executable code.
  const candidates = [
    path.join(SKILLS_ROOT, '_shared', 'lib', 'ledger.mjs'),
    path.join(SKILLS_ROOT, 'earn', 'lib', 'ledger.mjs'),
  ];
  for (const p of candidates) {
    try { const m = await import(p); if (typeof m.isProfitable === 'function') { isProfitable = m.isProfitable; break; } } catch { /* try next */ }
  }
  if (!isProfitable) {
    process.stderr.write('[loop] WARNING: could not load isProfitable from earn skill; all wakes will be non-profitable\n');
    isProfitable = () => false;
  }
}

// Load the skill registry → live slots + catalog (spec 25 O1: the LLM picks
// among the REAL live skills, not an opaque single "earn" slot).
// anicca-agent-economy REQ-201: also capture each live slot's `risk`/`alwaysAvailable` classification
// (maintainer-set DATA in registry.json, not something inferred at runtime) so the per-wake bootstrap-
// reserve catalog gate below (filterCatalog) has real `riskTagOf`/`alwaysAvailableOf` inputs.
let activeSkillSlots = [];
let skillCatalog = {};
// TOOL-2 Phase A: per-slot { toolDescription, argsExample } lifted verbatim from registry.json, only
// for slots that actually define them. Kept parallel to skillCatalog (never replaces it) so a slot
// missing toolDescription still falls back to its plain summary line — additive, backward-compatible.
let skillToolDocs = {};
let riskTagBySlot = {};
let alwaysAvailableBySlot = {};
// franklin-alwaysact-skill-router REQ-502: the FULL parsed registry object (not just the derived
// activeSkillSlots/riskTagBySlot maps above), needed as-is by assembleAlwaysActMenu each wake.
let registryForAlwaysAct = null;
{
  // Test-only env var (same idiom as ANICCA_BALANCE_OVERRIDE/CLAUDE_BIN elsewhere in this codebase):
  // lets REQ-502's empty-menu edge case be driven through a REAL spawned wake against a REAL (if
  // fixture) registry.json, rather than only through the pure assembleAlwaysActMenu unit test.
  // Absent in production; defaults to the real repo-relative path.
  const registryPath = process.env.ALWAYS_ACT_REGISTRY_PATH_OVERRIDE || path.join(SKILLS_ROOT, 'registry.json');
  try {
    let registry = JSON.parse(await fs.readFile(registryPath, 'utf8'));
    // ANICCA_SLOT_ALLOWLIST (x402-zero-to-one 2026-07-14): restrict the menu to an explicit slot
    // set for focused earn tests. One choke point — both activeSkillSlots below and
    // assembleAlwaysActMenu (per wake) read this same object. alwaysAvailable slots survive.
    {
      const { applySlotAllowlist } = await import('./slot-allowlist.mjs');
      const res = applySlotAllowlist(registry, process.env.ANICCA_SLOT_ALLOWLIST, {
        preserveAlwaysAvailable: process.env.ANICCA_STRICT_SLOT_ALLOWLIST !== '1',
      });
      registry = res.registry;
      if (res.applied) process.stderr.write(`[loop] slot allowlist active: ${res.applied.join(', ')}\n`);
    }
    registryForAlwaysAct = registry;
    activeSkillSlots = liveSlotNames(registry);
    for (const name of activeSkillSlots) {
      const slotDef = registry.slots[name] || {};
      skillCatalog[name] = slotDef.summary || '';
      if (slotDef.toolDescription) {
        skillToolDocs[name] = { toolDescription: slotDef.toolDescription, argsExample: slotDef.argsExample };
      }
      riskTagBySlot[name] = slotDef.risk;
      alwaysAvailableBySlot[name] = slotDef.alwaysAvailable === true;
    }
    process.stderr.write(`[loop] live skills: ${activeSkillSlots.join(', ') || '(none)'}\n`);
  } catch (err) {
    process.stderr.write(`[loop] WARNING: could not read registry.json (${err.message}); falling back to ['earn']\n`);
    activeSkillSlots = ['earn'];
  }
}

function riskTagOf(slotName) {
  return riskTagBySlot[slotName];
}
function alwaysAvailableOf(slotName) {
  return alwaysAvailableBySlot[slotName] === true;
}

/**
 * queryHlTradeOpenPositions — the REAL Hyperliquid position query REQ-201's `hl_trade` open-position
 * carve-out needs (see catalog-gate.mjs's hasOpenRiskPositionOfHlTrade, which invokes this ONLY when
 * balanceUsdc < BOOTSTRAP_RESERVE_USDC for the current wake). Reuses the SAME primitive
 * `skills/earn/hl-trade/hl.py account` already uses in production (its own `open_positions` array,
 * derived from `info.user_state(address).assetPositions` filtered to nonzero `szi`) by invoking it as a
 * subprocess -- the exact same tool `skills/earn/run.sh` already shells out to (same dedicated-venv
 * resolution: `skills/earn/hl-trade/.venv/bin/python` if present, else plain `python3`), not a new API
 * surface. Any failure (missing venv/deps, unfunded account, network) is caught by the caller
 * (hasOpenRiskPositionOfHlTrade), which fails OPEN (assumes a position may exist) rather than crashing
 * the wake loop or silently hiding `hl_trade` from an instance that might need it to close a position.
 */
async function queryHlTradeOpenPositions() {
  const hlDir = path.join(SKILLS_ROOT, 'earn', 'hl-trade');
  const hlScript = path.join(hlDir, 'hl.py');
  const venvPython = path.join(hlDir, '.venv', 'bin', 'python');
  let pythonBin = 'python3';
  try { await fs.access(venvPython); pythonBin = venvPython; } catch { /* fall back to system python3 */ }
  const { stdout } = await execFileAsync(pythonBin, [hlScript, 'account'], { timeout: 15000 });
  return JSON.parse(stdout);
}

// ── franklin-alwaysact-skill-router REQ-501: identity + flag gate ─────────────────────────────
//
// Mirrors skills/earn/sol-trade/run.sh:28-41's own-vs-CLI-wallet derivation-and-comparison idiom
// EXACTLY, including its FIND-001 fix (unset ANICCA_SOLANA_PRIVATE_KEY before deriving either side —
// an ambient override would otherwise make OWN_WALLET===CLI_WALLET for ANY instance and bypass the
// identity-match guard entirely). Read-only address derivation only — never signs, never spends,
// never logs the underlying secret (runtime/wallet-address-solana.mjs's own REQ-006 contract).

const WALLET_ADDRESS_SOLANA_PATH = path.join(
  path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..'),
  'runtime', 'wallet-address-solana.mjs',
);

function envWithoutSolanaKey(overrides = {}) {
  const env = { ...process.env, ...overrides };
  delete env.ANICCA_SOLANA_PRIVATE_KEY;
  return env;
}

async function deriveSolanaAddress(env) {
  try {
    const { stdout } = await execFileAsync(process.execPath, [WALLET_ADDRESS_SOLANA_PATH], { env, timeout: 15000 });
    const addr = (stdout || '').trim();
    return addr.length ? addr : null;
  } catch {
    return null; // fail-closed: derivation error/timeout -> treat as NOT Franklin (REQ-501 edge case)
  }
}

// Franklin's own body always runs with ANICCA_HOME literally === $HOME/.blockrun (the same real
// deployment topology wallet-address-solana.test.mjs's own live test verifies against
// /home/life-manager/.blockrun). This is a CHEAP, structural fast-path — not a substitute for the real
// crypto derivation below (which still always runs whenever this holds) — that lets every OTHER
// instance in the fleet (automaton, any future instance) whose ANICCA_HOME is structurally NOT
// $HOME/.blockrun skip the (real, subprocess-spawning) identity derivation entirely on every wake,
// forever: spawning 2+ node subprocesses per wake, 24/7, for a check that can only ever resolve
// "not Franklin" for those instances would be pure wasted compute for a cost-conscious self-funded
// fleet. A wake whose ANICCA_HOME genuinely IS $HOME/.blockrun always falls through to the real check.
function looksLikeFranklinHome(home) {
  return typeof process.env.ANICCA_HOME === 'string' && process.env.ANICCA_HOME === path.join(home, '.blockrun');
}

// Minimum wall-clock floor for a genuine (plausibly-Franklin) identity-gate resolution. This is a
// deliberate, deterministic pacing floor — not incidental subprocess-spawn timing — for a
// money-safety-critical decision that removes the sleep safety valve for the rest of the wake:
// it bounds how fast this gate can be re-evaluated back-to-back (avoiding a subprocess-spawn
// thrash if wakes were ever misconfigured to cycle without the normal SLEEP_BASE_S interval) and is
// negligible relative to a real wake's normal cadence (SLEEP_BASE_S defaults to 120s) or a real
// think() call's own network latency.
const ALWAYS_ACT_IDENTITY_SETTLE_FLOOR_MS = 500;

/** REQ-501(a): does THIS instance's own resolved Solana wallet match $HOME/.blockrun's (Franklin's
 * own home)? Fail-closed on any derivation error/empty/mismatch — never throws. Sequential (not
 * Promise.all), mirroring sol-trade/run.sh:35-36's own two SEQUENTIAL `$(...)` command substitutions.
 * A money-safety-critical identity gate (this decision governs whether the sleep safety valve is
 * removed for the rest of the wake) re-confirms OWN_WALLET after deriving CLI_WALLET — an A-B-A
 * pattern that guards against a TOCTOU race on the underlying identity file between the two reads,
 * not merely a single point-in-time snapshot. */
async function checkAlwaysActIdentity() {
  const home = process.env.HOME;
  if (!home || !looksLikeFranklinHome(home)) return false;

  const started = Date.now();
  const ownEnv = envWithoutSolanaKey();
  const cliEnv = envWithoutSolanaKey({ ANICCA_HOME: path.join(home, '.blockrun') });

  const match = await (async () => {
    const ownWallet = await deriveSolanaAddress(ownEnv);
    if (!ownWallet) return false;
    const cliWallet = await deriveSolanaAddress(cliEnv);
    if (!cliWallet || cliWallet !== ownWallet) return false;
    const ownWalletReconfirm = await deriveSolanaAddress(ownEnv);
    return ownWalletReconfirm === ownWallet;
  })();

  const elapsedMs = Date.now() - started;
  if (elapsedMs < ALWAYS_ACT_IDENTITY_SETTLE_FLOOR_MS) {
    await new Promise((r) => setTimeout(r, ALWAYS_ACT_IDENTITY_SETTLE_FLOOR_MS - elapsedMs));
  }
  return match;
}

/**
 * REQ-501(b): default-OFF config flag, mirroring SOL_GATE_LIVE_ENABLE's own fail-closed contract
 * (franklin-sol-evolvable-edge REQ-009) — unset/malformed (anything other than the literal string
 * "1") is treated as disabled. Resolved freshly on EVERY wake (never cached across wakes) — mirrors
 * sol-trade/run.sh's own per-invocation identity derivation, so a mid-session identity/flag change
 * takes effect on the very next wake, exactly matching REQ-501's "WHEN a wake begins..." EARS clause.
 *
 * @returns {Promise<{engaged: boolean, identityMatch: boolean, flagReason: string|null}>}
 */
async function resolveAlwaysActGate() {
  const identityMatch = await checkAlwaysActIdentity();
  const flagRaw = process.env.ALWAYS_ACT_ENABLED;
  const flagReason = flagRaw === '1'
    ? null
    : (flagRaw == null || flagRaw === '' ? 'flag_unset' : 'flag_malformed');
  const engaged = identityMatch && flagRaw === '1';
  return { engaged, identityMatch, flagReason };
}

// ── State ─────────────────────────────────────────────────────────────────────

let currentTier = { tier: 'broke', model: config.ANICCA_FREE_MODEL || 'free/gpt-oss-120b' };
let recentActions = [];
// When a loop is detected, the repeated slot is parked here and FORBIDDEN on the next wake (then cleared
// once the model picks something else) — this is what actually breaks the cook/x402 spin (Dais 2026-06-22).
let avoidSlot = null;
// Escalating cooldown state (§24 adversary #4): a weak free model can ignore the prompt's "forbidden"
// steer and re-pick the SAME dead slot a few wakes later, re-triggering loop_detect on a flat sleep
// forever (observed live: hl_trade thrashed ~25x/session against a constant 300s cooldown). Track how
// many CONSECUTIVE loop_detect events fired on the same slot; each one doubles the next cooldown
// (capped) so repeat-offending costs more wall-clock time. Resets the moment the model diversifies away
// from avoidSlot — the agent's choice is never blocked, only the enforced pause after ignoring it grows.
let loopDetectStreak = 0;
let loopDetectSlot = null;
let shuttingDown = false;
let currentChildKiller = null; // called to kill in-flight skill on SIGTERM

// ── SIGTERM handler (REQ-006) ─────────────────────────────────────────────────

process.on('SIGTERM', async () => {
  if (shuttingDown) return;
  shuttingDown = true;
  process.stderr.write('[loop] SIGTERM received — flushing shutdown ledger line\n');

  // Kill in-flight child if any
  if (currentChildKiller) {
    try { currentChildKiller(); } catch {}
    // Give child up to 5s to die
    await new Promise(r => setTimeout(r, 5000));
  }

  const wakeId = ulid();
  const record = formatRecord({
    ts: Math.floor(Date.now() / 1000),
    wake_id: wakeId,
    kind: 'shutdown',
    sleep_s: 0,
  });
  try {
    await appendLedgerLine(LEDGER_PATH, record);
  } catch (err) {
    process.stderr.write(`[loop] Could not write shutdown ledger line: ${err.message}\n`);
  }
  process.exit(0);
});

// ── Wake loop ─────────────────────────────────────────────────────────────────

process.stderr.write(`[loop] Starting Anicca loop. ANICCA_HOME=${ANICCA_HOME}\n`);
const singleWake = process.env.ANICCA_SINGLE_WAKE === '1';

while (!shuttingDown) {
  await runOneWake();
  // franklin-ledger-push (P2) REQ-701..709: best-effort, default-OFF (LEDGER_PUBLISH_ENABLED)
  // publish of BOTH this instance's ledger evidence sources (state/ledger.jsonl wake bookkeeping AND
  // skills/earn/state/earn-ledger.jsonl money evidence -- impl-review iter2 FIND-001) into the
  // __REPO_ROOT__ repo. publishLedgerCycle() itself never throws (its own REQ-703 contract) -- this
  // try/catch is deliberate defense-in-depth so a ledger-publish regression can NEVER take the wake
  // loop down with it.
  try {
    const publishResult = await publishLedgerCycle({
      ledgerPath: LEDGER_PATH,
      earnLedgerPath: defaultEarnLedgerPath(config),
      repoRoot: LOOP_REPO_ROOT,
      markerPath: LEDGER_PUBLISH_MARKER_PATH,
      instance: process.env.ANICCA_INSTANCE || 'clawrouter',
    });
    // impl-review iter2 FIND-005: N=5 consecutive publish-cycle failures escalate via the EXISTING
    // appendHarnessFailure mechanism (never a new writer) so healthchecks can see a stuck
    // ledger-publish pipeline (e.g. a revoked/read-only git credential) instead of it failing
    // silently on stderr forever.
    if (publishResult && publishResult.publishFailureStreak >= 5) {
      await appendHarnessFailure({
        ts: Math.floor(Date.now() / 1000),
        wakeId: 'n/a',
        kind: 'ledger_publish_stuck',
        layer: 'ledger_publish',
        exitCode: null,
        rawDetail: `ledger-publish: ${publishResult.publishFailureStreak} consecutive cycle failures, last reason=${publishResult.reason}`,
      });
    }
  } catch (err) {
    process.stderr.write(`[loop] ledger-publish cycle threw unexpectedly (should be impossible): ${err.message}\n`);
  }
  if (singleWake) break;
}

// ── Single wake ───────────────────────────────────────────────────────────────

async function runOneWake() {
  if (shuttingDown) return;

  const wakeId = ulid();
  const ts = Math.floor(Date.now() / 1000);

  // 0. franklin-alwaysact-skill-router REQ-501: identity+flag gate, freshly re-evaluated EVERY wake.
  const { engaged: alwaysActEngagedThisWake, identityMatch: alwaysActIdentityMatch, flagReason: alwaysActFlagReason } = await resolveAlwaysActGate();

  async function writeAlwaysActNotEngagedIfNeeded() {
    // REQ-512: a Franklin-identity wake whose flag is unset/malformed ledgers this diagnostic line
    // on EVERY such wake (unconditionally, not only after an intended go-live) — the anchor that
    // later lets a companion detector distinguish "not yet enabled" from "silently regressed back to
    // idle-permitted". Written once THINK has been attempted (never before) so it is never mistaken
    // by an observer for this wake's own terminal outcome being ready before the brain call happened.
    if (alwaysActIdentityMatch && !alwaysActEngagedThisWake) {
      const notEngagedRecord = formatRecord({
        ts, wake_id: wakeId, kind: 'always_act_not_engaged', reason: alwaysActFlagReason,
      });
      await safeAppend(LEDGER_PATH, notEngagedRecord);
    }
  }

  // 1. Load wallet address (no key derivation — REQ-004, REQ-008)
  const walletAddress = config.ANICCA_WALLET_ADDRESS || process.env.ANICCA_WALLET_ADDRESS || 'unknown';
  if (walletAddress === 'unknown') {
    process.stderr.write('[loop] WARNING: ANICCA_WALLET_ADDRESS not set, using "unknown"\n');
  }

  // 2. Fetch USDC balance (failure → keep prior tier, REQ-002)
  let liquidUsdc = 0;
  try {
    const balance = await fetchUsdcBalance(walletAddress, config);
    liquidUsdc = typeof balance === 'number' ? balance : 0;
    currentTier = selectTier(balance, config);
  } catch (err) {
    process.stderr.write(`[loop] Balance fetch failed: ${err.message} — keeping tier=${currentTier.tier}\n`);
  }

  // 2b. Fetch TOTAL net worth across every chain, token and venue this instance holds money in.
  //     This is deliberately NOT merged into liquidUsdc above: liquidUsdc gates capital-risk slots
  //     and must stay the conservative "spendable right here, right now" number, whereas most of an
  //     instance's money sits somewhere it can only be spent AT (pUSD inside a Polymarket deposit
  //     wallet buys Polymarket shares and nothing else; Hyperliquid margin backs perps and nothing
  //     else). Widening the GATE with venue-locked money would let a slot open on funds it cannot
  //     actually draw on. What the agent was missing was VISIBILITY, not permission -- measured
  //     2026-07-12, claude-p's readers showed $1.95 of a real $24.59, and Franklin, sent $18.88 of
  //     SOL, kept logging "my USDC is too small to trade" because SOL was invisible to it. So the
  //     total goes into the PROMPT (see prompt.mjs), where the model can reason about where its
  //     money actually is and move it, while the gate stays honest. Fail-soft: any error leaves
  //     netWorth null and the wake proceeds exactly as before.
  let netWorth = null;
  try {
    const wallets = resolveInstanceWallets(config);
    if (wallets.length > 0) netWorth = await fetchNetWorth(wallets);
  } catch (err) {
    process.stderr.write(`[loop] Net-worth fetch failed (non-fatal): ${err.message}\n`);
  }

  // 3. Read recent ledger lines for context
  let recentLedger = [];
  try {
    const all = await readLedgerLines(LEDGER_PATH);
    recentLedger = all.slice(-20);
  } catch {}

  // 3b. SELF-EVAL (H2): read the EARN ledger (the outcome trace, H1) and compute per-action realised P&L,
  // so the prompt can show the AI that e.g. hl-trade ×22 = $0 is a DEAD action. The AI then decides to
  // stop it itself (H3) — no hardcoded "avoid hl_trade" rule; we give it the money signal, it judges.
  let earnSteer = '';
  try {
    const earnLedgerPath = config.EARN_LEDGER || path.join(ANICCA_HOME, 'state', 'skills', 'earn', 'earn-ledger.jsonl');
    const raw = await fs.readFile(earnLedgerPath, 'utf8');
    const earnLines = raw.trim().split('\n').filter(Boolean)
      .map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
    earnSteer = selfEval(earnLines, { window: 25 }).steer;
  } catch { /* no earn ledger yet → no steer */ }

  // 4. Loop-detect check (REQ-005). Sleeping alone did NOT break the loop — the model just re-picked the
  // same slot+args next wake (cook×19 / x402×10 with identical args, observed 2026-06-22). So when a loop
  // is detected we (a) remember the repeated slot, (b) RESET the action history so the detector doesn't
  // instantly re-fire on stale entries, and (c) sleep briefly — then the NEXT wake's prompt FORBIDS that
  // slot, forcing the model to diversify (try a different earn path / actually act on what it found).
  const loopWindow = cfgNum(config.LOOP_DETECT_WINDOW, 3);
  if (loopWindow > 0 && isLooping(recentActions, loopWindow)) {
    avoidSlot = recentActions[recentActions.length - 1]?.slot || null;
    // Same slot re-offending back-to-back → escalate; a different slot looping → fresh streak of 1.
    loopDetectStreak = (avoidSlot && avoidSlot === loopDetectSlot) ? loopDetectStreak + 1 : 1;
    loopDetectSlot = avoidSlot;
    process.stderr.write(`[loop] Loop detected on '${avoidSlot}' (streak ${loopDetectStreak}) — forbidding it next wake to force diversification\n`);
    recentActions = [];
    const baseSleepS = cfgNum(config.SLEEP_LOOP_DETECT_S, 300);
    const maxSleepS = cfgNum(config.SLEEP_LOOP_DETECT_MAX_S, 3600);
    const sleepS = Math.min(baseSleepS * (2 ** (loopDetectStreak - 1)), maxSleepS);
    const record = formatRecord({ ts, wake_id: wakeId, kind: 'loop_detect', slot: avoidSlot, sleep_s: sleepS, streak: loopDetectStreak });
    await safeAppend(LEDGER_PATH, record);
    await sleepSecs(sleepS);
    return;
  }

  // 5. Assemble context
  // PATCH 3: summarize the latest deployed position from the ledger (no extra RPC) so the prompt
  // shows the model its portfolio and it can DECIDE a strategy from it.
  const lastYield = (Array.isArray(recentLedger) ? recentLedger : []).slice().reverse()
    .find(l => String(l && l.source || '').startsWith('yield') && l.tx);
  const positionsSummary = lastYield
    ? `~$${lastYield.deposited_usdc ?? '?'} in ${lastYield.source} (last tx ${String(lastYield.tx).slice(0, 10)})`
    : '';

  // 5b. anicca-agent-economy REQ-201/202/203: bootstrap-reserve catalog eligibility gate. Below
  // BOOTSTRAP_RESERVE_USDC, hide capital-risking earn slots from THIS wake's catalog/tool menu --
  // except a slot the instance currently needs to close/withdraw an ALREADY-open position in (the
  // FIND-003 carve-out). `hasOpenRiskPositionOf('yield')` reuses the SAME already-fetched ledger scan
  // as `positionsSummary` above (no new I/O); `hasOpenRiskPositionOf('hl_trade')` is resolved via the
  // lazy, threshold-gated Hyperliquid query (catalog-gate.mjs's hasOpenRiskPositionOfHlTrade only
  // actually calls queryHlTradeOpenPositions when liquidUsdc is below the reserve). Both booleans are
  // fully resolved here, BEFORE the pure filterCatalog call, so filterCatalog itself stays pure.
  const openPositionYield = hasOpenRiskPositionOfYield(recentLedger);
  const openPositionHlTrade = await hasOpenRiskPositionOfHlTrade({
    balanceUsdc: liquidUsdc,
    reserveThresholdUsdc: DEFAULT_BOOTSTRAP_RESERVE_USDC,
    queryFn: queryHlTradeOpenPositions,
  });
  function hasOpenRiskPositionOf(slotName) {
    if (slotName === 'yield') return openPositionYield;
    if (slotName === 'hl_trade') return openPositionHlTrade;
    return false; // no carve-out mechanism specified/required for any other slot (REQ-201)
  }
  let eligibleSkillSlots = activeSkillSlots;
  try {
    // Gate each slot on the money THAT slot can actually spend, not on Base USDC alone.
    // pUSD in a Polymarket deposit wallet buys Polymarket shares and nothing else — so it is
    // real, spendable money FOR the polymarket slot, and invisible everywhere else. Same for
    // Solana USDC and the sol-trade slot. Reading only Base USDC is what left claude-p with
    // $18 to its name, a $2 reserve it missed by five cents, and no slot to pick but narrate.
    // netWorth is fail-soft (null on any RPC error) → fall back to the old single balance.
    const spendableFor = (slotName) => {
      const holdings = netWorth?.holdings;
      if (!Array.isArray(holdings)) return liquidUsdc;
      const sum = (pred) =>
        holdings.filter(pred).reduce((acc, h) => acc + (Number(h.usd) || 0), 0);
      if (slotName === 'earn/polymarket-trade') {
        return liquidUsdc + sum((h) => h.symbol === 'pUSD');
      }
      if (slotName === 'earn/sol-trade') {
        return liquidUsdc + sum((h) => h.chain === 'solana');
      }
      return liquidUsdc;
    };

    eligibleSkillSlots = filterCatalog({
      balanceUsdc: spendableFor,
      allSlotNames: activeSkillSlots,
      riskTagOf,
      alwaysAvailableOf,
      hasOpenRiskPositionOf,
      reserveThresholdUsdc: DEFAULT_BOOTSTRAP_RESERVE_USDC,
    });
  } catch (err) {
    process.stderr.write(`[loop] catalog-gate filterCatalog failed (${err.message}) — falling back to the full unfiltered catalog\n`);
    eligibleSkillSlots = activeSkillSlots;
  }

  // 5c. franklin-alwaysact-skill-router REQ-502/503: the earn-action-only menu, only assembled when
  // this wake is actually always-act-engaged (REQ-501). Reuses the SAME riskTagOf/alwaysAvailableOf/
  // hasOpenRiskPositionOf/filterCatalog inputs the ordinary catalog gate above already resolved this
  // wake — no new I/O, no duplicate classification source.
  let alwaysActMenu = [];
  if (alwaysActEngagedThisWake) {
    try {
      alwaysActMenu = assembleAlwaysActMenu({
        registry: registryForAlwaysAct,
        catalogFilterFn: filterCatalog,
        balanceUsdc: liquidUsdc,
        reserveThresholdUsdc: DEFAULT_BOOTSTRAP_RESERVE_USDC,
        riskTagOf,
        alwaysAvailableOf,
        hasOpenRiskPositionOf,
      });
    } catch (err) {
      process.stderr.write(`[loop] assembleAlwaysActMenu failed (${err.message}) — treating as empty menu (REQ-508 escalation)\n`);
      alwaysActMenu = [];
    }
  }

  const ctx = assembleContext({
    walletAddress,
    balanceUsdc: liquidUsdc, // REAL liquid (was broke?0:undefined → always 0, so the buffer steer saw $0 for everyone)
    netWorth,                // TOTAL across chains/venues (see 2b) — visibility for the model, NOT a gate
    tier: currentTier.tier,
    model: currentTier.model,
    recentLedgerLines: recentLedger,
    genesisPrompt,
    wakeId,
    ts,
    activeSkillSlots: eligibleSkillSlots,
    skillCatalog,
    skillToolDocs,
    positionsSummary,
    earnSteer,
    avoidSlot,
    recentSlots: recentActions.map((a) => a.slot),
    alwaysActEngaged: alwaysActEngagedThisWake,
    alwaysActMenu,
  });

  // 6. THINK (brain call). REQ-505/506/511/513: an always-act-engaged wake runs through the bounded
  // attemptsUsed retry/reroute/escalation state machine (at most 2 think() calls total) instead of
  // today's single-think()-call path — see runAlwaysActWake. A non-engaged ctx (the overwhelming
  // majority of all wakes, every non-Franklin instance) is completely unaffected: it never reaches
  // this branch and falls straight through to the unchanged code below, byte-for-byte.
  if (ctx.alwaysActEngaged) {
    return runAlwaysActWake({ ctx, wakeId, ts, alwaysActMenu });
  }

  let rawResponse;
  try {
    rawResponse = await think(ctx, config);
  } catch (err) {
    await writeAlwaysActNotEngagedIfNeeded();
    await writeWakeErrorAndSleep({ wakeId, ts, err });
    return;
  }
  await writeAlwaysActNotEngagedIfNeeded();

  // 7. Parse tool call
  const toolCall = parseToolCall(rawResponse);

  if (!toolCall) {
    // Text-only narrate wake
    const sleepS = cfgNum(config.SLEEP_BASE_S, 120);
    const record = formatRecord({
      ts,
      wake_id: wakeId,
      kind: 'narrate',
      sleep_s: sleepS,
      model: currentTier.model,
    });
    await safeAppend(LEDGER_PATH, record);
    await sleepSecs(sleepS);
    return;
  }

  const { slot, args } = toolCall;

  // Sleep tool call
  if (slot === 'sleep') {
    const sleepS = args.seconds != null ? Number(args.seconds) : cfgNum(config.SLEEP_BASE_S, 120);
    const record = formatRecord({
      ts,
      wake_id: wakeId,
      kind: 'narrate',
      sleep_s: sleepS,
      model: currentTier.model,
      note: args.reason || 'agent chose to sleep',
    });
    await safeAppend(LEDGER_PATH, record);
    await sleepSecs(sleepS);
    return;
  }

  // 8. Execute skill. The model picked a slot — if it's not the forbidden one, the diversification worked,
  // so clear the avoid flag AND the escalating-cooldown streak (the thrash pattern broke; a fresh streak
  // starts from 1 if this or any slot loops again later).
  if (slot !== avoidSlot) { avoidSlot = null; loopDetectStreak = 0; loopDetectSlot = null; }
  recentActions.push({ slot, args: args || {} });
  const windowBuf = Math.max(loopWindow * 2, 10);
  if (recentActions.length > windowBuf) {
    recentActions = recentActions.slice(-windowBuf);
  }

  let skillResult;
  let childKillRef = { kill: null };
  currentChildKiller = () => { if (childKillRef.kill) childKillRef.kill(); };

  try {
    skillResult = await runSkillWithKillRef(slot, args, wakeId, config, childKillRef);
  } finally {
    currentChildKiller = null;
  }

  if (shuttingDown) return;

  // 9. Classify earn result (REQ-003 critical invariant)
  let kind = 'wake';
  let profitable = false;

  if (skillResult.notFound) {
    kind = 'skill_missing';
  } else if (skillResult.timedOut) {
    kind = 'skill_timeout';
  } else if (skillResult.exitCode !== 0) {
    kind = 'skill_error';
  } else if (isEarnSlot(slot)) {
    // Only classify earn from the earn-ledger line (exit code 0 alone is NOT sufficient).
    // isEarnSlot covers the legacy action slots AND per-method nested earn/<sub> (gig/clip/affiliate/video/audit).
    const earnLedgerPath = defaultEarnLedgerPath(config);
    const { profitable: p } = await classifyEarnResult(wakeId, earnLedgerPath, isProfitable);
    profitable = p;
  }

  // 9b. anicca-harness-tooluse-health R6: on tool_missing/tool_timeout/tool_logic (never on the
  // clean 'wake' kind), append one detail line to harness-failures.jsonl. skillResult.output is
  // ALREADY redacted (redactPrivateKeyPatterns applied inside runSkillWithKillRef) — reused verbatim
  // here, no new redaction pass for these three branches (R6).
  {
    const failureLayer = classifyLayer({ kind });
    if (failureLayer !== 'clean') {
      await appendHarnessFailure({
        ts,
        wakeId,
        slot,
        kind,
        layer: failureLayer,
        exitCode: skillResult.exitCode != null ? skillResult.exitCode : null,
        rawDetail: skillResult.output || '',
      });
    }
  }

  // 10. Persist ledger line (REQ-007)
  const sleepS = cfgNum(config.SLEEP_BASE_S, 120);

  // Redact any private key patterns from observation (PROP-020)
  const safeObservation = redactPrivateKeyPatterns(skillResult.output || '').slice(0, 1200);

  const recordFields = {
    ts,
    wake_id: wakeId,
    kind,
    sleep_s: sleepS,
    model: currentTier.model,
    slot,
    // OBSERVABILITY: log the model's decided args (strategy + params) so we can SEE what it chose each
    // wake — without this the wake line had no `args`, which read as "the model decided nothing" when it
    // actually did. Empty object when the model passed none.
    ...(args && Object.keys(args).length ? { args } : {}),
    ...(kind === 'wake' ? { profitable } : {}),
    ...(skillResult.exitCode != null ? { exit_code: skillResult.exitCode } : {}),
    // DEEP FEEDBACK FIX (Dais 2026-06-22): record a short summary of what the skill ACTUALLY returned
    // (cook's findings, x402's sales=0, yield's action) so the NEXT wake's prompt shows OUTCOMES, not
    // just "I ran cook" — the model was re-cooking the same query because it never saw the result.
    // TOOL-2 Phase A: skills' contract is "stdout emits one JSON line" — summarizeSkillResult prefers
    // that structured last line over the raw slice so the next wake reasons over sales/errors instead
    // of re-parsing raw logs.
    ...(safeObservation ? { result: summarizeSkillResult(safeObservation) } : {}),
  };

  // Verify ledger line has no private key pattern (PROP-020)
  const recordStr = formatRecord(recordFields);
  const safeRecord = redactPrivateKeyPatterns(recordStr);
  await safeAppend(LEDGER_PATH, safeRecord);

  await sleepSecs(sleepS);
}

// ── franklin-alwaysact-skill-router: the always-act-engaged wake (REQ-505/506/508/511/513) ────────
//
// Runs the bounded attemptsUsed retry/reroute/escalation state machine (behavioral-spec.md sec2.5's
// exhaustive 12-row transition matrix) in place of runOneWake's steps 6-10 above. `attemptsUsed`
// (nextRerouteState's own {0,1} output) is the SOLE arbiter of every branch decision — never
// `currentOfferedSlots` array identity (spec-review iteration-4 FIND-301). At most 2 think() calls
// total per wake (REQ-511).

async function runAlwaysActWake({ ctx, wakeId, ts, alwaysActMenu }) {
  const sleepS = cfgNum(config.SLEEP_BASE_S, 120);

  // REQ-502 empty-menu terminal case: zero think() calls, immediate truthful escalation, distinct
  // kind:'router_menu_empty' (spec-review Phase 3 iter1 FIND-001 fix) — this is a REGISTRY/CONFIG spec
  // violation (zero live earn-action slots resolved), never conflated with the ordinary
  // bounds-exhausted `router_no_realized_action` outcome below.
  if (!Array.isArray(alwaysActMenu) || alwaysActMenu.length === 0) {
    await writeAlwaysActEscalation({ wakeId, ts, sleepS, attemptsUsed: 0, kind: 'router_menu_empty' });
    await sleepSecs(sleepS);
    return;
  }

  let attemptsUsed = 0;
  let currentOfferedSlots = alwaysActMenu; // baseline attempt: the FULL always-act menu
  let reinforcement = null; // set only ahead of a REQ-505 reprompt attempt

  for (;;) {
    const attemptCtx = reinforcement
      ? { ...ctx, alwaysActMenu: currentOfferedSlots, mustActReinforcement: reinforcement }
      : { ...ctx, alwaysActMenu: currentOfferedSlots };

    let rawResponse;
    try {
      rawResponse = await think(attemptCtx, config);
    } catch (err) {
      // Brain-transport failure mid-attempt: identical handling to the non-always-act wake_error
      // path (REQ-509: never a money-safety guard interaction; no fabricated success).
      await writeWakeErrorAndSleep({ wakeId, ts, err });
      return;
    }

    const toolCall = parseToolCall(rawResponse);

    // Case A (REQ-505): no tool call at all this attempt.
    if (!toolCall) {
      const state = nextRerouteState({ attemptsUsed, maxAttempts: 1 });
      if (state.exhausted) {
        await writeAlwaysActEscalation({ wakeId, ts, sleepS, attemptsUsed });
        await sleepSecs(sleepS);
        return;
      }
      attemptsUsed = state.attemptsUsedNext;
      currentOfferedSlots = alwaysActMenu; // a reprompt NEVER narrows the schema (only a reroute does)
      reinforcement = buildMustActReinforcement(ctx, { outcome: 'no-tool-call' });
      continue;
    }

    const { slot, args } = toolCall;

    // Case B (REQ-513): a fabricated "sleep" or any slot absent from THIS attempt's offered set.
    if (isRejectableSleepOrOffMenu(slot, currentOfferedSlots)) {
      const state = nextRerouteState({ attemptsUsed, maxAttempts: 1 });
      if (state.exhausted) {
        await writeAlwaysActEscalation({ wakeId, ts, sleepS, attemptsUsed });
        await sleepSecs(sleepS);
        return;
      }
      attemptsUsed = state.attemptsUsedNext;
      currentOfferedSlots = alwaysActMenu; // a rejected-slot retry is ALWAYS a REQ-505 reprompt
      reinforcement = buildMustActReinforcement(ctx, { outcome: 'rejected-slot' });
      continue;
    }

    // Case C (REQ-507): a valid slot was picked — execute it verbatim (opaque args pass-through, no
    // ranking/filtering by content). Mirrors runOneWake's own step 8 loop-detect bookkeeping.
    reinforcement = null;
    if (slot !== avoidSlot) { avoidSlot = null; loopDetectStreak = 0; loopDetectSlot = null; }
    recentActions.push({ slot, args: args || {} });
    const windowBuf = Math.max(cfgNum(config.LOOP_DETECT_WINDOW, 3) * 2, 10);
    if (recentActions.length > windowBuf) recentActions = recentActions.slice(-windowBuf);

    let childKillRef = { kill: null };
    currentChildKiller = () => { if (childKillRef.kill) childKillRef.kill(); };
    let skillResult;
    try {
      skillResult = await runSkillWithKillRef(slot, args, wakeId, config, childKillRef);
    } finally {
      currentChildKiller = null;
    }
    if (shuttingDown) return;

    let kind = 'wake';
    let profitable = false;
    let earnLine = null;
    if (skillResult.notFound) kind = 'skill_missing';
    else if (skillResult.timedOut) kind = 'skill_timeout';
    else if (skillResult.exitCode !== 0) kind = 'skill_error';
    else if (isEarnActionSlot(slot)) {
      // REQ-506/FIND-002: the always-act-widened classify gate — isEarnActionSlot, not isEarnSlot —
      // so economy/gig and economy/lending picks are classify-eligible on an engaged wake exactly
      // like any isEarnSlot member.
      const earnLedgerPath = defaultEarnLedgerPath(config);
      const result = await classifyEarnResult(wakeId, earnLedgerPath, isProfitable);
      profitable = result.profitable;
      earnLine = result.earnLine;
    }

    // Harness-failure detail (mirrors runOneWake's own step 9b, unmodified mechanism).
    const failureLayer = classifyLayer({ kind });
    if (failureLayer !== 'clean') {
      await appendHarnessFailure({
        ts, wakeId, slot, kind, layer: failureLayer,
        exitCode: skillResult.exitCode != null ? skillResult.exitCode : null,
        rawDetail: skillResult.output || '',
      });
    }

    // REQ-506: an isEarnActionSlot pick that produced NO realized earn-ledger line for this wake
    // triggers the bounded reroute. A skill_error/skill_timeout/skill_missing outcome (already routed
    // through appendHarnessFailure above) or a non-earn-action slot is accepted as this wake's
    // terminal result exactly like today's ordinary wake — this reroute is never triggered by an
    // execution error (REQ-506 edge case).
    if (kind === 'wake' && isEarnActionSlot(slot) && noRealizedAction(earnLine)) {
      // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the spec's own literal AC text reads
      // "preserved verbatim in the ledger" (behavioral-spec.md:493-495) — "the ledger" is a consistent
      // proper noun throughout this spec (REQ-510's own EARS clause, REQ-512's "append ... a ledger
      // line", line 777's "distinguishable from the ledger alone") for the file LEDGER_PATH/
      // formatRecord/safeAppend write to (state/ledger.jsonl) — never harness-failures.jsonl, which
      // REQ-508 always names explicitly by its literal filename whenever meant. This attempt's own
      // outcome is NOT this wake's terminal ledger.jsonl line (it may be silently superseded by a
      // successful reroute pick, or by REQ-508's own escalation record below) — without a SEPARATE,
      // distinctly-kinded ledger.jsonl line here, a guard-blocked pick's own skip reason (e.g.
      // sol-trade's kill-switch `{"action":"skip","reason":"..."}` trace) would never be recorded in
      // "the ledger" at all, violating REQ-509's AC. Written via the SAME formatRecord/safeAppend/
      // LEDGER_PATH machinery every other ledger line in this file already uses (never a new writer),
      // under a DISTINCT kind ('router_reroute_skip', never classifyLayer-routed — REQ-512's
      // harness-health.mjs per-slot health/escalation tracking reads ledger.jsonl `kind` values via
      // CLEAN_KINDS/SLOT_HEALTH_KINDS exclusively, neither of which includes this kind, so no false
      // health-classification collision is introduced). `skip_reason` carries `skillResult.output`
      // UNTAMPERED — only the SAME redactPrivateKeyPatterns pass every other ledger line already gets,
      // never truncated/whitespace-collapsed — satisfying "preserved verbatim". This write is
      // unconditional — every no-realized-action pick's own record is preserved this way, whether the
      // wake goes on to reroute successfully or escalate, so the guard-blocked slot's skip reason is
      // never silently lost either way. (No harness-failures.jsonl write here: a routine guard-skip is
      // not a harness failure — REQ-508 defines that file for the wake's TERMINAL exhausted-bound
      // failure case, a semantically different, never-in-flight event this is not.)
      const skipFields = buildAlwaysActLedgerFields({ wakeId, slot, args, attemptsUsed, realized: false });
      const skipRecordStr = formatRecord({
        ts,
        wake_id: skipFields.wake_id,
        kind: 'router_reroute_skip',
        slot: skipFields.slot,
        ...(args && Object.keys(args).length ? { args } : {}),
        attemptsUsed: skipFields.attemptsUsed,
        ...(skillResult.exitCode != null ? { exit_code: skillResult.exitCode } : {}),
        skip_reason: skillResult.output || '',
      });
      await safeAppend(LEDGER_PATH, redactPrivateKeyPatterns(skipRecordStr));

      const state = nextRerouteState({ attemptsUsed, maxAttempts: 1 });
      if (state.exhausted) {
        await writeAlwaysActEscalation({ wakeId, ts, sleepS, attemptsUsed });
        await sleepSecs(sleepS);
        return;
      }
      // FIND-101: the reroute target set hard-excludes the just-picked slot AND every risk:"capital"
      // slot — a capital-risking slot is NEVER a valid reroute target after a no-edge WAIT.
      const rerouteTargets = alwaysActMenu.filter((s) => s !== slot && isMarketRiskFree(s, riskTagOf));
      if (rerouteTargets.length === 0) {
        // REQ-506 edge case: the risk-free-filtered reroute set is empty -> zero additional think()
        // calls, immediate escalation — NEVER a fallback into a risk:"capital" reroute target.
        await writeAlwaysActEscalation({ wakeId, ts, sleepS, attemptsUsed });
        await sleepSecs(sleepS);
        return;
      }
      attemptsUsed = state.attemptsUsedNext;
      currentOfferedSlots = rerouteTargets; // FIND-201: THIS attempt's real offered/validity set
      reinforcement = null; // the reroute narrows the SCHEMA itself; no extra directive text needed
      continue;
    }

    // Terminal: this attempt's pick is this wake's realized (or clean-non-earn-action) result.
    const safeObservation = redactPrivateKeyPatterns(skillResult.output || '').slice(0, 1200);
    const ledgerFields = buildAlwaysActLedgerFields({ wakeId, slot, args, attemptsUsed, realized: kind === 'wake' && !noRealizedAction(earnLine) });
    const recordFields = {
      ts,
      wake_id: ledgerFields.wake_id,
      kind,
      sleep_s: sleepS,
      model: currentTier.model,
      slot: ledgerFields.slot,
      attemptsUsed: ledgerFields.attemptsUsed,
      ...(args && Object.keys(args).length ? { args } : {}),
      ...(kind === 'wake' ? { profitable } : {}),
      ...(skillResult.exitCode != null ? { exit_code: skillResult.exitCode } : {}),
      ...(safeObservation ? { result: safeObservation.replace(/\s+/g, ' ').slice(0, 900) } : {}),
    };
    const recordStr = formatRecord(recordFields);
    const safeRecord = redactPrivateKeyPatterns(recordStr);
    await safeAppend(LEDGER_PATH, safeRecord);
    await sleepSecs(sleepS);
    return;
  }
}

/**
 * Shared THINK-transport-failure handler: writes the SAME `kind:'wake_error'` ledger line +
 * harness-failure detail (SLEEP_ERROR_S backoff, not SLEEP_BASE_S) that runOneWake's own non-always-
 * act path writes, and sleeps — used by both the always-act and non-always-act code paths so a brain-
 * transport failure is recorded/backed-off identically regardless of which path hit it.
 */
async function writeWakeErrorAndSleep({ wakeId, ts, err }) {
  process.stderr.write(`[loop] THINK failed: ${err.message}\n`);
  const sleepS = cfgNum(config.SLEEP_ERROR_S, 60);
  // CLAUDE-P-1 (2026-07-17): this was hardcoded to the literal string 'proxy_down' for EVERY
  // wake_error regardless of brain or actual cause -- confirmed live: claude-p (brain=claude-p, no
  // proxy involved) logged 797 straight ledger lines reading error:"proxy_down" while the real cause
  // (visible only in daemon.err.log / appendHarnessFailure's rawDetail below) was `claude_exit_1`, an
  // expired Claude subscription OAuth session. That false label sent the first diagnosis pass looking
  // at the wrong subsystem. Surface the real err.message (already redacted below for harness-failure)
  // so the ledger's own wake_error line is truthful, not just the separate harness-failure detail.
  const record = formatRecord({
    ts, wake_id: wakeId, kind: 'wake_error', sleep_s: sleepS,
    error: redactPrivateKeyPatterns(err.message || 'unknown').slice(0, 200), model: currentTier.model,
  });
  await safeAppend(LEDGER_PATH, record);
  // R6: err.message is NOT already redacted anywhere upstream (raw HTTP-response body/subprocess-
  // stderr text from brain.mjs's thinkProxy/thinkClaudeP/httpPost) — this is the ONE new
  // redactPrivateKeyPatterns call site this feature introduces, before any further processing.
  await appendHarnessFailure({
    ts, wakeId, kind: 'wake_error', layer: 'brain_transport', exitCode: null,
    rawDetail: redactPrivateKeyPatterns(err.message || ''),
  });
  await sleepSecs(sleepS);
}

/**
 * REQ-508: the exhausted-bound terminal case — truthfully recorded (never a fabricated `profitable`
 * or success value, `slot: null`) and escalated via the existing appendHarnessFailure mechanism
 * (unmodified) so the existing self-heal escalation path can pick it up.
 *
 * `kind` (spec-review Phase 3 iter1 FIND-001 fix) distinguishes REQ-502's empty-menu terminal case
 * (`kind:'router_menu_empty'` — a spec violation: zero live earn-action slots resolved at menu
 * assembly, before any pick was even attempted) from EVERY other escalation trigger (an empty
 * risk-free reroute-target set, or the ordinary REQ-505/506/511/513 retry/reroute budget exhausted
 * with no realized earn-ledger line) — REQ-508's own EARS text names `kind:'router_no_realized_action'`
 * as the default example for those. Defaults to `'router_no_realized_action'` so every existing call
 * site (bounds-exhausted, empty-reroute-target-set) is unaffected; only the REQ-502 empty-menu call
 * site passes the distinct kind explicitly.
 */
async function writeAlwaysActEscalation({ wakeId, ts, sleepS, attemptsUsed, kind = 'router_no_realized_action' }) {
  const ledgerFields = buildAlwaysActLedgerFields({ wakeId, slot: null, args: {}, attemptsUsed, realized: false });
  const recordFields = {
    ts,
    wake_id: ledgerFields.wake_id,
    kind,
    sleep_s: sleepS,
    model: currentTier.model,
    slot: ledgerFields.slot,
    attemptsUsed: ledgerFields.attemptsUsed,
    profitable: false,
  };
  const recordStr = formatRecord(recordFields);
  const safeRecord = redactPrivateKeyPatterns(recordStr);
  await safeAppend(LEDGER_PATH, safeRecord);
  await appendHarnessFailure({
    ts, wakeId, kind, layer: 'router', exitCode: null,
    rawDetail: kind === 'router_menu_empty'
      ? 'always-act router: REQ-502 empty-menu spec violation — zero live earn-action slots resolved this wake'
      : 'always-act router: REQ-505/506/511/513 retry/reroute budget exhausted with no realized earn-ledger line this wake',
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Run a skill but also expose a kill function so SIGTERM can terminate it.
 */
async function runSkillWithKillRef(slot, args, wakeId, config, killRef) {
  // Import spawn to get the child PID for SIGTERM forwarding
  const { spawn } = await import('node:child_process');
  const { access } = await import('node:fs/promises');
  const { scrubPrivateKeys: scrub, scrubUserPIIEnv: scrubPII, redactPrivateKeyPatterns: redact } = await import('./env-filter.mjs');

  // One registry-aware resolver owns aliases and non-run.sh entrypoints alike.
  const skillPath = resolveSkillPath(slot, { ...config, LIFE_MANAGER_SKILLS_ROOT: SKILLS_ROOT });

  try { await access(skillPath); }
  catch { return { output: `${slot} skill not found`, exitCode: null, timedOut: false, notFound: true }; }

  const timeoutMs = cfgNum(config.SKILL_TIMEOUT_S, 120) * 1000;
  const childEnv = buildSkillEnv(slot, wakeId, config, scrub, scrubPII, args);

  return new Promise((resolve) => {
    let output = '';
    let timedOut = false;

    const proc = spawn(skillPath, [], {
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    // Expose kill function for SIGTERM handler
    killRef.kill = () => {
      try { proc.kill('SIGTERM'); } catch {}
      setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 5000);
    };

    proc.stdout.on('data', d => { output += d; });
    proc.stderr.on('data', d => { output += d; });

    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill('SIGTERM'); } catch {}
      setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 2000);
    }, timeoutMs);

    proc.on('exit', (code) => {
      clearTimeout(timer);
      killRef.kill = null;
      if (timedOut) {
        resolve({ output: `${slot} skill timeout`, exitCode: null, timedOut: true, notFound: false });
        return;
      }
      resolve({ output: redact(output), exitCode: code, timedOut: false, notFound: false });
    });

    proc.on('error', (err) => {
      clearTimeout(timer);
      killRef.kill = null;
      resolve({ output: `${slot} skill error: ${err.message}`, exitCode: null, timedOut: false, notFound: true });
    });
  });
}

function buildSkillEnv(slot, wakeId, config, scrub, scrubPII, args) {
  const base = scrubPII(scrub(process.env));
  const skillsStateRoot = config.LIFE_MANAGER_SKILLS_STATE_ROOT || path.join(ANICCA_HOME, 'state', 'skills');
  const earnStateRoot = config.EARN_STATE_ROOT || path.join(skillsStateRoot, 'earn');
  const earnLedger = config.EARN_LEDGER || path.join(earnStateRoot, 'earn-ledger.jsonl');
  // O4: pass the model's decision to EVERY skill as $ANICCA_ARGS (JSON). HARD RULE #0 = the skill is
  // the tool, the MODEL decides the strategy/params; the skill reads its decision here. Optional —
  // skills keep a safe default when args is absent.
  const a = (args && typeof args === 'object') ? args : {};
  const ANICCA_ARGS = JSON.stringify(a);
  // PATCH 6: each earn ACTION is its own slot now — the SLOT names the strategy (yield/hl/x402/token).
  // `earn` stays as the back-compat fat tool that reads args.strategy.
  // isEarnSlot = legacy action slots {earn,yield,hl_trade,x402_sell,token_launch} ∪ per-method earn/<sub>.
  // earnStrategyFor: action slots → their map value; fat `earn` → null (keeps the args.strategy||yield
  // fallback below); earn/<sub> → '<sub>'. So every earn slot — incl gig/clip/affiliate/video — gets
  // EARN_LEDGER/EARN_MODE/WAKE_ID and can write the earn-ledger line the classify gate reads.
  if (isEarnSlot(slot)) {
    return {
      ...base,
      ANICCA_ARGS,
      EARN_MODE:     process.env.EARN_MODE     || 'execute',
      EARN_STRATEGY: process.env.EARN_STRATEGY || earnStrategyFor(slot) || (typeof a.strategy === 'string' && a.strategy.trim() ? a.strategy.trim() : 'yield'),
      WAKE_ID:       wakeId,
      LIFE_MANAGER_SKILLS_STATE_ROOT: skillsStateRoot,
      EARN_STATE_ROOT: earnStateRoot,
      EARN_LEDGER: earnLedger,
    };
  }
  return { ...base, ANICCA_ARGS, WAKE_ID: wakeId, LIFE_MANAGER_SKILLS_STATE_ROOT: skillsStateRoot, EARN_STATE_ROOT: earnStateRoot, EARN_LEDGER: earnLedger };
}

/**
 * anicca-harness-tooluse-health R6: append one JSON line to $ANICCA_HOME/state/harness-failures.jsonl
 * via the EXISTING appendLedgerLine primitive (never a new writer). `slot` is omitted entirely for
 * brain_transport (R1: brain-transport failures precede tool selection). `detail` is
 * capFailureDetail(rawDetail) — whitespace-collapsed and capped at 4000 chars, distinct from and
 * never affecting ledger.jsonl's own 900-char `result` cap (INV-NO-PROMPT-REGRESSION).
 */
async function appendHarnessFailure({ ts, wakeId, slot, kind, layer, exitCode, rawDetail }) {
  const detail = capFailureDetail(rawDetail);
  const fields = {
    ts,
    wake_id: wakeId,
    ...(slot != null ? { slot } : {}),
    kind,
    layer,
    exit_code: exitCode != null ? exitCode : null,
    detail,
  };
  try {
    await appendLedgerLine(HARNESS_FAILURES_PATH, formatRecord(fields));
  } catch (err) {
    process.stderr.write(`[loop] harness-failures append failed: ${err.message}\n`);
  }
}

async function safeAppend(ledgerPath, line) {
  try {
    await appendLedgerLine(ledgerPath, line);
  } catch (err) {
    process.stderr.write(`[loop] Ledger append failed: ${err.message}\n`);
    // Sleep error period; do not crash
    await sleepSecs(config.SLEEP_ERROR_S ?? 60);
  }
}

function sleepSecs(s) {
  if (singleWake) return Promise.resolve();
  const ms = Math.max(0, Math.floor(Number(s) * 1000));
  return new Promise(r => setTimeout(r, ms));
}

/** Safely read a numeric config value, using fallback only when value is null/undefined (not when 0). */
function cfgNum(val, fallback) {
  return val != null ? Number(val) : fallback;
}
