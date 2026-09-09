import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { privateKeyToAccount } from 'viem/accounts';
import {
  createPaymentPayload,
  extractPaymentDetails,
  parsePaymentRequired,
} from '@blockrun/llm';

import { loadEvmKey } from '../lib/resolve-identity.mjs';
import { evmErc20Balance, EVM_TOKENS, RPC } from '../lib/net-worth.mjs';
import {
  dailyCapTripped,
  floatGuardTripped,
  recordSpend,
  rolloverSpendState,
} from './lib/resale-guards.mjs';
import { LLM_OFFER_VARIANTS, assertProfitableOffer } from './llm-offers.mjs';
import { activeVariant } from './store-experiment.mjs';
import { X402_STATE_DIR } from './state-paths.mjs';

export { LLM_OFFER_VARIANTS, assertProfitableOffer } from './llm-offers.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_STATE_DIR = X402_STATE_DIR;
const BLOCKRUN_CHAT_URL = 'https://blockrun.ai/api/v1/chat/completions';
const BLOCKRUN_MODEL = 'zai/glm-5-turbo';
const USER_AGENT = 'anicca-x402-llm-resale/1.0';
const walletLocks = new Map();

for (const offer of LLM_OFFER_VARIANTS) assertProfitableOffer(offer);

export function llmProduct(env = process.env) {
  const key = loadEvmKey({ env });
  const payTo = env.X402_PAYTO || (key ? privateKeyToAccount(key).address : '');
  const offer = payTo ? activeVariant(payTo, LLM_OFFER_VARIANTS) : LLM_OFFER_VARIANTS[0];
  return {
    path: '/llm',
    price: offer.price,
    upstreamMaxUsd: offer.upstreamMaxUsd,
    what: 'GLM-5 Turbo LLM inference',
    example: '/llm?prompt=<text>&maxTokens=512',
    description: offer.description,
  };
}

function readState(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')); }
  catch { return null; }
}

function writeState(path, state) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(state));
}

async function getBalanceUsd(walletKey) {
  const address = privateKeyToAccount(walletKey).address;
  return evmErc20Balance('base', EVM_TOKENS.base[0], address, fetch, RPC.base);
}

function quoteFromHeader(header) {
  if (!header) throw new Error('402 response missing payment-required header');
  const required = parsePaymentRequired(header);
  const details = extractPaymentDetails(required);
  if (!details?.amount || !/^\d+$/.test(String(details.amount))) throw new Error('invalid BlockRun quote amount');
  const amountUsd = Number(BigInt(details.amount)) / 1e6;
  if (!Number.isFinite(amountUsd)) throw new Error('invalid BlockRun quote amount');
  return { required, details, amountUsd };
}

async function signPayment({ walletKey, details, required }) {
  const account = privateKeyToAccount(walletKey);
  return createPaymentPayload(
    walletKey,
    account.address,
    details.recipient,
    details.amount,
    details.network || 'eip155:8453',
    {
      resourceUrl: details.resource?.url || BLOCKRUN_CHAT_URL,
      resourceDescription: details.resource?.description || 'BlockRun LLM resale upstream',
      maxTimeoutSeconds: details.maxTimeoutSeconds || 60,
      extra: details.extra,
      extensions: required.extensions,
    },
  );
}

async function withWalletLock(wallet, fn) {
  const previous = walletLocks.get(wallet) || Promise.resolve();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const queued = previous.then(() => gate);
  walletLocks.set(wallet, queued);
  await previous;
  try { return await fn(); }
  finally {
    release();
    if (walletLocks.get(wallet) === queued) walletLocks.delete(wallet);
  }
}

function parseChatResponse(json) {
  const response = json?.choices?.[0]?.message?.content;
  if (typeof response !== 'string') throw new Error('invalid BlockRun chat response');
  return { response, model: json.model || BLOCKRUN_MODEL };
}

export function makeLlmResaleHandler(deps = {}) {
  const {
    loadKey = () => loadEvmKey(),
    getBalanceUsd: balance = getBalanceUsd,
    readState: read = readState,
    writeState: write = writeState,
    bareFetch = fetch,
    paidFetch = fetch,
    signPayment: sign = signPayment,
    now = () => new Date(),
    minFloatUsd = 0.5,
    dailyCapUsd = Number(process.env.LLM_RESALE_DAILY_CAP_USD || 0.25),
    stateDir = DEFAULT_STATE_DIR,
    offer = llmProduct(),
  } = deps;
  assertProfitableOffer(offer);

  return async function llmResaleHandler(req, res) {
    const rawPrompt = req.query?.prompt;
    if (typeof rawPrompt !== 'string' || rawPrompt.length < 1 || rawPrompt.length > 2000) {
      return res.status(400).json({ error: 'pass ?prompt=<1..2000 characters>' });
    }
    const maxTokens = Math.max(1, Math.min(512, Number.parseInt(req.query?.maxTokens, 10) || 512));
    const walletKey = loadKey();
    if (!walletKey) return res.status(503).json({ error: 'LLM resale paused: no agent wallet' });

    const ownBalance = await balance(walletKey);
    if (floatGuardTripped(ownBalance, minFloatUsd)) {
      return res.status(503).json({ error: 'LLM resale paused: low float' });
    }

    const wallet = privateKeyToAccount(walletKey).address.toLowerCase();
    const statePath = join(stateDir, `llm-resale-spend-${wallet}.json`);
    const body = {
      model: BLOCKRUN_MODEL,
      messages: [{ role: 'user', content: rawPrompt }],
      max_tokens: maxTokens,
      temperature: 0.7,
    };
    const request = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'User-Agent': USER_AGENT },
      body: JSON.stringify(body),
    };
    let challengeResponse;
    try {
      challengeResponse = await bareFetch(BLOCKRUN_CHAT_URL, request);
    } catch (error) {
      return res.status(502).json({ error: 'LLM upstream failed', detail: String(error?.message || error).slice(0, 200) });
    }
    if (challengeResponse.status === 200) {
      try {
        const result = parseChatResponse(await challengeResponse.json());
        return res.json({ prompt: rawPrompt, ...result, source: 'blockrun' });
      } catch (error) {
        return res.status(502).json({ error: 'LLM upstream failed', detail: String(error?.message || error).slice(0, 200) });
      }
    }
    if (challengeResponse.status !== 402) {
      return res.status(502).json({ error: 'LLM upstream failed', detail: `upstream status ${challengeResponse.status}` });
    }

    let quote;
    try {
      quote = quoteFromHeader(challengeResponse.headers?.get?.('payment-required'));
    } catch (error) {
      return res.status(503).json({ error: 'LLM upstream quote invalid', detail: String(error?.message || error).slice(0, 200) });
    }
    if (quote.amountUsd > offer.upstreamMaxUsd + Number.EPSILON) {
      return res.status(503).json({ error: 'LLM upstream quote above guard' });
    }

    return withWalletLock(wallet, async () => {
      const spend = rolloverSpendState(read(statePath), now().toISOString().slice(0, 10));
      if (dailyCapTripped(spend, dailyCapUsd)
        || spend.spentUsd + quote.amountUsd > dailyCapUsd + Number.EPSILON) {
        return res.status(503).json({ error: 'LLM resale paused: daily upstream cap reached' });
      }

      // Reserve before signing. A failure after this point may already have settled upstream, so
      // the conservative reservation is intentionally never rolled back.
      write(statePath, recordSpend(spend, quote.amountUsd));
      let signature;
      try {
        signature = await sign({ walletKey, details: quote.details, required: quote.required });
      } catch (error) {
        return res.status(502).json({ error: 'LLM payment signing failed', detail: String(error?.message || error).slice(0, 200) });
      }

      let paidResponse;
      try {
        paidResponse = await paidFetch(BLOCKRUN_CHAT_URL, {
          ...request,
          headers: { ...request.headers, 'PAYMENT-SIGNATURE': signature },
        });
      } catch (error) {
        return res.status(502).json({ error: 'LLM upstream failed after reservation', detail: String(error?.message || error).slice(0, 200) });
      }
      if (paidResponse.status !== 200) {
        return res.status(502).json({ error: 'LLM upstream failed after reservation', detail: `upstream status ${paidResponse.status}` });
      }
      try {
        const result = parseChatResponse(await paidResponse.json());
        return res.json({ prompt: rawPrompt, ...result, source: 'blockrun' });
      } catch (error) {
        return res.status(502).json({ error: 'LLM upstream failed after reservation', detail: String(error?.message || error).slice(0, 200) });
      }
    });
  };
}

export const llmResaleHandler = makeLlmResaleHandler();
