// cost-basis.mjs — tracks the net USDC PRINCIPAL deposited per yield/investment venue, so telemetry can
// compute per-source REVENUE (P&L) = current on-chain value − cost basis. Without this the dashboard can
// only show deposited BALANCES, not whether the agent is actually MAKING or LOSING money.
// Dais 2026-06-22: "nobody cares how much is parked in the bank — the only thing people care about is:
// is it making money? if it's losing money, show a minus."
//
// Venue keys MUST match telemetry netWorth() breakdown keys (aave/morpho/moonwell/beefy/fluid/bluechip)
// so the poster can pair current value ↔ cost basis per source.
import fs from "fs";

const FILE = (process.env.ANICCA_HOME || process.env.HOME + "/.anicca") + "/skills/earn/state/cost-basis.json";

export function readCostBasis() {
  try { return JSON.parse(fs.readFileSync(FILE, "utf8")); } catch { return {}; }
}

function write(o) {
  fs.mkdirSync(FILE.slice(0, FILE.lastIndexOf("/")), { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(o, null, 2));
}

// Record a deposit (+) or withdrawal (−) of `usd` principal into `venue`. A withdrawal reduces the basis
// (money moved back to liquid is principal-OUT, not a loss); basis is floored at 0 — any amount withdrawn
// beyond the tracked basis is realised profit, surfaced by the P&L delta, never as a negative basis.
export function recordDeposit(venue, usd) { return adjust(venue, +Number(usd)); }
export function recordWithdraw(venue, usd) { return adjust(venue, -Number(usd)); }

function adjust(venue, delta) {
  if (!venue || !Number.isFinite(delta)) return null;
  const o = readCostBasis();
  o[venue] = Math.max(0, +(((o[venue] || 0) + delta)).toFixed(6));
  write(o);
  return o[venue];
}

// One-time seed for venues that already hold positions, so P&L starts from "now" (experiment start)
// instead of counting pre-existing principal as profit. Only sets venues NOT already tracked.
export function seedIfEmpty(seed) {
  const o = readCostBasis();
  let changed = false;
  for (const [v, usd] of Object.entries(seed)) {
    if (o[v] == null && Number.isFinite(Number(usd))) { o[v] = +Number(usd).toFixed(6); changed = true; }
  }
  if (changed) write(o);
  return o;
}
