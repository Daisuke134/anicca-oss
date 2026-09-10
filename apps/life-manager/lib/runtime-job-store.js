"use strict";

const crypto = require("node:crypto");
const { isDeepStrictEqual } = require("node:util");

const EFFECT_CLASSES = new Set(["none", "publish", "message", "money"]);
// After this many consecutive unknown reconcile results a reconciling job dead-letters
// (migrations/20260730_runtime_reconcile_unknown_aging.sql) instead of looping forever.
const MAX_UNKNOWN_RECONCILE_RESULTS = 5;
const REFERENCE_KEY = /_(?:ref|refs)$/;
let defaultPool;

function commonRecordContract() {
  return require("../../../runtime/contracts/common-record.cjs");
}

function nonEmpty(value, label, max = 200) {
  const text = String(value == null ? "" : value).trim();
  if (!text || text.length > max) throw new Error(`${label} invalid`);
  return text;
}

function positiveInteger(value, label, max) {
  if (!Number.isInteger(value) || value < 1 || value > max) {
    throw new Error(`${label} invalid`);
  }
  return value;
}

function referenceObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("runtime input refs must be reference-only");
  }
  for (const [key, ref] of Object.entries(value)) {
    if (!REFERENCE_KEY.test(key)) {
      throw new Error("runtime input refs must be reference-only");
    }
    const values = Array.isArray(ref) ? ref : [ref];
    if (values.length < 1 || values.some((item) => (
      typeof item !== "string" || !item.trim() || item.length > 1000
    ))) {
      throw new Error("runtime input refs must be reference-only");
    }
  }
  if (Buffer.byteLength(JSON.stringify(value)) > 16_384) {
    throw new Error("runtime input refs too large");
  }
  return JSON.parse(JSON.stringify(value));
}

function database(opts = {}) {
  if (typeof opts.query === "function") return { query: opts.query };
  const connectionString = String(
    opts.connectionString
      || process.env.LM_RUNTIME_DATABASE_URL
      || process.env.LM_FEEDBACK_DATABASE_URL
      || "",
  ).trim();
  if (!connectionString) throw new Error("runtime job store unavailable");
  if (!defaultPool) {
    const Pool = opts.Pool || require("pg").Pool;
    defaultPool = new Pool({ connectionString, max: 8 });
  }
  return { query: defaultPool.query.bind(defaultPool) };
}

function buildRuntimeJob(input) {
  let source = input;
  if (input && input.job_id != null) {
    if ([
      "jobId",
      "tenantId",
      "loopId",
      "effectClass",
      "effectKey",
      "inputRefs",
      "maxAttempts",
    ].some((key) => input[key] != null)) {
      throw new Error("runtime job shape ambiguous");
    }
    source = {
      jobId: input.job_id,
      tenantId: input.tenant_id,
      loopId: input.loop_id,
      capability: input.capability,
      effectClass: input.effect_class,
      effectKey: input.effect_key,
      inputRefs: input.input_refs,
      maxAttempts: input.max_attempts,
    };
  }
  const effectClass = nonEmpty(source && source.effectClass, "runtime effect class", 20);
  if (!EFFECT_CLASSES.has(effectClass)) throw new Error("runtime effect class invalid");
  const suppliedEffectKey = source && source.effectKey;
  const effectKey = suppliedEffectKey == null ? null : String(suppliedEffectKey).trim();
  if (
    (effectClass === "none" && effectKey)
    || (effectClass !== "none" && (!effectKey || effectKey.length > 500))
  ) {
    throw new Error("runtime effect key invalid");
  }

  return Object.freeze({
    job_id: nonEmpty(source && source.jobId, "runtime job id"),
    tenant_id: nonEmpty(source && source.tenantId, "runtime tenant id"),
    loop_id: nonEmpty(source && source.loopId, "runtime loop id"),
    capability: nonEmpty(source && source.capability, "runtime capability"),
    effect_class: effectClass,
    effect_key: effectKey,
    input_refs: referenceObject(source && source.inputRefs),
    max_attempts: positiveInteger(source && source.maxAttempts, "runtime max attempts", 20),
  });
}

function assertSameJob(existing, expected) {
  const fields = [
    "job_id",
    "tenant_id",
    "loop_id",
    "capability",
    "effect_class",
    "effect_key",
    "input_refs",
    "max_attempts",
  ];
  const mismatched = fields.filter((field) => (
    !isDeepStrictEqual(existing[field], expected[field])
  ));
  if (mismatched.length === 0) return;
  throw new Error(
    "runtime job id collision: a job with this job_id already exists with different "
    + mismatched.join(", ")
    + "; for publish effects this usually means the same creative/platform effect was "
    + "re-planned with different slot lineage, and the existing effect row wins",
  );
}

async function enqueueJob(input, opts = {}) {
  const job = buildRuntimeJob(input);
  const { query } = database(opts);
  const inserted = (await query(`
    INSERT INTO public.lm_runtime_jobs (
      job_id, tenant_id, loop_id, capability, effect_class, effect_key, input_refs, max_attempts
    ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
    ON CONFLICT (job_id) DO NOTHING
    RETURNING *
  `, [
    job.job_id,
    job.tenant_id,
    job.loop_id,
    job.capability,
    job.effect_class,
    job.effect_key,
    JSON.stringify(job.input_refs),
    job.max_attempts,
  ])).rows;
  if (inserted.length === 1) return { created: true, job: inserted[0] };
  if (inserted.length > 1) throw new Error("runtime enqueue returned multiple rows");

  const existing = (await query(`
    SELECT * FROM public.lm_runtime_jobs
    WHERE job_id = $1 AND tenant_id = $2
    LIMIT 1
  `, [job.job_id, job.tenant_id])).rows;
  if (existing.length !== 1) {
    throw new Error(
      "runtime job id collision: this job_id already exists but is not visible to this tenant",
    );
  }
  assertSameJob(existing[0], job);
  return { created: false, job: existing[0] };
}

function availableInstant(value) {
  const text = String(value == null ? "" : value).trim();
  const milliseconds = Date.parse(text);
  if (!Number.isFinite(milliseconds) || !/[zZ]|[+-]\d\d:\d\d$/.test(text)) {
    throw new Error("runtime available time invalid");
  }
  return new Date(milliseconds).toISOString();
}

async function enqueueJobAt(input, availableAtValue, opts = {}) {
  const job = buildRuntimeJob(input);
  const availableAt = availableInstant(availableAtValue);
  const { query } = database(opts);
  const inserted = (await query(`
    INSERT INTO public.lm_runtime_jobs (
      job_id, tenant_id, loop_id, capability, effect_class, effect_key,
      input_refs, max_attempts, available_at
    ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9::timestamptz)
    ON CONFLICT (job_id) DO NOTHING
    RETURNING *
  `, [
    job.job_id,
    job.tenant_id,
    job.loop_id,
    job.capability,
    job.effect_class,
    job.effect_key,
    JSON.stringify(job.input_refs),
    job.max_attempts,
    availableAt,
  ])).rows;
  if (inserted.length > 1) throw new Error("runtime scheduled enqueue returned multiple rows");
  let row;
  let created;
  if (inserted.length === 1) {
    [row] = inserted;
    created = true;
  } else {
    const existing = (await query(`
      SELECT * FROM public.lm_runtime_jobs
      WHERE job_id = $1 AND tenant_id = $2
      LIMIT 1
    `, [job.job_id, job.tenant_id])).rows;
    if (existing.length !== 1) {
      throw new Error("runtime scheduled job id collision");
    }
    [row] = existing;
    created = false;
  }
  assertSameJob(row, job);
  let storedAvailableAt;
  try { storedAvailableAt = new Date(row.available_at).toISOString(); }
  catch { throw new Error("runtime available time collision"); }
  if (!Number.isFinite(Date.parse(storedAvailableAt)) || storedAvailableAt !== availableAt) {
    throw new Error("runtime available time collision");
  }
  return { created, job: row };
}

async function readCommonJob(input, opts = {}) {
  const tenantId = nonEmpty(input && input.tenantId, "runtime tenant id");
  const jobId = nonEmpty(input && input.jobId, "runtime job id");
  const { query } = database(opts);
  const rows = (await query(`
    SELECT job_id, tenant_id, loop_id, capability, effect_class, effect_key,
           input_refs, max_attempts
    FROM public.lm_runtime_jobs
    WHERE job_id = $1 AND tenant_id = $2
    LIMIT 1
  `, [jobId, tenantId])).rows;
  if (rows.length === 0) return null;
  if (rows.length !== 1) throw new Error("runtime common job read returned multiple rows");
  return commonRecordContract().projectJob(rows[0]);
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digestId(prefix, value) {
  return `${prefix}-${crypto.createHash("sha256").update(value).digest("hex").slice(0, 24)}`;
}

function postgresReceiptToCommon(row) {
  const payload = receiptObject(row && row.receipt);
  if (!new Set(["publish", "message", "money"]).has(row.effect_class)) {
    throw new Error("runtime common receipt requires an external effect");
  }
  const outcome = row.outcome === "completed" ? "verified"
    : row.outcome === "failed" ? "failed"
      : row.outcome === "reconciled_present" ? "reconciled"
        : null;
  if (row.outcome === "reconciled_absent") {
    throw new Error("runtime reconciled_absent receipt is not representable by the common contract");
  }
  if (!outcome) throw new Error("runtime receipt outcome is not common");
  const provider = payload.provider ?? payload.provider_route;
  const externalValue = payload.external_ref ?? payload.provider_post_id
    ?? payload.message_id ?? payload.public_url ?? null;
  if (externalValue !== null && typeof externalValue !== "string"
    && !(Number.isSafeInteger(externalValue) && externalValue >= 0)) {
    throw new Error("runtime receipt external reference is invalid");
  }
  const externalRef = externalValue == null ? null : String(externalValue);
  const evidenceRefs = Array.isArray(payload.evidence_refs) ? payload.evidence_refs : [];
  if (["verified", "reconciled"].includes(outcome)
    && (!provider || !externalRef || evidenceRefs.length < 1)) {
    throw new Error("runtime completed receipt lacks provider verification evidence");
  }
  const identity = `${row.tenant_id}\n${row.job_id}\n${row.attempt}`;
  return commonRecordContract().projectReceipt({
    receipt_id: digestId("receipt", identity),
    effect_id: digestId("effect", `${identity}\n${row.effect_key || "none"}`),
    loop_id: row.loop_id,
    run_id: payload.run_id || digestId("run", identity),
    outcome,
    provider: provider || "runtime-job-store",
    recorded_at: new Date(row.created_at).toISOString(),
    external_ref: externalRef,
    payload_sha256: crypto.createHash("sha256").update(canonicalJson(payload)).digest("hex"),
    evidence_refs: evidenceRefs,
  });
}

async function readCommonReceipt(input, opts = {}) {
  const tenantId = nonEmpty(input && input.tenantId, "runtime tenant id");
  const jobId = nonEmpty(input && input.jobId, "runtime job id");
  const attempt = positiveInteger(input && input.attempt, "runtime attempt", 20);
  const { query } = database(opts);
  const rows = (await query(`
    SELECT receipts.job_id, receipts.tenant_id, receipts.attempt, receipts.outcome,
           receipts.effect_key, receipts.receipt, receipts.created_at,
           jobs.loop_id, jobs.effect_class
    FROM public.lm_runtime_job_receipts AS receipts
    JOIN public.lm_runtime_jobs AS jobs
      ON jobs.job_id = receipts.job_id AND jobs.tenant_id = receipts.tenant_id
    WHERE receipts.job_id = $1 AND receipts.tenant_id = $2 AND receipts.attempt = $3
    LIMIT 1
  `, [jobId, tenantId, attempt])).rows;
  if (rows.length === 0) return null;
  if (rows.length !== 1) throw new Error("runtime common receipt read returned multiple rows");
  return postgresReceiptToCommon(rows[0]);
}

async function claimJobs(input, opts = {}) {
  const workerId = nonEmpty(input && input.workerId, "runtime worker id");
  const capabilities = input && input.capabilities;
  if (
    !Array.isArray(capabilities)
    || capabilities.length < 1
    || capabilities.length > 50
  ) {
    throw new Error("runtime capabilities invalid");
  }
  const boundedCapabilities = capabilities.map((value) => (
    nonEmpty(value, "runtime capability")
  ));
  const tenantId = input.tenantId == null
    ? null
    : nonEmpty(input.tenantId, "runtime tenant id");
  const limit = positiveInteger(input.limit == null ? 1 : input.limit, "runtime claim limit", 50);
  const leaseSeconds = positiveInteger(
    input.leaseSeconds == null ? 180 : input.leaseSeconds,
    "runtime lease",
    900,
  );
  if (leaseSeconds < 30) throw new Error("runtime lease invalid");
  const { query } = database(opts);
  return (await query(
    "SELECT * FROM public.claim_lm_runtime_jobs($1, $2::text[], $3, $4, $5)",
    [workerId, boundedCapabilities, tenantId, limit, leaseSeconds],
  )).rows;
}

function identity(input, withWorker = true) {
  const value = {
    tenantId: nonEmpty(input && input.tenantId, "runtime tenant id"),
    jobId: nonEmpty(input && input.jobId, "runtime job id"),
    attempt: positiveInteger(input && input.attempt, "runtime attempt", 20),
  };
  if (withWorker) value.workerId = nonEmpty(input && input.workerId, "runtime worker id");
  return value;
}

async function oneRow(query, sql, params, lostMessage) {
  const rows = (await query(sql, params)).rows;
  if (rows.length !== 1) throw new Error(lostMessage);
  return rows[0];
}

async function heartbeatJob(input, opts = {}) {
  const id = identity(input);
  const leaseSeconds = positiveInteger(
    input.leaseSeconds == null ? 180 : input.leaseSeconds,
    "runtime lease",
    900,
  );
  if (leaseSeconds < 30) throw new Error("runtime lease invalid");
  const { query } = database(opts);
  return oneRow(
    query,
    "SELECT * FROM public.heartbeat_lm_runtime_job($1, $2, $3, $4, $5)",
    [id.tenantId, id.jobId, id.attempt, id.workerId, leaseSeconds],
    "runtime heartbeat lost lease",
  );
}

function receiptObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("runtime receipt invalid");
  }
  if (Buffer.byteLength(JSON.stringify(value)) > 16_384) {
    throw new Error("runtime receipt too large");
  }
  return value;
}

async function completeJob(input, opts = {}) {
  const id = identity(input);
  const receipt = receiptObject(input.receipt);
  const { query } = database(opts);
  return oneRow(
    query,
    "SELECT * FROM public.complete_lm_runtime_job($1, $2, $3, $4, $5::jsonb)",
    [id.tenantId, id.jobId, id.attempt, id.workerId, JSON.stringify(receipt)],
    "runtime completion lost lease",
  );
}

async function completeJobAndEnqueue(input, opts = {}) {
  const id = identity(input);
  const receipt = receiptObject(input.receipt);
  const next = buildRuntimeJob(input.nextJob);
  const availableAt = availableInstant(input.availableAt);
  if (next.tenant_id !== id.tenantId) throw new Error("runtime continuation tenant mismatch");
  const { query } = database(opts);
  return oneRow(query,
    "SELECT * FROM public.complete_lm_runtime_job_and_enqueue($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14::timestamptz)",
    [id.tenantId, id.jobId, id.attempt, id.workerId, JSON.stringify(receipt), next.job_id,
      next.loop_id, next.capability, next.effect_class, next.effect_key, next.tenant_id,
      JSON.stringify(next.input_refs), next.max_attempts, availableAt],
    "runtime completion or continuation lost lease");
}

async function failJob(input, opts = {}) {
  const id = identity(input);
  const errorCode = nonEmpty(input && input.errorCode, "runtime error code");
  const unknownEffect = input.unknownEffect === true;
  const { query } = database(opts);
  return oneRow(
    query,
    "SELECT * FROM public.fail_lm_runtime_job($1, $2, $3, $4, $5, $6)",
    [id.tenantId, id.jobId, id.attempt, id.workerId, errorCode, unknownEffect],
    "runtime failure lost lease",
  );
}

async function resolveReconciliation(input, opts = {}) {
  const id = identity(input, false);
  if (!["present", "absent"].includes(input.decision)) {
    throw new Error("runtime reconciliation decision invalid");
  }
  const receipt = receiptObject(input.receipt);
  const { query } = database(opts);
  return oneRow(
    query,
    "SELECT * FROM public.resolve_lm_runtime_effect($1, $2, $3, $4, $5::jsonb)",
    [id.tenantId, id.jobId, id.attempt, input.decision, JSON.stringify(receipt)],
    "runtime reconciliation lost job",
  );
}

async function recordUnknownReconciliation(input, opts = {}) {
  const id = identity(input, false);
  const maxUnknown = input.maxUnknownResults == null
    ? MAX_UNKNOWN_RECONCILE_RESULTS
    : input.maxUnknownResults;
  if (!Number.isInteger(maxUnknown) || maxUnknown < 1 || maxUnknown > 20) {
    throw new Error("runtime unknown limit invalid");
  }
  const { query } = database(opts);
  return oneRow(
    query,
    "SELECT * FROM public.age_lm_runtime_reconciliation($1, $2, $3, $4)",
    [id.tenantId, id.jobId, id.attempt, maxUnknown],
    "runtime reconciliation lost job",
  );
}

async function closeRuntimeJobStore() {
  const pool = defaultPool;
  defaultPool = undefined;
  if (pool) await pool.end();
}

module.exports = {
  buildRuntimeJob,
  enqueueJob,
  enqueueJobAt,
  readCommonJob,
  readCommonReceipt,
  claimJobs,
  heartbeatJob,
  completeJob,
  completeJobAndEnqueue,
  failJob,
  resolveReconciliation,
  recordUnknownReconciliation,
  closeRuntimeJobStore,
  MAX_UNKNOWN_RECONCILE_RESULTS,
};
