// travel-routes.test.js — #71 Routes API migration: pure helpers for the traffic-aware DRIVE leg.
// Run: node --test apps/life-call/lib/travel-routes.test.js
"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const { parseDurationSeconds, minutesFromSeconds, buildDriveBody, clampDepartIso, directionsMinutes, acceptRouteResults } = require("./travel.js");
const { makeRouteCache } = require("./route-cache.js");
const freshCache = () => makeRouteCache({ store: new Map(), ttlMs: 600000 });

// ── fetch-injection helpers for the never-late ordering tests ────────────────────────────────────
// Route by URL: legacy Directions (transit) vs Routes API (drive). Each test supplies the two bodies.
function stubFetch({ transit, drive, capture } = {}) {
  const orig = global.fetch;
  global.fetch = async (url, opts) => {
    const u = String(url);
    if (capture) capture(u, opts);
    if (u.includes("maps.googleapis.com/maps/api/directions")) {
      return { ok: true, json: async () => transit };
    }
    if (u.includes("routes.googleapis.com")) {
      return { ok: true, json: async () => drive };
    }
    throw new Error("unexpected url " + u);
  };
  return () => { global.fetch = orig; };
}
const transitOK = (mins) => ({ status: "OK", routes: [{ legs: [{ duration: { value: mins * 60 } }] }] });
const driveOK = (mins) => ({ routes: [{ duration: `${mins * 60}s` }] });

test('parseDurationSeconds("812s") → 812', () => {
  assert.equal(parseDurationSeconds("812s"), 812);
});
test('parseDurationSeconds("1002s") → 1002', () => {
  assert.equal(parseDurationSeconds("1002s"), 1002);
});
test("parseDurationSeconds garbage / missing → null", () => {
  assert.equal(parseDurationSeconds(""), null);
  assert.equal(parseDurationSeconds(undefined), null);
  assert.equal(parseDurationSeconds("abc"), null);
  assert.equal(parseDurationSeconds(null), null);
});

test("minutesFromSeconds floors at 5 min", () => {
  assert.equal(minutesFromSeconds(60), 5);    // 1 min raw → floored to 5
  assert.equal(minutesFromSeconds(0), 5);
});
test("minutesFromSeconds rounds 812s → 14", () => {
  assert.equal(minutesFromSeconds(812), 14);
});
test("minutesFromSeconds rounds 1002s → 17", () => {
  assert.equal(minutesFromSeconds(1002), 17);
});
test("minutesFromSeconds NO ×1.4 fudge (1200s → 20, not 28)", () => {
  assert.equal(minutesFromSeconds(1200), 20);
});

test("buildDriveBody sets DRIVE + TRAFFIC_AWARE_OPTIMAL + departureTime", () => {
  const b = buildDriveBody("新宿区南元町15-27", "東京駅", "2026-06-21T05:00:00Z");
  assert.equal(b.travelMode, "DRIVE");
  assert.equal(b.routingPreference, "TRAFFIC_AWARE_OPTIMAL");
  assert.equal(b.departureTime, "2026-06-21T05:00:00Z");
  assert.equal(b.origin.address, "新宿区南元町15-27");
  assert.equal(b.destination.address, "東京駅");
});

test("clampDepartIso: future depart kept as-is (ISO, no ms)", () => {
  const now = Date.parse("2026-06-21T00:00:00Z");
  const depart = Date.parse("2026-06-21T05:00:00Z");
  assert.equal(clampDepartIso(depart, now), "2026-06-21T05:00:00Z");
});
test("clampDepartIso: past depart bumped to now+60s (Routes rejects past departureTime)", () => {
  const now = Date.parse("2026-06-21T00:00:00Z");
  const past = Date.parse("2026-06-20T00:00:00Z");
  assert.equal(clampDepartIso(past, now), "2026-06-21T00:01:00Z");
});

test("acceptRouteResults is operational with either provider and exposes degradation", () => {
  assert.deepEqual(acceptRouteResults({ legacyTransit: null, routesDrive: 15 }), {
    operational: true,
    minutes: 15,
    availableProviders: ["routes_drive"],
    degradedProviders: ["legacy_transit"],
  });
  assert.deepEqual(acceptRouteResults({ legacyTransit: 20, routesDrive: null }), {
    operational: true,
    minutes: 20,
    availableProviders: ["legacy_transit"],
    degradedProviders: ["routes_drive"],
  });
  assert.deepEqual(acceptRouteResults({ legacyTransit: 12, routesDrive: 45 }), {
    operational: true,
    minutes: 45,
    availableProviders: ["legacy_transit", "routes_drive"],
    degradedProviders: [],
  });
  assert.equal(acceptRouteResults({ legacyTransit: null, routesDrive: null }).operational, false);
});

// ── NEVER-LATE ordering (fix 1+2 from the adversarial gate) ──────────────────────────────────────
test("both modes resolve → directionsMinutes adapter uses the structured legacy-transit route", async () => {
  const restore = stubFetch({ transit: transitOK(12), drive: driveOK(45) });
  try {
    const now = Date.parse("2026-06-21T00:00:00Z");
    const future = Date.parse("2026-06-21T09:00:00Z");
    assert.equal(await directionsMinutes("A", "B", "k", future, now), 12); // one legacy Transit fallback request
  } finally { restore(); }
});

test("transit returns empty routes[] → structured Google fallback has no route", async () => {
  const restore = stubFetch({ transit: { status: "OK", routes: [] }, drive: driveOK(30) });
  try {
    const now = Date.parse("2026-06-21T00:00:00Z");
    assert.equal(await directionsMinutes("A", "B", "k", now + 3600000, now), null);
  } finally { restore(); }
});

test("transit anchors arrival_time to EVENT start (not departure_time=now) for a future event", async () => {
  let transitUrl = "";
  const restore = stubFetch({
    transit: transitOK(20), drive: driveOK(10),
    capture: (u) => { if (u.includes("/maps/api/directions")) transitUrl = u; },
  });
  try {
    const now = Date.parse("2026-06-21T00:00:00Z");
    const eventStart = Date.parse("2026-06-21T09:00:00Z");
    await directionsMinutes("A", "B", "k", eventStart, now, false, { _routeCache: freshCache() });
    assert.ok(transitUrl.includes(`arrival_time=${Math.floor(eventStart / 1000)}`), "must carry event-start arrival_time");
    assert.ok(!transitUrl.includes("departure_time=now"), "must NOT use departure_time=now for a future event");
  } finally { restore(); }
});

test("neither mode resolves → null (caller asks)", async () => {
  const restore = stubFetch({ transit: { status: "ZERO_RESULTS" }, drive: {} });
  try {
    const now = Date.parse("2026-06-21T00:00:00Z");
    assert.equal(await directionsMinutes("A", "B", "k", now + 3600000, now), null);
  } finally { restore(); }
});

test("missing key/src/dst → null without any fetch", async () => {
  assert.equal(await directionsMinutes("", "B", "k"), null);
  assert.equal(await directionsMinutes("A", "B", ""), null);
});
