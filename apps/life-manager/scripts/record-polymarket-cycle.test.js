"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  PUSD,
  main,
} = require("./record-polymarket-cycle.js");

const CONDITION = "0x5ecd0d050ea3e753b787ad8ef3b023448b78d232ebe28b24b3d18bf878fb8b5d";
const WALLET = "0x904B50d2e214Da947d83D6a2D32c4E3Ffc17Eb74";
const TRADE_TX = "0xe6bbfb7d610a774f4548af9393930e99039f0a33f6aaeae34d7fe1f240321659";
const REDEEM_TX = "0xdfaf37b33da21da10ba0398ccbe4e853d8111e2867606a4c81b85acc454086ef";

function evidence() {
  return {
    cycle_id: `polymarket:${CONDITION}`,
    wallet_address: WALLET,
    condition_id: CONDITION,
    deployed_microusd: "3150000",
    recovered_microusd: "0",
    fee_microusd: "0",
    realized_pnl_microusd: "-3150000",
    occurred_at: "2026-07-27T04:05:43.000Z",
    trade_tx_hash: TRADE_TX,
    redeem_tx_hash: REDEEM_TX,
    receipt_status: "0x1",
    evidence: { pusd_decimals: 6 },
  };
}

function rpcResponse(result, ok = true) {
  return {
    ok,
    status: ok ? 200 : 503,
    json: async () => ok ? { jsonrpc: "2.0", id: 1, result } : {},
  };
}

test("the command verifies the receipt, records the cycle, and reports a fresh pUSD balance", async () => {
  const rpcCalls = [];
  const sequence = [];
  const financialRecords = [];
  let output = "";
  const fetchImpl = async (_url, init) => {
    const body = JSON.parse(init.body);
    rpcCalls.push(body);
    if (body.method === "eth_getTransactionReceipt") {
      return rpcResponse({ status: "0x1", transactionHash: body.params[0] });
    }
    if (body.method === "eth_call") return rpcResponse("0x4379e6");
    throw new Error(`unexpected RPC method ${body.method}`);
  };
  const recordCycle = async (cycle, options) => {
    sequence.push("record");
    assert.equal(cycle.condition_id, CONDITION);
    await options.recordEntry({
      entry_key: `${cycle.cycle_id}:loss`, kind: "financial_realized_loss",
      amount_minor: 315, occurred_at: cycle.occurred_at, tx_hash: REDEEM_TX,
      source: "polymarket_cycle", meta: { redeem_tx_hash: REDEEM_TX },
    });
    return {
      ok: true,
      cycle_id: cycle.cycle_id,
      entries: [{ entry_key: `${cycle.cycle_id}:loss` }],
      writes: [{ ok: true, duplicate: false }],
    };
  };
  const generateReport = async (request) => {
    sequence.push("report");
    assert.equal(request.walletAddress, WALLET);
    assert.equal(request.balanceDecimals, 6);
    assert.equal(await request.readBalanceAtomic(), "4422118");
    return {
      summary: { net_minor: -315, balance_atomic: "4422118", balance_decimals: 6 },
      text: "・収益: -$3.15（マイナスでした）\n・私の残高: $4.422118",
    };
  };

  const result = await main({
    readFile: () => JSON.stringify(evidence()),
    fetchImpl,
    recordCycle,
    financialStore: { append: async (record) => {
      financialRecords.push(record);
      return { created: true, record };
    } },
    generateReport,
    writeOutput: (text) => { output += text; },
    env: {},
  }, ["--evidence", "cycle.json", "--month", "2026-07"]);

  assert.deepEqual(sequence, ["record", "report"]);
  assert.equal(result.record.writes[0].duplicate, false);
  assert.deepEqual([financialRecords[0].kind, financialRecords[0].direction], ["business_cost", "debit"]);
  assert.match(financialRecords[0].verification.evidence_refs[0], /^eip155:\/\/137\/tx\//);
  assert.match(output, /-\$3\.15/);
  assert.match(output, /\$4\.422118/);
  assert.equal(rpcCalls[0].method, "eth_getTransactionReceipt");
  assert.deepEqual(rpcCalls.slice(0, 2).map((call) => call.params[0]), [TRADE_TX, REDEEM_TX]);
  const balanceCall = rpcCalls.find((call) => call.method === "eth_call");
  assert.equal(balanceCall.params[0].to, PUSD);
  assert.equal(balanceCall.params[0].data, `0x70a08231${WALLET.slice(2).toLowerCase().padStart(64, "0")}`);
});

test("a failed redeem receipt aborts before the ledger write", async () => {
  let writes = 0;
  await assert.rejects(() => main({
    readFile: () => JSON.stringify(evidence()),
    fetchImpl: async (_url, init) => {
      const tx = JSON.parse(init.body).params[0];
      return rpcResponse({ status: tx === REDEEM_TX ? "0x0" : "0x1", transactionHash: tx });
    },
    recordCycle: async () => { writes += 1; },
    generateReport: async () => { throw new Error("must not report"); },
    writeOutput: () => {},
    env: {},
  }, ["--evidence", "cycle.json", "--month", "2026-07"]), /receipt/i);
  assert.equal(writes, 0);
});

test("an RPC transport failure is visible and never becomes a success report", async () => {
  let writes = 0;
  await assert.rejects(() => main({
    readFile: () => JSON.stringify(evidence()),
    fetchImpl: async () => rpcResponse(null, false),
    recordCycle: async () => { writes += 1; },
    generateReport: async () => { throw new Error("must not report"); },
    writeOutput: () => {},
    env: {},
  }, ["--evidence", "cycle.json", "--month", "2026-07"]), /RPC|503/i);
  assert.equal(writes, 0);
});
