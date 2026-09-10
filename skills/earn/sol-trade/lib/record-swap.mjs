// record-swap.mjs — record the on-chain USDC P&L of a sol-trade Jupiter swap (WIN *or* LOSS) so
// isProfitable / self-eval can finally see Franklin's own realized results. Mirrors the proven
// clip-promote/record-payout.mjs pipeline (sigStatus -> usdcDeltaForSig -> record) with TWO
// deliberate differences: (1) record-payout gates on delta>0 (a payout confirmation); this
// records ANY confirmed delta, because REQ-002's purpose is P&L VISIBILITY (a loss must be
// recorded too, or the guard/self-eval is blind to it). (2) this NEVER sets external:true -- a
// same-wallet Jupiter swap proves nothing about EXTERNAL revenue (GATE-0), so isProfitable()
// must stay false for every sol-trade line by construction (FIND-004; scope is P&L VISIBILITY
// only, a distinct, future decision). sig-keyed idempotency. run.sh invokes this under `env -i`
// so record.mjs's malice-guard passes. Never throws to the caller (the slot must never brick on
// a record attempt): an RPC failure at EITHER verify step (sigStatus or usdcDeltaForSig)
// degrades to a "verify-error" status, never an uncaught throw and never a NaN/Infinity-shaped
// delta reaching record.mjs (FIND-004/PROP-016).
import { sigStatus, usdcDeltaForSig } from "../../../_shared/lib/solana-verify.mjs";
import { readLedger } from "../../../_shared/lib/ledger.mjs";
import { record } from "../../lib/record.mjs";

export async function recordSwap(
  { sig, sigs, wallet, ledger, task = "jupiter swap round-trip", wake },
  opts = {},
) {
  const signatures = Array.isArray(sigs) && sigs.length ? [...new Set(sigs)] : (sig ? [sig] : []);
  sig = signatures.at(-1);
  if (!sig || !wallet || !ledger) return { status: "bad-args", sig: sig || null };
  const previousRows = await readLedger(ledger);
  const legacyRow = previousRows.find((row) => signatures.includes(row?.sig));
  const legacyDuplicate = Boolean(legacyRow);

  let delta = 0;
  for (const signature of signatures) {
    let confirmed;
    try {
      ({ confirmed } = await sigStatus(signature, opts));
    } catch (e) {
      return { status: "verify-error", sig: signature, error: e.message };
    }
    if (!confirmed) return { status: "unconfirmed", sig: signature };
    let part;
    try {
      part = await usdcDeltaForSig(signature, wallet, opts);
    } catch (e) {
      return { status: "verify-error", sig: signature, error: e.message };
    }
    if (part === null || part === undefined || Number.isNaN(part)) {
      return { status: "no-delta", sig: signature };
    }
    delta = Math.round((delta + part) * 1e6) / 1e6;
  }

  // net_usdc = earn - cost = delta, keeping earn_usdc/cost_usdc non-negative for both win and loss.
  const earn_usdc = delta > 0 ? delta : 0;
  const cost_usdc = delta < 0 ? -delta : 0;
  const occurredEpoch = legacyRow?.ts || Math.floor(Date.now() / 1000);
  const occurred_at = new Date(Number(occurredEpoch) * 1000).toISOString();
  const json = JSON.stringify({
    ts: occurredEpoch, wallet, source: "sol-trade", task, earn_usdc, cost_usdc,
    sig, confirmed: true, chain: "solana",
    // NO external:true here (FIND-004): sol-trade is P&L VISIBILITY only, not a GATE-0
    // external-revenue classification -- a same-wallet swap proves nothing about external
    // revenue, so isProfitable() must stay false for this source unconditionally.
    wake,
  });
  const result = { status: legacyDuplicate ? "duplicate" : "recorded", sig, signatures,
    net_usdc: delta, earn_usdc, cost_usdc, occurred_at, profitable: false };
  if (typeof opts.recordFinancial === "function") result.financial = await opts.recordFinancial(result);
  if (!legacyDuplicate) {
    const { profitable } = await record(json, ledger);
    result.profitable = profitable;
  }
  return result;
}

// CLI: run.sh calls `env -i PATH HOME SOLANA_RPC_URL SIG WALLET EARN_LEDGER WAKE_ID node record-swap.mjs`.
// Prints a JSON result line; never exits non-zero (the slot never bricks on a record attempt).
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop())) {
  let recordFinancial;
  if (process.env.LIFE_MANAGER_REPO && process.env.LM_FINANCIAL_RECORDS_DIR) {
    const { createRequire } = await import("node:module");
    const path = await import("node:path");
    const require = createRequire(import.meta.url);
    const root = path.resolve(process.env.LIFE_MANAGER_REPO);
    const { createJsonlFinancialRecordStore } = require(path.join(root, "apps/life-manager/lib/financial-record-store.js"));
    const { createSolanaTradeFinancialWriter } = require(path.join(root, "apps/life-manager/lib/solana-trade-financial-record.js"));
    recordFinancial = createSolanaTradeFinancialWriter({
      store: createJsonlFinancialRecordStore({ directoryPath: path.resolve(process.env.LM_FINANCIAL_RECORDS_DIR) }),
      subjectId: process.env.LM_CFO_SUBJECT_ID || process.env.LM_UID || "local",
    });
  }
  const r = await recordSwap({
    sig: process.env.SIG,
    sigs: process.env.SIGS_JSON ? JSON.parse(process.env.SIGS_JSON) : undefined,
    wallet: process.env.WALLET,
    ledger: process.env.EARN_LEDGER,
    wake: process.env.WAKE_ID,
  }, { recordFinancial }).catch((e) => ({ status: "error", error: e.message }));
  console.log(JSON.stringify(r));
}
