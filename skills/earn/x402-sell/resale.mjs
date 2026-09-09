// resale.mjs — PROD-2: the store's first RESALE product (StableEnrich model). Unlike every other
// route in serve-v2.mjs (deterministic compute, $0 marginal cost), /web-search spends THIS store's
// own wallet against an external x402 upstream (Exa's paid search API) and resells the result at a
// markup. Money-safety is the entire point of this file: every guard below runs BEFORE we ever pay
// the upstream, and the upstream call itself is wrapped so a failed resale can never leave us
// having paid Exa but not delivered (or vice versa: charged the buyer but never even tried Exa).
//
// SETTLE-ON-ERROR AUDIT (money-safety, cited not guessed — @x402/express@2.17.0, this repo's
// installed node_modules/@x402/express/dist/cjs/index.js):
//   - The paid-route branch (`case "payment-verified"`, index.js:232-367) buffers everything the
//     handler writes (res.writeHead/write/end/flushHeaders are all monkey-patched to buffer instead
//     of flush — index.js:251-279) and only decides whether to settle AFTER the handler finishes
//     (`await endPromise`, index.js:291).
//   - index.js:292-309: `if (res.statusCode >= 400) { await cancellationDispatcher.cancel({reason:
//     "handler_failed", responseStatus: res.statusCode}); ...; return; }` — this branch returns
//     WITHOUT ever calling `httpServer.processSettlement(...)`. The buffered response is replayed
//     to the real client via the ORIGINAL (unpatched) write methods, but the buyer's payment is
//     never captured.
//   - `httpServer.processSettlement(...)` (the only call that can settle) is reached exclusively
//     from the branch AFTER that check, i.e. only when `res.statusCode < 400` (index.js:310-343).
//   => CONCLUSION: a handler that responds 5xx (or any >=400) is settlement-safe BY CONSTRUCTION —
//   no buyer payment is captured. No extra "don't settle" plumbing is needed; the guards below only
//   need to make sure they always emit a non-2xx status BEFORE ever attempting to pay upstream, and
//   never emit 200 unless the upstream call actually succeeded. That is exactly what this file does.
//
// Margin (this session, upstreamMaxUsd is the worst-case guard ceiling, not the typical price):
//   price $0.014 - upstreamMaxUsd $0.011 = $0.003/call gross margin (21.4% of price, 27.3% markup
//   over worst-case cost). Actual margin is usually higher since upstreamMaxUsd is a ceiling, not
//   Exa's real price — the challenge guard (step c below) reads Exa's ACTUAL asking price every call.
import { wrapFetchWithPaymentFromConfig } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join, dirname as pdirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadEvmKey } from "../lib/resolve-identity.mjs";
import { evmErc20Balance, EVM_TOKENS, RPC } from "../lib/net-worth.mjs";
import {
  floatGuardTripped, rolloverSpendState, dailyCapTripped, recordSpend,
  decodeChallengeHeader, extractChallengeMaxUsd, challengeGuardTripped,
} from "./lib/resale-guards.mjs";
import { X402_STATE_DIR } from "./state-paths.mjs";

const UPSTREAM_URL = "https://api.exa.ai/search";
const STATE_DIR = X402_STATE_DIR;
const DEFAULT_STATE_PATH = join(STATE_DIR, "resale-spend.json");
const NETWORK = process.env.BUY_NETWORK || "eip155:8453"; // Base mainnet CAIP-2

export const RESALE_PRODUCTS = [
  {
    path: "/web-search", price: "$0.014", upstreamMaxUsd: 0.011, what: "live web search (Exa resale)",
    example: "/web-search?q=<query>&numResults=3",
    description: "Live web search results via the Exa API, paid per call by THIS store's own wallet over x402 so the buyer needs no Exa account or API key. GET /web-search?q=<query>&numResults=1..5. Deterministic pass-through of a live source.",
  },
];

// ---------------------------------------------------------------------------------------------
// default I/O implementations (overridable via makeResaleHandler(deps) for tests — no network,
// no real fs, no real payment in __tests__/resale.test.mjs)
// ---------------------------------------------------------------------------------------------
async function defaultGetBalanceUsd() {
  const pk = loadEvmKey();
  if (!pk) return 0; // fail-closed: no resolvable key -> treat as no float, guard trips.
  const address = privateKeyToAccount(pk).address;
  return evmErc20Balance("base", EVM_TOKENS.base[0], address, fetch, RPC.base);
}

function defaultReadState(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null; // missing/malformed -> caller rolls over to a fresh ledger
  }
}

function defaultWriteState(path, state) {
  mkdirSync(pdirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(state));
}

function buildDefaultPayingFetch() {
  const pk = loadEvmKey();
  if (!pk) return null;
  const account = privateKeyToAccount(pk);
  return wrapFetchWithPaymentFromConfig(fetch, { schemes: [{ network: NETWORK, client: new ExactEvmScheme(account) }] });
}

// Trim Exa's raw response to the fields a buyer actually wants — never pass through Exa's own
// account/billing metadata, only the search content.
function trimUpstreamResult(json) {
  const results = Array.isArray(json?.results) ? json.results : [];
  return results.map((r) => ({
    title: r?.title ?? null,
    url: r?.url ?? null,
    publishedDate: r?.publishedDate ?? null,
    author: r?.author ?? null,
    text: typeof r?.text === "string" ? r.text.slice(0, 2000) : null,
  }));
}

/**
 * Build a resaleHandler for GET /web-search. All I/O is injectable (deps) so the handler can be
 * unit-tested with fakes; serve-v2.mjs uses the zero-arg default export below, which wires the
 * real wallet, real fs state, and a real (payment-capable) fetch.
 */
export function makeResaleHandler(deps = {}) {
  const {
    getBalanceUsd = defaultGetBalanceUsd,
    statePath = DEFAULT_STATE_PATH,
    readState = defaultReadState,
    writeState = defaultWriteState,
    bareFetch = fetch,
    payingFetch = null, // if not supplied, built lazily from this instance's own key on first call
    minFloatUsd = 0.5,
    dailyCapUsd = Number(process.env.RESALE_DAILY_CAP_USD || 1.0),
    upstreamUrl = UPSTREAM_URL,
    upstreamMaxUsd = RESALE_PRODUCTS[0].upstreamMaxUsd,
    now = () => new Date(),
  } = deps;

  let payFetch = payingFetch;

  return async function resaleHandler(req, res) {
    const q = String(req.query?.q || "").slice(0, 500);
    if (!q) return res.status(400).json({ error: "pass ?q=<search query>" });
    const numResults = Math.max(1, Math.min(5, parseInt(req.query?.numResults, 10) || 3));

    // a) FLOAT GUARD — refuse before any upstream call.
    const balanceUsd = await getBalanceUsd();
    if (floatGuardTripped(balanceUsd, minFloatUsd)) {
      return res.status(503).json({ error: "resale paused: low float" });
    }

    // b) DAILY CAP GUARD.
    const today = now();
    const state = rolloverSpendState(readState(statePath), today.toISOString().slice(0, 10));
    if (dailyCapTripped(state, dailyCapUsd)) {
      return res.status(503).json({ error: "resale paused: daily upstream cap reached" });
    }

    // c) CHALLENGE GUARD — bare (unpaid) request to read Exa's actual asking price before paying.
    const body = JSON.stringify({ query: q, numResults });
    let challengeResp;
    try {
      challengeResp = await bareFetch(upstreamUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body });
    } catch (e) {
      return res.status(502).json({ error: "upstream failed", detail: String(e?.message || e).slice(0, 200) });
    }
    const challengeHeader = challengeResp.headers?.get?.("payment-required") || challengeResp.headers?.get?.("PAYMENT-REQUIRED");
    const challenge = decodeChallengeHeader(challengeHeader);
    const maxUsd = extractChallengeMaxUsd(challenge, { network: NETWORK });
    if (challengeGuardTripped(maxUsd, upstreamMaxUsd)) {
      return res.status(503).json({ error: "upstream price above guard" });
    }

    // d) PAY + FETCH. Only reachable once every guard above has cleared.
    if (!payFetch) payFetch = buildDefaultPayingFetch();
    if (!payFetch) return res.status(503).json({ error: "resale paused: no per-instance signing key resolvable" });
    let resp;
    try {
      resp = await payFetch(upstreamUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body });
    } catch (e) {
      return res.status(502).json({ error: "upstream failed", detail: String(e?.message || e).slice(0, 200) });
    }
    if (resp.status !== 200) {
      return res.status(502).json({ error: "upstream failed", detail: `upstream status ${resp.status}` });
    }
    const json = await resp.json();

    // spend accounting: prefer the actual settled amount off PAYMENT-RESPONSE (SettleResponse.amount,
    // atomic USDC units — node_modules/@x402/core/dist/cjs/http/index.js:963-966), else fall back to
    // the guard ceiling as a conservative (over-)estimate so we never under-count real spend.
    const settleHeader = resp.headers?.get?.("payment-response") || resp.headers?.get?.("PAYMENT-RESPONSE");
    let paidUsd = upstreamMaxUsd;
    if (settleHeader) {
      try {
        const settle = JSON.parse(Buffer.from(settleHeader, "base64").toString("utf8"));
        if (typeof settle?.amount === "string" && /^\d+$/.test(settle.amount)) paidUsd = Number(BigInt(settle.amount)) / 1e6;
      } catch { /* keep the conservative estimate */ }
    }
    writeState(statePath, recordSpend(state, paidUsd));

    return res.json({ query: q, results: trimUpstreamResult(json), source: "exa" });
  };
}

export const resaleHandler = makeResaleHandler();
