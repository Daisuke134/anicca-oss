"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  buildRuntimeJob,
  enqueueJob,
  enqueueJobAt,
  readCommonJob,
  readCommonReceipt,
  claimJobs,
  heartbeatJob,
  completeJob,
  failJob,
  resolveReconciliation,
  recordUnknownReconciliation,
  MAX_UNKNOWN_RECONCILE_RESULTS,
} = require("./runtime-job-store.js");

const MIGRATION = fs.readFileSync(path.join(
  __dirname,
  "../migrations/20260729_runtime_jobs.sql",
), "utf8");
const AGING_MIGRATION = fs.readFileSync(path.join(
  __dirname,
  "../migrations/20260730_runtime_reconcile_unknown_aging.sql",
), "utf8");

function sampleJob(overrides = {}) {
  return {
    jobId: "job-001",
    tenantId: "tenant-a",
    loopId: "marketing.an icca.slideshow",
    capability: "content.publish",
    effectClass: "publish",
    effectKey: "tiktok:anicca:asset-001",
    inputRefs: {
      product_ref: "product:anicca",
      asset_ref: "object:sha256:abc",
    },
    maxAttempts: 3,
    ...overrides,
  };
}

test("runtime job is tenant-bound, reference-only, bounded, and immutable", () => {
  const job = buildRuntimeJob(sampleJob());
  assert.equal(job.job_id, "job-001");
  assert.equal(job.tenant_id, "tenant-a");
  assert.equal(job.effect_class, "publish");
  assert.equal(job.max_attempts, 3);
  assert.deepEqual(job.input_refs, {
    product_ref: "product:anicca",
    asset_ref: "object:sha256:abc",
  });
  assert.equal(Object.isFrozen(job), true);

  assert.throws(() => buildRuntimeJob(sampleJob({
    inputRefs: { password: "do-not-store-secrets" },
  })), /reference-only/i);
  assert.throws(() => buildRuntimeJob(sampleJob({
    effectClass: "money",
    effectKey: "",
  })), /effect key/i);
  assert.throws(() => buildRuntimeJob(sampleJob({ maxAttempts: 0 })), /max attempts/i);
});

test("enqueue is idempotent by job id and rejects a cross-tenant collision", async () => {
  const calls = [];
  const existing = {
    job_id: "job-001",
    tenant_id: "tenant-a",
    loop_id: "marketing.anicca.slideshow",
    capability: "content.publish",
    effect_class: "publish",
    effect_key: "tiktok:anicca:asset-001",
    input_refs: sampleJob().inputRefs,
    max_attempts: 3,
    status: "queued",
  };
  const query = async (sql, params) => {
    calls.push({ sql, params });
    if (/INSERT INTO public\.lm_runtime_jobs/i.test(sql)) return { rows: [] };
    return { rows: [existing] };
  };

  const result = await enqueueJob(sampleJob({
    loopId: "marketing.anicca.slideshow",
  }), { query });
  assert.deepEqual(result, { created: false, job: existing });
  assert.match(calls[0].sql, /ON CONFLICT \(job_id\) DO NOTHING/i);
  assert.match(calls[1].sql, /WHERE job_id = \$1 AND tenant_id = \$2/i);
  assert.equal(calls[1].params[1], "tenant-a");

  await assert.rejects(
    enqueueJob(sampleJob(), {
      query: async (sql) => (/INSERT/i.test(sql) ? { rows: [] } : { rows: [] }),
    }),
    /collision/i,
  );
});

test("enqueue accepts the canonical job returned by buildRuntimeJob", async () => {
  const canonical = buildRuntimeJob(sampleJob({
    loopId: "marketing.anicca.slideshow",
  }));
  const result = await enqueueJob(canonical, {
    query: async (sql) => ({
      rows: /INSERT INTO public\.lm_runtime_jobs/i.test(sql)
        ? [{ ...canonical, status: "queued" }]
        : [],
    }),
  });

  assert.equal(result.created, true);
  assert.equal(result.job.job_id, canonical.job_id);
  assert.equal(result.job.effect_class, "publish");
});

test("scheduled enqueue writes available_at atomically and keeps idempotency exact", async () => {
  const calls = [];
  const canonical = buildRuntimeJob(sampleJob({ loopId: "connector.events" }));
  const availableAt = "2026-08-02T01:05:00.000Z";
  const query = async (sql, params) => {
    calls.push({ sql, params });
    return { rows: [{ ...canonical, available_at: availableAt, status: "queued" }] };
  };
  const created = await enqueueJobAt(canonical, availableAt, { query });
  assert.equal(created.created, true);
  assert.match(calls[0].sql, /available_at/i);
  assert.equal(calls[0].params.at(-1), availableAt);

  const existingQuery = async (sql) => ({
    rows: /INSERT INTO public\.lm_runtime_jobs/i.test(sql)
      ? []
      : [{ ...canonical, available_at: availableAt, status: "queued" }],
  });
  assert.equal((await enqueueJobAt(canonical, availableAt, { query: existingQuery })).created, false);
  await assert.rejects(enqueueJobAt(canonical, "not-a-time", { query }), /available time invalid/i);
  await assert.rejects(enqueueJobAt(canonical, "2026-08-02T01:06:00.000Z", {
    query: async (sql) => ({ rows: /INSERT/i.test(sql) ? [] : [{ ...canonical, available_at: availableAt }] }),
  }), /available time collision/i);
});

test("Postgres rows project through the common Job contract at a tenant-scoped read boundary", async () => {
  const row = {
    job_id: "job-001", tenant_id: "tenant-a", loop_id: "marketing.anicca.slideshow",
    capability: "content.publish", effect_class: "publish",
    effect_key: "tiktok:anicca:asset-001", input_refs: sampleJob().inputRefs,
    max_attempts: 3,
  };
  const calls = [];
  const projected = await readCommonJob({ tenantId: "tenant-a", jobId: "job-001" }, {
    query: async (sql, params) => { calls.push({ sql, params }); return { rows: [row] }; },
  });
  assert.deepEqual(projected, { schema_version: 1, record_type: "job", ...row });
  assert.match(calls[0].sql, /WHERE job_id = \$1 AND tenant_id = \$2/i);
  assert.deepEqual(calls[0].params, ["job-001", "tenant-a"]);

  assert.equal(await readCommonJob({ tenantId: "tenant-a", jobId: "missing" }, {
    query: async () => ({ rows: [] }),
  }), null);
  await assert.rejects(readCommonJob({ tenantId: "tenant-a", jobId: "legacy" }, {
    query: async () => ({ rows: [{ ...row, loop_id: "legacy loop" }] }),
  }), /common Job loop_id invalid/);
});

test("immutable Postgres receipts project only with provider verification evidence", async () => {
  const row = {
    job_id: "job-001", tenant_id: "tenant-a", attempt: 1, outcome: "completed",
    effect_key: "tiktok:anicca:asset-001", loop_id: "marketing.anicca.slideshow",
    effect_class: "publish", created_at: "2026-09-07T00:00:00Z",
    receipt: {
      provider: "postiz", provider_post_id: "post-1", run_id: "run-1",
      evidence_refs: ["postiz://post/post-1"], status: "published",
    },
  };
  const calls = [];
  const projected = await readCommonReceipt({ tenantId: "tenant-a", jobId: "job-001", attempt: 1 }, {
    query: async (sql, params) => { calls.push({ sql, params }); return { rows: [row] }; },
  });
  assert.equal(projected.record_type, "receipt");
  assert.equal(projected.outcome, "verified");
  assert.equal(projected.provider, "postiz");
  assert.equal(projected.external_ref, "post-1");
  assert.deepEqual(projected.evidence_refs, ["postiz://post/post-1"]);
  assert.match(projected.payload_sha256, /^[a-f0-9]{64}$/);
  assert.match(calls[0].sql, /JOIN public\.lm_runtime_jobs/i);
  assert.deepEqual(calls[0].params, ["job-001", "tenant-a", 1]);

  await assert.rejects(readCommonReceipt({ tenantId: "tenant-a", jobId: "job-001", attempt: 1 }, {
    query: async () => ({ rows: [{ ...row, receipt: { status: "published" } }] }),
  }), /lacks provider verification evidence/);
  await assert.rejects(readCommonReceipt({ tenantId: "tenant-a", jobId: "job-001", attempt: 1 }, {
    query: async () => ({ rows: [{ ...row, receipt: { ...row.receipt, provider_post_id: {} } }] }),
  }), /external reference is invalid/);
  await assert.rejects(readCommonReceipt({ tenantId: "tenant-a", jobId: "job-001", attempt: 1 }, {
    query: async () => ({ rows: [{ ...row, effect_class: "none", effect_key: null }] }),
  }), /requires an external effect/);
  await assert.rejects(readCommonReceipt({ tenantId: "tenant-a", jobId: "job-001", attempt: 1 }, {
    query: async () => ({ rows: [{ ...row, outcome: "reconciled_absent" }] }),
  }), /not representable/);
});

test("claim filters capabilities, has a bounded lease, and uses one narrow atomic RPC", async () => {
  const calls = [];
  const rows = [{ job_id: "job-001", tenant_id: "tenant-a", attempt: 1 }];
  const query = async (sql, params) => {
    calls.push({ sql, params });
    return { rows };
  };
  const claimed = await claimJobs({
    workerId: "worker-publish-1",
    capabilities: ["content.publish", "content.render"],
    tenantId: "tenant-a",
    limit: 2,
    leaseSeconds: 180,
  }, { query });

  assert.deepEqual(claimed, rows);
  assert.match(calls[0].sql, /claim_lm_runtime_jobs\(\$1, \$2::text\[\], \$3, \$4, \$5\)/i);
  assert.deepEqual(calls[0].params, [
    "worker-publish-1",
    ["content.publish", "content.render"],
    "tenant-a",
    2,
    180,
  ]);
});

test("heartbeat, completion, failure, and reconciliation are tenant and attempt scoped", async () => {
  const calls = [];
  const query = async (sql, params) => {
    calls.push({ sql, params });
    return { rows: [{ job_id: "job-001", tenant_id: "tenant-a", attempt: 1 }] };
  };
  const identity = {
    tenantId: "tenant-a",
    jobId: "job-001",
    attempt: 1,
    workerId: "worker-publish-1",
  };

  await heartbeatJob({ ...identity, leaseSeconds: 180 }, { query });
  await completeJob({
    ...identity,
    receipt: { provider_id: "post-123", url: "https://example.com/post/123" },
  }, { query });
  await failJob({ ...identity, errorCode: "TIMEOUT", unknownEffect: true }, { query });
  await resolveReconciliation({
    tenantId: "tenant-a",
    jobId: "job-001",
    attempt: 1,
    decision: "absent",
    receipt: { lookup: "not_found" },
  }, { query });

  for (const call of calls) {
    assert.equal(call.params[0], "tenant-a");
    assert.equal(call.params[1], "job-001");
    assert.equal(call.params[2], 1);
  }
  assert.match(calls[0].sql, /heartbeat_lm_runtime_job/i);
  assert.match(calls[1].sql, /complete_lm_runtime_job/i);
  assert.match(calls[2].sql, /fail_lm_runtime_job/i);
  assert.equal(calls[2].params[5], true);
  assert.match(calls[3].sql, /resolve_lm_runtime_effect/i);
  assert.equal(calls[3].params[3], "absent");
});

test("collision error names the mismatched fields so operators can diagnose slot-lineage drift", async () => {
  // S-1: the existing publish effect was enqueued with different lineage (for example the
  // same creative re-planned at a new slot). The error must say WHAT differs.
  const existing = {
    job_id: "job-001",
    tenant_id: "tenant-a",
    loop_id: "marketing.anicca.slideshow",
    capability: "content.publish",
    effect_class: "publish",
    effect_key: "tiktok:anicca:asset-001",
    input_refs: {
      product_ref: "product:anicca",
      asset_ref: "object:sha256:abc",
      slot_ref: "slot:2026-07-30T12:30:00.000Z",
    },
    max_attempts: 3,
    status: "queued",
  };
  const query = async (sql) => (
    /INSERT INTO public\.lm_runtime_jobs/i.test(sql) ? { rows: [] } : { rows: [existing] }
  );

  await assert.rejects(
    enqueueJob(sampleJob({ loopId: "marketing.anicca.slideshow" }), { query }),
    (error) => {
      assert.match(error.message, /runtime job id collision/);
      assert.match(error.message, /input_refs/);
      assert.match(error.message, /lineage|slot/i);
      return true;
    },
  );
});

test("unknown reconcile aging uses one narrow RPC with the bounded constant", async () => {
  assert.equal(MAX_UNKNOWN_RECONCILE_RESULTS, 5);
  const calls = [];
  const query = async (sql, params) => {
    calls.push({ sql, params });
    return {
      rows: [{
        job_id: "job-001", tenant_id: "tenant-a", attempt: 1,
        status: "reconciling", reconcile_attempts: 1,
      }],
    };
  };

  const row = await recordUnknownReconciliation({
    tenantId: "tenant-a",
    jobId: "job-001",
    attempt: 1,
    maxUnknownResults: MAX_UNKNOWN_RECONCILE_RESULTS,
  }, { query });

  assert.equal(row.status, "reconciling");
  assert.match(calls[0].sql, /age_lm_runtime_reconciliation\(\$1, \$2, \$3, \$4\)/i);
  assert.deepEqual(calls[0].params, ["tenant-a", "job-001", 1, 5]);

  await assert.rejects(recordUnknownReconciliation({
    tenantId: "tenant-a",
    jobId: "job-001",
    attempt: 1,
    maxUnknownResults: 0,
  }, { query }), /unknown limit/i);
  await assert.rejects(recordUnknownReconciliation({
    tenantId: "tenant-a",
    jobId: "job-001",
    attempt: 1,
  }, { query: async () => ({ rows: [] }) }), /lost job/i);
});

test("aging migration adds a durable unknown counter that dead-letters exhausted reconciliation", () => {
  assert.match(AGING_MIGRATION, /ADD COLUMN IF NOT EXISTS reconcile_attempts/i);
  assert.match(AGING_MIGRATION, /age_lm_runtime_reconciliation/i);
  assert.match(AGING_MIGRATION, /RECONCILE_UNKNOWN_EXHAUSTED/);
  assert.match(AGING_MIGRATION, /dead_letter/);
  // Resolution (present or absent) resets the counter so a later reconciliation
  // lifecycle starts its own aging window.
  assert.match(AGING_MIGRATION, /reconcile_attempts = 0/);
  assert.match(AGING_MIGRATION, /status = 'reconciling'/);
});

test("migration enforces atomic claim, bounded retry, unique effects, and immutable receipts", () => {
  assert.match(MIGRATION, /CREATE TABLE IF NOT EXISTS public\.lm_runtime_jobs/i);
  assert.match(MIGRATION, /CREATE TABLE IF NOT EXISTS public\.lm_runtime_job_receipts/i);
  assert.match(MIGRATION, /PRIMARY KEY \(job_id, attempt\)/i);
  assert.match(MIGRATION, /UNIQUE \(tenant_id, effect_key\)/i);
  assert.match(MIGRATION, /FOR UPDATE SKIP LOCKED/i);
  assert.match(MIGRATION, /UPDATE public\.lm_runtime_jobs[\s\S]*RETURNING/i);
  assert.match(MIGRATION, /attempt < max_attempts/i);
  assert.match(MIGRATION, /dead_letter/i);
  assert.match(MIGRATION, /status = 'reconciling'/i);
  assert.match(MIGRATION, /OLD TABLE|immutable/i);
  assert.doesNotMatch(MIGRATION, /advisory_(?:lock|xact_lock)/i);
});
