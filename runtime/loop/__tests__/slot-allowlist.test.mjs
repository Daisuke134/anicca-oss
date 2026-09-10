// slot-allowlist.mjs unit tests (x402-zero-to-one 2026-07-14).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applySlotAllowlist } from '../slot-allowlist.mjs';

const REG = () => ({
  slots: {
    x402_sell: { status: 'live', risk: 'safe' },
    'earn/sol-trade': { status: 'live', risk: 'capital' },
    report: { status: 'live', alwaysAvailable: true },
    cook: { status: 'live' },
  },
});

test('empty env -> registry untouched, applied null', () => {
  const reg = REG();
  const { registry, applied } = applySlotAllowlist(reg, '');
  assert.equal(registry, reg);
  assert.equal(applied, null);
});

test('allowlist keeps named slot + alwaysAvailable, drops the rest', () => {
  const { registry, applied } = applySlotAllowlist(REG(), 'x402_sell');
  assert.deepEqual(Object.keys(registry.slots).sort(), ['report', 'x402_sell']);
  assert.deepEqual(applied, ['x402_sell']);
});

test('comma list + whitespace + unknown names tolerated', () => {
  const { registry } = applySlotAllowlist(REG(), ' x402_sell , nonexistent ,earn/sol-trade');
  assert.deepEqual(Object.keys(registry.slots).sort(), ['earn/sol-trade', 'report', 'x402_sell']);
});

test('strict Cloud allowlist removes local-only alwaysAvailable utilities', () => {
  const { registry } = applySlotAllowlist(REG(), 'x402_sell', { preserveAlwaysAvailable: false });
  assert.deepEqual(Object.keys(registry.slots), ['x402_sell']);
});

test('does not mutate the input registry', () => {
  const reg = REG();
  applySlotAllowlist(reg, 'x402_sell');
  assert.equal(Object.keys(reg.slots).length, 4);
});

test('malformed registry -> untouched, no throw', () => {
  const { registry, applied } = applySlotAllowlist(null, 'x402_sell');
  assert.equal(registry, null);
  assert.equal(applied, null);
});
