import test from 'node:test';
import assert from 'node:assert/strict';
import { executeClaimedPayout } from '../ubi-payout-watcher.mjs';

test('two concurrent wakes claim one row and send exactly once', async () => {
  let status = 'queued';
  let sends = 0;
  const claimFn = async () => {
    if (status !== 'queued') return false;
    status = 'processing';
    return true;
  };
  const settleFn = async (_id, next) => { status = next; };
  const payFn = async () => {
    sends += 1;
    return { status: '0x1', tx: '0xabc', amount_base: 250000 };
  };
  const input = { row: { id: 1, notes: 'method=wallet' }, to: '0x1111111111111111111111111111111111111111', amountBase: 250000, claimFn, payFn, settleFn };

  const results = await Promise.all([executeClaimedPayout(input), executeClaimedPayout(input)]);

  assert.equal(sends, 1);
  assert.equal(status, 'paid');
  assert.deepEqual(results.map((r) => r.outcome).sort(), ['already_claimed', 'paid']);
});

test('confirmed send followed by settle failure is never sent again', async () => {
  let status = 'queued';
  let sends = 0;
  let failPaidSettle = true;
  const claimFn = async () => {
    if (status !== 'queued') return false;
    status = 'processing';
    return true;
  };
  const settleFn = async (_id, next) => {
    if (next === 'paid' && failPaidSettle) {
      failPaidSettle = false;
      throw new Error('database unavailable after broadcast');
    }
    status = next;
  };
  const payFn = async () => {
    sends += 1;
    return { status: '0x1', tx: '0xabc', amount_base: 250000 };
  };
  const input = { row: { id: 1, notes: 'method=wallet' }, to: '0x1111111111111111111111111111111111111111', amountBase: 250000, claimFn, payFn, settleFn };

  const first = await executeClaimedPayout(input);
  const second = await executeClaimedPayout(input);

  assert.equal(first.outcome, 'needs_review');
  assert.equal(second.outcome, 'already_claimed');
  assert.equal(status, 'needs_review');
  assert.equal(sends, 1);
});
