// PROP-011, PROP-012 — config.mjs unit tests
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../config.mjs';

// PROP-011: env wins over .env file values
test('PROP-011: process.env wins over .env file values', () => {
  const processEnv = { SLEEP_BASE_S: '5', ANICCA_HOME: '/tmp/test' };
  const dotenvText = 'SLEEP_BASE_S=999\nANICCA_HOME=/other\n';
  const config = loadConfig(processEnv, dotenvText);
  assert.equal(config.SLEEP_BASE_S, 5); // from process env
  assert.equal(config.ANICCA_HOME, '/tmp/test'); // from process env
});

// PROP-011: .env value used when env var not set
test('PROP-011: .env value used when env var absent', () => {
  const processEnv = { ANICCA_HOME: '/tmp/test' };
  const dotenvText = 'SLEEP_BASE_S=30\n';
  const config = loadConfig(processEnv, dotenvText);
  assert.equal(config.SLEEP_BASE_S, 30);
});

// PROP-012: malformed .env line is skipped, valid lines still loaded
test('PROP-012: malformed .env line skipped, valid lines parsed', () => {
  const processEnv = { ANICCA_HOME: '/tmp/test' };
  const dotenvText = [
    'SLEEP_BASE_S=10',
    'THIS IS NOT VALID',
    '=ALSO_INVALID',
    'SLEEP_ERROR_S=30',
    '',
  ].join('\n');
  assert.doesNotThrow(() => {
    const config = loadConfig(processEnv, dotenvText);
    assert.equal(config.SLEEP_BASE_S, 10);
    assert.equal(config.SLEEP_ERROR_S, 30);
  });
});

// PROP-012: .env with only malformed lines does not crash
test('PROP-012: entirely malformed .env does not crash', () => {
  const processEnv = { ANICCA_HOME: '/tmp/test' };
  const dotenvText = 'TOTALLY GARBAGE\nNOT=\n=ALSOWRONG\n';
  assert.doesNotThrow(() => loadConfig(processEnv, dotenvText));
});

// Default values applied when neither env nor .env sets them
test('PROP-011/012: defaults applied when nothing set', () => {
  const config = loadConfig({ ANICCA_HOME: '/tmp/test' }, '');
  assert.equal(config.OPENAI_BASE_URL, 'http://127.0.0.1:18402/v1');
  assert.equal(config.ANICCA_FREE_MODEL, 'free/glm-4.7');
  assert.equal(config.ANICCA_LEAN_MODEL, 'free/glm-4.7');
  assert.equal(config.ANICCA_FUNDED_MODEL, 'free/glm-4.7');
  assert.equal(config.SLEEP_BASE_S, 120);
  assert.equal(config.SLEEP_ERROR_S, 60);
  assert.equal(config.SLEEP_LOOP_DETECT_S, 300);
  assert.equal(config.SKILL_TIMEOUT_S, 120);
  assert.equal(config.LOOP_DETECT_WINDOW, 3);
  assert.equal(config.BALANCE_CACHE_TTL_S, 300);
  assert.equal(config.LEAN_TIER_THRESHOLD, 1.00);
});

// ANICCA_HOME not set -> error-like config (loop will handle)
test('PROP-011: ANICCA_HOME absent yields empty/null home path', () => {
  const config = loadConfig({}, '');
  assert.equal(config.ANICCA_HOME, undefined);
});

// .env with comments and blank lines
test('PROP-012: .env with # comments and blank lines parsed correctly', () => {
  const processEnv = { ANICCA_HOME: '/tmp/test' };
  const dotenvText = [
    '# this is a comment',
    '',
    'SLEEP_BASE_S=15',
    '  # another comment',
    'SLEEP_ERROR_S=45',
  ].join('\n');
  const config = loadConfig(processEnv, dotenvText);
  assert.equal(config.SLEEP_BASE_S, 15);
  assert.equal(config.SLEEP_ERROR_S, 45);
});
