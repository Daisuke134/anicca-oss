/**
 * earn-detect.mjs — Effectful: read earn-ledger.jsonl and classify result.
 *
 * REQ-003: Earn-result determination.
 * - Reads $EARN_LEDGER (or $ANICCA_HOME/state/earn/earn-ledger.jsonl)
 * - Finds the line where line.wake === WAKE_ID (NEVER uses last-line position)
 * - Applies isProfitable() from skills/earn/lib/ledger.mjs
 *
 * Returns { profitable: boolean, earnLine: object|null }
 */

import { promises as fs } from 'node:fs';
import path from 'node:path';

/**
 * Locate and classify the earn result for a given wake.
 *
 * @param {string} wakeId - ULID of the current wake
 * @param {string} earnLedgerPath - absolute path to earn-ledger.jsonl
 * @param {Function} isProfitableFn - isProfitable from skills/earn/lib/ledger.mjs
 * @returns {Promise<{ profitable: boolean, earnLine: object|null }>}
 */
export async function classifyEarnResult(wakeId, earnLedgerPath, isProfitableFn) {
  let lines;
  try {
    const raw = await fs.readFile(earnLedgerPath, 'utf8');
    lines = raw
      .split('\n')
      .filter(l => l.trim().length > 0)
      .map(l => { try { return JSON.parse(l); } catch { return null; } })
      .filter(Boolean);
  } catch (err) {
    if (err.code === 'ENOENT') return { profitable: false, earnLine: null };
    // Other read errors: treat as non-profitable (never crash)
    process.stderr.write(`[earn-detect] ledger read error: ${err.message}\n`);
    return { profitable: false, earnLine: null };
  }

  // Find by WAKE_ID correlation key (spec: line.wake === WAKE_ID)
  const earnLine = lines.find(l => l.wake === wakeId) ?? null;
  if (!earnLine) {
    return { profitable: false, earnLine: null };
  }

  const profitable = typeof isProfitableFn === 'function'
    ? isProfitableFn(earnLine)
    : false;

  return { profitable, earnLine };
}

/**
 * Derive the default earn-ledger path from config.
 *
 * @param {object} config - loaded config
 * @returns {string}
 */
export function defaultEarnLedgerPath(config) {
  // Explicit override wins (from EARN_LEDGER env or ANICCA_EARN_SKILL dir)
  if (config.EARN_LEDGER) return config.EARN_LEDGER;
  if (config.ANICCA_HOME) {
    return path.join(config.LIFE_MANAGER_SKILLS_STATE_ROOT || path.join(config.ANICCA_HOME, 'state', 'skills'), 'earn', 'earn-ledger.jsonl');
  }
  return path.join(process.cwd(), 'skills', 'earn', 'state', 'earn-ledger.jsonl');
}
