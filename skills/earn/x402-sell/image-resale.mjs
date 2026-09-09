import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
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
import { X402_STATE_DIR } from './state-paths.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_STATE_DIR = X402_STATE_DIR;
const BLOCKRUN_IMAGE_URL = 'https://blockrun.ai/api/v1/images/generations';
const USER_AGENT = 'anicca-x402-image-resale/1.0';
const walletLocks = new Map();

export const IMAGE_OFFER = Object.freeze({
  path: '/image',
  price: '$0.03',
  upstreamMaxUsd: 0.018,
  grossMarginUsd: 0.012,
  model: 'zai/cogview-4',
  size: '1024x1024',
  what: 'AI image generation',
  description: 'Generate one 1024x1024 image from a text prompt. Fixed-price USDC payment on Base.',
});

export function assertProfitableImageOffer(offer) {
  const priceUsd = Number(String(offer?.price ?? '').replace(/^\$/, ''));
  const upstreamMaxUsd = Number(offer?.upstreamMaxUsd);
  if (!Number.isFinite(priceUsd) || !Number.isFinite(upstreamMaxUsd)
    || priceUsd <= upstreamMaxUsd) {
    throw new Error('image offer price must exceed upstream maximum');
  }
  return offer;
}

assertProfitableImageOffer(IMAGE_OFFER);

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
  if (!details?.amount || !/^\d+$/.test(String(details.amount))) {
    throw new Error('invalid BlockRun image quote amount');
  }
  const amountUsd = Number(BigInt(details.amount)) / 1e6;
  if (!Number.isFinite(amountUsd)) throw new Error('invalid BlockRun image quote amount');
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
      resourceUrl: details.resource?.url || BLOCKRUN_IMAGE_URL,
      resourceDescription: details.resource?.description || 'BlockRun image resale upstream',
      maxTimeoutSeconds: details.maxTimeoutSeconds || 120,
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

async function parseImageResponse(response) {
  const body = await response.json();
  const url = body?.data?.[0]?.url;
  if (typeof url !== 'string' || !/^https:\/\//i.test(url)) {
    throw new Error('invalid BlockRun image response');
  }
  return { url, created: body.created ?? null, model: IMAGE_OFFER.model };
}

export function makeImageResaleHandler(deps = {}) {
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
    dailyCapUsd = Number(process.env.IMAGE_RESALE_DAILY_CAP_USD || 0.32),
    stateDir = DEFAULT_STATE_DIR,
    offer = IMAGE_OFFER,
  } = deps;
  assertProfitableImageOffer(offer);

  return async function imageResaleHandler(req, res) {
    const prompt = req.body?.prompt;
    if (typeof prompt !== 'string' || prompt.length < 1 || prompt.length > 2000) {
      return res.status(400).json({ error: 'pass JSON {"prompt":"1..2000 characters"}' });
    }
    const walletKey = loadKey();
    if (!walletKey) return res.status(503).json({ error: 'image resale paused: no agent wallet' });
    if (floatGuardTripped(await balance(walletKey), minFloatUsd)) {
      return res.status(503).json({ error: 'image resale paused: low float' });
    }

    const body = {
      model: offer.model,
      prompt,
      size: offer.size,
      n: 1,
    };
    const request = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'User-Agent': USER_AGENT },
      body: JSON.stringify(body),
    };
    let challengeResponse;
    try {
      challengeResponse = await bareFetch(BLOCKRUN_IMAGE_URL, request);
    } catch (error) {
      return res.status(502).json({ error: 'image upstream failed', detail: String(error?.message || error).slice(0, 200) });
    }
    if (challengeResponse.status === 200) {
      try { return res.json(await parseImageResponse(challengeResponse)); }
      catch (error) { return res.status(502).json({ error: 'image upstream failed', detail: String(error.message).slice(0, 200) }); }
    }
    if (challengeResponse.status !== 402) {
      return res.status(502).json({ error: 'image upstream failed', detail: `upstream status ${challengeResponse.status}` });
    }

    let quote;
    try {
      quote = quoteFromHeader(challengeResponse.headers?.get?.('payment-required'));
    } catch (error) {
      return res.status(503).json({ error: 'image upstream quote invalid', detail: String(error.message).slice(0, 200) });
    }
    if (quote.amountUsd > offer.upstreamMaxUsd + Number.EPSILON) {
      return res.status(503).json({ error: 'image upstream quote above guard' });
    }

    const wallet = privateKeyToAccount(walletKey).address.toLowerCase();
    return withWalletLock(wallet, async () => {
      const statePath = join(stateDir, `image-resale-spend-${wallet}.json`);
      const spend = rolloverSpendState(read(statePath), now().toISOString().slice(0, 10));
      if (dailyCapTripped(spend, dailyCapUsd)
        || spend.spentUsd + quote.amountUsd > dailyCapUsd + Number.EPSILON) {
        return res.status(503).json({ error: 'image resale paused: daily upstream cap reached' });
      }
      write(statePath, recordSpend(spend, quote.amountUsd));

      let signature;
      try { signature = await sign({ walletKey, details: quote.details, required: quote.required }); }
      catch (error) { return res.status(502).json({ error: 'image payment signing failed', detail: String(error.message).slice(0, 200) }); }

      let paidResponse;
      try {
        paidResponse = await paidFetch(BLOCKRUN_IMAGE_URL, {
          ...request,
          headers: { ...request.headers, 'PAYMENT-SIGNATURE': signature },
        });
      } catch (error) {
        return res.status(502).json({ error: 'image upstream failed after reservation', detail: String(error?.message || error).slice(0, 200) });
      }
      if (paidResponse.status !== 200) {
        return res.status(502).json({ error: 'image upstream failed after reservation', detail: `upstream status ${paidResponse.status}` });
      }
      try { return res.json({ prompt, ...await parseImageResponse(paidResponse), source: 'blockrun' }); }
      catch (error) { return res.status(502).json({ error: 'image upstream failed after reservation', detail: String(error.message).slice(0, 200) }); }
    });
  };
}

export const imageResaleHandler = makeImageResaleHandler();
