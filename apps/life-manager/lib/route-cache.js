// lib/route-cache.js — C3 (VCSDD life-manager-cost-connect-reliability): route-result cache so the 60s
// scheduler tick does NOT recompute a route it already has. This is a NEW store, distinct from
// lm_travel_log (which stays a dedup/claim ledger). In production the `store` is Supabase `lm_route_cache`
// (uid, from_geo, to_geo, time_bucket, provider, duration_secs, geometry, computed_at, ttl); here it is
// injected so the logic is pure + unit-testable.
"use strict";

const BUCKET_MS = 10 * 60_000; // coarse 10-min bucket: a moved event lands in a new bucket → recompute.
const NEGATIVE_TTL_MS = 30 * 60_000;
const TRANSIENT_TTL_MS = 2 * 60_000;
const CACHE_FAILURE = Symbol("route-cache-failure");

function cacheFailure(failureClass = "no_route") {
  return { [CACHE_FAILURE]: true, failureClass: String(failureClass || "no_route") };
}

function isCacheFailure(value) {
  return Boolean(value && value[CACHE_FAILURE] === true);
}

// Round a departure epoch (ms) down to a coarse bucket index.
function timeBucket(epochMs, bucketMs = BUCKET_MS) {
  return Math.floor(epochMs / bucketMs);
}

// Round a coordinate so trivially-different geos share a cache row (~11m at 4 dp is plenty for a route).
const q = (n) => {
  const value = Number(n);
  return Number.isFinite(value) ? Math.round(value * 1e4) / 1e4 : null;
};

function coordinateLongitude(geo) {
  return geo && (geo.lon == null ? geo.lng : geo.lon);
}

function contextValue(context, keys) {
  for (const key of keys) {
    if (context && context[key] != null && context[key] !== "") return String(context[key]);
  }
  return "";
}

function normalizeContext(context = {}) {
  const source = context && typeof context === "object" ? context : {};
  return {
    provider: contextValue(source, ["provider"]),
    mode: contextValue(source, ["mode"]),
    anchorType: contextValue(source, ["anchorType"]),
    timezone: contextValue(source, ["timezone"]),
    serviceDate: contextValue(source, ["serviceDate"]),
    fromKey: contextValue(source, ["fromKey"]),
    toKey: contextValue(source, ["toKey"]),
    eventVersion: contextValue(source, ["eventVersion"]),
    purpose: contextValue(source, ["purpose"]),
  };
}

function resolveBucketAndContext(bucket, context) {
  // Keep the explicit scope object available for cache-key inspection while retaining the original
  // positional `(uid, from, to, bucket, provider)` API.
  if (bucket && typeof bucket === "object" && !Array.isArray(bucket)) {
    const merged = { ...bucket, ...(context && typeof context === "object" ? context : {}) };
    const normalized = normalizeContext(merged);
    const value = merged.timeBucket == null ? merged.bucket : merged.timeBucket;
    return { bucket: value == null ? "" : String(value), context: normalized };
  }
  return { bucket: bucket == null ? "" : String(bucket), context: normalizeContext(context) };
}

function cacheKey(uid, fromGeo, toGeo, bucket, context = {}) {
  const resolved = resolveBucketAndContext(bucket, context);
  // JSON avoids delimiter collisions and leaves every scope component explicit. In particular,
  // provider/mode and anchor direction must never share a route result accidentally.
  return JSON.stringify([
    uid == null ? "" : String(uid),
    q(fromGeo && fromGeo.lat), q(coordinateLongitude(fromGeo)),
    q(toGeo && toGeo.lat), q(coordinateLongitude(toGeo)),
    resolved.context.provider,
    resolved.context.mode,
    resolved.context.timezone,
    resolved.context.serviceDate,
    resolved.context.anchorType,
    resolved.bucket,
    resolved.context.fromKey,
    resolved.context.toKey,
    resolved.context.eventVersion,
    resolved.context.purpose,
  ]);
}

function makeSupabaseRouteStore({ supaUrl, supaKey, fetchImpl = global.fetch } = {}) {
  const base = String(supaUrl || "").replace(/\/$/, "");
  const memory = new Map();
  const headers = {
    apikey: String(supaKey || ""),
    Authorization: `Bearer ${String(supaKey || "")}`,
    "Content-Type": "application/json",
  };
  return {
    async getByEvent(uid, eventVersion, purpose) {
      if (!base || !supaKey || typeof fetchImpl !== "function" || !uid || !eventVersion) return null;
      try {
        const url = `${base}/rest/v1/lm_route_cache?uid=eq.${encodeURIComponent(String(uid))}`
          + `&event_version=eq.${encodeURIComponent(String(eventVersion))}`
          + `&purpose=eq.${encodeURIComponent(String(purpose || "go"))}`
          + "&select=geometry,computed_at,ttl_secs,cache_state,failure_class&order=computed_at.desc&limit=1";
        const response = await fetchImpl(url, { headers });
        if (!response || response.ok !== true) return null;
        const rows = await response.json();
        const row = Array.isArray(rows) ? rows[0] : null;
        const computedAt = Date.parse(row && row.computed_at);
        if (!row || !Number.isFinite(computedAt)) return null;
        return { value: row.geometry, computedAt, ttlMs: Number(row.ttl_secs) * 1000,
          negative: row.cache_state === "negative", failureClass: row.failure_class || null };
      } catch { return null; }
    },
    async get(key) {
      if (memory.has(key)) return memory.get(key);
      if (!base || !supaKey || typeof fetchImpl !== "function") return null;
      try {
        const url = `${base}/rest/v1/lm_route_cache?cache_key=eq.${encodeURIComponent(key)}`
          + "&select=geometry,computed_at,ttl_secs,cache_state,failure_class&limit=1";
        const response = await fetchImpl(url, { headers });
        if (!response || response.ok !== true) return null;
        const rows = await response.json();
        const row = Array.isArray(rows) ? rows[0] : null;
        const computedAt = Date.parse(row && row.computed_at);
        if (!row || !Number.isFinite(computedAt)) return null;
        const entry = {
          value: row.geometry,
          computedAt,
          ttlMs: Number(row.ttl_secs) * 1000,
          negative: row.cache_state === "negative",
          failureClass: row.failure_class || null,
        };
        memory.set(key, entry);
        return entry;
      } catch { return null; }
    },
    async set(key, entry) {
      memory.set(key, entry);
      if (!base || !supaKey || typeof fetchImpl !== "function") return false;
      let parts;
      try { parts = JSON.parse(key); } catch { return false; }
      if (!Array.isArray(parts) || parts.length < 10) return false;
      const [uid, fromLat, fromLon, toLat, toLon, provider, , , , , bucket, , , eventVersion, purpose] = parts;
      const seconds = Number(entry && entry.value && (entry.value.durationSeconds
        ?? entry.value.durationSecs ?? entry.value.duration_seconds));
      const body = {
        uid: String(uid || "anonymous"),
        from_geo: `${fromLat},${fromLon}`,
        to_geo: `${toLat},${toLon}`,
        time_bucket: Number(bucket) || 0,
        provider: String(provider || "unknown"),
        duration_secs: Number.isFinite(seconds) ? Math.round(seconds) : 0,
        geometry: entry.negative ? null : entry.value,
        computed_at: new Date(entry.computedAt).toISOString(),
        ttl_secs: Math.max(1, Math.round(entry.ttlMs / 1000)),
        cache_key: key,
        cache_state: entry.negative ? "negative" : "success",
        failure_class: entry.failureClass || null,
        event_version: String(eventVersion || ""),
        purpose: String(purpose || "go"),
      };
      try {
        const response = await fetchImpl(`${base}/rest/v1/lm_route_cache?on_conflict=cache_key`, {
          method: "POST",
          headers: { ...headers, Prefer: "resolution=merge-duplicates,return=minimal" },
          body: JSON.stringify(body),
        });
        return Boolean(response && response.ok === true);
      } catch { return false; }
    },
  };
}

// makeRouteCache({ store: Map-like {get,set}, ttlMs, now }) → { getOrCompute }.
// INVARIANT: accepted non-null routes are computed at most once per scoped route key within ttlMs;
// concurrent same-key work still collapses, while null/undefined results retry on later calls/ticks.
function makeRouteCache({ store = new Map(), ttlMs = BUCKET_MS, negativeTtlMs = NEGATIVE_TTL_MS,
  transientTtlMs = TRANSIENT_TTL_MS, now = Date.now } = {}) {
  const inFlight = new Map();
  const reportedHits = new Set();
  async function getByEvent(uid, eventVersion, purpose = "go", onCacheHit = null) {
    if (!store || typeof store.getByEvent !== "function" || !uid || !eventVersion) return { hit: false, value: null };
    let entry = null;
    try { entry = await store.getByEvent(uid, eventVersion, purpose); } catch { entry = null; }
    const entryTtlMs = entry && Number.isFinite(entry.ttlMs) ? entry.ttlMs : ttlMs;
    if (!entry || !Number.isFinite(entry.computedAt) || now() - entry.computedAt >= entryTtlMs) {
      return { hit: false, value: null };
    }
    if (typeof onCacheHit === "function") await onCacheHit(entry.negative ? null : entry.value, {
      negative: entry.negative === true, failureClass: entry.failureClass || null,
    });
    return { hit: true, value: entry.negative ? null : entry.value, failureClass: entry.failureClass || null };
  }
  async function getOrCompute(uid, fromGeo, toGeo, bucket, provider, context = {}, onCacheHit = null) {
    const key = cacheKey(uid, fromGeo, toGeo, bucket, context);
    if (inFlight.has(key)) return inFlight.get(key);
    const run = (async () => {
      const t = now();
      let hit = null;
      try { hit = store && typeof store.get === "function" ? await store.get(key) : null; }
      catch { hit = null; }
      const hitTtlMs = hit && Number.isFinite(hit.ttlMs) ? hit.ttlMs : ttlMs;
      if (hit && Number.isFinite(hit.computedAt) && t - hit.computedAt < hitTtlMs) {
        if (typeof onCacheHit === "function" && !reportedHits.has(key)) {
          reportedHits.add(key);
          await onCacheHit(hit.negative ? null : hit.value, {
            negative: hit.negative === true,
            failureClass: hit.failureClass || null,
          });
        }
        return hit.negative ? null : hit.value;
      }
      const value = await provider();
      const computedAt = now();
      if (value != null && store && typeof store.set === "function") {
        const negative = isCacheFailure(value);
        const transient = negative && ["network", "provider_5xx", "timeout"].includes(value.failureClass);
        try { await store.set(key, {
          value: negative ? null : value,
          computedAt,
          negative,
          failureClass: negative ? value.failureClass : null,
          ttlMs: negative ? (transient ? transientTtlMs : negativeTtlMs) : ttlMs,
        }); } catch { /* cache persistence must not break required routing */ }
        reportedHits.delete(key);
      }
      return isCacheFailure(value) ? null : value;
    })();
    inFlight.set(key, run);
    try {
      return await run;
    } finally {
      inFlight.delete(key);
    }
  }
  return { getByEvent, getOrCompute };
}

module.exports = {
  timeBucket, cacheKey, makeRouteCache, makeSupabaseRouteStore, cacheFailure, isCacheFailure,
  BUCKET_MS, NEGATIVE_TTL_MS, TRANSIENT_TTL_MS,
};
