"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const {
  handleCalendarOnboardRequest,
  deriveCalendarState,
  CALENDAR_STATE_TTL_MS,
  CALENDAR_STATE_RE,
} = require("./calendar-onboard.js");

const ORIGIN = "https://life.example";
const SECRET = "calendar-session-secret";
const SESSION = "session-cookie";
const SCOPE = Object.freeze({ uid: "u-a", chatId: "101", csrf: "csrf-a" });

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function fixture({ active = false, status = active ? "ACTIVE" : "MISSING", redirect = "https://provider.example/consent" } = {}) {
  const calls = { assert: [], start: [], status: [], sync: [], oauth: [], states: [] };
  const store = {
    async readUser() { return { uid: SCOPE.uid, telegram_chat_id: SCOPE.chatId, calendar_connected_account_id: active ? "ca-current" : null }; },
    async assertCurrentScope(scope) {
      calls.assert.push({ ...scope });
      return scope.uid === SCOPE.uid && scope.chatId === SCOPE.chatId;
    },
    async syncCalendarStatus(scope, status) {
      calls.sync.push({ scope: { ...scope }, status });
      return true;
    },
    async createOAuthState(scope, state) {
      calls.states.push({ scope: { ...scope }, state: { ...state } });
      return true;
    },
    async attachOAuthAccount(scope, stateHash, connectedAccountId) {
      calls.states.at(-1).connectedAccountId = connectedAccountId;
      return Boolean(scope.uid === SCOPE.uid && stateHash && connectedAccountId);
    },
  };
  const opts = {
    panelOrigin: ORIGIN,
    panelBaseUrl: ORIGIN,
    sessionSecret: SECRET,
    sessionScopeImpl: async (session) => session === SESSION ? { ...SCOPE } : null,
    commandStore: store,
    composioCalendarStartImpl: async (scope) => {
      calls.start.push({ ...scope });
      return active ? { provider: "calendar", state: "connected" } : null;
    },
    composioCalendarStatusImpl: async (scope) => {
      calls.status.push({ ...scope });
      return status;
    },
    startCalendarOAuthImpl: async (scope, stateToken) => {
      calls.oauth.push({ scope: { ...scope }, stateToken });
      return { redirectUrl: redirect, connectedAccountId: "ca-current" };
    },
  };
  return { calls, store, opts };
}

async function withServer(opts, run) {
  const server = http.createServer((req, res) => Promise.resolve(
    handleCalendarOnboardRequest(req, res, opts),
  ).catch((error) => {
    if (!res.headersSent) res.writeHead(error.status || 500, { "content-type": "application/json" });
    if (!res.writableEnded) res.end(JSON.stringify({ error: "calendar_unavailable" }));
  }));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try { return await run(`http://127.0.0.1:${server.address().port}`); }
  finally { await new Promise((resolve) => server.close(resolve)); }
}

function cookieHeaders(extra = {}) {
  return { cookie: `__Host-lm_panel_session=${SESSION}`, ...extra };
}

test("calendar onboarding exposes the session-scoped request handler", () => {
  assert.equal(typeof handleCalendarOnboardRequest, "function");
  assert.equal(typeof deriveCalendarState, "function");
  assert.ok(CALENDAR_STATE_RE instanceof RegExp);
});

test("start derives the actor only from the verified session and ignores body/query identity", async () => {
  const fixtureState = fixture({ active: true });
  await withServer(fixtureState.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start?uid=u-b&tg=202`, {
      method: "POST",
      headers: cookieHeaders({
        origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf,
      }),
      body: JSON.stringify({ uid: "u-b", tg: "202" }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { connected: true, state: "connected" });
  });
  assert.deepEqual(fixtureState.calls.status.map(({ uid, chatId }) => ({ uid, chatId })), [{ uid: SCOPE.uid, chatId: SCOPE.chatId }]);
  assert.deepEqual(fixtureState.calls.sync.map(({ scope, status }) => ({ uid: scope.uid, chatId: scope.chatId, status })), [{ uid: SCOPE.uid, chatId: SCOPE.chatId, status: "ACTIVE" }]);
  assert.deepEqual(fixtureState.calls.start, []);
  assert.deepEqual(fixtureState.calls.states, []);
  assert.deepEqual(fixtureState.calls.oauth, []);
});

test("R1A a connected start re-syncs ACTIVE after an inactive marker is refreshed", async () => {
  const state = fixture({ active: true, status: "MISSING" });
  await withServer(state.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }), body: "{}",
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { connected: true, state: "connected" });
  });
  assert.deepEqual(state.calls.sync.map(({ status }) => status), ["MISSING", "ACTIVE"]);
});

test("unauthenticated and rebound sessions return 401 before store/provider effects", async () => {
  const unauth = fixture({ active: true });
  unauth.opts.sessionScopeImpl = async () => null;
  await withServer(unauth.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start?uid=u-a&tg=101`, {
      method: "POST", headers: { origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": "bad" },
      body: JSON.stringify({ uid: "u-a", tg: "101" }),
    });
    assert.equal(response.status, 401);
  });
  assert.deepEqual(unauth.calls.assert, []);
  assert.deepEqual(unauth.calls.start, []);
  assert.deepEqual(unauth.calls.oauth, []);

  const rebound = fixture({ active: true });
  rebound.opts.commandStore.assertCurrentScope = async () => false;
  await withServer(rebound.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }),
      body: JSON.stringify({ uid: "u-b", tg: "202" }),
    });
    assert.equal(response.status, 401);
  });
  assert.deepEqual(rebound.calls.start, []);
  assert.deepEqual(rebound.calls.oauth, []);
});

test("start enforces exact origin, panel CSRF, and JSON before effects", async () => {
  const state = fixture();
  await withServer(state.opts, async (base) => {
    const request = (headers = {}, body = "{}") => fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ "content-type": "application/json", ...headers }), body,
    });
    assert.equal((await request({ origin: "https://evil.example", "x-lm-csrf": SCOPE.csrf })).status, 403);
    assert.equal((await request({ origin: ORIGIN, "x-lm-csrf": "wrong" })).status, 403);
    assert.equal((await request({ origin: ORIGIN, "x-lm-csrf": SCOPE.csrf, "content-type": "text/plain" })).status, 415);
    assert.equal((await request({ origin: ORIGIN, "x-lm-csrf": SCOPE.csrf }, "not-json")).status, 400);
  });
  assert.equal(state.calls.start.length, 0);
  assert.equal(state.calls.states.length, 0);
  assert.equal(state.calls.oauth.length, 0);
});

test("start rejects WHATWG-normalized or whitespace-padded panel origins", async () => {
  for (const badOrigin of ["https:panel.example", "https:/panel.example", " https://life.example", "https://life.example "]) {
    const state = fixture();
    state.opts.panelOrigin = badOrigin;
    await withServer(state.opts, async (base) => {
      const normalized = badOrigin.includes("panel.example") ? "https://panel.example" : ORIGIN;
      const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
        method: "POST", headers: cookieHeaders({ origin: normalized, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }), body: "{}",
      });
      assert.equal(response.status, 403, badOrigin);
    });
    assert.equal(state.calls.start.length, 0);
    assert.equal(state.calls.oauth.length, 0);
  }
});

test("start fails closed without an explicit state secret before provider/store effects", async () => {
  const state = fixture();
  delete state.opts.sessionSecret;
  await withServer(state.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }), body: "{}",
    });
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: "calendar_unavailable" });
  });
  assert.equal(state.calls.start.length, 0);
  assert.equal(state.calls.states.length, 0);
  assert.equal(state.calls.oauth.length, 0);
});

test("rotated panel sessions renew the cookie on calendar responses", async () => {
  const state = fixture({ active: true });
  state.opts.sessionScopeImpl = async () => ({ ...SCOPE, replacement: "replacement-cookie", cookieMaxAge: 120 });
  await withServer(state.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/status`, {
      headers: { cookie: `__Host-lm_panel_session=${SESSION}` },
    });
    assert.equal(response.status, 200);
    assert.match(response.headers.get("set-cookie") || "", /replacement-cookie/);
  });
});

test("missing Calendar account stores only the hashed scoped state before one OAuth link", async () => {
  const state = fixture();
  state.opts.randomBytes = () => Buffer.alloc(32, 1);
  await withServer(state.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }),
      body: JSON.stringify({ tg: "other-chat", uid: "other-user" }),
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(Object.keys(body).sort(), ["connected", "redirectUrl", "state"]);
    assert.equal(body.connected, false);
    assert.equal(body.state, "action_required");
    assert.equal(body.redirectUrl, "https://provider.example/consent");
  });
  assert.equal(state.calls.states.length, 1);
  assert.equal(state.calls.oauth.length, 1);
  const [{ scope, state: stored }, { scope: oauthScope, stateToken }] = [state.calls.states[0], state.calls.oauth[0]];
  assert.deepEqual(scope, SCOPE);
  assert.deepEqual(oauthScope, SCOPE);
  assert.match(stateToken, CALENDAR_STATE_RE);
  assert.equal(stateToken, deriveCalendarState(Buffer.alloc(32, 1).toString("base64url"), SCOPE, SECRET));
  assert.equal(stored.stateHash, crypto.createHash("sha256").update(stateToken).digest("hex"));
  assert.equal(stored.provider, "calendar");
  assert.match(stored.expiresAt, /^20\d\d-/);
  assert.doesNotMatch(JSON.stringify(stored), new RegExp(stateToken));
  assert.doesNotMatch(JSON.stringify(stored), /nonce/i);
});

test("state is a 43-character HMAC bound to nonce and verified uid/chat scope", () => {
  const first = deriveCalendarState("nonce-a", SCOPE, SECRET);
  const peer = deriveCalendarState("nonce-a", { uid: "u-b", chatId: "202" }, SECRET);
  const next = deriveCalendarState("nonce-b", SCOPE, SECRET);
  assert.match(first, CALENDAR_STATE_RE);
  assert.notEqual(first, peer);
  assert.notEqual(first, next);
});

test("status is read-only, ACTIVE-only, and sanitizes provider failures", async () => {
  for (const [providerStatus, expectedState] of [["MISSING", "action_required"], ["DISABLED", "action_required"], ["INACTIVE", "action_required"], ["ACTIVE", "connected"]]) {
    const state = fixture({ status: providerStatus });
    await withServer(state.opts, async (base) => {
      const response = await fetch(`${base}/api/panel/onboarding/calendar/status?uid=u-b&tg=202`, {
        headers: { cookie: `__Host-lm_panel_session=${SESSION}` },
      });
      assert.equal(response.status, 200);
      const body = await response.json();
      assert.equal(body.connected, expectedState === "connected");
      assert.equal(body.state, expectedState);
    });
    assert.deepEqual(state.calls.states, []);
    assert.deepEqual(state.calls.oauth, []);
    assert.deepEqual(state.calls.status.map(({ uid, chatId }) => ({ uid, chatId })), [{ uid: SCOPE.uid, chatId: SCOPE.chatId }]);
    assert.deepEqual(state.calls.sync.map(({ status }) => status), [providerStatus]);
  }

  const failed = fixture();
  failed.opts.composioCalendarStatusImpl = async () => { throw new Error("provider secret and raw description"); };
  await withServer(failed.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/status`, { headers: { cookie: `__Host-lm_panel_session=${SESSION}` } });
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: "calendar_unavailable" });
  });
});

test("invalid provider redirect is rejected without leaking provider payload", async () => {
  const state = fixture({ redirect: "http://provider.example/consent?secret=raw" });
  await withServer(state.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }), body: "{}",
    });
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: "calendar_unavailable" });
  });
  assert.equal(state.calls.states.length, 1);
  assert.equal(state.calls.oauth.length, 1);
});

test("concurrent/repeated starts claim one live state and one provider link until expiry", async () => {
  const state = fixture();
  let nowMs = 0, attempts = 0, rows = 0, links = 0, expiresAt = 0;
  state.opts.nowMs = nowMs;
  state.opts.commandStore.createOAuthState = async (_scope, value) => {
    attempts++;
    if (expiresAt > nowMs) return false;
    rows++; expiresAt = Date.parse(value.expiresAt); return true;
  };
  state.opts.startCalendarOAuthImpl = async () => { links++; throw new Error("provider failed"); };
  const request = (base) => fetch(`${base}/api/panel/onboarding/calendar/start`, {
    method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }), body: "{}",
  });
  await withServer(state.opts, async (base) => {
    assert.equal((await request(base)).status, 502);
    assert.equal((await request(base)).status, 409);
    nowMs = CALENDAR_STATE_TTL_MS + 1;
    state.opts.nowMs = nowMs;
    assert.equal((await request(base)).status, 502);
  });
  assert.equal(attempts, 3);
  assert.equal(rows, 2, "expiry permits a fresh durable state");
  assert.equal(links, 2, "a blocked repeat cannot mint another provider link");
});

test("rebind between provider status read and atomic state claim blocks all start/provider effects", async () => {
  const state = fixture();
  let binding = SCOPE.chatId, stateClaimed = false;
  state.opts.commandStore.assertCurrentScope = async (scope) => scope.chatId === binding;
  state.opts.composioCalendarStatusImpl = async () => { binding = "rebound-chat"; return "MISSING"; };
  state.opts.commandStore.createOAuthState = async (scope) => { stateClaimed = true; return scope.chatId === binding; };
  await withServer(state.opts, async (base) => {
    const response = await fetch(`${base}/api/panel/onboarding/calendar/start`, {
      method: "POST", headers: cookieHeaders({ origin: ORIGIN, "content-type": "application/json", "x-lm-csrf": SCOPE.csrf }), body: "{}",
    });
    assert.equal(response.status, 401);
  });
  assert.equal(stateClaimed, true);
  assert.equal(state.calls.start.length, 0);
  assert.equal(state.calls.oauth.length, 0);
});

test("existing OAuth callback remains atomic and accepts only ACTIVE readback", async () => {
  const { handlePanelOAuthCallback } = require("./panel-api.js");
  const stateToken = "a".repeat(43);
  for (const [claimed, providerStatus, expectedStatus] of [[false, "ACTIVE", 403], [true, "DISABLED", 403], [true, "ACTIVE", 303]]) {
    let claimCalls = 0, providerCalls = 0;
    const response = { status: 0, headers: {}, writeHead(status, headers = {}) { this.status = status; this.headers = headers; }, setHeader() {}, end() {} };
    await handlePanelOAuthCallback({ method: "GET", url: `/panel/oauth/calendar?state=${stateToken}`, headers: { cookie: `__Host-lm_panel_session=${SESSION}` } }, response, {
      sessionScopeImpl: async () => ({ ...SCOPE }),
      commandStore: {
        assertCurrentScope: async () => true,
        claimPanelOAuthAccount: async (scope, hash) => { claimCalls++; assert.deepEqual(scope, SCOPE); assert.equal(hash.length, 64); return claimed ? "ca-a" : null; },
        syncCalendarConnection: async () => true,
      },
      composioKey: "provider-key",
      composioCalendarAccountStatusImpl: async (_scope, id) => { providerCalls++; assert.equal(id, "ca-a"); return providerStatus; },
    });
    assert.equal(response.status, expectedStatus);
    assert.equal(claimCalls, 1);
    assert.equal(providerCalls, claimed ? 1 : 0);
    if (expectedStatus === 303) assert.equal(response.headers.Location, "/panel");
  }
});

test("existing OAuth callback replay, expiry, and cross-scope claims produce no provider read", async () => {
  const { handlePanelOAuthCallback } = require("./panel-api.js");
  const stateToken = "b".repeat(43);
  let claimCount = 0;
  let providerReads = 0;
  const store = {
    assertCurrentScope: async () => true,
    claimPanelOAuthAccount: async (scope, stateHash) => {
      claimCount++;
      assert.equal(scope.uid, SCOPE.uid);
      assert.equal(scope.chatId, SCOPE.chatId);
      assert.equal(stateHash, crypto.createHash("sha256").update(stateToken).digest("hex"));
      return claimCount === 1 ? "ca-a" : null;
    },
    syncCalendarConnection: async () => true,
  };
  const run = async (sessionScope = SCOPE) => {
    const response = { status: 0, headers: {}, writeHead(status, headers = {}) { this.status = status; this.headers = headers; }, setHeader() {}, end() {} };
    await handlePanelOAuthCallback({ method: "GET", url: `/panel/oauth/calendar?state=${stateToken}`, headers: { cookie: `__Host-lm_panel_session=${SESSION}` } }, response, {
      sessionScopeImpl: async () => ({ ...sessionScope }), commandStore: store, composioKey: "provider-key",
      composioCalendarAccountStatusImpl: async (_scope, id) => { providerReads++; assert.equal(id, "ca-a"); return "ACTIVE"; },
    });
    return response.status;
  };
  assert.equal(await run(), 303);
  assert.equal(await run(), 403, "the same state is rejected after the first atomic claim");
  assert.equal(providerReads, 1);
  const expiredStore = { assertCurrentScope: async () => true, claimPanelOAuthAccount: async () => null };
  const expired = { status: 0, writeHead(status) { this.status = status; }, setHeader() {}, end() {} };
  await handlePanelOAuthCallback({ method: "GET", url: `/panel/oauth/calendar?state=${stateToken}`, headers: { cookie: `__Host-lm_panel_session=${SESSION}` } }, expired, {
    sessionScopeImpl: async () => ({ ...SCOPE }), commandStore: expiredStore, composioKey: "provider-key",
    fetchImpl: async () => { throw new Error("provider must not be read"); },
  });
  assert.equal(expired.status, 403);
  const cross = { status: 0, writeHead(status) { this.status = status; }, setHeader() {}, end() {} };
  await handlePanelOAuthCallback({ method: "GET", url: `/panel/oauth/calendar?state=${stateToken}`, headers: { cookie: `__Host-lm_panel_session=${SESSION}` } }, cross, {
    sessionScopeImpl: async () => ({ uid: "u-b", chatId: "202" }), commandStore: {
      assertCurrentScope: async () => true,
      claimPanelOAuthAccount: async () => null,
    }, composioKey: "provider-key", fetchImpl: async () => { throw new Error("provider must not be read"); },
  });
  assert.equal(cross.status, 403);
});

test("Telegram OAuth callback needs no browser cookie and returns to the exact claimed chat", async () => {
  const { handleTelegramOAuthCallback } = require("./panel-api.js");
  const stateToken = "c".repeat(43);
  const calls = [];
  const response = { status: 0, headers: {}, writeHead(status, headers = {}) { this.status = status; this.headers = headers; }, end() {} };
  await handleTelegramOAuthCallback({ method: "GET", url: `/telegram/oauth/calendar?state=${stateToken}&lang=ja-JP`, headers: {} }, response, {
    commandStore: {
      claimTelegramOAuthState: async (stateHash) => { calls.push(["claim", stateHash]); return { uid: "u-tg", chat_id: "303", connected_account_id: "ca-current" }; },
      assertCurrentScope: async (scope) => { calls.push(["scope", scope]); return true; },
      syncCalendarConnection: async (scope, status, id) => { calls.push(["sync", scope, status, id]); return true; },
    },
    composioKey: "provider-key",
    composioCalendarAccountStatusImpl: async (scope, id) => { calls.push(["provider", scope, id]); return "ACTIVE"; },
    composioCalendarEventCountImpl: async (scope, id) => { calls.push(["events", scope, id]); return 2; },
    sendMessage: async (chatId, text) => { calls.push(["send", chatId, text]); return { ok: true, result: { message_id: 44 } }; },
    telegramReturnUrl: "https://t.me/LifeManagerBotbot",
  });
  assert.equal(response.status, 303);
  assert.equal(response.headers.Location, "https://t.me/LifeManagerBotbot");
  assert.equal(calls[0][0], "claim");
  assert.equal(calls[0][1], crypto.createHash("sha256").update(stateToken).digest("hex"));
  assert.deepEqual(calls.find(([kind]) => kind === "scope")[1], { uid: "u-tg", chatId: "303" });
  assert.deepEqual(calls.find(([kind]) => kind === "sync").slice(1), [{ uid: "u-tg", chatId: "303" }, "ACTIVE", "ca-current"]);
  assert.deepEqual(calls.find(([kind]) => kind === "send").slice(1, 2), ["303"]);
  assert.match(calls.find(([kind]) => kind === "send")[2], /自宅の住所/);
});

test("Telegram OAuth callback rejects invalid, replayed, and cross-bound state before provider or send", async () => {
  const { handleTelegramOAuthCallback } = require("./panel-api.js");
  for (const fixture of [
    { url: "/telegram/oauth/calendar?state=bad", claimed: null },
    { url: `/telegram/oauth/calendar?state=${"d".repeat(43)}`, claimed: null },
    { url: `/telegram/oauth/calendar?state=${"e".repeat(43)}`, claimed: { uid: "u-tg", chat_id: "303" }, scope: false },
  ]) {
    let provider = 0, sends = 0, claims = 0;
    const response = { status: 0, writeHead(status) { this.status = status; }, end() {} };
    await handleTelegramOAuthCallback({ method: "GET", url: fixture.url, headers: {} }, response, {
      commandStore: {
        claimTelegramOAuthState: async () => { claims++; return fixture.claimed; },
        assertCurrentScope: async () => fixture.scope !== false,
      },
      composioCalendarStatusImpl: async () => { provider++; return "ACTIVE"; },
      sendMessage: async () => { sends++; return { ok: true }; },
      telegramReturnUrl: "https://t.me/LifeManagerBotbot",
    });
    assert.equal(response.status, 403);
    assert.equal(claims, fixture.url.includes("state=bad") ? 0 : 1);
    assert.equal(provider, 0);
    assert.equal(sends, 0);
  }
});

test("production server wires only the two session calendar onboarding paths", () => {
  const source = fs.readFileSync(path.join(__dirname, "../server.js"), "utf8");
  assert.match(source, /handleCalendarOnboardRequest/);
  assert.match(source, /\/api\/panel\/onboarding\/calendar\/start/);
  assert.match(source, /\/api\/panel\/onboarding\/calendar\/status/);
  assert.match(source, /LM_PANEL_SESSION_ROTATION_SECRET/);
});

test("atomic OAuth migration is additive, hash-only, and service-role RPC guarded", () => {
  const migration = fs.readFileSync(path.join(__dirname, "../migrations/2026-08-27-lm-panel-oauth-atomic.sql"), "utf8");
  assert.match(migration, /CREATE UNIQUE INDEX IF NOT EXISTS lm_panel_oauth_states_live_scope_idx/i);
  assert.match(migration, /WHERE used_at IS NULL/i);
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.create_lm_panel_oauth_state/i);
  assert.match(migration, /DELETE FROM public\.lm_panel_oauth_states/i);
  assert.match(migration, /FROM public\.lm_users[\s\S]*uid = p_uid[\s\S]*telegram_chat_id::text = p_chat_id[\s\S]*FOR SHARE/i);
  assert.match(migration, /REVOKE ALL ON FUNCTION public\.create_lm_panel_oauth_state[\s\S]*FROM PUBLIC, anon, authenticated/i);
  assert.match(migration, /GRANT EXECUTE ON FUNCTION public\.create_lm_panel_oauth_state[\s\S]*TO service_role/i);
  assert.doesNotMatch(migration, /CREATE TABLE/i);
});

test("R1A calendar reachability migration syncs ACTIVE truth and clears stale markers atomically", () => {
  const migration = fs.readFileSync(path.join(__dirname, "../migrations/2026-08-27-lm-panel-onboarding-reachability.sql"), "utf8");
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.sync_lm_panel_calendar_status\(p_uid text, p_chat_id text, p_status text\)/i);
  assert.match(migration, /calendar_provider\s*=\s*'composio_gcal'/i);
  assert.match(migration, /calendar_provider\s*=\s*NULL/i);
  assert.match(migration, /MISSING.*DISABLED.*INACTIVE/is);
  assert.match(migration, /telegram_chat_id::text\s*=\s*p_chat_id[\s\S]*FOR UPDATE/i);
  assert.match(migration, /REVOKE ALL ON FUNCTION public\.sync_lm_panel_calendar_status/i);
});

test("Telegram OAuth callback migration is hash-only, tenant-bound, one-time, and service-role only", () => {
  const migration = fs.readFileSync(path.join(__dirname, "../migrations/2026-09-07-lm-telegram-oauth-callback.sql"), "utf8");
  assert.match(migration, /p_state_hash\s+text/i);
  assert.match(migration, /state_hash\s*=\s*p_state_hash/i);
  assert.match(migration, /used_at\s+IS\s+NULL/i);
  assert.match(migration, /expires_at\s*>\s*now\(\)/i);
  assert.match(migration, /EXISTS[\s\S]*lm_users[\s\S]*telegram_chat_id::text\s*=\s*state\.chat_id/i);
  assert.match(migration, /REVOKE ALL[\s\S]*FROM PUBLIC, anon, authenticated/i);
  assert.match(migration, /GRANT EXECUTE[\s\S]*TO service_role/i);
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.create_lm_telegram_oauth_state/i);
  assert.match(migration, /DELETE FROM public\.lm_panel_oauth_states[\s\S]*provider\s*=\s*'calendar'[\s\S]*used_at\s+IS\s+NULL/i);
  assert.match(migration, /FROM public\.lm_users[\s\S]*uid\s*=\s*p_uid[\s\S]*telegram_chat_id::text\s*=\s*p_chat_id[\s\S]*FOR UPDATE/i);
  assert.match(migration, /REVOKE ALL ON FUNCTION public\.create_lm_telegram_oauth_state[\s\S]*FROM PUBLIC, anon, authenticated/i);
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.complete_lm_telegram_home/i);
  assert.match(migration, /telegram_chat_id::text\s*=\s*p_chat_id[\s\S]*calendar_provider\s*=\s*'composio_gcal'[\s\S]*FOR UPDATE/i);
  assert.match(migration, /UPDATE public\.lm_users SET home_address[\s\S]*INSERT INTO public\.lm_panel_preferences[\s\S]*notifications_enabled/i);
  assert.match(migration, /REVOKE ALL ON FUNCTION public\.complete_lm_telegram_home[\s\S]*FROM PUBLIC, anon, authenticated/i);
  assert.doesNotMatch(migration, /raw_state|CREATE TABLE/i);
});

test("Calendar account binding migration ties both callbacks and the selected account to one tenant", () => {
  const migration = fs.readFileSync(path.join(__dirname, "../migrations/2026-09-11-lm-calendar-account-binding.sql"), "utf8");
  assert.match(migration, /ADD COLUMN IF NOT EXISTS calendar_connected_account_id text/i);
  assert.match(migration, /ADD COLUMN IF NOT EXISTS connected_account_id text/i);
  assert.match(migration, /attach_lm_panel_oauth_account[\s\S]*state\.uid = p_uid[\s\S]*state\.chat_id = p_chat_id/i);
  assert.match(migration, /claim_lm_panel_oauth_account[\s\S]*used_at IS NULL[\s\S]*expires_at > now\(\)/i);
  assert.match(migration, /claim_lm_telegram_oauth_state[\s\S]*connected_account_id IS NOT NULL/i);
  assert.match(migration, /sync_lm_panel_calendar_connection[\s\S]*telegram_chat_id::text = p_chat_id/i);
  assert.equal((migration.match(/FROM PUBLIC, anon, authenticated/gi) || []).length, 4);
  assert.equal((migration.match(/TO service_role/gi) || []).length, 4);
});
