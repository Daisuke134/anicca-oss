import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('sale observer is wired as a five-minute one-shot LaunchAgent with a private candidate store', () => {
  const runner = readFileSync(new URL('../sale-observer.mjs', import.meta.url), 'utf8');
  const boot = readFileSync(new URL('../sale-observer-boot.sh', import.meta.url), 'utf8');
  const registry = JSON.parse(readFileSync(new URL('../../../../config/loop-registry.json', import.meta.url), 'utf8'));
  const job = registry.loops['x402-sale-observer'];

  assert.match(runner, /appendUniqueSaleCandidates/);
  assert.match(runner, /x402-sale-candidates\.jsonl/);
  assert.match(runner, /import\.meta\.url ===/);
  assert.match(runner, /candidate_not_verified_revenue/);
  assert.doesNotMatch(runner, /console\.(?:log|error)\([^\n]*(?:apiKey|api_key|credentials)/i);
  assert.match(boot, /exec \/usr\/bin\/env node "\$DIR\/sale-observer\.mjs"/);
  assert.equal(job.label, 'ai.anicca.x402-sale-observer');
  assert.equal(job.entrypoint, 'skills/earn/x402-sell/sale-observer-boot.sh');
  assert.equal(job.cadence.start_interval_seconds, 300);
  assert.equal(job.cadence.keep_alive, undefined);
});
