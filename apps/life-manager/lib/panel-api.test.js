"use strict";

const test = require("node:test");
const assert = require("node:assert");
const crypto = require("node:crypto");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

let handlePanelApiRequest = async (_req, res) => {
  res.writeHead(501, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: "panel API not implemented" }));
};
let handlePanelOAuthCallback = async (_req, res) => {
  res.writeHead(501, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: "panel callback not implemented" }));
};
let createSupabaseCommandStore = null;
try {
  ({ handlePanelApiRequest, handlePanelOAuthCallback, createSupabaseCommandStore } = require("./panel-api.js"));
} catch (error) {
  if (error.code !== "MODULE_NOT_FOUND") throw error;
}

const NOW = Date.parse("2026-07-21T12:00:00.000Z");
const SESSION = Buffer.alloc(32, 0x88).toString("base64url");
const SESSION_HASH = crypto.createHash("sha256").update(SESSION).digest("hex");

function jsonResponse(rows, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => rows };
}

function makeFixture() {
  const calls = [];
  const calendarUids = [];
  const byUid = {
    u1: {
      user: {
        uid: "u1", call_language: "ja", wake_policy: "travel-only",
        calendar_provider: "composio_gcal", gmail_account_id: null,
        telegram_chat_id: "101", payout_destination: null,
        agent_wallet_address: "0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad",
      },
      location: {
        uid: "u1", latitude: 35.0, longitude: 139.0,
        observed_at: "2026-07-21T11:00:00.000Z", expires_at: "2026-07-21T13:00:00.000Z",
      },
      wakes: [
        { uid: "u1", event_key: "evt-u1|10", called_at: "2026-07-21T08:50:00.000Z", answered_at: "2026-07-21T08:50:10.000Z" },
        { uid: "u1", event_key: "evt-u1|5", called_at: "2026-07-21T08:55:00.000Z", answered_at: null },
      ],
      costs: [
        { uid: "u1", ts: "2026-07-21T08:50:00.000Z", kind: "telnyx_call", quantity: 60, unit: "seconds", est_usd: 0.12 },
        { uid: "u1", ts: "2026-07-21T09:00:00.000Z", kind: "gemini_audio", quantity: 1, unit: "call", est_usd: 0.3 },
      ],
      earnings: [
        {
          entry_key: "pm:loss", kind: "financial_realized_loss", amount_minor: 315,
          currency: "USD", occurred_at: "2026-07-21T10:00:00.000Z",
          tx_hash: null, source: "polymarket", meta: {},
        },
      ],
      receipts: [
        {
          report_kind: "daily", period_key: "2026-07-21", status: "sent",
          snapshot_hash: "a".repeat(64), telegram_message_id: 71,
          period_end: "2026-07-21T11:00:00.000Z",
          snapshot: {
            kind: "daily", period_key: "2026-07-21",
            gross_usd_micros: "0", realized_loss_usd_micros: "0",
            financial_fee_usd_micros: "0", api_cost_usd_micros: "420000",
            operating_net_usd_micros: "-420000", balance_usdc_atomic: "0",
            distributable_usdc_atomic: "0", self_funded_bps: 0,
            stop_reason: "no_external_income", rail_pnl: [],
          },
        },
        {
          report_kind: "weekly", period_key: "2026-W30", status: "sent",
          snapshot_hash: "b".repeat(64), telegram_message_id: 72,
          period_end: "2026-07-20T11:05:00.000Z",
          snapshot: {
            kind: "weekly", period_key: "2026-W30",
            gross_usd_micros: "0", realized_loss_usd_micros: "3150000",
            financial_fee_usd_micros: "0", api_cost_usd_micros: "0",
            operating_net_usd_micros: "-3150000", balance_usdc_atomic: "0",
            distributable_usdc_atomic: "0", self_funded_bps: 0,
            stop_reason: "negative_net",
            rail_pnl: [{ rail: "CAPITAL", net_usd_micros: "-3150000" }],
          },
        },
      ],
    },
    u2: {
      user: { uid: "u2", call_language: "en", calendar_provider: "secret", telegram_chat_id: "202" },
      location: { uid: "u2", expires_at: "2026-07-22T00:00:00.000Z" },
      wakes: [{ uid: "u2", event_key: "secret-u2", called_at: "2026-07-21T09:00:00.000Z", answered_at: "2026-07-21T09:00:01.000Z" }],
      costs: [{ uid: "u2", ts: "2026-07-21T09:00:00.000Z", kind: "secret-u2", quantity: 999, unit: "secret", est_usd: 999 }],
    },
  };

  const fetchImpl = async (input, init = {}) => {
    const url = new URL(input);
    calls.push({ url, init });
    if (url.pathname.endsWith("/rpc/resolve_lm_panel_session")) {
      const body = JSON.parse(init.body);
      return jsonResponse(body.p_session_hash === SESSION_HASH ? [{ uid: "u1", chat_id: "101", rotated: false }] : []);
    }
    if (url.pathname.endsWith("/lm_panel_sessions")) {
      return jsonResponse(url.searchParams.get("session_hash") === `eq.${SESSION_HASH}` ? [{ uid: "u1", chat_id: "101" }] : []);
    }
    if (url.pathname.endsWith("/rpc/lm_panel_score_outcome_snapshot")) {
      const request = JSON.parse(init.body);
      return jsonResponse({ overflow: false, rows_by_organ: {
        daily: request.p_uid === "u1" ? [{
          public_ref: "10000000-0000-4000-8000-000000000001", revision_key: "20000000-0000-4000-8000-000000000001",
          uid: "u1", organ: "daily", entity_key: "evt-u1", outcome_kind: "daily_call", outcome_status: "required_succeeded",
          occurred_at: "2026-07-21T08:50:00.000Z", resolved_at: null, recorded_at: "2026-07-21T08:50:00.000Z", amount_minor: null, currency: null, components: {},
        }] : [], physical: [], mental: [], financial: [],
      } });
    }
    const uid = String(url.searchParams.get("uid") || "").replace(/^eq\./, "");
    const fixture = byUid[uid];
    if (url.pathname.endsWith("/lm_users")) return jsonResponse(fixture ? [fixture.user] : []);
    if (url.pathname.endsWith("/lm_panel_preferences")) return jsonResponse(uid === "u1" ? [{ call_time_zone: "UTC" }] : []);
    if (url.pathname.endsWith("/lm_user_locations")) return jsonResponse(fixture ? [fixture.location] : []);
    if (url.pathname.endsWith("/lm_wake_log")) return jsonResponse(fixture ? fixture.wakes : []);
    if (url.pathname.endsWith("/lm_api_cost")) return jsonResponse(fixture ? fixture.costs : []);
    if (url.pathname.endsWith("/lm_agent_earnings")) {
      const wallet = String(url.searchParams.get("wallet_address") || "").replace(/^eq\./, "");
      return jsonResponse(wallet === byUid.u1.user.agent_wallet_address ? byUid.u1.earnings : []);
    }
    if (url.pathname.endsWith("/lm_financial_report_receipts")) return jsonResponse(fixture ? fixture.receipts : []);
    throw new Error(`unexpected fixture URL ${url}`);
  };
  const calendar = {
    listEventsRaw: async (uid) => {
      calendarUids.push(uid);
      return uid === "u1" ? [{
        id: "evt-u1", summary: "Dentist", location: "Clinic",
        start: { dateTime: "2026-07-21T14:00:00.000Z", timeZone: "UTC" },
        end: { dateTime: "2026-07-21T15:00:00.000Z", timeZone: "UTC" },
      }] : [{ id: "secret-u2", summary: "secret-u2", start: { dateTime: "2026-07-21T14:00:00.000Z" } }];
    },
  };
  return { calls, calendarUids, fetchImpl, calendar, byUid };
}

async function withApiServer(fixture, run, overrides = {}) {
  const server = http.createServer((req, res) => {
    Promise.resolve(handlePanelApiRequest(req, res, {
      supaUrl: "https://db.example",
      supaKey: "service-key",
      fetchImpl: fixture.fetchImpl,
      calendar: fixture.calendar,
      nowMs: NOW,
      timeZone: "UTC",
      ...overrides,
    })).catch((error) => {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: error.message }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    return await run(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("Money Printer GET is tenant-bound and rejects mutation methods", async () => {
  const fixture = makeFixture();
  const seen = [];
  await withApiServer(fixture, async (base) => {
    const get = await getJson(base, "money-printer?uid=u2");
    assert.equal(get.response.status, 200);
    assert.deepEqual(get.body.metrics.opportunity_value, { JPY: "50000" });
    assert.deepEqual(get.body.metrics.paid_verified, {});
    assert.equal(get.body.columns.found[0].title, "Public opportunity");
    const post = await getJson(base, "money-printer", { method: "POST" });
    assert.equal(post.response.status, 405);
  }, {
    moneyPrinterSource: async (scope) => {
      seen.push(scope.uid);
      return {
        tenantId: scope.uid,
        observedAt: "2026-08-29T00:00:00.000Z",
        opportunities: [{ tenant_id: scope.uid, id: "op-1", title: "Public opportunity", status: "DISCOVERED", amount_minor: "50000", currency: "JPY", url: "https://example.test/op-1" }],
        runtimeJobs: [], generalReceipts: [], applicationReceipts: [], humanTasks: [], earnings: [],
      };
    },
  });
  assert.deepEqual(seen, ["u1"]);
});

async function getJson(base, endpoint, init = {}) {
  const response = await fetch(`${base}/api/panel/${endpoint}${init.query || ""}`, {
    method: init.method || "GET",
    headers: init.session === false ? {} : { Cookie: `lm_panel_session=${SESSION}` },
  });
  return { response, body: await response.json() };
}

async function humanTaskRequest(base, endpoint, { method = "POST", body, headers = {} } = {}) {
  const response = await fetch(`${base}/api/panel/${endpoint}`, {
    method,
    headers: {
      Cookie: `lm_panel_session=${SESSION}`,
      origin: "https://panel.example",
      "content-type": "application/json",
      "x-lm-csrf": "csrf-a",
      "idempotency-key": "human-task-01",
      ...headers,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return { response, body: await response.json() };
}

async function opportunityRequest(base, endpoint, { method = "POST", body, headers = {} } = {}) {
  const response = await fetch(`${base}/api/panel/${endpoint}`, {
    method,
    headers: {
      Cookie: `lm_panel_session=${SESSION}`,
      origin: "https://panel.example",
      "content-type": "application/json",
      "x-lm-csrf": "csrf-a",
      "idempotency-key": "opportunity-01",
      ...headers,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return { response, body: await response.json() };
}

function panelMutationHash(action, body) {
  return crypto.createHash("sha256").update(JSON.stringify({ action, body })).digest("hex");
}

function receiptCommandStore() {
  const receipts = new Map();
  return {
    receipts,
    async readReceipt(_scope, key) { return receipts.get(String(key)) || null; },
    async claimReceipt(_scope, key, value) {
      const normalized = String(key);
      if (receipts.has(normalized)) return false;
      receipts.set(normalized, { requestHash: value.requestHash, status: value.status, result: null });
      return true;
    },
    async finishReceipt(_scope, key, value) {
      const receipt = receipts.get(String(key));
      if (!receipt) throw new Error("receipt_missing");
      receipt.status = value.status;
      receipt.result = value.result || null;
    },
  };
}

test("Task 7B1 opportunity API creates one tenant-scoped workroom and returns only its public handle", async () => {
  const fixture = makeFixture();
  const calls = [];
  const commandStore = receiptCommandStore();
  const opportunityStore = {
    async create(opportunity) {
      calls.push(opportunity);
      return { ...opportunity, created_at: "2026-08-29T00:00:01.000Z" };
    },
  };
  const input = {
    source_url: "https://public.example/opportunity#tracking",
    title: "Public opportunity",
    goal_statement: "Complete the public opportunity and leave a verified receipt.",
    value_minor: "50000",
    currency: "JPY",
  };

  await withApiServer(fixture, async (base) => {
    const result = await opportunityRequest(base, "money-printer/opportunity", { body: input });
    assert.equal(result.response.status, 200);
    assert.deepEqual(Object.keys(result.body).sort(), ["job_ref", "opportunity_id", "status"]);
    assert.match(result.body.opportunity_id, /^[0-9a-f]{64}$/);
    assert.equal(result.body.job_ref, `runtime-job://tenant-a/goal%3A${result.body.opportunity_id}`);
    assert.equal(result.body.status, "DISCOVERED");
    const replay = await opportunityRequest(base, "money-printer/opportunity", { body: input });
    assert.equal(replay.response.status, 200);
    assert.deepEqual(replay.body, result.body);
    const conflict = await opportunityRequest(base, "money-printer/opportunity", { body: { ...input, title: "Different" } });
    assert.equal(conflict.response.status, 409);
    assert.deepEqual(conflict.body, { error: "idempotency_conflict" });
    for (const [status, error] of [["pending", "idempotency_in_progress"], ["failed", "idempotency_failed"]]) {
      const key = `opportunity-${status}`;
      commandStore.receipts.set(key, { requestHash: panelMutationHash("money-printer.opportunity.create", input), status, result: null });
      const blocked = await opportunityRequest(base, "money-printer/opportunity", { body: input, headers: { "idempotency-key": key } });
      assert.equal(blocked.response.status, 409);
      assert.deepEqual(blocked.body, { error });
    }
  }, {
    panelOrigin: "https://panel.example",
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }),
    commandStore,
    opportunityStore,
  });

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    uid: "tenant-a",
    opportunity_id: calls[0].opportunity_id,
    source_url: "https://public.example/opportunity",
    title: input.title,
    goal_statement: input.goal_statement,
    value_minor: input.value_minor,
    currency: input.currency,
    status: "DISCOVERED",
    goal_ref: `intent-entry://tenant-a/${calls[0].opportunity_id}`,
    job_id: `goal:${calls[0].opportunity_id}`,
    observed_at: "2026-07-21T12:00:00.000Z",
  });
});

test("Task find-work API dispatches a real cloud browser job to Mercor for the current tenant", async () => {
  const fixture = makeFixture();
  const commandStore = receiptCommandStore();
  const calls = [];
  const enqueueBrowserJobImpl = async (input) => {
    calls.push(input);
    return { created: true, job: { id: "job-find-1", status: "queued" } };
  };

  await withApiServer(fixture, async (base) => {
    const result = await opportunityRequest(base, "money-printer/find-work", {
      body: {},
      headers: { "idempotency-key": "find-work-01" },
    });
    assert.equal(result.response.status, 200);
    assert.deepEqual(result.body, { job_id: "job-find-1", status: "queued" });

    const replay = await opportunityRequest(base, "money-printer/find-work", {
      body: {},
      headers: { "idempotency-key": "find-work-01" },
    });
    assert.equal(replay.response.status, 200);
    assert.deepEqual(replay.body, result.body);

    const badBody = await opportunityRequest(base, "money-printer/find-work", {
      body: { query: "roles" },
      headers: { "idempotency-key": "find-work-02" },
    });
    assert.equal(badBody.response.status, 400);
    assert.deepEqual(badBody.body, { error: "invalid_find_work" });
  }, {
    panelOrigin: "https://panel.example",
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }),
    commandStore,
    enqueueBrowserJobImpl,
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].uid, "tenant-a");
  assert.match(calls[0].classification.goal, /https:\/\/work\.mercor\.com\/explore/);
  assert.equal(calls[0].classification.actionKind, "explore_listings");
  assert.equal(calls[0].classification.requiresLogin, false);
  assert.equal(calls[0].classification.principalKind, "none");
});

test("Task 7B1 opportunity API rejects invalid write fences and body shape before the store", async () => {
  const fixture = makeFixture();
  let writes = 0;
  const opportunityStore = { async create() { writes += 1; throw new Error("must not create"); } };
  const input = {
    source_url: "https://public.example/opportunity",
    title: "Public opportunity",
    goal_statement: "Complete it.",
    value_minor: "50000",
    currency: "JPY",
  };
  await withApiServer(fixture, async (base) => {
    for (const headers of [
      { origin: "https://evil.example" },
      { "x-lm-csrf": "wrong-csrf" },
      { "content-type": "text/plain" },
      { "idempotency-key": "" },
    ]) {
      const result = await opportunityRequest(base, "money-printer/opportunity", { body: input, headers });
      assert.notEqual(result.response.status, 200);
    }
    const malformed = await opportunityRequest(base, "money-printer/opportunity", { body: { ...input, private_ref: "must-not-pass" } });
    assert.equal(malformed.response.status, 400);
  }, {
    panelOrigin: "https://panel.example",
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }),
    opportunityStore,
  });
  assert.equal(writes, 0);
});

test("Task 7B1 workroom API returns the exact tenant opportunity, matching job, and activity only", async () => {
  const fixture = makeFixture();
  const opportunityId = "a".repeat(64);
  const otherId = "b".repeat(64);
  const sourceCalls = [];
  const moneyPrinterSource = async (scope) => {
    sourceCalls.push(scope);
    return {
      tenantId: scope.uid,
      observedAt: "2026-08-29T00:00:00.000Z",
      opportunities: [
        {
          tenant_id: scope.uid, opportunity_id: opportunityId,
          source_url: "https://public.example/opportunity", title: "Selected opportunity",
          value_minor: "50000", currency: "JPY", status: "WORKING",
          goal_ref: "private-goal-ref", observed_at: "2026-08-29T00:00:00.000Z",
          goal_statement: "must not leak",
        },
        {
          tenant_id: scope.uid, opportunity_id: otherId,
          source_url: "https://public.example/other", title: "Other opportunity",
          value_minor: "90000", currency: "JPY", status: "DISCOVERED",
          goal_ref: "private-other-goal-ref", observed_at: "2026-08-29T00:00:00.000Z",
        },
      ],
      runtimeJobs: [
        { tenant_id: scope.uid, job_id: `goal:${opportunityId}`, status: "running", created_at: "2026-08-29T00:00:00.000Z", updated_at: "2026-08-29T00:01:00.000Z", input_refs: { goal_ref: "private-input-ref" } },
        { tenant_id: scope.uid, job_id: `goal:${otherId}`, status: "queued", created_at: "2026-08-29T00:00:00.000Z", updated_at: "2026-08-29T00:01:00.000Z" },
      ],
      generalReceipts: [], applicationReceipts: [], humanTasks: [
        {
          tenant_id: scope.uid, task_id: "c".repeat(64), job_id: `goal:${opportunityId}`,
          reason_code: "identity_assessment", version: 1, status: "open",
          created_at: "2026-08-29T00:00:00.000Z", updated_at: "2026-08-29T00:02:00.000Z",
          question: "private question must not leak", answer_ref: "private-answer-ref", context_refs: { secret: "private" },
        },
        {
          tenant_id: scope.uid, task_id: "d".repeat(64), job_id: `goal:${otherId}`,
          reason_code: "identity_assessment", version: 1, status: "open",
          created_at: "2026-08-29T00:00:00.000Z", updated_at: "2026-08-29T00:02:00.000Z",
          question: "foreign job must not leak", answer_ref: "foreign-answer-ref", context_refs: { secret: "foreign" },
        },
      ], earnings: [],
    };
  };

  await withApiServer(fixture, async (base) => {
    const result = await opportunityRequest(base, `money-printer/workroom?opportunity_id=${opportunityId}`, { method: "GET" });
    assert.equal(result.response.status, 200);
    assert.deepEqual(result.body, {
      opportunity_id: opportunityId,
      title: "Selected opportunity",
      value_minor: "50000",
      currency: "JPY",
      source_url: "https://public.example/opportunity",
      status: "WORKING",
      job_ref: `runtime-job://tenant-a/goal%3A${opportunityId}`,
      activity: [
        { kind: "opportunity", ref: `opportunity://tenant-a/${opportunityId}`, status: "WORKING", observed_at: "2026-08-29T00:00:00.000Z" },
        { kind: "work", ref: `runtime-job://tenant-a/goal%3A${opportunityId}`, status: "running", observed_at: "2026-08-29T00:01:00.000Z" },
        {
          kind: "human_task", ref: `human-task://tenant-a/${"c".repeat(64)}`,
          job_ref: `runtime-job://tenant-a/goal%3A${opportunityId}`,
          status: "open", observed_at: "2026-08-29T00:02:00.000Z",
        },
      ],
    });
    assert.doesNotMatch(JSON.stringify(result.body), /goal_statement|private|foreign|input_refs|answer_ref|question|context_refs|other/);

    const unknown = await opportunityRequest(base, `money-printer/workroom?opportunity_id=${"c".repeat(64)}`, { method: "GET" });
    assert.equal(unknown.response.status, 404);
    assert.deepEqual(unknown.body, { error: "not_found" });
  }, {
    panelOrigin: "https://panel.example",
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }),
    moneyPrinterSource,
  });
  assert.deepEqual(sourceCalls.map((scope) => scope.uid), ["tenant-a", "tenant-a"]);
});

test("Task 7B1 server source wiring is lazy and has no fake Money Printer fallback", () => {
  const server = fs.readFileSync(path.join(__dirname, "../server.js"), "utf8");
  assert.match(server, /createMoneyPrinterSource/);
  assert.match(server, /createMoneyPrinterRuntimeStore/);
  assert.match(server, /LM_RUNTIME_DATABASE_URL\s*\|\|\s*process\.env\.LM_FEEDBACK_DATABASE_URL/);
  assert.match(server, /runtimeStore,\s*opportunityStore:\s*runtimeStore,\s*humanTaskStore:\s*runtimeStore/s);
  assert.match(server, /moneyPrinterSource\s*:/);
  assert.match(server, /supaUrl:\s*SUPA_URL/);
  assert.match(server, /supaKey:\s*SUPA_KEY/);
  assert.doesNotMatch(server, /moneyPrinterSource:\s*(?:async\s*)?\(?.*=>\s*\(\{\s*tenantId/);
});

test("Task 8A server only resolves runtime storage for Money Printer paths", () => {
  const { panelApiOptions, panelOriginForPath } = require("../server.js");
  const base = { supaUrl: "https://db.example", supaKey: "service-key" };
  let calls = 0;
  const getRuntimeStore = () => { calls += 1; return { name: "runtime" }; };

  assert.equal(panelApiOptions("/api/panel/timeline", base, getRuntimeStore), base);
  assert.equal(calls, 0);
  const money = panelApiOptions("/api/panel/money-printer/workroom", base, getRuntimeStore);
  assert.equal(calls, 1);
  assert.equal(money.runtimeStore.name, "runtime");
  assert.equal(money.opportunityStore, money.runtimeStore);
  assert.equal(money.humanTaskStore, money.runtimeStore);
  assert.equal(typeof money.moneyPrinterSource, "function");
});

test("Task 8A server uses public origin only for proxied Money Printer APIs", () => {
  const { panelOriginForPath } = require("../server.js");
  const source = fs.readFileSync(path.join(__dirname, "../server.js"), "utf8");
  assert.match(source, /path === "\/money-printer"/);
  assert.match(source, /handleMoneyPrinterGuestRequest\(req, res/);
  assert.equal(panelOriginForPath("/api/panel/money-printer"), "https://aniccaai.com");
  assert.equal(panelOriginForPath("/api/panel/money-printer/workroom"), "https://aniccaai.com");
  assert.equal(panelOriginForPath("/api/panel/timeline"), "");
});

test("Task 8A server routes both WebSocket paths through one upgrade listener", () => {
  const source = fs.readFileSync(path.join(__dirname, "../server.js"), "utf8");
  assert.match(source, /const wss = new WebSocket\.Server\(\{ noServer: true \}\)/);
  assert.match(source, /if \(upgradePath === "\/ws"\)[\s\S]*wss\.handleUpgrade/);
  assert.match(source, /if \(!upgradePath\.startsWith\(`\$\{BROWSER_CAST_PATH\}\/`\)\)/);
});

test("Task 8A Panel routes opportunity and human domain actions through runtimeStore", async () => {
  const fixture = makeFixture();
  const task = {
    uid: "tenant-a", task_id: "a".repeat(64), version: 1, question: "Approve?", required_format: "approval",
    reason_code: "model_boundary", resume_ref: "runtime-job://tenant-a/job-1", status: "open",
  };
  const calls = [];
  const runtimeStore = {
    async createOpportunity(canonical) { calls.push(["opportunity", canonical.uid]); return { ...canonical, created_at: "2026-08-29T00:00:00.000Z" }; },
    async readNext(scope) { calls.push(["next", scope.uid]); return task; },
    async answerOnce(answer) { calls.push(["answer", answer.uid]); return { ...task, status: "answered", answer_ref: answer.answerRef }; },
  };
  await withApiServer(fixture, async (base) => {
    const created = await opportunityRequest(base, "money-printer/opportunity", { body: {
      source_url: "https://public.example/opportunity", title: "Public opportunity", goal_statement: "Complete it.", value_minor: "50000", currency: "JPY",
    } });
    assert.equal(created.response.status, 200);
    assert.equal((await humanTaskRequest(base, "money-printer/human-task/next", { method: "GET" })).response.status, 200);
    assert.equal((await humanTaskRequest(base, "money-printer/human-task/answer", { body: {
      task_id: task.task_id, version: 1, answer_ref: "vault-answer://tenant-a/answer-1",
    }, headers: { "idempotency-key": "runtime-answer-01" } })).response.status, 200);
  }, {
    panelOrigin: "https://panel.example", runtimeStore, commandStore: receiptCommandStore(),
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }),
  });
  assert.deepEqual(calls.map(([kind]) => kind), ["opportunity", "next", "answer"]);
});

test("Task 5B human-task API returns one safe tenant task and replays one answer", async () => {
  const fixture = makeFixture();
  const task = {
    uid: "tenant-a",
    task_id: "a".repeat(64),
    version: 1,
    question: "Approve the prepared delivery.",
    required_format: { kind: "approval", values: ["approve", "request_changes"] },
    reason_code: "provider_interview",
    resume_ref: "runtime-job://tenant-a/job-1",
    status: "open",
  };
  const calls = [];
  const browserCompletions = [];
  let state = { ...task };
  const humanTaskStore = {
    async readNext(scope) {
      calls.push({ type: "read", scope });
      return state.status === "open" ? { ...state } : null;
    },
    async answerOnce(answer) {
      calls.push({ type: "answer", answer });
      if (answer.uid !== "tenant-a") throw new Error("human task scope mismatch");
      if (state.status === "answered") {
        if (state.answer_ref !== answer.answerRef) throw new Error("human task answer conflict");
        return { ...state };
      }
      if (answer.version !== state.version) throw new Error("human task version conflict");
      state = { ...state, status: "answered", version: 2, answer_ref: answer.answerRef, answered_at: "2026-08-29T00:00:00.000Z" };
      return { ...state };
    },
  };
  const commandStore = receiptCommandStore();
  await withApiServer(fixture, async (base) => {
    const next = await humanTaskRequest(base, "money-printer/human-task/next", { method: "GET" });
    assert.equal(next.response.status, 200);
    assert.deepEqual(next.body, {
      task: {
        task_id: task.task_id,
        version: 1,
        question: task.question,
        required_format: task.required_format,
        reason_code: task.reason_code,
        browser_takeover_available: true,
      },
    });
    assert.equal(Object.hasOwn(next.body.task, "uid"), false);
    assert.equal(Object.hasOwn(next.body.task, "resume_ref"), false);

    const answer = { task_id: task.task_id, version: 1, answer: "approve" };
    const first = await humanTaskRequest(base, "money-printer/human-task/answer", { body: answer });
    assert.equal(first.response.status, 200);
    assert.deepEqual(first.body, { task_id: task.task_id, resume_ref: task.resume_ref });
    const replay = await humanTaskRequest(base, "money-printer/human-task/answer", { body: answer });
    assert.equal(replay.response.status, 200);
    assert.deepEqual(replay.body, first.body);

    const conflict = await humanTaskRequest(base, "money-printer/human-task/answer", {
      body: { ...answer, answer: "request_changes" },
      headers: { "idempotency-key": "human-task-01" },
    });
    assert.equal(conflict.response.status, 409);
    assert.deepEqual(conflict.body, { error: "idempotency_conflict" });
    for (const [status, error] of [["pending", "idempotency_in_progress"], ["failed", "idempotency_failed"]]) {
      const key = `human-task-${status}`;
      commandStore.receipts.set(key, { requestHash: panelMutationHash("money-printer.human-task.answer", answer), status, result: null });
      const blocked = await humanTaskRequest(base, "money-printer/human-task/answer", { body: answer, headers: { "idempotency-key": key } });
      assert.equal(blocked.response.status, 409);
      assert.deepEqual(blocked.body, { error });
    }
  }, { panelOrigin: "https://panel.example", commandStore, humanTaskStore,
    browserHandoffReader: async () => ({ expired: false }),
    completeBrowserHandoff: async (uid, answer) => { browserCompletions.push({ uid, answer }); },
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }) });
  assert.equal(calls[0].type, "read");
  assert.deepEqual(calls[0].scope, { uid: "tenant-a", chatId: "101", csrf: "csrf-a" });
  assert.deepEqual(calls.slice(1).map((call) => call.answer), [
    { uid: "tenant-a", taskId: task.task_id, version: 1, answerRef: "vault-answer://tenant-a/approve" },
  ]);
  assert.deepEqual(browserCompletions, [{ uid: "tenant-a", answer: "approve" }]);
});

test("Task 5B request_changes resumes the same job before a browser handoff exists", async () => {
  const fixture = makeFixture();
  const task = {
    uid: "tenant-a", task_id: "a".repeat(64), version: 1, question: "Complete provider work.",
    required_format: { type: "confirmation", values: ["approve", "request_changes"] },
    reason_code: "provider_interview", resume_ref: "runtime-job://tenant-a/job-1", status: "open",
  };
  const answers = [];
  const humanTaskStore = {
    async answerOnce(answer) {
      answers.push(answer);
      return { ...task, status: "answered", version: 2, answer_ref: answer.answerRef };
    },
  };
  await withApiServer(fixture, async (base) => {
    const result = await humanTaskRequest(base, "money-printer/human-task/answer", {
      body: { task_id: task.task_id, version: 1, answer: "request_changes" },
    });
    assert.equal(result.response.status, 200);
    assert.deepEqual(result.body, { task_id: task.task_id, resume_ref: task.resume_ref });
  }, {
    panelOrigin: "https://panel.example", commandStore: receiptCommandStore(), humanTaskStore,
    browserHandoffReader: async () => null,
    completeBrowserHandoff: async () => { throw new Error("must not require a missing handoff"); },
    sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }),
  });
  assert.deepEqual(answers, [{
    uid: "tenant-a", taskId: task.task_id, version: 1,
    answerRef: "vault-answer://tenant-a/request_changes",
  }]);
});

test("Task 5B human-task answer rejects missing origin, CSRF, content type, and idempotency before the store", async () => {
  const fixture = makeFixture();
  let writes = 0;
  const humanTaskStore = {
    async readNext() { return null; },
    async answerOnce() { writes += 1; throw new Error("must not answer"); },
  };
  await withApiServer(fixture, async (base) => {
    for (const headers of [
      { Origin: "https://evil.example" },
      { "x-lm-csrf": "wrong-csrf" },
      { "content-type": "text/plain" },
      { "idempotency-key": "" },
    ]) {
      const result = await humanTaskRequest(base, "money-printer/human-task/answer", {
        body: { task_id: "a".repeat(64), version: 1, answer_ref: "vault-answer://tenant-a/a" },
        headers,
      });
      assert.notEqual(result.response.status, 200);
    }
  }, { panelOrigin: "https://panel.example", humanTaskStore, sessionScopeImpl: async () => ({ uid: "tenant-a", chatId: "101", csrf: "csrf-a" }) });
  assert.equal(writes, 0);
});

test("Task 8A command receipts stay in Supabase while human work is not", () => {
  const store = createSupabaseCommandStore({ supaUrl: "https://db.example", supaKey: "service-key" });
  assert.equal(typeof store.readNext, "undefined");
  assert.equal(typeof store.answerOnce, "undefined");
  assert.equal(typeof store.claimReceipt, "function");
});

test("LM-33b timeline returns today's interpreted calendar and call telemetry", async () => {
  const fixture = makeFixture();
  await withApiServer(fixture, async (base) => {
    const { response, body } = await getJson(base, "timeline");
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.deepEqual(body, {
      date: "2026-07-21", timezone: "UTC",
      items: [
        { sentence: "14:00開始の予定です。詳細はカレンダーで確認してください。", status: "カレンダーで確認" },
        { sentence: "08:50の電話は応答済みです。", status: "応答済み" },
        { sentence: "08:55の電話は未応答です。", status: "未応答" },
      ],
    });
  });
  assert.deepEqual(fixture.calendarUids, ["u1"]);
});

test("PANEL-8g scores use source outcomes and expose all four closed organs", async () => {
  await withApiServer(makeFixture(), async (base) => {
    const { response, body } = await getJson(base, "scores");
    assert.equal(response.status, 200);
    assert.deepEqual(Object.keys(body.organs), ["daily", "physical", "mental", "financial"]);
    assert.deepEqual({ status: body.organs.daily.status, value: body.organs.daily.value, numerator: body.organs.daily.numerator, denominator: body.organs.daily.denominator }, { status: "measured", value: 100, numerator: 1, denominator: 1 });
    assert.equal(body.organs.physical.status, "insufficient_data");
    assert.equal(body.organs.mental.status, "insufficient_data");
    assert.equal(body.organs.financial.status, "insufficient_data");
  });
});

test("REPORT-1 panel reads the real earnings table and the exact Telegram snapshots", async () => {
  const fixture = makeFixture();
  await withApiServer(fixture, async (base) => {
    const { response, body } = await getJson(base, "ledger");
    assert.equal(response.status, 200);
    assert.deepEqual(body, {
      api_cost: {
        no_data: false,
        total: "USD 0.42",
        items: [
          { label: "API利用料", date: "2026-07-21", amount: "USD 0.12", link: null },
          { label: "API利用料", date: "2026-07-21", amount: "USD 0.30", link: null },
        ],
      },
      financial: {
        no_data: false,
        items: [
          { label: "実現損失", date: "2026-07-21", amount: "USD 3.15", link: null },
        ],
      },
      reports: {
        daily: {
          period_key: "2026-07-21",
          snapshot_hash: "a".repeat(64),
          telegram_message_id: 71,
          gross_usd_micros: "0",
          realized_loss_usd_micros: "0",
          financial_fee_usd_micros: "0",
          api_cost_usd_micros: "420000",
          operating_net_usd_micros: "-420000",
          balance_usdc_atomic: "0",
          distributable_usdc_atomic: "0",
          self_funded_bps: 0,
          stop_reason: "no_external_income",
          rail_pnl: [],
        },
        weekly: {
          period_key: "2026-W30",
          snapshot_hash: "b".repeat(64),
          telegram_message_id: 72,
          gross_usd_micros: "0",
          realized_loss_usd_micros: "3150000",
          financial_fee_usd_micros: "0",
          api_cost_usd_micros: "0",
          operating_net_usd_micros: "-3150000",
          balance_usdc_atomic: "0",
          distributable_usdc_atomic: "0",
          self_funded_bps: 0,
          stop_reason: "negative_net",
          rail_pnl: [{ rail: "CAPITAL", net_usd_micros: "-3150000" }],
        },
      },
    });
  });
  const paths = fixture.calls.map((call) => call.url.pathname);
  assert.equal(paths.includes("/rest/v1/lm_financial_ledger"), false);
  assert.equal(paths.includes("/rest/v1/lm_agent_earnings"), true);
  assert.equal(paths.includes("/rest/v1/lm_financial_report_receipts"), true);
});

test("the authenticated ledger renders exact atomic USDC earnings without float rounding", async () => {
  const fixture = makeFixture();
  fixture.byUid.u1.earnings.unshift({
    entry_key: "taskmarket:award",
    kind: "financial_external_income",
    amount_minor: null,
    amount_atomic: "2312500",
    amount_decimals: 6,
    currency: "USD",
    occurred_at: "2026-07-21T10:30:00.000Z",
    tx_hash: `0x${"c".repeat(64)}`,
    source: "taskmarket_work",
    meta: {},
  });
  await withApiServer(fixture, async (base) => {
    const { response, body } = await getJson(base, "ledger");
    assert.equal(response.status, 200);
    assert.deepEqual(body.financial.items[0], {
      label: "外部収益",
      date: "2026-07-21",
      amount: "USD 2.3125",
      link: null,
    });
  });
  const earningsCall = fixture.calls.find((call) =>
    call.url.pathname.endsWith("/lm_agent_earnings"));
  assert.match(earningsCall.url.searchParams.get("select"), /amount_atomic,amount_decimals/);
});

test("LM-33b gates reuse LM-32 lock decisions and discovery copy", async () => {
  await withApiServer(makeFixture(), async (base) => {
    const { response, body } = await getJson(base, "gates");
    assert.equal(response.status, 200);
    assert.equal(body.gates.length, 2);
    assert.deepEqual(body.gates.map(({ id, unlocked }) => ({ id, unlocked })), [
      { id: "location", unlocked: true },
      { id: "payout", unlocked: false },
    ]);
    for (const gate of body.gates) assert.equal(typeof gate.unlock_method, "string");
  });
});

test("LM-33b settings mirror language, actual call schedule, and connection state", async () => {
  await withApiServer(makeFixture(), async (base) => {
    const { response, body } = await getJson(base, "settings");
    assert.equal(response.status, 200);
    assert.deepEqual(body, {
      call_language: "ja",
      call_schedule: { time_zone: "UTC", minutes_before: [10, 5], wake_policy: "travel-only" },
      connections: { calendar: false, gmail: false, telegram: true },
    });
  });
});

test("LM-33b negative: every endpoint requires a panel session", async () => {
  await withApiServer(makeFixture(), async (base) => {
    for (const endpoint of ["timeline", "scores", "ledger", "gates", "settings"]) {
      const { response, body } = await getJson(base, endpoint, { session: false });
      assert.equal(response.status, 401, endpoint);
      assert.deepEqual(body, { error: "unauthorized" });
    }
  });
});

test("LM-33b negative: a panel cookie outside /api/panel cannot touch session or panel data", async () => {
  const fixture = makeFixture();
  await withApiServer(fixture, async (base) => {
    const response = await fetch(`${base}/health`, {
      headers: { Cookie: `lm_panel_session=${SESSION}` },
    });
    assert.equal(response.status, 404);
    assert.deepEqual(await response.json(), { error: "not_found" });
  });
  assert.equal(fixture.calls.length, 0);
  assert.deepEqual(fixture.calendarUids, []);
});

test("LM-33b negative: API is read-only", async () => {
  const fixture = makeFixture();
  await withApiServer(fixture, async (base) => {
    const { response, body } = await getJson(base, "settings", { method: "POST" });
    assert.equal(response.status, 405);
    assert.deepEqual(body, { error: "method_not_allowed" });
    assert.equal(response.headers.get("allow"), "GET");
  });
  assert.ok(fixture.calls.every(({ url, init }) => !init.method || init.method === "GET" || url.pathname.endsWith("/rpc/resolve_lm_panel_session") || url.pathname.endsWith("/rpc/lm_panel_score_outcome_snapshot")));
});

test("LM-33b negative: request UID is ignored and every data source stays bound to session UID", async () => {
  const fixture = makeFixture();
  await withApiServer(fixture, async (base) => {
    for (const endpoint of ["timeline", "scores", "ledger", "gates", "settings"]) {
      const { response, body } = await getJson(base, endpoint, { query: "?uid=u2" });
      assert.equal(response.status, 200, endpoint);
      assert.doesNotMatch(JSON.stringify(body), /u2|secret/);
    }
  });
  const dataReads = fixture.calls.filter(({ url }) => !url.pathname.endsWith("/lm_panel_sessions") && !url.pathname.includes("/rpc/"));
  assert.ok(dataReads.length > 0);
  for (const { url } of dataReads) {
    if (url.pathname.endsWith("/lm_agent_earnings")) {
      assert.equal(
        url.searchParams.get("wallet_address"),
        `eq.${byUidWallet(fixture)}`,
        url.toString(),
      );
    } else {
      assert.equal(url.searchParams.get("uid"), "eq.u1", url.toString());
    }
  }
  assert.ok(fixture.calendarUids.length > 0);
  assert.ok(fixture.calendarUids.every((uid) => uid === "u1"));
});

function byUidWallet(fixture) {
  const userRead = fixture.calls.find(({ url }) => (
    url.pathname.endsWith("/lm_users")
    && url.searchParams.get("select") === "agent_wallet_address"
  ));
  assert.ok(userRead);
  return "0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad";
}

function onboardingHarness(initial = {}) {
  const writes = [];
  const syncs = [];
  const wrapperCalls = [];
  const providerReads = [];
  const reads = [];
  const receipts = new Map();
  const state = {
    step: "name", stage: "calendar", name: null, calendarConnected: false,
    homeAddress: null, notificationsEnabled: false, phone: null,
    callEnabled: false, paid: false, paymentLink: null, ...initial,
  };
  const scope = { uid: "tenant-a", chatId: "101", csrf: "csrf-a" };
  const commandStore = {
    async assertCurrentScope(value) { return value.uid === scope.uid && value.chatId === scope.chatId; },
    async readReceipt(value, key) { assert.deepEqual(value, scope); return receipts.get(String(key)) || null; },
    async claimReceipt(value, key, entry) {
      assert.deepEqual(value, scope);
      const normalized = String(key);
      if (receipts.has(normalized)) return false;
      receipts.set(normalized, { requestHash: entry.requestHash, status: entry.status, result: null });
      return true;
    },
    async finishReceipt(value, key, entry) {
      assert.deepEqual(value, scope);
      const receipt = receipts.get(String(key));
      if (!receipt) throw new Error("receipt_missing");
      receipt.status = entry.status;
      receipt.result = entry.result || null;
    },
    async readOnboardingState(value) {
      assert.deepEqual(value, scope);
      reads.push("state");
      return { ...state };
    },
    async syncCalendarStatus(value, status) { assert.deepEqual(value, scope); syncs.push(status); return true; },
    async mutateOnboardingWithCalendar(value, status, action, payload) {
      assert.deepEqual(value, scope);
      wrapperCalls.push({ status, action, payload });
      writes.push({ action, payload });
      return { ...state };
    },
    async mutateOnboarding(value, action, payload) {
      assert.deepEqual(value, scope);
      writes.push({ action, payload });
      return { ...state };
    },
  };
  const opts = {
    panelOrigin: "https://panel.example",
    panelBaseUrl: "https://panel.example",
    sessionScopeImpl: async () => scope,
    commandStore,
    stripePaymentLink: "https://buy.stripe.com/test_life_manager",
    composioKey: "provider-key",
    composioCalendarStatusImpl: async () => { providerReads.push("ACTIVE"); return "ACTIVE"; },
  };
  return { state, scope, writes, syncs, wrapperCalls, providerReads, reads, receipts, opts, commandStore };
}

async function onboardingRequest(harness, { method = "GET", path = "/api/panel/onboarding", body, headers = {} } = {}) {
  const req = new http.IncomingMessage();
  req.method = method;
  req.url = path;
  req.headers = {
    cookie: `__Host-lm_panel_session=${SESSION}`,
    origin: "https://panel.example",
    "content-type": "application/json",
    "x-lm-csrf": "csrf-a",
    "idempotency-key": Object.hasOwn(headers, "idempotency-key") ? headers["idempotency-key"] : `onboarding-${harness._requestNumber = (harness._requestNumber || 0) + 1}`,
    ...headers,
  };
  const chunks = [];
  if (body == null) req.push(null);
  else { req.push(JSON.stringify(body)); req.push(null); }
  const response = await new Promise((resolve, reject) => {
    const out = new (require("node:stream").Writable)({
      write(chunk, _encoding, callback) { chunks.push(Buffer.from(chunk)); callback(); },
    });
    out.writeHead = (status, responseHeaders) => { out.status = status; out.headers = new Map(Object.entries(responseHeaders)); };
    out.end = (chunk) => { if (chunk) chunks.push(Buffer.from(chunk)); resolve(out); };
    Promise.resolve(handlePanelApiRequest(req, out, harness.opts)).catch(reject);
  });
  const raw = Buffer.concat(chunks).toString("utf8");
  return { response, body: raw ? JSON.parse(raw) : null };
}

function onboardingRequestHash(action, payload) {
  return crypto.createHash("sha256").update(JSON.stringify({ action, payload })).digest("hex");
}

test("Task 7A onboarding API follows server-owned fixed progression and ignores client uid", async () => {
  const h = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
  const first = await onboardingRequest(h, { path: "/api/panel/onboarding?uid=tenant-b" });
  assert.equal(first.response.status, 200);
  assert.equal(first.body.step, "home");
  assert.equal(first.body.paymentLink, undefined);
  assert.equal(h.writes.length, 0);
  const forgedCalendar = await onboardingRequest(h, { method: "POST", body: { action: "calendar.complete", uid: "tenant-b" } });
  assert.equal(forgedCalendar.response.status, 400);
  assert.equal(h.writes.length, 0, "calendar completion is provider-owned, never a client write");

  const actions = [
    ["home.save", { home_address: "東京都渋谷区 1-1-1" }],
    ["notifications.enable", {}],
    ["phone.save", { phone: "+81 (90) 1234-5678", uid: "tenant-b" }],
    ["call.enable", {}],
    ["payment.skip", { paid: true, uid: "tenant-b" }],
  ];
  for (const [action, payload] of actions) {
    h.commandStore.mutateOnboardingWithCalendar = async (scope, status, actualAction, actualPayload) => {
      assert.deepEqual(scope, h.scope);
      assert.equal(status, "ACTIVE");
      h.wrapperCalls.push({ status, action: actualAction, payload: actualPayload });
      h.writes.push({ action: actualAction, payload: actualPayload });
      return { ...h.state, step: action === "payment.skip" ? "dashboard" : action.split(".")[0] };
    };
    const result = await onboardingRequest(h, { method: "POST", body: { action, ...payload } });
    assert.equal(result.response.status, 200, action);
    assert.notEqual(result.body.paid, true, "client action never grants paid");
  }
  assert.equal(h.writes.length, actions.length);
  assert.equal(Object.hasOwn(h.writes[2].payload, "uid"), false, "uid is stripped before the tenant-scoped transition");
  assert.equal(h.writes[2].payload.phone, ["+81", "90", "1234", "5678"].join(""), "phone is normalized before persistence");
});

test("Task 7A rejects an out-of-order onboarding mutation before any write", async () => {
  const h = onboardingHarness({ step: "home", stage: "home" });
  h.commandStore.mutateOnboardingWithCalendar = async () => {
    const error = new Error("onboarding_conflict");
    error.status = 409;
    throw error;
  };
  const result = await onboardingRequest(h, { method: "POST", body: { action: "call.enable" } });
  assert.equal(result.response.status, 409);
  assert.deepEqual(result.body, { error: "onboarding_conflict" });
  assert.equal(h.writes.length, 0);
});

test("Task 7A phone.save converts a Japanese domestic number and preserves explicit international form", async () => {
  const h = onboardingHarness({ step: "phone", stage: "phone", calendarConnected: true });
  const domestic = await onboardingRequest(h, { method: "POST", body: { action: "phone.save", phone: "090-1234-5678" }, headers: { "idempotency-key": "phone-domestic-01" } });
  assert.equal(domestic.response.status, 200);
  assert.equal(h.wrapperCalls.at(-1).payload.phone, ["+81", "90", "1234", "5678"].join(""));

  const international = await onboardingRequest(h, { method: "POST", body: { action: "phone.save", phone: "+44 (20) 7946-0958" }, headers: { "idempotency-key": "phone-intl-0001" } });
  assert.equal(international.response.status, 200);
  assert.equal(h.wrapperCalls.at(-1).payload.phone, "+442079460958");
});

test("Task 7A legacy payment stage returns ready dashboard with optional server Stripe link", async () => {
  const h = onboardingHarness({ step: "payment", stage: "payment", paymentLink: null });
  const result = await onboardingRequest(h);
  assert.equal(result.response.status, 200);
  assert.equal(result.body.step, "dashboard");
  assert.equal(result.body.paymentLink, "https://buy.stripe.com/test_life_manager?client_reference_id=tenant-a");
  const missing = onboardingHarness({ step: "payment", stage: "payment" });
  missing.opts.stripePaymentLink = "";
  const unavailable = await onboardingRequest(missing);
  assert.equal(unavailable.response.status, 200);
  assert.equal(unavailable.body.step, "dashboard");
  assert.equal(unavailable.body.paymentLink, undefined);
});

test("Task 3 legacy ready stages all normalize to dashboard", async () => {
  for (const stage of ["payment", "pay", "done", "gmail"]) {
    const h = onboardingHarness({ step: stage, stage, paid: false });
    h.opts.stripePaymentLink = "";
    const result = await onboardingRequest(h);
    assert.equal(result.response.status, 200, stage);
    assert.equal(result.body.step, "dashboard", stage);
    assert.equal(result.body.paymentLink, undefined, stage);
  }
});

test("Task 7A paid phone-less tenant resumes at dashboard after required core steps", async () => {
  const h = onboardingHarness({ step: "dashboard", stage: "phone", calendarConnected: true, homeAddress: "home", notificationsEnabled: true, paid: true, phone: null });
  const result = await onboardingRequest(h);
  assert.equal(result.response.status, 200);
  assert.equal(result.body.step, "dashboard");
  assert.equal(result.body.phone, null);
  assert.equal(result.body.callEnabled, false);
});

test("Task 7A refreshes official Calendar truth before every state read or transition", async () => {
  const h = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
  const get = await onboardingRequest(h);
  assert.equal(get.response.status, 200);
  assert.deepEqual(h.syncs, ["ACTIVE"]);
  h.opts.composioCalendarStatusImpl = async () => "MISSING";
  h.commandStore.mutateOnboardingWithCalendar = async () => { throw new Error("transition must not run when Calendar is missing"); };
  const post = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" } });
  assert.equal(post.response.status, 502);
  assert.deepEqual(h.syncs, ["ACTIVE"], "POST uses the combined transition RPC, not standalone sync");
  assert.deepEqual(h.reads, ["state"]);
});

test("R1A2 valid onboarding POST reads provider then uses exactly one combined sync/transition RPC", async () => {
  const h = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
  const result = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" } });
  assert.equal(result.response.status, 200);
  assert.deepEqual(h.providerReads, ["ACTIVE"]);
  assert.deepEqual(h.syncs, [], "POST must not call standalone Calendar sync");
  assert.equal(h.wrapperCalls.length, 1);
  assert.deepEqual(h.wrapperCalls[0], { status: "ACTIVE", action: "home.save", payload: { home_address: "home" } });
});

test("R1A2 provider failure prevents both onboarding RPCs", async () => {
  const h = onboardingHarness({ step: "home", stage: "home" });
  h.opts.composioCalendarStatusImpl = async () => { h.providerReads.push("error"); throw new Error("provider_down"); };
  const result = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" } });
  assert.equal(result.response.status, 502);
  assert.deepEqual(h.providerReads, ["error"]);
  assert.deepEqual(h.syncs, []);
  assert.deepEqual(h.wrapperCalls, []);
});

test("Task 7B R1: onboarding POST claims one receipt, replays success, and rejects key reuse", async () => {
  const h = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
  const key = "onboarding-replay-01";
  const first = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" }, headers: { "idempotency-key": key } });
  assert.equal(first.response.status, 200);
  const before = { provider: h.providerReads.length, wrapper: h.wrapperCalls.length, writes: h.writes.length };
  const replay = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" }, headers: { "idempotency-key": key } });
  assert.equal(replay.response.status, 200);
  assert.deepEqual(replay.body, first.body);
  assert.deepEqual({ provider: h.providerReads.length, wrapper: h.wrapperCalls.length, writes: h.writes.length }, before);
  const conflict = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "other" }, headers: { "idempotency-key": key } });
  assert.equal(conflict.response.status, 409);
  assert.deepEqual(conflict.body, { error: "idempotency_conflict" });
  assert.deepEqual({ provider: h.providerReads.length, wrapper: h.wrapperCalls.length, writes: h.writes.length }, before);
});

test("Task 7B R1: onboarding POST requires a valid key and never re-transitions pending or failed receipts", async () => {
  const missing = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
  const noKey = await onboardingRequest(missing, { method: "POST", body: { action: "home.save", home_address: "home" }, headers: { "idempotency-key": "" } });
  assert.equal(noKey.response.status, 400);
  assert.deepEqual(noKey.body, { error: "idempotency_required" });
  assert.equal(missing.providerReads.length, 0);

  const payload = { home_address: "home" };
  const requestHash = onboardingRequestHash("home.save", payload);
  for (const [status, error] of [["pending", "idempotency_in_progress"], ["failed", "idempotency_failed"]]) {
    const h = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
    h.receipts.set(`onboarding-${status}`, { requestHash, status, result: null });
    const result = await onboardingRequest(h, { method: "POST", body: { action: "home.save", payload }, headers: { "idempotency-key": `onboarding-${status}` } });
    assert.equal(result.response.status, 409, status);
    assert.deepEqual(result.body, { error }, status);
    assert.equal(h.providerReads.length, 0, status);
    assert.equal(h.wrapperCalls.length, 0, status);
  }
});

test("Task 7B R1: failed onboarding transition is durably non-retryable", async () => {
  const h = onboardingHarness({ step: "home", stage: "home", calendarConnected: true });
  h.commandStore.mutateOnboardingWithCalendar = async () => { throw new Error("provider transition failed"); };
  const key = "onboarding-failed-01";
  const first = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" }, headers: { "idempotency-key": key } });
  assert.equal(first.response.status, 502);
  assert.equal(h.receipts.get(key).status, "failed");
  const reads = h.providerReads.length;
  const second = await onboardingRequest(h, { method: "POST", body: { action: "home.save", home_address: "home" }, headers: { "idempotency-key": key } });
  assert.equal(second.response.status, 409);
  assert.deepEqual(second.body, { error: "idempotency_failed" });
  assert.equal(h.providerReads.length, reads);
});

test("Task 7B R1: verified Calendar callback returns onboarding only for incomplete server state", async () => {
  const stateToken = Buffer.alloc(32, 0x71).toString("base64url");
  const run = async (onboarding, expectedLocation, readError = false) => {
    const scope = { uid: "tenant-a", chatId: "101", csrf: "csrf-a" };
    const calls = [];
    const store = {
      async assertCurrentScope(value) { assert.deepEqual(value, scope); return true; },
      async claimOAuthState(value, hash) { calls.push({ type: "claim", value, hash }); return true; },
      async readOnboardingState(value) { calls.push({ type: "onboarding", value }); if (readError) throw new Error("state unavailable"); return onboarding; },
    };
    const response = { status: 0, headers: {}, writeHead(status, headers) { this.status = status; this.headers = headers || {}; }, end() {} };
    await handlePanelOAuthCallback({ method: "GET", url: `/panel/oauth/calendar?state=${stateToken}`, headers: { cookie: "" } }, response, {
      sessionScopeImpl: async () => scope,
      commandStore: store,
      composioCalendarStatusImpl: async () => "ACTIVE",
    });
    assert.equal(response.status, 303);
    assert.equal(response.headers.Location, expectedLocation);
    return calls;
  };
  const incomplete = await run({ step: "home" }, "/panel/onboarding");
  assert.equal(incomplete.filter((call) => call.type === "onboarding").length, 1);
  await run({ step: "dashboard" }, "/panel");
  await run({ step: "done" }, "/panel");
  await run({ step: "home" }, "/panel", true);
});

test("Task 7A rejects malformed JSON arrays, primitives, and payloads before provider/RPC", async () => {
  for (const malformed of [[], "not-an-object", 7, null, { action: "home.save", payload: [] }, { action: "home.save", payload: "home" }, { action: "home.save", extra: "home" }]) {
    const h = onboardingHarness({ step: "home", stage: "home" });
    const result = await onboardingRequest(h, { method: "POST", path: "/api/panel/onboarding/home/save", body: malformed });
    assert.equal(result.response.status, 400, String(malformed));
    assert.equal(h.writes.length, 0);
    assert.deepEqual(h.reads, [], "malformed body must not invoke state RPC");
    assert.deepEqual(h.providerReads, [], "malformed body must not query provider");
    assert.deepEqual(h.syncs, [], "malformed body must not sync provider");
  }
});

test("Task 7A unpaid dashboard remains checkout-reachable without granting paid", async () => {
  const h = onboardingHarness({ step: "dashboard", stage: "done", calendarConnected: true, paid: false });
  const result = await onboardingRequest(h);
  assert.equal(result.response.status, 200);
  assert.equal(result.body.step, "dashboard");
  assert.equal(result.body.paid, false);
  assert.equal(result.body.paymentLink, "https://buy.stripe.com/test_life_manager?client_reference_id=tenant-a");
});

test("Task 3 ready dashboard returns server trial truth and optional checkout", async () => {
  const h = onboardingHarness({
    step: "dashboard",
    stage: "done",
    paid: false,
    trialExpiresAt: "2026-08-31T12:00:00.000Z",
    trialActive: true,
  });
  const result = await onboardingRequest(h);
  assert.equal(result.response.status, 200);
  assert.equal(result.body.step, "dashboard");
  assert.equal(result.body.trialExpiresAt, "2026-08-31T12:00:00.000Z");
  assert.equal(result.body.trialActive, true);
  assert.equal(result.body.paymentLink, "https://buy.stripe.com/test_life_manager?client_reference_id=tenant-a");

  h.opts.stripePaymentLink = "";
  const withoutCheckout = await onboardingRequest(h);
  assert.equal(withoutCheckout.response.status, 200);
  assert.equal(withoutCheckout.body.step, "dashboard");
  assert.equal(withoutCheckout.body.paymentLink, undefined);
});

test("Task 3 ready dashboard previews the first future calendar event and degrades to null", async () => {
  const h = onboardingHarness({ step: "dashboard", stage: "done", paid: false });
  h.opts.supaUrl = "https://db.example";
  h.opts.supaKey = "service-key";
  h.opts.nowMs = Date.parse("2026-08-28T12:00:00.000Z");
  h.opts.timeZone = "UTC";
  h.opts.fetchImpl = async (input) => {
    const url = new URL(input);
    if (url.pathname.endsWith("/lm_panel_preferences")) return jsonResponse([{ call_time_zone: "UTC" }]);
    if (url.pathname.endsWith("/lm_users")) return jsonResponse([]);
    if (url.pathname.endsWith("/lm_wake_log")) return jsonResponse([]);
    throw new Error(`unexpected timeline URL ${url}`);
  };
  h.opts.calendar = {
    listEventsRaw: async () => [
      { id: "past", summary: "過去の予定", start: { dateTime: "2026-08-28T10:00:00.000Z" } },
      { id: "travel-helper", summary: "[Travel] Dentist", start: { dateTime: "2026-08-28T13:00:00.000Z" } },
      { id: "pending-helper", summary: "[PENDING] Dentist", start: { dateTime: "2026-08-28T13:10:00.000Z" } },
      { id: "applied-helper", summary: "[APPLIED] Dentist", start: { dateTime: "2026-08-28T13:20:00.000Z" } },
      { id: "next", summary: "Dentist", start: { dateTime: "2026-08-28T14:00:00.000Z" } },
    ],
  };
  const result = await onboardingRequest(h);
  assert.equal(result.response.status, 200);
  assert.deepEqual(result.body.nextEvent, { summary: "Dentist", startAt: "2026-08-28T14:00:00.000Z" });

  h.opts.fetchImpl = async () => { throw new Error("timeline unavailable"); };
  const degraded = await onboardingRequest(h);
  assert.equal(degraded.response.status, 200);
  assert.equal(degraded.body.step, "dashboard");
  assert.equal(degraded.body.nextEvent, null);
});

test("Task 7A onboarding migration is additive, tenant-scoped, and lock-atomic", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-08-28-lm-trial-first.sql"), "utf8");
  assert.match(sql, /CREATE OR REPLACE FUNCTION public\.lm_panel_onboarding_state/i);
  assert.match(sql, /CREATE OR REPLACE FUNCTION public\.lm_panel_onboarding_transition/i);
  assert.match(sql, /SELECT .*FROM public\.lm_users[\s\S]*FOR UPDATE/i);
  assert.match(sql, /telegram_chat_id::text\s*=\s*p_chat_id/i);
  assert.match(sql, /paid\s+IS\s+TRUE/i);
  assert.doesNotMatch(sql, /CREATE TABLE/i);
  const transition = sql.slice(sql.indexOf("CREATE OR REPLACE FUNCTION public.lm_panel_onboarding_transition"));
  assert.doesNotMatch(transition, /SET\s+paid\s*=/i, "client transitions cannot write paid");
  assert.match(transition, /call_enabled\s*=\s*false/i, "phone and notification transitions keep calls off");
  assert.match(transition, /p_action\s*=\s*'phone\.skip'[\s\S]*call_enabled\) VALUES \(p_uid, false\)/i);
  assert.match(transition, /p_action\s*=\s*'phone\.save'[\s\S]*call_enabled\) VALUES \(p_uid, false\)/i);
  assert.match(transition, /p_action\s*=\s*'call\.enable'[\s\S]*call_enabled\) VALUES \(p_uid, true\)/i);
  assert.match(sql, /REVOKE ALL ON FUNCTION public\.lm_panel_onboarding_transition/i);
});

test("R1A2 combined onboarding transition wraps Calendar sync and rollback in one RPC", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-08-27-lm-panel-onboarding-reachability.sql"), "utf8");
  assert.match(sql, /CREATE OR REPLACE FUNCTION public\.lm_panel_onboarding_transition_with_calendar/i);
  assert.match(sql, /sync_lm_panel_calendar_status[\s\S]*lm_panel_onboarding_transition/i);
  assert.match(sql, /LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp/i);
  assert.match(sql, /GRANT EXECUTE ON FUNCTION public\.lm_panel_onboarding_transition_with_calendar/i);
});

test("Task 7A state resumes for another session of the same actor and isolates another actor", async () => {
  const first = onboardingHarness({ step: "phone", stage: "phone", phone: null });
  const resumed = onboardingHarness({ step: "phone", stage: "phone", phone: null });
  assert.deepEqual((await onboardingRequest(first)).body, (await onboardingRequest(resumed)).body);
  const other = onboardingHarness({ step: "dashboard", stage: "dashboard", paid: true });
  other.opts.sessionScopeImpl = async () => ({ uid: "tenant-b", chatId: "202", csrf: "csrf-b" });
  other.opts.commandStore.assertCurrentScope = async () => false;
  const denied = await onboardingRequest(other, { headers: { "x-lm-csrf": "csrf-b" } });
  assert.equal(denied.response.status, 401);
  assert.equal(denied.body.error, "unauthorized");
});
