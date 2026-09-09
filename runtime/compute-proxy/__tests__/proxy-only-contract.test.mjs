import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const scriptPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'start-local.sh');
const script = readFileSync(scriptPath, 'utf8');
const proxy = readFileSync(path.resolve(path.dirname(scriptPath), 'proxy.mjs'), 'utf8');

test('compute proxy listens only on loopback', () => {
  assert.match(proxy, /const HOST = ["']127\.0\.0\.1["']/);
  assert.match(proxy, /server\.listen\(PORT, HOST,/);
  assert.doesNotMatch(proxy, /server\.listen\(PORT,\s*\(\)/);
});

test('proxy-only mode is explicit and never launches the loop command', () => {
  assert.match(script, /--proxy-only/);
  assert.match(script, /PROXY_ONLY/);
  assert.match(script, /if \[ "\$PROXY_ONLY" -eq 1 \]/);
  assert.match(script, /exec env -u ANICCA_EVM_PRIVATE_KEY -u BLOCKRUN_WALLET_KEY -u PKVAR -u BASE_CHAIN_WALLET_KEY/);
  assert.match(script, /\$HERE"'\/node_modules\/viem\/accounts/);
  assert.doesNotMatch(script, /\$HERE"'\/\.\.\/node_modules\/viem\/accounts/);
});

test('proxy-only execution prepares only the instance EVM wallet when an endpoint is already ready', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'lm-proxy-only-'));
  const bin = path.join(root, 'bin');
  const home = path.join(root, 'home');
  const instance = path.join(root, 'instance');
  const calls = path.join(root, 'node-calls');
  mkdirSync(bin, { recursive: true });
  writeFileSync(path.join(bin, 'curl'), '#!/bin/sh\nexit 0\n');
  writeFileSync(path.join(bin, 'node'), `#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ -n "\${WALLET_PATH:-}" ]; then
  mkdir -p "$(dirname "$WALLET_PATH")"
  printf '%s\\n' '{"privateKey":"0x01","address":"0x0000000000000000000000000000000000000001"}' > "$WALLET_PATH"
  chmod 600 "$WALLET_PATH"
fi
`);
  chmodSync(path.join(bin, 'curl'), 0o755);
  chmodSync(path.join(bin, 'node'), 0o755);

  execFileSync('/bin/bash', [scriptPath, '--proxy-only'], {
    env: { HOME: home, ANICCA_HOME: instance, CALL_LOG: calls, PATH: `${bin}:/usr/bin:/bin` },
    stdio: 'pipe',
  });

  const invoked = readFileSync(calls, 'utf8');
  assert.match(invoked, /generatePrivateKey/);
  assert.doesNotMatch(invoked, /ensure-solana-wallet|proxy\.mjs/);
});

test('fresh install generates a real instance wallet from compute-proxy dependencies', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'lm-proxy-real-wallet-'));
  const bin = path.join(root, 'bin');
  const home = path.join(root, 'home');
  const instance = path.join(root, 'instance');
  mkdirSync(bin, { recursive: true });
  writeFileSync(path.join(bin, 'curl'), '#!/bin/sh\nexit 0\n');
  chmodSync(path.join(bin, 'curl'), 0o755);

  execFileSync('/bin/bash', [scriptPath, '--proxy-only'], {
    env: {
      HOME: home,
      ANICCA_HOME: instance,
      PATH: `${bin}:${process.env.PATH}`,
    },
    stdio: 'pipe',
  });

  const wallet = JSON.parse(readFileSync(path.join(instance, '.automaton', 'wallet.json'), 'utf8'));
  assert.match(wallet.privateKey, /^0x[0-9a-f]{64}$/i);
  assert.match(wallet.address, /^0x[0-9a-f]{40}$/i);
});

test('proxy-only execution removes inherited wallet selectors before starting proxy.mjs', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'lm-proxy-wallet-env-'));
  const bin = path.join(root, 'bin');
  const home = path.join(root, 'home');
  const instance = path.join(root, 'instance');
  const observed = path.join(root, 'observed-env');
  mkdirSync(bin, { recursive: true });
  writeFileSync(path.join(bin, 'curl'), '#!/bin/sh\nexit 1\n');
  writeFileSync(path.join(bin, 'node'), `#!/bin/sh
if [ "$1" = "proxy.mjs" ]; then
  printf '%s|%s|%s|%s\\n' "\${ANICCA_EVM_PRIVATE_KEY:-}" "\${BLOCKRUN_WALLET_KEY:-}" "\${PKVAR:-}" "\${BASE_CHAIN_WALLET_KEY:-}" > "$OBSERVED_ENV"
  exit 0
fi
if [ -n "\${WALLET_PATH:-}" ]; then
  mkdir -p "$(dirname "$WALLET_PATH")"
  printf '%s\\n' '{"privateKey":"0x01","address":"0x0000000000000000000000000000000000000001"}' > "$WALLET_PATH"
  chmod 600 "$WALLET_PATH"
fi
`);
  chmodSync(path.join(bin, 'curl'), 0o755);
  chmodSync(path.join(bin, 'node'), 0o755);

  execFileSync('/bin/bash', [scriptPath, '--proxy-only'], {
    env: {
      HOME: home,
      ANICCA_HOME: instance,
      OBSERVED_ENV: observed,
      PATH: `${bin}:/usr/bin:/bin`,
      BLOCKRUN_WALLET_KEY: 'borrowed',
      PKVAR: 'BORROWED_KEY',
      BASE_CHAIN_WALLET_KEY: 'borrowed',
      ANICCA_EVM_PRIVATE_KEY: 'borrowed',
    },
    stdio: 'pipe',
  });
  assert.equal(readFileSync(observed, 'utf8').trim(), '|||');
});
