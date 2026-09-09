"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  claimFinancialReceipt,
  dueReportKinds,
  formatUsdMicros,
  readCostLedger,
  readFinancialCostTotals,
  readFinancialTenant,
  renderFinancialReport,
  runFinancialReport,
  snapshotHash,
} = require("./financial-report-runtime.js");

const WALLET = "0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad";
const SUNDAY_2004_JST = Date.parse("2026-08-02T11:04:00.000Z");
const SUNDAY_2005_JST = Date.parse("2026-08-02T11:05:00.000Z");

function tenant(overrides = {}) {
  return {
    uid: "u1",
    telegram_chat_id: "chat-1",
    agent_wallet_address: WALLET,
    notifications_enabled: true,
    call_time_zone: "Asia/Tokyo",
    ...overrides,
  };
}

function earning(kind, amountMinor, overrides = {}) {
  return {
    entry_key: `${kind}:${amountMinor}:${overrides.suffix || "a"}`,
    wallet_address: WALLET,
    kind,
    amount_minor: amountMinor,
    currency: "USD",
    occurred_at: "2026-08-02T10:00:00.000Z",
    source: "x402_sale",
    ...overrides,
  };
}

function deps(events, overrides = {}) {
  return {
    readTenant: async () => tenant(),
    readReceipt: async () => null,
    readLedger: async () => {
      events.push("ledger");
      return [earning("financial_external_income", 100)];
    },
    readCosts: async () => {
      events.push("costs");
      return [{ ts: "2026-08-02T10:30:00.000Z", kind: "gemini_live", est_usd: "0.25" }];
    },
    readBalance: async () => {
      events.push("balance");
      return "42000000";
    },
    claimReceipt: async (receipt) => {
      events.push("claim");
      assert.equal(receipt.status, "pending");
      assert.equal(receipt.snapshot.kind, "daily");
      assert.match(receipt.snapshot_hash, /^[0-9a-f]{64}$/);
      return { claimed: true };
    },
    sendTelegram: async (_token, chatId, text) => {
      events.push("telegram");
      assert.equal(chatId, "chat-1");
      assert.match(text, /今日のagent収支/);
      return { ok: true, result: { message_id: 987 } };
    },
    markReceiptSent: async (_identity, messageId) => {
      events.push("sent");
      assert.equal(messageId, 987);
      return true;
    },
    markReceiptFailed: async () => {
      events.push("failed");
      return true;
    },
    telegramToken: "token",
    ...overrides,
  };
}

test("daily becomes due at 20:00 local and weekly at Sunday 20:05 local", () => {
  assert.deepEqual(dueReportKinds(SUNDAY_2004_JST, "Asia/Tokyo"), ["daily"]);
  assert.deepEqual(dueReportKinds(SUNDAY_2005_JST, "Asia/Tokyo"), ["daily", "weekly"]);
  assert.deepEqual(
    dueReportKinds(Date.parse("2026-08-03T11:05:00.000Z"), "Asia/Tokyo"),
    ["daily"],
  );
  assert.deepEqual(
    dueReportKinds(Date.parse("2026-08-03T10:59:59.000Z"), "Asia/Tokyo"),
    [],
  );
});

test("USD micros format without losing sub-cent measured cost", () => {
  assert.equal(formatUsdMicros("1000000"), "$1.00");
  assert.equal(formatUsdMicros("3001"), "$0.003001");
  assert.equal(formatUsdMicros("-3150000"), "-$3.15");
});

test("snapshot hash survives PostgreSQL jsonb object-key reordering", () => {
  const beforePersistence = {
    schema_version: 1,
    kind: "weekly",
    rail_pnl: [
      { rail: "WORK", net_usd_micros: "10", gross_usd_micros: "20" },
    ],
  };
  const afterJsonbReadback = {
    kind: "weekly",
    rail_pnl: [
      { gross_usd_micros: "20", net_usd_micros: "10", rail: "WORK" },
    ],
    schema_version: 1,
  };

  assert.equal(snapshotHash(beforePersistence), snapshotHash(afterJsonbReadback));
});

test("a due report claims its immutable snapshot before Telegram and stores the provider receipt", async () => {
  const events = [];
  const result = await runFinancialReport({
    uid: "u1",
    kind: "daily",
    nowMs: SUNDAY_2005_JST,
  }, deps(events));

  assert.deepEqual(events, ["ledger", "costs", "balance", "claim", "telegram", "sent"]);
  assert.equal(result.status, "sent");
  assert.equal(result.report_kind, "daily");
  assert.equal(result.period_key, "2026-08-02");
  assert.equal(result.telegram_message_id, 987);
  assert.equal(result.snapshot.gross_usd_micros, "1000000");
  assert.equal(result.snapshot.api_cost_usd_micros, "250000");
  assert.equal(result.snapshot.operating_net_usd_micros, "750000");
});

test("an already-sent period exits before ledger, chain, or Telegram reads", async () => {
  const events = [];
  const result = await runFinancialReport({
    uid: "u1",
    kind: "daily",
    nowMs: SUNDAY_2005_JST,
  }, deps(events, {
    readReceipt: async () => ({
      status: "sent",
      telegram_message_id: 123,
      snapshot_hash: "a".repeat(64),
      sent_at: "2026-08-02T11:05:01.000Z",
      period_end: "2026-08-02T11:05:00.000Z",
    }),
  }));

  assert.deepEqual(events, []);
  assert.deepEqual(result, {
    status: "duplicate",
    report_kind: "daily",
    period_key: "2026-08-02",
    telegram_message_id: 123,
    snapshot_hash: "a".repeat(64),
    chat_id_hash: "eaeb9111b1c6744278e803977dbf25fbdac6a9b6d32244ec91fc0c8266a7f65b",
    sent_at: "2026-08-02T11:05:01.000Z",
    source_freshness: {
      report_cutoff_at: "2026-08-02T11:05:00.000Z",
      earnings_latest_at: null,
      costs_latest_at: null,
      balance_observed_at: null,
    },
  });
});

test("disabled notifications and an unbound wallet fail closed before money reads", async () => {
  for (const [row, reason] of [
    [tenant({ notifications_enabled: false }), "notifications_disabled"],
    [tenant({ agent_wallet_address: null }), "agent_wallet_unbound"],
    [tenant({ telegram_chat_id: null }), "telegram_unbound"],
  ]) {
    const events = [];
    const result = await runFinancialReport({
      uid: "u1",
      kind: "daily",
      nowMs: SUNDAY_2005_JST,
    }, deps(events, { readTenant: async () => row }));
    assert.deepEqual(events, []);
    assert.equal(result.status, "skipped");
    assert.equal(result.reason, reason);
  }
});

test("Telegram rejection marks a bounded failure and never claims a provider receipt", async () => {
  const events = [];
  await assert.rejects(() => runFinancialReport({
    uid: "u1",
    kind: "daily",
    nowMs: SUNDAY_2005_JST,
  }, deps(events, {
    sendTelegram: async () => {
      events.push("telegram");
      return { ok: false, description: "provider detail must not persist" };
    },
    markReceiptFailed: async (_identity, code) => {
      events.push(`failed:${code}`);
      return true;
    },
  })), /Telegram/i);

  assert.deepEqual(events, [
    "ledger", "costs", "balance", "claim", "telegram", "failed:telegram_rejected",
  ]);
});

test("daily and weekly copy expose exactly the AE-AC4 fields from the snapshot", () => {
  const base = {
    schema_version: 1,
    kind: "daily",
    period_key: "2026-08-02",
    period_start: "2026-08-01T15:00:00.000Z",
    period_end: "2026-08-02T11:05:00.000Z",
    gross_usd_micros: "1000000",
    realized_loss_usd_micros: "250000",
    financial_fee_usd_micros: "50000",
    api_cost_usd_micros: "3001",
    operating_net_usd_micros: "696999",
    balance_usdc_atomic: "42000000",
    distributable_usdc_atomic: "7000000",
    self_funded_bps: 40000,
    self_funded_status: "measured",
    stop_reason: "running",
    rail_pnl: [
      { rail: "SELL", net_usd_micros: "700000" },
      { rail: "CAPITAL", net_usd_micros: "-250000" },
    ],
  };
  const daily = renderFinancialReport(base);
  assert.match(daily, /残高: \$42\.00/);
  assert.match(daily, /Gross: \+\$1\.00/);
  assert.match(daily, /Cost: \$0\.303001/);
  assert.match(daily, /Net: \+\$0\.696999/);
  assert.match(daily, /状態: 稼働中/);

  const weekly = renderFinancialReport({ ...base, kind: "weekly", period_key: "2026-W31" });
  assert.match(weekly, /SELL: \+\$0\.70/);
  assert.match(weekly, /CAPITAL: -\$0\.25/);
  assert.match(weekly, /Self-funded率: 400\.00%/);
  assert.match(weekly, /User分配可能額: \$7\.00/);
});

test("production tenant reads are exact UID scope and preferences never come from lm_users", async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    if (String(url).includes("/lm_users?")) {
      return { ok: true, json: async () => [{
        uid: "u1", telegram_chat_id: "chat-1", agent_wallet_address: WALLET,
      }] };
    }
    return {
      ok: true,
      json: async () => [{ notifications_enabled: false, call_time_zone: "UTC" }],
    };
  };
  const row = await readFinancialTenant("u1", {
    supaUrl: "https://db.example",
    supaKey: "service",
    fetchImpl,
  });

  assert.equal(row.notifications_enabled, false);
  assert.equal(row.call_time_zone, "UTC");
  assert.equal(calls.length, 2);
  assert.match(calls[0], /lm_users\?uid=eq\.u1&select=uid,telegram_chat_id,agent_wallet_address&limit=1/);
  assert.doesNotMatch(calls[0], /notifications_enabled|call_time_zone|select=\*/);
  assert.match(calls[1], /lm_panel_preferences\?uid=eq\.u1&select=notifications_enabled,call_time_zone&limit=1/);
});

test("cost ledger reads only one tenant and receipt claim uses database conflict arbitration", async () => {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("/lm_api_cost?")) {
      return { ok: true, json: async () => [{ id: 7, ts: "2026-08-02T10:00:00Z", est_usd: 0.1 }] };
    }
    return { ok: true, json: async () => [] };
  };
  const options = { supaUrl: "https://db.example", supaKey: "service", fetchImpl };
  const costs = await readCostLedger("u1", {
    ...options, since: "2026-08-01T15:00:00.000Z",
  });
  const claim = await claimFinancialReceipt({
    uid: "u1",
    report_kind: "daily",
    period_key: "2026-08-02",
  }, options);

  assert.equal(costs.length, 1);
  assert.match(calls[0].url, /lm_api_cost\?uid=eq\.u1/);
  assert.match(calls[0].url, /ts=gte\.2026-08-01T15%3A00%3A00\.000Z/);
  assert.match(calls[0].url, /select=id,ts,kind,quantity,unit,est_usd,meta/);
  assert.doesNotMatch(calls[0].url, /uid=not|select=\*/);
  assert.equal(calls[0].init.headers.Range, "0-999");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.headers.Prefer, "resolution=ignore-duplicates,return=representation");
  assert.deepEqual(claim, { claimed: false });
});

test("production cost aggregation is one tenant-scoped Postgres RPC rather than an unbounded row read", async () => {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      json: async () => [{
        period_est_usd: "0.283027566666",
        all_time_est_usd: "12.345678900001",
        period_rows: 16,
        all_time_rows: 12001,
      }],
    };
  };
  const result = await readFinancialCostTotals("u1", {
    period_start: "2026-08-01T15:00:00.000Z",
    period_end: "2026-08-02T11:05:00.000Z",
  }, {
    supaUrl: "https://db.example",
    supaKey: "service",
    fetchImpl,
  });

  assert.deepEqual(result, {
    period_est_usd: "0.283027566666",
    all_time_est_usd: "12.345678900001",
    period_rows: 16,
    all_time_rows: 12001,
  });
  assert.match(calls[0].url, /\/rpc\/lm_financial_cost_totals$/);
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    p_uid: "u1",
    p_period_start: "2026-08-01T15:00:00.000Z",
    p_period_end: "2026-08-02T11:05:00.000Z",
  });
});
