// VSDD RED→GREEN: cost-basis accounting must be correct or the dashboard's revenue numbers are lies.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "fs";
import os from "os";

// isolate state to a temp HOME so we never touch the real ledger
const TMP = fs.mkdtempSync(os.tmpdir() + "/cb-");
process.env.ANICCA_HOME = TMP;
fs.mkdirSync(TMP + "/skills/earn/state", { recursive: true });

const { readCostBasis, recordDeposit, recordWithdraw, seedIfEmpty } = await import("../cost-basis.mjs");

test("deposit accumulates basis per venue", () => {
  recordDeposit("beefy", 1.5);
  recordDeposit("beefy", 0.5);
  recordDeposit("aave", 0.2);
  assert.equal(readCostBasis().beefy, 2.0);
  assert.equal(readCostBasis().aave, 0.2);
});

test("withdraw reduces basis (money out is principal-out, not loss)", () => {
  recordWithdraw("beefy", 0.5);
  assert.equal(readCostBasis().beefy, 1.5);
});

test("basis floors at 0 — never negative (excess withdrawal = realised profit elsewhere)", () => {
  recordWithdraw("aave", 5);
  assert.equal(readCostBasis().aave, 0);
});

test("seedIfEmpty only sets untracked venues, never overwrites", () => {
  // use real writable venues (VENUE_KEY in execute-yield only ever writes aave/fluid/beefy) — do NOT
  // seed `morpho`, which has no deposit path and was the phantom key removed in fix #8.
  seedIfEmpty({ beefy: 99, fluid: 0.45, moonwell: 1.0 });
  const o = readCostBasis();
  assert.equal(o.beefy, 1.5, "existing beefy basis preserved");
  assert.equal(o.fluid, 0.45, "new venue seeded");
  assert.equal(o.moonwell, 1.0, "new venue seeded");
});

test("ignores garbage input", () => {
  assert.equal(recordDeposit("", 1), null);
  assert.equal(recordDeposit("x", NaN), null);
});
