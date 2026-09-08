import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('acquisition controller is a five-minute one-shot LaunchAgent with a private action log', () => {
  const runner = readFileSync(new URL('../acquisition-controller.mjs', import.meta.url), 'utf8');
  const boot = readFileSync(new URL('../acquisition-controller-boot.sh', import.meta.url), 'utf8');
  const registry = JSON.parse(readFileSync(new URL('../../../../config/loop-registry.json', import.meta.url), 'utf8'));
  const job = registry.loops['x402-acquisition-controller'];

  assert.match(runner, /runAcquisitionCycle/);
  assert.match(runner, /the402-inbox\.sqlite/);
  assert.match(runner, /x402-acquisition-actions\.jsonl/);
  assert.match(runner, /0o600/);
  assert.doesNotMatch(runner, /Moltbook|posts\/.*comments|create.*post/i);
  assert.match(boot, /exec \/usr\/bin\/env node "\$DIR\/acquisition-controller\.mjs"/);
  assert.equal(job.label, 'ai.anicca.x402-acquisition-controller');
  assert.equal(job.entrypoint, 'skills/earn/x402-sell/acquisition-controller-boot.sh');
  assert.equal(job.cadence.start_interval_seconds, 300);
  assert.equal(job.cadence.keep_alive, undefined);
});
