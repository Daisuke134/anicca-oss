#!/usr/bin/env node
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolveX402StateDir } from './state-paths.mjs';

const DISCOVERY_URL = 'https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=1000';
const MAX_RESOURCES = 30_000;
const USDC_DECIMALS = 1_000_000n;

export function inferCategory(input) {
  const value = (input && typeof input === 'object'
    ? [input.resource, input.serviceName, ...(Array.isArray(input.tags) ? input.tags : [])].join(' ')
    : String(input ?? '')).toLowerCase();
  if (value.includes('search')) return 'search';
  if (/(swap|defi|dex|yield|apy|lend|liquidity|vault|protocol|pool)/.test(value)) return 'defi';
  if (/(funding|price|market)/.test(value)) return 'data';
  if (/(gpt|llm|chat)/.test(value)) return 'llm';
  if (value.includes('image')) return 'image';
  if (/(audio|speech|tts)/.test(value)) return 'audio';
  if (/(swap|defi|dex)/.test(value)) return 'defi';
  if (/(calc|compound|mortgage|hash)/.test(value)) return 'calc';
  return 'other';
}

function atomicUsdcToUsd(value) {
  let atomic;
  try {
    if (typeof value === 'bigint') {
      atomic = value;
    } else if (typeof value === 'number' && Number.isSafeInteger(value)) {
      atomic = BigInt(value);
    } else if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
      atomic = BigInt(value.trim());
    } else {
      return null;
    }
  } catch {
    return null;
  }

  if (atomic < 0n) return null;
  const usd = Number(atomic / USDC_DECIMALS) + Number(atomic % USDC_DECIMALS) / 1_000_000;
  return Number.isFinite(usd) ? usd : null;
}

function listingFromResource(item) {
  if (!item || typeof item !== 'object') return null;
  const resource = typeof item.resource === 'string'
    ? item.resource
    : typeof item.resource?.url === 'string'
      ? item.resource.url
      : typeof item.url === 'string'
        ? item.url
        : '';
  if (!resource) return null;

  const prices = [];
  for (const accept of Array.isArray(item.accepts) ? item.accepts : []) {
    if (!accept || typeof accept !== 'object') continue;
    for (const candidate of [accept.maxAmountRequired, accept.amount]) {
      const priceUsd = atomicUsdcToUsd(candidate);
      if (priceUsd !== null) {
        prices.push(priceUsd);
        break;
      }
    }
  }
  if (prices.length === 0) return null;

  return {
    resource,
    priceUsd: Math.min(...prices),
    category: inferCategory(item),
    calls30d: Math.max(0, Number(item.quality?.l30DaysTotalCalls) || 0),
    payerSignals30d: Math.max(0, Number(item.quality?.l30DaysUniquePayers) || 0),
  };
}

function compareText(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

function roundUsd(value) {
  return value === null ? null : Math.round((value + Number.EPSILON) * 1_000_000) / 1_000_000;
}

function percentile(sorted, quantile) {
  if (sorted.length === 0) return null;
  const index = (sorted.length - 1) * quantile;
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (index - lower);
}

export function aggregateMarket(resources, now) {
  if (!Array.isArray(resources)) throw new TypeError('resources must be an array');
  const timestamp = Number(now);
  if (!Number.isFinite(timestamp)) throw new TypeError('now must be a finite Unix timestamp');

  const listings = resources.map(listingFromResource).filter(Boolean);
  const prices = listings.map((item) => item.priceUsd).sort((a, b) => a - b);
  const categories = new Map();
  for (const listing of listings) {
    const aggregate = categories.get(listing.category) ?? { prices: [], calls30d: 0, payerSignals30d: 0 };
    aggregate.prices.push(listing.priceUsd);
    aggregate.calls30d += listing.calls30d;
    aggregate.payerSignals30d += listing.payerSignals30d;
    categories.set(listing.category, aggregate);
  }

  const byCategory = [...categories.entries()]
    .map(([category, aggregate]) => {
      aggregate.prices.sort((a, b) => a - b);
      return {
        category,
        count: aggregate.prices.length,
        medianPriceUsd: roundUsd(percentile(aggregate.prices, 0.5)),
        calls30d: aggregate.calls30d,
        payerSignals30d: aggregate.payerSignals30d,
      };
    })
    .sort((a, b) => b.count - a.count || compareText(a.category, b.category));

  const topPricedSamples = [...listings]
    .sort((a, b) => b.priceUsd - a.priceUsd || compareText(a.resource, b.resource))
    .slice(0, 15)
    .map(({ resource, priceUsd }) => ({ resource, priceUsd: roundUsd(priceUsd) }));
  const topDemandSamples = [...listings]
    .filter((item) => item.calls30d > 0 && item.payerSignals30d > 0)
    .sort((a, b) => (b.calls30d * b.priceUsd) - (a.calls30d * a.priceUsd)
      || b.payerSignals30d - a.payerSignals30d
      || compareText(a.resource, b.resource))
    .slice(0, 50)
    .map(({ resource, category, priceUsd, calls30d, payerSignals30d }) => ({
      resource,
      category,
      priceUsd: roundUsd(priceUsd),
      calls30d,
      payerSignals30d,
      estimatedGrossUsd30d: roundUsd(calls30d * priceUsd),
    }));

  const paidCalls30d = byCategory.reduce((sum, item) => sum + item.calls30d, 0);
  const payerSignals30d = byCategory.reduce((sum, item) => sum + item.payerSignals30d, 0);
  const categoriesWithPaidDemand = byCategory
    .filter((item) => item.calls30d > 0 && item.payerSignals30d > 0)
    .length;
  const demandPassed = categoriesWithPaidDemand > 0;

  return {
    ts: Math.floor(timestamp),
    source: 'cdp-bazaar',
    sampled: listings.length,
    priceDistribution: {
      p25: roundUsd(percentile(prices, 0.25)),
      median: roundUsd(percentile(prices, 0.5)),
      p75: roundUsd(percentile(prices, 0.75)),
      p90: roundUsd(percentile(prices, 0.9)),
    },
    byCategory,
    topPricedSamples,
    topDemandSamples,
    demandGate: {
      passed: demandPassed,
      paidCalls30d,
      payerSignals30d,
      categoriesWithPaidDemand,
      reason: demandPassed
        ? 'observed paid calls and payer signals in the last 30 days'
        : 'no category has both paid calls and payer signals in the last 30 days',
    },
  };
}

function responseItems(body) {
  if (Array.isArray(body)) return body;
  if (!body || typeof body !== 'object') return [];
  if (Array.isArray(body.items)) return body.items;
  if (Array.isArray(body.resources)) return body.resources;
  if (Array.isArray(body.data?.items)) return body.data.items;
  if (Array.isArray(body.data?.resources)) return body.data.resources;
  return [];
}

function nextPageUrl(body, currentUrl, pageSize) {
  if (!body || typeof body !== 'object') return null;
  const pagination = body.pagination && typeof body.pagination === 'object' ? body.pagination : {};
  const directNext = body.nextUrl ?? pagination.nextUrl ?? body.next ?? pagination.next;
  if (typeof directNext === 'string' && directNext) {
    if (/^https?:\/\//i.test(directNext) || directNext.startsWith('/')) {
      return new URL(directNext, currentUrl).href;
    }
    const next = new URL(currentUrl);
    next.searchParams.set('cursor', directNext);
    next.searchParams.set('limit', new URL(currentUrl).searchParams.get('limit') || '1000');
    return next.href;
  }

  const cursor = body.nextCursor ?? pagination.nextCursor ?? body.cursor?.next ?? pagination.cursor?.next;
  if (typeof cursor === 'string' && cursor) {
    const next = new URL(currentUrl);
    next.searchParams.set('cursor', cursor);
    next.searchParams.set('limit', new URL(currentUrl).searchParams.get('limit') || '1000');
    return next.href;
  }

  const offset = Number(pagination.offset);
  const total = Number(pagination.total);
  if (Number.isFinite(offset) && Number.isFinite(total) && pageSize > 0 && offset + pageSize < total) {
    const next = new URL(currentUrl);
    next.searchParams.set('offset', String(offset + pageSize));
    next.searchParams.set('limit', new URL(currentUrl).searchParams.get('limit') || '1000');
    return next.href;
  }
  return null;
}

export async function fetchResources({
  fetchImpl = fetch,
  discoveryUrl = DISCOVERY_URL,
  maxResources = MAX_RESOURCES,
} = {}) {
  const resources = [];
  const visited = new Set();
  let nextUrl = discoveryUrl;

  while (nextUrl && resources.length < maxResources && !visited.has(nextUrl)) {
    visited.add(nextUrl);
    const response = await fetchImpl(nextUrl, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`CDP Bazaar request failed: ${response.status}`);
    const body = await response.json();
    const items = responseItems(body);
    resources.push(...items.slice(0, maxResources - resources.length));
    nextUrl = nextPageUrl(body, nextUrl, items.length);
  }

  return resources;
}

async function main() {
  const resources = await fetchResources();
  const report = aggregateMarket(resources, Math.floor(Date.now() / 1000));
  const stateDir = resolveX402StateDir();
  await mkdir(stateDir, { recursive: true });
  const output = JSON.stringify(report);
  await writeFile(join(stateDir, 'market-scout.json'), output + '\n', 'utf8');
  process.stdout.write(output + '\n');
}

const isEntry = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntry) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
