#!/usr/bin/env node
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

import {
  appendUniqueSaleCandidates,
  normalizeClawMerchantSales,
  normalizeImageSales,
  normalizeRailwaySettlements,
  normalizeThe402Sales,
} from './lib/sale-observer.mjs';
import { resolveX402StateDir } from './state-paths.mjs';
const HERE = dirname(fileURLToPath(import.meta.url));
const FRANKLIN1 = '0x3EcCAD24794ca298D25378E9902A251322ea8749';
const FRANKLIN2 = '0xe7747Fd899D8987821Bb4CB3D6aDf22565F87ce9';
const CLAUDE_P = '0x810F6D61F7606dEEE2657d3083E150a222Bc29C5';
const THE402_PRODUCT_ID = 'prod_653429e9dd234895';
const CLAW_ASSET_ID = '54a0fabf-a95a-47bd-b2cc-81f3189430cb';
const RAILWAY_BASE_URL = 'https://x402-agents-production.up.railway.app';
const RAILWAY_PAY_TO = '0x6592EB8EF820aBC092e8C3474fb2042dffCCEDc7';

function unwrap(body) {
  return body?.data ?? body;
}

function rows(value, key) {
  if (Array.isArray(value)) return value;
  return Array.isArray(value?.[key]) ? value[key] : [];
}

function metric(value) {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

async function fetchJson(fetchFn, url, { apiKey } = {}) {
  const headers = apiKey ? { 'X-API-Key': apiKey } : {};
  const response = await fetchFn(url, { headers, signal: AbortSignal.timeout(30_000) });
  const body = await response.json().catch(() => null);
  if (!response.ok || body === null) throw new Error(`HTTP ${response.status}`);
  return unwrap(body);
}

export async function pollSaleSources({
  fetchFn = fetch,
  imageSources = [],
  the402,
  claw,
  railway,
} = {}) {
  if (typeof fetchFn !== 'function') throw new TypeError('fetchFn must be a function');
  const candidates = [];
  const errors = [];
  const metrics = {
    image: { settled_candidates: 0 },
    railway: { settlement_candidates: 0 },
    the402: {
      jobs: null,
      threads: null,
      settled_usd: null,
      held_usd: null,
      pending_usd: null,
      product_purchases: null,
      settlement_candidates: 0,
    },
    clawmerchants: {
      purchases: null,
      discovery_count: null,
      transaction_candidates: 0,
    },
  };

  for (const source of imageSources) {
    try {
      const offers = Array.isArray(source.offers) && source.offers.length
        ? source.offers
        : [{ route: '/image', priceUsd: '0.03' }];
      for (const offer of offers) {
        candidates.push(...normalizeImageSales(source.rows, {
          payTo: source.payTo,
          route: offer.route,
          priceUsd: offer.priceUsd,
        }));
      }
    } catch {
      errors.push({ source: 'x402-image', code: 'source_invalid' });
    }
  }
  metrics.image.settled_candidates = candidates.length;

  if (railway) {
    try {
      const baseUrl = String(railway.baseUrl || '').replace(/\/+$/, '');
      const body = await fetchJson(fetchFn, `${baseUrl}/settlements?limit=100`);
      const railwayCandidates = normalizeRailwaySettlements(rows(body, 'settlements'), {
        payTo: railway.payTo,
        allowedOffers: railway.allowedOffers,
      });
      candidates.push(...railwayCandidates);
      metrics.railway.settlement_candidates = railwayCandidates.length;
    } catch {
      errors.push({ source: 'x402-railway', code: 'poll_failed' });
    }
  }

  try {
    const [jobsBody, threadsBody, earningsBody, productBody] = await Promise.all([
      fetchJson(fetchFn, 'https://api.the402.ai/v1/jobs', { apiKey: the402.apiKey }),
      fetchJson(fetchFn, 'https://api.the402.ai/v1/threads', { apiKey: the402.apiKey }),
      fetchJson(fetchFn, 'https://api.the402.ai/v1/provider/earnings', { apiKey: the402.apiKey }),
      fetchJson(fetchFn, `https://api.the402.ai/v1/products/${the402.productId}`, { apiKey: the402.apiKey }),
    ]);
    const jobs = rows(jobsBody, 'jobs');
    const threads = rows(threadsBody, 'threads');
    const the402Candidates = normalizeThe402Sales(earningsBody, {
      payTo: the402.payTo,
      allowedOffers: the402.allowedOffers,
    });
    candidates.push(...the402Candidates);
    metrics.the402 = {
      jobs: jobs.length,
      threads: threads.length,
      settled_usd: metric(earningsBody?.earnings?.settled_usd),
      held_usd: metric(earningsBody?.earnings?.held_usd),
      pending_usd: metric(earningsBody?.earnings?.pending_usd),
      product_purchases: metric(productBody?.total_purchases ?? productBody?.purchase_count),
      settlement_candidates: the402Candidates.length,
    };
  } catch {
    errors.push({ source: 'the402', code: 'poll_failed' });
  }

  try {
    const [assetBody, transactionsBody] = await Promise.all([
      fetchJson(fetchFn, `https://clawmerchants.com/api/v1/assets/${claw.assetId}`),
      fetchJson(fetchFn, 'https://clawmerchants.com/api/v1/transactions?limit=100'),
    ]);
    const asset = assetBody?.asset ?? assetBody;
    const transactions = rows(transactionsBody, 'transactions');
    const clawCandidates = normalizeClawMerchantSales(transactions, {
      assetId: claw.assetId,
      payTo: claw.payTo,
      priceUsd: claw.priceUsd,
    });
    candidates.push(...clawCandidates);
    metrics.clawmerchants = {
      purchases: metric(asset?.totalPurchases ?? asset?.total_purchases),
      discovery_count: metric(asset?.discoveryCount ?? asset?.discovery_count),
      transaction_candidates: clawCandidates.length,
    };
  } catch {
    errors.push({ source: 'clawmerchants', code: 'poll_failed' });
  }

  return {
    candidates,
    metrics,
    errors,
  };
}

function readJsonLines(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').split('\n').filter(Boolean).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
}

function imageSources(stateDir = resolveX402StateDir()) {
  return [FRANKLIN1, FRANKLIN2, CLAUDE_P].map((payTo) => ({
    payTo,
    rows: readJsonLines(join(stateDir, `sales-${payTo.toLowerCase()}.jsonl`)),
    offers: payTo === FRANKLIN1
      ? [
        { route: '/image', priceUsd: '0.03' },
        { route: '/base-usdc-balance', priceUsd: '0.003' },
      ]
      : [{ route: '/image', priceUsd: '0.03' }],
  }));
}

async function main() {
  const stateRoot = join(homedir(), '.anicca');
  const stateDir = resolveX402StateDir();
  const credentials = JSON.parse(readFileSync(join(stateRoot, 'the402-credentials.json'), 'utf8'));
  if (typeof credentials.api_key !== 'string' || credentials.api_key.length < 16) {
    throw new Error('invalid credentials');
  }
  const result = await pollSaleSources({
    imageSources: imageSources(stateDir),
    the402: {
      apiKey: credentials.api_key,
      payTo: FRANKLIN1,
      productId: THE402_PRODUCT_ID,
      allowedOffers: {
        svc_1c7ca3dd9de841b1: { minUsd: '0.50', maxUsd: '25' },
        svc_128b02a7f3464be4: { minUsd: '0.50', maxUsd: '25' },
        [THE402_PRODUCT_ID]: { minUsd: '0.50', maxUsd: '0.50' },
      },
    },
    claw: { assetId: CLAW_ASSET_ID, payTo: FRANKLIN1, priceUsd: '0.03' },
    railway: {
      baseUrl: RAILWAY_BASE_URL,
      payTo: RAILWAY_PAY_TO,
      allowedOffers: {
        '/context-compressor': '8000',
        '/emotion-detector': '10000',
        '/buddhist-counsel': '10000',
        '/focus-coach': '10000',
        '/habit-designer': '10000',
        '/prompt-sanitizer': '5000',
        '/decision-clarifier': '8000',
        '/intent-router': '5000',
        '/funding-rates': '10000',
      },
    },
  });
  const store = join(stateDir, 'x402-sale-candidates.jsonl');
  const write = appendUniqueSaleCandidates(store, result.candidates);
  const candidateNotice = write.recorded > 0;
  process.stdout.write(`${JSON.stringify({
    observed_at: new Date().toISOString(),
    metrics: result.metrics,
    errors: result.errors,
    candidates_seen: result.candidates.length,
    ...write,
    candidate_not_verified_revenue: candidateNotice,
  })}\n`);
  if (candidateNotice) {
    spawnSync('/usr/bin/osascript', ['-e', 'display notification "Sale candidate detected; awaiting finalized Base verification." with title "x402 candidate"'], {
      stdio: 'ignore',
      timeout: 5_000,
    });
  }
}

const isEntry = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntry) {
  main().catch(() => {
    process.stderr.write('{"ok":false,"error":"observer_failed"}\n');
    process.exitCode = 1;
  });
}
