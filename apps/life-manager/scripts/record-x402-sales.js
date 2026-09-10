#!/usr/bin/env node
"use strict";

const {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} = require("node:fs");
const { homedir } = require("node:os");
const { basename, join, resolve } = require("node:path");
const { pathToFileURL } = require("node:url");

const {
  recordX402Sale,
  recordX402Work,
  saleLedgerEntry,
} = require("../lib/x402-sale-ledger.js");
const { classifyThe402Revenue } = require("../lib/the402-work-provenance.js");
const { createJsonlFinancialRecordStore } = require("../lib/financial-record-store.js");
const { createLocalFinancialTransitionStore } = require("../lib/financial-transition-local.js");
const { createFinancialEarningsWriter } = require("../lib/earnings-financial-record.js");

const USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
const TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
const DEFAULT_RPC = "https://mainnet.base.org";
const MAX_LEDGER_BYTES = 4 * 1024 * 1024;
const MAX_LINES = 10_000;
const MAX_THE402_RESPONSE_BYTES = 2 * 1024 * 1024;
const LEDGER_NAME = /^external-inflows-(0x[0-9a-fA-F]{40})\.jsonl$/;

function normalizeAddress(value) {
  const raw = String(value == null ? "" : value).trim().toLowerCase();
  return /^0x[0-9a-f]{40}$/.test(raw) ? raw : null;
}

function normalizeTx(value) {
  const raw = String(value == null ? "" : value).trim().toLowerCase();
  return /^0x[0-9a-f]{64}$/.test(raw) ? raw : null;
}

function integerHex(value) {
  try {
    const parsed = Number(BigInt(value));
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
  } catch {
    return null;
  }
}

function topicAddress(address) {
  return `0x${address.slice(2).padStart(64, "0")}`;
}

function ledgerReceiver(path) {
  const match = LEDGER_NAME.exec(basename(path));
  return match ? normalizeAddress(match[1]) : null;
}

function findLedgerPaths(stateDir) {
  if (!existsSync(stateDir)) return [];
  return readdirSync(stateDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && LEDGER_NAME.test(entry.name))
    .map((entry) => join(stateDir, entry.name))
    .sort();
}

function readLedger(path) {
  if (!existsSync(path) || !statSync(path).isFile()) throw new Error("x402 ledger path is not a file");
  if (statSync(path).size > MAX_LEDGER_BYTES) throw new Error("x402 ledger exceeds the bounded reader size");
  const lines = readFileSync(path, "utf8").split("\n").filter((line) => line.trim() !== "");
  if (lines.length > MAX_LINES) throw new Error("x402 ledger exceeds the bounded line count");
  return lines;
}

function exactTransfer(row, receipt) {
  const tx = normalizeTx(row.tx);
  const payTo = normalizeAddress(row.payTo);
  const payer = normalizeAddress(row.from);
  if (!tx || !payTo || !payer || !receipt || receipt.status !== "0x1") return false;
  if (normalizeTx(receipt.transactionHash) !== tx) return false;
  const receiptBlock = integerHex(receipt.blockNumber);
  if (receiptBlock === null || receiptBlock !== row.block) return false;

  const matches = (Array.isArray(receipt.logs) ? receipt.logs : []).filter((log) => (
    normalizeAddress(log && log.address) === USDC_ADDRESS
    && normalizeTx((log && log.transactionHash) || tx) === tx
    && Array.isArray(log && log.topics)
    && log.topics.length >= 3
    && String(log.topics[0]).toLowerCase() === TRANSFER_TOPIC
    && String(log.topics[1]).toLowerCase() === topicAddress(payer)
    && String(log.topics[2]).toLowerCase() === topicAddress(payTo)
    && /^0x[0-9a-f]+$/i.test(String(log.data || ""))
    && BigInt(log.data).toString() === row.usdc_atomic
  ));
  return matches.length === 1;
}

async function verifySaleOnBase(row, boundary, rpcCall, chainContext) {
  if (chainContext.chainId == null) {
    chainContext.chainId = integerHex(await rpcCall("eth_chainId", []));
  }
  if (chainContext.chainId !== 8453) return false;
  if (chainContext.finalizedBlock == null) {
    const finalized = await rpcCall("eth_getBlockByNumber", ["finalized", false]);
    chainContext.finalizedBlock = integerHex(finalized && finalized.number);
  }
  if (chainContext.finalizedBlock == null || row.block > chainContext.finalizedBlock) return false;

  const receipt = await rpcCall("eth_getTransactionReceipt", [row.tx]);
  if (!exactTransfer(row, receipt)) return false;
  const transaction = await rpcCall("eth_getTransactionByHash", [row.tx]);
  if (normalizeTx(transaction && transaction.hash) !== normalizeTx(row.tx)) return false;
  const initiator = normalizeAddress(transaction && transaction.from);
  const selfSet = new Set((boundary.selfWallets || []).map(normalizeAddress).filter(Boolean));
  for (const owned of boundary.ownedPayTos || []) selfSet.add(normalizeAddress(owned));
  return Boolean(initiator && !selfSet.has(initiator));
}

async function processLedgers({
  ledgerPaths,
  selfWallets,
  rpcCall,
  recordSale = recordX402Sale,
  recordWork = recordX402Work,
  classifyRevenue = async () => ({ kind: "sale" }),
}) {
  if (!Array.isArray(ledgerPaths)) throw new TypeError("ledgerPaths must be an array");
  if (!Array.isArray(selfWallets)) throw new TypeError("selfWallets must be an array");
  if (typeof rpcCall !== "function") throw new TypeError("rpcCall must be a function");
  if (typeof recordSale !== "function") throw new TypeError("recordSale must be a function");
  if (typeof recordWork !== "function") throw new TypeError("recordWork must be a function");
  if (typeof classifyRevenue !== "function") throw new TypeError("classifyRevenue must be a function");

  const result = {
    ledgers_seen: ledgerPaths.length,
    lines_seen: 0,
    invalid: 0,
    chain_rejected: 0,
    blocked_subcent: 0,
    recorded: 0,
    duplicates: 0,
    sale_recorded: 0,
    work_recorded: 0,
    provenance_rejected: 0,
    transactions: [],
  };
  const chainContext = { chainId: null, finalizedBlock: null };

  for (const ledgerPath of ledgerPaths) {
    const payTo = ledgerReceiver(ledgerPath);
    const lines = readLedger(ledgerPath);
    result.lines_seen += lines.length;
    for (const line of lines) {
      let row;
      try {
        row = JSON.parse(line);
      } catch {
        result.invalid += 1;
        continue;
      }
      if (!payTo || normalizeAddress(row && row.payTo) !== payTo) {
        result.invalid += 1;
        continue;
      }
      if (typeof row.usdc_atomic === "string" && /^\d+$/.test(row.usdc_atomic)
        && BigInt(row.usdc_atomic) > 0n && BigInt(row.usdc_atomic) % 10_000n !== 0n) {
        result.blocked_subcent += 1;
        continue;
      }
      const boundary = { ownedPayTos: [payTo], selfWallets };
      try {
        saleLedgerEntry(row, boundary);
      } catch {
        result.invalid += 1;
        continue;
      }
      let verified = false;
      try {
        verified = await verifySaleOnBase(row, boundary, rpcCall, chainContext);
      } catch {
        verified = false;
      }
      if (!verified) {
        result.chain_rejected += 1;
        continue;
      }
      let provenance = { kind: "sale" };
      if (row.source === "the402") {
        try {
          provenance = await classifyRevenue(row);
        } catch {
          provenance = { kind: "rejected", reason: "provenance_unavailable" };
        }
      }
      if (!provenance || !["sale", "work"].includes(provenance.kind)) {
        result.provenance_rejected += 1;
        continue;
      }
      const write = provenance.kind === "work"
        ? await recordWork(row, provenance, boundary)
        : await recordSale(row, boundary);
      result.transactions.push(normalizeTx(row.tx));
      if (write && write.duplicate) result.duplicates += 1;
      else {
        result.recorded += 1;
        if (provenance.kind === "work") result.work_recorded += 1;
        else result.sale_recorded += 1;
      }
    }
  }
  return result;
}

function the402EvidenceClassifier({
  fetchImpl,
  credentialsPath = join(homedir(), ".anicca", "the402-credentials.json"),
} = {}) {
  if (typeof fetchImpl !== "function") throw new TypeError("The402 evidence needs fetch");
  let evidencePromise = null;
  return async (row) => {
    if (!evidencePromise) {
      evidencePromise = (async () => {
        const credentials = JSON.parse(readFileSync(credentialsPath, "utf8"));
        if (typeof credentials.api_key !== "string" || credentials.api_key.length < 16) {
          throw new Error("invalid The402 credentials");
        }
        const get = async (path) => {
          const response = await fetchImpl(`https://api.the402.ai${path}`, {
            headers: { "X-API-Key": credentials.api_key },
            signal: AbortSignal.timeout(30_000),
          });
          const text = await response.text();
          if (!response.ok || Buffer.byteLength(text) > MAX_THE402_RESPONSE_BYTES) {
            throw new Error("The402 evidence request failed");
          }
          return JSON.parse(text);
        };
        const [earnings, jobs] = await Promise.all([
          get("/v1/provider/earnings"),
          get("/v1/jobs"),
        ]);
        return { earnings, jobs };
      })();
    }
    try {
      return classifyThe402Revenue(row, await evidencePromise);
    } catch {
      return { kind: "rejected", reason: "provenance_unavailable" };
    }
  };
}

function args(argv) {
  const ledgerPaths = [];
  let stateDir = null;
  let rpcUrl = null;
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === "--ledger" && argv[index + 1]) ledgerPaths.push(resolve(argv[++index]));
    else if (key === "--state-dir" && argv[index + 1]) stateDir = resolve(argv[++index]);
    else if (key === "--rpc" && argv[index + 1]) rpcUrl = argv[++index];
    else throw new Error(`unknown or incomplete argument ${key}`);
  }
  return { ledgerPaths, stateDir, rpcUrl };
}

function rpcClient(fetchImpl, rpcUrl) {
  return async (method, params) => {
    const response = await fetchImpl(rpcUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      signal: AbortSignal.timeout(30_000),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok || !body || body.error || body.result === undefined) {
      throw new Error("Base RPC request failed");
    }
    return body.result;
  };
}

async function main(deps = {}, argv = process.argv.slice(2)) {
  const parsed = args(argv);
  const stateDir = parsed.stateDir || deps.stateDir || process.env.X402_SELL_STATE_DIR
    || join(homedir(), "anicca", "skills", "earn", "x402-sell", "state");
  const ledgerPaths = parsed.ledgerPaths.length ? parsed.ledgerPaths : findLedgerPaths(stateDir);
  const selfWalletModule = deps.selfWalletModule || process.env.X402_SELF_WALLETS_MODULE
    || join(stateDir, "..", "lib", "self-wallets.mjs");
  const imported = deps.selfWallets
    ? { SELF_WALLETS: deps.selfWallets }
    : await import(pathToFileURL(resolve(selfWalletModule)).href);
  const fetchImpl = deps.fetchImpl || globalThis.fetch;
  const rpcCall = deps.rpcCall || rpcClient(fetchImpl, parsed.rpcUrl || process.env.BASE_RPC_URL || DEFAULT_RPC);
  const classifyRevenue = deps.classifyRevenue || the402EvidenceClassifier({
    fetchImpl,
    credentialsPath: deps.the402CredentialsPath || process.env.THE402_CREDENTIALS_PATH
      || join(homedir(), ".anicca", "the402-credentials.json"),
  });
  const subjectId = deps.subjectId || process.env.LM_CFO_SUBJECT_ID
    || process.env.LM_CFO_UID || process.env.LM_UID || "local";
  const baseFinancialStore = createJsonlFinancialRecordStore({
    directoryPath: deps.financialRecordsDir || process.env.LM_FINANCIAL_RECORDS_DIR
      || join(process.env.CFO_STATE_DIR || join(homedir(), ".local/state/life-manager/life-manager-cfo-hourly"), "financial-records"),
  });
  const financialStore = deps.financialStore || createLocalFinancialTransitionStore({
    store: baseFinancialStore, env: process.env,
  });
  const recordFinancial = createFinancialEarningsWriter({ store: financialStore, subjectId });
  const recordSale = deps.recordSale || ((input, boundary) => recordX402Sale(input, { ...boundary, recordEntry: recordFinancial }));
  const recordWork = deps.recordWork || ((input, provenance, boundary) => recordX402Work(input, provenance, { ...boundary, recordEntry: recordFinancial }));
  const result = await processLedgers({
    ledgerPaths,
    selfWallets: imported.SELF_WALLETS,
    rpcCall,
    recordSale,
    recordWork,
    classifyRevenue,
  });
  const output = { observed_at: new Date().toISOString(), ok: true, ...result };
  (deps.writeOutput || ((text) => process.stdout.write(text)))(`${JSON.stringify(output)}\n`);
  return output;
}

if (require.main === module) {
  main().catch(() => {
    process.stderr.write('{"ok":false,"error":"x402_sale_ledger_failed"}\n');
    process.exitCode = 1;
  });
}

module.exports = {
  USDC_ADDRESS,
  TRANSFER_TOPIC,
  DEFAULT_RPC,
  findLedgerPaths,
  processLedgers,
  verifySaleOnBase,
  the402EvidenceClassifier,
  main,
};
