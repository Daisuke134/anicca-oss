// node:test — extractLastSignature (REQ-002 / PROP-010): deterministic, total parsing of
// franklin-trading's own fixed-format "Signature: <base58>" receipt line. NOT judgment (the
// trading decision already happened; this only reads the CLI's own receipt back).
import { test } from "node:test";
import assert from "node:assert/strict";
import { extractLastSignature, extractSignatures } from "../parse-pass.mjs";

const SIG_A = "A".repeat(88);
const SIG_B = "B".repeat(88);

test("extractLastSignature: no 'Signature:' line -> null (agent judged WAIT)", () => {
  assert.equal(
    extractLastSignature("TradingSignal: neutral -- WAIT (no edge this session)"),
    null,
  );
});

test("extractLastSignature: exactly one 'Signature:' line -> that signature", () => {
  const stdout = `Swapping SOL -> USDC...\nSignature: ${SIG_A}\nSolscan: https://solscan.io/tx/${SIG_A}`;
  assert.equal(extractLastSignature(stdout), SIG_A);
});

test("extractLastSignature: multiple 'Signature:' lines -> the LAST one only (multi-step swap chain)", () => {
  const stdout = `Signature: ${SIG_A}\n...rebalancing...\nSignature: ${SIG_B}\ndone.`;
  assert.equal(extractLastSignature(stdout), SIG_B);
  assert.deepEqual(extractSignatures(stdout), [SIG_A, SIG_B]);
});

test("extractLastSignature: malformed/truncated/empty input never throws, returns null", () => {
  assert.equal(extractLastSignature("Signature: "), null);
  assert.equal(extractLastSignature("Signature:"), null);
  assert.equal(extractLastSignature(""), null);
  assert.equal(extractLastSignature(undefined), null);
  assert.equal(extractLastSignature(null), null);
  assert.equal(extractLastSignature(12345), null);
});

test("extractLastSignature: is total/deterministic across repeated calls on the same input", () => {
  const stdout = `Signature: ${SIG_A}`;
  assert.equal(extractLastSignature(stdout), extractLastSignature(stdout));
});
