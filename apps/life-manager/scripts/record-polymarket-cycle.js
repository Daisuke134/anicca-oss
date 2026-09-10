#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { recordPolymarketCycle } = require("../lib/polymarket-cycle.js");
const { generateMonthlyReport } = require("../lib/earnings-runtime.js");
const { createJsonlFinancialRecordStore } = require("../lib/financial-record-store.js");
const { createPolymarketFinancialWriter } = require("../lib/earnings-financial-record.js");

const PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB";
const PUSD_DECIMALS = 6;
const DEFAULT_POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com";
const LOSS_CAUSE = "片側だけが約定し、勝ち側を持たずに解決したこと";
const LOSS_PLAN = "両脚成立を確認できないcycleを停止し、片側約定を即時解消すること";

function arg(argv, name, fallback = null) {
  const index = argv.indexOf(`--${name}`);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
}

async function rpc(fetchImpl, rpcUrl, method, params) {
  const response = await fetchImpl(rpcUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  if (!response || !response.ok) {
    throw new Error(`Polygon RPC ${method} failed (${response ? response.status : "no response"})`);
  }
  const body = await response.json();
  if (body.error) throw new Error(`Polygon RPC ${method} failed: ${body.error.message}`);
  if (body.result == null) throw new Error(`Polygon RPC ${method} returned no result`);
  return body.result;
}

async function verifyTransactionReceipt(txHash, label, fetchImpl, rpcUrl) {
  const receipt = await rpc(fetchImpl, rpcUrl, "eth_getTransactionReceipt", [txHash]);
  if (receipt.status !== "0x1") throw new Error(`${label} receipt is not successful (${receipt.status})`);
  if (String(receipt.transactionHash).toLowerCase() !== String(txHash).toLowerCase()) {
    throw new Error(`${label} receipt transaction hash does not match the cycle`);
  }
  return receipt;
}

async function readPusdBalanceAtomic(walletAddress, fetchImpl, rpcUrl) {
  const data = `0x70a08231${String(walletAddress).slice(2).toLowerCase().padStart(64, "0")}`;
  const result = await rpc(fetchImpl, rpcUrl, "eth_call", [{ to: PUSD, data }, "latest"]);
  return BigInt(result).toString();
}

async function main(deps = {}, argv = process.argv.slice(2)) {
  const readFile = deps.readFile || fs.readFileSync;
  const fetchImpl = deps.fetchImpl || globalThis.fetch;
  const recordCycle = deps.recordCycle || recordPolymarketCycle;
  const generateReport = deps.generateReport || generateMonthlyReport;
  const writeOutput = deps.writeOutput || ((text) => process.stdout.write(text));
  const env = deps.env || process.env;
  if (typeof fetchImpl !== "function") throw new Error("a fetch implementation is required");

  const evidencePath = arg(argv, "evidence");
  if (!evidencePath) throw new Error("--evidence <cycle.json> is required");
  const monthArg = String(arg(argv, "month") || "");
  const [year, month] = monthArg.split("-").map(Number);
  if (!/^\d{4}-\d{2}$/.test(monthArg) || !Number.isInteger(year) || month < 1 || month > 12) {
    throw new Error("--month YYYY-MM is required");
  }
  const timezone = arg(argv, "timezone", env.LM_TIMEZONE || "Asia/Tokyo");
  const rpcUrl = env.POLYGON_RPC || DEFAULT_POLYGON_RPC;
  const cycle = JSON.parse(readFile(evidencePath, "utf8"));

  const tradeReceipt = await verifyTransactionReceipt(cycle.trade_tx_hash, "trade", fetchImpl, rpcUrl);
  const receipt = await verifyTransactionReceipt(cycle.redeem_tx_hash, "redeem", fetchImpl, rpcUrl);
  const receipts = {
    [String(cycle.trade_tx_hash).toLowerCase()]: tradeReceipt,
    [String(cycle.redeem_tx_hash).toLowerCase()]: receipt,
  };
  const subjectId = env.LM_CFO_SUBJECT_ID || env.LM_CFO_UID || env.LM_UID || "local";
  const financialStore = deps.financialStore || createJsonlFinancialRecordStore({
    directoryPath: env.LM_FINANCIAL_RECORDS_DIR
      || path.join(env.CFO_STATE_DIR || path.join(os.homedir(), ".local", "state", "life-manager", "life-manager-cfo-hourly"), "financial-records"),
  });
  const recordEntry = createPolymarketFinancialWriter({
    store: financialStore, subjectId, receipts,
  });
  const record = await recordCycle(cycle, { recordEntry });
  const report = await generateReport({
    year,
    month,
    timezone,
    walletAddress: cycle.wallet_address,
    readBalanceAtomic: () => readPusdBalanceAtomic(cycle.wallet_address, fetchImpl, rpcUrl),
    balanceDecimals: PUSD_DECIMALS,
    explorerBaseUrl: "polygonscan.com",
    cause: LOSS_CAUSE,
    plan: LOSS_PLAN,
  });

  const result = {
    ok: true,
    cycle_id: cycle.cycle_id,
    receipt: {
      status: receipt.status,
      block_number: receipt.blockNumber,
      transaction_hash: receipt.transactionHash,
    },
    record,
    report,
  };
  writeOutput(`${JSON.stringify(result, null, 2)}\n\n${report.text}\n`);
  return result;
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error && error.message ? error.message : error}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  PUSD,
  PUSD_DECIMALS,
  DEFAULT_POLYGON_RPC,
  readPusdBalanceAtomic,
  main,
};
