"use strict";

const test = require("node:test");
const assert = require("node:assert");
const crypto = require("crypto");
const http = require("http");
const fs = require("fs");
const path = require("path");

const { sendPanelLink, handlePanelRequest, handleMoneyPrinterGuestRequest, claimTelegramWebhookActor } = require("./panel-auth.js");
const { isPanelCommand } = require("./telegram.js");
const PANEL_NOW = new Date("2026-08-27T00:00:00.000Z");
const PANEL_AUTH_DATE = Math.floor(PANEL_NOW.getTime() / 1000);

test("Telegram webhook actor claim is deterministic and private-chat scoped", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return { ok: true, json: async () => [{ status: "claimed", uid: "lm_tg_fixture", chat_id: "123" }] };
  };
  const input = { actorId: "123", chatId: "123", updateId: "9001", profileName: "Fixture User" };
  const opts = { supaUrl: "https://fixture.supabase.co", supaKey: "key", fetchImpl };
  assert.deepEqual(await claimTelegramWebhookActor(input, opts), { status: "claimed", uid: "lm_tg_fixture", chat_id: "123" });
  await claimTelegramWebhookActor(input, opts);
  assert.deepEqual(calls[0].body, calls[1].body);
  assert.match(calls[0].body.p_init_hash, /^[a-f0-9]{64}$/);
  assert.equal(calls[0].body.p_actor_id, "123");
  for (const invalid of [{ ...input, actorId: "999" }, { ...input, updateId: "" }, { ...input, actorId: "bad" }]) {
    await assert.rejects(() => claimTelegramWebhookActor(invalid, opts), /telegram actor unavailable/);
  }
});

function telegramInitData({ actorId = 123, authDate = PANEL_AUTH_DATE, token = "telegram-token", chatId = actorId, firstName = "Fixture", lastName = "" } = {}) {
  const params = new URLSearchParams({
    auth_date: String(authDate),
    user: JSON.stringify({ id: actorId, first_name: firstName, last_name: lastName }),
    chat: JSON.stringify({ id: chatId, type: "private" }),
  });
  const secret = crypto.createHmac("sha256", "WebAppData").update(token).digest();
  const dataCheckString = [...params.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
  const hash = crypto.createHmac("sha256", secret).update(dataCheckString).digest("hex");
  params.set("hash", hash);
  return params.toString();
}

function postTelegramInitData(base, initData, returnTo) {
  return fetch(`${base}/api/panel/session/telegram`, {
    method: "POST",
    headers: { Origin: base, "content-type": "application/json" },
    body: JSON.stringify(returnTo ? { initData, returnTo } : { initData }),
  });
}

test("PANEL-2: valid Telegram initData creates a server session through the existing auth boundary", async () => {
  const calls = [];
  await withPanelServer({
    token: "telegram-token",
    supaUrl: "https://db.example",
    supaKey: "service-key",
    now: () => new Date(PANEL_NOW),
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/rpc/claim_lm_panel_telegram_init_v2")) {
        assert.equal(JSON.parse(init.body).p_profile_name, "Fixture");
        return { ok: true, status: 200, json: async () => [{ status: "claimed", uid: "lm_u1", chat_id: "123" }] };
      }
      if (parsed.pathname.endsWith("/lm_panel_sessions") && init.method === "POST") {
        return { ok: true, status: 201, json: async () => [] };
      }
      throw new Error(`unexpected panel auth fetch ${init.method || "GET"} ${url}`);
    },
  }, async (base) => {
    const response = await postTelegramInitData(base, telegramInitData());
    assert.equal(response.status, 200, await response.clone().text());
    assert.deepEqual(await response.json(), { redirect: "/panel" });
    assert.match(response.headers.get("set-cookie") || "", /__Host-lm_panel_session=/);
  });
  assert.equal(calls.filter(({ url }) => url.endsWith("/rpc/claim_lm_panel_telegram_init_v2")).length, 1);
  assert.equal(calls.filter(({ url }) => url.endsWith("/lm_panel_sessions")).length, 1);
});

test("PANEL-2: invalid and stale Telegram initData are rejected before any session write", async () => {
  const valid = telegramInitData();
  const tampered = new URLSearchParams(valid);
  tampered.set("user", JSON.stringify({ id: 999, first_name: "Tampered" }));
  const cases = [
    { name: "invalid signature", initData: tampered.toString(), status: 401 },
    { name: "stale auth_date", initData: telegramInitData({ authDate: PANEL_AUTH_DATE - 301 }), status: 401 },
  ];
  for (const current of cases) {
    const writes = [];
    await withPanelServer({
      token: "telegram-token",
      supaUrl: "https://db.example",
      supaKey: "service-key",
      now: () => new Date(PANEL_NOW),
      fetchImpl: async (url, init) => {
        writes.push({ url: String(url), init });
        throw new Error(`${current.name} must not reach Supabase`);
      },
    }, async (base) => {
      const response = await postTelegramInitData(base, current.initData);
      assert.equal(response.status, current.status, `${current.name}: ${await response.clone().text()}`);
      assert.deepEqual(await response.json(), { error: "telegram_auth_rejected" }, current.name);
    });
    assert.equal(writes.length, 0, `${current.name} must not write a session`);
  }
});

test("PANEL-2: replayed and cross-actor Telegram claims remain rejected", async () => {
  const cases = [
    { name: "replayed", claim: { status: "replayed" }, status: 409, error: "telegram_auth_replayed" },
    { name: "cross-actor", claim: { status: "claimed", uid: "lm_u1", chat_id: "999" }, status: 403, error: "telegram_actor_unbound" },
  ];
  for (const current of cases) {
    const writes = [];
    await withPanelServer({
      token: "telegram-token",
      supaUrl: "https://db.example",
      supaKey: "service-key",
      now: () => new Date(PANEL_NOW),
      fetchImpl: async (url, init) => {
        writes.push({ url: String(url), init });
        const parsed = new URL(String(url));
        if (parsed.pathname.endsWith("/rpc/claim_lm_panel_telegram_init_v2")) {
          return { ok: true, status: 200, json: async () => [current.claim] };
        }
        throw new Error(`${current.name} must not create a session`);
      },
    }, async (base) => {
      const response = await postTelegramInitData(base, telegramInitData());
      assert.equal(response.status, current.status, current.name);
      assert.deepEqual(await response.json(), { error: current.error }, current.name);
    });
    assert.equal(writes.filter(({ url }) => url.endsWith("/lm_panel_sessions")).length, 0, `${current.name} must not write a session`);
  }
});

test("R1A2 HMAC verification carries only a bounded Telegram display name", () => {
  const { verifyTelegramInitData } = require("./panel-auth.js");
  const verified = verifyTelegramInitData(telegramInitData({ actorId: 123, firstName: "Aiko", lastName: "Tanaka" }), { token: "telegram-token", now: () => PANEL_NOW });
  assert.equal(verified.ok, true);
  assert.equal(verified.actorId, "123");
  assert.equal(verified.profileName, "Aiko Tanaka");
  const long = verifyTelegramInitData(telegramInitData({ firstName: "A".repeat(100), lastName: "B".repeat(100) }), { token: "telegram-token", now: () => PANEL_NOW });
  assert.equal(long.profileName.length, 120);
});

test("Task 7B: /panel/onboarding strips query identity without changing the onboarding pathname", async () => {
  const calls = [];
  await withPanelServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    fetchImpl: async (...args) => {
      calls.push(args);
      throw new Error("query identity must not reach storage");
    },
  }, async (base) => {
    const response = await fetch(`${base}/panel/onboarding?tg=123`, { redirect: "manual" });
    assert.equal(response.status, 303);
    assert.equal(response.headers.get("location"), "/panel/onboarding");
    assert.equal(response.headers.get("set-cookie"), null);
  });
  assert.equal(calls.length, 0);
});

test("Task 7B: an authenticated onboarding visit renders the Telegram-native page, not the dashboard", async () => {
  const session = Buffer.alloc(32, 0x44).toString("base64url");
  await withPanelServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: () => Buffer.alloc(32, 0x45),
    fetchImpl: async (url) => {
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/rpc/resolve_lm_panel_session")) return { ok: true, status: 200, json: async () => [{ uid: "lm_u1", chat_id: "123", rotated: false }] };
      throw new Error(`unexpected onboarding fetch ${url}`);
    },
  }, async (base) => {
    const response = await fetch(`${base}/panel/onboarding`, { headers: { Cookie: `__Host-lm_panel_session=${session}` } });
    assert.equal(response.status, 200);
    const html = await response.text();
    assert.match(html, /data-panel-onboarding/);
    assert.doesNotMatch(html, /data-panel-section="timeline"/);
    assert.doesNotMatch(html, /\bAnicca\b/i);
  });
});

test("Task 7B: unauthenticated Telegram onboarding keeps the return path through the auth boundary", async () => {
  await withPanelServer({
    token: "telegram-token",
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: () => Buffer.alloc(32, 0x50),
    now: () => new Date(PANEL_NOW),
    fetchImpl: async (url, init) => {
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/lm_panel_device_challenges")) return { ok: true, status: 201, json: async () => [] };
      if (parsed.pathname.endsWith("/rpc/claim_lm_panel_telegram_init_v2")) return { ok: true, status: 200, json: async () => [{ status: "claimed", uid: "lm_u1", chat_id: "123" }] };
      if (parsed.pathname.endsWith("/lm_panel_sessions")) return { ok: true, status: 201, json: async () => [] };
      throw new Error(`unexpected onboarding auth fetch ${url}`);
    },
  }, async (base) => {
    const login = await fetch(`${base}/panel/onboarding`);
    assert.equal(login.status, 200);
    const loginHtml = await login.text();
    assert.match(loginHtml, /returnTo.*\/panel\/onboarding/);
    assert.doesNotMatch(loginHtml, /[?&](?:tg|uid)=/i);

    const response = await postTelegramInitData(base, telegramInitData(), "/panel/onboarding");
    assert.equal(response.status, 200, await response.clone().text());
    assert.deepEqual(await response.json(), { redirect: "/panel/onboarding" });
    for (const forged of ["//evil.example", "/panel/onboarding/extra", "https://evil.example/panel/onboarding"]) {
      const fallback = await postTelegramInitData(base, telegramInitData(), forged);
      assert.equal(fallback.status, 200, forged);
      assert.deepEqual(await fallback.json(), { redirect: "/panel" }, forged);
    }
  });
});

test("Task 7B R1: device-code completion preserves onboarding only through the fixed path allowlist", async () => {
  const exchanges = [];
  await withPanelServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: () => Buffer.alloc(32, 0x51),
    fetchImpl: async (url, init = {}) => {
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/lm_panel_device_challenges")) return { ok: true, status: 201, json: async () => [] };
      if (parsed.pathname.endsWith("/rpc/exchange_lm_panel_device_challenge")) { exchanges.push(JSON.parse(init.body)); return { ok: true, status: 200, json: async () => [{ status: "claimed", uid: "lm_u1", chat_id: "123" }] }; }
      if (parsed.pathname.endsWith("/lm_panel_sessions")) return { ok: true, status: 201, json: async () => [] };
      throw new Error(`unexpected device auth fetch ${url}`);
    },
  }, async (base) => {
    const login = await fetch(`${base}/panel/onboarding`);
    const challenge = /__Host-lm_panel_challenge=([^;]+)/.exec(login.headers.get("set-cookie") || "")?.[1] || "";
    assert.match(challenge, /^[A-Za-z0-9_-]{43}$/);
    const accepted = await fetch(`${base}/api/panel/session/device`, {
      method: "POST",
      headers: { Origin: base, Cookie: `__Host-lm_panel_challenge=${challenge}`, "content-type": "application/json" },
      body: JSON.stringify({ returnTo: "/panel/onboarding" }),
    });
    assert.equal(accepted.status, 200, await accepted.clone().text());
    assert.deepEqual(await accepted.json(), { redirect: "/panel/onboarding" });
    const fallback = await fetch(`${base}/api/panel/session/device`, {
      method: "POST",
      headers: { Origin: base, Cookie: `__Host-lm_panel_challenge=${challenge}`, "content-type": "application/json" },
      body: JSON.stringify({ returnTo: "//evil.example" }),
    });
    assert.equal(fallback.status, 200);
    assert.deepEqual(await fallback.json(), { redirect: "/panel" });
    assert.equal(exchanges.length, 2);
  });
});

async function withPanelServer(opts, run) {
  let origin = "";
  const server = http.createServer((req, res) => {
    const dynamicOrigin = opts.panelOrigin || opts.panelBaseUrl ? {} : { panelOrigin: origin, panelBaseUrl: origin };
    Promise.resolve().then(() => handlePanelRequest(req, res, { ...opts, ...dynamicOrigin })).catch((error) => {
      res.writeHead(500);
      res.end(error.message);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const { port } = server.address();
    origin = `http://127.0.0.1:${port}`;
    return await run(origin);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function withMoneyPrinterGuestServer(opts, run) {
  const server = http.createServer((req, res) => {
    Promise.resolve().then(() => handleMoneyPrinterGuestRequest(req, res, opts)).catch((error) => {
      if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(error.message);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const { port } = server.address();
    return await run(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("WebMCP judge guest first GET creates one isolated guest and renders one session", async () => {
  const calls = [];
  const familyId = "judge-family-1";
  const guestUid = `webmcp-guest-${"61".repeat(12)}`;
  const expectedCsrf = crypto.createHash("sha256").update(`${familyId}:panel-family-csrf`).digest("hex");
  await withMoneyPrinterGuestServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: (size) => Buffer.alloc(size, 0x61),
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/lm_users")) return { ok: true, status: 201 };
      if (parsed.pathname.endsWith("/lm_panel_sessions")) return { ok: true, status: 201 };
      if (parsed.pathname.endsWith("/rpc/resolve_lm_panel_session")) return { ok: true, status: 200, json: async () => [{ uid: guestUid, chat_id: guestUid, family_id: familyId, rotated: false }] };
      throw new Error(`unexpected guest fetch ${init.method || "GET"} ${url}`);
    },
  }, async (base) => {
    const response = await fetch(`${base}/money-printer`);
    assert.equal(response.status, 200, await response.clone().text());
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.equal(response.headers.get("x-content-type-options"), "nosniff");
    assert.equal(response.headers.get("referrer-policy"), "no-referrer");
    assert.equal(response.headers.get("origin-agent-cluster"), "?1");
    assert.equal(response.headers.get("permissions-policy"), "tools=(self)");
    assert.match(response.headers.get("content-security-policy") || "", /script-src 'self'/);
    assert.match(response.headers.get("content-security-policy") || "", /connect-src 'self'/);
    assert.match(response.headers.get("set-cookie") || "", /__Host-lm_panel_session=/);
    const html = await response.text();
    assert.match(html, /data-guest-mode/);
    assert.match(html, /Judge guest — isolated workroom/);
    assert.match(html, /data-panel-section="money-printer"/);
    assert.match(html, new RegExp(`const pageCsrf = "${expectedCsrf}"`));
  });
  const userCalls = calls.filter(({ url }) => new URL(url).pathname.endsWith("/lm_users"));
  assert.equal(userCalls.length, 1);
  const userCall = userCalls[0];
  assert.equal(new URL(userCall.url).search, "?on_conflict=uid");
  assert.equal(userCall.init.method, "POST");
  assert.match(userCall.init.headers.Prefer, /resolution=merge-duplicates/);
  assert.deepEqual(JSON.parse(userCall.init.body), {
    uid: guestUid, name: "WebMCP Judge Guest", telegram_chat_id: guestUid, paid: false,
  });
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/lm_panel_sessions")).length, 1);
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/rpc/resolve_lm_panel_session")).length, 1);
});

test("WebMCP judge guest repeat reuses a valid guest session without another user upsert", async () => {
  const session = Buffer.alloc(32, 0x62).toString("base64url");
  const guestUid = `webmcp-guest-${"62".repeat(12)}`;
  const calls = [];
  await withMoneyPrinterGuestServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: (size) => Buffer.alloc(size, 0x63),
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/rpc/resolve_lm_panel_session")) return { ok: true, status: 200, json: async () => [{ uid: guestUid, chat_id: guestUid, rotated: false }] };
      if (parsed.pathname.endsWith("/lm_users")) return { ok: true, status: 201 };
      if (parsed.pathname.endsWith("/lm_panel_sessions")) return { ok: true, status: 201 };
      throw new Error(`unexpected guest fetch ${init.method || "GET"} ${url}`);
    },
  }, async (base) => {
    const response = await fetch(`${base}/money-printer`, { headers: { Cookie: `__Host-lm_panel_session=${session}` } });
    assert.equal(response.status, 200, await response.clone().text());
    assert.match(await response.text(), /Judge guest — isolated workroom/);
  });
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/rpc/resolve_lm_panel_session")).length, 1);
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/lm_users")).length, 0);
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/lm_panel_sessions")).length, 0);
});

test("WebMCP judge guest never adopts an owner session and rejects wrong request shape", async () => {
  const owner = Buffer.alloc(32, 0x64).toString("base64url");
  const calls = [];
  let resolves = 0;
  const guestUid = `webmcp-guest-${"65".repeat(12)}`;
  const opts = {
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: (size) => Buffer.alloc(size, 0x65),
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const parsed = new URL(String(url));
      if (parsed.pathname.endsWith("/rpc/resolve_lm_panel_session")) {
        resolves += 1;
        return { ok: true, status: 200, json: async () => [resolves === 1
          ? { uid: "owner-uid", chat_id: "owner-chat", rotated: false }
          : { uid: guestUid, chat_id: guestUid, family_id: "judge-family-2", rotated: false }] };
      }
      if (parsed.pathname.endsWith("/lm_users") || parsed.pathname.endsWith("/lm_panel_sessions")) return { ok: true, status: 201 };
      throw new Error(`unexpected guest fetch ${init.method || "GET"} ${url}`);
    },
  };
  await withMoneyPrinterGuestServer(opts, async (base) => {
    const ownerResponse = await fetch(`${base}/money-printer`, { headers: { Cookie: `__Host-lm_panel_session=${owner}` } });
    assert.equal(ownerResponse.status, 200);
    const ownerHtml = await ownerResponse.text();
    assert.match(ownerHtml, /Judge guest — isolated workroom/);
    assert.doesNotMatch(ownerHtml, /owner-uid|owner-chat/);

    for (const request of [
      { url: `${base}/money-printer`, init: { method: "POST" }, status: 405 },
      { url: `${base}/money-printer/`, init: {}, status: 404 },
      { url: `${base}/money-printer?judge=1`, init: {}, status: 400 },
    ]) {
      const response = await fetch(request.url, { ...request.init, redirect: "manual" });
      assert.equal(response.status, request.status, request.url);
    }
  });
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/lm_users")).length, 1);
  assert.equal(calls.filter(({ url }) => new URL(url).pathname.endsWith("/lm_panel_sessions")).length, 1);
  const sessionBody = JSON.parse(calls.find(({ url }) => new URL(url).pathname.endsWith("/lm_panel_sessions")).init.body);
  assert.equal(sessionBody.uid, guestUid);
  assert.equal(sessionBody.chat_id, guestUid);
});

test("PANEL-1: a legacy ?t= URL is stripped without claiming or creating a session", async () => {
  const rawToken = Buffer.alloc(32, 0x11).toString("base64url");
  const calls = [];

  await withPanelServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      throw new Error(`legacy panel URL must not call ${url}`);
    },
  }, async (base) => {
    const response = await fetch(`${base}/panel?t=${rawToken}`, { redirect: "manual" });
    assert.equal(response.status, 303);
    assert.equal(response.headers.get("location"), "/panel");
    assert.equal(response.headers.get("set-cookie"), null);
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  });

  assert.equal(calls.length, 0);
});

test("LM-33a/33c: /panel with a live session renders the authenticated mirror without leaking uid", async () => {
  const session = Buffer.alloc(32, 0x33).toString("base64url");
  let lookupUrl = "";
  await withPanelServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    now: () => new Date("2026-07-21T00:00:00.000Z"),
    randomBytes: () => Buffer.alloc(32, 0x34),
    fetchImpl: async (url, init) => {
      lookupUrl = url;
      assert.equal(JSON.parse(init.body).p_session_hash, crypto.createHash("sha256").update(session).digest("hex"));
      return { ok: true, status: 200, json: async () => [{ uid: "lm_u1", chat_id: "123", rotated: false }] };
    },
  }, async (base) => {
    const response = await fetch(`${base}/panel`, {
      headers: { Cookie: `other=x; lm_panel_session=${session}` },
    });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
    const html = await response.text();
    assert.match(html, /<h1>Life Manager<\/h1>/);
    assert.doesNotMatch(html, /\bAnicca\b/i);
    for (const section of ["timeline", "scores", "ledger", "gates", "settings"]) {
      assert.match(html, new RegExp(`data-panel-section="${section}"`));
    }
    assert.doesNotMatch(html, /lm_u1/);
  });
  const lookup = new URL(lookupUrl);
  assert.equal(lookup.pathname, "/rest/v1/rpc/resolve_lm_panel_session");
});

test("LM-33a negative: /panel without a session returns human login HTML", async () => {
  await withPanelServer({
    supaUrl: "https://db.example",
    supaKey: "service-key",
    randomBytes: () => Buffer.alloc(32, 0x70),
    fetchImpl: async (url, init) => {
      assert.match(url, /\/rest\/v1\/lm_panel_device_challenges$/);
      assert.equal(init.method, "POST");
      return { ok: true, status: 201 };
    },
  }, async (base) => {
    const response = await fetch(`${base}/panel`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type"), /text\/html/);
    const html = await response.text();
    assert.equal(html.match(/<title>([^<]+)<\/title>/)?.[1], "Life Manager sign in");
    assert.equal(html.match(/<main class="card" data-panel-login><p>([^<]+)<\/p>/)?.[1], "Life Manager");
    assert.doesNotMatch(html, /\bAnicca\b/i);
  });
});

test("LM-33a: Telegram recognizes only the /panel command", () => {
  assert.equal(isPanelCommand("/panel"), true);
  assert.equal(isPanelCommand(" /PANEL@LifeManagerBotbot "), true);
  assert.equal(isPanelCommand("/panel please"), true);
  assert.equal(isPanelCommand("/panelx"), false);
  assert.equal(isPanelCommand("show /panel"), false);
});

test("PANEL-1: Telegram /panel sends one canonical web_app button with zero token-table mutation", async () => {
  const sent = [];
  const dbCalls = [];
  const result = await sendPanelLink({ uid: "lm_u1", chatId: "123" }, {
    token: "telegram-token",
    supaUrl: "https://db.example",
    supaKey: "service-key",
    panelBaseUrl: "https://life.example/",
    fetchImpl: async (...args) => { dbCalls.push(args); return { ok: true, status: 201 }; },
    sendMessage: async (...args) => { sent.push(args); return { ok: true }; },
  });
  assert.deepEqual(result, { url: "https://life.example/panel" });
  assert.equal(dbCalls.length, 0);
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], "telegram-token");
  assert.equal(sent[0][1], "123");
  assert.equal(sent[0][2], "Open your personal Life Manager panel.");
  assert.doesNotMatch(sent[0][2], /\bAnicca\b/i);
  assert.doesNotMatch(sent[0][2], /expires|temporary|token|\?t=/i);
  const button = sent[0][3].reply_markup.inline_keyboard[0][0];
  assert.deepEqual(button.web_app, { url: "https://life.example/panel" });
  assert.equal(Object.hasOwn(button, "url"), false);
  const buttonUrl = new URL(button.web_app.url);
  assert.equal(buttonUrl.pathname, "/panel");
  assert.equal(buttonUrl.search, "");
  assert.equal(buttonUrl.hash, "");
});

test("PANEL-1: Telegram /panel fails closed when the canonical web_app button is rejected", async () => {
  const dbCalls = [];
  await assert.rejects(() => sendPanelLink({ uid: "lm_u1", chatId: "123" }, {
    token: "telegram-token",
    supaUrl: "https://db.example",
    supaKey: "service-key",
    panelBaseUrl: "https://life.example/",
    fetchImpl: async (...args) => { dbCalls.push(args); return { ok: true, status: 201 }; },
    sendMessage: async () => ({ ok: false, error_code: 400 }),
  }), /panel web app button send failed/);
  assert.equal(dbCalls.length, 0);
});

test("PANEL-0: live session resolves immutable uid + telegram chat", async () => {
  const { sessionScope } = require("./panel-auth.js");
  const session = Buffer.alloc(32, 0x7a).toString("base64url");
  const scope = await sessionScope(session, { supaUrl: "https://db.example", supaKey: "key", randomBytes: () => Buffer.alloc(32, 0x7b), fetchImpl: async (url, init) => {
    assert.match(url, /rpc\/resolve_lm_panel_session$/);
    assert.equal(JSON.parse(init.body).p_session_hash, crypto.createHash("sha256").update(session).digest("hex"));
    return { ok: true, json: async () => [{ uid: "u-a", chat_id: "101", rotated: false }] };
  } });
  assert.deepEqual(scope, { uid: "u-a", chatId: "101", replacement: null, csrf: require("./panel-auth.js").csrfToken(session) });
});

test("LM-33a: additive migration stores token/session hashes and atomically claims once before expiry", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-07-21-lm33a-panel-auth.sql"), "utf8");
  assert.match(sql, /CREATE TABLE IF NOT EXISTS public\.lm_panel_tokens/i);
  assert.match(sql, /token_hash\s+text\s+PRIMARY KEY/i);
  assert.match(sql, /uid\s+text\s+NOT NULL/i);
  assert.match(sql, /chat_id\s+text\s+NOT NULL/i);
  assert.match(sql, /expires_at\s+timestamptz\s+NOT NULL/i);
  assert.match(sql, /used_at\s+timestamptz/i);
  assert.match(sql, /CREATE TABLE IF NOT EXISTS public\.lm_panel_sessions/i);
  assert.match(sql, /session_hash\s+text\s+PRIMARY KEY/i);
  assert.match(sql, /UPDATE public\.lm_panel_tokens[\s\S]*used_at\s+IS\s+NULL[\s\S]*expires_at\s*>\s*now\(\)[\s\S]*RETURNING/i);
  assert.match(sql, /GRANT EXECUTE ON FUNCTION public\.claim_lm_panel_token\(text\) TO service_role/i);
});

test("LM-33a: life-call wires GET /panel and Telegram /panel without changing /lm onboarding", () => {
  const source = fs.readFileSync(path.join(__dirname, "../server.js"), "utf8");
  assert.match(source, /isPanelCommand/);
  assert.match(source, /sendPanelLink/);
  assert.match(source, /handlePanelRequest/);
  assert.match(source, /path === "\/panel"/);
  assert.match(source, /if \(isPanelCommand\(u\.text\) \|\| isPanelDeepLink\(u\.text\)/);

  const telegramSource = fs.readFileSync(path.join(__dirname, "telegram.js"), "utf8");
  assert.match(telegramSource, /return `\$\{root\}\/lm\?tg=/, "the /lm?tg onboarding handoff must remain");
});

test("R1A actor claim provisions one deterministic Telegram tenant under the existing 2-arg RPC", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-08-27-lm-panel-onboarding-reachability.sql"), "utf8");
  assert.match(sql, /CREATE OR REPLACE FUNCTION public\.claim_lm_panel_telegram_init\(p_init_hash text, p_actor_id text\)/i);
  assert.match(sql, /INSERT INTO public\.lm_panel_telegram_replays/i);
  assert.match(sql, /INSERT INTO public\.lm_users/i);
  assert.match(sql, /md5\(p_actor_id\)/i);
  assert.match(sql, /ON CONFLICT ON CONSTRAINT lm_users_pkey DO NOTHING/i);
  assert.match(sql, /telegram_chat_id::text\s*=\s*p_actor_id/i);
  assert.match(sql, /FOR UPDATE/i);
  assert.match(sql, /RETURN QUERY SELECT 'replayed'/i);
  assert.match(sql, /RETURN QUERY SELECT 'claimed'/i);
});

test("R1A2 v2 actor claim stores profile name only for an empty existing name", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-08-27-lm-panel-onboarding-reachability.sql"), "utf8");
  assert.match(sql, /CREATE OR REPLACE FUNCTION public\.claim_lm_panel_telegram_init_v2\(p_init_hash text, p_actor_id text, p_profile_name text\)/i);
  assert.match(sql, /p_profile_name[\s\S]*char_length[\s\S]*120/i);
  assert.match(sql, /NULLIF\(trim\((?:p_)?profile_name\), ''\)/i);
  assert.match(sql, /CASE WHEN name IS NULL OR trim\(name\)\s*=\s*'' THEN NULLIF\(trim\(profile_name\), ''\) ELSE name END/i);
  assert.match(sql, /claim_lm_panel_telegram_init_v2\(p_init_hash, p_actor_id, NULL\)/i);
  assert.match(sql, /REVOKE ALL ON FUNCTION public\.claim_lm_panel_telegram_init_v2/i);
});

test("Task 11: clean and additive actor claims pin the lm_users primary-key arbiter", () => {
  const migrationDir = path.join(__dirname, "../migrations");
  const cleanName = "2026-08-27-lm-panel-onboarding-reachability.sql";
  const additiveName = "2026-08-28-lm-panel-onboarding-reachability-arbiter.sql";
  assert.equal(additiveName > cleanName, true, "the production repair migration must be lexically later");

  for (const [label, name] of [["clean-install", cleanName], ["additive", additiveName]]) {
    const migrationPath = path.join(migrationDir, name);
    assert.equal(fs.existsSync(migrationPath), true, `${label} migration must exist`);
    const sql = fs.readFileSync(migrationPath, "utf8");
    const start = sql.search(/CREATE OR REPLACE FUNCTION public\.claim_lm_panel_telegram_init_v2\(/i);
    assert.notEqual(start, -1, `${label} migration must define the v2 actor claim`);
    const functionBody = sql.slice(start, sql.indexOf("$$;", start) + 3);
    assert.match(functionBody, /ON CONFLICT ON CONSTRAINT lm_users_pkey DO NOTHING/i, `${label} migration must pin lm_users_pkey`);
    assert.doesNotMatch(functionBody, /ON CONFLICT\s*\(\s*uid\s*\)\s+DO NOTHING/i, `${label} migration must not use an ambiguous uid arbiter`);
    assert.match(functionBody, /UPDATE\s+public\.lm_users\s+AS\s+u[\s\S]*WHERE\s+u\.uid\s*=\s*bound_uid/i, `${label} migration must qualify the update uid`);
    assert.doesNotMatch(functionBody, /UPDATE\s+public\.lm_users\s+SET[\s\S]*WHERE\s+uid\s*=\s*bound_uid/i, `${label} migration must not use an ambiguous update uid`);
  }
});
