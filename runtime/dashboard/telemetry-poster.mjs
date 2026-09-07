// Telemetry poster — the local anicca reports its live state to aniccaai.com every 120s so it
// shows on the public dashboard (ranking) and its own /<host> live page. Signs the verbatim
// message with the agent's own wallet key (no human key). Includes the last 20 ledger lines so
// the /<host> page can stream the agent's REAL activity log (no fakes).
import { privateKeyToAccount } from "viem/accounts";
import { createPublicClient, http } from "viem";
import { base } from "viem/chains";
import fs from "fs";
import { assignIdentity } from "../identity.mjs";
import { readCostBasis } from "../../skills/earn/lib/cost-basis.mjs";
import { revenueBySource as pureRevenueBySource } from "../../skills/earn/lib/revenue.mjs";
import { hlState } from "../lib/hl-state.mjs";
import { buildTelemetryMsg } from "./telemetry-msg.mjs";
import { resolveEvmPrivateKey } from "../../skills/earn/lib/resolve-identity.mjs";

const HOME = process.env.HOME;
// #28: sign telemetry with THIS instance's OWN gated per-instance key (EFFECTIVE_HOME first, legacy
// only for the rightful owner). A foreign spawn must NEVER read $HOME/.automaton/wallet.json directly
// and self-report as automaton (identity spoofing + private key in a non-owner process) — if it has no
// own key yet, it simply does not post (fail-closed) rather than misreport another instance's identity.
const pk = resolveEvmPrivateKey();
if (!pk) { console.error("telemetry-poster: no per-instance EVM key resolvable — not posting (would misreport identity)"); process.exit(0); }
const acct = privateKeyToAccount(pk);
// Unique-by-construction identity: explicit ANICCA_NAME wins, else a guaranteed-unique handle derived
// from this instance's wallet address (collision-impossible across spawns; auto-registers on first POST).
const NAME = process.env.ANICCA_NAME || (await assignIdentity(acct.address)).name;
// Identity attributes for the fleet dashboard (REQ-1): funding origin (human gives compute only / self =
// own wallet), runtime environment (local Mac vs cloud), and brain backend (claude-p subscription vs the
// self-pay proxy). Declared via env so one runtime serves both types by config, not by a separate script.
const FUNDING = process.env.ANICCA_FUNDING || "human";
const ENV = process.env.ANICCA_ENV || "local";
const BRAIN = process.env.ANICCA_BRAIN || "claude-p";
const LEDGER = (process.env.ANICCA_HOME || HOME + "/.anicca") + "/state/ledger.jsonl";
const SKILLS_STATE_ROOT = process.env.LIFE_MANAGER_SKILLS_STATE_ROOT
  || (process.env.ANICCA_HOME || HOME + "/.anicca") + "/state/skills";
const EARN_LEDGER = process.env.EARN_LEDGER || SKILLS_STATE_ROOT + "/earn/earn-ledger.jsonl";

// REAL realised earnings (no hardcoded 0): read the earn ledger and sum earn_usdc / cost_usdc, both in
// total and per-source, so the live page can show WHICH tool earned HOW MUCH. revenue/burn were
// hardcoded 0 before — that is why the dashboard showed $0 earned despite real positions.
function earnings() {
  let revenue = 0, cost = 0; const bySource = {};
  try {
    for (const line of fs.readFileSync(EARN_LEDGER, "utf8").trim().split("\n")) {
      if (!line) continue;
      let o; try { o = JSON.parse(line); } catch { continue; }
      const e = Number(o.earn_usdc || 0), c = Number(o.cost_usdc || 0);
      revenue += e; cost += c;
      const s = o.source || "unknown";
      bySource[s] = +(((bySource[s] || 0) + e)).toFixed(6);
    }
  } catch { /* no ledger yet */ }
  return { revenue: +revenue.toFixed(6), cost: +cost.toFixed(6), bySource };
}

const pub = createPublicClient({ chain: base, transport: http("https://base-rpc.publicnode.com") });
const ABI = [{ name: "balanceOf", type: "function", stateMutability: "view", inputs: [{ type: "address" }], outputs: [{ type: "uint256" }] }];
const U = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", A = "0x4e65fE4DbA92790696d040ac24Aa414708F5c0AB",
      // NOTE (verified on-chain 2026-06-22): M = Moonwell mUSDC (name "Moonwell USDC") → drives the
      // `moonwell` field; V = Steakhouse Prime USDC (a Morpho MetaMorpho vault) → drives the `morpho`
      // field. The single-letter names are NOT mnemonic; the field assignments below are the source of truth.
      M = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22", V = "0xbeef0e0834849aCC03f0089F01f4F1Eeb06873C9",
      BF = "0x83152eE78d8f20Bba134A5FF000D551355Ce3996", // Beefy morpho-gauntlet-frontier USDC vault
      FL = "0xf42f5795D9ac7e9D757dB633D693cD548Cfd9169", // Fluid fUSDC (ERC4626) — was MISSING from net worth
      WETH = "0x4200000000000000000000000000000000000006"; // blue-chip ETH investment leg
// Snapshot store for daily/monthly revenue (P&L change over a period). Date.now/new Date are fine here
// (this is a long-running daemon, not a replayable Workflow script).
const PNL_SNAP = process.env.EARN_PNL_SNAPSHOT || SKILLS_STATE_ROOT + "/earn/pnl-snapshots.json";
const bal = (t, w) => pub.readContract({ address: t, abi: ABI, functionName: "balanceOf", args: [w] }).then(Number);
async function ethPrice() {
  try { const r = await fetch("https://api.coinbase.com/v2/prices/ETH-USD/spot"); return Number((await r.json()).data.amount) || 0; } catch { return 0; }
}

async function netWorth() {
  const [l, a, mt, ms, bfsh, flsh, weth, nativeEth, ep, hl] = await Promise.all([
    bal(U, acct.address), bal(A, acct.address), bal(M, acct.address), bal(V, acct.address), bal(BF, acct.address),
    bal(FL, acct.address), bal(WETH, acct.address), pub.getBalance({ address: acct.address }).then(Number), ethPrice(),
    hlState(acct.address), // Hyperliquid account (was MISSING — $8.84 invisible; the user asked "why is HL not in the dashboard")
  ]);
  const ex = Number(await pub.readContract({ address: M, abi: [{ name: "exchangeRateStored", type: "function", stateMutability: "view", inputs: [], outputs: [{ type: "uint256" }] }], functionName: "exchangeRateStored" }));
  const mo = Number(await pub.readContract({ address: V, abi: [{ name: "convertToAssets", type: "function", stateMutability: "view", inputs: [{ type: "uint256" }], outputs: [{ type: "uint256" }] }], functionName: "convertToAssets", args: [BigInt(ms)] }));
  const fl = Number(await pub.readContract({ address: FL, abi: [{ name: "convertToAssets", type: "function", stateMutability: "view", inputs: [{ type: "uint256" }], outputs: [{ type: "uint256" }] }], functionName: "convertToAssets", args: [BigInt(flsh)] }).catch(() => 0));
  const ppfs = Number(await pub.readContract({ address: BF, abi: [{ name: "getPricePerFullShare", type: "function", stateMutability: "view", inputs: [], outputs: [{ type: "uint256" }] }], functionName: "getPricePerFullShare" }).catch(() => 0));
  const usd = (x) => x / 1e6;
  // blue-chip leg: WETH + native ETH (gas+investment) valued at spot ETH price
  const bluechip = ((weth + nativeEth) / 1e18) * ep;
  return { liquid: usd(l), aave: usd(a), morpho: usd(mo), moonwell: (mt * ex / 1e18) / 1e6, beefy: (bfsh * ppfs / 1e18) / 1e6, fluid: usd(fl), bluechip, hl: hl.accountValue, hlUpnl: hl.unrealizedPnl };
}

// REVENUE per source = pure lib (skills/earn/lib/revenue.mjs) so the logic is unit-tested independently
// of this daemon. Reads cost basis here (I/O) and delegates the pure P&L math.
function revenueBySource(nw, earnBySource) {
  return pureRevenueBySource(nw, readCostBasis(), earnBySource);
}

// Daily / monthly revenue, defined so the numbers ADD UP with the per-source breakdown (Dais 2026-06-22:
// "+$0.0007 today but ETH invest −$0.0079 makes no sense"):
//   monthly = totalPnl EXACTLY = Σ revenue_by_source  → the breakdown always sums to the monthly figure.
//   daily   = totalPnl − (totalPnl at the first reading today) → "how much P&L moved today" (a delta).
// So "this month −$0.0079" matches "ETH invest −$0.0079", and "today +$0.0007" is just today's tick.
function periodRevenue(totalPnl) {
  const day = new Date().toISOString().slice(0, 10);
  let snap = {}; try { snap = JSON.parse(fs.readFileSync(PNL_SNAP, "utf8")); } catch { /* first run */ }
  if (!snap.day || snap.day.date !== day) snap.day = { date: day, pnl: totalPnl };
  try { fs.mkdirSync(PNL_SNAP.slice(0, PNL_SNAP.lastIndexOf("/")), { recursive: true }); fs.writeFileSync(PNL_SNAP, JSON.stringify(snap)); } catch { /* best effort */ }
  return { daily: +(totalPnl - snap.day.pnl).toFixed(6), monthly: +totalPnl.toFixed(6) };
}

function recentLog(n = 20) {
  try {
    return fs.readFileSync(LEDGER, "utf8").trim().split("\n").slice(-n)
      .map((s) => { try { const o = JSON.parse(s); return { ts: o.ts, kind: o.kind, slot: o.slot || null, model: o.model || null, note: o.task || o.source || (o.deposited_usdc ? `+${o.deposited_usdc} USDC->yield` : "") }; } catch { return null; } })
      .filter(Boolean);
  } catch { return []; }
}

function lastModel() {
  const lines = recentLog(20);
  for (let i = lines.length - 1; i >= 0; i--) if (lines[i].model) return lines[i].model;
  return "auto";
}
const FREE_RE = /nvidia|flash|qwen|free|oss|gpt-oss/i;

let lastGood = null;
const sumNw = (x) => +(x.liquid + x.aave + x.morpho + x.moonwell + (x.beefy || 0) + (x.fluid || 0) + (x.bluechip || 0) + (x.hl || 0)).toFixed(2);

async function post() {
  try {
    let nw = await netWorth();
    let total = sumNw(nw);
    // GLITCH GUARD: on-chain position reads (morpho/moonwell/bluechip) occasionally return 0 on a
    // transient RPC hiccup, collapsing net worth to liquid-only (false-alarm $0.05/$1.42). A real
    // change can't halve a positions-heavy wallet in one 2-min cycle — so on a >50% collapse, re-read
    // once, and if still collapsed, keep the last good value instead of posting the glitch.
    if (lastGood && total < lastGood.total * 0.5) {
      nw = await netWorth();
      total = sumNw(nw);
      if (total < lastGood.total * 0.5) { nw = lastGood.nw; total = lastGood.total; }
    }
    lastGood = { nw, total };
    const ts = Math.floor(Date.now() / 1000);
    const model = lastModel();
    const tier = FREE_RE.test(model) ? "free" : "frontier";
    const earn = earnings();
    // REVENUE the dashboard actually wants: per-source P&L (earned/lost, minus allowed) + daily + monthly.
    // HL revenue = its unrealised PnL (Hyperliquid gives it directly — no cost basis needed). Merge it in
    // alongside realised earnings so the dashboard shows an `hl` P&L cell (red when the perp is losing).
    const rev = revenueBySource(nw, { ...earn.bySource, hl: nw.hlUpnl || 0 });
    const period = periodRevenue(rev.total);
    // Net worth = total held. Daily/monthly revenue = P&L change over the period (negative when losing).
    // The message SHAPE lives in telemetry-msg.mjs (pure, side-effect-free) so it is unit-testable for
    // shape + key-safety without importing this poster (which posts on import).
    const { msg } = buildTelemetryMsg({
      id: acct.address, ts, host: NAME, geo: "JP",
      funding: FUNDING, env: ENV, brain: BRAIN,
      model_live: model, model_tier: tier,
      net_worth_usd: total,
      daily_revenue_usd: period.daily, monthly_revenue_usd: period.monthly,
      revenue_by_source: rev.bySource,
      burn_day_usd: earn.cost, runway_days: 999,
      status: "alive",
      breakdown: nw, log: recentLog(20),
    });
    const signature = await acct.signMessage({ message: msg });
    const r = await fetch("https://aniccaai.com/.netlify/functions/telemetry", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: msg, signature }),
    });
    console.log(new Date().toISOString(), NAME, "net", total, "log", recentLog(20).length, "->", r.status, await r.text());
  } catch (e) { console.log("err", e.message); }
}
await post();
setInterval(post, 120000);
