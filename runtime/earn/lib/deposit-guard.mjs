// deposit-guard.mjs — read-after-write proof that a yield deposit ACTUALLY moved money.
//
// Why: execute-yield used to record cost-basis on `tx.status === "success"`. But a tx can succeed and
// still deposit nothing (wrong vault / no-op / silent revert path), producing a PHANTOM basis — exactly
// the `morpho: $1` in cost-basis.json while the on-chain morpho balance was 0. Recording phantom basis
// inflates net worth and makes the dashboard lie. The only honest proof a deposit landed is that LIQUID
// USDC strictly dropped by ~the amount we tried to deposit.
//
// Pure + bigint so it is unit-testable without any chain access.
//
// @param statusOk  tx receipt status === "success"
// @param liqBefore liquid USDC (6-dec) read BEFORE the deposit
// @param liqAfter  liquid USDC (6-dec) read AFTER  the deposit
// @param surplus   amount (6-dec) we attempted to deposit
// @returns true iff the deposit verifiably landed (record cost-basis only then)
export function depositLanded({ statusOk, liqBefore, liqAfter, surplus }) {
  if (!statusOk) return false;
  if (BigInt(surplus) <= 0n) return false; // nothing to deposit → never "landed" (avoids moved>=0 trivially true)
  const moved = BigInt(liqBefore) - BigInt(liqAfter);
  if (moved <= 0n) return false; // liquid did not drop (or rose) → no deposit happened
  // require ≥99% of the intended amount left the wallet (1% tolerance for dust/rounding only)
  return moved >= (BigInt(surplus) * 99n) / 100n;
}
