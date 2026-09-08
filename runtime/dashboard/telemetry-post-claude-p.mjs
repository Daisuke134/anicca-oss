// telemetry-post-claude-p.mjs — ONE-SHOT signed telemetry POST for claude-p (human-funded, EVM/Polygon
// instance — this Claude → Polymarket earner). Appended fail-safe to
// the active Polymarket trading pass. Posts once and exits — this is a
// launchd one-shot pass, not a daemon.
//
// IDENTITY NOTE (2026-07-05 finding, see skills/earn/polymarket-trade/SKILL.md): claude-p's REAL funded
// Polymarket wallet (0x904B50d2e214Da947d83D6a2D32c4E3Ffc17Eb74) is a smart-contract PROXY (ERC-1167,
// POLY_1271 signature type) controlled by the owner EOA in .env's POLYGON_WALLET_PRIVATE_KEY — it has NO
// private key of its own, so it structurally cannot produce a plain EIP-191 signature (which is all the
// telemetry endpoint currently verifies). The owner EOA (0x810F6D61…) is ALSO the shared
// founder/treasury identity used elsewhere (SEED_ADDRESSES) — signing telemetry as that address would
// misreport claude-p's identity as the treasury's. So, exactly like anicca-a3cdd4's own poster (whose
// wallet.json SIGNS while net worth is read from many separate DeFi vault addresses), this poster uses a
// DEDICATED signing-only identity key (~/.anicca-founder/state/telemetry-identity.json, holds no funds,
// never used for trading) while reporting the REAL balance read from the documented funded proxy address.
import fs from "fs";
import { privateKeyToAccount } from "viem/accounts";

const IDENTITY_PATH = process.env.HOME + "/.anicca-founder/state/telemetry-identity.json";
const FUNDED_ADDRESS = "0x904B50d2e214Da947d83D6a2D32c4E3Ffc17Eb74"; // claude-p's real Polymarket proxy wallet (documented in CLAUDE.md SSOT + colony-status.sh)
const POLY_RPC = "https://polygon-bor-rpc.publicnode.com";
const PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB";
const BALANCE_OF_SELECTOR = "0x70a08231";
const TELEMETRY_URL = process.env.ANICCA_TELEMETRY_URL || "https://aniccaai.com/.netlify/functions/telemetry";

const pk = JSON.parse(fs.readFileSync(IDENTITY_PATH, "utf8")).privateKey;
const acct = privateKeyToAccount(pk.startsWith("0x") ? pk : "0x" + pk);

// #18 DASH: stream claude-p's REAL activity + realized revenue to the /<host> page. The earner writes
// realized rows (redeems/trades) to the mother earn ledger, keyed by claude-p's funded wallet — read
// them here so the dashboard shows WHAT it did and WHICH source earned HOW MUCH (no fakes: realized
// net_usdc only, never unrealized). Path is relative to this script so it works from any launchd cwd.
const EARN_LEDGER = process.env.EARN_LEDGER
  || `${process.env.LIFE_MANAGER_SKILLS_STATE_ROOT || `${process.env.HOME}/.local/state/life-manager/state/skills`}/earn/earn-ledger.jsonl`;
function activityAndRevenue() {
  let lines = [];
  try { lines = fs.readFileSync(EARN_LEDGER, "utf8").trim().split("\n"); } catch { return { log: [], revenue_by_source: {}, monthly: 0 }; }
  const mine = [];
  for (const l of lines) {
    try {
      const e = JSON.parse(l);
      if (String(e.wallet || "").toLowerCase() === FUNDED_ADDRESS.toLowerCase()) mine.push(e);
    } catch { /* skip bad line */ }
  }
  const bySource = {}; let monthly = 0;
  for (const e of mine) {
    const net = Number(e.net_usdc) || 0;
    const s = e.source || "earn";
    bySource[s] = +(((bySource[s] || 0) + net)).toFixed(6);
    monthly += net;
  }
  // The /<host> page (apps/landing/app/[id]/AgentClient.tsx) reads the field `log` with shape
  // {ts, kind, slot, model, note} — NOT `log_feed`. Match it exactly or the page shows
  // "waiting for the next wake…" even though data was posted.
  const log = mine.slice(-15).reverse().map((e) => ({
    ts: e.ts, kind: e.source || "earn", slot: null, model: null,
    note: `${e.task || e.wake || ""}${e.net_usdc != null ? ` (net $${(+e.net_usdc).toFixed(4)})` : ""}`.slice(0, 160),
  }));
  return { log, revenue_by_source: bySource, monthly: +monthly.toFixed(6) };
}

async function erc20Balance(rpc, token, addr) {
  try {
    const data = BALANCE_OF_SELECTOR + addr.slice(2).toLowerCase().padStart(64, "0");
    const r = await fetch(rpc, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_call", params: [{ to: token, data }, "latest"] }) });
    const j = await r.json();
    if (!j.result || j.result === "0x") return 0;
    return Number(BigInt(j.result)) / 1e6;
  } catch { return 0; }
}

async function pmUnrealizedPnl(addr) {
  try {
    const r = await fetch(`https://data-api.polymarket.com/positions?user=${addr}&sizeThreshold=0.1`);
    const d = await r.json();
    const list = Array.isArray(d) ? d : [];
    return list.reduce((s, p) => s + (Number(p.cashPnl) || 0), 0);
  } catch { return 0; }
}

async function post() {
  const [bal, upnl] = await Promise.all([erc20Balance(POLY_RPC, PUSD, FUNDED_ADDRESS), pmUnrealizedPnl(FUNDED_ADDRESS)]);
  const net_worth_usd = +bal.toFixed(6);
  const { log, revenue_by_source, monthly } = activityAndRevenue();
  const payload = {
    id: acct.address, ts: Math.floor(Date.now() / 1000), host: "claude-p", geo: "JP",
    // chain:"polygon-proxy" (see apps/landing/netlify/functions/_lib/telemetry-schema.js on the
    // anicca-products side): this signing identity is deliberately NOT the funds-holding address
    // (FUNDED_ADDRESS, above), so on-chain verification of ITS balance would be a real-but-wrong
    // $0 — the endpoint intentionally wires no reader for this chain value and keeps the row
    // honestly self-reported/unverified instead.
    chain: "polygon-proxy",
    funding: "human", env: "local", brain: "claude-p",
    model_live: "claude-sonnet-5", model_tier: "frontier",
    net_worth_usd,
    // revenue = REALIZED per-source (sum of net_usdc from the earn ledger). We do NOT report the
    // unrealized PnL (upnl) as revenue — paper gains are not earnings (#18 R5).
    revenue_mo_usd: monthly, revenue_by_source, log,
    burn_day_usd: 0, runway_days: 999, status: "alive",
  };
  const message = JSON.stringify(payload);
  const signature = await acct.signMessage({ message });
  const r = await fetch(TELEMETRY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, signature }) });
  console.log(new Date().toISOString(), "claude-p net", net_worth_usd, "->", r.status, await r.text());
}
await post();
