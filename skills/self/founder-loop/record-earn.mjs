#!/usr/bin/env node
// record-earn.mjs — record ONLY real EXTERNAL earnings to the FOUNDER node's own body ledger.
//
// An earning = USDC Transferred to the founder wallet by an EXTERNAL payer (`from` ∉ MY wallets), summed since the
// last processed block (a BLOCK CURSOR, not a balanceOf delta). A self-transfer between my own wallets contributes
// ZERO — so I can NEVER fabricate an earning by moving my own money (THE GOAL / INV-7). All sprint-1..5 anti-fake
// invariants are preserved: prod root is an env-INDEPENDENT literal (os.homedir() honors $HOME → never the root); every
// seam is FOUNDER_TEST-gated; the wallet is the pinned canonical founder wallet; the ledger must resolve INSIDE the
// founder dir (symlink-deref); fail-closed; the cursor advances crash-safely (under-count, never double-count).
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const TEST = process.env.FOUNDER_TEST === "1";   // every override below is honored ONLY in test
const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf("--" + k); return i >= 0 ? args[i + 1] : d; };
function die(m) { console.error("record-earn: " + m); process.exit(1); }

// Production follows the registry-owned per-loop root; only tests may use FOUNDER_DIR.
const DEFAULT_FOUNDER_DIR = path.join(os.userInfo().homedir, ".local", "state", "life-manager", "founder-loop-cadence");
const FOUNDER_DIR = TEST
  ? (process.env.FOUNDER_DIR || DEFAULT_FOUNDER_DIR)
  : (process.env.LIFE_MANAGER_STATE_ROOT || DEFAULT_FOUNDER_DIR);
if (!path.isAbsolute(FOUNDER_DIR)) die("LIFE_MANAGER_STATE_ROOT must be absolute");
if (!TEST && path.resolve(FOUNDER_DIR) !== path.resolve(DEFAULT_FOUNDER_DIR)) {
  die("LIFE_MANAGER_STATE_ROOT must equal the canonical founder-loop-cadence root");
}
const STATE = path.join(FOUNDER_DIR, "state");
const WALLET_JSON = path.join(FOUNDER_DIR, "wallet.json");
const CURSOR_FILE = path.join(STATE, "block-cursor.txt");
const LEDGER = (TEST && process.env.FOUNDER_LEDGER) || path.join(STATE, "earn-ledger.jsonl");

// INV-3 (symlink-deref): the ledger must resolve INSIDE the founder body.
let realFounder;
try { realFounder = fs.realpathSync(FOUNDER_DIR); } catch { die("founder dir missing/unreadable: " + FOUNDER_DIR + " (gen-wallet/init first)"); }
let anc = path.resolve(LEDGER);
while (!fs.existsSync(anc) && path.dirname(anc) !== anc) anc = path.dirname(anc);
const realAnc = fs.realpathSync(anc);
if (realAnc !== realFounder && !(realAnc + path.sep).startsWith(realFounder + path.sep)) die("INV-3: ledger must resolve INSIDE the founder dir, never the public site/dashboard render.");

// INV-1: the wallet is the pinned canonical founder wallet — never a shared/other-instance wallet.
const FOUNDER_WALLET_EXPECTED = "0x810f6d61f7606deee2657d3083e150a222bc29c5";
// 0xa3cdd4... = automaton's wallet BEFORE the 2026-07-07 rotation (key leaked, kept here so a
// self-transfer from the now-retired address still never counts as earned); 0xb9dd3b67... = its
// current (post-rotation) address. 0x9b1ee988... = a separate known-internal wallet (unrelated).
//
// 0xf70da978... = FOUND 2026-07-12 by forensically tracing the two largest ledger rows ("x402 earn
// $22.97" tx 0x3b3eeee6…, "x402 earn $7.98" tx 0x41ead2f3…): both were USDC Transfers FROM this SAME
// address. It has NO contract code (eth_getCode = "0x", a plain EOA) and currently holds >$3.1M USDC
// on Base — far too large and too centralized to be an x402 micro-payment counterparty. That size/shape
// matches a funding/bridge/exchange-withdrawal hot wallet, not a paying customer, and the founder
// wallet's actual Base balance right after those two "earn" rows (~$1.95) never reflected $30+ of new
// revenue — confirming the money was routed THROUGH, not earned. Fail-closed per the no-human-loop
// rule (task 2026-07-12, "稼ぎを過大に見積もる方が危険"): treat as internal/funding, never as earnings,
// until it is positively identified as a legitimate counterparty.
const SHARED = ["0xa3cdd4ec6b94f01826aaf90a6d5538a2aa8c4c21", "0xb9dd3b67921b354c656523d6851537988f31dd56", "0x9b1ee988b1a2931abce467f0a8eaff6c70c93e83", "0xf70da97812cb96acdf810712aa562db8dfa3dbef"];
let wallet;
if (TEST && process.env.FOUNDER_WALLET) wallet = process.env.FOUNDER_WALLET;
else { try { wallet = JSON.parse(fs.readFileSync(WALLET_JSON, "utf8")).address; } catch { die("no founder wallet.json (gen-wallet first)"); } }
if (!/^0x[a-fA-F0-9]{40}$/.test(wallet || "")) die("bad founder wallet address");
if (SHARED.includes(wallet.toLowerCase())) die("INV-1: founder wallet must NOT be a shared/other instance's wallet");
if (wallet.toLowerCase() !== FOUNDER_WALLET_EXPECTED) die("INV-1: founder wallet must be the PINNED founder wallet — FIND-302");

// MY wallets — a Transfer FROM any of these to the founder wallet is INTERNAL, never an earning (INV-7).
const MY_WALLETS = [wallet.toLowerCase(), ...SHARED];

const source = opt("source", "x402");
const cost = Number(opt("cost", "0")) || 0;
const task = opt("task", "earn");
const wake = opt("wake", String(Math.floor(Date.now() / 1000)));

const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"; // Transfer(address,address,uint256)
const TRUSTED_RPC = "https://mainnet.base.org";
async function rpc(method, params) {
  const url = (TEST && process.env.BASE_RPC_URL) || TRUSTED_RPC;
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  const j = await r.json();
  if (j.error || j.result === undefined) throw new Error(method + ": " + JSON.stringify(j.error || j));
  return j.result;
}
async function blockNow() {
  if (TEST && process.env.FOUNDER_BLOCK_NOW !== undefined) return Number(process.env.FOUNDER_BLOCK_NOW);
  // FIND-601: scan the FINALIZED head only. eth_blockNumber (latest) is un-finalized — a Transfer in a block that
  // later reorgs would be counted though it never settled, and a reorged-out tx re-mined later would double-count.
  // Finalized blocks cannot reorg, so the cursor never advances past genuinely-settled income.
  const b = await rpc("eth_getBlockByNumber", ["finalized", false]);
  if (!b || b.number === undefined) throw new Error("no finalized block");
  return Number(BigInt(b.number));
}
// sum the value of EXTERNAL inflows ({from} ∉ MY_WALLETS) from already-parsed {from,value} logs
function sumExternal(logs) {
  let sum = 0;
  for (const lg of logs) if (!MY_WALLETS.includes(String(lg.from || "").toLowerCase())) sum += Number(lg.value) || 0;
  return +sum.toFixed(6);
}
// parse RAW eth_getLogs entries → {from,value}, RE-VERIFYING topic0==Transfer, address==USDC, to==founder
// (never trust the RPC's server-side filter for a money invariant — FIND-603). Malformed entries are dropped.
function parseRawLogs(res) {
  if (!Array.isArray(res)) throw new Error("getLogs not an array");
  const wlow = wallet.slice(2).toLowerCase();
  return res
    .filter((l) => l && String(l.address || "").toLowerCase() === USDC.toLowerCase())
    .filter((l) => Array.isArray(l.topics) && l.topics.length >= 3 && String(l.topics[0]).toLowerCase() === TRANSFER_TOPIC && String(l.topics[2]).toLowerCase() === "0x" + "0".repeat(24) + wlow) // FIND-704: exact padded-topic equality, not a suffix match
    .map((l) => ({ from: "0x" + String(l.topics[1]).slice(26), value: Number(BigInt(l.data)) / 1e6 }));
}
// sum of USDC Transferred to the founder wallet by EXTERNAL payers in (fromBlock..toBlock]
async function externalInflow(fromBlock, toBlock) {
  const logsSeam = TEST ? process.env.FOUNDER_LOGS_JSON : undefined;     // pre-parsed [{from,value}] — filter/sum tests
  if (logsSeam !== undefined) return sumExternal(JSON.parse(logsSeam));
  const rawSeam = TEST ? process.env.FOUNDER_RAW_LOGS_JSON : undefined;  // raw eth_getLogs result — exercises the real parse
  let res;
  if (rawSeam !== undefined) res = JSON.parse(rawSeam);
  else {
    const toTopic = "0x000000000000000000000000" + wallet.slice(2).toLowerCase();
    res = await rpc("eth_getLogs", [{ address: USDC, topics: [TRANSFER_TOPIC, null, toTopic], fromBlock: "0x" + fromBlock.toString(16), toBlock: "0x" + toBlock.toString(16) }]);
  }
  return sumExternal(parseRawLogs(res));
}
function writeCursorAtomic(v) {
  fs.mkdirSync(STATE, { recursive: true });
  const tmp = CURSOR_FILE + ".tmp";
  fs.writeFileSync(tmp, String(v));
  fs.renameSync(tmp, CURSOR_FILE);
}

// cursor = last processed block. STRICT: corrupt → fail-closed, never coerced. null = not initialized.
let cursor = null;
const cursorSeam = TEST ? process.env.FOUNDER_CURSOR : undefined;
if (cursorSeam !== undefined) { cursor = Number(cursorSeam); if (!Number.isInteger(cursor) || cursor < 0) die("bad cursor seam"); }
else if (fs.existsSync(CURSOR_FILE)) { const raw = fs.readFileSync(CURSOR_FILE, "utf8").trim(); const n = Number(raw); if (raw === "" || !Number.isInteger(n) || n < 0) die("cursor file corrupt — fail-closed"); cursor = n; }

const now = await blockNow().catch((e) => die("block query failed (fail-closed): " + e.message));

// FIRST run: initialize the cursor, record nothing — pre-existing balance is NOT an earning.
if (cursor === null) { writeCursorAtomic(now); console.log(`record-earn: initialized block cursor=${now} (first run — no earning recorded)`); process.exit(0); }
if (now < cursor) die("block height went backwards (fail-closed)");

// FIND-702: cap the scan span so a single eth_getLogs never exceeds the provider's block-range limit (a silent
// truncation would under-count). Advance the cursor only by what we actually scanned — the next run continues; no skip.
const MAX_SPAN = 9000;
const to = Math.min(now, cursor + MAX_SPAN);

const earned = await externalInflow(cursor + 1, to).catch((e) => die("getLogs failed (fail-closed): " + e.message));
writeCursorAtomic(to); // advance FIRST to what we scanned: a crash here loses one window (honest under-count), never double-counts already-cursored blocks
if (!(earned > 0)) { console.log(`record-earn: no EXTERNAL income in blocks ${cursor + 1}..${to} (self-transfers ignored) — nothing recorded`); process.exit(0); }

const row = { ts: Math.floor(Date.now() / 1000), wallet, source, task, earn_usdc: earned, cost_usdc: cost, net_usdc: +(earned - cost).toFixed(6), wake, from_block: cursor + 1, to_block: to };
fs.mkdirSync(path.dirname(LEDGER), { recursive: true });
fs.appendFileSync(LEDGER, JSON.stringify(row) + "\n");
console.log(`record-earn: VERIFIED +${earned} USDC from EXTERNAL payer(s) (${source}) -> ${LEDGER}`);
