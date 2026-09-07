/**
 * ledger-publish.mjs — Effectful (+ pure core): per-wake, best-effort commit+push of BOTH this
 * instance's ledger evidence sources into github.com/Daisuke134/life-manager, so "the balance/actions grow
 * every hour" is third-party-verifiable from git history alone -- WITHOUT ever touching the git
 * working tree/index/branches the loop itself (or a sibling process such as evolve.mjs's promote())
 * is concurrently using.
 *
 * franklin-ledger-push (P2) — spec: .vcsdd/features/franklin-ledger-push/specs/behavioral-spec.md
 * (REQ-701..710).
 *
 * impl-review iter3 fixes (this revision, FIND-001..003 — new numbering, distinct findings from
 * iter2's own FIND-001..006 documented further below):
 *
 *   FIND-001 (critical): `projectEarnField()`'s earn allowlist dropped `external`/`confirmed`/
 *   `fill_tid` entirely. `skills/_shared/lib/ledger.mjs::isProfitable()` — the repo's SINGLE source
 *   of truth for "is this earn-ledger line a real, GATE-0 profitable wake" — requires
 *   `external===true` unconditionally plus a chain-correct confirmation (`tx`+`status==='0x1'` for
 *   EVM, `sig`+`confirmed===true` for Solana, `fill_tid`+`confirmed===true` for Hyperliquid). Without
 *   these three fields, `isProfitable()` returns `false` for EVERY published earn line regardless of
 *   the source line's real profitability, so a third party cloning the branch could see dollar
 *   amounts but never run the project's own classifier against them. Fixed: `external`/`confirmed`
 *   (boolean-typed) and `fill_tid` (a settled Hyperliquid fill id — a finite number, or a bounded
 *   identifier-shaped string) are now preserved, type/shape-checked, never routed through
 *   free-text redaction (they are structured settlement references, not secrets).
 *
 *   FIND-002 (major): in the push-rejection retry branch, `pushed = true` was set unconditionally
 *   after the retry's try block, even on the path where the post-reset reconcile found nothing
 *   pending and NO `git push` was actually re-issued — misreporting a push as confirmed (and
 *   advancing `lastPushTs`/resetting `publishFailureStreak`) when no push call ran in that branch.
 *   Fixed: `pushed = true` now only executes immediately after the retry's own `git push` call, inside
 *   the `if (result.pendingLineCount > 0)` guard.
 *
 *   FIND-003: `sig` (Solana tx signature) previously only got a bare length/type check
 *   (`1 <= length <= 200`), not real shape validation — unlike `tx`'s hash-shape regex. Fixed: `sig`
 *   is now validated against the same base58, 64-88-char shape `record-swap.mjs` actually writes
 *   (`SIG_VALUE`), giving it the same shape-validation discipline REQ-706 already claimed it had.
 *
 * impl-review iter2 fixes (prior revision, FIND-001..006):
 *
 *   FIND-001 (money evidence never published): the ORIGINAL design read only `state/ledger.jsonl`
 *   (per-wake bookkeeping — kind/slot/exit_code, never a dollar amount). The actual money fields
 *   (`net_usdc`/`earn_usdc`/`cost_usdc`/`tx`/`sig`/`status`/`chain`) live EXCLUSIVELY in a separate
 *   file, `skills/earn/state/earn-ledger.jsonl` (written by skills/earn/lib/record.mjs via
 *   skills/_shared/lib/ledger.mjs::deriveLine, e.g. skills/earn/sol-trade/lib/record-swap.mjs:48-55).
 *   This revision publishes BOTH sources onto the SAME per-instance branch as two separate files —
 *   `<instance>-wake.jsonl` (from `state/ledger.jsonl`) and `<instance>-earn.jsonl` (from
 *   `skills/earn/state/earn-ledger.jsonl`) — each with its own field allowlist and its own
 *   independently-reconciled cursor (SOURCES registry below).
 *
 *   FIND-002 (silent gap: a lost/recreated publish-repo directory produces a CLEAN fast-forward
 *   push, never a rejection, so the old design's rejection-only recovery never fired and the marker
 *   falsely claimed the lost lines were pushed): this revision NEVER trusts the marker's cached
 *   `pushedLineCount` as ground truth. Every cycle, AFTER ensurePublishRepo() syncs the dedicated
 *   clone's working tree to origin's REAL current tip (`checkout -B branch origin/branch`, or a
 *   guaranteed-empty state pre-first-push — see ensurePublishRepo's own doc comment), the ACTUAL
 *   line count of the just-synced destination file IS this cycle's `pushedLineCount` for that
 *   source — unconditionally (reconcileSource()). This single reconciliation simultaneously covers
 *   both directions the spec's REQ-709 describes: a cached value that was too HIGH (commits actually
 *   lost, e.g. the publish-repo directory was deleted/recreated between cycles) is healed DOWNWARD to
 *   the real count; a cached value that was too LOW (the marker itself was lost/reset while origin
 *   already had more content) is adopted UPWARD to the real count. The marker is now purely a cache
 *   for decidePublish()'s timing math — never the source of truth for "what's actually on origin".
 *   To make "actual published line count" a valid proxy for "source lines processed" at all, every
 *   processed source line ALWAYS produces exactly one destination line (a `{}` placeholder for a
 *   malformed/non-object source line, never a silent drop from the position sequence) — this 1:1
 *   index alignment is what makes reconcileSource()'s line-count reconciliation sound.
 *
 *   FIND-003 (unrealistic divergence trigger): the reachable, realistic trigger for this class of bug
 *   in THIS single-writer-per-branch topology (REQ-705) is publishRepoDir loss/recreation between
 *   cycles, not an outside writer colliding on the same exclusive branch. The test suite now proves
 *   recovery against the REAL trigger: delete publishRepoDir entirely between two cycles and assert
 *   every source line still ends up on the branch exactly once.
 *
 *   FIND-004 (fabricated precedent citation): the only real sibling lock precedent in this repo is
 *   `skills/self/claude-p-mainloop.sh`'s PIDFILE guard (its own header explicitly says "NOT flock").
 *   `scripts/disk-cleaner.sh` does not exist anywhere in this repository — that citation is removed.
 *
 *   FIND-005 (no escalation, no reachability check): publishLedgerCycle() now tracks
 *   `publishFailureStreak` in the marker — incremented on a setup failure or a push-attempt failure
 *   (both the primary attempt and its one re-sync retry), reset to 0 the moment ensurePublishRepo()
 *   itself succeeds (proof the auth/clone/fetch/checkout path is healthy this cycle, independent of
 *   whether there was anything new to push). The streak is returned to the caller
 *   (`publishFailureStreak`); index.mjs's wiring escalates via the EXISTING appendHarnessFailure
 *   mechanism once it reaches 5 consecutive failures (kind: 'ledger_publish_stuck'), so a stuck
 *   pipeline (e.g. a revoked/read-only git credential) surfaces to healthchecks instead of failing
 *   silently on stderr forever. A ONE-TIME, non-fatal `git ls-remote` reachability probe runs right
 *   before the FIRST-EVER dedicated-clone setup (ensurePublishRepo's own "setup" phase) and logs
 *   clearly (no secrets — the origin URL for this repo carries no embedded token, auth is via the
 *   host-global `gh auth git-credential` helper) if it fails.
 *
 *   FIND-006 (unbounded full-history clone): the dedicated clone is now `--depth 1 --single-branch
 *   --no-tags`; every subsequent fetch of the per-instance branch uses an explicit `src:dst` refspec
 *   (`+refs/heads/<branch>:refs/remotes/origin/<branch>`) WITH `--depth 1 --no-tags` too, so the
 *   remote-tracking ref for a branch outside the clone's default single-branch config is still
 *   reliably created/updated (explicit refspecs always do this regardless of the configured default
 *   fetch refspec), and the clone never silently deepens/unshallows across cycles.
 *
 * Pure core (no I/O, directly unit-testable):
 *   - decidePublish()             — REQ-704 push throttle decision.
 *   - extractWakeId()/extractEarnRef() — parse a source line's own id field for the commit message.
 *   - projectWakeLine()/projectEarnLine() — per-source field-allowlist projection (FIND-001/003):
 *     parses one raw source line and returns ONLY an explicit allowlist of known-safe fields,
 *     type-checked; every other field (including the model-authored `args` object) is DROPPED,
 *     fail-closed. Free-text fields are redacted (see below) and capped at 200 chars. Malformed/
 *     non-object JSON -> null (the ORCHESTRATOR substitutes a `{}` placeholder for it — see FIND-002
 *     above — the pure function itself keeps its "never publish raw garbage" contract).
 *   - redactBroaderSecretPatterns() — a second, STRICTER redaction pass applied only to the
 *     free-text fields the projections allow through (`result`/`skip_reason` for wake, `task` for
 *     earn) -- covers base58 64-88 char runs (Solana secret-key shape) and generic 40+ hex char runs
 *     (a stricter bar than env-filter.mjs's own 64-hex-only contract, deliberately: content bound for
 *     a PUBLIC git branch gets a higher redaction floor than content that only ever stays local).
 *   - Reused: redactPrivateKeyPatterns (env-filter.mjs, unmodified).
 *
 * Effectful shell:
 *   - readMarker/writeMarker      — throttle/cursor state at $ANICCA_HOME/state/.ledger-publish-marker
 *     (now `{ wake: {pushedLineCount}, earn: {pushedLineCount}, lastPushTs, publishFailureStreak }`).
 *   - readLinesOrEmpty/appendRawLines — jsonl read (source or destination file), fs append.
 *   - acquireLock/releaseLock     — REQ-708 same-instance-overlap guard: an mkdir-atomic lock at
 *     $ANICCA_HOME/state/.ledger-publish-<instance>.lock with pid-staleness reclaim, copied from this
 *     repo's own established idiom — skills/self/claude-p-mainloop.sh's pidfile guard (FIND-004).
 *   - ensurePublishRepo           — REQ-705 (FIND-001/002/006): idempotently sets up the DEDICATED,
 *     shallow clone at `publishRepoDir`, synced to (or freshly, deterministically empty-orphan for)
 *     `ledger-<instance>`. Every git call in this function runs with cwd=publishRepoDir — repoRoot is
 *     passed nowhere near it.
 *   - reconcileSource             — FIND-002: derives this cycle's ground-truth `pushedLineCount` for
 *     ONE source directly from the just-synced destination file's actual line count, never the marker.
 *   - defaultGit                  — child_process wrapper (injectable via opts.git for tests).
 *   - publishLedgerCycle          — the orchestrator; the ONLY function index.mjs calls.
 *
 * REQ-703 (non-fatality) is enforced at multiple layers: every individual git call is wrapped in its
 * own try/catch with a specific `reason`, the lock is released in a `finally`, and the whole function
 * body is wrapped in an outermost try/catch so publishLedgerCycle() can never throw/reject under any
 * input, ever.
 */

import { promises as fs } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { redactPrivateKeyPatterns } from './env-filter.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// REQ-704: 10 new committed lines OR 15 minutes since the last successful push, whichever first.
export const DEFAULT_MIN_LINES = 10;
export const DEFAULT_MIN_INTERVAL_MS = 15 * 60 * 1000;

/**
 * decidePublish(...) — pure REQ-704 throttle decision. No I/O.
 *
 * @param {{pendingLineCount: number, lastPushTs: number, nowMs: number, minLines?: number, minIntervalMs?: number}} args
 * @returns {{shouldPush: boolean, reason: string}}
 */
export function decidePublish({
  pendingLineCount,
  lastPushTs,
  nowMs,
  minLines = DEFAULT_MIN_LINES,
  minIntervalMs = DEFAULT_MIN_INTERVAL_MS,
}) {
  const pending = Number(pendingLineCount) || 0;
  if (pending <= 0) return { shouldPush: false, reason: 'no-pending-lines' };
  if (pending >= minLines) return { shouldPush: true, reason: 'line-threshold' };
  const elapsed = Number(nowMs) - Number(lastPushTs || 0);
  if (elapsed >= minIntervalMs) return { shouldPush: true, reason: 'time-threshold' };
  return { shouldPush: false, reason: 'throttled' };
}

/**
 * extractRecordId(line, idField) — pure. Parses a single jsonl line and returns its `idField`
 * string value, or 'unknown' for anything malformed/missing/non-string (never throws).
 *
 * @param {string} line
 * @param {string} idField
 * @returns {string}
 */
export function extractRecordId(line, idField) {
  try {
    const parsed = JSON.parse(line);
    const v = parsed ? parsed[idField] : undefined;
    return typeof v === 'string' && v ? v : 'unknown';
  } catch {
    return 'unknown';
  }
}

/** extractWakeId(line) — wake source's own id field (`wake_id`). Used in commit messages. */
export function extractWakeId(line) {
  return extractRecordId(line, 'wake_id');
}

/** extractEarnRef(line) — earn source's own id field (`wake`, per earn-ledger.jsonl's schema). */
export function extractEarnRef(line) {
  return extractRecordId(line, 'wake');
}

// ── Field-allowlist projections (pure) ──────────────────────────────────────────────────────────

// Matches self-eval.mjs's OWN real earn-ledger field-naming convention (net_usdc/cost_usdc/earn_usdc)
// generalised to any net_*/earn_*/cost_* numeric field a wake-ledger line may carry.
const NUMERIC_STAT_FIELD = /^(net|earn|cost)_[A-Za-z0-9]+$/;
// Matches evolve.mjs's own earn-ledger `tx` field convention (a tx hash), plus tx_hash/txHash spellings.
const TX_HASH_FIELD = /^tx([_-]?hash)?$/i;
const TX_HASH_VALUE = /^0x[0-9a-fA-F]{6,64}$/;

// Base58 (Bitcoin/Solana alphabet — excludes 0/O/I/l) run of 64-88 chars: the shape of a Solana
// base58-encoded secret key. A generic 40+ hex run: a stricter bar than env-filter.mjs's 64-hex-
// only PRIVKEY_PATTERN, deliberately, since this text is bound for a PUBLIC git branch.
const BASE58_SECRET_RUN = /[1-9A-HJ-NP-Za-km-z]{64,88}/g;
const HEX_40PLUS_RUN = /(0x)?[0-9a-fA-F]{40,}/g;

// impl-review iter3 FIND-003: `sig` (a Solana tx signature) is base58-alphabet, 64-88 chars — the
// SAME shape record-swap.mjs actually writes (skills/earn/sol-trade/lib/record-swap.mjs, verified
// live). This gives `sig` real shape validation, the same discipline TX_HASH_VALUE already gives
// `tx`, instead of the previous bare length/type check.
const SIG_VALUE = /^[1-9A-HJ-NP-Za-km-z]{64,88}$/;

// impl-review iter4 FIND-001: `fill_tid` is Hyperliquid's per-fill settlement id
// (skills/earn/hl-trade/lib/reconcile.py:139-148, `fill["tid"]`) — verified live as a JSON
// integer (the userFills API's own numeric trade id), never a string, in every real HL-sourced
// earn-ledger line, and `record.mjs`/`deriveLine` perform no field-shape validation that would
// reject a future string-typed value before it reaches the ledger. Fail-closed to what real
// writers actually produce today: accept ONLY a finite number. A prior iteration also accepted a
// bounded alphanumeric/`:`/`-`/`_` string form "in case a future writer serializes it as a
// string" — that branch was strictly broader than a settlement-reference shape (128 chars of
// alphanumeric+punctuation is API-key/token shaped) and was exempt from BOTH redaction layers, so
// it was removed rather than tightened: no real writer needs it, and `isProfitable()`
// (skills/_shared/lib/ledger.mjs:62, `line.fill_tid != null`) only requires presence, not string
// form. If a future writer genuinely needs a string fill_tid, that requires a new, narrowly-scoped
// allowlist change reviewed against the writer that actually produces it — not a speculative one.

/**
 * redactBroaderSecretPatterns(str) — pure. Second, stricter redaction pass for free-text fields
 * only, applied ON TOP OF redactPrivateKeyPatterns.
 *
 * @param {string} str
 * @returns {string}
 */
export function redactBroaderSecretPatterns(str) {
  if (typeof str !== 'string') return String(str ?? '');
  return str.replace(BASE58_SECRET_RUN, '[REDACTED]').replace(HEX_40PLUS_RUN, '[REDACTED]');
}

function redactFreeText(value) {
  if (typeof value !== 'string') return undefined;
  const passOne = redactPrivateKeyPatterns(value);
  const passTwo = redactBroaderSecretPatterns(passOne);
  return passTwo.slice(0, 200);
}

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function projectRecord(rawLine, projectField) {
  let parsed;
  try {
    parsed = JSON.parse(rawLine);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const out = {};
  for (const [key, value] of Object.entries(parsed)) {
    const projected = projectField(key, value);
    if (projected !== undefined) out[key] = projected;
  }
  return JSON.stringify(out);
}

function projectWakeField(key, value) {
  switch (key) {
    case 'ts': return isFiniteNumber(value) ? value : undefined;
    case 'wake_id': return typeof value === 'string' ? value : undefined;
    case 'kind': return typeof value === 'string' ? value : undefined;
    case 'slot': return typeof value === 'string' || value === null ? value : undefined;
    case 'attemptsUsed': return isFiniteNumber(value) ? value : undefined;
    case 'profitable': return typeof value === 'boolean' ? value : undefined;
    case 'exit_code': return isFiniteNumber(value) || value === null ? value : undefined;
    case 'sleep_s': return isFiniteNumber(value) ? value : undefined;
    case 'model': return typeof value === 'string' ? value : undefined;
    case 'result': return redactFreeText(value);
    case 'skip_reason': return redactFreeText(value);
    default:
      if (NUMERIC_STAT_FIELD.test(key)) return isFiniteNumber(value) ? value : undefined;
      if (TX_HASH_FIELD.test(key)) return typeof value === 'string' && TX_HASH_VALUE.test(value) ? value : undefined;
      return undefined; // fail-closed: every other field (incl. the model-authored `args` object) is DROPPED.
  }
}

/**
 * projectWakeLine(rawLine) — pure. Parses one raw `state/ledger.jsonl` line and returns a JSON
 * string (no trailing newline) containing ONLY the explicit field allowlist above, or `null` if the
 * line isn't parseable JSON / isn't a plain object.
 *
 * @param {string} rawLine
 * @returns {string|null}
 */
export function projectWakeLine(rawLine) {
  return projectRecord(rawLine, projectWakeField);
}

// FIND-001: the earn-ledger's own real schema (skills/_shared/lib/ledger.mjs::deriveLine,
// verified live against $ANICCA_HOME/state/earn/earn-ledger.jsonl): ts, wallet, source, task,
// earn_usdc, cost_usdc, net_usdc, wake, plus tx/status (EVM) or sig/confirmed/chain (Solana) or
// fill_tid/confirmed/chain (Hyperliquid) on real on-chain lines. This allowlist publishes the
// money-evidence fields structurally (public wallet address, public tx/sig/fill_tid on-chain/
// settlement references, numeric $ amounts, the id/source labels) and redacts the one free-text
// field (`task`) exactly like the wake source's `result`/`skip_reason`.
//
// impl-review iter3 FIND-001 (critical): `external`/`confirmed`/`fill_tid` were previously dropped
// here, which meant `skills/_shared/lib/ledger.mjs::isProfitable()` — the repo's SINGLE source of
// truth for "is this a real, GATE-0 profitable earn" (external===true AND a chain-correct
// confirmation: `tx`+`status==='0x1'` for EVM, `sig`+`confirmed===true` for Solana, or
// `chain==='hyperliquid'`+`fill_tid`+`confirmed===true` for Hyperliquid) — could NEVER return true
// against the published branch: `external` alone unconditionally gates the whole function to false,
// Solana lines additionally lost `confirmed`, and Hyperliquid lines lost BOTH of their only two
// settlement-proof fields (`fill_tid` is HL's sole on-chain-equivalent reference — `tx`/`sig` do not
// apply to HL fills at all). All three are now preserved, each type/shape-checked, so a third party
// can run the project's own `isProfitable()` against the published earn line and get the SAME
// verdict the real ledger would give.
function projectEarnField(key, value) {
  switch (key) {
    case 'ts': return isFiniteNumber(value) ? value : undefined;
    case 'wallet': return typeof value === 'string' ? value : undefined;
    case 'source': return typeof value === 'string' ? value : undefined;
    case 'wake': return typeof value === 'string' ? value : undefined;
    case 'earn_usdc': return isFiniteNumber(value) ? value : undefined;
    case 'cost_usdc': return isFiniteNumber(value) ? value : undefined;
    case 'net_usdc': return isFiniteNumber(value) ? value : undefined;
    // tx (EVM) / sig (Solana) / fill_tid (Hyperliquid) are PUBLIC on-chain/settlement references,
    // not secrets — validated by shape/type instead of routed through free-text redaction (same
    // treatment REQ-702/706 already give `tx` on the wake source).
    case 'tx': return typeof value === 'string' && TX_HASH_VALUE.test(value) ? value : undefined;
    case 'sig': return typeof value === 'string' && SIG_VALUE.test(value) ? value : undefined;
    case 'status': return typeof value === 'string' ? value : undefined;
    case 'chain': return typeof value === 'string' ? value : undefined;
    case 'task': return redactFreeText(value);
    // FIND-001 (iter3): required by isProfitable() — dropping any of these three silently makes
    // EVERY published earn line unclassifiable as profitable, regardless of the source line's truth.
    case 'external': return typeof value === 'boolean' ? value : undefined;
    case 'confirmed': return typeof value === 'boolean' ? value : undefined;
    // impl-review iter4 FIND-001: number-only — the only shape any real writer produces (see
    // comment above the removed SETTLEMENT_ID_VALUE regex); a string form is fail-closed DROPPED.
    case 'fill_tid': return isFiniteNumber(value) ? value : undefined;
    default: return undefined; // fail-closed, same allowlist discipline as the wake source.
  }
}

/**
 * projectEarnLine(rawLine) — pure. Parses one raw `skills/earn/state/earn-ledger.jsonl` line and
 * returns a JSON string containing ONLY the explicit earn allowlist above, or `null` if the line
 * isn't parseable JSON / isn't a plain object.
 *
 * @param {string} rawLine
 * @returns {string|null}
 */
export function projectEarnLine(rawLine) {
  return projectRecord(rawLine, projectEarnField);
}

// ── Per-source registry (FIND-001: both sources published to the same branch, distinct files) ────

const SOURCES = {
  wake: { fileName: (instance) => `${instance}-wake.jsonl`, projectLine: projectWakeLine, extractRef: extractWakeId, label: 'wake' },
  earn: { fileName: (instance) => `${instance}-earn.jsonl`, projectLine: projectEarnLine, extractRef: extractEarnRef, label: 'earn' },
};

// ── Effectful shell ──────────────────────────────────────────────────────────────────────────────

function defaultGit(args, cwd) {
  return execFileSync('git', args, { cwd, encoding: 'utf8' });
}

const MARKER_SOURCE_DEFAULTS = { pushedLineCount: 0 };
const MARKER_DEFAULTS = { wake: { ...MARKER_SOURCE_DEFAULTS }, earn: { ...MARKER_SOURCE_DEFAULTS }, lastPushTs: 0, publishFailureStreak: 0 };

function normalizeSourceMarker(raw) {
  return { pushedLineCount: raw && Number.isInteger(raw.pushedLineCount) ? raw.pushedLineCount : 0 };
}

async function readMarker(markerPath) {
  try {
    const raw = await fs.readFile(markerPath, 'utf8');
    const parsed = JSON.parse(raw);
    return {
      wake: normalizeSourceMarker(parsed.wake),
      earn: normalizeSourceMarker(parsed.earn),
      lastPushTs: Number.isFinite(parsed.lastPushTs) ? parsed.lastPushTs : 0,
      publishFailureStreak: Number.isInteger(parsed.publishFailureStreak) ? parsed.publishFailureStreak : 0,
    };
  } catch {
    // impl-review iter4 FIND-002: derive from MARKER_DEFAULTS (single source of truth for the
    // shape) instead of hand-duplicating its literal, so the two cannot silently drift out of
    // sync. A fresh deep copy (not the shared MARKER_DEFAULTS object itself) so callers mutating
    // the returned marker never leak state into the next readMarker() call.
    return { wake: { ...MARKER_DEFAULTS.wake }, earn: { ...MARKER_DEFAULTS.earn }, lastPushTs: MARKER_DEFAULTS.lastPushTs, publishFailureStreak: MARKER_DEFAULTS.publishFailureStreak };
  }
}

async function writeMarker(markerPath, marker) {
  await fs.mkdir(path.dirname(markerPath), { recursive: true });
  await fs.writeFile(markerPath, JSON.stringify(marker) + '\n');
}

async function readLinesOrEmpty(filePath) {
  let raw;
  try {
    raw = await fs.readFile(filePath, 'utf8');
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
  return raw.split('\n').filter((l) => l.trim().length > 0);
}

async function countLines(filePath) {
  return (await readLinesOrEmpty(filePath)).length;
}

async function appendRawLines(destPath, lines) {
  if (lines.length === 0) return;
  await fs.mkdir(path.dirname(destPath), { recursive: true });
  await fs.appendFile(destPath, lines.map((l) => l + '\n').join(''));
}

async function pathExists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

async function ensureReadme(readmePath, instance) {
  if (await pathExists(readmePath)) return false;
  const content =
    `# Life Manager ledger-publish — ${instance}\n\n` +
    `Append-only, field-allowlisted projection of this instance's two ledger evidence sources.\n` +
    `Machine-generated by \`runtime/loop/ledger-publish.mjs\` (feature: franklin-ledger-push).\n\n` +
    `- \`${instance}-wake.jsonl\` — per-wake bookkeeping, projected from \`state/ledger.jsonl\`.\n` +
    `- \`${instance}-earn.jsonl\` — money evidence (earn/cost/net $, on-chain tx/sig refs), projected\n` +
    `  from \`skills/earn/state/earn-ledger.jsonl\`.\n\n` +
    `Unknown fields are dropped before publish; free-text fields are redacted. This branch is\n` +
    `dedicated to instance \`${instance}\` only — see github.com/Daisuke134/life-manager.\n`;
  await fs.mkdir(path.dirname(readmePath), { recursive: true });
  await fs.writeFile(readmePath, content);
  return true;
}

// ── Same-instance overlap guard: mkdir-atomic lock with pid-staleness reclaim ──────────────────────
// FIND-004: the only REAL sibling precedent in this repo is skills/self/claude-p-mainloop.sh's own
// pidfile guard (its header explicitly documents "NOT flock — macOS has no flock(1) binary"). The
// previously-cited `scripts/disk-cleaner.sh` does not exist anywhere in this repository.

function isProcessAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function acquireLock(lockDir) {
  try {
    await fs.mkdir(lockDir);
  } catch (err) {
    if (err.code !== 'EEXIST') throw err;
    let stale = false;
    try {
      const pidRaw = await fs.readFile(path.join(lockDir, 'pid'), 'utf8');
      const pid = Number(pidRaw.trim());
      stale = !Number.isInteger(pid) || !isProcessAlive(pid);
    } catch {
      stale = true; // no readable pid file — treat as a stale/crashed lock, safe to reclaim.
    }
    if (!stale) return false; // a live process genuinely holds the lock — caller must skip this cycle.
    await fs.rm(lockDir, { recursive: true, force: true });
    await fs.mkdir(lockDir);
  }
  await fs.writeFile(path.join(lockDir, 'pid'), String(process.pid));
  return true;
}

async function releaseLock(lockDir) {
  try {
    await fs.rm(lockDir, { recursive: true, force: true });
  } catch {
    // best-effort — a failed unlock is not fatal (next cycle's staleness check reclaims it).
  }
}

// ── FIND-005: one-time, non-fatal origin-reachability probe at first-ever setup ────────────────────

/**
 * checkOriginReachability(...) — best-effort, NEVER throws. Runs `git ls-remote` against the
 * resolved origin URL so an auth/network problem is logged CLEARLY (not just as a downstream clone
 * failure) — no secrets are logged (the origin URL for this repo carries no embedded token; auth is
 * via the host-global `gh auth git-credential` helper, never printed here).
 */
function checkOriginReachability(originUrl, git, log) {
  try {
    git(['ls-remote', '--exit-code', originUrl, 'HEAD']);
  } catch (err) {
    const firstLine = String(err && err.message ? err.message : err).split('\n')[0].slice(0, 300);
    log(`[ledger-publish] origin reachability check failed (auth/network?) — publishing will likely fail until this is resolved: ${firstLine}\n`);
  }
}

// ── FIND-001/002/006: dedicated per-instance orphan publish branch, shallow clone ──────────────────

/**
 * ensurePublishRepo(...) — idempotently sets up the DEDICATED, shallow clone at `publishRepoDir`, on
 * branch `ledger-<instance>`. Every git call here runs with cwd=publishRepoDir; `repoRoot`/the shared
 * checkout is NEVER passed to this function or touched by it.
 *
 * FIND-002: when the remote branch already exists, `checkout -B branch origin/branch` syncs the
 * local working tree to origin's REAL current tip — this is what makes reconcileSource()'s
 * "actual file line count = ground truth" reconciliation valid. When the remote branch does NOT yet
 * exist (no first-ever push has landed for this instance), this function FORCES a deterministic,
 * guaranteed-EMPTY orphan state on every such call — it never trusts locally-lingering
 * committed-but-never-confirmed content from a prior cycle as truth, which would otherwise make
 * reconcileSource() reconcile against stale local state instead of origin.
 */
async function ensurePublishRepo({ publishRepoDir, originUrl, branch, instance, git, log }) {
  const gitMetaDir = path.join(publishRepoDir, '.git');
  if (!(await pathExists(gitMetaDir))) {
    await fs.mkdir(path.dirname(publishRepoDir), { recursive: true });
    checkOriginReachability(originUrl, git, log); // FIND-005: one-time, right at first-ever setup.
    // FIND-006: shallow + single-branch + no-tags — this clone only ever needs to read/write ONE
    // small orphan branch's tip, never the mother repo's full history across every branch.
    git(['clone', '--no-checkout', '--quiet', '--depth', '1', '--single-branch', '--no-tags', originUrl, publishRepoDir]);
  }

  let remoteBranchExists = true;
  try {
    // Explicit src:dst refspec — ALWAYS creates/updates refs/remotes/origin/<branch>, regardless of
    // this clone's single-branch default (which only knows the remote's default HEAD branch, e.g.
    // 'main', not our per-instance ledger-<instance> branch). --depth 1 keeps every subsequent fetch
    // shallow too (FIND-006) — it never silently deepens/unshallows this clone.
    git(['fetch', '--quiet', '--depth', '1', '--no-tags', 'origin', `+refs/heads/${branch}:refs/remotes/origin/${branch}`], publishRepoDir);
  } catch {
    remoteBranchExists = false;
  }

  if (remoteBranchExists) {
    git(['checkout', '-B', branch, `origin/${branch}`], publishRepoDir);
    return;
  }

  // Pre-first-push: force a clean, deterministic EMPTY state every cycle (FIND-002).
  let currentBranch = '';
  try {
    currentBranch = git(['branch', '--show-current'], publishRepoDir).trim();
  } catch {
    currentBranch = '';
  }
  if (currentBranch !== branch) {
    try {
      git(['checkout', '--orphan', branch], publishRepoDir);
    } catch {
      git(['checkout', branch], publishRepoDir);
    }
  }
  for (const cfg of Object.values(SOURCES)) {
    await fs.rm(path.join(publishRepoDir, cfg.fileName(instance)), { force: true });
  }
  await fs.rm(path.join(publishRepoDir, 'README.md'), { force: true });
}

/**
 * reconcileSource(...) — FIND-002: derives this source's ground-truth `pushedLineCount` for THIS
 * cycle directly from the just-synced destination file's actual line count (never the marker's
 * cache), then slices `sourceLines` from that point onward.
 */
async function reconcileSource({ cfg, publishRepoDir, sourceLines, instance }) {
  const relDest = cfg.fileName(instance);
  const destPath = path.join(publishRepoDir, relDest);
  const actualPublishedLineCount = await countLines(destPath);
  const newLines = sourceLines.slice(actualPublishedLineCount);
  return { cfg, destPath, relDest, actualPublishedLineCount, newLines };
}

/**
 * appendAndCommit(...) — projects + appends each source's `newLines` (FIND-002: a `{}` placeholder
 * for any line the pure projector dropped as malformed, preserving 1:1 index alignment) onto its
 * destination file, then a SINGLE path-scoped `git add -- <touched paths>` + commit covering
 * whichever source(s) actually had new content this cycle (never a bare `git add -A`/`git commit -a`).
 *
 * REQ-703: the `add`/`commit` step is its OWN try/catch (never allowed to fall through to
 * publishLedgerCycle's generic outermost 'error' catch) — a commit failure (e.g. a stray lock file)
 * is logged and non-fatal; `committed:false` tells the caller the new lines were appended to disk but
 * are NOT yet part of any commit, so they must never be treated as push-eligible/confirmed.
 */
async function appendAndCommit({ publishRepoDir, readmePath, instance, wakeState, earnState, git, log }) {
  const touched = [];
  if (!(await pathExists(readmePath)) && (await ensureReadme(readmePath, instance))) {
    touched.push('README.md');
  }
  for (const state of [wakeState, earnState]) {
    if (state.newLines.length === 0) continue;
    const projected = state.newLines.map((l) => state.cfg.projectLine(l) ?? '{}');
    await appendRawLines(state.destPath, projected);
    touched.push(state.relDest);
  }

  let committed = false;
  if (touched.length > 0) {
    try {
      git(['add', '--', ...touched], publishRepoDir);
      const msgParts = [];
      if (wakeState.newLines.length > 0) {
        msgParts.push(`wake ${wakeState.cfg.extractRef(wakeState.newLines[wakeState.newLines.length - 1])}`);
      }
      if (earnState.newLines.length > 0) {
        msgParts.push(`earn ${earnState.cfg.extractRef(earnState.newLines[earnState.newLines.length - 1])}`);
      }
      git(
        [
          '-c', 'user.name=Life Manager Ledger Publish',
          '-c', 'user.email=ledger-publish@anicca.local',
          'commit', '-m', `ledger(${instance}): ${msgParts.join(' + ') || 'update'}`,
          '--', ...touched,
        ],
        publishRepoDir,
      );
      committed = true;
    } catch (err) {
      log(`[ledger-publish] commit failed: ${err.message}\n`);
    }
  }

  return { committed, pendingLineCount: wakeState.newLines.length + earnState.newLines.length };
}

/**
 * publishLedgerCycle(opts) — the orchestrator. Called once per completed wake from index.mjs's main
 * loop. NEVER throws (REQ-703) — every failure path returns a `reason` string instead.
 *
 * @param {{
 *   enabled?: boolean,
 *   ledgerPath: string,             // state/ledger.jsonl (wake source).
 *   earnLedgerPath?: string,        // skills/earn/state/earn-ledger.jsonl (earn/money source, FIND-001).
 *   markerPath: string,
 *   instance?: string,
 *   repoRoot?: string,              // the SHARED checkout — used ONLY to read `git remote get-url origin`.
 *   publishRepoDir?: string,        // DEDICATED clone, default $ANICCA_HOME/state/.ledger-publish-repo.
 *   lockDir?: string,               // default $ANICCA_HOME/state/.ledger-publish-<instance>.lock.
 *   originUrl?: string,             // injectable (tests point this at a file:// bare-repo fixture).
 *   git?: (args: string[], cwd?: string) => string,
 *   now?: () => number,
 *   log?: (msg: string) => void,
 *   minLines?: number,
 *   minIntervalMs?: number,
 * }} opts
 * @returns {Promise<{published: boolean, pushed: boolean, reason: string, publishFailureStreak: number}>}
 */
export async function publishLedgerCycle(opts) {
  const {
    enabled = process.env.LEDGER_PUBLISH_ENABLED === '1',
    ledgerPath,
    earnLedgerPath,
    repoRoot = path.resolve(__dirname, '..', '..'),
    instance = process.env.ANICCA_INSTANCE || 'clawrouter',
    markerPath,
    publishRepoDir = path.join(path.dirname(markerPath), '.ledger-publish-repo'),
    lockDir = path.join(path.dirname(markerPath), `.ledger-publish-${instance}.lock`),
    originUrl,
    git = defaultGit,
    now = () => Date.now(),
    log = (msg) => process.stderr.write(msg),
    minLines = DEFAULT_MIN_LINES,
    minIntervalMs = DEFAULT_MIN_INTERVAL_MS,
  } = opts;

  if (!enabled) return { published: false, pushed: false, reason: 'disabled', publishFailureStreak: 0 };

  const resolvedEarnLedgerPath =
    earnLedgerPath || path.join(path.dirname(markerPath), 'skills', 'earn', 'earn-ledger.jsonl');

  try {
    const marker = await readMarker(markerPath);

    // Cheap short-circuit: a source that has NEVER had a single line ever written needs no lock/git
    // I/O at all. (This does NOT attempt to detect "no NEW lines since last publish" — FIND-002 means
    // that can only be known after syncing to origin's actual state below.)
    const wakeSourceLines = await readLinesOrEmpty(ledgerPath);
    const earnSourceLines = await readLinesOrEmpty(resolvedEarnLedgerPath);
    if (wakeSourceLines.length === 0 && earnSourceLines.length === 0) {
      return { published: false, pushed: false, reason: 'no-new-lines', publishFailureStreak: marker.publishFailureStreak };
    }

    const gotLock = await acquireLock(lockDir);
    if (!gotLock) return { published: false, pushed: false, reason: 'locked', publishFailureStreak: marker.publishFailureStreak };

    try {
      const branch = `ledger-${instance}`;
      const readmePath = path.join(publishRepoDir, 'README.md');
      let publishFailureStreak = marker.publishFailureStreak;

      let resolvedOriginUrl = originUrl;
      try {
        if (!resolvedOriginUrl) resolvedOriginUrl = git(['remote', 'get-url', 'origin'], repoRoot).trim();
        await ensurePublishRepo({ publishRepoDir, originUrl: resolvedOriginUrl, branch, instance, git, log });
        // FIND-005: setup succeeding (auth/clone/fetch/checkout all worked) is the meaningful health
        // signal — reset the streak here, independent of whether there is anything new to push.
        publishFailureStreak = 0;
      } catch (err) {
        publishFailureStreak += 1;
        await writeMarker(markerPath, { ...marker, publishFailureStreak });
        log(`[ledger-publish] publish-repo setup failed, skipping cycle: ${err.message}\n`);
        return { published: false, pushed: false, reason: 'setup-failed', publishFailureStreak };
      }

      const attempt = async () => {
        const wakeState = await reconcileSource({ cfg: SOURCES.wake, publishRepoDir, sourceLines: wakeSourceLines, instance });
        const earnState = await reconcileSource({ cfg: SOURCES.earn, publishRepoDir, sourceLines: earnSourceLines, instance });
        const { committed, pendingLineCount } = await appendAndCommit({ publishRepoDir, readmePath, instance, wakeState, earnState, git, log });
        return { wakeState, earnState, committed, pendingLineCount };
      };

      let result = await attempt();

      if (result.pendingLineCount === 0) {
        // Nothing new relative to actual origin truth this cycle — still persist the reconciled
        // (possibly healed/adopted) cursors so the cache reflects reality even on a no-op cycle.
        await writeMarker(markerPath, {
          wake: { pushedLineCount: result.wakeState.actualPublishedLineCount },
          earn: { pushedLineCount: result.earnState.actualPublishedLineCount },
          lastPushTs: marker.lastPushTs,
          publishFailureStreak,
        });
        return { published: false, pushed: false, reason: 'no-new-lines', publishFailureStreak };
      }

      const nowMs = now();
      const decision = decidePublish({ pendingLineCount: result.pendingLineCount, lastPushTs: marker.lastPushTs, nowMs, minLines, minIntervalMs });
      let published = result.committed;
      let pushed = false;
      let lastPushTs = marker.lastPushTs;

      if (decision.shouldPush) {
        try {
          git(['push', 'origin', branch], publishRepoDir);
          pushed = true;
        } catch (pushErr) {
          log(`[ledger-publish] push rejected, re-syncing from origin and retrying once: ${pushErr.message}\n`);
          try {
            git(['fetch', '--quiet', '--depth', '1', '--no-tags', 'origin', `+refs/heads/${branch}:refs/remotes/origin/${branch}`], publishRepoDir);
            git(['reset', '--quiet', '--hard', `origin/${branch}`], publishRepoDir);
            result = await attempt();
            published = published || result.committed;
            // impl-review iter3 FIND-002: `pushed` must only become true when a `git push` was
            // actually (re-)issued and observed to succeed THIS cycle — not unconditionally after the
            // surrounding try block. If the post-reset reconcile finds nothing pending (e.g. origin
            // already had this cycle's content — the first push attempt landed before the client saw
            // the failure), no push call is made here and `pushed` correctly stays whatever it already
            // was (false, since the primary attempt's own `pushed = true;` never ran before it threw).
            if (result.pendingLineCount > 0) {
              git(['push', 'origin', branch], publishRepoDir);
              pushed = true;
            }
          } catch (retryErr) {
            publishFailureStreak += 1;
            log(`[ledger-publish] retry after re-sync also failed, will retry next cycle: ${retryErr.message}\n`);
          }
        }
      }

      if (pushed) {
        lastPushTs = nowMs;
        publishFailureStreak = 0;
      }

      // The cursor may only advance past this cycle's newLines if they were BOTH committed AND
      // confirmed-pushed — a `git push` that trivially no-ops ("Everything up-to-date") because the
      // preceding commit itself failed must never be mistaken for those lines being confirmed.
      const confirmedThisCycle = pushed && result.committed;
      const wakePushedLineCount = confirmedThisCycle
        ? result.wakeState.actualPublishedLineCount + result.wakeState.newLines.length
        : result.wakeState.actualPublishedLineCount;
      const earnPushedLineCount = confirmedThisCycle
        ? result.earnState.actualPublishedLineCount + result.earnState.newLines.length
        : result.earnState.actualPublishedLineCount;

      await writeMarker(markerPath, {
        wake: { pushedLineCount: wakePushedLineCount },
        earn: { pushedLineCount: earnPushedLineCount },
        lastPushTs,
        publishFailureStreak,
      });

      return { published, pushed, reason: decision.reason, publishFailureStreak };
    } finally {
      await releaseLock(lockDir);
    }
  } catch (err) {
    // Outermost safety net (REQ-703): NOTHING escapes this function, ever.
    log(`[ledger-publish] cycle failed unexpectedly: ${err.message}\n`);
    let streak = 1;
    try {
      const marker = await readMarker(markerPath);
      streak = (marker.publishFailureStreak || 0) + 1;
      await writeMarker(markerPath, { ...marker, publishFailureStreak: streak });
    } catch {
      // best-effort — the streak counter itself is not allowed to make this function throw.
    }
    return { published: false, pushed: false, reason: 'error', publishFailureStreak: streak };
  }
}
