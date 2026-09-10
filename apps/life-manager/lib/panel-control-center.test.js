"use strict";

const test = require("node:test");
const assert = require("node:assert");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { parseUserCommand, executeUserCommand, buildControlCenter, claimCalendarOAuthState, validateCommand, disconnectCalendar, startCalendarOAuth } = require("./user-command.js");
const { handlePanelApiRequest, composioCalendarDisconnect, composioCalendarStart, composioCalendarStatus } = require("./panel-api.js");

function fixtureStore() {
  const users = new Map([
    ["u-a", { uid: "u-a", name: "Aiko", telegram_chat_id: "101", phone: "+810000000000", call_language: "ja", wake_policy: "travel-only", calendar_provider: "composio_gcal", gmail_account_id: null, payout_destination: null }],
    ["u-b", { uid: "u-b", name: "Ben", telegram_chat_id: "202", phone: null, call_language: "en", wake_policy: "all-events", calendar_provider: null, gmail_account_id: "stale", payout_destination: { type: "wallet" } }],
  ]);
  const preferences = new Map([
    ["u-a", { call_enabled: true, notifications_enabled: true, daily_automation_enabled: true, delegation_enabled: false, call_time_zone: "Asia/Tokyo" }],
    ["u-b", { call_enabled: false, notifications_enabled: false, daily_automation_enabled: false, delegation_enabled: true, call_time_zone: "Europe/London" }],
  ]);
  const receipts = new Map(), oauth = [], mutations = [];
  return {
    users, preferences, receipts, oauth, mutations,
    async readUser(scope) { const row = users.get(scope.uid); return row && row.telegram_chat_id === scope.chatId ? { ...row } : null; },
    async readPreferences(scope) { return { ...(preferences.get(scope.uid) || {}) }; },
    async readLocation(scope) { return scope.uid === "u-a" ? { observed_at: "2026-07-21T00:00:00Z", expires_at: "2099-01-01T00:00:00Z" } : null; },
    async readReceipt(scope, key) { return receipts.get(`${scope.uid}:${key}`) || null; },
    async claimReceipt(scope, key, value) { const k = `${scope.uid}:${key}`; if (receipts.has(k)) return false; receipts.set(k, value); return true; },
    async finishReceipt(scope, key, value) { receipts.set(`${scope.uid}:${key}`, value); },
    async patchPreferences(scope, patch) { mutations.push({ uid: scope.uid, patch: { ...patch } }); preferences.set(scope.uid, { ...preferences.get(scope.uid), ...patch }); return { ...preferences.get(scope.uid) }; },
    async patchUser(scope, patch) { mutations.push({ uid: scope.uid, patch: { ...patch } }); users.set(scope.uid, { ...users.get(scope.uid), ...patch }); return { ...users.get(scope.uid) }; },
    async createOAuthState(scope, state) { oauth.push({ uid: scope.uid, chatId: scope.chatId, ...state }); },
    async claimOAuthState(scope, stateHash) { const state = oauth.find((item) => item.uid === scope.uid && item.chatId === scope.chatId && item.stateHash === stateHash && !item.used); if (!state) return false; state.used = true; return true; },
  };
}

test("PANEL-0 personalized data differs and capabilities stay honest", async () => {
  const store = fixtureStore();
  const a = await buildControlCenter({ uid: "u-a", chatId: "101" }, { store, nowMs: Date.parse("2026-07-21T01:00:00Z"), calendarStatus: async () => "ACTIVE" });
  const b = await buildControlCenter({ uid: "u-b", chatId: "202" }, { store, nowMs: Date.parse("2026-07-21T01:00:00Z"), calendarStatus: async () => "INACTIVE" });
  assert.equal(a.identity.name, "Life Manager user"); assert.equal(b.identity.name, "Life Manager user"); assert.notDeepEqual(a.settings, b.settings);
  assert.equal(a.connections.calendar.state, "connected"); assert.equal(b.connections.calendar.state, "action_required");
  assert.equal(a.connections.email.state, "unavailable"); assert.equal(b.connections.email.state, "unavailable");
  assert.deepEqual(b.connections.email.actions, []); assert.doesNotMatch(JSON.stringify(a), /Dais/);
});

test("#1085 missing Calendar action uses the clear Connect Calendar label", async () => {
  const model = await buildControlCenter({ uid: "u-b", chatId: "202" }, { store: fixtureStore(), calendarStatus: async () => "INACTIVE" });
  assert.equal(model.connections.calendar.actionLabel, "Connect Calendar");
});

test("PANEL-8h control center preserves an unconfigured call language", async () => {
  const store = fixtureStore();
  store.users.get("u-a").call_language = null;
  const model = await buildControlCenter(
    { uid: "u-a", chatId: "101" },
    { store, calendarStatus: async () => "ACTIVE" },
  );
  assert.equal(model.settings.call_language, null);
});

test("PANEL-0 isolates read, mutation target, OAuth state, and chat scope", async () => {
  const store = fixtureStore();
  await assert.rejects(buildControlCenter({ uid: "u-a", chatId: "202" }, { store }), /scope_mismatch/);
  const result = await executeUserCommand({ uid: "u-a", chatId: "101" }, { type: "setting.set", setting: "notifications_enabled", value: false }, { store, idempotencyKey: "iso-key-0001" });
  assert.equal(result.ok, true); assert.deepEqual(store.mutations, [{ uid: "u-a", patch: { notifications_enabled: false } }]);
  const oauth = await executeUserCommand({ uid: "u-b", chatId: "202" }, { type: "connection.start", provider: "calendar" }, { store, idempotencyKey: "iso-key-0002", randomBytes: () => Buffer.alloc(32, 7), startCalendarOAuth: async (scope, stateToken) => {
    assert.deepEqual(scope, { uid: "u-b", chatId: "202" });
    assert.equal(stateToken, Buffer.alloc(32, 7).toString("base64url"));
    assert.deepEqual(store.oauth.map(({ uid, chatId }) => ({ uid, chatId })), [{ uid: "u-b", chatId: "202" }]);
    return { redirectUrl: "https://provider.example/oauth" };
  } });
  assert.equal(oauth.ok, true); assert.deepEqual(store.oauth.map(({ uid, chatId }) => ({ uid, chatId })), [{ uid: "u-b", chatId: "202" }]);
  const token = Buffer.alloc(32, 7).toString("base64url");
  assert.equal(await claimCalendarOAuthState({ uid: "u-a", chatId: "101" }, token, { store }), false);
  assert.equal(await claimCalendarOAuthState({ uid: "u-b", chatId: "202" }, token, { store }), true);
  assert.equal(await claimCalendarOAuthState({ uid: "u-b", chatId: "202" }, token, { store }), false);
});

test("PANEL-0 bilingual chat and panel commands converge", async () => {
  const cases = [["turn calls off", { type: "setting.set", setting: "call_enabled", value: false }], ["電話を止めて", { type: "setting.set", setting: "call_enabled", value: false }], ["connect calendar", { type: "connection.start", provider: "calendar" }], ["カレンダーをつないで", { type: "connection.start", provider: "calendar" }], ["通知をオン", { type: "setting.set", setting: "notifications_enabled", value: true }]];
  for (const [text, command] of cases) assert.deepEqual(parseUserCommand(text).command, command);
  const ambiguous = parseUserCommand("settings"); assert.equal(ambiguous.kind, "help"); assert.ok(ambiguous.availableActions.length >= 4);
  const panelStore = fixtureStore(), chatStore = fixtureStore(), command = parseUserCommand("電話を止めて").command;
  const panel = await executeUserCommand({ uid: "u-a", chatId: "101" }, command, { store: panelStore, idempotencyKey: "panel-key-1" });
  const chat = await executeUserCommand({ uid: "u-a", chatId: "101" }, command, { store: chatStore, idempotencyKey: "chat-key-1" });
  assert.deepEqual(panel.state, chat.state);
});

test("PANEL-0 Calendar disconnect has deterministic EN/JA chat grammar and allowlist", () => {
  const command = { type: "connection.disconnect", provider: "calendar" };
  for (const text of ["disconnect calendar", "disconnect my google calendar", "カレンダーを切断", "カレンダーを解除して"]) {
    assert.deepEqual(parseUserCommand(text), { kind: "command", command });
  }
  assert.deepEqual(validateCommand(command), command);
});

test("PANEL-0 Calendar disconnect is user-scoped, provider-idempotent, and reads back inactive", async () => {
  const store = fixtureStore(), calls = [];
  const deps = {
    store,
    idempotencyKey: "disconnect-key-1",
    disconnectCalendar: async (scope) => { calls.push({ ...scope }); return { provider: "calendar", state: "action_required" }; },
  };
  const command = { type: "connection.disconnect", provider: "calendar" };
  const first = await executeUserCommand({ uid: "u-a", chatId: "101" }, command, deps);
  const duplicate = await executeUserCommand({ uid: "u-a", chatId: "101" }, command, deps);
  assert.equal(first.ok, true);
  assert.equal(first.state.state, "action_required");
  assert.deepEqual(duplicate, first);
  assert.deepEqual(calls, [{ uid: "u-a", chatId: "101" }]);
  await assert.rejects(executeUserCommand({ uid: "u-a", chatId: "202" }, command, { ...deps, idempotencyKey: "disconnect-key-2" }), /scope_mismatch/);
});

test("PANEL-0 Calendar disconnect provider failure preserves ACTIVE readback", async () => {
  const store = fixtureStore();
  await assert.rejects(executeUserCommand({ uid: "u-a", chatId: "101" }, { type: "connection.disconnect", provider: "calendar" }, {
    store,
    idempotencyKey: "disconnect-fail-1",
    disconnectCalendar: async () => { throw new Error("provider_failed"); },
  }), /provider_failed/);
  const model = await buildControlCenter({ uid: "u-a", chatId: "101" }, { store, calendarStatus: async () => "ACTIVE" });
  assert.equal(model.connections.calendar.state, "connected");
  assert.ok(model.connections.calendar.actions.includes("connection.disconnect:calendar"));
});

test("PANEL-0 Composio disconnect disables only the user account and requires inactive readback", async () => {
  const requests = [], responses = [
    { ok: true, json: async () => ({ items: [{ id: "ca-user-a", user_id: "u-a", toolkit: { slug: "googlecalendar" }, status: "ACTIVE", is_disabled: false, enabled: true }] }) },
    { ok: true, json: async () => ({ id: "ca-user-a", is_disabled: true }) },
    { ok: true, json: async () => ({ items: [{ id: "ca-user-a", user_id: "u-a", toolkit: { slug: "googlecalendar" }, status: "INACTIVE", is_disabled: true, enabled: false }] }) },
  ];
  const result = await composioCalendarDisconnect({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async (url, init = {}) => { requests.push({ url: String(url), init }); return responses.shift(); } });
  assert.deepEqual(result, { provider: "calendar", state: "action_required" });
  assert.match(requests[0].url, /user_ids=u-a/); assert.match(requests[0].url, /toolkit_slugs=googlecalendar/);
  assert.match(requests[1].url, /connected_accounts\/ca-user-a\/status$/);
  assert.equal(requests[1].init.method, "PATCH");
  assert.deepEqual(JSON.parse(requests[1].init.body), { enabled: false });
  assert.match(requests[2].url, /user_ids=u-a/);
});

test("PANEL-0 Composio disconnect fails closed on ambiguous ownership or ACTIVE readback", async () => {
  const response = (items) => ({ ok: true, json: async () => ({ items }) });
  const account = id => ({ id, user_id: "u-a", toolkit: { slug: "googlecalendar" }, status: "ACTIVE", is_disabled: false, enabled: true });
  await assert.rejects(composioCalendarDisconnect({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([account("one"), account("two")]) }), /provider_ambiguous/);
  const sequence = [response([account("one")]), { ok: true, json: async () => ({}) }, response([account("one")]), { ok: true, json: async () => ({}) }, response([account("one")])];
  await assert.rejects(composioCalendarDisconnect({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => sequence.shift() }), /provider_readback_failed/);
});

test("PANEL-0 Composio reconnect enables the scoped inactive account and requires ACTIVE readback", async () => {
  const requests = [], response = (items) => ({ ok: true, json: async () => ({ items }) });
  const sequence = [response([{ id: "ca-user-a", user_id: "u-a", toolkit: { slug: "googlecalendar" }, status: "INACTIVE", is_disabled: true, enabled: false }]), { ok: true, json: async () => ({}) }, response([{ id: "ca-user-a", user_id: "u-a", toolkit: { slug: "googlecalendar" }, status: "ACTIVE", is_disabled: false, enabled: true }])];
  const result = await composioCalendarStart({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async (url, init = {}) => { requests.push({ url: String(url), init }); return sequence.shift(); } });
  assert.deepEqual(result, { provider: "calendar", state: "connected" });
  assert.match(requests[0].url, /user_ids=u-a/);
  assert.equal(requests[1].init.method, "PATCH");
  assert.deepEqual(JSON.parse(requests[1].init.body), { enabled: true });
  assert.match(requests[2].url, /user_ids=u-a/);
});

test("PANEL-0 Calendar ACTIVE follows Composio status and disabled fields, not a legacy enabled field", async () => {
  const response = (items) => ({ ok: true, json: async () => ({ items }) });
  const base = { id: "ca-user-a", user_id: "u-a", toolkit: { slug: "googlecalendar" }, status: "ACTIVE", is_disabled: false };
  assert.equal(await composioCalendarStatus({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([base]) }), "ACTIVE");
  assert.equal(await composioCalendarStatus({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([{ ...base, enabled: false }]) }), "DISABLED");
  assert.equal(await composioCalendarStatus({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([{ ...base, enabled: true }]) }), "ACTIVE");
  assert.equal(await composioCalendarStatus({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([{ ...base, enabled: null }]) }), "DISABLED");
  assert.equal(await composioCalendarStatus({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([{ ...base, enabled: 1 }]) }), "DISABLED");
  assert.equal(await composioCalendarStatus({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async () => response([{ ...base, is_disabled: true, enabled: true }]) }), "DISABLED");
  const calls = [];
  const sequence = [response([{ ...base, status: "INACTIVE", is_disabled: true, enabled: false }]), { ok: true, json: async () => ({}) }, response([{ ...base }])];
  const result = await composioCalendarStart({ uid: "u-a", chatId: "101" }, { composioKey: "test-key", fetchImpl: async (url, init = {}) => { calls.push({ url: String(url), init }); return sequence.shift(); } });
  assert.deepEqual(result, { provider: "calendar", state: "connected" });
  assert.equal(calls.filter((call) => call.init.method === "PATCH").length, 1);
});

test("PANEL-0 expired Composio account starts a fresh auth link instead of enabling the dead session", async () => {
  const calls = [];
  const expired = {
    id: "ca-expired", user_id: "u-a", toolkit: { slug: "googlecalendar" },
    status: "EXPIRED", is_disabled: false,
  };
  const result = await composioCalendarStart({ uid: "u-a", chatId: "101" }, {
    composioKey: "test-key",
    fetchImpl: async (url, init = {}) => {
      calls.push({ url: String(url), init });
      return { ok: true, json: async () => ({ items: [expired] }) };
    },
  });
  assert.equal(result, null, "caller must create a new hosted auth link");
  assert.equal(calls.length, 1, "the expired account must never receive an enable PATCH");
  assert.equal(calls[0].init.method, undefined);
});

test("PANEL-0 concurrent OAuth state claim failure blocks legacy control-center provider link", async () => {
  let providerLinks = 0;
  const store = {
    async readUser() { return { uid: "u-a", telegram_chat_id: "101", calendar_provider: null }; },
    async readReceipt() { return null; },
    async claimReceipt() { return true; },
    async finishReceipt() {},
    async assertCurrentScope() { return true; },
    async createOAuthState() { const error = new Error("oauth_state_in_progress"); error.status = 409; throw error; },
  };
  await assert.rejects(executeUserCommand({ uid: "u-a", chatId: "101" }, { type: "connection.start", provider: "calendar" }, {
    store, idempotencyKey: "oauth-race-01", startCalendarOAuth: async () => { providerLinks++; return { redirectUrl: "https://provider.example/consent" }; },
  }), /oauth_state_in_progress/);
  assert.equal(providerLinks, 0);
});

test("Calendar OAuth state outlives Composio's ten-minute hosted connect link", async () => {
  let expiresAt = null;
  const store = {
    async readUser() { return { uid: "u-a", telegram_chat_id: "101", calendar_provider: null }; },
    async readReceipt() { return null; },
    async claimReceipt() { return true; },
    async finishReceipt() {},
    async assertCurrentScope() { return true; },
    async createOAuthState(_scope, state) { expiresAt = state.expiresAt; },
  };
  const before = Date.now();
  await executeUserCommand({ uid: "u-a", chatId: "101" }, { type: "connection.start", provider: "calendar" }, {
    store,
    idempotencyKey: "oauth-expiry-01",
    randomBytes: () => Buffer.alloc(32, 5),
    startCalendarOAuth: async () => ({ redirectUrl: "https://provider.example/consent" }),
  });
  const ttlMs = Date.parse(expiresAt) - before;
  assert.ok(ttlMs >= 15 * 60 * 1000, `OAuth state TTL was only ${ttlMs}ms`);
  assert.ok(ttlMs <= 15 * 60 * 1000 + 1000);
});

test("PANEL-0 provider disconnect helper contracts are explicit and fail closed", async () => {
  await assert.rejects(disconnectCalendar({ uid: "u-a", chatId: "101" }, {}), /provider_unavailable/);
  assert.deepEqual(await disconnectCalendar({ uid: "u-a", chatId: "101" }, { composioKey: "test", calendarAccount: { disable: async (scope) => ({ scope }) } }), { scope: { uid: "u-a", chatId: "101" } });
});

test("PANEL-0 Composio managed OAuth uses the exact link contract and preserves the provider redirect", async () => {
  const requests = [];
  const stateToken = Buffer.alloc(32, 3).toString("base64url");
  const providerRedirect = "https://provider.example/connect?state=provider-owned&prompt=consent&scope=calendar%20events#continue";
  const oauth = await startCalendarOAuth({ uid: "u-a", chatId: "101" }, stateToken, {
    composioKey: "test", composioAuthConfig: "auth-test", panelBaseUrl: "https://panel.example/",
    fetchImpl: async (url, init) => { requests.push({ url: String(url), init }); return { ok: true, json: async () => ({ redirect_url: providerRedirect }) }; },
  });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "https://backend.composio.dev/api/v3/connected_accounts/link");
  assert.equal(requests[0].init.method, "POST");
  const requestBody = JSON.parse(requests[0].init.body);
  assert.deepEqual(requestBody, {
    auth_config_id: "auth-test",
    user_id: "u-a",
    callback_url: `https://panel.example/panel/oauth/calendar?state=${stateToken}`,
  });
  assert.equal(Object.hasOwn(requestBody, "auth_config"), false);
  assert.equal(Object.hasOwn(requestBody, "connection"), false);
  assert.equal(oauth.redirectUrl, providerRedirect);
});

test("Telegram Calendar OAuth uses its cookie-free callback and carries language only as display metadata", async () => {
  const requests = [];
  const stateToken = Buffer.alloc(32, 4).toString("base64url");
  await startCalendarOAuth({ uid: "u-b", chatId: "202" }, stateToken, {
    composioKey: "test", composioAuthConfig: "auth-test", panelBaseUrl: "https://panel.example/",
    calendarCallbackPath: "/telegram/oauth/calendar", calendarCallbackParams: { lang: "ja-JP" },
    fetchImpl: async (url, init) => { requests.push({ url: String(url), init }); return { ok: true, json: async () => ({ redirect_url: "https://connect.composio.dev/link/opaque" }) }; },
  });
  const callback = new URL(JSON.parse(requests[0].init.body).callback_url);
  assert.equal(callback.pathname, "/telegram/oauth/calendar");
  assert.equal(callback.searchParams.get("state"), stateToken);
  assert.equal(callback.searchParams.get("lang"), "ja-JP");
  assert.equal(callback.searchParams.has("uid"), false);
  assert.equal(callback.searchParams.has("chat_id"), false);
});

test("PANEL-0 Composio OAuth failure is fail-closed without a legacy retry", async (t) => {
  for (const status of [400, 503]) {
    await t.test(`HTTP ${status}`, async () => {
      const requests = [];
      await assert.rejects(startCalendarOAuth({ uid: "u-a", chatId: "101" }, Buffer.alloc(32, 4).toString("base64url"), {
        composioKey: "test", composioAuthConfig: "auth-test", panelBaseUrl: "https://panel.example",
        fetchImpl: async (url) => { requests.push(String(url)); return { ok: false, status, json: async () => ({}) }; },
      }), /provider_failed/);
      assert.deepEqual(requests, ["https://backend.composio.dev/api/v3/connected_accounts/link"]);
    });
  }
});

test("PANEL-0 Composio OAuth redirect data is required and must be usable", async (t) => {
  const cases = [
    ["missing", {}],
    ["malformed", { redirect_url: "not a usable URL" }],
    ["legacy redirect_uri only", { redirect_uri: "https://provider.example/legacy" }],
  ];
  for (const [name, body] of cases) {
    await t.test(name, async () => {
      let calls = 0;
      await assert.rejects(startCalendarOAuth({ uid: "u-a", chatId: "101" }, Buffer.alloc(32, 5).toString("base64url"), {
        composioKey: "test", composioAuthConfig: "auth-test", panelBaseUrl: "https://panel.example",
        fetchImpl: async () => { calls += 1; return { ok: true, json: async () => body }; },
      }), /provider_failed/);
      assert.equal(calls, 1);
    });
  }
});

test("PANEL-0 allowlist, idempotency, and provider rollback", async () => {
  assert.throws(() => validateCommand({ type: "setting.set", setting: "wallet_balance", value: 9 }), /invalid_action/);
  assert.throws(() => validateCommand({ type: "connection.start", provider: "gmail" }), /invalid_action/);
  assert.throws(() => validateCommand({ type: "setting.set", setting: "call_enabled", value: false, uid: "u-b" }), /invalid_action/);
  const store = fixtureStore(), deps = { store, idempotencyKey: "same-key-1234" }, command = { type: "setting.set", setting: "call_enabled", value: false };
  const first = await executeUserCommand({ uid: "u-a", chatId: "101" }, command, deps), second = await executeUserCommand({ uid: "u-a", chatId: "101" }, command, deps);
  assert.deepEqual(second, first); assert.equal(store.mutations.length, 1);
  await assert.rejects(executeUserCommand({ uid: "u-a", chatId: "101" }, { ...command, value: true }, deps), /idempotency_conflict/);
  const failed = fixtureStore(), before = { ...failed.preferences.get("u-a") };
  await assert.rejects(executeUserCommand({ uid: "u-a", chatId: "101" }, { type: "connection.start", provider: "calendar" }, { store: failed, idempotencyKey: "provider-fail-1", startCalendarOAuth: async () => { throw new Error("provider_failed"); } }), /provider_failed/);
  assert.deepEqual(failed.preferences.get("u-a"), before);
});

async function withApi(handlerOpts, run) {
  const server = http.createServer((req, res) => Promise.resolve(handlePanelApiRequest(req, res, handlerOpts)).catch((error) => { res.writeHead(500, { "content-type": "application/json" }); res.end(JSON.stringify({ error: error.message })); }));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try { return await run(`http://127.0.0.1:${server.address().port}`); } finally { await new Promise((resolve) => server.close(resolve)); }
}

test("PANEL-0 POST enforces origin, CSRF, JSON and idempotency", async () => {
  const calls = [], opts = { panelOrigin: "https://life.example", sessionScopeImpl: async () => ({ uid: "u-a", chatId: "101", csrf: "csrf-a" }), executeCommandImpl: async (scope, command, deps) => { calls.push({ scope, command, key: deps.idempotencyKey }); return { ok: true }; } };
  await withApi(opts, async (base) => {
    const body = JSON.stringify({ type: "setting.set", setting: "call_enabled", value: false });
    const request = (headers = {}) => fetch(`${base}/api/panel/commands`, { method: "POST", headers: { "content-type": "application/json", ...headers }, body });
    assert.equal((await request()).status, 403);
    assert.equal((await request({ Origin: "https://evil.example", "x-lm-csrf": "csrf-a", "idempotency-key": "key-0001" })).status, 403);
    assert.equal((await request({ Origin: "https://life.example", "x-lm-csrf": "bad", "idempotency-key": "key-0001" })).status, 403);
    assert.equal((await request({ Origin: "https://life.example", "x-lm-csrf": "csrf-a" })).status, 400);
    assert.equal((await request({ Origin: "https://life.example", "x-lm-csrf": "csrf-a", "idempotency-key": "key-0001" })).status, 200);
  });
  assert.equal(calls.length, 1);
});

test("PANEL-0 migration and rollback are additive and user keyed", () => {
  const migration = fs.readFileSync(path.join(__dirname, "../migrations/2026-07-21-lm-panel-control-center.sql"), "utf8");
  const rollback = fs.readFileSync(path.join(__dirname, "../migrations/2026-07-21-lm-panel-control-center.rollback.sql"), "utf8");
  for (const table of ["lm_panel_preferences", "lm_panel_command_receipts", "lm_panel_oauth_states"]) { assert.match(migration, new RegExp(`CREATE TABLE IF NOT EXISTS public\\.${table}`, "i")); assert.match(rollback, new RegExp(`DROP TABLE IF EXISTS public\\.${table}`, "i")); }
  assert.match(migration, /uid text NOT NULL REFERENCES public\.lm_users\(uid\)/i); assert.doesNotMatch(rollback, /DROP TABLE[^;]*lm_users/i);
});

// spec §5.2.1 + §3 row 2b: the wake call became opt-in (RUNTIME_DEFAULTS.call_enabled = false), but
// buildControlCenter carried its OWN hardcoded `call_enabled: true`. A user with a phone and no
// preference row was therefore told "Calls are enabled" while the scheduler placed none, and the only
// action offered was to turn them OFF — no way to turn on the thing they were told was already on.
// One default, one source: lib/runtime-preferences.js.
const { DEFAULTS: PREF_DEFAULTS } = require("./runtime-preferences.js");

test("a user with no preference row is told calls are off, and offered the switch to turn them on", async () => {
  const scope = { chatId: "555", uid: "u-nopref" };
  const panel = await buildControlCenter(scope, {
    store: {
      readUser: async () => ({ uid: "u-nopref", telegram_chat_id: "555", phone: "+810000000000", calendar_provider: null }),
      readPreferences: async () => ({}), // no row: every value comes from the shared default
      readLocation: async () => null,
    },
    nowMs: 0,
  });
  assert.equal(PREF_DEFAULTS.call_enabled, false, "the shared default is the one being reflected");
  assert.equal(panel.connections.call.state, "action_required");
  assert.match(panel.connections.call.reason, /turned off/);
  assert.deepEqual(panel.connections.call.actions, ["setting.set:call_enabled:true"],
    "the person who now needs to opt in must be offered the way to do it");
});

test("a user who explicitly opted in is still shown as enabled", async () => {
  const panel = await buildControlCenter({ chatId: "556", uid: "u-optin" }, {
    store: {
      readUser: async () => ({ uid: "u-optin", telegram_chat_id: "556", phone: "+810000000000", calendar_provider: null }),
      readPreferences: async () => ({ call_enabled: true }),
      readLocation: async () => null,
    },
    nowMs: 0,
  });
  assert.equal(panel.connections.call.state, "connected");
  assert.match(panel.connections.call.reason, /enabled/);
});
