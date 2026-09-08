// telemetry-post-franklin.mjs — ONE-SHOT signed telemetry POST for Franklin (SELF-funded, Solana
// ed25519 instance). Appended fail-safe to skills/earn/sol-trade/run.sh after each trading pass (never
// blocks/affects the trade itself). Reads Franklin's OWN wallet key in-process only (never logged,
// never echoed) to sign; every balance read is a PUBLIC RPC call. Posts once and exits (unlike the
// long-running anicca-a3cdd4 daemon poster) because this runs once per launchd pass, not as a service.
//
// Contract = apps/landing/netlify/functions/_lib/{telemetry-schema,telemetry-verify}.js chain:"solana"
// branch: id = base58 pubkey (the verification key itself, ed25519 has no signer-recovery step), never
// case-folded; signature = base58(tweetnacl detached signature over the verbatim JSON.stringify bytes).
import fs from "fs";
import bs58 from "bs58";
import nacl from "tweetnacl";
import { normalizeModelSelection } from "../compute-proxy/model-map.mjs";

// franklin2-daemon-identity impl-review iteration-1 FIND-001 fix: the dashboard `host` label must
// derive from ANICCA_INSTANCE so franklin2 (and future franklin3, franklin10, …) are distinguishable
// from Franklin#1 on the dashboard, instead of every Franklin-family instance reporting the identical
// literal "Franklin" label. Pure, deterministic, no I/O — deliberately kept extractable/testable
// without importing this file (import triggers wallet-secret read + network I/O as a side effect); see
// __tests__/telemetry-host-label.test.mjs, which extracts this function verbatim from the source text.
function instanceHostLabel(instance) {
  const name = (instance || "franklin").trim();
  if (name === "franklin") return "Franklin"; // backward-compat: Franklin#1's dashboard row name is pinned, never changes
  return name.charAt(0).toUpperCase() + name.slice(1); // franklin2 -> Franklin2, franklin10 -> Franklin10, ...
}

function instanceHome(argv, env) {
  const marker = argv.indexOf("--home");
  const explicit = marker >= 0 ? argv[marker + 1] : "";
  if (explicit?.startsWith("/")) return explicit;
  if (env.ANICCA_HOME?.startsWith("/")) return env.ANICCA_HOME;
  return `${env.HOME}/.blockrun`;
}

const INSTANCE_HOME = instanceHome(process.argv.slice(2), process.env);
const SOLANA_WALLET_FILE = INSTANCE_HOME + "/.solana-session"; // base58 64-byte secret key (Franklin's own wallet, written by @blockrun/llm on first `franklin setup`)
const COST_LOG = INSTANCE_HOME + "/cost_log.jsonl";
const SOLR = "https://api.mainnet-beta.solana.com";
const SOL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const TELEMETRY_URL = process.env.ANICCA_TELEMETRY_URL || "https://aniccaai.com/.netlify/functions/telemetry";

const secretB58 = fs.readFileSync(SOLANA_WALLET_FILE, "utf8").trim();
const secretKey = bs58.decode(secretB58); // 64 bytes: tweetnacl secretKey format == Solana Keypair.secretKey
const address = bs58.encode(Buffer.from(secretKey.slice(32))); // last 32 bytes = the public key

// #18 DASH: stream Franklin's REAL wake activity to the /<host> page. The page (AgentClient.tsx) reads
// the field `log` with shape {ts, kind, slot, model, note} and polls dashboard-sync every 4s, so this
// makes the "Live activity" feed update in real time from Franklin's own ledger — no fakes. Franklin's
// realized earnings are $0 so far (it correctly WAITs when the edge doesn't clear fees), so revenue_by_source
// is empty/0 honestly; the activity log still shows it IS awake and deciding.
const LEDGER = INSTANCE_HOME + "/state/ledger.jsonl";
function activityLog() {
  try {
    return fs.readFileSync(LEDGER, "utf8").trim().split("\n").slice(-15).reverse().map((s) => {
      try {
        const o = JSON.parse(s);
        const res = String(o.result || "").replace(/\s+/g, " ").trim().slice(0, 120);
        return { ts: o.ts, kind: o.kind || "wake", slot: o.slot || null, model: o.model || null, note: res };
      } catch { return null; }
    }).filter(Boolean);
  } catch { return []; }
}

async function rpc(method, params) {
  const r = await fetch(SOLR, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  const j = await r.json();
  return j.result;
}

async function solBalance() {
  try { const r = await rpc("getBalance", [address]); return (r?.value || 0) / 1e9; } catch { return 0; }
}
async function usdcBalance() {
  try {
    const r = await rpc("getTokenAccountsByOwner", [address, { mint: SOL_USDC_MINT }, { encoding: "jsonParsed" }]);
    const list = r?.value || [];
    return list.reduce((s, a) => s + (Number(a.account?.data?.parsed?.info?.tokenAmount?.uiAmount) || 0), 0);
  } catch { return 0; }
}
async function solPrice() {
  try { const r = await fetch("https://api.coinbase.com/v2/prices/SOL-USD/spot"); return Number((await r.json()).data.amount) || 0; } catch { return 0; }
}
// Franklin's own LLM spend ledger (BlockRun x402 per-call cost, from its own wallet) — today's sum.
function burnToday() {
  const midnight = Math.floor(Date.now() / 86400000) * 86400;
  try {
    return fs.readFileSync(COST_LOG, "utf8").trim().split("\n")
      .map((l) => { try { return JSON.parse(l); } catch { return null; } })
      .filter((o) => o && o.ts >= midnight)
      .reduce((s, o) => s + (Number(o.cost_usd) || 0), 0);
  } catch { return 0; }
}

// Derive the dashboard label through the same normalizer as the repository proxy. ANICCA_MODEL is
// the loop's highest-precedence request model; without an override every current tier uses free/glm.
const FRANKLIN_MODEL = normalizeModelSelection(
  process.env.ANICCA_MODEL || "free/glm-4.7",
  process.env.ANICCA_FRONTIER_MODEL || "anthropic/claude-sonnet-4-6",
);
const HOST_LABEL = instanceHostLabel(process.env.ANICCA_INSTANCE);

async function post() {
  const [sol, usdc, price] = await Promise.all([solBalance(), usdcBalance(), solPrice()]);
  const net_worth_usd = +(usdc + sol * price).toFixed(6);
  const burn_day_usd = +burnToday().toFixed(6);
  const payload = {
    id: address, ts: Math.floor(Date.now() / 1000), host: HOST_LABEL, geo: "JP", chain: "solana",
    funding: "self", env: "local", brain: "proxy",
    model_live: FRANKLIN_MODEL.model, model_tier: FRANKLIN_MODEL.tier,
    net_worth_usd, revenue_mo_usd: 0, revenue_by_source: {}, log: activityLog(),
    burn_day_usd, runway_days: 999, status: "alive",
  };
  const message = JSON.stringify(payload);
  const sig = nacl.sign.detached(Buffer.from(message, "utf8"), secretKey);
  const signature = bs58.encode(Buffer.from(sig));
  const r = await fetch(TELEMETRY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, signature }) });
  console.log(new Date().toISOString(), "Franklin net", net_worth_usd, "->", r.status, await r.text());
}
await post();
