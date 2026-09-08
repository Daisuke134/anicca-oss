// VSDD RED→GREEN: a yield deposit must only be recorded as cost-basis if money ACTUALLY moved.
// "tx.status === success" is NOT proof — a tx can succeed yet deposit nothing (phantom). The guard
// is: liquid USDC strictly dropped by ~the deposited amount. This kills phantom basis (the morpho $1
// that existed in cost-basis.json while on-chain morpho balance was 0).
import { test } from "node:test";
import assert from "node:assert/strict";
import { depositLanded } from "../deposit-guard.mjs";

const S = 1_000_000n; // 1 USDC (6-dec)

test("status ok + liquid dropped by full surplus → landed", () => {
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 5_000_000n, surplus: S }), true);
});

test("status ok + liquid dropped by 99% (gas/dust tolerance) → landed", () => {
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 5_010_000n, surplus: S }), true);
});

test("status ok BUT liquid unchanged (PHANTOM) → NOT landed", () => {
  // this is the exact bug: tx 'succeeded' but no money moved → must NOT be recorded
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 6_000_000n, surplus: S }), false);
});

test("status ok but liquid dropped only a little (partial/no real deposit) → NOT landed", () => {
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 5_900_000n, surplus: S }), false);
});

test("tx reverted (status not ok) → NOT landed", () => {
  assert.equal(depositLanded({ statusOk: false, liqBefore: 6_000_000n, liqAfter: 5_000_000n, surplus: S }), false);
});

test("EDGE: negative delta (liquid INCREASED mid-cycle) → NOT landed", () => {
  assert.equal(depositLanded({ statusOk: true, liqBefore: 5_000_000n, liqAfter: 6_000_000n, surplus: S }), false);
});

test("EDGE: surplus 0 (nothing to deposit) → NOT landed (no trivial moved>=0 pass)", () => {
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 6_000_000n, surplus: 0n }), false);
});

test("BOUNDARY: one unit under the 99% threshold → NOT landed", () => {
  // threshold for S=1e6 is 990000; a drop of 989999 (liqAfter 5_010_001) must fail
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 5_010_001n, surplus: S }), false);
});

test("BOUNDARY: exactly at the 99% threshold → landed", () => {
  // a drop of exactly 990000 (liqAfter 5_010_000) must pass
  assert.equal(depositLanded({ statusOk: true, liqBefore: 6_000_000n, liqAfter: 5_010_000n, surplus: S }), true);
});
