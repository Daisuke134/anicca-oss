// anicca-agent-economy REQ-201/202/203 (Phase 2a, RED phase).
//
// `runtime/loop/catalog-gate.mjs` does NOT exist yet -- every test in this file is EXPECTED TO FAIL at
// IMPORT time (Node ESM: "Cannot find module '.../catalog-gate.mjs'"). This is the intended RED signal.
//
// Proposed Phase 2b module contract (documented here so Phase 2b has a concrete target -- Phase 2b MAY
// adjust exact internal structure as long as these externally-observable behaviors hold):
//
//   export function filterCatalog({
//     balanceUsdc, allSlotNames, riskTagOf, alwaysAvailableOf, hasOpenRiskPositionOf, reserveThresholdUsdc
//   }) -> string[]
//     Pure, no I/O. Directly analogous to tier.mjs::selectTier. See behavioral-spec.md REQ-201/202/203.
//
//   export const DEFAULT_BOOTSTRAP_RESERVE_USDC = Number(process.env.BOOTSTRAP_RESERVE_USDC) || 20
//     The literal fallback (resolves FIND-001); must be >= 5 (COMPUTE_RESERVE_USDC's own default,
//     runtime/loop/context.mjs:39) as a standing invariant.
//
//   export function hasOpenRiskPositionOfYield(recentLedger) -> boolean
//     Synchronous, zero I/O: `recentLedger.slice().reverse().find(l => String(l.source||'').startsWith('yield') && l.tx)`
//     (the SAME expression index.mjs already uses to populate ctx.positionsSummary -- PROP-201h,
//     resolves FIND-102's yield half). Co-located here (rather than only inline in index.mjs) so it is
//     directly Tier-1 unit-testable against ledger fixtures with zero I/O, per PROP-201h's own acceptance
//     criterion ("fed directly to the real ... implementation, not a stub").
//
//   export async function hasOpenRiskPositionOfHlTrade({ balanceUsdc, reserveThresholdUsdc, queryFn }) -> Promise<boolean>
//     Lazy + fail-open (PROP-201i, resolves FIND-102's hl_trade half): calls `queryFn()` ONLY when
//     `balanceUsdc < reserveThresholdUsdc`; on any queryFn rejection/timeout, resolves `true` (not
//     `false`) -- fail-open, deliberately the opposite direction from every other fail-closed default in
//     this gate (see behavioral-spec.md REQ-201's fail-open rationale). `queryFn` resolves an
//     `{ open_positions: [...] }`-shaped value in production (wired to `hl.py account`'s real output);
//     tests inject a fake queryFn.
//
// (index.mjs's real wiring -- registry read, RPC balance fetch, invoking the real Hyperliquid query via
// `hl.py account` as queryFn -- is EFFECTFUL SHELL, out of this file's scope; see
// verification-architecture.md's Purity Boundary Map. index.mjs is never imported directly in this
// codebase's tests -- see integration.test.mjs's child-process `spawnLoop` pattern -- so a Tier-2
// two-wake integration test of index.mjs's ACTUAL wiring (PROP-202b's integration half) is deferred to
// Phase 2b's own test additions once that wiring lands, not attempted here at the pure-function level.)

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  filterCatalog,
  DEFAULT_BOOTSTRAP_RESERVE_USDC,
  hasOpenRiskPositionOfYield,
  hasOpenRiskPositionOfHlTrade,
} from "../catalog-gate.mjs";
import { getToolDefinitions } from "../prompt.mjs";

const ALL_SLOTS = [
  "report", "cook", "self/spawn", "self/spawn-child", "self/issue-dev", "self/coordinate",
  "economy/gig", "economy/ubi", "x402_sell", "earn/example", "earn/clip-producer",
  "yield", "hl_trade", "token_launch", "earn/sol-trade", "earn/polymarket-trade",
];
const CAPITAL_SLOTS = new Set(["yield", "hl_trade", "token_launch", "earn/sol-trade", "earn/polymarket-trade"]);
const ALWAYS_AVAILABLE_SLOTS = new Set(["report", "cook"]);

function safeRiskTagOf(name) {
  return CAPITAL_SLOTS.has(name) ? "capital" : "safe";
}
function safeAlwaysAvailableOf(name) {
  return ALWAYS_AVAILABLE_SLOTS.has(name);
}
const neverOpenPosition = () => false;

// ── PROP-201a ────────────────────────────────────────────────────────────────
test("PROP-201a: below threshold, filterCatalog excludes every capital-risking slot (no always-available/open-position carve-out in play)", () => {
  const result = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  for (const risky of CAPITAL_SLOTS) {
    assert.ok(!result.includes(risky), `${risky} must be excluded below threshold`);
  }
  for (const name of ALL_SLOTS) {
    if (!CAPITAL_SLOTS.has(name)) assert.ok(result.includes(name), `${name} (non-risking) must remain`);
  }
});

// ── PROP-201b ────────────────────────────────────────────────────────────────
test("PROP-201b: at or above threshold, filterCatalog returns the full unfiltered slot list (boundary: equality counts as at-or-above)", () => {
  const atThreshold = filterCatalog({
    balanceUsdc: 20,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  assert.deepEqual([...atThreshold].sort(), [...ALL_SLOTS].sort(), "balance === threshold must be treated as at-or-above (full catalog)");

  const aboveThreshold = filterCatalog({
    balanceUsdc: 20.01,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  assert.deepEqual([...aboveThreshold].sort(), [...ALL_SLOTS].sort());
});

// ── PROP-201c ────────────────────────────────────────────────────────────────
test("PROP-201c: unset/non-finite/negative reserveThresholdUsdc falls back to the documented default 20, and the gate is never silently disabled", () => {
  for (const badThreshold of [undefined, NaN, -5, Infinity]) {
    const result = filterCatalog({
      balanceUsdc: 5, // below 20 (the documented default) but the caller passed a bad threshold
      allSlotNames: ALL_SLOTS,
      riskTagOf: safeRiskTagOf,
      alwaysAvailableOf: () => false,
      hasOpenRiskPositionOf: neverOpenPosition,
      reserveThresholdUsdc: badThreshold,
    });
    assert.ok(!result.includes("hl_trade"), `bad threshold (${badThreshold}) must still fall back to a real default and filter -- gate must never be silently disabled`);
  }
  assert.equal(DEFAULT_BOOTSTRAP_RESERVE_USDC, 20, "the literal fallback value must resolve to 20 (Number(process.env.BOOTSTRAP_RESERVE_USDC) || 20)");
  assert.ok(DEFAULT_BOOTSTRAP_RESERVE_USDC >= 5, "standing invariant: BOOTSTRAP_RESERVE_USDC's default must be >= COMPUTE_RESERVE_USDC's own default (5)");
});

test("PROP-201c: non-finite/negative balanceUsdc is treated as below-threshold (fail-closed), mirroring tier.mjs's own NaN/Infinity/negative handling", () => {
  for (const badBalance of [NaN, -1, -Infinity, undefined]) {
    const result = filterCatalog({
      balanceUsdc: badBalance,
      allSlotNames: ALL_SLOTS,
      riskTagOf: safeRiskTagOf,
      alwaysAvailableOf: () => false,
      hasOpenRiskPositionOf: neverOpenPosition,
      reserveThresholdUsdc: 20,
    });
    assert.ok(!result.includes("hl_trade"), `balanceUsdc=${badBalance} must fail-closed to below-threshold (capital slots excluded)`);
  }
});

// ── PROP-201d ────────────────────────────────────────────────────────────────
test("PROP-201d: a slot with no explicit risk tag defaults to capital-risking (excluded) while below threshold", () => {
  const riskTagOfWithGap = (name) => (name === "future_new_slot" ? undefined : safeRiskTagOf(name));
  const result = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: [...ALL_SLOTS, "future_new_slot"],
    riskTagOf: riskTagOfWithGap,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  assert.ok(!result.includes("future_new_slot"), "an untagged slot must default to capital-risking (excluded) below threshold -- fail-closed by default, never fail-open by omission");
});

// ── PROP-201e ────────────────────────────────────────────────────────────────
test("PROP-201e: alwaysAvailable:true slots (report, cook) survive filtering even in a pathological all-risky-tag input", () => {
  const allRiskyTagOf = () => "capital"; // pathological misconfiguration: every slot tagged risky
  const result = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: allRiskyTagOf,
    alwaysAvailableOf: safeAlwaysAvailableOf, // report/cook overridden regardless of the (misconfigured) risk tag
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  assert.ok(result.includes("report"), "report must remain available regardless of its (pathological) risk tag");
  assert.ok(result.includes("cook"), "cook must remain available regardless of its (pathological) risk tag");
  assert.ok(result.length >= 2, "the gate must never produce an empty actionable toolset");
});

test("PROP-201e: the `sleep` meta-tool is structurally unaffected by filterCatalog entirely -- getToolDefinitions always appends it, independent of the slots array", () => {
  const withNoSlots = getToolDefinitions([]);
  const withSomeSlots = getToolDefinitions(["report"]);
  for (const defs of [withNoSlots, withSomeSlots]) {
    const names = defs.map((d) => d.function && d.function.name);
    assert.ok(names.includes("sleep"), "sleep must be present unconditionally, regardless of the slots list");
  }
});

// ── PROP-201f ────────────────────────────────────────────────────────────────
test("PROP-201f: a capital-risking slot with hasOpenRiskPositionOf(slot)===true remains available below threshold (open-position carve-out)", () => {
  const withOpenHlPosition = (name) => name === "hl_trade";
  const result = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: withOpenHlPosition,
    reserveThresholdUsdc: 20,
  });
  assert.ok(result.includes("hl_trade"), "hl_trade must remain visible when an open position exists, so it can be closed -- resolves the FIND-003 deadlock");
  assert.ok(!result.includes("token_launch"), "token_launch (no open position, no carve-out mechanism) must still be excluded");

  const withoutOpenPosition = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => false,
    reserveThresholdUsdc: 20,
  });
  assert.ok(!withoutOpenPosition.includes("hl_trade"), "hl_trade must be excluded when hasOpenRiskPositionOf returns false");
});

test("PROP-201f: the same open-position carve-out applies to `yield`", () => {
  const withOpenYieldPosition = (name) => name === "yield";
  const result = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: withOpenYieldPosition,
    reserveThresholdUsdc: 20,
  });
  assert.ok(result.includes("yield"), "yield must remain visible (withdraw path) when an open position exists");
});

// ── PROP-201h ────────────────────────────────────────────────────────────────
test("PROP-201h: hasOpenRiskPositionOfYield reads the SAME ledger-scan expression index.mjs uses for ctx.positionsSummary -- zero I/O", () => {
  const withYieldTx = [
    { source: "cook", tx: "0xnope" },
    { source: "yield-deposit", tx: "0xabc123" },
    { source: "hl-trade", tx: "0xother" },
  ];
  const withoutYieldTx = [
    { source: "cook", tx: "0xnope" },
    { source: "hl-trade", tx: "0xother" },
  ];
  const withYieldButNoTx = [{ source: "yield-deposit" }]; // no `tx` field -- must NOT count as an open position

  assert.equal(hasOpenRiskPositionOfYield(withYieldTx), true);
  assert.equal(hasOpenRiskPositionOfYield(withoutYieldTx), false);
  assert.equal(hasOpenRiskPositionOfYield(withYieldButNoTx), false);
  assert.equal(hasOpenRiskPositionOfYield([]), false, "empty ledger -> no open position");
  assert.equal(hasOpenRiskPositionOfYield(undefined), false, "missing/garbage ledger input must not throw");
});

// ── PROP-201i ────────────────────────────────────────────────────────────────
test("PROP-201i(a): hasOpenRiskPositionOfHlTrade's query is invoked lazily -- ONLY when balanceUsdc < reserveThresholdUsdc, never when at/above it", async () => {
  let callCount = 0;
  const queryFn = async () => { callCount += 1; return { open_positions: [] }; };

  await hasOpenRiskPositionOfHlTrade({ balanceUsdc: 25, reserveThresholdUsdc: 20, queryFn });
  assert.equal(callCount, 0, "the query must NEVER be invoked when balance is at/above threshold");

  await hasOpenRiskPositionOfHlTrade({ balanceUsdc: 5, reserveThresholdUsdc: 20, queryFn });
  assert.equal(callCount, 1, "the query must be invoked exactly once when balance is below threshold");
});

test("PROP-201i(b): a query failure/timeout/rejection resolves to true (fail-open), never false and never an uncaught throw", async () => {
  const rejectingQueryFn = async () => { throw new Error("HL API unreachable"); };
  const result = await hasOpenRiskPositionOfHlTrade({ balanceUsdc: 5, reserveThresholdUsdc: 20, queryFn: rejectingQueryFn });
  assert.equal(result, true, "a failing Hyperliquid query must fail OPEN (true), the opposite direction from this gate's other fail-closed defaults");
});

test("PROP-201i(c): a resolved non-empty open_positions array -> true; an empty array -> false", async () => {
  const withPositions = async () => ({ open_positions: [{ coin: "ETH", szi: "1.5" }] });
  const withoutPositions = async () => ({ open_positions: [] });
  assert.equal(await hasOpenRiskPositionOfHlTrade({ balanceUsdc: 5, reserveThresholdUsdc: 20, queryFn: withPositions }), true);
  assert.equal(await hasOpenRiskPositionOfHlTrade({ balanceUsdc: 5, reserveThresholdUsdc: 20, queryFn: withoutPositions }), false);
});

// ── PROP-202a ────────────────────────────────────────────────────────────────
test("PROP-202a: filterCatalog is a pure function of the CURRENT call's inputs only -- no state leaks between successive calls", () => {
  const belowResult = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => false,
    reserveThresholdUsdc: 20,
  });
  const aboveResult = filterCatalog({
    balanceUsdc: 25,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => true, // different bookkeeping inputs too -- must not matter once above threshold
    reserveThresholdUsdc: 20,
  });
  const belowResultAgain = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => false,
    reserveThresholdUsdc: 20,
  });
  assert.ok(!belowResult.includes("hl_trade"));
  assert.deepEqual([...aboveResult].sort(), [...ALL_SLOTS].sort());
  assert.deepEqual([...belowResultAgain].sort(), [...belowResult].sort(), "identical inputs on a later call must reproduce the identical output -- no cross-call state leakage");
});

// ── PROP-202b (pure-function half; the Tier-2 index.mjs two-wake integration half is deferred, see file header) ──
test("PROP-202b: a balance transition from below to at/above threshold between two successive calls restores the exact pre-restriction slot set", () => {
  const wake1 = filterCatalog({
    balanceUsdc: 19.99,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: safeAlwaysAvailableOf,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  const wake2 = filterCatalog({
    balanceUsdc: 20.01,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: safeAlwaysAvailableOf,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  assert.ok(wake1.length < ALL_SLOTS.length, "wake1 (below threshold) must be restricted");
  assert.deepEqual([...wake2].sort(), [...ALL_SLOTS].sort(), "wake2 (crossed at/above threshold) must restore the FULL, byte-for-byte identical pre-restriction slot set -- no slot permanently lost, no hysteresis");
});

// ── PROP-203a ────────────────────────────────────────────────────────────────
test("PROP-203a: filterCatalog's return type is a plain array of strings, with no attached score/rank/preference field", () => {
  const result = filterCatalog({
    balanceUsdc: 5,
    allSlotNames: ALL_SLOTS,
    riskTagOf: safeRiskTagOf,
    alwaysAvailableOf: safeAlwaysAvailableOf,
    hasOpenRiskPositionOf: neverOpenPosition,
    reserveThresholdUsdc: 20,
  });
  assert.ok(Array.isArray(result), "must return a plain array");
  for (const item of result) {
    assert.equal(typeof item, "string", "every element must be a bare slot-name string, never an {name, score} object or similar");
  }
});

// ── PROP-203b ────────────────────────────────────────────────────────────────
// Tier 0, structural, scoped strictly to THIS increment's new code (filterCatalog itself) -- verified by
// reading the source for absence of scoring/ranking keywords, not by a runtime assertion (per
// verification-architecture.md's own note that PROP-203b is a Phase 3 structural-read obligation). This
// test only asserts the OBSERVABLE contract (no score field, order is not meaningfully significant
// beyond "a set"), which is the closest a Phase 2 runtime test can get to that Tier-0 obligation.
test("PROP-203b (partial runtime proxy): filterCatalog's output ordering is not used to encode a preference -- the SAME input set, given in a different order, produces the SAME resulting set", () => {
  const reserveThresholdUsdc = 20;
  const common = { balanceUsdc: 5, riskTagOf: safeRiskTagOf, alwaysAvailableOf: safeAlwaysAvailableOf, hasOpenRiskPositionOf: neverOpenPosition, reserveThresholdUsdc };
  const forward = filterCatalog({ ...common, allSlotNames: ALL_SLOTS });
  const reversed = filterCatalog({ ...common, allSlotNames: [...ALL_SLOTS].reverse() });
  assert.deepEqual([...forward].sort(), [...reversed].sort(), "the resulting SET of eligible slots must not depend on input ordering -- confirms no positional/ranking semantics are being encoded");
});

// --- per-slot spendable balance (2026-07-12) -------------------------------------------------
// claude-p held $1.95 of Base USDC (five cents under its $2 reserve) and $5.18 of pUSD inside its
// Polymarket deposit wallet. The gate read Base USDC alone, hid every capital slot, and left the
// brain nothing to pick but narrate — while the money it DID have was the exact currency the
// polymarket slot spends. These lock the fix: a slot is gated on what IT can spend.
test('function balanceUsdc gates each slot on its own spendable money', () => {
  const spendable = (name) =>
    name === 'earn/polymarket-trade' ? 1.95 + 5.18 : 1.95; // pUSD only spendable on polymarket

  const eligible = filterCatalog({
    balanceUsdc: spendable,
    allSlotNames: ['earn/polymarket-trade', 'earn/sol-trade', 'report'],
    riskTagOf: (n) => (n === 'report' ? 'safe' : 'capital'),
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => false,
    reserveThresholdUsdc: 2,
  });

  assert.ok(eligible.includes('earn/polymarket-trade'), 'polymarket is funded IN ITS OWN VENUE');
  assert.ok(!eligible.includes('earn/sol-trade'), 'sol-trade has no Solana money — still gated');
  assert.ok(eligible.includes('report'), 'safe slots are never gated');
});

test('numeric balanceUsdc keeps the original all-slots-share-one-balance behaviour', () => {
  const eligible = filterCatalog({
    balanceUsdc: 1.95,
    allSlotNames: ['earn/polymarket-trade', 'report'],
    riskTagOf: (n) => (n === 'report' ? 'safe' : 'capital'),
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => false,
    reserveThresholdUsdc: 2,
  });

  assert.deepEqual(eligible, ['report']);
});
