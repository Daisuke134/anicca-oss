/**
 * config-drift-detector.test.mjs — Phase 3 adversary FIND-001 fix: the COMPOSED, runnable detector
 * (`runConfigDriftDetector`), not just its parts.
 *
 * Every I/O boundary (`listProcesses`/`readPlistEnv`/`readRegistry`) is injected with fixture-backed
 * fakes below — this file NEVER spawns a real `ps`/`plutil`/`launchctl` process and never touches the
 * real filesystem. The fixtures below are literal copies of the REAL bug-1 (ANICCA_BRAIN drift) and
 * bug-2 (hl_trade registry drift) data recorded in specs/behavioral-spec.md's Context section.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runConfigDriftDetector } from '../config-drift.mjs';

const AGENT_ECONOMY_PS_LINE =
  '94249 node __REPO_ROOT__/runtime/loop/index.mjs ANICCA_HOME=/home/life-manager/.anicca-founder XPC_SERVICE_NAME=ai.anicca.agent-economy-loop ANICCA_BRAIN=proxy';
const FRANKLIN_PS_LINE =
  '79192 node __REPO_ROOT__/runtime/loop/index.mjs ANICCA_HOME=/home/life-manager/.blockrun XPC_SERVICE_NAME=ai.anicca.franklin-loop ANICCA_BRAIN=proxy';
const FRANKLIN2_PS_LINE =
  '86698 node __REPO_ROOT__/runtime/loop/index.mjs ANICCA_HOME=/home/life-manager/.franklin2-home/.blockrun XPC_SERVICE_NAME=ai.anicca.franklin2-loop ANICCA_BRAIN=proxy';

const CANONICAL_REGISTRY_DORMANT = { slots: { hl_trade: { status: 'dormant' } } };
const RUNTIME_REGISTRY_LIVE = { slots: { hl_trade: { status: 'live' } } };
const RUNTIME_REGISTRY_DORMANT = { slots: { hl_trade: { status: 'dormant' } } };

function fakeDeps({
  psLines,
  plistEnvsByPath = {},
  registriesByPath = {},
  dotenvTextByHome = {}, // FIND-003(iter2): fake $ANICCA_HOME/.env contents, keyed by aniccaHome -- NEVER the real filesystem
  canonicalRegistryPath = '/repo/skills/registry.json',
}) {
  return {
    listProcesses: async () => psLines,
    readPlistEnv: async (plistPath) => (plistPath in plistEnvsByPath ? plistEnvsByPath[plistPath] : null),
    readRegistry: async (filePath) => (filePath in registriesByPath ? registriesByPath[filePath] : null),
    readDotenvText: async (aniccaHome) => (aniccaHome in dotenvTextByHome ? dotenvTextByHome[aniccaHome] : ''),
    canonicalRegistryPath,
  };
}

const PLIST_DIR = `${process.env.HOME}/Library/LaunchAgents`;

test('FIND-001: the real bug-1 fixture (agent-economy-loop plist declares claude-p, runtime shows proxy) is detected end-to-end through the composed detector', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE],
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_BRAIN: 'claude-p' },
    },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT, // matches -> no registry drift for this run
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'FAIL');
  assert.equal(report.brainDrift.length, 1);
  assert.deepEqual(report.brainDrift[0], {
    key: 'ANICCA_BRAIN',
    instance: 'ai.anicca.agent-economy-loop',
    declared: 'claude-p',
    actual: 'proxy',
  });
  assert.deepEqual(report.registryDrift, []);
});

test('one canonical registry means stale instance copies are not runtime inputs', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE],
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_BRAIN: 'proxy' }, // no brain drift this time
    },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_LIVE,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'PASS');
  assert.deepEqual(report.brainDrift, []);
  assert.deepEqual(report.registryDrift, []);
});

test('FIND-001: BOTH real bugs fire simultaneously across 3 discovered targets (agent-economy-loop drifting, both Franklins clean) — REQ-003 per-instance expectations, no global hardcoding', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE, FRANKLIN_PS_LINE, FRANKLIN2_PS_LINE],
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_BRAIN: 'claude-p' },
      [`${PLIST_DIR}/ai.anicca.franklin-loop.plist`]: { ANICCA_BRAIN: 'proxy' },
      [`${PLIST_DIR}/ai.anicca.franklin2-loop.plist`]: { ANICCA_BRAIN: 'proxy' },
    },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
      '/home/life-manager/.blockrun/skills/registry.json': RUNTIME_REGISTRY_LIVE,
      '/home/life-manager/.franklin2-home/.blockrun/skills/registry.json': RUNTIME_REGISTRY_LIVE,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.targets.length, 3);
  assert.equal(report.overallStatus, 'FAIL');
  assert.equal(report.brainDrift.length, 1, 'only agent-economy-loop should show a brain drift, NOT the Franklins (REQ-003: same function, per-instance declared values)');
  assert.equal(report.brainDrift[0].instance, 'ai.anicca.agent-economy-loop');
  assert.equal(report.registryDrift.length, 0, 'per-instance registry copies are intentionally ignored');
});

test('everything clean -> overallStatus PASS, zero drift entries', async () => {
  const deps = fakeDeps({
    psLines: [FRANKLIN_PS_LINE],
    plistEnvsByPath: { [`${PLIST_DIR}/ai.anicca.franklin-loop.plist`]: { ANICCA_BRAIN: 'proxy' } },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.blockrun/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'PASS');
  assert.deepEqual(report.brainDrift, []);
  assert.deepEqual(report.registryDrift, []);
});

test('real-fleet fix (com.anicca.daemon, found running the real detector 2026-07-12): a 4th discovered instance whose plist and runtime env BOTH simply never mention ANICCA_BRAIN at all -> PASS, not a spurious drift', async () => {
  const CLAWROUTER_PS_LINE =
    '61618 node __REPO_ROOT__/runtime/loop/index.mjs ANICCA_HOME=/home/life-manager/.anicca XPC_SERVICE_NAME=com.anicca.daemon ANICCA_INSTANCE=clawrouter';
  const deps = fakeDeps({
    psLines: [CLAWROUTER_PS_LINE],
    plistEnvsByPath: {
      [`${PLIST_DIR}/com.anicca.daemon.plist`]: { ANICCA_HOME: '/home/life-manager/.anicca', ANICCA_INSTANCE: 'clawrouter' }, // no ANICCA_BRAIN key, matches the real deployed plist
    },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'PASS', `expected PASS, got brainDrift=${JSON.stringify(report.brainDrift)}`);
  assert.deepEqual(report.brainDrift, []);
});

test('REQ-010 edge case: zero discovered targets -> overallStatus is the explicit "NO_TARGETS", never silently "PASS"', async () => {
  const deps = fakeDeps({ psLines: [], registriesByPath: { '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT } });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.targets.length, 0);
  assert.equal(report.overallStatus, 'NO_TARGETS');
});

test('FIND-003 wired end-to-end: an unreadable plist (readPlistEnv resolves null) reports FAIL/unobservable through the composed detector, not a silent PASS', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE],
    plistEnvsByPath: {}, // deliberately empty: readPlistEnv will resolve null for the one plist path requested
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'FAIL');
  assert.equal(report.brainDrift.length, 1);
  assert.equal(report.brainDrift[0].reason, 'unobservable');
});

test('REQ-004: an unreadable canonical registry.json fails EVERY runtime copy closed, never an empty (PASS) registryDrift', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE],
    plistEnvsByPath: { [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_BRAIN: 'proxy' } },
    registriesByPath: {
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
      // canonical path deliberately absent from registriesByPath -> readRegistry resolves null for it
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'FAIL');
  assert.equal(report.registryDrift.length, 1);
  assert.equal(report.registryDrift[0].reason, 'unobservable');
});

test('injected deps mean this file never touches a real ps/plutil/launchctl process or the real filesystem', async () => {
  let listProcessesCalls = 0;
  let readDotenvCalls = 0;
  const deps = {
    listProcesses: async () => { listProcessesCalls += 1; return [AGENT_ECONOMY_PS_LINE]; },
    readPlistEnv: async () => ({ ANICCA_BRAIN: 'proxy' }),
    readRegistry: async () => ({ slots: {} }),
    readDotenvText: async () => { readDotenvCalls += 1; return ''; },
    canonicalRegistryPath: '/fake/registry.json',
  };
  await runConfigDriftDetector(deps);
  assert.equal(listProcessesCalls, 1, 'the injected fake, not a real ps invocation, must be the one and only process source');
  assert.equal(readDotenvCalls, 1, 'the injected fake, not a real filesystem read, must be the one and only .env source (FIND-003 iter2 wiring)');
});

// ── FIND-002 (adversary iteration-2, blocking): a target whose ANICCA_HOME could not be observed at all
// must still surface as an explicit FAIL in registryDrift, never be silently excluded (REQ-004/REQ-010
// edge case). Previously this branch had no `else` -- such a target contributed NOTHING at all. ────────

test('a target without ANICCA_HOME still uses the observable canonical registry', async () => {
  const NO_HOME_PS_LINE =
    '77777 node __REPO_ROOT__/runtime/loop/index.mjs XPC_SERVICE_NAME=ai.anicca.orphan-loop ANICCA_BRAIN=proxy'; // deliberately no ANICCA_HOME= token
  const deps = fakeDeps({
    psLines: [NO_HOME_PS_LINE],
    plistEnvsByPath: { [`${PLIST_DIR}/ai.anicca.orphan-loop.plist`]: { ANICCA_BRAIN: 'proxy' } },
    registriesByPath: { '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.targets.length, 1);
  assert.equal(report.targets[0].aniccaHome, null);
  assert.equal(report.overallStatus, 'PASS');
  assert.deepEqual(report.registryDrift, []);
});

test('mixed home observability does not create registry copies', async () => {
  const NO_HOME_PS_LINE = '77777 node __REPO_ROOT__/runtime/loop/index.mjs XPC_SERVICE_NAME=ai.anicca.orphan-loop ANICCA_BRAIN=proxy';
  const deps = fakeDeps({
    psLines: [FRANKLIN_PS_LINE, NO_HOME_PS_LINE],
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.franklin-loop.plist`]: { ANICCA_BRAIN: 'proxy' },
      [`${PLIST_DIR}/ai.anicca.orphan-loop.plist`]: { ANICCA_BRAIN: 'proxy' },
    },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.blockrun/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'PASS');
  assert.deepEqual(report.registryDrift, []);
});

// ── FIND-003 (adversary iteration-2, blocking): the OBSERVED-side codeDefault fallback (added to stop
// Franklin/com.anicca.daemon false positives) opened a false NEGATIVE for a real ANICCA_BRAIN override
// sourced from $ANICCA_HOME/.env, which `ps` can never see (index.mjs never writes dotenv values back
// to process.env). The detector must now read .env too and treat it as a (lower-precedence-than-plist)
// declared source, matching config.mjs's own real processEnv > dotenv > DEFAULTS precedence. ───────────

test('FIND-003: a real ANICCA_BRAIN override sourced ONLY from $ANICCA_HOME/.env (plist has no such key) is now detected as a real drift, not silently PASSed', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE], // observed runtime ANICCA_BRAIN=proxy (ps can never see .env)
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_HOME: '/home/life-manager/.anicca-founder' }, // plist itself never mentions ANICCA_BRAIN
    },
    dotenvTextByHome: {
      '/home/life-manager/.anicca-founder': 'ANICCA_BRAIN=claude-p\n', // the REAL declared intent lives only here
    },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'FAIL', `expected FAIL (a real .env-sourced drift), got brainDrift=${JSON.stringify(report.brainDrift)}`);
  assert.equal(report.brainDrift.length, 1);
  assert.equal(report.brainDrift[0].declared, 'claude-p', 'declared must be resolved from .env, since the plist itself never mentions the key');
  assert.equal(report.brainDrift[0].actual, 'proxy');
});

test('FIND-003: when .env ALSO says proxy (matching the observed runtime value), no false drift is introduced by reading .env', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE],
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_HOME: '/home/life-manager/.anicca-founder' },
    },
    dotenvTextByHome: { '/home/life-manager/.anicca-founder': 'ANICCA_BRAIN=proxy\n' },
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'PASS');
  assert.deepEqual(report.brainDrift, []);
});

test('FIND-003: the plist EXPLICITLY declaring a key wins over a conflicting .env value (processEnv-equivalent precedence over dotenv, matching config.mjs:104-112 exactly)', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE], // observed = proxy
    plistEnvsByPath: {
      [`${PLIST_DIR}/ai.anicca.agent-economy-loop.plist`]: { ANICCA_BRAIN: 'proxy' }, // plist EXPLICITLY says proxy
    },
    dotenvTextByHome: { '/home/life-manager/.anicca-founder': 'ANICCA_BRAIN=claude-p\n' }, // .env disagrees, but is LOWER precedence
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'PASS', 'the plist (higher precedence, matching real processEnv-injection semantics) must win over .env, exactly like config.mjs loadConfig');
  assert.deepEqual(report.brainDrift, []);
});

test('FIND-003: an unreadable plist stays unobservable regardless of .env content -- a lower-precedence layer never rescues an unreadable higher-precedence identity source', async () => {
  const deps = fakeDeps({
    psLines: [AGENT_ECONOMY_PS_LINE],
    plistEnvsByPath: {}, // plist itself unreadable
    dotenvTextByHome: { '/home/life-manager/.anicca-founder': 'ANICCA_BRAIN=proxy\n' }, // matches actual, but must NOT rescue an unreadable plist
    registriesByPath: {
      '/repo/skills/registry.json': CANONICAL_REGISTRY_DORMANT,
      '/home/life-manager/.anicca-founder/skills/registry.json': RUNTIME_REGISTRY_DORMANT,
    },
  });
  const report = await runConfigDriftDetector(deps);
  assert.equal(report.overallStatus, 'FAIL');
  assert.equal(report.brainDrift[0].reason, 'unobservable', 'an unreadable plist must stay unobservable even if .env happens to "agree" with the observed value -- fail-closed, not fail-open');
});
