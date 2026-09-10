"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { buildOpportunity, createOpportunity } = require("./money-printer-opportunity.js");

const INPUT = Object.freeze({
  tenantId: "tenant-a",
  sourceUrl: "https://Example.com:443/opportunity#tracking",
  title: "Public opportunity",
  goalStatement: "Complete the public opportunity and leave a verified receipt.",
  valueMinor: "50000",
  currency: "JPY",
  observedAt: "2026-08-29T00:00:00.000Z",
});

test("canonical opportunity identity is stable from tenant and canonical HTTPS URL", () => {
  const first = buildOpportunity(INPUT);
  const replay = buildOpportunity({ ...INPUT, sourceUrl: "https://example.com/opportunity" });
  const opportunityId = crypto.createHash("sha256")
    .update("tenant-a\nhttps://example.com/opportunity", "utf8")
    .digest("hex");

  assert.equal(first.uid, "tenant-a");
  assert.equal(first.source_url, "https://example.com/opportunity");
  assert.equal(first.opportunity_id, opportunityId);
  assert.equal(first.job_id, `goal:${opportunityId}`);
  assert.equal(first.goal_ref, `intent-entry://tenant-a/${opportunityId}`);
  assert.equal(first.status, "DISCOVERED");
  assert.equal(first.value_minor, "50000");
  assert.deepEqual(replay, first);
  assert.equal(Object.hasOwn(first, "provider"), false);
});

test("title with embedded newlines and double spaces canonicalizes to single-spaced text", () => {
  const built = buildOpportunity({
    ...INPUT,
    title: "Biohub \n Cell  Tracking During Development",
  });
  assert.equal(built.title, "Biohub Cell Tracking During Development");
});

test("atomic create readback is called once and rejects a conflicting row", async () => {
  const canonical = buildOpportunity(INPUT);
  const calls = [];
  const persisted = new Map();
  const store = {
    async create(opportunity) {
      calls.push(opportunity);
      const current = persisted.get(opportunity.opportunity_id);
      if (current) return current;
      const row = { ...opportunity, created_at: "2026-08-29T00:00:01.000Z" };
      persisted.set(opportunity.opportunity_id, row);
      return row;
    },
  };

  const stored = await createOpportunity(INPUT, store);
  persisted.set(canonical.opportunity_id, { ...stored, status: "WORKING" });
  const replay = await createOpportunity({
    ...INPUT,
    sourceUrl: "https://example.com/opportunity",
    observedAt: "2026-08-30T00:00:00.000Z",
  }, store);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].opportunity_id, canonical.opportunity_id);
  assert.equal(stored.opportunity_id, canonical.opportunity_id);
  assert.equal(replay.opportunity_id, stored.opportunity_id);
  assert.equal(replay.status, "WORKING");
  assert.equal(replay.observed_at, stored.observed_at);

  await assert.rejects(
    createOpportunity(INPUT, {
      async create(opportunity) { return { ...opportunity, goal_statement: "different" }; },
    }),
    /readback|conflict|mismatch/i,
  );
});

test("migration is additive and atomically verifies the matching runtime job", () => {
  const sql = fs.readFileSync(
    path.join(__dirname, "../migrations/2026-08-29-lm-money-printer-opportunities.sql"),
    "utf8",
  );
  assert.match(sql, /CREATE TABLE IF NOT EXISTS public\.lm_money_opportunities/i);
  assert.match(sql, /PRIMARY KEY \(uid, opportunity_id\)/i);
  assert.match(sql, /UNIQUE \(uid, source_url\)/i);
  assert.match(sql, /CREATE OR REPLACE FUNCTION public\.create_lm_money_opportunity/i);
  assert.match(sql, /SELECT[\s\S]+FROM public\.lm_runtime_jobs[\s\S]+FOR UPDATE/i);
  assert.match(sql, /INSERT INTO public\.lm_runtime_jobs/i);
  assert.match(sql, /general-agent\.work/i);
  assert.match(sql, /input_refs[\s\S]+goal_ref/i);
  assert.doesNotMatch(sql, /v_opportunity\.observed_at\s+IS DISTINCT FROM\s+p_observed_at/i);
  assert.doesNotMatch(sql, /provider/i);
});
