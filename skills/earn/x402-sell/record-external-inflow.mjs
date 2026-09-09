#!/usr/bin/env node
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { SELF_WALLETS } from './lib/self-wallets.mjs';
import {
  appendUniqueExternalInflows,
  collectVerifiedExternalInflows,
  walletLedgerPath,
} from './lib/external-inflow-recorder.mjs';
import { resolveX402StateDir } from './state-paths.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
// Match verify-inflow.mjs: the untagged legacy watcher is the pinned founder wallet.
const payTo = String(process.env.X402_PAYTO || '0x810f6d61f7606deee2657d3083e150a222bc29c5').toLowerCase();
const hoursBack = Number(process.argv[2] || 2);
const stateDir = resolveX402StateDir();
const rpcUrl = process.env.X402_RPC_URL || 'https://mainnet.base.org';

if (!/^0x[0-9a-f]{40}$/.test(payTo)) throw new Error('set X402_PAYTO to the seller wallet');
if (!Number.isFinite(hoursBack) || hoursBack <= 0) throw new Error('hoursBack must be positive');

async function rpcCall(method, params) {
  const response = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    signal: AbortSignal.timeout(30_000),
  });
  const body = await response.json();
  if (!response.ok || body.error || body.result === undefined) {
    throw new Error(`${method}: ${JSON.stringify(body.error || body)}`);
  }
  return body.result;
}

function settledTransactionsFromSales(path) {
  if (!existsSync(path)) return new Set();
  return new Set(readFileSync(path, 'utf8').split('\n').filter(Boolean).flatMap((line) => {
    try {
      const row = JSON.parse(line);
      return row?.settled === true && typeof row.tx === 'string' ? [row.tx] : [];
    } catch { return []; }
  }));
}

const salesLog = process.env.X402_SALES_LOG || join(stateDir, `sales-${payTo}.jsonl`);
const settledTransactions = settledTransactionsFromSales(salesLog);
const latestHex = await rpcCall('eth_blockNumber', []);
const latest = Number(BigInt(latestHex));
const fromBlock = Math.max(0, latest - Math.ceil(hoursBack * 1_800));
const verified = await collectVerifiedExternalInflows({
  payTo,
  fromBlock,
  rpcCall,
  selfWallets: SELF_WALLETS,
  settledTransactions,
});
const ledger = walletLedgerPath(payTo, { stateDir });
const write = appendUniqueExternalInflows(ledger, verified.rows);

console.log(JSON.stringify({
  payTo,
  hoursBack,
  finalizedBlock: verified.finalizedBlock,
  settledTelemetry: settledTransactions.size,
  verified: verified.rows.length,
  ...write,
  ledger,
}));
