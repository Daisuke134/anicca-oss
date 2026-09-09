#!/usr/bin/env node
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { inferCategory } from './scout-market.mjs';
import { resolveX402StateDir } from './state-paths.mjs';

export const CORE_PATHS = ['/web-search', '/funding-rates', '/funding-rate-arb', '/research', '/llm', '/image'];
const EXCLUDED_CATEGORIES = new Set(['other', 'calc']);

function formatUsd(value) {
  return value.toFixed(6).replace(/\.?0+$/, '');
}

export function computeGaps(scout, ourCategories, now, opts = {}) {
  if (!(ourCategories instanceof Set)) throw new TypeError('ourCategories must be a Set');
  const timestamp = Number(now);
  if (!Number.isFinite(timestamp)) throw new TypeError('now must be a finite Unix timestamp');
  const { minMarketCount = 3, minCalls30d = 1, minPayerSignals30d = 1 } = opts;

  const opportunities = (Array.isArray(scout?.byCategory) ? scout.byCategory : [])
    .filter((item) => item && !EXCLUDED_CATEGORIES.has(item.category))
    .map((item) => {
      const category = String(item.category ?? '');
      const marketCount = Number(item.count);
      const medianPriceUsd = Number(item.medianPriceUsd);
      const calls30d = Number(item.calls30d);
      const payerSignals30d = Number(item.payerSignals30d);
      if (!category || !Number.isFinite(marketCount) || marketCount < minMarketCount
        || !Number.isFinite(medianPriceUsd) || medianPriceUsd < 0
        || !Number.isFinite(calls30d) || calls30d < minCalls30d
        || !Number.isFinite(payerSignals30d) || payerSignals30d < minPayerSignals30d) {
        return null;
      }

      const weServe = ourCategories.has(category);
      const payerSignals = Number.isFinite(payerSignals30d) && payerSignals30d >= 0 ? payerSignals30d : 0;
      const marketSummary = `${calls30d} paid calls/30d, ${payerSignals} payer signals, ${marketCount} listings @ $${formatUsd(medianPriceUsd)} median`;
      return {
        category,
        marketCount,
        medianPriceUsd,
        calls30d,
        payerSignals30d: payerSignals,
        weServe,
        opportunityScore: Number(((calls30d * medianPriceUsd) / marketCount).toFixed(6)),
        rationale: weServe
          ? `already covered (${marketSummary})`
          : `observed paid demand (${marketSummary}) we do NOT serve yet`,
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.opportunityScore - a.opportunityScore
      || a.category.localeCompare(b.category, 'en'));

  return {
    ts: Math.floor(timestamp),
    ourCategories: [...ourCategories],
    opportunities,
  };
}

async function main() {
  const stateDir = resolveX402StateDir();
  let scout;
  try {
    scout = JSON.parse(await readFile(join(stateDir, 'market-scout.json'), 'utf8'));
  } catch (error) {
    if (error?.code === 'ENOENT') {
      process.stdout.write('{"error":"no scout data"}\n');
      return;
    }
    throw error;
  }

  const ourCategories = new Set(CORE_PATHS.map(inferCategory));
  const report = computeGaps(scout, ourCategories, Math.floor(Date.now() / 1000));
  const output = JSON.stringify(report);
  await mkdir(stateDir, { recursive: true });
  await writeFile(join(stateDir, 'product-gaps.json'), output + '\n', 'utf8');
  process.stdout.write(output + '\n');
}

const isEntry = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntry) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
