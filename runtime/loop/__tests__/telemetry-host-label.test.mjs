// REQ-002(b) (impl-review iteration-1 FIND-001 fix) — runtime/dashboard/telemetry-post-franklin.mjs
// `instanceHostLabel()` tests (franklin2-daemon-identity, P4-code, 2026-07-11).
//
// Before this fix, telemetry-post-franklin.mjs hardcoded `host: "Franklin"` regardless of
// ANICCA_INSTANCE, so franklin2's poster reported under the IDENTICAL dashboard label as Franklin#1
// once daemon-script-franklin2-identity's routing fix made franklin2 reach this file at all — a real,
// sprint-activated dashboard-identity collision (see reviews/impl/iteration-1/output/findings/FIND-001.json).
//
// Method (mirrors daemon-script-franklin2-identity.test.mjs's extraction technique): the function is
// extracted VERBATIM from the real telemetry-post-franklin.mjs source text and evaluated in a
// throwaway `node --eval` subprocess — never `import`s telemetry-post-franklin.mjs directly, since
// importing it triggers a real wallet-secret file read + Solana RPC/HTTP POST as top-level side
// effects (this file has no exports; `await post()` runs unconditionally at module load).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TELEMETRY_PATH = path.resolve(__dirname, '../../dashboard/telemetry-post-franklin.mjs');
const source = fs.readFileSync(TELEMETRY_PATH, 'utf8');

// Extracts the `function instanceHostLabel(instance) { ... }` definition verbatim, up to (and
// including) the first standalone closing brace after it. Pure text slice — never executes anything.
function extractHostLabelFunction(text) {
  const match = text.match(/function instanceHostLabel\(instance\)\s*\{[\s\S]*?\n\}\n/);
  if (!match) {
    throw new Error('instanceHostLabel() function not found in telemetry-post-franklin.mjs');
  }
  return match[0];
}

function hostLabel(instance) {
  const fn = extractHostLabelFunction(source);
  const arg = instance === undefined ? 'undefined' : JSON.stringify(instance);
  const result = spawnSync(
    process.execPath,
    ['--eval', `${fn}\nconsole.log(instanceHostLabel(${arg}));`],
    { env: { PATH: process.env.PATH || '' }, encoding: 'utf8', timeout: 5000 },
  );
  assert.equal(result.status, 0, `node --eval must run cleanly (stderr: ${result.stderr})`);
  return result.stdout.trim();
}

function instanceHome(argv, env) {
  const match = source.match(/function instanceHome\(argv, env\)\s*\{[\s\S]*?\n\}\n/);
  assert.ok(match, 'instanceHome() function not found');
  const result = spawnSync(
    process.execPath,
    ['--eval', `${match[0]}\nconsole.log(instanceHome(${JSON.stringify(argv)}, ${JSON.stringify(env)}));`],
    { env: { PATH: process.env.PATH || '' }, encoding: 'utf8', timeout: 5000 },
  );
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

test('REQ-002(b) (FIND-001 fix): instanceHostLabel("franklin") -> "Franklin" (backward-compat, Franklin#1\'s dashboard row name is unchanged)', () => {
  assert.equal(hostLabel('franklin'), 'Franklin');
});

test('REQ-002(b) (FIND-001 fix): instanceHostLabel("franklin2") -> "Franklin2" (the new capability — was the bug\'s duplicate-label collision before this fix)', () => {
  assert.equal(hostLabel('franklin2'), 'Franklin2');
});

test('REQ-002(b) (FIND-001 fix): instanceHostLabel matches franklin + any digit-run (franklin3, franklin10, franklin99) with the digits preserved', () => {
  assert.equal(hostLabel('franklin3'), 'Franklin3');
  assert.equal(hostLabel('franklin10'), 'Franklin10');
  assert.equal(hostLabel('franklin99'), 'Franklin99');
});

test('REQ-002(b) (FIND-001 fix): instanceHostLabel(undefined) -> "Franklin" (unset ANICCA_INSTANCE defaults to the original citizen\'s label, matching pre-fix behavior for standalone/manual runs)', () => {
  assert.equal(hostLabel(undefined), 'Franklin');
});

test('REQ-002(b) (FIND-001 fix): instanceHostLabel is a general capitalize-first-letter mapping for any other franklin-family-adjacent string, never silently reused across distinct instances', () => {
  assert.equal(hostLabel('clawrouter'), 'Clawrouter');
});

test('ARCH-11: telemetry reads the exact instance home passed by its owning daemon', () => {
  assert.equal(
    instanceHome(['--home', '/instances/franklin2'], { HOME: '/users/owner', ANICCA_HOME: '/wrong' }),
    '/instances/franklin2',
  );
  assert.equal(instanceHome([], { HOME: '/users/owner', ANICCA_HOME: '/instances/franklin1' }), '/instances/franklin1');
  assert.equal(instanceHome([], { HOME: '/users/owner' }), '/users/owner/.blockrun');
});

test('ARCH-11: telemetry names the concrete model used by the shared repository proxy', () => {
  assert.match(source, /import \{ normalizeModelSelection \} from "\.\.\/compute-proxy\/model-map\.mjs";/);
  assert.match(source, /process\.env\.ANICCA_MODEL \|\| "free\/glm-4\.7"/);
  assert.match(source, /process\.env\.ANICCA_FRONTIER_MODEL \|\| "anthropic\/claude-sonnet-4-6"/);
  assert.match(source, /model_live: FRANKLIN_MODEL\.model, model_tier: FRANKLIN_MODEL\.tier/);
  assert.doesNotMatch(source, /nvidia\/llama-4-maverick|franklin proxy --model/);
});
