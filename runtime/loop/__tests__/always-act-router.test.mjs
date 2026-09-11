// franklin-alwaysact-skill-router — Phase 2a (RED phase), Pure Core.
//
// `runtime/loop/always-act-router.mjs` does NOT exist yet — every test in this file is EXPECTED TO
// FAIL at IMPORT time (Node ESM: "Cannot find module '.../always-act-router.mjs'"). This is the
// intended RED signal for every PROP tested here.
//
// Proposed Phase 2b module contract (documented here so Phase 2b has a concrete target — see
// .vcsdd/features/franklin-alwaysact-skill-router/specs/verification-architecture.md's "Pure Core"
// section for the authoritative, adversary-reviewed design this mirrors):
//
//   export const DOCTRINE_EARN_ACTIONS = new Set(['economy/gig', 'economy/lending'])
//   export function isEarnActionSlot(name, { isEarnSlotFn = isEarnSlot } = {}) -> boolean
//     REQ-502: isEarnSlotFn(name) || DOCTRINE_EARN_ACTIONS.has(name). Purely additive over isEarnSlot.
//   export function assembleAlwaysActMenu({ registry, catalogFilterFn, balanceUsdc,
//     reserveThresholdUsdc, riskTagOf, alwaysAvailableOf, hasOpenRiskPositionOf }) -> string[]
//     REQ-502/503: every registry.slots[name].status==='live' slot that is isEarnActionSlot(name),
//     further filtered through catalogFilterFn (catalog-gate.mjs::filterCatalog, injected).
//   export function isMarketRiskFree(slot, riskTagOf) -> boolean       // REQ-506: riskTagOf(slot)==='safe'
//   export function noRealizedAction(earnLine) -> boolean              // REQ-506: earnLine === null
//   export function isRejectableSleepOrOffMenu(slot, currentOfferedSlots) -> boolean
//     REQ-513: slot === 'sleep' || !currentOfferedSlots.includes(slot)
//   export function nextRerouteState({ attemptsUsed, maxAttempts }) ->
//     { shouldRetry: boolean, attemptsUsedNext: number, exhausted: boolean }
//     REQ-505/506/511/513: attemptsUsed ∈ {0,1}, maxAttempts=1 — the SOLE arbiter of branch selection
//     (spec-review iteration-4 FIND-301: never re-derived from array identity).
//   export function buildMustActReinforcement(ctx, priorAttempt) -> string          // REQ-505
//   export function buildAlwaysActLedgerFields({ wakeId, slot, args, attemptsUsed, realized }) -> object
//     REQ-508/510: attemptsUsed ledgered VERBATIM in the {0,1} domain (spec-review iteration-5 notes.md
//     non-blocking observation #2 fix — never a think()-call count).
//   export function isPostGoLiveRegression(ledgerTail, { minRun }) -> boolean        // REQ-512
//     Design decision made THIS Phase 2a commit (verification-architecture.md left the host module
//     open, "Phase 2b decides"): implemented here as a pure .mjs sibling of
//     skills/self/earning-health.py::is_fresh_but_barren, not as a Python function, so Phase 2a/2b/5 all
//     run under the SAME `node --test` runner without adding a pytest dependency to this JS-first
//     module.
//
// (runtime/loop/prompt.mjs::getToolDefinitions's `omitSleep` opts param — the OTHER half of REQ-504 —
// is tested directly against prompt.mjs in this same file, since it lives there per the Purity
// Boundary Map, not in always-act-router.mjs. buildAlwaysActToolDefinitions here is a thin wrapper
// over it.)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  isEarnActionSlot,
  assembleAlwaysActMenu,
  buildAlwaysActToolDefinitions,
  isMarketRiskFree,
  noRealizedAction,
  isRejectableSleepOrOffMenu,
  nextRerouteState,
  buildMustActReinforcement,
  buildAlwaysActLedgerFields,
  buildGoLiveRecord,
  shouldRecordGoLive,
  isPostGoLiveRegression,
} from '../always-act-router.mjs';

import { isEarnSlot } from '../earn-slot.mjs';
import { filterCatalog, DEFAULT_BOOTSTRAP_RESERVE_USDC } from '../catalog-gate.mjs';
import { getToolDefinitions } from '../prompt.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REGISTRY_PATH = path.resolve(__dirname, '../../../skills/registry.json');

// ===========================================================================
// REQ-502 / PROP-502a — isEarnActionSlot = isEarnSlot UNION doctrine set
// ===========================================================================

test('PROP-502a: isEarnActionSlot is the union of isEarnSlot and {economy/gig, economy/lending}', () => {
  for (const legacy of ['earn', 'yield', 'hl_trade', 'x402_sell', 'token_launch', 'earn/clip', 'earn/gig']) {
    assert.equal(isEarnActionSlot(legacy), isEarnSlot(legacy), `${legacy}: must track isEarnSlot`);
  }
  assert.equal(isEarnActionSlot('economy/gig'), true, 'economy/gig must be earn-action-eligible (doctrine-named)');
  assert.equal(isEarnActionSlot('economy/lending'), true, 'economy/lending must be earn-action-eligible (doctrine-named)');
  assert.equal(isEarnSlot('economy/gig'), false, 'ground truth: isEarnSlot itself must NOT cover economy/gig (unmodified)');
  assert.equal(isEarnSlot('economy/lending'), false, 'ground truth: isEarnSlot itself must NOT cover economy/lending (unmodified)');
  assert.equal(isEarnActionSlot('report'), false, 'report is neither isEarnSlot nor doctrine-named');
  assert.equal(isEarnActionSlot('economy/ubi'), false, 'economy/ubi is NOT in the doctrine-named set');
  assert.equal(isEarnActionSlot(''), false);
  assert.equal(isEarnActionSlot(null), false);
});

test('PROP-502a (property): for arbitrary slot names, isEarnActionSlot never returns true for a slot that is neither isEarnSlot nor {economy/gig, economy/lending}', () => {
  fc.assert(
    fc.property(fc.string(), (name) => {
      if (isEarnSlot(name) || name === 'economy/gig' || name === 'economy/lending') return true;
      return isEarnActionSlot(name) === false;
    }),
    { numRuns: 200 },
  );
});

// ===========================================================================
// REQ-502 / PROP-502b/c/d — menu assembly: live-only, excluded-set, empty-menu
// ===========================================================================

function fixtureRegistry(slotDefs) {
  return { slots: slotDefs };
}

function identityCatalogFilter({ allSlotNames }) {
  return allSlotNames.slice();
}

test('PROP-502b (property): assembleAlwaysActMenu never includes a non-live slot, for arbitrary generated registries', () => {
  const slotDefArb = fc.record({
    status: fc.constantFrom('live', 'declared', 'retired'),
    risk: fc.constantFrom('safe', 'capital', undefined),
  });
  fc.assert(
    fc.property(fc.dictionary(fc.constantFrom('yield', 'economy/gig', 'economy/lending', 'earn/clip', 'x402_sell', 'report'), slotDefArb), (slots) => {
      const registry = fixtureRegistry(slots);
      const menu = assembleAlwaysActMenu({
        registry,
        catalogFilterFn: identityCatalogFilter,
        balanceUsdc: 1000,
        reserveThresholdUsdc: 20,
        riskTagOf: (n) => slots[n]?.risk,
        alwaysAvailableOf: () => false,
        hasOpenRiskPositionOf: () => false,
      });
      return menu.every((name) => slots[name] && slots[name].status === 'live');
    }),
    { numRuns: 200 },
  );
});

test('PROP-502c: given the CURRENT skills/registry.json, menu membership resolves to the documented live always-act slots', async () => {
  const registry = JSON.parse(await fs.readFile(REGISTRY_PATH, 'utf8'));
  const menu = assembleAlwaysActMenu({
    registry,
    catalogFilterFn: identityCatalogFilter, // balance/reserve gate not under test here (REQ-503 owns that)
    balanceUsdc: 1000,
    reserveThresholdUsdc: DEFAULT_BOOTSTRAP_RESERVE_USDC,
    riskTagOf: (n) => registry.slots[n]?.risk,
    alwaysAvailableOf: (n) => registry.slots[n]?.alwaysAvailable === true,
    hasOpenRiskPositionOf: () => false,
  });
  const expected = [
    'yield', 'x402_sell', 'economy/lending', 'earn/taskmarket',
    'earn/sol-trade', 'earn/polymarket-trade',
  ];
  assert.deepEqual(new Set(menu), new Set(expected), `menu must equal the documented live slots, got: ${JSON.stringify(menu.sort())}`);
  for (const excluded of ['report', 'cook', 'self/spawn', 'self/spawn-child', 'self/issue-dev', 'self/coordinate', 'economy/ubi', 'earn/audit', 'earn/_probe', 'earn']) {
    assert.ok(!menu.includes(excluded), `${excluded} must be absent from the always-act menu`);
  }
});

test('PROP-502d (pure-planning prerequisite only -- see always-act-reroute.test.mjs::PROP-502d for the REAL ledger-kind assertion, Phase 3 iter1 FIND-001 fix): an empty resolved menu is representable (assembleAlwaysActMenu returns []), never throws, never silently substitutes a default', () => {
  const registry = fixtureRegistry({ report: { status: 'live' }, cook: { status: 'live', alwaysAvailable: true } });
  const menu = assembleAlwaysActMenu({
    registry,
    catalogFilterFn: identityCatalogFilter,
    balanceUsdc: 1000,
    reserveThresholdUsdc: 20,
    riskTagOf: () => undefined,
    alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: () => false,
  });
  assert.deepEqual(menu, [], 'no isEarnActionSlot members present -> empty array, not a thrown error or a default fallback');
});

// ===========================================================================
// REQ-503 / PROP-503a/b — bootstrap-reserve gate composition (reuses catalog-gate.mjs, unmodified)
// ===========================================================================

test('PROP-503a (property, boundary sweep): at-or-above reserveThresholdUsdc, the menu is the FULL unfiltered isEarnActionSlot set (mirrors catalog-gate.mjs::isBelowThreshold at-or-above semantics)', () => {
  const registry = fixtureRegistry({
    'earn/sol-trade': { status: 'live', risk: 'capital' },
    'economy/gig': { status: 'live', risk: 'safe' },
  });
  fc.assert(
    fc.property(fc.double({ min: 0, max: 1000, noNaN: true }), fc.double({ min: 0, max: 1000, noNaN: true }), (balance, threshold) => {
      fc.pre(balance >= threshold);
      const menu = assembleAlwaysActMenu({
        registry, catalogFilterFn: filterCatalog, balanceUsdc: balance, reserveThresholdUsdc: threshold,
        riskTagOf: (n) => registry.slots[n]?.risk, alwaysAvailableOf: () => false, hasOpenRiskPositionOf: () => false,
      });
      return new Set(menu).size === 2 && menu.includes('earn/sol-trade') && menu.includes('economy/gig');
    }),
    { numRuns: 100 },
  );
});

test('PROP-503b: below-threshold WITH an open position for a capital-risking slot keeps it in the menu (carve-out preserved); economy/gig/earn/clip/x402_sell remain regardless', () => {
  const registry = fixtureRegistry({
    'earn/sol-trade': { status: 'live', risk: 'capital' },
    'hl_trade': { status: 'live', risk: 'capital' },
    'economy/gig': { status: 'live', risk: 'safe' },
    'earn/clip': { status: 'live', risk: 'safe' },
    'x402_sell': { status: 'live', risk: 'safe' },
  });
  const menu = assembleAlwaysActMenu({
    registry, catalogFilterFn: filterCatalog, balanceUsdc: 1, reserveThresholdUsdc: 20,
    riskTagOf: (n) => registry.slots[n]?.risk, alwaysAvailableOf: () => false,
    hasOpenRiskPositionOf: (n) => n === 'earn/sol-trade',
  });
  assert.ok(menu.includes('earn/sol-trade'), 'open-position carve-out must keep earn/sol-trade visible');
  assert.ok(!menu.includes('hl_trade'), 'hl_trade has no open position -> hidden below reserve');
  for (const safe of ['economy/gig', 'earn/clip', 'x402_sell']) {
    assert.ok(menu.includes(safe), `${safe} (risk:safe) must remain regardless of balance`);
  }
});

// ===========================================================================
// REQ-504 / PROP-504a — sleep-omission at the pure-function level (necessary, not sufficient alone —
// see always-act-wire-seam.test.mjs::PROP-504b for the REAL outbound-wire assertion)
// ===========================================================================

test('PROP-504a: getToolDefinitions(slots, {omitSleep:true}) never includes a sleep tool; default/omitted opts is byte-identical to the current single-arg call', () => {
  const menu = ['economy/gig', 'earn/clip', 'earn/sol-trade'];
  const withOmit = getToolDefinitions(menu, { omitSleep: true });
  assert.ok(Array.isArray(withOmit) && withOmit.length === 1, 'exactly one tool (run_skill) when sleep is omitted');
  assert.equal(withOmit.some((t) => t.function?.name === 'sleep'), false);
  assert.deepEqual(withOmit[0].function.parameters.properties.slot.enum, menu);

  const legacyCall = getToolDefinitions(menu);
  const explicitFalse = getToolDefinitions(menu, { omitSleep: false });
  assert.deepEqual(explicitFalse, legacyCall, 'omitSleep:false (or omitted opts) must be byte-identical to the existing single-arg call');
  assert.equal(legacyCall.some((t) => t.function?.name === 'sleep'), true, 'ground truth: today\'s call still includes sleep');
});

test('PROP-504a: buildAlwaysActToolDefinitions is a thin wrapper — getToolDefinitions(menu,{omitSleep:true}) — not a parallel reimplementation', () => {
  const menu = ['yield', 'token_launch'];
  assert.deepEqual(buildAlwaysActToolDefinitions(menu), getToolDefinitions(menu, { omitSleep: true }));
});

test('PROP-504a (property): for ANY non-empty menu, buildAlwaysActToolDefinitions never contains a tool named sleep', () => {
  fc.assert(
    fc.property(fc.uniqueArray(fc.constantFrom('yield', 'hl_trade', 'x402_sell', 'economy/gig', 'earn/clip', 'token_launch'), { minLength: 1 }), (menu) => {
      const defs = buildAlwaysActToolDefinitions(menu);
      return defs.every((t) => t.function?.name !== 'sleep');
    }),
    { numRuns: 100 },
  );
});

// ===========================================================================
// REQ-506 / PROP-506e/f — isMarketRiskFree pure predicate (fail-closed on untagged slots)
// ===========================================================================

test('PROP-506e: isMarketRiskFree is true iff riskTagOf(slot) === "safe" exactly; an untagged (undefined) slot is fail-closed to NOT risk-free', () => {
  const riskTagOf = (n) => ({ 'economy/gig': 'safe', 'earn/sol-trade': 'capital' }[n]); // untagged -> undefined
  assert.equal(isMarketRiskFree('economy/gig', riskTagOf), true);
  assert.equal(isMarketRiskFree('earn/sol-trade', riskTagOf), false);
  assert.equal(isMarketRiskFree('some/untagged-slot', riskTagOf), false, 'fail-closed: undefined !== "safe"');
});

test('PROP-506e (property): isMarketRiskFree exactly mirrors riskTagOf(slot)==="safe" for arbitrary tag values', () => {
  fc.assert(
    fc.property(fc.string(), fc.constantFrom('safe', 'capital', undefined, '', 'SAFE', 'safe '), (slot, tag) => {
      const riskTagOf = () => tag;
      return isMarketRiskFree(slot, riskTagOf) === (tag === 'safe');
    }),
    { numRuns: 100 },
  );
});

test('PROP-506e (literal, current registry): earn/sol-trade, hl_trade, token_launch, earn/polymarket-trade, yield are never risk-free reroute targets', async () => {
  const registry = JSON.parse(await fs.readFile(REGISTRY_PATH, 'utf8'));
  const riskTagOf = (n) => registry.slots[n]?.risk;
  for (const capital of ['earn/sol-trade', 'hl_trade', 'token_launch', 'earn/polymarket-trade', 'yield']) {
    assert.equal(isMarketRiskFree(capital, riskTagOf), false, `${capital} must be risk:"capital" per the current registry and never risk-free`);
  }
  for (const safe of ['economy/lending', 'x402_sell', 'earn/taskmarket']) {
    assert.equal(isMarketRiskFree(safe, riskTagOf), true, `${safe} must be risk:"safe" per the current registry`);
  }
});

// ===========================================================================
// REQ-506 — noRealizedAction pure predicate
// ===========================================================================

test('noRealizedAction: true iff earnLine === null (never merely falsy — a real {profitable:false} line is still a realized action)', () => {
  assert.equal(noRealizedAction(null), true);
  assert.equal(noRealizedAction({ wake: 'w1', profitable: false, earn_usdc: 0 }), false, 'a real loss line is a realized action, not a no-op');
  assert.equal(noRealizedAction({ wake: 'w1', profitable: true }), false);
  assert.equal(noRealizedAction(undefined), false, 'strict equality to null only — undefined is NOT the documented no-op signal');
});

// ===========================================================================
// REQ-513 — isRejectableSleepOrOffMenu pure predicate (structural correctness; the REAL
// index.mjs dispatch integration for PROP-513a-e lives in always-act-reroute.test.mjs)
// ===========================================================================

test('isRejectableSleepOrOffMenu: rejects "sleep" unconditionally and any slot absent from currentOfferedSlots; accepts any member', () => {
  const offered = ['economy/gig', 'earn/clip'];
  assert.equal(isRejectableSleepOrOffMenu('sleep', offered), true);
  assert.equal(isRejectableSleepOrOffMenu('hl_trade', offered), true, 'hl_trade not offered this attempt -> rejected even if it is a real registry slot');
  assert.equal(isRejectableSleepOrOffMenu('economy/gig', offered), false);
  assert.equal(isRejectableSleepOrOffMenu('earn/clip', offered), false);
});

test('isRejectableSleepOrOffMenu (property): for any offered set and any slot, rejection is EXACTLY (slot==="sleep" || !offered.includes(slot)) — independent of array identity/order', () => {
  fc.assert(
    fc.property(fc.array(fc.string(), { maxLength: 8 }), fc.string(), (offered, slot) => {
      const expected = slot === 'sleep' || !offered.includes(slot);
      return isRejectableSleepOrOffMenu(slot, offered) === expected;
    }),
    { numRuns: 200 },
  );
});

// ===========================================================================
// REQ-505/506/511/513 / PROP-511a — nextRerouteState: attemptsUsed is the SOLE arbiter
// (spec-review iteration-4 FIND-301's class-killing fix)
// ===========================================================================

test('nextRerouteState: attemptsUsed===0 -> shouldRetry true, attemptsUsedNext 1, exhausted false; attemptsUsed===1 -> shouldRetry false, exhausted true (maxAttempts=1)', () => {
  const first = nextRerouteState({ attemptsUsed: 0, maxAttempts: 1 });
  assert.equal(first.shouldRetry, true);
  assert.equal(first.attemptsUsedNext, 1);
  assert.equal(first.exhausted, false);

  const second = nextRerouteState({ attemptsUsed: 1, maxAttempts: 1 });
  assert.equal(second.shouldRetry, false);
  assert.equal(second.exhausted, true);
});

test('PROP-511a (property, bounded exhaustive): for EVERY reachable sequence of up to 2 invalid outcomes, attemptsUsed transitions 0->1 exactly once and never exceeds maxAttempts=1 (2 think() calls total, never 3)', () => {
  // Simulates index.mjs's retry loop driving nextRerouteState across an adversarial sequence of
  // invalid outcomes — the SAME state machine REQ-511 names as the sole arbiter.
  fc.assert(
    fc.property(fc.array(fc.constantFrom('no-tool-call', 'fabricated-slot', 'no-realized-action'), { minLength: 1, maxLength: 5 }), (outcomes) => {
      let attemptsUsed = 0;
      let thinkCalls = 1; // baseline attempt always happens
      let transitions = 0;
      for (const _outcome of outcomes) {
        const state = nextRerouteState({ attemptsUsed, maxAttempts: 1 });
        if (state.exhausted) break; // REQ-508 escalation — no further think() calls, ever
        attemptsUsed = state.attemptsUsedNext;
        transitions += 1;
        thinkCalls += 1;
      }
      return thinkCalls <= 2 && transitions <= 1 && attemptsUsed <= 1;
    }),
    { numRuns: 300 },
  );
});

// ===========================================================================
// REQ-505 — buildMustActReinforcement pure string builder
// ===========================================================================

test('buildMustActReinforcement: returns a non-empty, deterministic directive string for the same inputs', () => {
  const ctx = { wakeId: 'w1', alwaysActMenu: ['economy/gig', 'earn/clip'] };
  const a = buildMustActReinforcement(ctx, { outcome: 'no-tool-call' });
  const b = buildMustActReinforcement(ctx, { outcome: 'no-tool-call' });
  assert.equal(typeof a, 'string');
  assert.ok(a.length > 0);
  assert.equal(a, b, 'pure function: identical inputs -> identical output');
});

// ===========================================================================
// REQ-508/510 / PROP-510a — buildAlwaysActLedgerFields shape + the {0,1} attemptsUsed domain pin
// (spec-review iteration-5 notes.md non-blocking observation #2 fix)
// ===========================================================================

test('PROP-510a: buildAlwaysActLedgerFields ledgers wake_id/slot/args/attemptsUsed/realized; attemptsUsed is passed through VERBATIM in the {0,1} domain, never re-derived as a think()-call count', () => {
  const resolvedOnFirstPick = buildAlwaysActLedgerFields({ wakeId: 'w1', slot: 'economy/gig', args: { action: 'post' }, attemptsUsed: 0, realized: true });
  assert.equal(resolvedOnFirstPick.wake_id, 'w1');
  assert.equal(resolvedOnFirstPick.slot, 'economy/gig');
  assert.deepEqual(resolvedOnFirstPick.args, { action: 'post' });
  assert.equal(resolvedOnFirstPick.attemptsUsed, 0);
  assert.equal(resolvedOnFirstPick.realized, true);

  const resolvedAfterReroute = buildAlwaysActLedgerFields({ wakeId: 'w2', slot: 'earn/clip', args: {}, attemptsUsed: 1, realized: true });
  assert.equal(resolvedAfterReroute.attemptsUsed, 1, 'the reroute/reprompt path ledgers attemptsUsed=1, never a think()-call count of 2');

  const escalated = buildAlwaysActLedgerFields({ wakeId: 'w3', slot: null, args: {}, attemptsUsed: 1, realized: false });
  assert.equal(escalated.slot, null, 'REQ-508: null slot when escalation fired, never a fabricated success value');
  assert.equal(escalated.realized, false);
});

test('PROP-510a (property): attemptsUsed is echoed unchanged for both domain values {0,1}; never coerced/rounded/count-like', () => {
  fc.assert(
    fc.property(fc.constantFrom(0, 1), (attemptsUsed) => {
      const fields = buildAlwaysActLedgerFields({ wakeId: 'w', slot: 'economy/gig', args: {}, attemptsUsed, realized: true });
      return fields.attemptsUsed === attemptsUsed;
    }),
    { numRuns: 20 },
  );
});

// ===========================================================================
// REQ-512 / PROP-512a — buildGoLiveRecord / shouldRecordGoLive: the pure planning half of the
// go-live operational action (Phase 3 iter1 FIND-002 fix). The effectful shell-append half
// (recordGoLive) lives in go-live.mjs and is unit-tested there (real fs, tmp ledger).
// ===========================================================================

test('PROP-512a: buildGoLiveRecord shapes exactly kind:\'always_act_go_live\' + the passed-through ts and config_source, REQ-512\'s own named payload fields', () => {
  const record = buildGoLiveRecord({ ts: '2026-07-11T00:00:00.000Z', configSource: '.env' });
  assert.deepEqual(record, { ts: '2026-07-11T00:00:00.000Z', kind: 'always_act_go_live', config_source: '.env' });
});

test('PROP-512a (property): buildGoLiveRecord\'s kind is ALWAYS the literal string always_act_go_live, regardless of ts/configSource content', () => {
  fc.assert(
    fc.property(fc.string(), fc.string(), (ts, configSource) => {
      const record = buildGoLiveRecord({ ts, configSource });
      return record.kind === 'always_act_go_live' && record.ts === ts && record.config_source === configSource;
    }),
    { numRuns: 50 },
  );
});

test('PROP-512a: shouldRecordGoLive is true for an empty tail and for a tail with no existing always_act_go_live line', () => {
  assert.equal(shouldRecordGoLive([]), true);
  assert.equal(shouldRecordGoLive([{ kind: 'always_act_not_engaged', reason: 'flag_unset' }, { kind: 'router_no_realized_action' }]), true);
});

test('PROP-512a: shouldRecordGoLive is false once a always_act_go_live line already exists anywhere in the tail -- the "exactly once at the flip" guard', () => {
  assert.equal(shouldRecordGoLive([{ kind: 'always_act_go_live', ts: 't0', config_source: 'env' }]), false);
  assert.equal(shouldRecordGoLive([{ kind: 'wake' }, { kind: 'always_act_go_live' }, { kind: 'always_act_not_engaged', reason: 'flag_malformed' }]), false);
});

// ===========================================================================
// REQ-512 / PROP-512b — isPostGoLiveRegression pure detector
// (mirrors skills/self/earning-health.py::is_fresh_but_barren's exact contract: pure, no I/O,
// pre-gathered tail in)
// ===========================================================================

const notEngaged = (reason = 'flag_unset') => ({ kind: 'always_act_not_engaged', reason });
const goLive = () => ({ kind: 'always_act_go_live' });
const engagedWake = () => ({ kind: 'router_no_realized_action' });

test('PROP-512b(a): no always_act_go_live line anywhere in the tail -> never regression-detected, regardless of how many always_act_not_engaged lines precede it (benign pre-rollout state)', () => {
  const tail = Array.from({ length: 25 }, () => notEngaged());
  assert.equal(isPostGoLiveRegression(tail, { minRun: 20 }), false);
});

test('PROP-512b(b): always_act_go_live followed by >= minRun consecutive always_act_not_engaged lines DOES trigger regression-detected', () => {
  const tail = [goLive(), ...Array.from({ length: 20 }, () => notEngaged('flag_malformed'))];
  assert.equal(isPostGoLiveRegression(tail, { minRun: 20 }), true);
});

test('PROP-512b(b): fewer than minRun consecutive always_act_not_engaged lines after go-live does NOT trigger it', () => {
  const tail = [goLive(), ...Array.from({ length: 19 }, () => notEngaged())];
  assert.equal(isPostGoLiveRegression(tail, { minRun: 20 }), false);
});

test('PROP-512b(c): a single always_act_not_engaged line surrounded by successfully-engaged wakes after go-live does NOT trigger regression (a blip resets the run)', () => {
  const tail = [goLive(), engagedWake(), engagedWake(), notEngaged(), engagedWake(), engagedWake()];
  assert.equal(isPostGoLiveRegression(tail, { minRun: 20 }), false);
});

test('PROP-512b (property): for ANY tail with no always_act_go_live line, the detector is always false', () => {
  fc.assert(
    fc.property(fc.array(fc.constantFrom(notEngaged(), engagedWake()), { maxLength: 30 }), (tail) => {
      return isPostGoLiveRegression(tail, { minRun: 20 }) === false;
    }),
    { numRuns: 100 },
  );
});
