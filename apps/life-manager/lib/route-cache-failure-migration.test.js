"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

test("COST-02 migration adds a scoped negative-cache state without weakening RLS", () => {
  const sql = fs.readFileSync(path.join(__dirname,
    "../migrations/2026-09-07-lm-route-cache-failures.sql"), "utf8");
  assert.match(sql, /ADD COLUMN IF NOT EXISTS cache_key text/i);
  assert.match(sql, /CREATE UNIQUE INDEX IF NOT EXISTS lm_route_cache_cache_key_idx/i);
  assert.match(sql, /DROP CONSTRAINT IF EXISTS lm_route_cache_uid_from_geo_to_geo_time_bucket_key/i);
  assert.doesNotMatch(sql, /WHERE cache_key IS NOT NULL/i);
  assert.match(sql, /cache_state IN \('success', 'negative'\)/i);
  assert.doesNotMatch(sql, /DISABLE ROW LEVEL SECURITY/i);
  assert.doesNotMatch(sql, /GRANT\s+.*(?:anon|authenticated)/i);
});

test("event cache index ships in a later additive migration", () => {
  const historical = fs.readFileSync(path.join(__dirname,
    "../migrations/2026-09-07-lm-route-cache-failures.sql"), "utf8");
  const sql = fs.readFileSync(path.join(__dirname,
    "../migrations/2026-09-07-lm-route-cache-version-index.sql"), "utf8");
  assert.doesNotMatch(historical, /event_version|purpose/i);
  assert.match(sql, /ADD COLUMN IF NOT EXISTS event_version text/i);
  assert.match(sql, /ADD COLUMN IF NOT EXISTS purpose text/i);
  assert.match(sql, /lm_route_cache_event_version_idx/i);
  assert.doesNotMatch(sql, /DISABLE ROW LEVEL SECURITY|GRANT\s+.*(?:anon|authenticated)/i);
});
