"use strict";

const assert = require("node:assert/strict");
const { mkdtempSync, readFileSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const test = require("node:test");

const {
  USDC_ADDRESS,
  TRANSFER_TOPIC,
  findLedgerPaths,
  processLedgers,
  the402EvidenceClassifier,
  main,
} = require("./record-x402-sales.js");

const PAY_TO = "0x810f6d61f7606deee2657d3083e150a222bc29c5";
const PAYER = "0x1111111111111111111111111111111111111111";
const TX_INITIATOR = "0x3333333333333333333333333333333333333333";
const TX = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const BLOCK = 34123456;

function sale(overrides = {}) {
  return {
    source: "the402",
    source_sale_id: "the402:order-42",
    offer_id: "forecast/weekly",
    tx: TX,
    block: BLOCK,
    from: PAYER,
    to: PAY_TO,
    payTo: PAY_TO,
    usdc: 0.5,
    usdc_atomic: "500000",
    finalized: true,
    status: "success",
    external: true,
    observed_at: "2026-07-27T13:31:59.000Z",
    ...overrides,
  };
}

function topicAddress(value) {
  return `0x${value.slice(2).padStart(64, "0")}`;
}

function rpcFixture(overrides = {}) {
  const responses = {
    eth_chainId: "0x2105",
    eth_getBlockByNumber: { number: `0x${(BLOCK + 20).toString(16)}` },
    eth_getTransactionReceipt: {
      status: "0x1",
      transactionHash: TX,
      blockNumber: `0x${BLOCK.toString(16)}`,
      logs: [{
        address: USDC_ADDRESS,
        transactionHash: TX,
        topics: [TRANSFER_TOPIC, topicAddress(PAYER), topicAddress(PAY_TO)],
        data: "0x7a120",
      }],
    },
    eth_getTransactionByHash: { hash: TX, from: TX_INITIATOR },
    ...overrides,
  };
  const calls = [];
  return {
    calls,
    rpcCall: async (method) => {
      calls.push(method);
      return responses[method];
    },
  };
}

function ledger(lines) {
  const dir = mkdtempSync(join(tmpdir(), "lm-x402-"));
  const path = join(dir, `external-inflows-${PAY_TO}.jsonl`);
  writeFileSync(path, `${lines.join("\n")}${lines.length ? "\n" : ""}`);
  return { dir, path };
}

test("an empty state directory is a healthy zero-row run with no RPC or write", async () => {
  const dir = mkdtempSync(join(tmpdir(), "lm-x402-empty-"));
  const writes = [];
  const rpc = rpcFixture();
  const result = await processLedgers({
    ledgerPaths: findLedgerPaths(dir),
    selfWallets: [PAY_TO],
    rpcCall: rpc.rpcCall,
    recordSale: async (row) => writes.push(row),
  });

  assert.deepEqual(result, {
    ledgers_seen: 0,
    lines_seen: 0,
    invalid: 0,
    chain_rejected: 0,
    blocked_subcent: 0,
    recorded: 0,
    duplicates: 0,
    sale_recorded: 0,
    work_recorded: 0,
    provenance_rejected: 0,
    transactions: [],
  });
  assert.equal(rpc.calls.length, 0);
  assert.equal(writes.length, 0);
});

test("a finalized exact USDC transfer is reverified and delegated once", async () => {
  const source = ledger([JSON.stringify(sale())]);
  const rpc = rpcFixture();
  const writes = [];
  const result = await processLedgers({
    ledgerPaths: [source.path],
    selfWallets: [PAY_TO],
    rpcCall: rpc.rpcCall,
    recordSale: async (row, options) => {
      writes.push({ row, options });
      return { ok: true, duplicate: false, entry_key: `x402:${row.tx}:income` };
    },
  });

  assert.equal(result.recorded, 1);
  assert.equal(result.duplicates, 0);
  assert.deepEqual(result.transactions, [TX]);
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].options.ownedPayTos, [PAY_TO]);
  assert.deepEqual(rpc.calls, [
    "eth_chainId",
    "eth_getBlockByNumber",
    "eth_getTransactionReceipt",
    "eth_getTransactionByHash",
  ]);
});

test("a proven The402 job is recorded through the work recipe, never the sale recipe", async () => {
  const source = ledger([JSON.stringify(sale({
    source_sale_id: "the402:settlement_42",
    offer_id: "svc_research",
  }))]);
  const saleWrites = [];
  const workWrites = [];
  const result = await processLedgers({
    ledgerPaths: [source.path],
    selfWallets: [PAY_TO],
    rpcCall: rpcFixture().rpcCall,
    classifyRevenue: () => ({
      kind: "work",
      settlementId: "settlement_42",
      jobId: "job_42",
      postingId: "post_42",
    }),
    recordSale: async (row) => saleWrites.push(row),
    recordWork: async (row, provenance) => {
      workWrites.push({ row, provenance });
      return { ok: true, duplicate: false, entry_key: `x402:${row.tx}:income` };
    },
  });

  assert.equal(result.recorded, 1);
  assert.equal(result.work_recorded, 1);
  assert.equal(result.sale_recorded, 0);
  assert.equal(result.provenance_rejected, 0);
  assert.equal(saleWrites.length, 0);
  assert.equal(workWrites.length, 1);
  assert.equal(workWrites[0].provenance.jobId, "job_42");
});

test("ambiguous The402 provenance blocks both work and sale writes", async () => {
  const source = ledger([JSON.stringify(sale())]);
  let writes = 0;
  const result = await processLedgers({
    ledgerPaths: [source.path],
    selfWallets: [PAY_TO],
    rpcCall: rpcFixture().rpcCall,
    classifyRevenue: () => ({ kind: "rejected", reason: "job_mismatch" }),
    recordSale: async () => { writes += 1; },
    recordWork: async () => { writes += 1; },
  });

  assert.equal(result.recorded, 0);
  assert.equal(result.provenance_rejected, 1);
  assert.equal(writes, 0);
});

test("The402 evidence is fetched once, bounded, and reused by the classifier", async () => {
  const dir = mkdtempSync(join(tmpdir(), "lm-the402-evidence-"));
  const credentialsPath = join(dir, "credentials.json");
  writeFileSync(credentialsPath, JSON.stringify({ api_key: "sk_test_agent_only_123" }));
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, apiKey: init.headers["X-API-Key"] });
    if (url.endsWith("/v1/provider/earnings")) {
      return Response.json({
        recent_settlements: [{
          settlement_id: "settlement_42",
          status: "settled",
          tx_hash: TX,
          provider_amount_usd: "0.50",
          service_id: "svc_research",
        }],
      });
    }
    return Response.json({ jobs: [] });
  };
  const classify = the402EvidenceClassifier({ fetchImpl, credentialsPath });
  const row = sale({
    source_sale_id: "the402:settlement_42",
    offer_id: "svc_research",
  });
  const first = await classify(row);
  const second = await classify(row);

  assert.equal(first.kind, "sale");
  assert.deepEqual(second, first);
  assert.equal(calls.length, 2);
  assert.ok(calls.every((call) => call.apiKey === "sk_test_agent_only_123"));
});

test("The402 API or credential failure rejects provenance without throwing or writing", async () => {
  const classify = the402EvidenceClassifier({
    fetchImpl: async () => new Response("unavailable", { status: 503 }),
    credentialsPath: join(tmpdir(), "missing-the402-credentials.json"),
  });
  assert.deepEqual(await classify(sale()), {
    kind: "rejected",
    reason: "provenance_unavailable",
  });
});

test("malformed rows and fractional-cent sales never touch chain or database", async () => {
  const source = ledger([
    "{bad-json",
    JSON.stringify(sale({ usdc: 0.001, usdc_atomic: "1000" })),
  ]);
  const rpc = rpcFixture();
  let writes = 0;
  const result = await processLedgers({
    ledgerPaths: [source.path],
    selfWallets: [PAY_TO],
    rpcCall: rpc.rpcCall,
    recordSale: async () => {
      writes += 1;
      return { ok: true, duplicate: false };
    },
  });

  assert.equal(result.lines_seen, 2);
  assert.equal(result.invalid, 1);
  assert.equal(result.blocked_subcent, 1);
  assert.equal(rpc.calls.length, 0);
  assert.equal(writes, 0);
});

test("wrong chain, pending receipt, transfer mismatch, and self initiator fail closed", async () => {
  const cases = [
    { eth_chainId: "0x89" },
    { eth_getBlockByNumber: { number: `0x${(BLOCK - 1).toString(16)}` } },
    {
      eth_getTransactionReceipt: {
        status: "0x1",
        transactionHash: TX,
        blockNumber: `0x${BLOCK.toString(16)}`,
        logs: [],
      },
    },
    { eth_getTransactionByHash: { hash: TX, from: PAY_TO } },
  ];

  for (const override of cases) {
    const source = ledger([JSON.stringify(sale())]);
    let writes = 0;
    const result = await processLedgers({
      ledgerPaths: [source.path],
      selfWallets: [PAY_TO],
      rpcCall: rpcFixture(override).rpcCall,
      recordSale: async () => {
        writes += 1;
        return { ok: true };
      },
    });
    assert.equal(result.chain_rejected, 1);
    assert.equal(writes, 0);
  }
});

test("store idempotency is exposed as a duplicate, not new revenue", async () => {
  const source = ledger([JSON.stringify(sale())]);
  const result = await processLedgers({
    ledgerPaths: [source.path],
    selfWallets: [PAY_TO],
    rpcCall: rpcFixture().rpcCall,
    recordSale: async () => ({
      ok: true,
      duplicate: true,
      entry_key: `x402:${TX}:income`,
    }),
  });

  assert.equal(result.recorded, 0);
  assert.equal(result.duplicates, 1);
  assert.deepEqual(result.transactions, [TX]);
});

test("default owner writes verified x402 revenue to the common FinancialRecord store", async () => {
  const source = ledger([JSON.stringify(sale())]);
  const appended = [];
  const result = await main({
    stateDir: source.dir, selfWallets: [PAY_TO], rpcCall: rpcFixture().rpcCall,
    classifyRevenue: async () => ({ kind: "sale" }), subjectId: "tenant-a",
    financialStore: { append: async (record) => { appended.push(record); return { created: true, record }; } },
    writeOutput: () => {},
  }, []);
  assert.equal(result.recorded, 1);
  assert.deepEqual(appended.map((row) => [row.subject_id, row.kind, row.amount_minor, row.currency]), [
    ["tenant-a", "business_revenue", 500000, "USDC"],
  ]);
});

test("ledger receiver is bound by its canonical external-inflows filename", async () => {
  const source = ledger([JSON.stringify(sale({ payTo: PAYER, to: PAYER }))]);
  let writes = 0;
  const result = await processLedgers({
    ledgerPaths: [source.path],
    selfWallets: [PAY_TO],
    rpcCall: rpcFixture().rpcCall,
    recordSale: async () => {
      writes += 1;
      return { ok: true };
    },
  });

  assert.equal(result.invalid, 1);
  assert.equal(writes, 0);
});

test("launchd wiring uses absolute executables, a bounded timeout, and five-minute cadence", () => {
  const boot = readFileSync(join(__dirname, "x402-sale-ledger-boot.sh"), "utf8");
  const installer = readFileSync(join(__dirname, "install-x402-sale-ledger-launchd.sh"), "utf8");
  const plist = readFileSync(
    join(__dirname, "..", "launchd", "ai.anicca.life-manager-x402-ledger.plist.template"),
    "utf8",
  );

  assert.match(boot, /\/opt\/homebrew\/bin\/timeout 240 \/opt\/homebrew\/bin\/node/);
  assert.doesNotMatch(boot, /(?:^|[;&|]\s*)timeout\s/);
  assert.match(boot, /LIFE_MANAGER_ENV_FILE:-\$\{HOME\}\/\.local\/state\/life-manager\/\.env/);
  assert.doesNotMatch(boot, /\.openclaw/);
  assert.match(boot, /X402_STATE_DIR.*\.local\/state\/life-manager\/x402-sell/);
  assert.match(boot, /REPO_ROOT.*skills\/earn\/x402-sell\/lib\/self-wallets\.mjs/);
  assert.match(boot, /LM_FINANCIAL_RECORDS_DIR/);
  assert.doesNotMatch(boot, /\$\{HOME\}\/anicca/);
  assert.match(plist, /<string>\/bin\/bash<\/string>/);
  assert.match(plist, /<key>StartInterval<\/key>\s*<integer>300<\/integer>/);
  assert.match(plist, /life-manager-x402-ledger\.out\.log/);
  assert.match(installer, /plutil -lint/);
  assert.match(installer, /launchctl bootstrap/);
  assert.match(installer, /launchctl enable/);
  assert.match(installer, /launchctl print[^|]+\|\s*\/usr\/bin\/grep/);
  assert.doesNotMatch(installer, /launchctl print "\$DOMAIN\/\$LABEL"\s*$/m);
});

test("x402 producers and observers share external Life Manager state", () => {
  const skill = join(__dirname, "..", "..", "..", "skills", "earn", "x402-sell");
  const runtimeEnv = readFileSync(join(skill, "runtime-env.sh"), "utf8");
  const watcher = readFileSync(join(skill, "watch-inflow.sh"), "utf8");
  const serve = readFileSync(join(skill, "serve.mjs"), "utf8");
  const serveV2 = readFileSync(join(skill, "serve-v2.mjs"), "utf8");
  const statePaths = readFileSync(join(skill, "state-paths.mjs"), "utf8");
  assert.match(runtimeEnv, /X402_STATE_DIR.*\.local\/state\/life-manager\/x402-sell/);
  assert.match(watcher, /source "\$DIR\/runtime-env\.sh"/);
  assert.match(watcher, /\$X402_STATE_DIR\/logs/);
  assert.doesNotMatch(watcher, /\.openclaw/);
  assert.match(serve, /resolveX402StateDir/);
  assert.match(serveV2, /resolveX402StateDir/);
  assert.match(statePaths, /env\.X402_STATE_DIR/);
  assert.match(statePaths, /env\.LIFE_MANAGER_STATE_ROOT/);
  assert.match(statePaths, /\.local.*state.*life-manager.*x402-sell/s);
});
