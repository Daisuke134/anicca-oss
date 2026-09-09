import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { X402_STATE_DIR } from './state-paths.mjs';

export const EXPERIMENT_INTERVAL_MS = 5 * 60 * 1000;
export const STATE_DIR = X402_STATE_DIR;

function validVariantIndex(state, variants) {
  const index = Number(state?.variantIndex);
  return Number.isInteger(index) && index >= 0 && index < variants.length ? index : null;
}

export function decideExperiment({ state, externalCount, now, variants, intervalMs = EXPERIMENT_INTERVAL_MS }) {
  if (!Array.isArray(variants) || variants.length === 0) throw new TypeError('variants must not be empty');
  if (!Number.isFinite(externalCount) || externalCount < 0) throw new TypeError('externalCount must be non-negative');
  if (!Number.isFinite(now)) throw new TypeError('now must be finite');

  const index = validVariantIndex(state, variants);
  if (index === null) {
    return {
      action: 'applied',
      rewardExternalCount: 0,
      state: {
        experimentId: variants[0].id,
        variantIndex: 0,
        startedAt: now,
        baselineExternalCount: externalCount,
        status: 'running',
      },
    };
  }

  const baseline = Number.isFinite(state.baselineExternalCount) ? state.baselineExternalCount : externalCount;
  const rewardExternalCount = Math.max(0, externalCount - baseline);
  if (rewardExternalCount > 0 || state.status === 'winner') {
    return {
      action: 'winner',
      rewardExternalCount,
      state: { ...state, status: 'winner' },
    };
  }

  const startedAt = Number.isFinite(state.startedAt) ? state.startedAt : now;
  if (now - startedAt < intervalMs) {
    return { action: 'waiting', rewardExternalCount: 0, state };
  }

  const nextIndex = (index + 1) % variants.length;
  return {
    action: 'applied',
    rewardExternalCount: 0,
    state: {
      experimentId: variants[nextIndex].id,
      variantIndex: nextIndex,
      startedAt: now,
      baselineExternalCount: externalCount,
      status: 'running',
    },
  };
}

export function experimentStatePath(payTo, stateDir = STATE_DIR) {
  const wallet = String(payTo || '').toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(wallet)) throw new Error('valid EVM payTo required');
  return join(stateDir, `store-experiment-${wallet}.json`);
}

export function readExperimentState(payTo, stateDir = STATE_DIR) {
  try { return JSON.parse(readFileSync(experimentStatePath(payTo, stateDir), 'utf8')); }
  catch { return null; }
}

export function writeExperimentState(payTo, state, stateDir = STATE_DIR) {
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(experimentStatePath(payTo, stateDir), `${JSON.stringify(state)}\n`, 'utf8');
}

export function activeVariant(payTo, variants, stateDir = STATE_DIR) {
  const state = readExperimentState(payTo, stateDir);
  const index = validVariantIndex(state, variants) ?? 0;
  return variants[index];
}
