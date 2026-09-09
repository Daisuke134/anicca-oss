import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { runExperimentTick } from '../experiment-tick.mjs';

test('independent tick activates only a newly applied experiment', async () => {
  const calls = [];
  const result = await runExperimentTick({}, {
    improve: () => ({ experiment: { action: 'applied', price: '$0.012' } }),
    activate: async () => { calls.push('activate'); return { activated: true }; },
  });
  assert.deepEqual(calls, ['activate']);
  assert.equal(result.activation.activated, true);
});

test('waiting tick is read-only and does not restart the seller', async () => {
  let activated = false;
  const result = await runExperimentTick({}, {
    improve: () => ({ experiment: { action: 'waiting', price: '$0.015' } }),
    activate: async () => { activated = true; },
  });
  assert.equal(activated, false);
  assert.equal(result.experiment.action, 'waiting');
});

test('registry runs the franklin1 controller independently every five minutes', () => {
  const registry = JSON.parse(readFileSync(new URL('../../../../config/loop-registry.json', import.meta.url), 'utf8'));
  const job = registry.loops['x402-experiment-franklin1'];

  assert.equal(job.label, 'ai.anicca.x402-experiment-franklin1');
  assert.equal(job.entrypoint, 'skills/earn/x402-sell/experiment-tick.mjs');
  assert.deepEqual(job.cadence, { start_interval_seconds: 300 });
  assert.equal(job.provider_route, 'deterministic');
});
