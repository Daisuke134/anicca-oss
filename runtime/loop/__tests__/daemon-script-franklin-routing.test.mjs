// REQ-004, REQ-005, PROP-016 — runtime/anicca-daemon.sh Franklin THINK-routing tests
// (franklin-loop-revival).
//
// These tests exercise the REAL bash source text of runtime/anicca-daemon.sh — either by actually
// running small, side-effect-free snippets extracted verbatim from the file (the PORT/
// OPENAI_BASE_URL assignment logic: pure variable arithmetic, no process spawn, no network, no
// git/npm calls), or by statically inspecting the franklin branch of step 2 (`ensure_brain`) for
// forbidden tokens — mirroring verification-architecture.md PROP-016's own two-part method
// ("(1) static source check ... (2) live process ENV check", of which only (1) is safe/appropriate
// to automate here; (2) is a live-machine check for Phase 2b/3, not a node:test unit test).
//
// The lifecycle test executes a copied daemon against fake node/curl/pkill binaries and a temporary
// repository only. It never starts a real proxy, performs network I/O, or reads production state.
//
// RED PHASE: today (behavioral-spec.md Root cause B), `INSTANCE=franklin` resolves
// PORT="${FRANKLIN_PROXY_PORT:-8403}" (line 27) and the franklin ensure_brain branch (lines 67-71)
// spawns `franklin proxy --port "$PORT" ...`. Every "new capability" test below therefore FAILS
// until Phase 2b implements REQ-004(a)/(b)/(c).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON_PATH = path.resolve(__dirname, '../../anicca-daemon.sh');
const source = fs.readFileSync(DAEMON_PATH, 'utf8');

function extractBetween(text, startMarker, endMarker) {
  const startIdx = text.indexOf(startMarker);
  if (startIdx === -1) throw new Error(`start marker not found in anicca-daemon.sh: ${JSON.stringify(startMarker)}`);
  const endIdx = text.indexOf(endMarker, startIdx + startMarker.length);
  if (endIdx === -1) throw new Error(`end marker not found in anicca-daemon.sh: ${JSON.stringify(endMarker)}`);
  return text.slice(startIdx, endIdx);
}

function extractLine(text, marker) {
  const idx = text.indexOf(marker);
  if (idx === -1) throw new Error(`marker not found in anicca-daemon.sh: ${JSON.stringify(marker)}`);
  const lineEnd = text.indexOf('\n', idx);
  return text.slice(idx, lineEnd === -1 ? text.length : lineEnd);
}

// The PORT-assignment block (currently lines 26-30). Pure variable arithmetic — safe to actually
// execute in a throwaway bash subshell (no I/O, no external commands).
const PORT_SNIPPET = extractBetween(
  source,
  'INSTANCE="${ANICCA_INSTANCE:-clawrouter}"',
  'LOGDIR="$ANICCA_HOME/logs"',
);

function runPortSnippet(env) {
  return spawnSync('/bin/bash', ['-c', `${PORT_SNIPPET}\necho "PORT=$PORT"`], {
    env: { PATH: '/usr/bin:/bin', ...env },
    encoding: 'utf8',
    timeout: 5000,
  });
}

test('REQ-004(a): INSTANCE=franklin resolves PORT to ClawRouter\'s 8402 (matching the non-franklin branch), not 8403', () => {
  const result = runPortSnippet({ ANICCA_INSTANCE: 'franklin' });
  assert.equal(result.status, 0, `snippet must run cleanly (stderr: ${result.stderr})`);
  assert.equal(
    result.stdout.trim(),
    'PORT=8402',
    'today this resolves PORT=8403 via FRANKLIN_PROXY_PORT (behavioral-spec.md Root cause B) — REQ-004(a) requires 8402',
  );
});

test('REQ-004(c): even with FRANKLIN_PROXY_PORT=8403 set (mirroring the deployed plist\'s current value), PORT still resolves 8402 for franklin', () => {
  const result = runPortSnippet({ ANICCA_INSTANCE: 'franklin', FRANKLIN_PROXY_PORT: '8403' });
  assert.equal(result.status, 0, `stderr: ${result.stderr}`);
  assert.equal(
    result.stdout.trim(),
    'PORT=8402',
    'daemon.sh must no longer derive franklin\'s PORT from FRANKLIN_PROXY_PORT at all (REQ-004(c))',
  );
});

test('ARCH-11: non-franklin uses a dedicated repository-proxy port, never Franklin ClawRouter :8402', () => {
  const unsetResult = runPortSnippet({});
  assert.equal(unsetResult.stdout.trim(), 'PORT=18402');
  const clawrouterResult = runPortSnippet({ ANICCA_INSTANCE: 'clawrouter' });
  assert.equal(clawrouterResult.stdout.trim(), 'PORT=18402');
  const invalidFranklinResult = runPortSnippet({ ANICCA_INSTANCE: 'franklin2x' });
  assert.equal(invalidFranklinResult.stdout.trim(), 'PORT=18402');
});

test('REQ-004(a): OPENAI_BASE_URL (the SAME $PORT variable line 117 already uses) resolves to http://127.0.0.1:8402/v1 for franklin', () => {
  const exportLine = extractLine(source, 'export OPENAI_BASE_URL=');
  const result = spawnSync('/bin/bash', ['-c', `${PORT_SNIPPET}\n${exportLine}\necho "URL=$OPENAI_BASE_URL"`], {
    env: { PATH: '/usr/bin:/bin', ANICCA_INSTANCE: 'franklin' },
    encoding: 'utf8',
    timeout: 5000,
  });
  assert.equal(result.status, 0, `stderr: ${result.stderr}`);
  assert.equal(result.stdout.trim(), 'URL=http://127.0.0.1:8402/v1');
});

test('REQ-004(b)/REQ-005/PROP-016 (static): step-2 franklin branch never spawns `franklin proxy` or `clawrouter`, never reads $HOME/.local/state/life-manager/.env or BLOCKRUN_WALLET_KEY, but keeps AT MOST a curl readiness probe', () => {
  const step2 = extractBetween(source, '# 2. brain:', '# 3. telemetry poster');
  // franklin2-daemon-identity rewired the literal `"$INSTANCE" = "franklin"` comparison to the shared
  // is_franklin_instance() predicate (so franklin2/franklin3/… route the same way) — the condition text
  // changed, the franklin-branch BODY this test inspects did not.
  const franklinBranchMatch = step2.match(/if is_franklin_instance "\$INSTANCE"; then([\s\S]*?)\nelse\b/);
  assert.ok(franklinBranchMatch, 'expected an `if is_franklin_instance "$INSTANCE"; then ... else` block inside daemon.sh step 2');
  const franklinBranch = franklinBranchMatch[1];

  assert.ok(!/franklin proxy/.test(franklinBranch), 'must NEVER spawn `franklin proxy` (REQ-004(b)) — today it does (line ~69)');
  assert.ok(!/\bclawrouter\b/i.test(franklinBranch), 'must NEVER spawn a clawrouter process for franklin (REQ-005)');
  assert.ok(!/\.openclaw\/\.env/.test(franklinBranch), 'must NEVER read $HOME/.local/state/life-manager/.env for franklin (REQ-005/PROP-016)');
  assert.ok(!/BLOCKRUN_WALLET_KEY/.test(franklinBranch), 'must NEVER read/export BLOCKRUN_WALLET_KEY for franklin (REQ-005/PROP-016)');
  assert.ok(/curl/.test(franklinBranch), 'the franklin branch must still be AT MOST a curl readiness probe (REQ-004(b)), not deleted entirely');
});

test('ARCH-11: the non-franklin brain starts the repository-owned compute proxy without OpenClaw or a global ClawRouter', () => {
  const step2 = extractBetween(source, '# 2. brain:', '# 3. telemetry poster');
  // The two branches must remain textually distinct: exactly one `if is_franklin_instance "$INSTANCE"`
  // conditional inside step 2, with its own `else` — never collapsed into one ensure_brain (REQ-005).
  // (franklin2-daemon-identity: condition text rewired from a literal comparison to the shared predicate.)
  const franklinConditionals = (step2.match(/if is_franklin_instance "\$INSTANCE"; then/g) || []).length;
  assert.equal(franklinConditionals, 1, 'step 2 must have exactly one franklin/else conditional (branches not collapsed, REQ-005)');
  assert.match(step2, /runtime\/compute-proxy\/start-local\.sh" --proxy-only/);
  assert.doesNotMatch(step2, /npm install -g|command -v clawrouter|\bclawrouter\s*>>/i);
  assert.doesNotMatch(step2, /\.openclaw|OpenClaw instance|OpenClaw gateway/i);
  assert.match(step2, /env -u ANICCA_EVM_PRIVATE_KEY -u BLOCKRUN_WALLET_KEY -u PKVAR -u BASE_CHAIN_WALLET_KEY/);
  assert.doesNotMatch(step2, /BLOCKRUN_WALLET_KEY=/);
});

test('ARCH-11: daemon owns and reaps only the repository proxy process it started', () => {
  assert.match(source, /BRAIN_PID="\$!"/);
  assert.match(source, /kill -TERM "\$BRAIN_PID"/);
  assert.match(source, /wait "\$BRAIN_PID"/);
  assert.match(source, /trap 'stop_owned_processes; exit 143' TERM INT/);
  assert.doesNotMatch(source, /exec node "\$REPO\/runtime\/loop\/index\.mjs"/);
});

test('ARCH-11: a naturally exiting loop reaps the daemon-owned proxy process', () => {
  const root = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'lm-daemon-reap-'));
  const repo = path.join(root, 'repo');
  const bin = path.join(root, 'bin');
  const home = path.join(root, 'home');
  const ready = path.join(root, 'ready');
  const stopped = path.join(root, 'stopped');
  fs.mkdirSync(path.join(repo, 'runtime', 'compute-proxy'), { recursive: true });
  fs.mkdirSync(path.join(repo, 'runtime', 'dashboard'), { recursive: true });
  fs.mkdirSync(path.join(repo, 'runtime', 'loop'), { recursive: true });
  fs.mkdirSync(path.join(repo, 'skills'), { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.copyFileSync(DAEMON_PATH, path.join(repo, 'runtime', 'anicca-daemon.sh'));
  fs.writeFileSync(path.join(repo, 'runtime', 'compute-proxy', 'start-local.sh'), `#!/bin/sh
touch "$READY_FILE"
trap 'touch "$STOPPED_FILE"; exit 0' TERM INT
while :; do sleep 1; done
`);
  fs.writeFileSync(path.join(bin, 'curl'), '#!/bin/sh\n[ -f "$READY_FILE" ]\n');
  fs.writeFileSync(path.join(bin, 'pkill'), '#!/bin/sh\nexit 0\n');
  fs.writeFileSync(path.join(bin, 'node'), `#!/bin/sh
case "$1" in
  */runtime/loop/index.mjs) sleep 0.2; exit 0 ;;
  *) exit 0 ;;
esac
`);
  for (const file of [
    path.join(repo, 'runtime', 'anicca-daemon.sh'),
    path.join(repo, 'runtime', 'compute-proxy', 'start-local.sh'),
    path.join(bin, 'curl'), path.join(bin, 'pkill'), path.join(bin, 'node'),
  ]) fs.chmodSync(file, 0o755);

  const result = spawnSync('/bin/bash', [path.join(repo, 'runtime', 'anicca-daemon.sh')], {
    env: {
      HOME: home,
      ANICCA_HOME: path.join(home, '.anicca'),
      ANICCA_REPO: repo,
      READY_FILE: ready,
      STOPPED_FILE: stopped,
      PATH: `${bin}:/usr/bin:/bin`,
    },
    encoding: 'utf8',
    timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.ok(fs.existsSync(ready));
  assert.ok(fs.existsSync(stopped));
});
