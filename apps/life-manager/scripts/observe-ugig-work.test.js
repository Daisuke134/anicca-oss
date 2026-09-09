"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");

const { main } = require("./observe-ugig-work.js");

const APPLICATION_ID = "5e315cfd-33fc-433b-a5f0-3cfcdc27a9a4";
const GIG_ID = "2b410cad-7cc9-44fd-b2f1-843d9eae6c24";
const WALLET = "71FfqFniYoMsWZb1qFeQDb1fk2xqvajzivpsnMb44gTf";
const PAYER = "9xQeWvG816bUx9EPfEz3Tq9FZzY5hNhWZQpLJQhY7G6e";
const SIGNATURE = "4".repeat(88);

test("live-shaped pending application produces a truthful zero-mutation result", async () => {
  const requests = [];
  let output = "";
  const result = await main({
    apiKey: "ugig_live_test",
    deliveries: [{
      application_id: APPLICATION_ID,
      gig_id: GIG_ID,
      amount_usd: 1,
      payment_currency: "sol",
      merchant_wallet_address: "71FfqFniYoMsWZb1qFeQDb1fk2xqvajzivpsnMb44gTf",
      category: "code",
      pr_links: ["https://github.com/profullstack/aiornot.vote/pull/100"],
      description: "RSS enclosure MIME fix",
    }],
    fetchImpl: async (url, init = {}) => {
      requests.push({ url, init });
      return {
        ok: true,
        status: 200,
        async text() {
          return JSON.stringify({
            applications: [{ id: APPLICATION_ID, gig_id: GIG_ID, status: "pending" }],
          });
        },
      };
    },
    now: () => new Date("2026-07-28T09:00:00.000Z"),
    writeOutput: (text) => { output += text; },
  });

  assert.equal(result.pending, 1);
  assert.equal(result.invoice_created, 0);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "https://ugig.net/api/applications/my");
  assert.equal(requests[0].init.headers.authorization, "Bearer ugig_live_test");
  assert.doesNotMatch(output, /ugig_live_test/);
});

test("CoinPay setup-required response is a truthful no-effect observation", async () => {
  let output = "";
  const requests = [];
  const result = await main({
    apiKey: "ugig_live_test",
    deliveries: [{
      application_id: APPLICATION_ID,
      gig_id: GIG_ID,
      amount_usd: 1,
      payment_currency: "sol",
      merchant_wallet_address: WALLET,
      category: "code",
      pr_links: ["https://github.com/profullstack/aiornot.vote/pull/100"],
      description: "RSS enclosure MIME fix",
    }],
    fetchImpl: async (url, init = {}) => {
      requests.push({ url: String(url), method: init.method || "GET" });
      let body;
      let status = 200;
      if (String(url).endsWith("/api/applications/my")) {
        body = { applications: [{ id: APPLICATION_ID, gig_id: GIG_ID, status: "accepted" }] };
      } else if (String(url).endsWith(`/api/gigs/${GIG_ID}/invoice`) && !init.method) {
        body = { data: [] };
      } else if (String(url).endsWith(`/api/gigs/${GIG_ID}/invoice`) && init.method === "POST") {
        status = 409;
        body = {
          error: "Connect your CoinPay account before sending an invoice",
          oauth_required: true,
          setup_required: true,
          setup_instructions: ["provider-owned instructions"],
        };
      } else if (String(url).startsWith("https://api.github.com/")) {
        body = { merged_at: "2026-07-28T08:00:00.000Z" };
      } else {
        throw new Error(`unexpected request ${url}`);
      }
      return {
        ok: status >= 200 && status < 300,
        status,
        async text() { return JSON.stringify(body); },
      };
    },
    writeOutput: (text) => { output += text; },
  });

  assert.equal(result.setup_required, 1);
  assert.equal(result.invoice_created, 0);
  assert.equal(requests.filter((request) => request.method === "POST").length, 1);
  assert.match(output, /"setup_required":1/);
  assert.doesNotMatch(output, /CoinPay|provider-owned instructions|ugig_live_test/);
});

test("the registry alone owns the separate five-minute UGig loop", () => {
  const root = join(__dirname, "..");
  const boot = readFileSync(join(__dirname, "ugig-invoice-observer-boot.sh"), "utf8");
  const registry = JSON.parse(readFileSync(join(root, "..", "..", "config", "loop-registry.json"), "utf8"));
  const loop = registry.loops["life-manager-ugig-invoice-observer"];

  assert.match(boot, /observe-ugig-work\.js/);
  assert.match(boot, /UGIG_API_KEY_FILE/);
  assert.match(boot, /timeout 180/);
  assert.equal(loop.adapter, "exec");
  assert.deepEqual(loop.command, []);
  assert.equal(loop.entrypoint, "apps/life-manager/scripts/ugig-invoice-observer-boot.sh");
  assert.deepEqual(loop.cadence, { start_interval_seconds: 300 });
  assert.equal(existsSync(join(__dirname, "install-ugig-invoice-observer-launchd.sh")), false);
  assert.equal(existsSync(join(root, "launchd", "ai.anicca.life-manager-ugig-invoice-observer.plist.template")), false);
});

test("a completed paid application is finalized through Solana RPC before one ledger write", async () => {
  const requests = [];
  const writes = [];
  const result = await main({
    apiKey: "ugig_live_test",
    solanaRpcUrl: "https://solana.example",
    deliveries: [{
      application_id: APPLICATION_ID,
      gig_id: GIG_ID,
      amount_usd: 1,
      payment_currency: "sol",
      merchant_wallet_address: WALLET,
      category: "code",
      pr_links: ["https://github.com/profullstack/aiornot.vote/pull/100"],
      description: "RSS enclosure MIME fix",
    }],
    fetchImpl: async (url, init = {}) => {
      requests.push({ url: String(url), init });
      let body;
      if (String(url).endsWith("/api/applications/my")) {
        body = { applications: [{ id: APPLICATION_ID, gig_id: GIG_ID, status: "completed" }] };
      } else if (String(url).endsWith(`/api/gigs/${GIG_ID}/invoice`)) {
        body = { invoices: [{
          id: "invoice-live-shaped",
          application_id: APPLICATION_ID,
          amount_usd: 1,
          currency: "USD",
          status: "paid",
          metadata: {
            merchant_tx_hash: SIGNATURE,
            paid_at: "2026-07-28T10:00:00.000Z",
            settlement_chain: "solana",
            payment_currency: "USD",
            receiver_payment_currency: "sol",
            merchant_wallet_address: WALLET,
            amount_crypto: "0.005",
          },
        }] };
      } else if (String(url) === "https://solana.example") {
        const rpc = JSON.parse(init.body);
        body = rpc.method === "getSignatureStatuses"
          ? { jsonrpc: "2.0", id: 1, result: {
            value: [{ slot: 351000000, err: null, confirmationStatus: "finalized" }],
          } }
          : { jsonrpc: "2.0", id: 1, result: {
            slot: 351000000,
            blockTime: 1785232800,
            transaction: {
              signatures: [SIGNATURE],
              message: { accountKeys: [PAYER, WALLET] },
            },
            meta: {
              err: null,
              preBalances: [1_000_000_000, 10_000_000],
              postBalances: [994_995_000, 15_000_000],
            },
          } };
      } else {
        throw new Error(`unexpected request ${url}`);
      }
      return {
        ok: true,
        status: 200,
        async text() { return JSON.stringify(body); },
      };
    },
    recordEntry: async (row) => {
      writes.push(row);
      return { ok: true, duplicate: false, entry_key: row.entry_key };
    },
    writeOutput: () => {},
  });

  assert.equal(result.paid, 1);
  assert.equal(result.revenue_recorded, 1);
  assert.equal(result.revenue_duplicates, 0);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].tx_hash, SIGNATURE);
  assert.deepEqual(
    requests.filter((request) => request.url === "https://solana.example")
      .map((request) => JSON.parse(request.init.body).method),
    ["getSignatureStatuses", "getTransaction"],
  );
});
