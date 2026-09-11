// test/telegram-slash-http-contract.test.js — spec §12.1 row 4: slash-command routing through the
// REAL server.js webhook with a fake transport (no Telegram token, no network sends).
//
// Ordering contract under test, in webhook order:
//   payout typed intake → feedback intake → panel (incl. device-code confirm) → SLASH ROUTER →
//   location → parsed-control commands (/connect alias) → browser task → onboarding.
// The fake fetch THROWS on any unexpected host, so a mis-ordered slash command that fell into the
// browser-task classifier (Gemini) or any other branch fails the test physically, not rhetorically.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test("POST /telegram routes the legacy-parity slash surface without disturbing earlier branches", async () => {
  process.env.LM_TELEGRAM_BOT_TOKEN = "fixture-token";
  process.env.LM_TELEGRAM_WEBHOOK_SECRET = "fixture-webhook-secret";
  process.env.LM_FEEDBACK_PROVENANCE_KEY = "fixture-feedback-provenance";
  process.env.SUPABASE_URL = "https://fixture.supabase.co";
  process.env.SUPABASE_SERVICE_ROLE_KEY = "fixture-service-role";
  process.env.LIFE_RUN_LOOPS = "false";
  process.env.PUBLIC_BASE = "https://lm.test";
  process.env.LM_PANEL_BASE_URL = "https://panel.test/ignored-path";
  process.env.COMPOSIO_API_KEY = "fixture-composio-key";
  process.env.COMPOSIO_GCAL_AUTH_CONFIG = "fixture-calendar-auth";
  process.env.LM_TELEGRAM_BOT_USERNAME = "LifeManagerBotbot";
  process.env.LM_STRIPE_PAYMENT_LINK = "https://buy.stripe.com/test_life_manager";
  process.env.LM_CLOUD_CITIZEN_ENCRYPTION_KEY = Buffer.alloc(32, 9).toString("base64");
  // Browser tasks ON: if slash routing ever fell through to this branch for the paid+done fixture
  // user, the classifier would call Gemini and the fake fetch below would throw.
  process.env.LM_BROWSER_TASKS_ENABLED = "1";
  delete process.env.RAILWAY_PUBLIC_DOMAIN;

  const originalCreateServer = http.createServer;
  const originalFetch = global.fetch;
  const pg = require("pg");
  const OriginalPool = pg.Pool;
  let productionServer;
  http.createServer = (handler) => {
    productionServer = originalCreateServer(handler);
    return productionServer;
  };

  // Mutable tenant fixtures. Chat 100 is linked to u1; chat 200 is unlinked.
  const userRow = {
    uid: "u1", name: "Fixture", telegram_chat_id: "100", tg_onboard_stage: "done",
    calendar_provider: "composio_gcal", gmail_account_id: null, gmail_skipped: true,
    calendar_connected_account_id: "ca-u1",
    email: "fixture@example.com", phone: "+819012345678", home_address: "Tokyo home",
    notifications_enabled: true, paid: true, payout_destination: null,
  };
  let startedRow = null;
  const commandReceipts = new Map();
  const actorClaimHashes = new Set();
  let actorClaims = 0;
  let telegramCalendarCallback = null;
  let calendarActive = false;
  const locationStore = new Map([
    ["u-other", { uid: "u-other", latitude: 1.5, longitude: 2.5, observed_at: "2026-07-30T00:00:00.000Z", expires_at: "2099-01-01T00:00:00.000Z" }],
  ]);
  const sent = [];
  const userPatches = [];
  const feedbackRows = [];
  const investmentReads = [];
  const runtimeJobs = new Map();
  const cloudCitizens = new Map();
  let telegramSendOk = true;
  const logs = [];
  const originalConsoleLog = console.log;
  console.log = (...args) => logs.push(args.map(String).join(" "));
  pg.Pool = class FixturePool {
    query(sql, values) {
      if (/provision_lm_cloud_citizen/.test(sql)) {
        const [tenantId, citizenId, instanceId, walletAddress] = values;
        const existing = cloudCitizens.get(tenantId);
        if (existing) return Promise.resolve({ rows: [{ ...existing, created: false }] });
        const row = { tenant_id: tenantId, citizen_id: citizenId, instance_id: instanceId,
          wallet_address: walletAddress, agent_economy_paused_at: null };
        cloudCitizens.set(tenantId, row);
        return Promise.resolve({ rows: [{ ...row, created: true }] });
      }
      if (/FROM public\.lm_cloud_citizens AS c/.test(sql)) {
        const citizen = cloudCitizens.get(values[0]);
        const job = [...runtimeJobs.values()].find((value) => value.tenant_id === values[0]);
        return Promise.resolve({ rows: citizen ? [{ ...citizen,
          ...(job ? { job_id: job.job_id, job_status: "queued", input_refs: job.input_refs,
            available_at: "2026-09-11T00:00:00.000Z" } : {}),
        }] : [] });
      }
      if (/UPDATE public\.lm_cloud_citizens/.test(sql)) {
        const citizen = cloudCitizens.get(values[0]);
        if (!citizen) return Promise.resolve({ rows: [] });
        citizen.agent_economy_paused_at ||= "2026-09-11T00:00:00.000Z";
        return Promise.resolve({ rows: [{ agent_economy_paused_at: citizen.agent_economy_paused_at }] });
      }
      if (/FROM public\.lm_investment_states/.test(sql)) {
        investmentReads.push(values[0]);
        return Promise.resolve({ rows: values[0] === "u1" ? [{
        uid: "u1", lifecycle: "in_review", deployment: "cloud", mode: "paper",
        paused: false, killed: false, core_digest: null, receipt_refs: [],
        alpaca_api_key_ref: null, alpaca_api_secret_ref: null,
        }] : [] });
      }
      if (/INSERT INTO public\.lm_runtime_jobs/.test(sql)) {
        const [jobId, tenantId, loopId, capability, effectClass, effectKey, refs, maxAttempts] = values;
        if (runtimeJobs.has(jobId)) return Promise.resolve({ rows: [] });
        const row = { job_id: jobId, tenant_id: tenantId, loop_id: loopId, capability,
          effect_class: effectClass, effect_key: effectKey, input_refs: JSON.parse(refs), max_attempts: maxAttempts };
        runtimeJobs.set(jobId, row);
        return Promise.resolve({ rows: [row] });
      }
      if (/SELECT \* FROM public\.lm_runtime_jobs/.test(sql)) {
        const row = runtimeJobs.get(values[0]);
        return Promise.resolve({ rows: row && row.tenant_id === values[1] ? [row] : [] });
      }
      throw new Error("unexpected runtime query");
    }
  };
  process.env.LM_RUNTIME_DATABASE_URL = "postgresql://fixture.invalid/runtime";

  global.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const method = String(init.method || "GET").toUpperCase();
    if (url.hostname === "api.telegram.org") {
      if (/sendMessage$/.test(url.pathname)) {
        sent.push(JSON.parse(init.body));
        return response(200, telegramSendOk
          ? { ok: true, result: { message_id: 9000 + sent.length } }
          : { ok: false, error_code: 400, description: "token=fixture-token chat_id=200" });
      }
      if (/answerCallbackQuery$/.test(url.pathname)) return response(200, { ok: true, result: true });
      if (/editMessageText$/.test(url.pathname)) {
        sent.push(JSON.parse(init.body));
        return response(200, { ok: true, result: { message_id: 9000 + sent.length } });
      }
      throw new Error(`unexpected telegram call ${url.pathname}`);
    }
    if (url.pathname === "/rest/v1/lm_users" && method === "GET") {
      const uid = String(url.searchParams.get("uid") || "").replace(/^eq\./, "");
      if (uid) {
        if (uid === "u1") return response(200, [{ ...userRow }]);
        if (startedRow && uid === startedRow.uid) return response(200, [{ ...startedRow }]);
        return response(200, []);
      }
      const chat = String(url.searchParams.get("telegram_chat_id") || "").replace(/^eq\./, "");
      return response(200, chat === "100" ? [{ ...userRow }] : startedRow && chat === startedRow.telegram_chat_id ? [{ ...startedRow }] : []);
    }
    if (url.pathname === "/rest/v1/lm_users" && method === "PATCH") {
      const uid = String(url.searchParams.get("uid") || "").replace(/^eq\./, "");
      const patch = JSON.parse(init.body || "{}");
      userPatches.push({ uid, patch });
      if (uid === "u1") Object.assign(userRow, patch);
      return response(200, []);
    }
    if (url.pathname === "/rest/v1/lm_user_locations" && method === "GET") {
      const uid = String(url.searchParams.get("uid") || "").replace(/^eq\./, "");
      return response(200, locationStore.has(uid) ? [locationStore.get(uid)] : []);
    }
    if (url.pathname === "/rest/v1/lm_user_locations" && method === "POST") {
      const row = JSON.parse(init.body || "{}");
      locationStore.set(row.uid, row);
      return response(201, []);
    }
    if (url.pathname === "/rest/v1/lm_user_locations" && method === "DELETE") {
      const uid = String(url.searchParams.get("uid") || "").replace(/^eq\./, "");
      assert.ok(uid, "an unfiltered location DELETE must never be issued");
      const removed = locationStore.has(uid) ? [locationStore.get(uid)] : [];
      locationStore.delete(uid);
      return response(200, removed);
    }
    if (url.pathname === "/rest/v1/lm_feedback_intake" && method === "POST") {
      feedbackRows.push(JSON.parse(init.body || "{}"));
      return response(201, [{ id: `feedback-${feedbackRows.length}` }]);
    }
    if (url.pathname === "/rest/v1/lm_panel_device_challenges" && method === "POST") {
      return response(201, []);
    }
    if (url.pathname === "/rest/v1/rpc/claim_lm_panel_telegram_init_v2" && method === "POST") {
      actorClaims += 1;
      const body = JSON.parse(init.body || "{}");
      if (actorClaimHashes.has(body.p_init_hash)) return response(200, [{ status: "replayed", uid: null, chat_id: null }]);
      actorClaimHashes.add(body.p_init_hash);
      startedRow = {
        uid: "u-started", name: body.p_profile_name, telegram_chat_id: body.p_actor_id,
        tg_onboard_stage: "calendar", calendar_provider: null, phone: null,
        notifications_enabled: true, paid: false,
      };
      return response(200, [{ status: "claimed", uid: startedRow.uid, chat_id: startedRow.telegram_chat_id }]);
    }
    if (url.pathname === "/rest/v1/rpc/provision_lm_cloud_citizen" && method === "POST") {
      const body = JSON.parse(init.body || "{}");
      const existing = cloudCitizens.get(body.p_tenant_id);
      if (existing) return response(200, [{ ...existing, created: false }]);
      const row = { tenant_id: body.p_tenant_id, citizen_id: body.p_citizen_id,
        instance_id: body.p_instance_id, wallet_address: body.p_wallet_address };
      cloudCitizens.set(body.p_tenant_id, row);
      return response(200, [{ ...row, created: true }]);
    }
    if (url.pathname === "/rest/v1/lm_panel_command_receipts" && method === "GET") {
      const key = String(url.searchParams.get("idempotency_key") || "").replace(/^eq\./, "");
      return response(200, commandReceipts.has(key) ? [commandReceipts.get(key)] : []);
    }
    if (url.pathname === "/rest/v1/lm_panel_command_receipts" && method === "POST") {
      const body = JSON.parse(init.body || "{}");
      commandReceipts.set(body.idempotency_key, body);
      return response(201, []);
    }
    if (url.pathname === "/rest/v1/lm_panel_command_receipts" && method === "PATCH") {
      const key = String(url.searchParams.get("idempotency_key") || "").replace(/^eq\./, "");
      commandReceipts.set(key, { ...commandReceipts.get(key), ...JSON.parse(init.body || "{}") });
      return response(200, []);
    }
    if (url.pathname === "/rest/v1/rpc/create_lm_panel_oauth_state" && method === "POST") {
      return response(200, true);
    }
    if (url.pathname === "/rest/v1/rpc/create_lm_telegram_oauth_state" && method === "POST") {
      return response(200, true);
    }
    if (url.pathname === "/rest/v1/rpc/attach_lm_panel_oauth_account" && method === "POST") {
      return response(200, true);
    }
    if (url.pathname === "/rest/v1/rpc/claim_lm_telegram_oauth_state" && method === "POST") {
      return response(200, startedRow ? [{ uid: startedRow.uid, chat_id: startedRow.telegram_chat_id, connected_account_id: "ca-started" }] : []);
    }
    if (url.pathname === "/rest/v1/rpc/sync_lm_panel_calendar_connection" && method === "POST") {
      if (startedRow) Object.assign(startedRow, { calendar_provider: "composio_gcal", calendar_connected_account_id: "ca-started" });
      return response(200, true);
    }
    if (url.pathname === "/rest/v1/rpc/sync_lm_panel_calendar_status" && method === "POST") {
      if (startedRow) startedRow.calendar_provider = "composio_gcal";
      return response(200, true);
    }
    if (url.hostname === "backend.composio.dev" && url.pathname === "/api/v3/connected_accounts" && method === "GET") {
      return response(200, { items: calendarActive && startedRow ? [{ id: "ca-started", user_id: startedRow.uid, toolkit: { slug: "googlecalendar" }, status: "ACTIVE", is_disabled: false, enabled: true }] : [] });
    }
    if (url.hostname === "backend.composio.dev" && url.pathname === "/api/v3/connected_accounts/link" && method === "POST") {
      telegramCalendarCallback = JSON.parse(init.body || "{}").callback_url;
      return response(200, { redirect_url: "https://accounts.google.com/o/oauth2/auth?state=fixture", connected_account_id: "ca-started" });
    }
    if (url.hostname === "backend.composio.dev" && url.pathname === "/api/v3.1/connected_accounts/ca-started" && method === "GET") {
      return response(200, { id: "ca-started", user_id: startedRow.uid, toolkit: { slug: "googlecalendar" }, status: calendarActive ? "ACTIVE" : "INITIATED", is_disabled: false, enabled: true });
    }
    if (url.hostname === "backend.composio.dev" && url.pathname === "/api/v3.1/tools/execute/GOOGLECALENDAR_EVENTS_LIST" && method === "POST") {
      return response(200, { successful: true, data: { items: [{ id: "event-1" }] } });
    }
    throw new Error(`unexpected fetch ${method} ${url}`);
  };

  try {
    const serverPath = require.resolve("../server.js");
    delete require.cache[serverPath];
    require(serverPath);
    assert.ok(productionServer, "the production HTTP server must be captured");
    await new Promise((resolve) => productionServer.listen(0, "127.0.0.1", resolve));
    const origin = `http://127.0.0.1:${productionServer.address().port}`;

    const onboardingPage = await new Promise((resolve, reject) => {
      http.get(`${origin}/panel/onboarding`, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body }));
      }).on("error", reject);
    });
    assert.equal(onboardingPage.status, 200);
    assert.match(String(onboardingPage.headers["content-type"] || ""), /text\/html/);
    assert.match(onboardingPage.body, /data-panel-login/);

    let updateId = 9100;
    const post = (payload) => new Promise((resolve, reject) => {
      const body = JSON.stringify({ update_id: ++updateId, ...payload });
      const request = http.request(`${origin}/telegram`, {
        method: "POST",
        headers: {
          "content-type": "application/json", "content-length": Buffer.byteLength(body),
          "x-telegram-bot-api-secret-token": "fixture-webhook-secret",
        },
      }, (res) => { res.resume(); res.on("end", () => resolve(res.statusCode)); });
      request.on("error", reject);
      request.end(body);
    });
    const message = (chatId, text) => post({ message: {
      message_id: updateId + 500, date: Math.floor(Date.now() / 1000),
      from: { id: Number(chatId), first_name: "Fixture", language_code: "ja" }, chat: { id: Number(chatId) }, text,
    } });
    const exactMessage = (exactUpdateId, chatId, text, actorId = chatId) => new Promise((resolve, reject) => {
      const body = JSON.stringify({ update_id: exactUpdateId, message: {
        message_id: exactUpdateId + 500, date: Math.floor(Date.now() / 1000),
        from: { id: Number(actorId), first_name: "Fixture", language_code: "ja" },
        chat: { id: Number(chatId) }, text,
      } });
      const request = http.request(`${origin}/telegram`, {
        method: "POST",
        headers: {
          "content-type": "application/json", "content-length": Buffer.byteLength(body),
          "x-telegram-bot-api-secret-token": "fixture-webhook-secret",
        },
      }, (res) => { res.resume(); res.on("end", () => resolve(res.statusCode)); });
      request.on("error", reject);
      request.end(body);
    });
    const telegramCallback = (chatId, data) => post({ callback_query: {
      id: `cb-${updateId}`, from: { id: Number(chatId) }, data,
      message: { message_id: updateId + 700, chat: { id: Number(chatId) }, text: "Agent Economy" },
    } });
    const lastSent = () => sent[sent.length - 1];

    // 1. Unknown /command → honest unknown reply; never feedback, never onboarding, never a browser
    //    task (the fixture user is paid+done with LM_BROWSER_TASKS_ENABLED=1, so a fall-through
    //    would hit the Gemini classifier and the fake fetch would throw → no reply).
    assert.equal(await message("100", "/frobnicate now"), 200);
    assert.equal(sent.length, 1);
    assert.equal(String(lastSent().chat_id), "100");
    assert.match(lastSent().text, /Unknown command: \/frobnicate/);
    assert.match(lastSent().text, /\/help/);
    assert.equal(feedbackRows.length, 0);
    assert.equal(userPatches.length, 0);

    // 2. /help → the command list, including the previously dropped kind:"help" NL actions.
    assert.equal(await message("100", "/help"), 200);
    for (const expected of ["/status", "/where", "/stop", "/subscribe", "/connect", "/payout", "/reset", "/invest", "connect calendar"]) {
      assert.ok(lastSent().text.includes(expected), `/help must list ${expected}`);
    }

    // 2b. /invest crosses the real authenticated webhook + tenant lookup + shared renderer +
    //     Telegram transport. It reads this exact tenant's Cloud Investment state and records the
    //     provider's message id. The fake fetch
    //     rejects every unknown host, proving this path contacted neither Alpaca nor a scheduler.
    const investBefore = sent.length;
    assert.equal(await message("100", "/invest"), 200);
    assert.equal(sent.length, investBefore + 1, "one /invest update produces exactly one provider send");
    assert.equal(String(lastSent().chat_id), "100");
    assert.match(lastSent().text, /^Investment Loop/m);
    assert.match(lastSent().text, /審査中/);
    assert.deepEqual(investmentReads, ["u1"]);
    assert.equal(lastSent().reply_markup, undefined);
    assert.ok(logs.some((line) => /command=invest .*provider_message_id=\d+/.test(line)),
      "the authenticated E2E must retain Telegram's provider message id");

    // An unlinked chat cannot reach Investment state or inherit the linked tenant's signup reply.
    const unlinkedBefore = sent.length;
    assert.equal(await message("200", "/invest"), 200);
    assert.equal(sent.length, unlinkedBefore + 1);
    assert.equal(lastSent().text, "Complete Life Manager setup with /start before using /invest.");
    assert.equal(lastSent().reply_markup, undefined);
    assert.deepEqual(investmentReads, ["u1"], "an unlinked chat must never read another tenant's Investment state");

    // 3. Ordering vs the typed payout-address intake: a pending awaiting_address intake must NOT
    //    swallow a slash command (and the slash reply must not be the address-rejection copy).
    userRow.payout_destination = { type: "wallet", status: "awaiting_address" };
    assert.equal(await message("100", "/help"), 200);
    assert.match(lastSent().text, /Commands:/);
    assert.ok(!/walletアドレス/.test(lastSent().text), "the intake's rejection copy must not answer a slash command");
    assert.equal(userPatches.length, 0, "the pending intake must not consume the slash message");
    userRow.payout_destination = null;

    // 4. Ordering vs feedback: "feedback: ..." text (even mentioning a /command) stays feedback.
    assert.equal(await message("100", "feedback: /where seems broken"), 200);
    assert.equal(feedbackRows.length, 1);
    assert.match(lastSent().text, /feedback was recorded/i);

    // 5. Ordering vs panel: /panel is still owned by the panel branch and sends its canonical
    //    authenticated web_app button, never the unknown-command reply.
    assert.equal(await message("100", "/panel"), 200);
    assert.deepEqual(lastSent().reply_markup.inline_keyboard[0][0].web_app, {
      url: "https://panel.test/panel",
    });

    // 6. /start stays in Telegram and exposes only Google's consent URL. A deep-link payload:
    //    core.telegram.org/bots/features → Deep Linking says "https://t.me/your_bot?start=airplane"
    //    delivers "/start airplane" (groups deliver "/start@your_bot spaceship"), i.e. the payload
    //    is always space-separated. The production branch must not fall back to the legacy ?tg= URL.
    assert.equal(await message("300", "/start"), 200);
    assert.match(lastSent().text, /ライフマネージャー/);
    const onboardingButton = lastSent().reply_markup.inline_keyboard[0][0];
    assert.equal(onboardingButton.url, "https://accounts.google.com/o/oauth2/auth?state=fixture");
    assert.equal(Object.hasOwn(onboardingButton, "web_app"), false);
    assert.doesNotMatch(JSON.stringify(onboardingButton), /\?tg=|300|token/i);
    assert.equal(actorClaims, 1);
    const sentBeforeReplay = sent.length;
    assert.equal(await exactMessage(9108, "300", "/start"), 200);
    assert.equal(sent.length, sentBeforeReplay, "the same Telegram update never sends twice");
    assert.equal(await message("300", "/start airplane"), 200);
    assert.equal(lastSent().reply_markup.inline_keyboard[0][0].url, "https://accounts.google.com/o/oauth2/auth?state=fixture");
    assert.equal(await message("300", "/start@Bot payload"), 200);
    assert.equal(lastSent().reply_markup.inline_keyboard[0][0].url, "https://accounts.google.com/o/oauth2/auth?state=fixture");
    assert.equal(actorClaims, 4, "each new update is fenced while the existing tenant is reused");
    assert.equal(cloudCitizens.size, 1, "all /start updates reuse one Cloud citizen");
    assert.equal(runtimeJobs.size, 1, "all /start updates reuse one Agent Economy start job");
    assert.equal(await message("300", "/economy"), 200);
    assert.match(lastSent().text, /Agent Economy/);
    assert.equal(lastSent().reply_markup.inline_keyboard[0][0].callback_data, "economy:pause");
    assert.equal(await telegramCallback("300", "economy:pause"), 200);
    assert.match(lastSent().text, /Status: paused/);
    assert.equal(lastSent().reply_markup, undefined, "a paused card cannot enqueue a second pause");
    assert.equal(await exactMessage(9199, "300", "/start", "999"), 200);
    assert.equal(sent.length, sentBeforeReplay + 4, "a different actor in the existing chat cannot start onboarding");
    assert.equal(actorClaims, 4, "cross-actor input is rejected before the claim RPC");
    assert.equal(await message("300", "/start panel"), 200);
    assert.ok(lastSent().reply_markup.inline_keyboard[0][0].web_app, "/start panel remains owned by the panel deep-link route");
    assert.ok(telegramCalendarCallback, "the provider receives a Telegram callback URL");
    const callback = new URL(telegramCalendarCallback);
    assert.equal(callback.pathname, "/telegram/oauth/calendar");
    assert.equal(callback.searchParams.get("lang"), "ja");
    assert.equal(callback.searchParams.has("uid"), false);
    assert.equal(callback.searchParams.has("chat_id"), false);
    calendarActive = true;
    const callbackResult = await new Promise((resolve, reject) => {
      http.get(`${origin}${callback.pathname}${callback.search}`, (res) => {
        res.resume(); res.on("end", () => resolve({ status: res.statusCode, location: res.headers.location }));
      }).on("error", reject);
    });
    assert.equal(callbackResult.status, 303);
    assert.equal(callbackResult.location, "https://t.me/LifeManagerBotbot");
    assert.equal(String(lastSent().chat_id), "300");
    assert.match(lastSent().text, /自宅の住所/);
    assert.equal(await message("300", "/start-foo"), 200);
    assert.match(lastSent().text, /Unknown command/);
    assert.ok(!lastSent().reply_markup, "/start-foo must not open onboarding");
    assert.equal(await message("300", "/start?"), 200);
    assert.match(lastSent().text, /Unknown command/);
    assert.ok(!lastSent().reply_markup, "/start? must not open onboarding");
    // 6b. REGRESSION: "/startfoo" is NOT a start. A real deep link can never produce it, so it is
    //     answered as the unknown command it is.
    assert.equal(await message("300", "/startfoo"), 200);
    assert.match(lastSent().text, /Unknown command: \/startfoo/);
    assert.ok(!/Welcome to Life Manager/.test(lastSent().text), "/startfoo must not open onboarding");

    // 7. /where with nothing stored is honest; the edited_message live-location stream then routes
    //    to upsertLiveLocation; /where afterwards reads the stored fix back (age + rounded coords).
    assert.equal(await message("100", "/where"), 200);
    assert.match(lastSent().text, /don't have a fresh live location/i);
    const nowSec = Math.floor(Date.now() / 1000);
    assert.equal(await post({ edited_message: {
      message_id: 41, date: nowSec - 20, edit_date: nowSec - 10,
      from: { id: 100 }, chat: { id: 100 },
      location: { latitude: 35.681236, longitude: 139.767125, live_period: 900 },
    } }), 200);
    const storedFix = locationStore.get("u1");
    assert.ok(storedFix, "the edited_message live location must be upserted for u1");
    assert.equal(storedFix.latitude, 35.681236);
    assert.equal(storedFix.source, "telegram_live_location");
    assert.equal(await message("100", "/where"), 200);
    assert.match(lastSent().text, /35\.68/);
    assert.match(lastSent().text, /139\.77/);
    assert.ok(!lastSent().text.includes("35.681236"), "full precision never echoed");
    assert.match(lastSent().text, /observed: \d+s ago/);

    // 8. /stop deletes ONLY u1's row (tenant-scoped) and the other tenant's fix survives.
    assert.equal(await message("100", "/stop"), 200);
    assert.match(lastSent().text, /deleted/i);
    assert.equal(locationStore.has("u1"), false);
    assert.ok(locationStore.has("u-other"), "another tenant's location must be untouched by /stop");

    // 9. /subscribe: an active subscription is said honestly; an unpaid row gets the existing
    //    onboard-link builder's URL (web /lm hosts the Stripe checkout) — no invented URLs.
    assert.equal(await message("100", "/subscribe"), 200);
    assert.match(lastSent().text, /already active/i);
    userRow.paid = false;
    assert.equal(await message("100", "/subscribe"), 200);
    assert.equal(lastSent().reply_markup.inline_keyboard[0][0].url, "https://buy.stripe.com/test_life_manager?client_reference_id=u1");
    userRow.paid = true;

    // 10. /reset reuses setStage, confirms, and DISCLOSES that rewinding tg_onboard_stage also closes
    //     the browser-task gate (lib/browser-task-intake.js requires the column to read "done").
    assert.equal(await message("100", "/reset"), 200);
    assert.deepEqual(userPatches[userPatches.length - 1], { uid: "u1", patch: { tg_onboard_stage: "calendar" } });
    assert.match(lastSent().text, /\/start/);
    assert.match(lastSent().text, /browser task/i);
    userRow.tg_onboard_stage = "done";

    // 11. /payout reopens the picker when no destination exists, and answers "already registered"
    //     (no second picker) when one does — idempotent vs the callback flow.
    assert.equal(await message("100", "/payout"), 200);
    const picker = lastSent();
    assert.equal(picker.reply_markup.inline_keyboard[0][0].callback_data, "payout:answer:bank");
    userRow.payout_destination = { type: "bank", status: "awaiting_details", answered_at: "2026-07-30T00:00:00.000Z" };
    const beforeRepeat = sent.length;
    assert.equal(await message("100", "/payout"), 200);
    assert.equal(sent.length, beforeRepeat + 1, "exactly one reply, no duplicate picker");
    assert.match(lastSent().text, /送金先は登録済み/);
    assert.equal(lastSent().reply_markup, undefined, "no second pending question");
    userRow.payout_destination = null;

    // 12. /connect is an alias into the SAME parsed-control flow as "connect calendar": on an
    //     unlinked chat both spellings produce the command branch's own setup-first copy.
    assert.equal(await message("200", "/connect"), 200);
    assert.equal(lastSent().text, "Complete Life Manager setup with /start before changing settings.");
    assert.equal(await message("200", "connect calendar"), 200);
    assert.equal(lastSent().text, "Complete Life Manager setup with /start before changing settings.");
    // 12b. An argument naming anything OTHER than the calendar declines the alias and is answered
    //      honestly — it must not fall into the calendar flow (nor into the /status branch).
    assert.equal(await message("100", "/connect gmail"), 200);
    assert.match(lastSent().text, /only .*Google Calendar/i);
    assert.ok(!/Life Manager status/.test(lastSent().text), "/connect must never answer with /status");

    // 13. /status projects the row + location state the webhook actually read.
    assert.equal(await message("100", "/status"), 200);
    assert.match(lastSent().text, /Life Manager status/);
    assert.match(lastSent().text, /Onboarding: done/);
    assert.match(lastSent().text, /Calendar: connected/);
    assert.match(lastSent().text, /Location: not available/);

    // 14. Telegram transport failure is observable but the webhook still acknowledges the update so
    //    Telegram does not retry it. The server logs only the generic contract error, never the bot
    //    token, chat id, or provider description returned by Telegram.
    const sentBeforeFailure = sent.length;
    const errors = [];
    const originalConsoleError = console.error;
    console.error = (...args) => errors.push(args.map(String).join(" "));
    telegramSendOk = false;
    try {
      assert.equal(await message("200", "/start"), 200);
    } finally {
      telegramSendOk = true;
      console.error = originalConsoleError;
    }
    assert.equal(sent.length, sentBeforeFailure + 1, "a failed /start delivery is attempted exactly once");
    assert.ok(errors.some((line) => line.includes("Telegram onboarding send failed")));
    assert.doesNotMatch(errors.join("\n"), /fixture-token|chat_id=200|token=|description/i);
    assert.equal(await message("200", "/start"), 200);
    assert.equal(sent.length, sentBeforeFailure + 2, "a new explicit /start recovers after the failed delivery");
    assert.match(lastSent().text, /ライフマネージャー/, "a user without an exactly bound account receives a fresh Calendar link");
    assert.ok(lastSent().reply_markup.inline_keyboard[0][0].url, "the recovery is actionable in the same chat");
  } finally {
    console.log = originalConsoleLog;
    global.fetch = originalFetch;
    pg.Pool = OriginalPool;
    http.createServer = originalCreateServer;
    delete process.env.LM_RUNTIME_DATABASE_URL;
    delete process.env.LM_BROWSER_TASKS_ENABLED;
    delete process.env.LM_PANEL_BASE_URL;
    if (productionServer) await new Promise((resolve) => productionServer.close(resolve));
  }
});
