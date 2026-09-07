// franklin-alwaysact-skill-router — Phase 2a (RED phase), the REAL runtime/loop/index.mjs wake-loop
// integration harness: REQ-501 (identity gate), REQ-505/506/508/511/513 (the attemptsUsed-arbitrated
// retry/reroute/escalation state machine + the exhaustive sec2.5 transition matrix), REQ-506's
// classify-call-site widening (PROP-506c), REQ-509 (money-safety non-regression), REQ-512
// (observability).
//
// index.mjs is NEVER imported directly in this codebase's tests (it is a self-executing script — see
// runtime/loop/__tests__/integration.test.mjs's own header) — every test here spawns it as a REAL
// child process (mirrors integration.test.mjs's spawnLoop/waitForLines pattern) against:
//   - a mock HTTP brain server (scripted think() response sequences, request bodies captured)
//   - a temporary skills root with mock skill scripts (never a real skills/*/run.sh)
//   - the REAL, unmodified skills/registry.json (11 documented always-act slots, real risk tags —
//     verified in specs/verification-architecture.md's iteration-5 ground-truth spot-check) — NOT a
//     fixture registry. LIFE_MANAGER_SKILLS_ROOT points skill execution at the temporary fixture tree.
//   - a tmp .blockrun/.solana-session identity fixture (REQ-501's own sol-trade/run.sh idiom,
//     mirrored via runtime/loop/__tests__/wallet-address-solana.test.mjs's exact pattern) — always a
//     FRESH, randomly generated, UNFUNDED keypair, never Franklin's real production secret.
//
// EVERY test in this file is EXPECTED TO FAIL against the CURRENT (pre-Phase-2b) index.mjs/brain.mjs:
// there is no identity gate, no ctx.alwaysActEngaged, no attemptsUsed retry loop, no dispatch-rejection
// guard, no router_no_realized_action/always_act_* ledger kinds — every wake behaves exactly as today
// (sleep always offered, 1 think() call, isEarnSlot-only classify gate). This is the intended RED
// signal for REQ-501/505/506/508/509/511/512/513 and every row of behavioral-spec.md sec2.5's matrix.
//
// Assumed REQ-501(b) config-flag NAME (behavioral-spec.md names no literal string — "a config flag
// (default OFF), mirroring... SOL_GATE_LIVE_ENABLE"): this Phase 2a commit fixes it as
// ALWAYS_ACT_ENABLED (config.mjs will need to pass it through, same as SOL_GATE_LIVE_ENABLE's own
// sibling flags) — Phase 2b MAY rename it, but must then update this test file's constant below.

import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import {
  makeTmpHome, writeGenesis, writeMockSkill, writeMockEarnSkill, writeMockGuardBlockedSkill,
  writeEmptyRegistry, writeRiskTaggedRegistry,
  generateSolanaKeypair, writeBlockrunSession, writeAutomatonSolanaJson,
  startMockBrainServer, makeToolCallResponse, makeNarrateResponse,
  spawnLoop, readLedger, waitForLines, waitForCondition, baseSpawnEnv, cleanupHome,
  REPO_ROOT,
} from './helpers/always-act-harness.mjs';

const ALWAYS_ACT_FLAG = 'ALWAYS_ACT_ENABLED';

const cleanups = [];
after(async () => {
  for (const fn of cleanups.splice(0)) { try { await fn(); } catch { /* best-effort */ } }
});

function track(proc, server, home, legacyHome) {
  cleanups.push(async () => {
    try { proc.kill('SIGTERM'); } catch { /* already dead */ }
    try { server.close(); } catch { /* already closed */ }
    await cleanupHome(home);
    if (legacyHome) await cleanupHome(legacyHome);
  });
}

/** Sets up a Franklin-IDENTITY-MATCHING fixture: ANICCA_HOME === $HOME/.blockrun, with a fresh
 * unfunded keypair's .solana-session written there, so REQ-501(a)'s own-vs-CLI derivation matches. */
function franklinIdentityEnv() {
  const legacyHome = makeTmpHome('always-act-legacy-');
  const { secretBase58 } = generateSolanaKeypair();
  const blockrunDir = writeBlockrunSession(legacyHome, secretBase58); // === legacyHome/.blockrun
  return { HOME: legacyHome, ANICCA_HOME: blockrunDir, legacyHome };
}

/** Sets up an identity-MISMATCH fixture: ANICCA_HOME resolves to a DIFFERENT, real, resolvable wallet
 * (not $HOME/.blockrun's own). */
function mismatchedIdentityEnv() {
  const legacyHome = makeTmpHome('always-act-legacy-mismatch-');
  const { secretBase58: franklinSecret } = generateSolanaKeypair();
  writeBlockrunSession(legacyHome, franklinSecret); // CLI_WALLET resolves to Franklin's (fake) address
  const ownHome = makeTmpHome('always-act-own-');
  const { secretBase58: ownSecret } = generateSolanaKeypair();
  writeAutomatonSolanaJson(ownHome, ownSecret, 'DifferentAddr222'); // OWN_WALLET resolves to a DIFFERENT address
  return { HOME: legacyHome, ANICCA_HOME: ownHome, legacyHome, ownHome };
}

// ===========================================================================
// REQ-501 / PROP-501a/b/c — identity + flag gate
// ===========================================================================

test('PROP-501a: identity MISMATCH -> always-act NOT engaged (sleep tool still on the real outbound wire), regardless of the flag', { timeout: 20000 }, async () => {
  const { HOME, ANICCA_HOME, legacyHome, ownHome } = mismatchedIdentityEnv();
  writeGenesis(ANICCA_HOME);
  const { server, url, requests } = await startMockBrainServer(() => makeNarrateResponse());
  const proc = spawnLoop({ ...baseSpawnEnv(), HOME, ANICCA_HOME, OPENAI_BASE_URL: url, [ALWAYS_ACT_FLAG]: '1' });
  track(proc, server, ownHome, legacyHome);

  await waitForLines(path.join(ANICCA_HOME, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  assert.ok(requests.length >= 1);
  const hasSleep = requests[0].tools.some((t) => t.function?.name === 'sleep');
  assert.equal(hasSleep, true, 'a mismatched instance must never have sleep withheld, even with the flag set');
});

test('PROP-501b: identity MATCH + flag unset -> always-act NOT engaged; ledgers kind:always_act_not_engaged reason:flag_unset (REQ-512)', { timeout: 20000 }, async () => {
  const { HOME, ANICCA_HOME, legacyHome } = franklinIdentityEnv();
  writeGenesis(ANICCA_HOME);
  const { server, url, requests } = await startMockBrainServer(() => makeNarrateResponse());
  const proc = spawnLoop({ ...baseSpawnEnv(), HOME, ANICCA_HOME, OPENAI_BASE_URL: url }); // flag NOT set
  track(proc, server, ANICCA_HOME, legacyHome);

  const lines = await waitForLines(path.join(ANICCA_HOME, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  const hasSleep = requests[0].tools.some((t) => t.function?.name === 'sleep');
  assert.equal(hasSleep, true, 'flag unset -> fail-closed, sleep still offered');
  const notEngagedLine = lines.find((l) => l.kind === 'always_act_not_engaged');
  assert.ok(notEngagedLine, 'a Franklin-identity wake with the flag unset must ledger kind:always_act_not_engaged');
  assert.equal(notEngagedLine.reason, 'flag_unset');
});

test('PROP-501b: identity MATCH + flag malformed ("yes") -> always-act NOT engaged; ledgers reason:flag_malformed', { timeout: 20000 }, async () => {
  const { HOME, ANICCA_HOME, legacyHome } = franklinIdentityEnv();
  writeGenesis(ANICCA_HOME);
  const { server, url } = await startMockBrainServer(() => makeNarrateResponse());
  const proc = spawnLoop({ ...baseSpawnEnv(), HOME, ANICCA_HOME, OPENAI_BASE_URL: url, [ALWAYS_ACT_FLAG]: 'yes' });
  track(proc, server, ANICCA_HOME, legacyHome);

  const lines = await waitForLines(path.join(ANICCA_HOME, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  const notEngagedLine = lines.find((l) => l.kind === 'always_act_not_engaged');
  assert.ok(notEngagedLine);
  assert.equal(notEngagedLine.reason, 'flag_malformed');
});

test('PROP-501c: identity MATCH + flag "1" -> always-act ENGAGED (sleep withheld on the real outbound wire, run_skill enum = the always-act menu)', { timeout: 20000 }, async () => {
  const { HOME, ANICCA_HOME, legacyHome } = franklinIdentityEnv();
  writeGenesis(ANICCA_HOME);
  const { server, url, requests } = await startMockBrainServer(() => makeNarrateResponse());
  const proc = spawnLoop({ ...baseSpawnEnv(), HOME, ANICCA_HOME, OPENAI_BASE_URL: url, [ALWAYS_ACT_FLAG]: '1' });
  track(proc, server, ANICCA_HOME, legacyHome);

  await waitForCondition(() => requests.length >= 1, 15000);
  proc.kill('SIGTERM');
  const hasSleep = requests[0].tools.some((t) => t.function?.name === 'sleep');
  assert.equal(hasSleep, false, 'identity match + flag=1 -> sleep withheld on the REAL wire');
  const runSkillTool = requests[0].tools.find((t) => t.function?.name === 'run_skill');
  assert.ok(runSkillTool, 'run_skill must still be offered');
  const enumMembers = new Set(runSkillTool.function.parameters.properties.slot.enum || []);
  assert.ok(enumMembers.has('economy/gig'), 'the always-act menu must include economy/gig');
  assert.ok(!enumMembers.has('report'), 'the always-act menu must exclude report');
});

// ===========================================================================
// The sec2.5 transition matrix — one spawn-based test per reachable row (rows 1-12), engaged wakes only.
// Each test writes mock skills for exactly the real registry slot names it needs (real risk tags:
// economy/gig=safe, earn/clip=safe, earn/sol-trade=capital, hl_trade=capital — verified ground truth).
// ===========================================================================

function engagedSpawn({ home, legacyHome, url, extraEnv = {} } = {}) {
  return spawnLoop({ ...baseSpawnEnv(), HOME: legacyHome, ANICCA_HOME: home, OPENAI_BASE_URL: url, [ALWAYS_ACT_FLAG]: '1', ...extraEnv });
}

function setupEngaged() {
  const { HOME, ANICCA_HOME, legacyHome } = franklinIdentityEnv();
  writeGenesis(ANICCA_HOME);
  return { home: ANICCA_HOME, legacyHome: HOME };
}

test('Row 1 / PROP-506b: valid slot picked, execution completes, earnLine !== null -> immediate EXECUTE, 1 think() call total, no reroute', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  let capturedWakeId = null;
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    if (count === 0) {
      // capture the wake_id from the user message so the mock skill can key its ledger line on it
      const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
      capturedWakeId = m ? m[1] : null;
      return makeToolCallResponse('economy/gig', { action: 'post' });
    }
    return makeNarrateResponse(); // should never be reached
  });
  writeMockSkill(home, 'economy/gig', 'exit 0'); // placeholder; overwritten once wake_id known below
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => capturedWakeId !== null, 10000);
  writeMockEarnSkill(home, 'economy/gig', { realizeForWakeId: capturedWakeId });

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  assert.equal(requests.length, 1, 'exactly 1 think() call — realized on first pick, no retry');
  assert.equal(lines[0].slot, 'economy/gig');
  assert.equal(lines[0].profitable, true);
});

test('Row 2 / PROP-505a: no-tool-call -> reprompt (same menu) -> valid pick, earnLine !== null -> EXECUTE via reprompt, exactly 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  let wakeIds = [];
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
    wakeIds.push(m ? m[1] : null);
    if (count === 0) return makeNarrateResponse('thinking, no tool call'); // no-tool-call
    return makeToolCallResponse('earn/clip', {});
  });
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => requests.length >= 2, 15000);
  const secondWakeId = wakeIds[1];
  writeMockEarnSkill(home, 'earn/clip', { realizeForWakeId: secondWakeId });

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'exactly 2 think() calls: baseline no-tool-call + 1 reprompt, never 3');
  assert.equal(lines[0].slot, 'earn/clip');
  assert.equal(lines[0].attemptsUsed, 1);
  const reprompt2ndSchema = requests[1].tools.find((t) => t.function?.name === 'run_skill');
  assert.ok(new Set(reprompt2ndSchema.function.parameters.properties.slot.enum).has('earn/clip'), 'reprompt must offer the SAME menu, not a narrowed one');
});

test('Row 3 / PROP-513e (money-safety-critical, FIND-301 direct regression): no-tool-call -> reprompt -> fabricated slot:"sleep" -> ESCALATE, exactly 2 think() calls, never 3, no skill execution', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeNarrateResponse('no tool call');
    if (count === 1) return makeToolCallResponse('sleep', { seconds: 5 });
    return makeNarrateResponse(); // must never be reached — a 3rd call is the exact bug FIND-301 closes
  });
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  // give any (incorrect) 3rd call a moment to happen before asserting the count is final
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'NEVER a 3rd think() call — the exact FIND-301 regression');
  const escalated = lines.find((l) => l.kind === 'router_no_realized_action');
  assert.ok(escalated, 'must escalate truthfully via REQ-508, never accept the fabricated sleep');
  assert.notEqual(escalated.kind, 'narrate', 'the fabricated sleep must NOT be silently honored as an idle/sleep outcome');
  assert.equal(escalated.slot, null);
  assert.notEqual(escalated.profitable, true);
});

test('Row 4 / PROP-505a: no-tool-call -> reprompt -> no-tool-call again -> ESCALATE, exactly 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer(() => makeNarrateResponse('still thinking'));
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2);
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'));
});

test('Row 5 / PROP-506a (hard tool-enum exclusion): capital slot picked, earnLine===null -> reroute EXCLUDES the just-picked slot from the REAL schema -> valid safe reroute pick, earnLine!==null -> EXECUTE, 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  // Every attempt within one wake shares the SAME wake_id (only the offered-slot schema narrows on a
  // reroute) -- capture it from the FIRST request/attempt and write the reroute target's mock skill
  // immediately, rather than racing the write against the 2nd think() call's own response reaching the
  // child process (writing after `requests.length >= 2` resolves is too late: the mock server records
  // the request synchronously before responding, so the child may already be resolving the skill path
  // before this test-side write lands).
  let wakeId = null;
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    if (count === 0) {
      const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
      wakeId = m ? m[1] : null;
      return makeToolCallResponse('earn/sol-trade', { strategy: 'wait' });
    }
    return makeToolCallResponse('economy/gig', {});
  });
  writeMockSkill(home, 'earn/sol-trade', 'echo "wait, no edge"\nexit 0'); // exits 0, writes NO earn-ledger line
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => wakeId !== null, 10000);
  writeMockEarnSkill(home, 'economy/gig', { realizeForWakeId: wakeId });

  await waitForCondition(() => requests.length >= 2, 15000);
  const secondEnum = new Set(requests[1].tools.find((t) => t.function?.name === 'run_skill').function.parameters.properties.slot.enum);
  assert.ok(!secondEnum.has('earn/sol-trade'), 'the just-picked slot must be structurally absent from the reroute schema');

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the guard-blocked FIRST pick's own
  // router_reroute_skip record now ALSO lands in ledger.jsonl (kind-distinct from the wake's own
  // terminal line) -- wait for BOTH lines and locate the terminal one by kind/slot, never by index.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'exactly 2 think() calls: baseline no-op pick + 1 reroute');
  const skipRecord = lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'earn/sol-trade');
  assert.ok(skipRecord, 'the excluded first pick\'s own skip record must be preserved in ledger.jsonl');
  const terminal = lines.find((l) => l.kind === 'wake' && l.slot === 'economy/gig');
  assert.ok(terminal, 'the reroute pick must be ledgered as this wake\'s own terminal wake line');
  assert.equal(terminal.attemptsUsed, 1);
});

test('Row 6 / PROP-513b/c (money-safety-critical, FIND-201): reroute in flight, model re-emits the just-excluded capital slot -> REJECTED, no execution, no 3rd think() call, direct ESCALATE', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('earn/sol-trade', {});
    if (count === 1) return makeToolCallResponse('earn/sol-trade', {}); // re-emits the excluded slot
    return makeNarrateResponse(); // must never be reached
  });
  writeMockSkill(home, 'earn/sol-trade', 'exit 0');
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the excluded first pick's own
  // router_reroute_skip record now ALSO lands in ledger.jsonl -- wait for BOTH lines (skip +
  // escalation) so the escalation `.find()` below never races ahead of the 2nd write.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'never a 3rd think() call');
  assert.ok(lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'earn/sol-trade'), 'the excluded first pick\'s own skip record must be preserved in ledger.jsonl');
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'), 'must escalate, not silently execute the re-emitted excluded slot');
});

test('Row 6b / PROP-513c: reroute in flight, model emits a DIFFERENT capital slot (still in ctx.alwaysActMenu, absent from this reroute\'s currentOfferedSlots) -> REJECTED, ESCALATE', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('earn/sol-trade', {});
    if (count === 1) return makeToolCallResponse('hl_trade', { coin: 'ETH', side: 'long', size_usd: 1 });
    return makeNarrateResponse();
  });
  writeMockSkill(home, 'earn/sol-trade', 'exit 0');
  writeMockSkill(home, 'hl_trade', 'exit 0'); // must NEVER actually run
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the excluded first pick's own
  // router_reroute_skip record now ALSO lands in ledger.jsonl -- wait for BOTH lines before the
  // fresh `finalLines` re-read below.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2);
  const finalLines = readLedger(path.join(home, 'state', 'ledger.jsonl'));
  assert.ok(!finalLines.some((l) => l.slot === 'hl_trade'), 'hl_trade must never have been executed');
  assert.ok(finalLines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'earn/sol-trade'), 'the excluded first pick\'s own skip record must be preserved in ledger.jsonl');
  assert.ok(finalLines.find((l) => l.kind === 'router_no_realized_action'));
});

test('Row 7 / REQ-506 edge case ("rerouted second pick ALSO no-ops"): reroute\'s own pick ALSO produces earnLine===null -> ESCALATE, never a 3rd think() call', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('earn/sol-trade', {});
    if (count === 1) return makeToolCallResponse('economy/gig', {}); // safe, but ALSO no-ops
    return makeNarrateResponse();
  });
  writeMockSkill(home, 'earn/sol-trade', 'exit 0');
  writeMockEarnSkill(home, 'economy/gig', { realizeForWakeId: null }); // no-op: never realizes
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): BOTH the first pick's AND the rerouted
  // (also no-op) second pick's own router_reroute_skip records now land in ledger.jsonl, plus the
  // final escalation line -- 3 lines total for this wake; wait for all 3.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 3, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'never a 3rd think() call, even though the reroute pick also no-opped');
  assert.ok(lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'earn/sol-trade'), 'the first pick\'s own skip record must be preserved in ledger.jsonl');
  assert.ok(lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'economy/gig'), 'the rerouted second pick\'s own skip record must ALSO be preserved in ledger.jsonl');
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'));
});

test('Row 8 / PROP-513a (structural, FIND-103 direct regression): fabricated slot:"sleep" on the VERY FIRST think() call -> REJECTED (never silently honored as idle) -> reprompt (same menu) -> valid pick -> EXECUTE, 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  let wakeIds = [];
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
    wakeIds.push(m ? m[1] : null);
    if (count === 0) return makeToolCallResponse('sleep', { reason: 'nothing to do' });
    return makeToolCallResponse('earn/clip', {});
  });
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => requests.length >= 2, 15000);
  writeMockEarnSkill(home, 'earn/clip', { realizeForWakeId: wakeIds[1] });

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2);
  assert.ok(!lines.some((l) => l.kind === 'narrate' && l.note === 'agent chose to sleep'), 'the fabricated sleep must never be honored as a real idle/sleep outcome');
  assert.equal(lines[0].slot, 'earn/clip');
});

test('Row 9 / PROP-506g (money-safety-critical, FIND-301 REQ-506 symmetric extension): no-tool-call -> reprompt -> VALID pick but earnLine===null -> ESCALATE directly, NO reroute (never a 3rd think() call)', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeNarrateResponse('no tool call');
    if (count === 1) return makeToolCallResponse('economy/gig', {});
    return makeNarrateResponse(); // must never be reached (would be an illegitimate 3rd call: a reroute)
  });
  writeMockEarnSkill(home, 'economy/gig', { realizeForWakeId: null }); // always no-ops
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the reprompt-attempt no-op pick's own
  // router_reroute_skip record now ALSO lands in ledger.jsonl, ahead of the escalation line -- 2 lines.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'a no-op on the REPROMPT attempt must never trigger a reroute (that would be a 3rd think() call)');
  assert.ok(lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'economy/gig'), 'the reprompt-attempt no-op pick\'s own skip record must be preserved in ledger.jsonl');
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'));
});

test('Row 10 (spec-review iteration-5 notes.md fix): fabricated slot on attempt 1 -> reprompt -> fabricated slot AGAIN -> ESCALATE, exactly 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('sleep', {});
    if (count === 1) return makeToolCallResponse('report', {}); // real registry slot, but NOT in the always-act menu
    return makeNarrateResponse();
  });
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2);
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'));
});

test('Row 11 (spec-review iteration-5 notes.md fix): fabricated slot on attempt 1 -> reprompt -> no tool call -> ESCALATE, exactly 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('sleep', {});
    if (count === 1) return makeNarrateResponse('nothing');
    return makeNarrateResponse();
  });
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2);
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'));
});

test('Row 12 (spec-review iteration-5 notes.md fix): fabricated slot on attempt 1 -> reprompt -> VALID pick but earnLine===null -> ESCALATE, NO reroute, exactly 2 think() calls', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('sleep', {});
    if (count === 1) return makeToolCallResponse('earn/clip', {});
    return makeNarrateResponse();
  });
  writeMockEarnSkill(home, 'earn/clip', { realizeForWakeId: null });
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the reprompt-attempt no-op pick's own
  // router_reroute_skip record now ALSO lands in ledger.jsonl, ahead of the escalation line -- 2 lines.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'never a 3rd think() call (a reroute) after a reprompt-attempt no-op');
  assert.ok(lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'earn/clip'), 'the reprompt-attempt no-op pick\'s own skip record must be preserved in ledger.jsonl');
  assert.ok(lines.find((l) => l.kind === 'router_no_realized_action'));
});

// ===========================================================================
// REQ-502 / PROP-502d (Phase 3 impl-review iteration-1 FIND-001 fix) — a REAL spawned wake against an
// empty-yielding registry escalates with the DISTINCT kind:'router_menu_empty', never conflated with
// the ordinary bounds-exhausted 'router_no_realized_action' kind. The pure `assembleAlwaysActMenu`
// unit test in always-act-router.test.mjs only proves the MENU-ASSEMBLY helper can represent an empty
// array; it never observes the real ledger `kind` a live empty-menu wake actually writes (the exact
// tautological-coverage gap FIND-001 identified) — this test drives the REAL runAlwaysActWake() path
// end-to-end via ALWAYS_ACT_REGISTRY_PATH_OVERRIDE (writeEmptyRegistry, a well-formed `{"slots":{}}`
// fixture, never a malformed-JSON parse-error fallback) and asserts the ACTUAL ledger line.
// ===========================================================================

test('PROP-502d (REAL wake): an empty-yielding registry escalates a REAL spawned wake with the DISTINCT kind:\'router_menu_empty\', zero think() calls, never the ordinary router_no_realized_action kind', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const registryOverridePath = writeEmptyRegistry(home);
  const { server, url, requests } = await startMockBrainServer(() => makeNarrateResponse()); // must never be reached -- zero think() calls
  const proc = engagedSpawn({ home, legacyHome, url, extraEnv: { ALWAYS_ACT_REGISTRY_PATH_OVERRIDE: registryOverridePath } });
  track(proc, server, home, legacyHome);

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 0, 'REQ-502 empty-menu terminal case spends ZERO think() calls -- never even reaches the brain');
  assert.equal(lines[0].kind, 'router_menu_empty', 'the empty-MENU escalation must ledger the DISTINCT kind, never the ordinary router_no_realized_action');
  assert.equal(lines[0].slot, null);
  assert.notEqual(lines[0].profitable, true);
});

// ===========================================================================
// REQ-506 / PROP-506f (spec-review iteration-2 FIND-101 regression test, "empty-safe-set-escalates";
// Phase 3 impl-review iteration-2 FIND-003 fix) -- a REAL spawned wake whose reroute-eligible
// risk-free set is empty (every OTHER live always-act slot besides the just-picked one is ALSO
// risk:"capital") must escalate directly -- zero additional think() calls, NEVER a fallback into a
// risk:"capital" reroute target. Also proves REQ-510's own literal attemptsUsed===0 domain-pin AC for
// this exact terminal case: one baseline think() call was made, yet attemptsUsed must ledger 0, not 1
// (falsifying a naive think()-call-count reading of the field).
// ===========================================================================

test('PROP-506f (empty-safe-set-escalates): reroute-eligible risk-free set is empty (all other live always-act slots besides the pick are risk:"capital") -> immediate REQ-508 escalation, zero additional think() calls, ledgered attemptsUsed===0 despite exactly ONE think() call made', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const registryOverridePath = writeRiskTaggedRegistry(home, {
    'earn/sol-trade': 'capital',
    hl_trade: 'capital',
  });
  const { server, url, requests } = await startMockBrainServer((count) => {
    if (count === 0) return makeToolCallResponse('earn/sol-trade', {});
    return makeNarrateResponse(); // must never be reached -- the risk-free-filtered reroute set is empty
  });
  writeMockSkill(home, 'earn/sol-trade', 'echo "wait, no edge"\nexit 0'); // exits 0, writes NO earn-ledger line
  const proc = engagedSpawn({ home, legacyHome, url, extraEnv: { ALWAYS_ACT_REGISTRY_PATH_OVERRIDE: registryOverridePath } });
  track(proc, server, home, legacyHome);

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): the just-picked slot's own
  // router_reroute_skip record now ALSO lands in ledger.jsonl, ahead of the escalation line -- 2 lines.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  await new Promise((r) => setTimeout(r, 300));
  proc.kill('SIGTERM');
  assert.equal(requests.length, 1, 'the empty risk-free reroute set must spend ZERO additional think() calls -- never a 2nd call');
  const skipRecord = lines.find((l) => l.kind === 'router_reroute_skip' && l.slot === 'earn/sol-trade');
  assert.ok(skipRecord, 'the just-picked slot\'s own skip record must be preserved in ledger.jsonl');
  const escalated = lines.find((l) => l.kind === 'router_no_realized_action');
  assert.ok(escalated, 'must escalate truthfully via REQ-508, never a fallback into a risk:"capital" reroute target');
  assert.equal(escalated.slot, null);
  assert.notEqual(escalated.profitable, true);
  assert.equal(escalated.attemptsUsed, 0, 'REQ-510 domain pin: attemptsUsed ledgers the {0,1} state variable, never a think()-call count -- exactly ONE think() call was made, yet attemptsUsed must be literally 0 here');
});

// ===========================================================================
// REQ-506 / PROP-506c (closes FIND-002) — economy/gig and economy/lending are classify-eligible
// under the widened index.mjs:450 call-site condition, not just menu-eligible
// ===========================================================================

test('PROP-506c: economy/gig pick with earnLine===null triggers the SAME reroute path as any isEarnSlot member (classify call-site widening, index.mjs:450)', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  // Every attempt within one wake shares the SAME wake_id -- capture it from the FIRST request and
  // write the reroute target's mock skill immediately (see Row 5's identical comment): writing after
  // `requests.length >= 2` resolves races the child's own skill-path resolution and is too late.
  let wakeId = null;
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    if (count === 0) {
      const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
      wakeId = m ? m[1] : null;
      return makeToolCallResponse('economy/gig', { action: 'list' });
    }
    return makeToolCallResponse('earn/clip', {});
  });
  writeMockSkill(home, 'economy/gig', 'exit 0'); // no earn-ledger line -> earnLine===null
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => wakeId !== null, 10000);
  writeMockEarnSkill(home, 'earn/clip', { realizeForWakeId: wakeId });

  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): economy/gig's own no-op pick now ALSO
  // ledgers a router_reroute_skip line ahead of the reroute's terminal `wake` line -- wait for both,
  // and locate the terminal line by kind/slot, never by index.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2, 'economy/gig no-op must trigger classifyEarnResult + a reroute, never silently resolve as an ordinary wake');
  assert.ok(!lines.some((l) => l.kind === 'wake_error'), 'must never fail as a brain-transport error');
  assert.ok(lines.some((l) => l.kind === 'wake' && l.slot === 'earn/clip'), 'must terminate with the reroute pick executed as an ordinary wake');
});

test('PROP-506c: economy/lending pick with earnLine===null ALSO triggers the reroute path (same widening)', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  let wakeIds = [];
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
    wakeIds.push(m ? m[1] : null);
    if (count === 0) return makeToolCallResponse('economy/lending', {});
    return makeToolCallResponse('earn/clip', {});
  });
  writeMockSkill(home, 'economy/lending', 'exit 0');
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => requests.length >= 2, 15000);
  writeMockEarnSkill(home, 'earn/clip', { realizeForWakeId: wakeIds[1] });
  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): economy/lending's own no-op pick now
  // ALSO ledgers a router_reroute_skip line ahead of the reroute's terminal `wake` line -- wait for
  // both before killing, so this test's process teardown never races ahead of the terminal write.
  await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  proc.kill('SIGTERM');
  assert.equal(requests.length, 2);
});

// ===========================================================================
// REQ-507 / PROP-507a — opaque args pass-through (concrete integration confirmation; the general
// "for ALL menu members" property is proven at the pure level in always-act-router.test.mjs against
// isRejectableSleepOrOffMenu's slot-only, args-blind membership check)
// ===========================================================================

test('PROP-507a: the model\'s chosen args reach the skill\'s WAKE_ID-scoped execution UNMODIFIED — the harness never substitutes/filters by args content', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const strategyMarker = 'nonstandard-strategy-xyz-123';
  const { server, url } = await startMockBrainServer(() => makeToolCallResponse('economy/gig', { action: 'post', taskSpec: strategyMarker }));
  const capturePath = path.join(home, 'captured-args.txt');
  writeMockSkill(home, 'economy/gig', `echo "$ANICCA_ARGS" > "${capturePath}"\nexit 0`);
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => fs.existsSync(capturePath), 15000);
  proc.kill('SIGTERM');
  const captured = fs.readFileSync(capturePath, 'utf8');
  assert.ok(captured.includes(strategyMarker), 'the model\'s literal args content must reach the skill unmodified, regardless of its (nonstandard) content');
});

// ===========================================================================
// REQ-509 / PROP-509a/b — money-safety non-regression
// ===========================================================================

test('PROP-509a (money-safety-critical, Tier 0 static guard): this feature\'s current diff touches none of the pre-existing trading guard files', () => {
  const disallowed = [
    /^skills\/earn\/(?:sol-trade|polymarket-trade)\/run\.sh$/,
    /^skills\/earn\/(?:sol-trade|polymarket-trade)\/lib\/resolve-max-spend\.sh$/,
    /^skills\/_shared\/lib\/earn-guard\.mjs$/,
  ];
  let changed = [];
  try {
    const mergeBase = execFileSync('git', ['merge-base', 'origin/main', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8' }).trim();
    changed = execFileSync('git', ['diff', '--name-only', mergeBase, 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8' })
      .split('\n').map((l) => l.trim()).filter(Boolean);
  } catch (err) {
    assert.fail(`could not compute git diff against origin/main merge-base: ${err.message}`);
  }
  const violations = changed.filter((f) => disallowed.some((re) => re.test(f)));
  assert.deepEqual(violations, [], `this feature must never touch a money-safety guard file; found: ${JSON.stringify(violations)}`);

  const catalogGateDiff = (() => {
    try {
      const mergeBase = execFileSync('git', ['merge-base', 'origin/main', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8' }).trim();
      return execFileSync('git', ['diff', mergeBase, 'HEAD', '--', 'runtime/loop/catalog-gate.mjs'], { cwd: REPO_ROOT, encoding: 'utf8' });
    } catch { return ''; }
  })();
  assert.ok(!catalogGateDiff.includes('DEFAULT_BOOTSTRAP_RESERVE_USDC ='), 'this feature must never alter catalog-gate.mjs\'s threshold constant');
});

test('PROP-509b (money-safety-critical): a REAL guard-block (kill-switch-style skip, exits 0, writes no earn-ledger line) on the first pick triggers a reroute to a DIFFERENT slot, and the guard\'s own skip record is preserved VERBATIM in the ledger (never overwritten/silenced)', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  // Every attempt within one wake shares the SAME wake_id -- capture it from the FIRST request and
  // write the reroute target's mock skill immediately (see Row 5's identical comment): writing after
  // `requests.length >= 2` resolves races the child's own skill-path resolution and is too late.
  let wakeId = null;
  const { server, url, requests } = await startMockBrainServer((count, body) => {
    if (count === 0) {
      const m = /Wake ([A-Z0-9]+):/.exec(body.messages?.[1]?.content || '');
      wakeId = m ? m[1] : null;
      return makeToolCallResponse('earn/sol-trade', {});
    }
    return makeToolCallResponse('economy/gig', {});
  });
  writeMockGuardBlockedSkill(home, 'earn/sol-trade', 'kill-switch');
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  await waitForCondition(() => wakeId !== null, 10000);
  writeMockEarnSkill(home, 'economy/gig', { realizeForWakeId: wakeId });

  await waitForCondition(() => requests.length >= 2, 15000);
  // REQ-509 (Phase 3 impl-review iteration-3 FIND-001 fix): behavioral-spec.md:493-495's own literal
  // AC text is "preserved verbatim in the ledger" -- "the ledger" is a proper noun this spec uses
  // consistently for ledger.jsonl (REQ-510/512/777), never for harness-failures.jsonl (which REQ-508
  // always names explicitly by its literal filename). This wake now writes TWO ledger.jsonl lines: the
  // guard-blocked first pick's own `router_reroute_skip` record, then the reroute's terminal `wake`
  // line -- wait for both, then find each by kind, never by index.
  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 2, 15000);
  proc.kill('SIGTERM');
  const terminal = lines.find((l) => l.kind === 'wake' && l.slot === 'economy/gig');
  assert.ok(terminal, 'the reroute must pick a DIFFERENT slot, never retry earn/sol-trade with a relaxed guard');
  // This assertion is UNCONDITIONAL (no dead-code `if` guard) -- it genuinely fails if the record is
  // ever missing from ledger.jsonl or its reason text is lost/altered. `skip_reason` carries
  // `skillResult.output` untampered (only the SAME redactPrivateKeyPatterns pass every other ledger
  // line already gets -- never truncated/whitespace-collapsed), satisfying "preserved verbatim".
  const guardSkipRecord = lines.find((l) => l.slot === 'earn/sol-trade' && l.kind === 'router_reroute_skip');
  assert.ok(guardSkipRecord, 'the guard-blocked slot\'s own skip record must be preserved (not silenced) in ledger.jsonl -- "the ledger" per REQ-509\'s own literal AC text');
  assert.ok(
    (guardSkipRecord.skip_reason || '').includes('kill-switch'),
    'the guard-blocked slot\'s own skip reason must be preserved verbatim',
  );

  // Regression guard (REQ-508 scope discipline): a routine guard-skip is NEVER a harness failure --
  // harness-failures.jsonl must carry NO router_reroute_skip record; that file is reserved for
  // REQ-508's own TERMINAL exhausted-bound escalation case, a semantically different event.
  const failureLines = readLedger(path.join(home, 'state', 'harness-failures.jsonl'));
  assert.ok(
    !failureLines.some((l) => l.kind === 'router_reroute_skip'),
    'a routine guard-skip must never be written to harness-failures.jsonl (REQ-508 is for the terminal exhausted-bound case only)',
  );
});

// ===========================================================================
// REQ-512 / PROP-512a — go-live ledger line, exactly once at the flip (integration confirmation)
// ===========================================================================

test('PROP-512a: a Franklin-identity wake with the flag set to "1" takes the ENGAGED path (sleep withheld on the real wire) and never ledgers a stray always_act_not_engaged line for that wake', { timeout: 20000 }, async () => {
  const { home, legacyHome } = setupEngaged();
  const { server, url, requests } = await startMockBrainServer(() => makeNarrateResponse());
  const proc = engagedSpawn({ home, legacyHome, url });
  track(proc, server, home, legacyHome);

  const lines = await waitForLines(path.join(home, 'state', 'ledger.jsonl'), 1, 15000);
  proc.kill('SIGTERM');
  // The two halves of this assertion together are what makes it a genuine RED signal today (rather
  // than a vacuously-true "no such kind exists yet" pass): flag=1+identity-match must BOTH actually
  // engage (sleep withheld — currently false, since REQ-501/504 do not exist yet) AND never mislabel
  // that successful engagement as always_act_not_engaged.
  assert.ok(requests.length >= 1, 'at least one think() call must have happened');
  const hasSleep = requests[0].tools.some((t) => t.function?.name === 'sleep');
  assert.equal(hasSleep, false, 'flag=1 + identity match must actually ENGAGE always-act (sleep withheld on the real wire)');
  assert.ok(!lines.some((l) => l.kind === 'always_act_not_engaged'), 'flag=1 + identity match must never ledger always_act_not_engaged');
});
