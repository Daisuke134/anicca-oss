import {
  appendFileSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  unlinkSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { X402_STATE_DIR } from '../state-paths.mjs';

export const USDC_ADDRESS = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
export const TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';

const DEFAULT_STATE_DIR = X402_STATE_DIR;

function normalizeAddress(value) {
  const normalized = String(value || '').toLowerCase();
  return /^0x[0-9a-f]{40}$/.test(normalized) ? normalized : null;
}

function normalizeTx(value) {
  const normalized = String(value || '').toLowerCase();
  return /^0x[0-9a-f]{64}$/.test(normalized) ? normalized : null;
}

function topicAddress(address) {
  return `0x${address.slice(2).padStart(64, '0')}`;
}

function blockNumber(value) {
  try {
    const parsed = Number(BigInt(value));
    return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
  } catch { return null; }
}

function receiptTransfer(receipt, { tx, payTo, finalizedBlock, selfSet }) {
  if (!receipt || receipt.status !== '0x1') return null;
  const receiptTx = normalizeTx(receipt.transactionHash);
  const receiptBlock = blockNumber(receipt.blockNumber);
  if (receiptTx !== tx || receiptBlock === null || receiptBlock > finalizedBlock) return null;

  const matches = (Array.isArray(receipt.logs) ? receipt.logs : []).filter((log) => {
    if (String(log?.address || '').toLowerCase() !== USDC_ADDRESS.toLowerCase()) return false;
    if (!Array.isArray(log.topics) || log.topics.length < 3) return false;
    if (String(log.topics[0] || '').toLowerCase() !== TRANSFER_TOPIC) return false;
    if (String(log.topics[2] || '').toLowerCase() !== topicAddress(payTo)) return false;
    const logTx = log.transactionHash == null ? tx : normalizeTx(log.transactionHash);
    return logTx === tx;
  });
  if (matches.length !== 1) return null;

  const log = matches[0];
  const fromTopic = String(log.topics[1] || '').toLowerCase();
  if (!/^0x[0-9a-f]{64}$/.test(fromTopic) || !/^0{24}[0-9a-f]{40}$/.test(fromTopic.slice(2))) return null;
  const from = normalizeAddress(`0x${fromTopic.slice(-40)}`);
  if (!from || selfSet.has(from)) return null;
  if (!/^0x[0-9a-f]+$/i.test(String(log.data || ''))) return null;
  let atomic;
  try { atomic = BigInt(log.data); } catch { return null; }
  if (atomic <= 0n) return null;

  return {
    tx,
    block: receiptBlock,
    from,
    to: payTo,
    payTo,
    usdc: Number(atomic) / 1_000_000,
    atomic: atomic.toString(),
    finalized: true,
    status: 'success',
    external: true,
  };
}

export async function collectVerifiedExternalInflows({
  payTo,
  fromBlock,
  rpcCall,
  selfWallets = [],
  settledTransactions = new Set(),
}) {
  const receiver = normalizeAddress(payTo);
  if (!receiver) throw new TypeError('payTo must be a valid EVM address');
  if (typeof rpcCall !== 'function') throw new TypeError('rpcCall must be a function');
  const settledSet = new Set([...settledTransactions].map(normalizeTx).filter(Boolean));

  const finalized = await rpcCall('eth_getBlockByNumber', ['finalized', false]);
  const finalizedBlock = blockNumber(finalized?.number);
  if (finalizedBlock === null) throw new Error('RPC returned no finalized block');
  const start = Number.isSafeInteger(Number(fromBlock)) && Number(fromBlock) >= 0
    ? Number(fromBlock)
    : 0;
  if (start > finalizedBlock || settledSet.size === 0) return { finalizedBlock, rows: [] };

  const candidates = await rpcCall('eth_getLogs', [{
    address: USDC_ADDRESS,
    fromBlock: `0x${start.toString(16)}`,
    toBlock: `0x${finalizedBlock.toString(16)}`,
    topics: [TRANSFER_TOPIC, null, topicAddress(receiver)],
  }]);
  if (!Array.isArray(candidates)) throw new Error('eth_getLogs returned a non-array');
  const txs = [...new Set(candidates.map((log) => normalizeTx(log?.transactionHash)).filter((tx) => tx && settledSet.has(tx)))];
  const selfSet = new Set([receiver, ...selfWallets.map(normalizeAddress).filter(Boolean)]);
  const rows = [];

  for (const tx of txs) {
    const receipt = await rpcCall('eth_getTransactionReceipt', [tx]);
    const transaction = await rpcCall('eth_getTransactionByHash', [tx]);
    const initiator = normalizeAddress(transaction?.from);
    if (!initiator || selfSet.has(initiator)) continue;
    const row = receiptTransfer(receipt, { tx, payTo: receiver, finalizedBlock, selfSet });
    if (row) {
      const { atomic: _atomic, ...legacyRow } = row;
      rows.push(legacyRow);
    }
  }
  return { finalizedBlock, rows };
}

function normalizeSaleCandidate(row, allowedPayToSet) {
  const source = ['x402-image', 'x402-railway', 'the402', 'clawmerchants'].includes(row?.source) ? row.source : null;
  const sourceSaleId = typeof row?.source_sale_id === 'string'
    && /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,191}$/.test(row.source_sale_id)
    ? row.source_sale_id
    : null;
  const offerId = typeof row?.offer_id === 'string'
    && /^[a-zA-Z0-9/][a-zA-Z0-9._:/-]{0,191}$/.test(row.offer_id)
    ? row.offer_id
    : null;
  const tx = normalizeTx(row?.tx);
  const payTo = normalizeAddress(row?.expected_pay_to);
  const atomic = typeof row?.expected_usdc_atomic === 'string' && /^\d+$/.test(row.expected_usdc_atomic)
    ? row.expected_usdc_atomic.replace(/^0+(?=\d)/, '')
    : null;
  const observed = typeof row?.observed_at === 'string' ? new Date(row.observed_at) : null;
  const observedAt = observed && !Number.isNaN(observed.getTime()) ? observed.toISOString() : null;
  if (!source || !sourceSaleId || !offerId || !tx || !payTo || !atomic || BigInt(atomic) <= 0n || !observedAt) return null;
  if (!allowedPayToSet.has(payTo)) return null;
  return { source, sourceSaleId, offerId, tx, payTo, atomic, observedAt };
}

export async function collectVerifiedSaleCandidates({
  candidates,
  rpcCall,
  selfWallets = [],
  allowedPayTos = [],
}) {
  if (typeof rpcCall !== 'function') throw new TypeError('rpcCall must be a function');
  const allowedPayToSet = new Set(allowedPayTos.map(normalizeAddress).filter(Boolean));
  if (allowedPayToSet.size === 0) throw new TypeError('allowedPayTos must contain an owned wallet');

  const chainHex = await rpcCall('eth_chainId', []);
  const chainId = blockNumber(chainHex);
  if (chainId !== 8453) throw new Error('RPC is not Base mainnet');
  const finalized = await rpcCall('eth_getBlockByNumber', ['finalized', false]);
  const finalizedBlock = blockNumber(finalized?.number);
  if (finalizedBlock === null) throw new Error('RPC returned no finalized block');

  const selfSet = new Set(selfWallets.map(normalizeAddress).filter(Boolean));
  const seenTxs = new Set();
  const rows = [];
  for (const input of Array.isArray(candidates) ? candidates : []) {
    const candidate = normalizeSaleCandidate(input, allowedPayToSet);
    if (!candidate || seenTxs.has(candidate.tx)) continue;
    seenTxs.add(candidate.tx);
    const receipt = await rpcCall('eth_getTransactionReceipt', [candidate.tx]);
    const transaction = await rpcCall('eth_getTransactionByHash', [candidate.tx]);
    const initiator = normalizeAddress(transaction?.from);
    if (!initiator || selfSet.has(initiator)) continue;
    const transfer = receiptTransfer(receipt, {
      tx: candidate.tx,
      payTo: candidate.payTo,
      finalizedBlock,
      selfSet: new Set([candidate.payTo, ...selfSet]),
    });
    if (!transfer || transfer.atomic !== candidate.atomic) continue;
    const { atomic, ...verified } = transfer;
    rows.push({
      source: candidate.source,
      source_sale_id: candidate.sourceSaleId,
      offer_id: candidate.offerId,
      ...verified,
      usdc_atomic: atomic,
      observed_at: candidate.observedAt,
    });
  }
  return { chainId, finalizedBlock, rows };
}

export function walletLedgerPath(payTo, { stateDir = DEFAULT_STATE_DIR } = {}) {
  const receiver = normalizeAddress(payTo);
  if (!receiver) throw new TypeError('payTo must be a valid EVM address');
  return join(stateDir, `external-inflows-${receiver}.jsonl`);
}

function readExistingKeys(ledgerPath) {
  const txs = new Set();
  const sourceSaleIds = new Set();
  if (!existsSync(ledgerPath)) return { txs, sourceSaleIds };
  for (const line of readFileSync(ledgerPath, 'utf8').split('\n').filter(Boolean)) {
    try {
      const row = JSON.parse(line);
      const tx = normalizeTx(row?.tx);
      if (tx) txs.add(tx);
      if (typeof row?.source_sale_id === 'string') sourceSaleIds.add(row.source_sale_id);
    } catch { /* malformed historical rows provide no dedupe evidence */ }
  }
  return { txs, sourceSaleIds };
}

export function appendUniqueExternalInflows(ledgerPath, rows) {
  mkdirSync(dirname(ledgerPath), { recursive: true });
  const lockPath = `${ledgerPath}.lock`;
  let lock;
  try {
    lock = openSync(lockPath, 'wx');
    const { txs, sourceSaleIds } = readExistingKeys(ledgerPath);
    const fresh = [];
    let duplicates = 0;
    for (const row of rows || []) {
      const tx = normalizeTx(row?.tx);
      const sourceSaleId = typeof row?.source_sale_id === 'string' ? row.source_sale_id : null;
      if (!tx || txs.has(tx) || (sourceSaleId && sourceSaleIds.has(sourceSaleId))) {
        duplicates += 1;
        continue;
      }
      txs.add(tx);
      if (sourceSaleId) sourceSaleIds.add(sourceSaleId);
      fresh.push({ ...row, tx });
    }
    if (fresh.length) appendFileSync(ledgerPath, `${fresh.map((row) => JSON.stringify(row)).join('\n')}\n`);
    return { recorded: fresh.length, duplicates };
  } finally {
    if (lock !== undefined) {
      closeSync(lock);
      try { unlinkSync(lockPath); } catch { /* already removed */ }
    }
  }
}
