/**
 * serve.mjs — the X402 PRODUCT pillar: a TOOL that turns any capability into a paid HTTP endpoint
 * that receives USDC (no buyer account, no API key — the HTTP 402 Payment Required flow).
 *
 * Per HARD RULE #0: this is a TOOL, not a product idea. It does NOT decide WHAT to sell or for HOW
 * MUCH — the MODEL decides that (via env / args) and decides how to create demand. Here we ship the
 * default "web research" product (powered by the Agent-Reach skill: read Twitter/Reddit/YouTube/GitHub
 * with $0 API fees) because at $0 compute, every sale is pure profit — but the model can point the
 * worker at anything it wants to sell.
 *
 * Demand is the GOAL, not a wall: building what people want + attracting buyers is the model's job
 * (list it, share it on socials/GitHub, price it right). This file just makes "get paid in USDC for
 * doing X over HTTP" a one-command primitive. Works local AND cloud (needs a public URL for real
 * buyers — the cloud anicca or a tunnel provides that).
 *
 * Env (the MODEL sets these):
 *   X402_PAYTO        receiving wallet (default: ~/.automaton/wallet.json address)
 *   X402_PRICE        price per request, e.g. "$0.05" (default "$0.02")
 *   X402_NETWORK      "base" (default) | "base-sepolia"
 *   X402_PORT         default 8403
 *   X402_PRODUCT_CMD  shell command template that PRODUCES the paid result; "{q}" is the buyer's query.
 *                     default = Agent-Reach web research. The model can override to sell anything.
 */
import express from "express";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { privateKeyToAccount } from "viem/accounts";
import { loadEvmKey } from "../lib/resolve-identity.mjs";
import {
  compoundInterest, mortgage, loanPayoff, roi, npv, irr, dcf, cagr, aprApy, breakEven,
  presentValue, futureValueAnnuity, savingsGoal, percentChange, inflationAdjust, positionSize,
  kelly, liquidationPrice, perpPnl, impermanentLoss, hashText, base64, timestamp,
  calcEval, flattenJson, dnsLookup, whois, stockQuote,
} from "./primitives.mjs";
import { getFundingRatesCached, buildFundingRatesResponse, annualizedBps } from "./funding-rates.mjs";
import { buildFundingRateArbResponse } from "./funding-rate-arb.mjs";
import { resolveX402StateDir } from "./state-paths.mjs";

function payTo() {
  if (process.env.X402_PAYTO) return process.env.X402_PAYTO;
  // #28: derive THIS instance's own payTo address from its gated per-instance key — never the shared
  // $HOME wallet's address (which would route another instance's earnings to the wrong wallet).
  const pk = loadEvmKey();
  if (pk) return privateKeyToAccount(pk).address;
  throw new Error("set X402_PAYTO (no per-instance EVM key resolvable)");
}

const PRICE = process.env.X402_PRICE || "$0.003";
const NETWORK = process.env.X402_NETWORK || "base";
const PORT = Number(process.env.X402_PORT || 8403);
// Public HTTPS origin the CDP Bazaar crawler will probe. MUST be the real reachable https:// URL:
// behind a TLS-terminating proxy (tailscale funnel / cloudflared) Express sees req.protocol="http",
// so x402-express would auto-derive an http:// resource URL that the crawler can't reach → never indexed
// (root cause 2026-07-14; cf coinbase/agentkit#877). Set X402_PUBLIC_URL to the https funnel origin.
const PUBLIC_URL = (process.env.X402_PUBLIC_URL || "").replace(/\/+$/, "");
// default product = $0 web-research digest via research-product.mjs (Wikipedia + HN + Jina, zero paid keys).
const PRODUCT_CMD = process.env.X402_PRODUCT_CMD ||
  `node ${new URL('./research-product.mjs', import.meta.url).pathname} {q}`;

const app = express();

// x402: protect the paid route. paymentMiddleware(payTo, routes, facilitator). Verified primitive
// (x402-express, prior session): payTo=wallet, $price USDC on Base, returns 402 until paid — no key
// needed to RECEIVE. Loaded dynamically so the skill installs the dep on first run.
const { paymentMiddleware } = await import("x402-express");
// CDP facilitator (when CDP keys present) → settles on Base mainnet AND lists the endpoint in the x402
// Bazaar discovery layer (how buyer agents FIND us). Falls back to the x402.org testnet facilitator when
// no CDP keys (generic install / dev). payTo stays our wallet — CDP only facilitates + catalogs, never custodies.
let facilitator = { url: "https://x402.org/facilitator" };
if (process.env.CDP_API_KEY_ID && process.env.CDP_API_KEY_SECRET) {
  const { createFacilitatorConfig } = await import("@coinbase/x402");
  facilitator = createFacilitatorConfig(process.env.CDP_API_KEY_ID, process.env.CDP_API_KEY_SECRET);
}
// single product table — drives the payment middleware, "/" index, /.well-known/x402.json and /llms.txt.
const PRODUCTS = [
  { path: "/research", price: PRICE, example: "/research?q=<topic>", what: "web research digest",
    description: "On-demand web research digest — free-source curated (Wikipedia + Hacker News + Jina Reader). GET /research?q=<topic>; pay per request in USDC on Base. Runs on any install, $0 source cost." },
  // deterministic compute primitives — pure CPU or free data, no LLM in the serving path.
  { path: "/compound-interest", price: "$0.001", example: "/compound-interest?principal=10000&rate=5&years=10&compoundsPerYear=12", what: "compound interest calc",
    description: "Compound interest calculator. GET /compound-interest?principal=10000&rate=5&years=10&compoundsPerYear=12 → final amount + interest earned. Deterministic JSON, instant." },
  { path: "/mortgage", price: "$0.001", example: "/mortgage?principal=300000&rate=6&years=30", what: "mortgage payment calc",
    description: "Mortgage payment and total interest calculator. GET /mortgage?principal=300000&rate=6&years=30 → monthlyPayment, totalPaid, totalInterest. Deterministic JSON, instant." },
  { path: "/loan-payoff", price: "$0.001", example: "/loan-payoff?balance=10000&rate=6&monthlyPayment=500", what: "loan payoff calc",
    description: "Loan payoff duration and interest calculator. GET /loan-payoff?balance=10000&rate=6&monthlyPayment=500 → months, totalInterest. Deterministic JSON, instant." },
  { path: "/roi", price: "$0.001", example: "/roi?initial=1000&final=1500&years=3", what: "ROI calc",
    description: "Total and optional annualized return on investment calculator. GET /roi?initial=1000&final=1500&years=3 → roiPercent, annualizedPercent. Deterministic JSON, instant." },
  { path: "/npv", price: "$0.001", example: "/npv?rate=10&cashflows=-1000,600,600", what: "net present value calc",
    description: "Net present value calculator for comma-separated cash flows starting at t0. GET /npv?rate=10&cashflows=-1000,600,600 → npv. Deterministic JSON, instant." },
  { path: "/irr", price: "$0.001", example: "/irr?cashflows=-1000,600,600", what: "internal rate of return calc",
    description: "Internal rate of return calculator with numerical fallback. GET /irr?cashflows=-1000,600,600 → irrPercent. Deterministic JSON, instant." },
  { path: "/dcf", price: "$0.001", example: "/dcf?fcf=100&growthRate=5&discountRate=10&years=5&terminalGrowth=2", what: "discounted cash flow calc",
    description: "Discounted cash flow valuation with Gordon terminal value. GET /dcf?fcf=100&growthRate=5&discountRate=10&years=5&terminalGrowth=2 → presentValue. Deterministic JSON, instant." },
  { path: "/cagr", price: "$0.001", example: "/cagr?start=100&end=200&years=5", what: "CAGR calc",
    description: "Compound annual growth rate calculator. GET /cagr?start=100&end=200&years=5 → cagrPercent. Deterministic JSON, instant." },
  { path: "/apr-apy", price: "$0.001", example: "/apr-apy?apr=12&compoundsPerYear=12", what: "APR APY converter",
    description: "APR and APY bidirectional converter. GET /apr-apy?apr=12&compoundsPerYear=12 → apr, apy. Deterministic JSON, instant." },
  { path: "/break-even", price: "$0.001", example: "/break-even?fixedCosts=1000&pricePerUnit=50&variableCostPerUnit=30", what: "break-even calc",
    description: "Break-even unit and revenue calculator. GET /break-even?fixedCosts=1000&pricePerUnit=50&variableCostPerUnit=30 → units, revenue. Deterministic JSON, instant." },
  { path: "/present-value", price: "$0.001", example: "/present-value?future=1000&rate=10&years=2", what: "present value calc",
    description: "Present value calculator for a future amount. GET /present-value?future=1000&rate=10&years=2 → presentValue. Deterministic JSON, instant." },
  { path: "/future-value-annuity", price: "$0.001", example: "/future-value-annuity?payment=100&rate=5&years=10&compoundsPerYear=12", what: "annuity future value calc",
    description: "Future value calculator for recurring end-of-period contributions. GET /future-value-annuity?payment=100&rate=5&years=10&compoundsPerYear=12 → futureValue, totalContributed, interestEarned. Deterministic JSON, instant." },
  { path: "/savings-goal", price: "$0.001", example: "/savings-goal?target=10000&rate=5&years=3&compoundsPerYear=12", what: "savings goal calc",
    description: "Required recurring contribution calculator for a savings target. GET /savings-goal?target=10000&rate=5&years=3&compoundsPerYear=12 → monthlyContribution. Deterministic JSON, instant." },
  { path: "/percent-change", price: "$0.001", example: "/percent-change?from=100&to=125", what: "percent change calc",
    description: "Percentage change calculator. GET /percent-change?from=100&to=125 → percentChange. Deterministic JSON, instant." },
  { path: "/inflation-adjust", price: "$0.001", example: "/inflation-adjust?amount=1000&rate=3&years=10", what: "inflation adjustment calc",
    description: "Inflation-adjusted nominal need and purchasing power calculator. GET /inflation-adjust?amount=1000&rate=3&years=10 → futureNominalNeeded, presentPurchasingPower. Deterministic JSON, instant." },
  { path: "/position-size", price: "$0.002", example: "/position-size?balance=10000&riskPercent=1&entry=100&stop=95", what: "trading position size calc",
    description: "Risk-based long or short position sizing calculator. GET /position-size?balance=10000&riskPercent=1&entry=100&stop=95 → positionSize, notional, direction. Deterministic JSON, instant." },
  { path: "/kelly", price: "$0.002", example: "/kelly?winProb=0.6&winLossRatio=2", what: "Kelly criterion calc",
    description: "Kelly criterion and half-Kelly sizing calculator. GET /kelly?winProb=0.6&winLossRatio=2 → kellyFraction, halfKelly. Deterministic JSON, instant." },
  { path: "/liquidation-price", price: "$0.002", example: "/liquidation-price?entry=100&leverage=10&side=long&maintenanceMarginPercent=0.5", what: "liquidation price calc",
    description: "Approximate leveraged long or short liquidation price calculator. GET /liquidation-price?entry=100&leverage=10&side=long&maintenanceMarginPercent=0.5 → liquidationPrice. Deterministic JSON, instant." },
  { path: "/perp-pnl", price: "$0.002", example: "/perp-pnl?entry=100&exit=110&size=2&side=long&leverage=5", what: "perpetual PnL calc",
    description: "Perpetual futures PnL, return, and optional leveraged ROE calculator. GET /perp-pnl?entry=100&exit=110&size=2&side=long&leverage=5 → pnl, pnlPercent, roePercent. Deterministic JSON, instant." },
  { path: "/impermanent-loss", price: "$0.002", example: "/impermanent-loss?priceRatio=4", what: "impermanent loss calc",
    description: "Constant-product AMM impermanent loss calculator. GET /impermanent-loss?priceRatio=4 → ilPercent. Deterministic JSON, instant." },
  { path: "/hash", price: "$0.001", example: "/hash?text=hello&algo=sha256", what: "text hash",
    description: "SHA-256, SHA-512, or MD5 text digest generator. GET /hash?text=hello&algo=sha256 → digest. Deterministic JSON, instant." },
  { path: "/base64", price: "$0.001", example: "/base64?text=hello", what: "Base64 codec",
    description: "Base64 UTF-8 encoder and validated decoder. GET /base64?text=hello or /base64?text=aGVsbG8%3D&decode=1 → result. Deterministic JSON, instant." },
  { path: "/timestamp", price: "$0.001", example: "/timestamp?value=1704067200", what: "timestamp converter",
    description: "Unix seconds, Unix milliseconds, and ISO 8601 timestamp converter. GET /timestamp?value=1704067200 → unixSeconds, unixMillis, iso, utc. Deterministic JSON, instant." },
  { path: "/calc", price: "$0.001", example: "/calc?expr=(2%2B3)*4", what: "expression evaluator",
    description: "Arithmetic expression evaluator (+ - * / % ^ and parentheses). GET /calc?expr=(2%2B3)*4 → {result}. Deterministic JSON, instant, no code execution." },
  { path: "/json-flatten", price: "$0.001", example: "/json-flatten?json=<url-encoded JSON>", what: "flatten nested JSON",
    description: "Flatten nested JSON to dot-notation key/value pairs. GET /json-flatten?json=<url-encoded JSON> → flat object. Deterministic, instant." },
  { path: "/dns-lookup", price: "$0.001", example: "/dns-lookup?domain=example.com&type=A", what: "DNS records",
    description: "DNS lookup. GET /dns-lookup?domain=example.com&type=A|AAAA|MX|TXT|NS|CNAME|SOA → records as JSON. Live authoritative data, instant." },
  { path: "/whois", price: "$0.002", example: "/whois?domain=example.com", what: "WHOIS lookup",
    description: "WHOIS lookup with IANA referral follow. GET /whois?domain=example.com → registrar whois text as JSON. Live registry data." },
  { path: "/stock-quote", price: "$0.003", example: "/stock-quote?symbol=AAPL", what: "stock quote",
    description: "Real-time stock quote (price, currency, previous close, exchange). GET /stock-quote?symbol=AAPL → JSON. Free-source market data." },
  { path: "/funding-rates", price: "$0.003", example: "/funding-rates?symbol=BTC", what: "cross-exchange perp funding rates",
    description: "Perp funding rates across Binance/Bybit/Hyperliquid, normalized to 8h-equivalent, plus cross-exchange divergence (annualized bps, top20 arbitrage signal). GET /funding-rates or /funding-rates?symbol=BTC → JSON. Free-source, 60s cache." },
  { path: "/funding-rate-arb", price: "$0.003", example: "/funding-rate-arb?symbol=BTC", what: "pairwise funding-rate arbitrage signal",
    description: "Every distinct Binance/Bybit/Hyperliquid exchange-pair funding-rate spread per symbol (not just the single highest-vs-lowest pair), sorted by annualized bps divergence descending, with short(high-funding venue)/long(low-funding venue) direction per pair. GET /funding-rate-arb (top20 across all symbols) or /funding-rate-arb?symbol=BTC (all pairs for that symbol) → JSON. Pure arithmetic on the same cached data as /funding-rates, $0 marginal cost, 60s cache." },
];

// per-route config: discoverable + explicit https resource (Bazaar crawler probes it; see PUBLIC_URL note).
const routes = Object.fromEntries(PRODUCTS.map((p) => [
  `GET ${p.path}`,
  { price: p.price, network: NETWORK,
    config: { description: p.description, discoverable: true, ...(PUBLIC_URL ? { resource: `${PUBLIC_URL}${p.path}` } : {}) } },
]));

// free discovery surfaces (mounted BEFORE the payment middleware so they are never paywalled):
// .well-known/x402.json manifest + llms.txt — how buyer agents and their crawlers find what's for sale.
const USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
app.get("/.well-known/x402.json", (_req, res) =>
  res.json({
    x402Version: 1,
    resources: PRODUCTS.map((p) => ({
      resource: PUBLIC_URL ? `${PUBLIC_URL}${p.path}` : p.path, method: "GET", price: p.price,
      network: NETWORK, payTo: payTo(), asset: USDC_BASE, description: p.description,
    })),
  }));
app.get("/llms.txt", (_req, res) =>
  res.type("text/plain").send([
    "# x402 paid API — pay-per-request in USDC on Base (HTTP 402 flow, no account, no API key)",
    `# payTo: ${payTo()}  |  manifest: ${PUBLIC_URL || ""}/.well-known/x402.json`,
    "",
    ...PRODUCTS.map((p) => `GET ${PUBLIC_URL || ""}${p.example}  (${p.price}) — ${p.description}`),
  ].join("\n")));

// GET-with-query param schemas per route, for the OpenAPI discovery doc below (x402scan requires
// an input schema per operation or the route is "non-invocable" and skipped from registration —
// see x402scan.com/discovery/spec). Query params only (no request body: all our routes are GET).
const QUERY_PARAMS = {
  "/research": [{ name: "q", required: true, type: "string", description: "topic to research" }],
  "/compound-interest": [
    { name: "principal", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "years", required: true, type: "number" }, { name: "compoundsPerYear", required: false, type: "number" },
  ],
  "/mortgage": [
    { name: "principal", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "years", required: true, type: "number" },
  ],
  "/loan-payoff": [
    { name: "balance", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "monthlyPayment", required: true, type: "number" },
  ],
  "/roi": [
    { name: "initial", required: true, type: "number" }, { name: "final", required: true, type: "number" },
    { name: "years", required: false, type: "number" },
  ],
  "/npv": [{ name: "rate", required: true, type: "number" }, { name: "cashflows", required: true, type: "string", description: "comma-separated cash flows, first value is t0" }],
  "/irr": [{ name: "cashflows", required: true, type: "string", description: "comma-separated cash flows, first value is t0" }],
  "/dcf": [
    { name: "fcf", required: true, type: "number" }, { name: "growthRate", required: true, type: "number" },
    { name: "discountRate", required: true, type: "number" }, { name: "years", required: true, type: "number" },
    { name: "terminalGrowth", required: true, type: "number" },
  ],
  "/cagr": [
    { name: "start", required: true, type: "number" }, { name: "end", required: true, type: "number" },
    { name: "years", required: true, type: "number" },
  ],
  "/apr-apy": [
    { name: "apr", required: false, type: "number", description: "provide exactly one of apr or apy" },
    { name: "apy", required: false, type: "number", description: "provide exactly one of apr or apy" },
    { name: "compoundsPerYear", required: false, type: "number" },
  ],
  "/break-even": [
    { name: "fixedCosts", required: true, type: "number" }, { name: "pricePerUnit", required: true, type: "number" },
    { name: "variableCostPerUnit", required: true, type: "number" },
  ],
  "/present-value": [
    { name: "future", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "years", required: true, type: "number" },
  ],
  "/future-value-annuity": [
    { name: "payment", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "years", required: true, type: "number" }, { name: "compoundsPerYear", required: false, type: "number" },
  ],
  "/savings-goal": [
    { name: "target", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "years", required: true, type: "number" }, { name: "compoundsPerYear", required: false, type: "number" },
  ],
  "/percent-change": [{ name: "from", required: true, type: "number" }, { name: "to", required: true, type: "number" }],
  "/inflation-adjust": [
    { name: "amount", required: true, type: "number" }, { name: "rate", required: true, type: "number" },
    { name: "years", required: true, type: "number" },
  ],
  "/position-size": [
    { name: "balance", required: true, type: "number" }, { name: "riskPercent", required: true, type: "number" },
    { name: "entry", required: true, type: "number" }, { name: "stop", required: true, type: "number" },
  ],
  "/kelly": [{ name: "winProb", required: true, type: "number" }, { name: "winLossRatio", required: true, type: "number" }],
  "/liquidation-price": [
    { name: "entry", required: true, type: "number" }, { name: "leverage", required: true, type: "number" },
    { name: "side", required: true, type: "string", description: "long or short" },
    { name: "maintenanceMarginPercent", required: false, type: "number" },
  ],
  "/perp-pnl": [
    { name: "entry", required: true, type: "number" }, { name: "exit", required: true, type: "number" },
    { name: "size", required: true, type: "number" }, { name: "side", required: true, type: "string", description: "long or short" },
    { name: "leverage", required: false, type: "number" },
  ],
  "/impermanent-loss": [{ name: "priceRatio", required: true, type: "number", description: "new price divided by old price" }],
  "/hash": [{ name: "text", required: true, type: "string" }, { name: "algo", required: false, type: "string", description: "sha256, sha512, or md5" }],
  "/base64": [{ name: "text", required: true, type: "string" }, { name: "decode", required: false, type: "string", description: "1 or true to decode" }],
  "/timestamp": [{ name: "value", required: true, type: "string", description: "unix seconds, unix millis, or ISO 8601" }],
  "/calc": [{ name: "expr", required: true, type: "string", description: "arithmetic expression" }],
  "/json-flatten": [{ name: "json", required: true, type: "string", description: "URL-encoded JSON" }],
  "/dns-lookup": [{ name: "domain", required: true, type: "string" }, { name: "type", required: false, type: "string" }],
  "/whois": [{ name: "domain", required: true, type: "string" }],
  "/stock-quote": [{ name: "symbol", required: true, type: "string" }],
  "/funding-rates": [{ name: "symbol", required: false, type: "string" }],
  "/funding-rate-arb": [{ name: "symbol", required: false, type: "string", description: "base asset, e.g. BTC — filters to all exchange pairs for that symbol; omit for top20 across all symbols" }],
};
// Discovery spec (x402scan.com/discovery/spec): canonical machine-readable contract for buyer
// agents; also required to pass SIWX-gated registration (POST /api/x402/registry/register-origin).
app.get("/openapi.json", (_req, res) =>
  res.json({
    openapi: "3.1.0",
    info: {
      title: "Anicca x402 seller", version: "1",
      "x-guidance": "Deterministic pay-per-call primitives + web research, priced in USDC on Base via x402. GET each path with its query params; a 402 challenge carries the payment requirements, pay it, retry with X-PAYMENT.",
    },
    paths: Object.fromEntries(PRODUCTS.map((p) => [p.path, {
      get: {
        operationId: p.path.slice(1).replace(/-([a-z])/g, (_, c) => c.toUpperCase()),
        summary: p.what, description: p.description,
        parameters: (QUERY_PARAMS[p.path] || []).map((q) => ({
          name: q.name, in: "query", required: q.required, description: q.description,
          schema: { type: q.type },
        })),
        "x-payment-info": { price: { mode: "fixed", currency: "USD", amount: p.price.replace("$", "") }, protocols: [{ x402: {} }] },
        responses: { 200: { description: "OK" }, 402: { description: "Payment Required" } },
      },
    }])),
  }));

app.use(paymentMiddleware(payTo(), routes, facilitator));

// sales log — INV-SETTLE: a request is only a SALE once settle() succeeds on-chain, never on
// verify alone. x402-express (node_modules/x402-express dist/cjs/index.js) buffers res.write/end
// after next() and only flushes them once `settled=true`; on settle SUCCESS it does
// res.setHeader("X-PAYMENT-RESPONSE", ...) immediately before that flush, on settle FAILURE/absence
// (statusCode>=400 short-circuit, or settle() throwing/returning success:false) it flushes a plain
// 402 with NO such header. So the X-PAYMENT-RESPONSE header, read at the res 'finish' event (after
// the real response left the process), is the only correct settle signal. The prior code logged on
// next() — i.e. right after verify() passed, BEFORE settle() ever ran — which recorded verify-only
// (often never-settled) requests as sales: root cause of franklin1's 21 payer:null false-positive
// "sales" (2026-07-17 root-cause session). Requests that clear verify but never settle still carry a
// real demand signal, so they go to attempts-<wallet>.jsonl instead of being silently dropped.
import { appendFileSync, mkdirSync } from "node:fs";
import { join, dirname as pdirname } from "node:path";
const STATE_DIR = resolveX402StateDir();
const SALES_LOG = process.env.X402_SALES_LOG || join(STATE_DIR, `sales-${payTo().toLowerCase()}.jsonl`);
const ATTEMPTS_LOG = process.env.X402_ATTEMPTS_LOG || join(STATE_DIR, `attempts-${payTo().toLowerCase()}.jsonl`);
try { mkdirSync(STATE_DIR, { recursive: true }); } catch { /* best-effort */ }
app.use((req, res, next) => {
  const product = PRODUCTS.find((p) => p.path === req.path);
  if (!product) return next();
  let payer = null;
  try {
    const xp = JSON.parse(Buffer.from(req.header("x-payment") || "", "base64").toString("utf8"));
    payer = xp?.payload?.authorization?.from || null;
  } catch { /* header absent/opaque — payer stays null, never blocks logging */ }
  res.on("finish", () => {
    const settled = !!res.getHeader("X-PAYMENT-RESPONSE");
    const line = JSON.stringify({ ts: new Date().toISOString(), route: req.path, price: product.price, payer, settled }) + "\n";
    try { appendFileSync(settled ? SALES_LOG : ATTEMPTS_LOG, line); } catch { /* logging must never break serving */ }
  });
  next();
});

// the paid endpoint: runs the product command with the buyer's query, returns the result.
app.get("/research", (req, res) => {
  const q = String(req.query.q || "").slice(0, 500);
  if (!q) return res.status(400).json({ error: "pass ?q=<what to research>" });
  const parts = PRODUCT_CMD.replace("{q}", JSON.stringify(q)).split(" ");
  execFile(parts[0], parts.slice(1), { timeout: 90_000, maxBuffer: 4 << 20 }, (err, stdout) => {
    if (err) return res.status(502).json({ error: "product failed", detail: String(err).slice(0, 200) });
    res.json({ query: q, result: stdout.slice(0, 200_000), paidTo: payTo(), price: PRICE });
  });
});

// primitive handlers: payment middleware above already gated them; here is pure compute.
const answer = (res, fn) =>
  Promise.resolve().then(fn).then((out) => res.json(out))
    .catch((e) => res.status(422).json({ error: String(e && e.message || e).slice(0, 200) }));

app.get("/compound-interest", (req, res) => answer(res, () => compoundInterest(req.query)));
app.get("/mortgage", (req, res) => answer(res, () => mortgage(req.query)));
app.get("/loan-payoff", (req, res) => answer(res, () => loanPayoff(req.query)));
app.get("/roi", (req, res) => answer(res, () => roi(req.query)));
app.get("/npv", (req, res) => answer(res, () => npv(req.query)));
app.get("/irr", (req, res) => answer(res, () => irr(req.query)));
app.get("/dcf", (req, res) => answer(res, () => dcf(req.query)));
app.get("/cagr", (req, res) => answer(res, () => cagr(req.query)));
app.get("/apr-apy", (req, res) => answer(res, () => aprApy(req.query)));
app.get("/break-even", (req, res) => answer(res, () => breakEven(req.query)));
app.get("/present-value", (req, res) => answer(res, () => presentValue(req.query)));
app.get("/future-value-annuity", (req, res) => answer(res, () => futureValueAnnuity(req.query)));
app.get("/savings-goal", (req, res) => answer(res, () => savingsGoal(req.query)));
app.get("/percent-change", (req, res) => answer(res, () => percentChange(req.query)));
app.get("/inflation-adjust", (req, res) => answer(res, () => inflationAdjust(req.query)));
app.get("/position-size", (req, res) => answer(res, () => positionSize(req.query)));
app.get("/kelly", (req, res) => answer(res, () => kelly(req.query)));
app.get("/liquidation-price", (req, res) => answer(res, () => liquidationPrice(req.query)));
app.get("/perp-pnl", (req, res) => answer(res, () => perpPnl(req.query)));
app.get("/impermanent-loss", (req, res) => answer(res, () => impermanentLoss(req.query)));
app.get("/hash", (req, res) => answer(res, () => hashText(req.query)));
app.get("/base64", (req, res) => answer(res, () => base64(req.query)));
app.get("/timestamp", (req, res) => answer(res, () => timestamp(req.query)));
app.get("/calc", (req, res) => answer(res, () => calcEval(String(req.query.expr || ""))));
app.get("/json-flatten", (req, res) => answer(res, () => flattenJson(JSON.parse(String(req.query.json || "")))));
app.get("/dns-lookup", (req, res) => answer(res, () => dnsLookup(String(req.query.domain || ""), String(req.query.type || "A"))));
app.get("/whois", (req, res) => answer(res, () => whois(String(req.query.domain || ""))));
app.get("/stock-quote", (req, res) => answer(res, () => stockQuote(String(req.query.symbol || ""))));

// funding-rates: unlike the other primitives, "failure" has two distinct meanings — a single
// exchange dying should DEGRADE (still serve the other exchanges, 200 + degraded:true), only ALL
// THREE dying is a real outage (503). getFundingRatesCached() throws only in that all-dead case.
app.get("/funding-rates", (req, res) => {
  getFundingRatesCached()
    .then(({ rows, errors }) => res.json(buildFundingRatesResponse(rows, { symbol: req.query.symbol, errors })))
    .catch((e) => res.status(503).json({ error: "all upstream exchanges unavailable", detail: String(e && e.message || e).slice(0, 300) }));
});

app.get("/funding-rate-arb", (req, res) => {
  getFundingRatesCached()
    .then(({ rows, errors }) => res.json(buildFundingRateArbResponse(rows, annualizedBps, { symbol: req.query.symbol, errors })))
    .catch((e) => res.status(503).json({ error: "all upstream exchanges unavailable", detail: String(e && e.message || e).slice(0, 300) }));
});

// free: tells a buyer what's for sale + the price (so demand can find it).
app.get("/", (_req, res) =>
  res.json({
    products: PRODUCTS.map((p) => ({ path: p.example, price: p.price, what: p.what })),
    manifest: "/.well-known/x402.json", llms: "/llms.txt",
    pay: "x402 — pay per request in USDC on " + NETWORK, payTo: payTo(),
  }));

const server = app.listen(PORT, () =>
  console.log(JSON.stringify({ x402_seller: "up", port: PORT, price: PRICE, network: NETWORK, payTo: payTo() })));

// Without this, losing the port race printed "up" and exited 0 — so run.sh's UP check, the ledger
// narrate, and launchd's exit code all read a dead seller as a healthy one, and KeepAlive respawned
// it 364 times without ever flagging a fault. A seller that cannot bind must say so and exit nonzero.
server.on("error", (err) => {
  console.error(JSON.stringify({ x402_seller: "failed", port: PORT, error: err.code || String(err) }));
  process.exit(1);
});
