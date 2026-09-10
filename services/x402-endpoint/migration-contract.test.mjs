import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const EXPECTED_PAID_ROUTES = Object.freeze({
  'POST /context-compressor': '0.008',
  'POST /emotion-detector': '0.01',
  'POST /buddhist-counsel': '0.01',
  'POST /focus-coach': '0.01',
  'POST /habit-designer': '0.01',
  'POST /prompt-sanitizer': '0.005',
  'POST /decision-clarifier': '0.008',
  'POST /intent-router': '0.005',
  'GET /funding-rates': '0.01',
});

test('canonical endpoint owns the complete production paid-route catalog', async () => {
  const { PAID_ROUTE_CATALOG, buildOpenApiDocument } = await import('./src/lib/discovery.js');

  assert.deepEqual(
    Object.keys(PAID_ROUTE_CATALOG).sort(),
    Object.keys(EXPECTED_PAID_ROUTES).sort(),
  );

  const document = buildOpenApiDocument({
    origin: 'https://x402-agents-production.up.railway.app',
  });
  const documentedRoutes = Object.entries(document.paths)
    .flatMap(([path, methods]) => Object.keys(methods).map(method => `${method.toUpperCase()} ${path}`))
    .sort();

  assert.deepEqual(documentedRoutes, Object.keys(EXPECTED_PAID_ROUTES).sort());
});

test('Railway healthcheck allows facilitator startup time', async () => {
  const railway = await readFile(new URL('./railway.toml', import.meta.url), 'utf8');
  const deploy = railway.match(/^\[deploy\]\n([\s\S]*)$/m)?.[1];

  assert.ok(deploy, 'railway.toml must define a deploy section');
  assert.match(deploy, /^healthcheckPath = "\/health"$/m);
  assert.match(deploy, /^healthcheckTimeout = 60$/m);
});

test('Railway limits x402 deploys to the x402 service subtree', async () => {
  const railway = await readFile(new URL('./railway.toml', import.meta.url), 'utf8');

  assert.match(railway, /^watchPatterns = \["\/services\/x402-endpoint\/\*\*"\]$/m);
});
