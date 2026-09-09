#!/usr/bin/env node
// store-improve.mjs — market + own-store 5-minute experiment controller for action=improve.
// Loop child-script contract: prints exactly ONE JSON line on stdout, never throws.
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { allocateBandit } from './bandit.mjs';
import { CORE_PATHS, computeGaps } from './product-gaps.mjs';
import { inferCategory } from './scout-market.mjs';
import { SELF_WALLETS } from './lib/self-wallets.mjs';
import { readJsonl, resolvePayTo, resolveStateDir } from './store-review.mjs';
import { LLM_OFFER_VARIANTS } from './llm-offers.mjs';
import {
  decideExperiment,
  readExperimentState,
  writeExperimentState,
} from './store-experiment.mjs';
import { resolveX402StateDir } from './state-paths.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SCOUT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const WAKE_MS = 120_000;

function rowPath(row) {
  return row?.route ?? row?.path ?? null;
}

function validTimestamp(value) {
  const timestamp = Date.parse(value ?? '');
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function summarizeOwnProducts(salesRows, attemptRows, servedPaths, selfSet, now = Date.now()) {
  const sales = Array.isArray(salesRows) ? salesRows : [];
  const attempts = Array.isArray(attemptRows) ? attemptRows : [];
  const served = Array.isArray(servedPaths) ? servedPaths : [];
  const selfWallets = selfSet instanceof Set ? selfSet : new Set();
  const timestamp = Number.isFinite(Number(now)) ? Number(now) : Date.now();

  return served.map((path) => {
    const routeSales = sales.filter((row) => rowPath(row) === path);
    const routeAttempts = attempts.filter((row) => rowPath(row) === path);
    const external = routeSales.filter((row) => row?.settled
      && !selfWallets.has(String(row?.payer || '').toLowerCase())).length;
    const firstSeenTs = [...routeSales, ...routeAttempts]
      .map((row) => validTimestamp(row?.ts))
      .filter((value) => value !== null)
      .sort((a, b) => a - b)[0];
    const ageWakes = firstSeenTs === undefined
      ? 1
      : Math.max(1, Math.floor((timestamp - firstSeenTs) / WAKE_MS));

    return { path, external, attempts: routeAttempts.length, ageWakes };
  });
}

export function experimentExternalCount(products, path) {
  const product = (Array.isArray(products) ? products : []).find((item) => item.path === path);
  return Number.isFinite(product?.external) ? product.external : 0;
}

function readScout(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')); } catch { return null; }
}

export function scoutIsFresh(scout, now) {
  const scoutMs = Number(scout?.ts) * 1000;
  const demandAware = Array.isArray(scout?.byCategory)
    && scout.byCategory.length > 0
    && scout.byCategory.every((item) => Number.isFinite(Number(item?.calls30d)));
  return demandAware && Number.isFinite(scoutMs) && now - scoutMs <= SCOUT_MAX_AGE_MS;
}

function loadScout(now, stateDir, env) {
  const scoutPath = join(stateDir, 'market-scout.json');
  let scout = readScout(scoutPath);
  if (!scoutIsFresh(scout, now)) {
    try {
      execFileSync(process.execPath, [join(HERE, 'scout-market.mjs')], {
        cwd: HERE,
        env: { ...process.env, ...env, X402_STATE_DIR: stateDir },
        stdio: 'ignore',
      });
    } catch { /* best-effort refresh; stale or empty data remains usable */ }
    scout = readScout(scoutPath) ?? scout;
  }
  return scout ?? {};
}

function recommendationFor({ keep, explore, drop }, topGaps) {
  const gap = topGaps[0]?.category;
  if (drop.length > 0) {
    const keepClause = keep.length > 0 ? `keep ${keep.join(', ')}, ` : '';
    return `${keepClause}drop ${drop.join(', ')} (never earned external), and ${gap ? `consider the observed-demand ${gap} category` : 'note no proven demand category'}.`;
  }
  if (keep.length > 0) {
    return `Keep ${keep.join(', ')} and ${gap ? `consider the observed-demand ${gap} category` : 'note no proven demand category'}.`;
  }
  return `Keep testing ${explore.join(', ') || 'current routes'} and ${gap ? `consider the observed-demand ${gap} category` : 'note no proven demand category'}.`;
}

export function improve(env = process.env, now = Date.now()) {
  const payTo = resolvePayTo(env);
  if (!payTo) throw new Error('no payTo resolvable (no X402_PAYTO, no per-instance key)');

  const lower = payTo.toLowerCase();
  const logStateDir = resolveStateDir(payTo, env);
  const sales = readJsonl(join(logStateDir, `sales-${lower}.jsonl`));
  const attempts = readJsonl(join(logStateDir, `attempts-${lower}.jsonl`));
  const products = summarizeOwnProducts(sales, attempts, CORE_PATHS, new Set(SELF_WALLETS), now);
  const bandit = allocateBandit(products);
  const scout = loadScout(now, resolveX402StateDir(env), env);
  const ourCategories = new Set(CORE_PATHS.map(inferCategory));
  const gaps = computeGaps(scout, ourCategories, Math.floor(now / 1000));
  const topGaps = gaps.opportunities
    .filter(({ weServe }) => !weServe)
    .slice(0, 3)
    .map(({ category, marketCount, medianPriceUsd, calls30d, payerSignals30d, opportunityScore }) => ({
      category,
      marketCount,
      medianPriceUsd,
      calls30d,
      payerSignals30d,
      opportunityScore,
    }));

  return {
    ts: Math.floor(now / 1000),
    keep: bandit.keep,
    explore: bandit.explore,
    drop: bandit.drop,
    topGaps,
    recommendation: recommendationFor(bandit, topGaps),
    externalCount: experimentExternalCount(products, '/llm'),
  };
}

export function improveAndApply(env = process.env, now = Date.now()) {
  const result = improve(env, now);
  const payTo = resolvePayTo(env);
  const stateDir = resolveStateDir(payTo, env);
  const previous = readExperimentState(payTo, stateDir);
  const decision = decideExperiment({
    state: previous,
    externalCount: result.externalCount,
    now,
    variants: LLM_OFFER_VARIANTS,
  });
  if (decision.action === 'applied') writeExperimentState(payTo, decision.state, stateDir);
  const active = LLM_OFFER_VARIANTS[decision.state.variantIndex] || LLM_OFFER_VARIANTS[0];
  return {
    ...result,
    experiment: {
      action: decision.action,
      rewardExternalCount: decision.rewardExternalCount,
      experimentId: decision.state.experimentId,
      price: active.price,
      upstreamMaxUsd: active.upstreamMaxUsd,
      startedAt: decision.state.startedAt,
    },
  };
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  let output;
  try {
    output = improveAndApply();
    const stateDir = resolveX402StateDir();
    mkdirSync(stateDir, { recursive: true });
    writeFileSync(join(stateDir, 'store-improve.json'), `${JSON.stringify(output)}\n`, 'utf8');
  } catch (error) {
    output = { error: error instanceof Error ? error.message : String(error) };
  }
  process.stdout.write(`${JSON.stringify(output)}\n`);
  process.exitCode = 0;
}
