import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeRequestBody } from '../model-map.mjs';

test('default ClawRouter free model is forwarded as the verified raw BlockRun free model', () => {
  const input = { model: 'free/glm-4.7', messages: [{ role: 'user', content: 'ping' }] };
  const forwarded = normalizeRequestBody(input, 'anthropic/claude-sonnet-4-6');
  assert.deepEqual(forwarded, {
    model: 'nvidia/gpt-oss-120b',
    messages: [{ role: 'user', content: 'ping' }],
  });
  assert.equal(input.model, 'free/glm-4.7', 'normalization does not mutate the caller request');
});

test('paid profile maps to configured frontier and concrete raw IDs pass through', () => {
  assert.equal(normalizeRequestBody({ model: 'auto' }, 'openai/gpt-5-mini').model, 'openai/gpt-5-mini');
  assert.equal(normalizeRequestBody({ model: 'nvidia/custom' }, 'unused').model, 'nvidia/custom');
});
