import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

test('franklin1 image boot pins its identity, local port, public origin, and additive funnel path', () => {
  const boot = fs.readFileSync(path.join(ROOT, 'image-franklin1-boot.sh'), 'utf8');

  assert.match(boot, /source .*runtime-env\.sh/);
  assert.ok(boot.includes('export ANICCA_HOME="$HOME/.blockrun"'));
  assert.match(boot, /unset BLOCKRUN_WALLET_KEY/);
  assert.ok(boot.includes('export X402_PAYTO="0x3EcCAD24794ca298D25378E9902A251322ea8749"'));
  assert.ok(boot.includes('export X402_IMAGE_PORT="8093"'));
  // T2 fix (2026-07-25): aniccanomac-mini-1 has no public DNS record at all (dig @8.8.8.8/@1.1.1.1
  // both empty) and its `tailscale funnel --bg` calls now hang forever (Funnel ACL/authorization
  // lost) — no outside buyer could ever have reached that URL. Moved to a dedicated tsbridge tsnet
  // node (same proven pattern as franklin1/franklin2/claude-p), which has working public DNS.
  assert.ok(boot.includes('export X402_IMAGE_PUBLIC_URL="https://franklin1-image.tail7a0ba4.ts.net"'));
  assert.ok(boot.includes('export X402_IMAGE_UPSTREAM_OPENAPI="http://127.0.0.1:8411/openapi.json"'));
  // tsbridge proxies the whole backend at its own root — no --set-path funnel calls needed anymore.
  assert.ok(!boot.includes('/opt/homebrew/bin/tailscale funnel'));
  assert.match(boot, /exec \/usr\/bin\/env node "\$DIR\/image-server\.mjs"/);
});

test('franklin2 image boot pins its identity, local port, public origin, and additive funnel path', () => {
  const boot = fs.readFileSync(path.join(ROOT, 'image-franklin2-boot.sh'), 'utf8');

  assert.match(boot, /source .*runtime-env\.sh/);
  assert.ok(boot.includes('export ANICCA_HOME="$HOME/.franklin2-home/.blockrun"'));
  assert.match(boot, /unset BLOCKRUN_WALLET_KEY/);
  assert.ok(boot.includes('export X402_PAYTO="0xe7747Fd899D8987821Bb4CB3D6aDf22565F87ce9"'));
  assert.ok(boot.includes('export X402_IMAGE_PORT="8094"'));
  // T2 fix (2026-07-25): same dead-node problem as franklin1-image; moved to a dedicated tsbridge
  // tsnet node with working public DNS instead of aniccanomac-mini-1.
  assert.ok(boot.includes('export X402_IMAGE_PUBLIC_URL="https://franklin2-image.tail7a0ba4.ts.net"'));
  assert.ok(boot.includes('export X402_IMAGE_UPSTREAM_OPENAPI="http://127.0.0.1:8413/openapi.json"'));
  assert.ok(!boot.includes('/opt/homebrew/bin/tailscale funnel'));
  assert.match(boot, /exec \/usr\/bin\/env node "\$DIR\/image-server\.mjs"/);
});

test('claude-p image boot pins its identity, local port, public origin, and additive funnel path', () => {
  const boot = fs.readFileSync(path.join(ROOT, 'image-claude-p-boot.sh'), 'utf8');

  assert.match(boot, /source .*runtime-env\.sh/);
  assert.ok(boot.includes('export ANICCA_HOME="$HOME/.anicca-founder"'));
  assert.match(boot, /unset BLOCKRUN_WALLET_KEY/);
  assert.ok(boot.includes('export X402_PAYTO="0x810F6D61F7606dEEE2657d3083E150a222Bc29C5"'));
  assert.ok(boot.includes('export X402_IMAGE_PORT="8095"'));
  // T2 fix (2026-07-25): same dead-node problem as franklin1-image; moved to a dedicated tsbridge
  // tsnet node with working public DNS instead of aniccanomac-mini-1.
  assert.ok(boot.includes('export X402_IMAGE_PUBLIC_URL="https://claude-p-image.tail7a0ba4.ts.net"'));
  assert.ok(boot.includes('export X402_IMAGE_UPSTREAM_OPENAPI="http://127.0.0.1:8412/openapi.json"'));
  assert.ok(!boot.includes('/opt/homebrew/bin/tailscale funnel'));
  assert.match(boot, /exec \/usr\/bin\/env node "\$DIR\/image-server\.mjs"/);
});
