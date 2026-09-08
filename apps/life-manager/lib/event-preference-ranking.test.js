"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { buildRollingEventCoverage } = require("./rolling-event-coverage.js");
const { collectLumaInventory } = require("./luma-discovery.js");
const { normalizeLumaEventDetail } = require("./luma-event-detail.js");
const { buildLumaDateInventory } = require("./luma-date-inventory.js");
const {
  eligibleRankedCandidates,
  inferProviderCandidateRanking,
  inferEventPreferenceRanking,
  isVerifiedEventPreferenceRanking,
  validateProviderCandidateRanking,
  validateEventPreferenceRanking,
} = require("./event-preference-ranking.js");

const PROVIDER_CANDIDATES = Object.freeze([
  Object.freeze({ provider: "luma", event_ref: "luma-event://event/yc-ai", canonical_url: "https://luma.com/yc-ai", title: "YC AI Hackathon", body: "Y Combinator hackathon in Tokyo" }),
  Object.freeze({ provider: "luma", event_ref: "luma-event://event/open-lt", canonical_url: "https://luma.com/open-lt", title: "AI Lightning Talks", body: "Open five-minute LT applications" }),
  Object.freeze({ provider: "luma", event_ref: "luma-event://event/agents", canonical_url: "https://luma.com/agents", title: "AI Agent Night", body: "LLM agents" }),
  Object.freeze({ provider: "connpass", event_ref: "connpass-event://event/400001", canonical_url: "https://example.connpass.com/event/400001/", title: "Web3 Builders", body: "Crypto engineering" }),
  Object.freeze({ provider: "peatix", event_ref: "peatix-event://event/500001", canonical_url: "https://peatix.com/event/500001", title: "Startup Founders", body: "Founder and VC meetup" }),
  Object.freeze({ provider: "peatix", event_ref: "peatix-event://event/500002", canonical_url: "https://peatix.com/event/500002", title: "Pottery Social", body: "Make a bowl" }),
  Object.freeze({ provider: "meetup", event_ref: "meetup-event://event/unknown", canonical_url: "https://www.meetup.com/example/events/unknown", title: "Untitled gathering", body: "No useful description" }),
]);

function providerDecision() {
  return {
    ranked_events: [
      { event_ref: "peatix-event://event/500002", priority_class: "other", preference_fit: "weak", preference_reason: "Topic is unrelated." },
      { event_ref: "luma-event://event/agents", priority_class: "ai", preference_fit: "strong", preference_reason: "Directly about AI agents." },
      { event_ref: "meetup-event://event/unknown", priority_class: "other", preference_fit: "unknown", preference_reason: "Description is insufficient." },
      { event_ref: "peatix-event://event/500001", priority_class: "startup", preference_fit: "moderate", preference_reason: "Founder audience is relevant." },
      { event_ref: "luma-event://event/open-lt", priority_class: "open_talk", preference_fit: "strong", preference_reason: "Open lightning-talk applications." },
      { event_ref: "connpass-event://event/400001", priority_class: "crypto", preference_fit: "strong", preference_reason: "Directly about crypto builders." },
      { event_ref: "luma-event://event/yc-ai", priority_class: "yc_hackathon", preference_fit: "strong", preference_reason: "Official YC-style hackathon opportunity." },
    ],
  };
}

test("provider-neutral ranking orders YC, open talk, AI, crypto, startup, then weak or unknown", () => {
  const ranking = validateProviderCandidateRanking(providerDecision(), {
    candidates: PROVIDER_CANDIDATES,
    preferences: "Tokyo YC LT AI crypto startup events",
  });

  assert.deepEqual(ranking.ranked_events.map((row) => row.event_ref), [
    "luma-event://event/yc-ai",
    "luma-event://event/open-lt",
    "luma-event://event/agents",
    "connpass-event://event/400001",
    "peatix-event://event/500001",
    "peatix-event://event/500002",
    "meetup-event://event/unknown",
  ]);
});

test("provider-neutral ranking preserves weak and unknown rows but never returns them for auto apply", async () => {
  const ranking = await inferProviderCandidateRanking({
    candidates: PROVIDER_CANDIDATES,
    preferences: "Tokyo YC LT AI crypto startup events",
  }, { generateDecision: async () => providerDecision() });

  assert.deepEqual(ranking.ranked_events.map((row) => row.auto_apply_eligible), [
    true, true, true, true, true, false, false,
  ]);
  assert.deepEqual(eligibleRankedCandidates(ranking).map((row) => row.event_ref), [
    "luma-event://event/yc-ai",
    "luma-event://event/open-lt",
    "luma-event://event/agents",
    "connpass-event://event/400001",
    "peatix-event://event/500001",
  ]);
});

test("provider ranking requires an explicit target-topic signal even when the model over-ranks an unrelated business event", () => {
  for (const body of ["酒粕・米麹から生まれるビジネスの話", "酒粕・米麹の専門家が登壇するビジネスイベント"]) {
    const fermentation = {
      provider: "luma",
      event_ref: "luma-event://event/fermentation-business",
      canonical_url: "https://luma.com/fermentation-business",
      title: "発酵でひらく、素材の新しい可能性",
      body,
    };
    const ranking = validateProviderCandidateRanking({ ranked_events: [{
      event_ref: fermentation.event_ref,
      priority_class: "startup",
      preference_fit: "moderate",
      preference_reason: "The model inferred a business opportunity.",
    }] }, { candidates: [fermentation], preferences: "AI crypto hackathon lightning talk startup events" });
    assert.equal(ranking.ranked_events[0].auto_apply_eligible, false, body);
    assert.deepEqual(eligibleRankedCandidates(ranking), [], body);
  }
});

test("provider ranking preserves public onsite location context when title and body omit Tokyo", async () => {
  const candidate = Object.freeze({
    provider: "luma",
    event_ref: "luma-event://event/location-context",
    canonical_url: "https://luma.com/location-context",
    title: "AI Builders Evening",
    body: "A practical meetup for agent builders.",
    venue_name: "Shibuya AI Hub",
    venue_address: "Tokyo, Shibuya",
    attendance_mode: "in_person",
  });
  let transported;
  const ranking = await inferProviderCandidateRanking({
    candidates: [candidate],
    preferences: "Tokyo in-person AI events",
  }, {
    generateDecision: async ({ prompt }) => {
      transported = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      return { ranked_events: [{
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "strong",
        preference_reason: "Verified in-person Tokyo AI event.",
      }] };
    },
  });

  assert.deepEqual(transported, [candidate]);
  assert.deepEqual(eligibleRankedCandidates(ranking).map((row) => row.event_ref), [candidate.event_ref]);
  assert.equal(ranking.ranked_events[0].venue_name, candidate.venue_name);
  assert.equal(ranking.ranked_events[0].venue_address, candidate.venue_address);
  assert.equal(ranking.ranked_events[0].attendance_mode, candidate.attendance_mode);
});

test("provider ranking fails closed on malformed optional location context", async () => {
  const base = {
    provider: "luma",
    event_ref: "luma-event://event/location-invalid",
    canonical_url: "https://luma.com/location-invalid",
    title: "AI Builders Evening",
    body: "A practical meetup for agent builders.",
  };
  const modelDecision = {
    ranked_events: [{
      event_ref: base.event_ref,
      priority_class: "ai",
      preference_fit: "strong",
      preference_reason: "Verified AI event.",
    }],
  };
  for (const [field, value] of [
    ["venue_name", { private: true }],
    ["venue_address", "x".repeat(1_001)],
    ["attendance_mode", "bad\u0000mode"],
  ]) {
    await assert.rejects(() => inferProviderCandidateRanking({
      candidates: [{ ...base, [field]: value }],
      preferences: "Tokyo in-person AI events",
    }, { generateDecision: async () => modelDecision }), /event preference ranking invalid/i);
  }
});

test("Gemini provider ranking strips unsupported schema keywords only from the transport payload", async () => {
  let request;
  const ranking = await inferProviderCandidateRanking({
    candidates: PROVIDER_CANDIDATES,
    preferences: "Tokyo YC LT AI crypto startup events",
  }, {
    apiKey: "fixture-key",
    fetchImpl: async (_url, options) => {
      request = JSON.parse(options.body);
      return { ok: true, json: async () => ({
        candidates: [{ content: { parts: [{ text: JSON.stringify(providerDecision()) }] } }],
      }) };
    },
  });

  const schema = request.generationConfig.responseSchema;
  assert.equal(Object.hasOwn(schema, "additionalProperties"), false);
  assert.equal(Object.hasOwn(schema.properties.ranked_events.items, "additionalProperties"), false);
  assert.equal(eligibleRankedCandidates(ranking).length, 5);
});

test("provider ranking chunks a large inventory and still validates every candidate exactly once", async () => {
  const candidates = Object.freeze(Array.from({ length: 51 }, (_, index) => Object.freeze({
    provider: "connpass",
    event_ref: `connpass-event://event/${700_000 + index}`,
    canonical_url: `https://tokyo-ai.connpass.com/event/${700_000 + index}/`,
    title: `AI Builders ${index}`,
    body: "Tokyo AI engineering event",
  })));
  const chunkSizes = [];
  const ranking = await inferProviderCandidateRanking({
    candidates,
    preferences: "Tokyo AI crypto startup events",
  }, {
    apiKey: "fixture-key",
    fetchImpl: async (_url, options) => {
      const prompt = JSON.parse(options.body).contents[0].parts[0].text;
      const chunk = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      chunkSizes.push(chunk.length);
      return { ok: true, json: async () => ({ candidates: [{ content: { parts: [{ text: JSON.stringify({
        ranked_events: chunk.map((candidate) => ({
          event_ref: candidate.event_ref,
          priority_class: "ai",
          preference_fit: "moderate",
          preference_reason: "Verified AI event.",
        })),
      }) }] } }] }) };
    },
  });

  assert.equal(chunkSizes.length, 17);
  assert.equal(Math.max(...chunkSizes), 3);
  assert.equal(chunkSizes.reduce((total, size) => total + size, 0), 51);
  assert.equal(ranking.ranked_events.length, 51);
  assert.equal(new Set(ranking.ranked_events.map((row) => row.event_ref)).size, 51);
});

test("provider ranking also bounds each chunk by UTF-8 payload bytes", async () => {
  const candidates = Object.freeze(Array.from({ length: 40 }, (_, index) => Object.freeze({
    provider: "connpass",
    event_ref: `connpass-event://event/${800_000 + index}`,
    canonical_url: `https://tokyo-ai.connpass.com/event/${800_000 + index}/`,
    title: `AI Builders ${index}`,
    body: "x".repeat(8_000),
  })));
  const chunkSizes = [];
  const chunkBytes = [];
  const ranking = await inferProviderCandidateRanking({ candidates, preferences: "Tokyo AI events" }, {
    generateDecision: async ({ prompt }) => {
      const chunk = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      chunkSizes.push(chunk.length);
      chunkBytes.push(Buffer.byteLength(JSON.stringify(chunk), "utf8"));
      return { ranked_events: chunk.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "moderate",
        preference_reason: "Verified AI event.",
      })) };
    },
  });

  assert.equal(chunkSizes.length > 1, true);
  assert.equal(chunkSizes.reduce((total, size) => total + size, 0), 40);
  assert.equal(Math.max(...chunkBytes) <= 24_000, true);
  assert.equal(ranking.ranked_events.length, 40);
});

test("provider ranking retries one transient chunk failure without dropping candidates", async () => {
  let attempts = 0;
  const ranking = await inferProviderCandidateRanking({
    candidates: PROVIDER_CANDIDATES,
    preferences: "Tokyo YC LT AI crypto startup events",
  }, {
    apiKey: "fixture-key",
    fetchImpl: async () => {
      attempts += 1;
      if (attempts === 1) {
        const error = new Error("transient timeout");
        error.name = "TimeoutError";
        throw error;
      }
      return { ok: true, json: async () => ({
        candidates: [{ content: { parts: [{ text: JSON.stringify(providerDecision()) }] } }],
      }) };
    },
  });

  assert.equal(attempts, 2);
  assert.equal(ranking.ranked_events.length, PROVIDER_CANDIDATES.length);
});

test("provider ranking bisects a persistently unavailable multi-event chunk without dropping candidates", async () => {
  const candidates = Object.freeze(Array.from({ length: 4 }, (_, index) => Object.freeze({
    provider: "connpass",
    event_ref: `connpass-event://event/${900_000 + index}`,
    canonical_url: `https://tokyo-ai.connpass.com/event/${900_000 + index}/`,
    title: `Tokyo Builders ${index}`,
    body: "Public engineering event description.",
  })));
  const chunkSizes = [];
  const ranking = await inferProviderCandidateRanking({ candidates, preferences: "Tokyo AI events" }, {
    generateDecision: async ({ prompt }) => {
      const chunk = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      chunkSizes.push(chunk.length);
      if (chunk.length > 2) throw new DOMException("bounded timeout", "TimeoutError");
      return { ranked_events: chunk.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "moderate",
        preference_reason: "Verified AI event.",
      })) };
    },
  });

  assert.deepEqual(chunkSizes, [4, 4, 2, 2]);
  assert.equal(ranking.ranked_events.length, 4);
  assert.equal(new Set(ranking.ranked_events.map((row) => row.event_ref)).size, 4);
});

test("provider ranking processes independent chunks with at most three concurrent requests", async () => {
  const candidates = Object.freeze(Array.from({ length: 60 }, (_, index) => Object.freeze({
    provider: "connpass",
    event_ref: `connpass-event://event/${910_000 + index}`,
    canonical_url: `https://tokyo-ai.connpass.com/event/${910_000 + index}/`,
    title: `Tokyo AI ${index}`,
    body: "x".repeat(8_000),
  })));
  let active = 0;
  let maximumActive = 0;
  const ranking = await inferProviderCandidateRanking({ candidates, preferences: "Tokyo AI events" }, {
    generateDecision: async ({ prompt }) => {
      const chunk = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise((resolve) => setTimeout(resolve, 10));
      active -= 1;
      return { ranked_events: chunk.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "moderate",
        preference_reason: "Verified AI event.",
      })) };
    },
  });

  assert.equal(maximumActive, 3);
  assert.equal(ranking.ranked_events.length, 60);
  assert.equal(new Set(ranking.ranked_events.map((row) => row.event_ref)).size, 60);
});

test("provider ranking sends a compact public body to the model but preserves the full candidate body", async () => {
  const fullBody = "x".repeat(8_000);
  const candidate = Object.freeze({
    provider: "connpass",
    event_ref: "connpass-event://event/920000",
    canonical_url: "https://tokyo-ai.connpass.com/event/920000/",
    title: "Tokyo AI Builders",
    body: fullBody,
  });
  let transportedBody = "";
  const ranking = await inferProviderCandidateRanking({ candidates: [candidate], preferences: "Tokyo AI events" }, {
    generateDecision: async ({ prompt }) => {
      const transported = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      transportedBody = transported[0].body;
      return { ranked_events: [{
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "strong",
        preference_reason: "Verified AI event.",
      }] };
    },
  });

  assert.equal(transportedBody.length, 1_000);
  assert.equal(ranking.ranked_events[0].body, fullBody);
});

test("provider ranking gives each bounded model request a forty-five second deadline", async () => {
  let timeoutMs = null;
  await inferProviderCandidateRanking({
    candidates: PROVIDER_CANDIDATES,
    preferences: "Tokyo AI crypto startup events",
  }, {
    generateDecision: async (request) => {
      timeoutMs = request.timeoutMs;
      return providerDecision();
    },
  });

  assert.equal(timeoutMs, 45_000);
});

test("provider ranking reports bounded aggregate request retry and bisect timing without event data", async () => {
  const candidates = PROVIDER_CANDIDATES.slice(0, 4);
  let clock = 1_000;
  let firstLargeAttempts = 0;
  let audit;
  const ranking = await inferProviderCandidateRanking({ candidates, preferences: "Tokyo AI events" }, {
    nowMs: () => clock,
    onAudit: async (value) => { audit = value; },
    generateDecision: async ({ prompt }) => {
      const chunk = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      clock += 25;
      if (chunk.length === 4) {
        firstLargeAttempts += 1;
        throw new Error("omitted rows");
      }
      return { ranked_events: chunk.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "moderate",
        preference_reason: "Verified AI event.",
      })) };
    },
  });
  assert.equal(ranking.ranked_events.length, 4);
  assert.equal(firstLargeAttempts, 2);
  assert.deepEqual(audit, {
    schema_version: 1,
    request_count: 4,
    retry_count: 1,
    bisect_count: 1,
    total_request_ms: 100,
    max_request_ms: 25,
    elapsed_ms: 100,
  });
  assert.deepEqual(Object.keys(audit), [
    "schema_version", "request_count", "retry_count", "bisect_count",
    "total_request_ms", "max_request_ms", "elapsed_ms",
  ]);
});

test("provider ranking bounds a long public event description before validation and transport", async () => {
  const candidate = Object.freeze({
    ...PROVIDER_CANDIDATES[0],
    body: `AI builders ${"x".repeat(9_000)}`,
  });
  let transported;
  const ranking = await inferProviderCandidateRanking({
    candidates: [candidate], preferences: "Tokyo AI events",
  }, {
    generateDecision: async ({ prompt }) => {
      transported = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      return { ranked_events: transported.map((event) => ({
        event_ref: event.event_ref,
        priority_class: "ai",
        preference_fit: "strong",
        preference_reason: "Direct AI event.",
      })) };
    },
  });
  assert.equal(ranking.ranked_events.length, 1);
  assert.equal(transported[0].body.length, 1_000);
});

async function fixtureSnapshot(slugs = ["ai-night", "pottery-social", "crypto-builders"]) {
  const coverage = buildRollingEventCoverage({
    tenantId: "dais-local",
    timeZone: "Asia/Tokyo",
    now: "2026-08-01T16:00:00.000Z",
    resolvedDays: [],
  });
  let round = 0;
  const inventory = await collectLumaInventory({
    readSnapshot: async () => {
      round += 1;
      return round === 1 ? slugs.map((slug) => ({
        href: `https://luma.com/${slug}`,
        title: slug,
        cardText: `${slug} 19:00`,
        timelineText: "8月2日 日曜日",
      })) : [];
    },
    advance: async () => ({ atEnd: true, scrollHeight: 100 }),
    stableEndRounds: 1,
  });
  const details = slugs.map((slug, index) => normalizeLumaEventDetail({
    canonicalUrl: `https://luma.com/${slug}`,
    jsonLd: [{
      "@type": "Event",
      name: slug.replaceAll("-", " "),
      startDate: `2026-08-02T${String(9 + index).padStart(2, "0")}:00:00.000Z`,
      endDate: `2026-08-02T${String(10 + index).padStart(2, "0")}:00:00.000Z`,
      eventAttendanceMode: "https://schema.org/OfflineEventAttendanceMode",
      eventStatus: "https://schema.org/EventScheduled",
      location: { "@type": "Place", name: `Tokyo venue ${index + 1}` },
    }],
    controls: ["Register"],
  }));
  return buildLumaDateInventory({
    coverage,
    inventory,
    details,
    now: "2026-08-02T01:00:00.000Z",
  });
}

const PREFERENCES = "AI、crypto、英語の会話、founderとの出会いを高く評価する。それ以外も除外しない。";

function decision(overrides = {}) {
  return {
    ranked_events: [
      {
        event_ref: "luma-event://event/ai-night",
        preference_fit: "strong",
        preference_reason: "AIへの関心と直接一致するため最上位です。",
      },
      {
        event_ref: "luma-event://event/crypto-builders",
        preference_fit: "strong",
        preference_reason: "crypto builderとの接点が期待できます。",
      },
      {
        event_ref: "luma-event://event/pottery-social",
        preference_fit: "weak",
        preference_reason: "直接の好みとは離れますが新しい人との接点として候補に残します。",
      },
    ],
    ...overrides,
  };
}

test("accepts one immutable ranking that preserves every candidate including weak fits", async () => {
  const snapshot = await fixtureSnapshot();
  const ranking = validateEventPreferenceRanking(decision(), {
    dateInventory: snapshot,
    date: "2026-08-02",
    preferences: PREFERENCES,
  });

  assert.equal(ranking.date, "2026-08-02");
  assert.equal(ranking.inventory_snapshot_id, snapshot.inventory_snapshot_id);
  assert.deepEqual(ranking.ranked_events.map((row) => row.event_ref), [
    "luma-event://event/ai-night",
    "luma-event://event/crypto-builders",
    "luma-event://event/pottery-social",
  ]);
  assert.equal(ranking.ranked_events.at(-1).preference_fit, "weak");
  assert.match(ranking.preference_profile_hash, /^sha256:[0-9a-f]{64}$/);
  assert.equal(Object.isFrozen(ranking), true);
  assert.equal(isVerifiedEventPreferenceRanking(ranking), true);
  assert.equal(isVerifiedEventPreferenceRanking(structuredClone(ranking)), false);
});

test("rejects omitted, duplicate, unknown, malformed, or exclusion-shaped model output", async () => {
  const snapshot = await fixtureSnapshot();
  const valid = decision().ranked_events;
  const cases = [
    { ranked_events: valid.slice(0, 2) },
    { ranked_events: [valid[0], valid[0], valid[2]] },
    { ranked_events: [valid[0], valid[1], { ...valid[2], event_ref: "luma-event://event/unknown" }] },
    { ranked_events: [valid[0], valid[1], { ...valid[2], preference_fit: "excluded" }] },
    { ranked_events: [valid[0], valid[1], { ...valid[2], exclude: true }] },
  ];
  for (const value of cases) {
    assert.throws(() => validateEventPreferenceRanking(value, {
      dateInventory: snapshot,
      date: "2026-08-02",
      preferences: PREFERENCES,
    }), /event preference ranking invalid/i);
  }
});

test("Gemini receives all candidates as untrusted data and preferences can only change order", async () => {
  const snapshot = await fixtureSnapshot();
  let request;
  const ranking = await inferEventPreferenceRanking({
    dateInventory: snapshot,
    date: "2026-08-02",
    preferences: PREFERENCES,
  }, {
    apiKey: "fixture-key",
    fetchImpl: async (url, options) => {
      request = { url, options, body: JSON.parse(options.body) };
      return {
        ok: true,
        json: async () => ({
          candidates: [{ content: { parts: [{ text: JSON.stringify(decision()) }] } }],
        }),
      };
    },
  });

  assert.equal(ranking.ranked_events.length, 3);
  assert.match(request.url, /gemini-2\.5-flash:generateContent/);
  assert.equal(request.options.headers["x-goog-api-key"], "fixture-key");
  const prompt = request.body.contents[0].parts[0].text;
  assert.match(prompt, /untrusted data/i);
  assert.match(prompt, /Never omit, exclude, discard/i);
  assert.match(prompt, /pottery social/i);
  assert.match(prompt, /それ以外も除外しない/);
  assert.equal(request.body.generationConfig.responseMimeType, "application/json");
  assert.equal(request.body.generationConfig.temperature, 0);
  assert.equal(Object.hasOwn(request.body.generationConfig.responseSchema, "additionalProperties"), false);
  assert.equal(Object.hasOwn(request.body.generationConfig.responseSchema.properties.ranked_events.items, "additionalProperties"), false);
});

test("model failure and invalid JSON never become a keyword ranking fallback", async () => {
  const snapshot = await fixtureSnapshot();
  const input = { dateInventory: snapshot, date: "2026-08-02", preferences: PREFERENCES };
  await assert.rejects(inferEventPreferenceRanking(input, {
    apiKey: "fixture-key",
    fetchImpl: async () => ({ ok: false, status: 503 }),
  }), /event preference ranking unavailable/i);
  await assert.rejects(inferEventPreferenceRanking(input, {
    apiKey: "fixture-key",
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({ candidates: [{ content: { parts: [{ text: "not json" }] } }] }),
    }),
  }), /event preference ranking unavailable/i);
});

test("a fully read empty day returns a verified empty ranking without calling the model", async () => {
  const snapshot = await fixtureSnapshot();
  let called = false;
  const ranking = await inferEventPreferenceRanking({
    dateInventory: snapshot,
    date: "2026-08-03",
    preferences: PREFERENCES,
  }, {
    apiKey: "fixture-key",
    fetchImpl: async () => { called = true; },
  });
  assert.equal(called, false);
  assert.deepEqual(ranking.ranked_events, []);
  assert.equal(isVerifiedEventPreferenceRanking(ranking), true);
});

test("a structured generator still returns a verified preference ranking", async () => {
  const snapshot = await fixtureSnapshot();
  let request;
  const ranking = await inferEventPreferenceRanking({
    dateInventory: snapshot, date: "2026-08-02", preferences: PREFERENCES,
  }, {
    generateDecision: async (value) => { request = value; return decision(); },
  });
  assert.match(request.prompt, /untrusted data/i);
  assert.equal(request.schema.type, "object");
  assert.equal(request.schema.additionalProperties, false);
  assert.equal(request.schema.properties.ranked_events.items.additionalProperties, false);
  assert.equal(isVerifiedEventPreferenceRanking(ranking), true);
});
