"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  DEPENDENCY_NAMES,
  createDependencyChecks,
  runPreflight,
  sanitizeEvidence,
} = require("./daily-preflight.js");

const REQUIRED = [
  "health",
  "telegram",
  "calendar",
  "call",
  "location",
  "email",
  "discovery",
  "gemini",
  "maps",
];

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function productionLikeEnv() {
  return {
    RAILWAY_PUBLIC_DOMAIN: "life-call.example.test",
    LM_TELEGRAM_BOT_TOKEN: "telegram-secret-token",
    LM_TELEGRAM_WEBHOOK_SECRET: "telegram-webhook-secret",
    COMPOSIO_API_KEY: "composio-secret-key",
    SUPABASE_URL: "https://project.supabase.co",
    SUPABASE_SERVICE_ROLE_KEY: "supabase-secret-key",
    TELNYX_API_KEY: "telnyx-secret-key",
    TELNYX_PHONE_NUMBER: "+12025550100",
    TELNYX_CONNECTION_ID: "call-control-123",
    RESEND_API_KEY: "resend-secret-key",
    LM_MAIL_FROM: "Life Manager <hello@aniccaai.com>",
    GEMINI_API_KEY: "gemini-secret-key",
    LIFE_MAPS_KEY: "maps-secret-key",
    PUBLIC_WSS: "wss://life-call.example.test",
    LM_CALL_SECRET: "production-like-call-secret-value",
  };
}

function successfulFetch(url, options = {}) {
  const target = String(url);
  if (target === "https://life-call.example.test/health") {
    return Promise.resolve(jsonResponse({ ok: true, service: "life-call", build: "build-123" }));
  }
  if (target.includes("api.telegram.org") && target.endsWith("/getWebhookInfo")) {
    return Promise.resolve(jsonResponse({
      ok: true,
      result: {
        url: "https://life-call.example.test/telegram",
        pending_update_count: 0,
        allowed_updates: ["callback_query", "edited_message", "message"],
      },
    }));
  }
  if (target.includes("/rest/v1/lm_users?") && target.includes("calendar_provider=in.")) {
    return Promise.resolve(jsonResponse([{ uid: "user-private-id", calendar_provider: "composio_gcal" }]));
  }
  if (target.includes("backend.composio.dev/api/v3/tools/execute/GOOGLECALENDAR_EVENTS_LIST")) {
    return Promise.resolve(jsonResponse({ successful: true, data: { items: [] } }));
  }
  if (target.endsWith("/v2/balance")) {
    return Promise.resolve(jsonResponse({ data: { balance: "3.25", currency: "USD" } }));
  }
  if (target.includes("/v2/phone_numbers?")) {
    return Promise.resolve(jsonResponse({ data: [{ phone_number: "+12025550100", status: "active", connection_id: "call-control-123" }] }));
  }
  if (target.endsWith("/v2/call_control_applications/call-control-123")) {
    return Promise.resolve(jsonResponse({
      data: {
        id: "call-control-123",
        active: true,
        webhook_event_url: "https://life-call.example.test/telnyx-events",
        outbound: { outbound_voice_profile_id: "profile-private-id" },
      },
    }));
  }
  if (target.endsWith("/v2/outbound_voice_profiles/profile-private-id")) {
    return Promise.resolve(jsonResponse({ data: { id: "profile-private-id", enabled: true } }));
  }
  if (target.includes("/rest/v1/lm_user_locations?")) {
    return Promise.resolve(jsonResponse([{ observed_at: "2026-07-21T00:00:00Z", expires_at: "2026-07-22T00:00:00Z" }]));
  }
  if (target === "https://api.resend.com/domains") {
    return Promise.resolve(jsonResponse({ data: [{ name: "aniccaai.com", status: "verified" }] }));
  }
  if (target.includes("/rest/v1/lm_users?telegram_chat_id=not.is.null")) {
    return Promise.resolve(jsonResponse([{
      uid: "user-private-id",
      telegram_chat_id: "private-chat-id",
      last_discovery_at: "2026-07-14T00:00:00Z",
      last_discovery_gate: "location",
      payout_destination: null,
    }]));
  }
  if (target.endsWith("/gemini-2.5-flash:generateContent")) {
    return Promise.resolve(jsonResponse({ candidates: [{ content: { parts: [{ text: "OK" }] } }] }));
  }
  if (target.includes("generativelanguage.googleapis.com/v1beta/models/")) {
    return Promise.resolve(jsonResponse({
      name: "models/gemini-2.5-flash-native-audio-preview-09-2025",
      supportedGenerationMethods: ["bidiGenerateContent"],
    }));
  }
  if (target === "https://routes.googleapis.com/directions/v2:computeRoutes") {
    assert.equal(options.method, "POST");
    return Promise.resolve(jsonResponse({ routes: [{ duration: "900s" }] }));
  }
  if (target.includes("maps.googleapis.com/maps/api/directions/json?")) {
    return Promise.resolve(jsonResponse({ status: "OK", routes: [{ legs: [{ duration: { value: 1200 } }] }] }));
  }
  throw new Error(`unexpected test URL: ${target}`);
}

test("manifest covers every required DAILY runtime dependency and real adapters pass with useful redacted evidence", async () => {
  assert.deepEqual(DEPENDENCY_NAMES, REQUIRED);
  const env = productionLikeEnv();
  const requests = [];
  const fetchImpl = (url, options = {}) => {
    requests.push({ url: String(url), method: options.method || "GET" });
    return successfulFetch(url, options);
  };
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const checks = createDependencyChecks({
    env,
    fetchImpl,
    nowMs,
    controlledL3: {
      email: {
        attempted: true, providerAccepted: true, inboxReceived: true, recipientOwned: true,
        checkedAt: new Date(nowMs).toISOString(), providerRef: "sha256:555555555555", messageIdRef: "sha256:333333333333",
      },
      telegram: {
        verified: true, checkedAt: new Date(nowMs).toISOString(),
        requestMessageRef: "sha256:111111111111", replyMessageRef: "sha256:222222222222",
        pendingUpdateSamples: [0], pendingUpdateCount: 0,
      },
    },
    webSocketProbe: async () => ({ opened: true, closeCode: 1008 }),
  });

  const report = await runPreflight({ checks, timeoutMs: 100, now: () => Date.parse("2026-07-21T06:00:00Z") });

  assert.equal(report.overallStatus, "pass");
  assert.equal(report.exitCode, 0);
  assert.deepEqual(report.summary, { required: 9, passed: 9, failed: 0, timedOut: 0 });
  assert.deepEqual(report.dependencies.map((item) => item.dependency), REQUIRED);
  for (const item of report.dependencies) {
    assert.equal(item.status, "pass", item.dependency);
    assert.equal(item.failureClass, null, item.dependency);
    assert.equal(typeof item.latencyMs, "number", item.dependency);
    assert.ok(item.evidence && Object.keys(item.evidence).length > 0, item.dependency);
  }
  const serialized = JSON.stringify(report);
  for (const secret of Object.values(env).filter((value) => /secret|private|\+1555/.test(value))) {
    assert.equal(serialized.includes(secret), false, `must redact ${secret}`);
  }
  assert.equal(serialized.includes("hello@aniccaai.com"), false);
  assert.equal(serialized.includes("+12025550100"), false);
  assert.equal(requests.some(({ url }) => /\/emails$|\/v2\/calls$|sendMessage|CREATE_EVENT|PATCH_EVENT/.test(url)), false);
  assert.deepEqual(
    requests.filter(({ method }) => method === "POST").map(({ url }) => new URL(url).pathname).sort(),
    [
      "/api/v3/tools/execute/GOOGLECALENDAR_EVENTS_LIST",
      "/bottelegram-secret-token/getWebhookInfo",
      "/directions/v2:computeRoutes",
      "/v1beta/models/gemini-2.5-flash:generateContent",
    ].sort(),
  );
});

test("explicit failure for every dependency is fail-closed and never becomes zero/empty success", async () => {
  const checks = REQUIRED.map((name) => ({
    name,
    run: async () => ({ ok: false, failureClass: "auth", evidence: { count: 0 } }),
  }));

  const report = await runPreflight({ checks, timeoutMs: 100 });

  assert.equal(report.overallStatus, "fail");
  assert.equal(report.exitCode, 1);
  assert.deepEqual(report.summary, { required: 9, passed: 0, failed: 9, timedOut: 0 });
  assert.ok(report.dependencies.every((item) => item.status === "fail"));
  assert.ok(report.dependencies.every((item) => item.failureClass === "auth"));
});

test("empty, false, and zero adapter results are invalid failures rather than success", async () => {
  const emptyValues = [undefined, null, false, 0, "", [], {}];
  const checks = REQUIRED.map((name, index) => ({ name, run: async () => emptyValues[index % emptyValues.length] }));

  const report = await runPreflight({ checks, timeoutMs: 100 });

  assert.equal(report.exitCode, 1);
  assert.equal(report.summary.passed, 0);
  assert.ok(report.dependencies.every((item) => item.status === "fail"));
  assert.ok(report.dependencies.every((item) => item.failureClass === "invalid_result"));
});

test("timeout for every dependency is classified and forces a nonzero exit", async () => {
  const checks = REQUIRED.map((name) => ({ name, run: () => new Promise(() => {}) }));

  const report = await runPreflight({ checks, timeoutMs: 5 });

  assert.equal(report.overallStatus, "fail");
  assert.equal(report.exitCode, 1);
  assert.deepEqual(report.summary, { required: 9, passed: 0, failed: 0, timedOut: 9 });
  assert.ok(report.dependencies.every((item) => item.status === "timeout"));
  assert.ok(report.dependencies.every((item) => item.failureClass === "timeout"));
});

test("thrown provider errors expose only a classification, never raw messages or secrets", async () => {
  const report = await runPreflight({
    checks: [{
      name: "health",
      run: async () => { throw new Error("Bearer super-secret hello@example.com +12025550100"); },
    }],
    timeoutMs: 100,
  });

  assert.equal(report.exitCode, 1);
  assert.equal(report.dependencies[0].failureClass, "dependency_error");
  assert.deepEqual(report.dependencies[0].evidence, { reason: "dependency_error" });
  assert.equal(JSON.stringify(report).includes("super-secret"), false);
  assert.equal(JSON.stringify(report).includes("hello@example.com"), false);
});

async function runNamed(name, options = {}) {
  const checks = createDependencyChecks(options);
  const check = checks.find((candidate) => candidate.name === name);
  assert.ok(check, `missing ${name} check`);
  return runPreflight({ checks: [check], timeoutMs: 100, now: options.now || Date.now });
}

test("email: restricted send-only key needs controlled POST /emails plus inbox receipt proof", async () => {
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const env = productionLikeEnv();
  let domainsCalled = false;
  const withoutProof = await runNamed("email", {
    env,
    nowMs,
    fetchImpl: async () => {
      domainsCalled = true;
      return jsonResponse({ name: "restricted_api_key" }, 401);
    },
    now: () => nowMs,
  });
  assert.equal(withoutProof.dependencies[0].status, "fail");

  const withProof = await runNamed("email", {
    env,
    nowMs,
    fetchImpl: async () => {
      domainsCalled = true;
      return jsonResponse({ name: "restricted_api_key" }, 401);
    },
    controlledL3: {
      email: {
        attempted: true, providerAccepted: true,
        inboxReceived: true,
        checkedAt: new Date(nowMs).toISOString(),
        providerRef: "sha256:111111111111",
        messageIdRef: "sha256:0123456789ab",
        recipientOwned: true,
      },
    },
    now: () => nowMs,
  });
  assert.equal(withProof.dependencies[0].status, "pass");
  assert.equal(withProof.dependencies[0].evidence.inbox_receipt, true);
  assert.equal(domainsCalled, false, "a non-sending endpoint must not be used as send-scope proof");

  const rejectedProof = await runNamed("email", {
    env,
    nowMs,
    fetchImpl: async () => { throw new Error("must not fetch"); },
    controlledL3: {
      email: {
        attempted: true,
        providerAccepted: false,
        inboxReceived: false,
        recipientOwned: true,
        checkedAt: new Date(nowMs).toISOString(),
      },
    },
    now: () => nowMs,
  });
  assert.equal(rejectedProof.dependencies[0].status, "fail");
  assert.equal(rejectedProof.dependencies[0].evidence.controlled_send_attempted, true);
  assert.equal(rejectedProof.dependencies[0].evidence.provider_accepted, false);
});

test("maps: preflight accepts the same drive-only or transit-only results and reports degradation", async () => {
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const env = productionLikeEnv();
  const driveOnly = await runNamed("maps", {
    env,
    nowMs,
    fetchImpl: async (url) => String(url).includes("routes.googleapis.com")
      ? jsonResponse({ routes: [{ duration: "900s" }] })
      : jsonResponse({ status: "REQUEST_DENIED", routes: [] }),
    now: () => nowMs,
  });
  assert.equal(driveOnly.dependencies[0].status, "pass");
  assert.deepEqual(driveOnly.dependencies[0].evidence.degraded_providers, ["legacy_transit"]);

  const transitOnly = await runNamed("maps", {
    env,
    nowMs,
    fetchImpl: async (url) => String(url).includes("routes.googleapis.com")
      ? jsonResponse({ error: "unavailable" }, 403)
      : jsonResponse({ status: "OK", routes: [{ legs: [{ duration: { value: 1200 } }] }] }),
    now: () => nowMs,
  });
  assert.equal(transitOnly.dependencies[0].status, "pass");
  assert.deepEqual(transitOnly.dependencies[0].evidence.degraded_providers, ["routes_drive"]);
});

test("telegram: exact production webhook URL and fresh provider-to-webhook round-trip proof are required", async () => {
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const env = productionLikeEnv();
  const telegramFetch = async () => jsonResponse({
    ok: true,
    result: {
      url: "https://wrong.example.test/not-telegram",
      pending_update_count: 0,
      allowed_updates: ["message", "edited_message", "callback_query"],
    },
  });
  const wrongUrl = await runNamed("telegram", { env, fetchImpl: telegramFetch, nowMs, now: () => nowMs });
  assert.equal(wrongUrl.dependencies[0].status, "fail");

  const exactFetch = async () => jsonResponse({
    ok: true,
    result: {
      url: "https://life-call.example.test/telegram",
      pending_update_count: 0,
      allowed_updates: ["message", "edited_message", "callback_query"],
    },
  });
  const noProof = await runNamed("telegram", { env, fetchImpl: exactFetch, nowMs, now: () => nowMs });
  assert.equal(noProof.dependencies[0].status, "fail");
  const verified = await runNamed("telegram", {
    env,
    fetchImpl: exactFetch,
    nowMs,
    controlledL3: {
      telegram: {
        verified: true,
        checkedAt: new Date(nowMs).toISOString(),
        requestMessageRef: "sha256:111111111111",
        replyMessageRef: "sha256:222222222222",
        pendingUpdateSamples: [0], pendingUpdateCount: 0,
      },
    },
    now: () => nowMs,
  });
  assert.equal(verified.dependencies[0].status, "pass");
  assert.equal(verified.dependencies[0].evidence.round_trip_verified, true);
});

test("call: production PUBLIC_WSS /ws, non-placeholder LM_CALL_SECRET, and bridge auth-gate reachability are required", async () => {
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const baseEnv = productionLikeEnv();
  delete baseEnv.PUBLIC_WSS;
  delete baseEnv.LM_CALL_SECRET;
  const telnyxFetch = async (url) => {
    const target = String(url);
    if (target.endsWith("/v2/balance")) return jsonResponse({ data: { balance: "3.25", currency: "USD" } });
    if (target.includes("/v2/phone_numbers?")) return jsonResponse({ data: [{ phone_number: "+12025550100", status: "active", connection_id: "call-control-123" }] });
    if (target.endsWith("/v2/call_control_applications/call-control-123")) return jsonResponse({ data: {
      id: "call-control-123", active: true,
      webhook_event_url: "https://life-call.example.test/telnyx-events",
      outbound: { outbound_voice_profile_id: "profile-123" },
    } });
    if (target.endsWith("/v2/outbound_voice_profiles/profile-123")) return jsonResponse({ data: { id: "profile-123", enabled: true } });
    throw new Error(`unexpected ${target}`);
  };
  const missingRuntimeConfig = await runNamed("call", {
    env: baseEnv,
    fetchImpl: telnyxFetch,
    nowMs,
    webSocketProbe: async () => ({ opened: true, closeCode: 1008 }),
    now: () => nowMs,
  });
  assert.equal(missingRuntimeConfig.dependencies[0].status, "fail");

  const invalidPath = await runNamed("call", {
    env: { ...baseEnv, PUBLIC_WSS: "wss://life-call.example.test/wrong", LM_CALL_SECRET: "production-call-secret-value" },
    fetchImpl: telnyxFetch,
    nowMs,
    webSocketProbe: async () => ({ opened: true, closeCode: 1008 }),
    now: () => nowMs,
  });
  assert.equal(invalidPath.dependencies[0].status, "fail");

  const placeholderSecret = await runNamed("call", {
    env: { ...baseEnv, PUBLIC_WSS: "wss://life-call.example.test", LM_CALL_SECRET: "placeholder" },
    fetchImpl: telnyxFetch,
    nowMs,
    webSocketProbe: async () => ({ opened: true, closeCode: 1008 }),
    now: () => nowMs,
  });
  assert.equal(placeholderSecret.dependencies[0].status, "fail");

  let probedUrl = "";
  const ready = await runNamed("call", {
    env: { ...baseEnv, PUBLIC_WSS: "wss://life-call.example.test", LM_CALL_SECRET: "production-call-secret-value" },
    fetchImpl: telnyxFetch,
    nowMs,
    webSocketProbe: async (url) => { probedUrl = url; return { opened: true, closeCode: 1008 }; },
    now: () => nowMs,
  });
  assert.equal(ready.dependencies[0].status, "pass");
  assert.equal(probedUrl, "wss://life-call.example.test/ws");
});

test("call: each Telnyx binding and auth gate mismatch fails nonzero", async (t) => {
  const env = productionLikeEnv();
  const cases = [
    ["number_connection", (url, body) => url.includes("/phone_numbers?") ? { data: [{ phone_number: "+12025550100", status: "active", connection_id: "wrong" }] } : body],
    ["application_id", (url, body) => url.includes("/call_control_applications/") ? { data: { ...body.data, id: "wrong" } } : body],
    ["profile", (url, body) => url.includes("/outbound_voice_profiles/") ? { data: { ...body.data, enabled: false } } : body],
    ["webhook", (url, body) => url.includes("/call_control_applications/") ? { data: { ...body.data, webhook_event_url: "https://wrong.example/telnyx-events" } } : body],
  ];
  for (const [name, mutate] of cases) await t.test(name, async () => {
    const fetchImpl = async (url, options) => {
      const response = await successfulFetch(url, options);
      const body = await response.json();
      return jsonResponse(mutate(String(url), body));
    };
    const report = await runNamed("call", { env, fetchImpl, webSocketProbe: async () => ({ opened: true, closeCode: 1008 }) });
    assert.equal(report.exitCode, 1);
    assert.equal(report.dependencies[0].status, "fail");
  });
  const authMismatch = await runNamed("call", {
    env, fetchImpl: successfulFetch, webSocketProbe: async () => ({ opened: true, closeCode: 1000 }),
  });
  assert.equal(authMismatch.exitCode, 1);
});

test("location: required scheduler cohort user must have a present unexpired live-location row", async () => {
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const env = productionLikeEnv();
  const cohort = [{ uid: "synthetic-user" }];
  const missing = await runNamed("location", {
    env,
    nowMs,
    fetchImpl: async (url) => String(url).includes("lm_user_locations") ? jsonResponse([]) : jsonResponse(cohort),
    now: () => nowMs,
  });
  assert.equal(missing.dependencies[0].status, "fail");
  const stale = await runNamed("location", {
    env,
    nowMs,
    fetchImpl: async (url) => String(url).includes("lm_user_locations")
      ? jsonResponse([{ observed_at: "2026-07-20T00:00:00Z", expires_at: "2026-07-21T05:59:59Z" }])
      : jsonResponse(cohort),
    now: () => nowMs,
  });
  assert.equal(stale.dependencies[0].status, "fail");
});

test("gemini: same credential proves Live bidi and DAILY gemini-2.5-flash generateContent", async () => {
  const env = productionLikeEnv();
  const calls = [];
  const report = await runNamed("gemini", {
    env,
    fetchImpl: async (url, options = {}) => {
      calls.push({ url: String(url), method: options.method || "GET", key: options.headers && options.headers["x-goog-api-key"] });
      if (String(url).endsWith(":generateContent")) return jsonResponse({ candidates: [{ content: { parts: [{ text: "OK" }] } }] });
      return jsonResponse({
        name: "models/gemini-2.5-flash-native-audio-preview-09-2025",
        supportedGenerationMethods: ["bidiGenerateContent"],
      });
    },
  });
  assert.equal(report.dependencies[0].status, "pass");
  assert.equal(calls.some((call) => call.url.endsWith("/gemini-2.5-flash:generateContent") && call.method === "POST"), true);
  assert.ok(calls.every((call) => call.key === env.GEMINI_API_KEY));
});

test("gemini: missing Live bidi or standard generateContent each fails nonzero", async (t) => {
  const env = productionLikeEnv();
  for (const missing of ["bidi", "standard"]) await t.test(missing, async () => {
    const report = await runNamed("gemini", {
      env,
      fetchImpl: async (url) => String(url).endsWith(":generateContent")
        ? jsonResponse(missing === "standard" ? { candidates: [] } : { candidates: [{}] })
        : jsonResponse({
          name: "models/gemini-2.5-flash-native-audio-preview-09-2025",
          supportedGenerationMethods: missing === "bidi" ? [] : ["bidiGenerateContent"],
        }),
    });
    assert.equal(report.exitCode, 1);
    assert.equal(report.dependencies[0].status, "fail");
  });
});

test("evidence sanitizer redacts query values, phones, email, IDs, provider keys, and nested messages by value", () => {
  const fakeStripeKey = ["sk", "live", "abcdefghijklmnop"].join("_");
  const fakeProviderToken = ["tg", "abcdefghijklmnop"].join("_");
  const raw = {
    note: "https://example.test/path?token=query-secret&chat_id=99887766",
    opaquePanel: "https://example.test/panel/opaquePanelTokenAbcd1234",
    botPath: "https://api.telegram.org/bot123456789:providerSecretValue/getMe",
    domestic: "090-1234-5678",
    international: "+81 90 1234 5678",
    contact: "person@example.test",
    identifiers: "chat 123456789 user_id=987654321",
    bearer: "Bearer provider-secret",
    provider: { message: "api_key=sk_live_placeholder token=tg_placeholder" },
  };
  const serialized = JSON.stringify(sanitizeEvidence(raw));
  for (const forbidden of [
    "query-secret", "99887766", "090-1234-5678", "+81 90 1234 5678", "person@example.test",
    "123456789", "987654321", "provider-secret", "sk_live_placeholder", "tg_placeholder",
    "opaquePanelTokenAbcd1234", "providerSecretValue",
  ]) assert.equal(serialized.includes(forbidden), false, `leaked ${forbidden}`);
});

test("calendar: target selection is the scheduler supported-provider cohort (phone and payment optional)", async () => {
  const nowMs = Date.parse("2026-07-21T06:00:00Z");
  const env = productionLikeEnv();
  const requested = [];
  const previousNow = Date.now;
  Date.now = () => nowMs;
  let report;
  try {
    report = await runNamed("calendar", {
      env,
      nowMs,
      now: () => nowMs,
      fetchImpl: async (url) => {
        requested.push(String(url));
        if (String(url).includes("/rest/v1/lm_users?")) return jsonResponse([{ uid: "synthetic-user" }]);
        return jsonResponse({ successful: true, data: { items: [] } });
      },
    });
  } finally {
    Date.now = previousNow;
  }
  assert.equal(report.dependencies[0].status, "pass");
  const selectorUrl = new URL(requested.find((url) => url.includes("/rest/v1/lm_users?")));
  assert.equal(selectorUrl.searchParams.get("phone"), null);
  assert.equal(selectorUrl.searchParams.get("or"), null);
  assert.equal(selectorUrl.searchParams.get("calendar_provider"), "in.(composio_gcal,pipedream_gcal)");
});
