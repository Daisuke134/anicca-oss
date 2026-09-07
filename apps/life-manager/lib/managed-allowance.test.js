"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FREE_LIMIT, PAID_LIMIT, validResult, reserveManagedAction } = require("./managed-allowance.js");

test("monthly allowance has one free and one paid limit", () => {
  assert.equal(FREE_LIMIT, 30);
  assert.equal(PAID_LIMIT, 500);
});

test("allowance RPC client rejects ambiguous responses and never leaks credentials", async () => {
  assert.equal(validResult({ allowed: true }), false);
  let request;
  const result = await reserveManagedAction("tenant-a", "event-a", "https://db.example/", "secret", {
    fetchImpl: async (url, init) => {
      request = { url, init };
      return { ok: true, json: async () => ({ allowed: true, used: 4, limit: 30, periodStart: "2026-09-01", resetAt: "2026-10-01" }) };
    },
  });
  assert.equal(result.allowed, true);
  assert.equal(request.url, "https://db.example/rest/v1/rpc/reserve_lm_managed_action");
  assert.deepEqual(JSON.parse(request.init.body), { p_uid: "tenant-a", p_action_key: "event-a" });
});

test("migration is tenant-scoped, race-safe, success-only, and service-role-only", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-09-07-lm-monthly-managed-allowance.sql"), "utf8");
  assert.match(sql, /PRIMARY KEY \(uid, period_start, action_key\)/);
  assert.match(sql, /pg_advisory_xact_lock/);
  assert.match(sql, /status = 'succeeded'/);
  assert.match(sql, /status = 'pending'/);
  assert.match(sql, /INTERVAL '15 minutes'/);
  assert.match(sql, /THEN 500 ELSE 30/);
  assert.match(sql, /REVOKE ALL ON FUNCTION public\.reserve_lm_managed_action.*PUBLIC, anon, authenticated/);
  assert.match(sql, /GRANT EXECUTE ON FUNCTION public\.reserve_lm_managed_action.*service_role/);
});
