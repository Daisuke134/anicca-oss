import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import {
  resolveThe402ConfigRoot,
  resolveThe402PublicOrigin,
  resolveX402StateDir,
} from '../state-paths.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');

test('x402 state precedence is explicit, loop root, then shared default', () => {
  assert.equal(resolveX402StateDir({ HOME: '/tmp/home', LIFE_MANAGER_STATE_ROOT: '/tmp/loop', X402_STATE_DIR: '/tmp/x402' }), '/tmp/x402');
  assert.equal(resolveX402StateDir({ HOME: '/tmp/home', LIFE_MANAGER_STATE_ROOT: '/tmp/loop' }), '/tmp/loop');
  assert.equal(resolveX402StateDir({ HOME: '/tmp/home' }), '/tmp/home/.local/state/life-manager/x402-sell');
});

test('all The402 owners share one portable config-root precedence', () => {
  assert.equal(resolveThe402ConfigRoot({ HOME: '/tmp/home', ANICCA_HOME: '/tmp/anicca', THE402_CONFIG_ROOT: '/tmp/the402' }), '/tmp/the402');
  assert.equal(resolveThe402ConfigRoot({ HOME: '/tmp/home', ANICCA_HOME: '/tmp/anicca' }), '/tmp/anicca');
  assert.equal(resolveThe402ConfigRoot({ HOME: '/tmp/home' }), '/tmp/home/.anicca');
  for (const name of ['the402-server.mjs', 'the402-worker-daemon.mjs', 'acquisition-controller.mjs']) {
    const source = readFileSync(join(ROOT, name), 'utf8');
    assert.match(source, /resolveThe402ConfigRoot/);
  }
});

test('The402 public origin accepts only a host-neutral HTTPS origin', () => {
  assert.equal(resolveThe402PublicOrigin({ THE402_PUBLIC_URL: 'https://seller.example/' }), 'https://seller.example');
  for (const value of ['', 'http://seller.example', 'https://seller.example/path', 'https://user:pass@seller.example', 'https://seller.example?x=1']) {
    assert.throws(() => resolveThe402PublicOrigin({ THE402_PUBLIC_URL: value }), /public HTTPS origin/);
  }
});

test('The402 boot loads the public origin from the canonical Life Manager env before mutation', () => {
  const home = mkdtempSync(join(tmpdir(), 'the402-env-'));
  const envFile = join(home, 'life-manager.env');
  writeFileSync(envFile, 'THE402_PUBLIC_URL=https://portable.example\n', { mode: 0o600 });
  const result = spawnSync('bash', ['-c', '. "$1"; printf "%s" "$THE402_PUBLIC_URL"', 'test', join(ROOT, 'runtime-env.sh')], {
    encoding: 'utf8',
    env: { HOME: home, LIFE_MANAGER_ENV_FILE: envFile },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, 'https://portable.example');
  const boot = readFileSync(join(ROOT, 'the402-boot.sh'), 'utf8');
  assert.ok(boot.indexOf('resolveThe402PublicOrigin') < boot.indexOf('lsof -ti tcp:8096'));
});

test('shell entrypoints source the shared runtime environment', () => {
  for (const name of ['acquisition-controller-boot.sh', 'sale-observer-boot.sh', 'settlement-recorder-boot.sh']) {
    const source = readFileSync(join(ROOT, name), 'utf8');
    assert.match(source, /\. "\$DIR\/runtime-env\.sh"/);
    assert.doesNotMatch(source, /\.anicca\/state/);
  }
  const runtime = readFileSync(join(ROOT, 'runtime-env.sh'), 'utf8');
  assert.match(runtime, /X402_STATE_DIR:-\$\{LIFE_MANAGER_STATE_ROOT:-\$\{HOME\}\/\.local\/state\/life-manager\/x402-sell\}/);
});

test('active state readers and writers do not default beside the release', () => {
  for (const name of ['acquisition-controller.mjs', 'sale-observer.mjs', 'settlement-recorder.mjs', 'scout-market.mjs', 'product-gaps.mjs', 'store-experiment.mjs', 'store-improve.mjs', 'store-review.mjs', 'serve.mjs', 'serve-v2.mjs', 'the402-server.mjs', 'the402-worker-daemon.mjs']) {
    const source = readFileSync(join(ROOT, name), 'utf8');
    assert.doesNotMatch(source, /join\([^\n]*(?:HERE|import\.meta|fileURLToPath)[^\n]*['"]state['"]/);
    assert.doesNotMatch(source, /\.anicca['"], ['"]state/);
  }
});

test('the402 provider, worker, and acquisition controller share one canonical inbox', () => {
  for (const name of ['the402-server.mjs', 'the402-worker-daemon.mjs', 'acquisition-controller.mjs']) {
    const source = readFileSync(join(ROOT, name), 'utf8');
    assert.match(source, /resolveX402StateDir/);
    assert.match(source, /the402-inbox\.sqlite/);
    assert.doesNotMatch(source, /\/Users\/[^/]+/);
  }
  const server = readFileSync(join(ROOT, 'the402-server.mjs'), 'utf8');
  const paths = readFileSync(join(ROOT, 'state-paths.mjs'), 'utf8');
  assert.match(server, /resolveThe402PublicOrigin\(\)/);
  assert.match(paths, /THE402_PUBLIC_URL/);
  assert.doesNotMatch(server, /aniccanomac-mini/);
});
