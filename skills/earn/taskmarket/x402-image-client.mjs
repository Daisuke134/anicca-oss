import { privateKeyToAccount } from 'viem/accounts';
import { createHash } from 'node:crypto';
import {
  createPaymentPayload,
  extractPaymentDetails,
  parsePaymentRequired,
} from '@blockrun/llm';

export const BLOCKRUN_IMAGE_URL = 'https://blockrun.ai/api/v1/images/generations';
export const GPT_IMAGE_MODEL = 'openai/gpt-image-2';
export const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

const USER_AGENT = 'anicca-taskmarket-work/1.0';

function parseQuote(header) {
  if (!header) throw new Error('image response has no payment-required header');
  const required = parsePaymentRequired(header);
  const details = extractPaymentDetails(required);
  if (!details || !/^\d+$/.test(String(details.amount || ''))) {
    throw new Error('image quote amount is invalid');
  }
  return {
    required,
    details,
    amountUsd: Number(BigInt(details.amount)) / 1_000_000,
  };
}

async function defaultCreateSignature({ walletKey, details, required }) {
  const account = privateKeyToAccount(walletKey);
  return createPaymentPayload(
    walletKey,
    account.address,
    details.recipient,
    details.amount,
    details.network,
    {
      resourceUrl: details.resource?.url || BLOCKRUN_IMAGE_URL,
      resourceDescription: details.resource?.description || 'BlockRun GPT Image 2 generation',
      maxTimeoutSeconds: details.maxTimeoutSeconds || 600,
      extra: details.extra,
      extensions: required.extensions,
    },
  );
}

function paymentReceiptId(response) {
  const value = response?.headers?.get?.('payment-response')
    || response?.headers?.get?.('x-payment-response');
  return value ? `blockrun:${createHash('sha256').update(value).digest('hex')}` : null;
}

function imageBodyResult(body, costUsd, receiptId = null) {
  const url = body?.data?.[0]?.url;
  if (typeof url !== 'string' || !/^https:\/\//i.test(url) || body.data.length !== 1) {
    throw new Error('image response must contain one HTTPS image URL');
  }
  return {
    url,
    model: GPT_IMAGE_MODEL,
    costUsd,
    paymentReceiptId: receiptId,
    created: body.created ?? null,
  };
}

async function responseJson(response) {
  try {
    return await response.json();
  } catch {
    throw new Error('image response is not JSON');
  }
}

async function imageResult(response, costUsd) {
  return imageBodyResult(await responseJson(response), costUsd, paymentReceiptId(response));
}

function blockrunPollUrl(raw) {
  let url;
  try {
    url = new URL(String(raw || ''), 'https://blockrun.ai');
  } catch {
    throw new Error('image poll URL is invalid');
  }
  if (url.origin !== 'https://blockrun.ai' || !url.pathname.startsWith('/api/')) {
    throw new Error('image poll URL is not on blockrun.ai');
  }
  return url.href;
}

async function pollImageJob({
  pollUrl,
  signature,
  costUsd,
  receiptId,
  fetchImpl,
  sleepImpl,
  pollIntervalMs,
  maxPollDurationMs,
}) {
  const url = blockrunPollUrl(pollUrl);
  for (let elapsed = 0; elapsed < maxPollDurationMs; elapsed += pollIntervalMs) {
    await sleepImpl(pollIntervalMs);
    const response = await fetchImpl(url, {
      method: 'GET',
      headers: {
        'PAYMENT-SIGNATURE': signature,
        'X-PAYMENT': signature,
        'User-Agent': USER_AGENT,
      },
    });
    if (response.status === 202) continue;
    const body = await responseJson(response);
    if (!response.ok) throw new Error(`image poll returned HTTP ${response.status}`);
    if (body?.status === 'failed') {
      throw new Error(`image generation failed: ${String(body.error || 'unknown error')}`);
    }
    if (Array.isArray(body?.data)) return imageBodyResult(body, costUsd, paymentReceiptId(response) || receiptId);
    if (body?.status === 'queued' || body?.status === 'in_progress') continue;
    throw new Error('image poll returned no terminal result');
  }
  throw new Error(`image generation poll timed out after ${maxPollDurationMs}ms`);
}

export async function generateImage({
  prompt,
  walletKey,
  fetchImpl = fetch,
  createSignature = defaultCreateSignature,
  reserveSpend = async () => {},
  maxQuoteUsd = 0.07,
  sleepImpl = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  pollIntervalMs = 5_000,
  maxPollDurationMs = 240_000,
}) {
  if (typeof prompt !== 'string' || prompt.trim().length === 0 || prompt.length > 12_000) {
    throw new Error('image prompt must contain 1..12000 characters');
  }
  if (!/^0x[0-9a-fA-F]{64}$/.test(String(walletKey || ''))) {
    throw new Error('agent image wallet key is missing or invalid');
  }
  const body = JSON.stringify({
    model: GPT_IMAGE_MODEL,
    prompt,
    size: '1024x1024',
    n: 1,
  });
  const request = {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'User-Agent': USER_AGENT,
    },
    body,
  };
  const challenge = await fetchImpl(BLOCKRUN_IMAGE_URL, request);
  if (challenge.status === 200) return imageResult(challenge, 0);
  if (challenge.status !== 402) {
    throw new Error(`image challenge returned HTTP ${challenge.status}`);
  }

  const header = challenge.headers.get('payment-required')
    || challenge.headers.get('x-payment-required');
  const quote = parseQuote(header);
  if (quote.details.network !== 'eip155:8453') {
    throw new Error('image quote is not for Base mainnet');
  }
  if (String(quote.details.asset || '').toLowerCase() !== USDC_BASE.toLowerCase()) {
    throw new Error('image quote is not for Base USDC');
  }
  if (!Number.isFinite(quote.amountUsd) || quote.amountUsd <= 0
    || quote.amountUsd > Number(maxQuoteUsd) + Number.EPSILON) {
    throw new Error(`image quote exceeds ${maxQuoteUsd} USDC cap`);
  }

  const signature = await createSignature({
    walletKey,
    details: quote.details,
    required: quote.required,
    amountUsd: quote.amountUsd,
  });
  await reserveSpend(quote.amountUsd);
  const paid = await fetchImpl(BLOCKRUN_IMAGE_URL, {
    ...request,
    headers: {
      ...request.headers,
      'PAYMENT-SIGNATURE': signature,
    },
  });
  if (paid.status === 202) {
    const accepted = await responseJson(paid);
    return pollImageJob({
      pollUrl: accepted?.poll_url,
      signature,
      costUsd: quote.amountUsd,
      receiptId: paymentReceiptId(paid),
      fetchImpl,
      sleepImpl,
      pollIntervalMs,
      maxPollDurationMs,
    });
  }
  if (paid.status !== 200) {
    throw new Error(`paid image generation returned HTTP ${paid.status}`);
  }
  return imageResult(paid, quote.amountUsd);
}
