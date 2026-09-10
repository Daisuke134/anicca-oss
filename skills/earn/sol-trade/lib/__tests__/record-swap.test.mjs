// node:test — recordSwap (REQ-002 / PROP-005, PROP-007, PROP-016): the sol-trade RECORD executor.
// Injected fetch -> no network. Mirrors clip-promote/tests/test_record_payout.mjs's ACTUAL proven
// pattern exactly (same imports/injectable-opts technique), with recordSwap's ONE deliberate
// difference from recordPayout: it accepts ANY confirmed delta (positive, negative, or zero) since
// sol-trade must record losses too (P&L VISIBILITY, not a payout-confirmation gate). No
// `franklin-trading balance` CLI read is used anywhere here (FIND-009/FIND-010).
import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { recordSwap } from "../record-swap.mjs";
import { readLedger, isProfitable } from "../../../../_shared/lib/ledger.mjs";

const WALLET = "8FpqdcCHqjqkVXR58eVJa53neXbJf9emXhvHhgeUPCV9";
const USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const SIG = "52RZBMCUqGWrWPZCJo82xDaYusUhGNFNCEzW51nyJNhFN9AECae9dWyp8TQQZMP731LpP2Xu8JDpJDRxp3xCRD7m";
const SIG_2 = "62RZBMCUqGWrWPZCJo82xDaYusUhGNFNCEzW51nyJNhFN9AECae9dWyp8TQQZMP731LpP2Xu8JDpJDRxp3xCRD7m";

async function tmpFile() {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), "sol-trade-record-"));
  return path.join(d, "earn-ledger.jsonl");
}

// pre=1.0 always; post=pre+delta -- usdcDeltaForSig computes (post-pre)=delta exactly, for any sign.
function fetchFor({ status = "finalized", err = null, delta = 0, malformed = false }) {
  return async (_rpc, init) => {
    if (malformed) return { ok: false, status: 500, json: async () => ({}) };
    const body = JSON.parse(init.body);
    if (body.method === "getSignatureStatuses") {
      return { ok: true, json: async () => ({ result: { value: [{ confirmationStatus: status, err }] } }) };
    }
    if (body.method === "getTransaction") {
      const pre = 1.0;
      const post = pre + delta;
      const meta = {
        err: null,
        preTokenBalances: [{ accountIndex: 3, uiTokenAmount: { amount: String(Math.round(pre * 1e6)), decimals: 6, uiAmount: pre, uiAmountString: String(pre) } }],
        postTokenBalances: [{ accountIndex: 3, owner: WALLET, mint: USDC, uiTokenAmount: { amount: String(Math.round(post * 1e6)), decimals: 6, uiAmount: post, uiAmountString: String(post) } }],
      };
      return { ok: true, json: async () => ({ result: { meta } }) };
    }
    throw new Error("unexpected method " + body.method);
  };
}

test("recordSwap: bad-args (missing sig/wallet/ledger) never crashes, never records", async () => {
  const ledger = await tmpFile();
  assert.equal((await recordSwap({ sig: null, wallet: WALLET, ledger }, {})).status, "bad-args");
  assert.equal((await recordSwap({ sig: SIG, wallet: null, ledger }, {})).status, "bad-args");
  assert.equal((await recordSwap({ sig: SIG, wallet: WALLET, ledger: null }, {})).status, "bad-args");
  assert.deepEqual(await readLedger(ledger), []);
});

test("PROP-005: confirmed + POSITIVE delta -> recorded, earn_usdc=delta, cost_usdc=0, persisted line isProfitable-eligible shape", async () => {
  const ledger = await tmpFile();
  const r = await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: fetchFor({ delta: 0.5 }) });
  assert.equal(r.status, "recorded");
  assert.equal(r.earn_usdc, 0.5);
  assert.equal(r.cost_usdc, 0);
  const rows = await readLedger(ledger);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].sig, SIG);
  assert.equal(rows[0].chain, "solana");
  assert.equal(rows[0].source, "sol-trade");
  assert.equal(rows[0].net_usdc, 0.5);
});

test("PROP-007: confirmed + NEGATIVE delta -> STILL recorded (earn_usdc=0, cost_usdc=|delta|, net_usdc<0) -- losses are visible too", async () => {
  const ledger = await tmpFile();
  const r = await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: fetchFor({ delta: -0.3 }) });
  assert.equal(r.status, "recorded");
  assert.equal(r.earn_usdc, 0);
  assert.equal(r.cost_usdc, 0.3);
  const rows = await readLedger(ledger);
  assert.equal(rows.length, 1);
  assert.ok(rows[0].net_usdc < 0);
});

test("recordSwap: confirmed + ZERO delta -> still recorded (net_usdc=0), never rejected as 'zero-delta' (deliberate difference from record-payout.mjs)", async () => {
  const ledger = await tmpFile();
  const r = await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: fetchFor({ delta: 0 }) });
  assert.equal(r.status, "recorded");
  const rows = await readLedger(ledger);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].net_usdc, 0);
});

test("recordSwap: unconfirmed signature -> NOT recorded (dropped tx must never masquerade as a completed pass)", async () => {
  const ledger = await tmpFile();
  const r = await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: fetchFor({ status: "processed", delta: 0.5 }) });
  assert.equal(r.status, "unconfirmed");
  assert.deepEqual(await readLedger(ledger), []);
});

test("PROP-016: sigStatus RPC failure -> verify-error status, never a thrown exception, never recorded", async () => {
  const ledger = await tmpFile();
  const r = await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: fetchFor({ malformed: true }) });
  assert.equal(r.status, "verify-error");
  assert.deepEqual(await readLedger(ledger), []);
});

test("PROP-016: usdcDeltaForSig RPC failure (getTransaction throws after getSignatureStatuses succeeds) -> verify-error, never NaN/Infinity in a recorded line", async () => {
  const ledger = await tmpFile();
  const flaky = async (_rpc, init) => {
    const body = JSON.parse(init.body);
    if (body.method === "getSignatureStatuses") {
      return { ok: true, json: async () => ({ result: { value: [{ confirmationStatus: "finalized", err: null }] } }) };
    }
    return { ok: false, status: 500, json: async () => ({}) };
  };
  const r = await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: flaky });
  assert.equal(r.status, "verify-error");
  assert.deepEqual(await readLedger(ledger), []);
});

test("recordSwap never sets external:true (P&L VISIBILITY only -- this is not a GATE-0 classification)", async () => {
  const ledger = await tmpFile();
  await recordSwap({ sig: SIG, wallet: WALLET, ledger, wake: "w1" }, { fetchImpl: fetchFor({ delta: 0.5 }) });
  const rows = await readLedger(ledger);
  assert.notEqual(rows[0].external, true);
  assert.equal(isProfitable(rows[0]), false, "sol-trade lines never claim GATE-0 (no external:true)");
});

test("multi-transaction round trip records the sum, not only the sale proceeds", async () => {
  const ledger = await tmpFile();
  const deltas = new Map([[SIG, -1], [SIG_2, 1.2]]);
  const fetchImpl = async (_rpc, init) => {
    const body = JSON.parse(init.body);
    const signature = Array.isArray(body.params[0]) ? body.params[0][0] : body.params[0];
    if (body.method === "getSignatureStatuses") {
      return { ok: true, json: async () => ({ result: { value: [{ confirmationStatus: "finalized", err: null }] } }) };
    }
    const delta = deltas.get(signature);
    const pre = 2;
    return { ok: true, json: async () => ({ result: { meta: {
      preTokenBalances: [{ accountIndex: 3, uiTokenAmount: { uiAmount: pre } }],
      postTokenBalances: [{ accountIndex: 3, owner: WALLET, mint: USDC, uiTokenAmount: { uiAmount: pre + delta } }],
    } } }) };
  };
  const result = await recordSwap({ sigs: [SIG, SIG_2], wallet: WALLET, ledger }, { fetchImpl });
  assert.equal(result.net_usdc, 0.2);
  assert.deepEqual(result.signatures, [SIG, SIG_2]);
  assert.equal((await readLedger(ledger))[0].net_usdc, 0.2);
});

test("a legacy duplicate still repairs a missing common FinancialRecord", async () => {
  const ledger = await tmpFile();
  await recordSwap({ sig: SIG, wallet: WALLET, ledger }, { fetchImpl: fetchFor({ delta: 0.2 }) });
  let financialCalls = 0;
  const result = await recordSwap({ sig: SIG, wallet: WALLET, ledger }, {
    fetchImpl: fetchFor({ delta: 0.2 }),
    recordFinancial: async (value) => { financialCalls += 1; return { ok: true, value }; },
  });
  assert.equal(result.status, "duplicate");
  assert.equal(financialCalls, 1);
  assert.equal((await readLedger(ledger)).length, 1);
});
