"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { createMoneyPrinterRuntimeStore } = require("./money-printer-runtime-store.js");
const { buildOpportunity } = require("./money-printer-opportunity.js");

const TENANT = "tenant-a";
const ID = "a".repeat(64);
const NOW = "2026-08-29T00:00:00.000Z";
const SYMPHONY_MIGRATION = path.join(__dirname, "../migrations/2026-08-30-lm-symphony-dispatches.sql");
const CLOSE_RECOVERY_MIGRATION = path.join(__dirname, "../migrations/2026-08-31-lm-symphony-close-recovery.sql");
const HUMAN_TASK_MIGRATION = path.join(__dirname, "../migrations/2026-08-29-lm-money-printer-human-tasks.sql");

test("R01 Symphony migration adds waiting_agent without dropping runtime states", () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /DROP CONSTRAINT IF EXISTS lm_runtime_jobs_status_check/i);
  const statusConstraint = migration.match(/ADD CONSTRAINT lm_runtime_jobs_status_check CHECK \([\s\S]*?status IN \(([^)]+)\)/i);
  assert.ok(statusConstraint);
  for (const status of ["queued", "running", "waiting_agent", "waiting_human", "reconciling", "completed", "dead_letter"]) {
    assert.match(statusConstraint[1], new RegExp(`'${status}'`));
  }
});

test("R02 Symphony migration adds one tenant-job scoped dispatch ledger", () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /CREATE TABLE IF NOT EXISTS public\.lm_symphony_dispatches/i);
  assert.match(migration, /PRIMARY KEY \(tenant_id, dispatch_id\)/i);
  assert.match(migration, /FOREIGN KEY \(job_id, tenant_id\)[\s\S]*REFERENCES public\.lm_runtime_jobs \(job_id, tenant_id\)/i);
  for (const status of ["claimed", "mirrored", "result_ready", "consumed", "failed"]) {
    assert.match(migration, new RegExp(`'${status}'`));
  }
  assert.match(migration, /consumed_at timestamptz/i);
  assert.match(migration, /CREATE UNIQUE INDEX IF NOT EXISTS lm_symphony_dispatches_open_job_idx[\s\S]*ON public\.lm_symphony_dispatches \(tenant_id, job_id\)[\s\S]*WHERE status IN \('claimed', 'mirrored', 'result_ready'\)/i);
  assert.match(migration, /ENABLE ROW LEVEL SECURITY/i);
  assert.match(migration, /REVOKE ALL ON TABLE public\.lm_symphony_dispatches FROM PUBLIC, anon, authenticated/i);
});

test("R03 Symphony claim atomically moves one queued general-agent job", async () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.claim_lm_symphony_job\(p_tenant_id text\)/i);
  assert.match(migration, /capability = 'general-agent\.work'[\s\S]*effect_class = 'none'[\s\S]*status = 'queued'[\s\S]*FOR UPDATE SKIP LOCKED/i);
  assert.match(migration, /AND EXISTS \([\s\S]*FROM public\.lm_money_opportunities AS opportunities[\s\S]*opportunities\.uid = jobs\.tenant_id[\s\S]*jobs\.job_id = 'goal:' \|\| opportunities\.opportunity_id[\s\S]*\)/i);
  assert.match(migration, /SET status = 'waiting_agent'[\s\S]*lease_owner = NULL[\s\S]*lease_expires_at = NULL/i);
  assert.match(migration, /INSERT INTO public\.lm_symphony_dispatches[\s\S]*'claimed'/i);

  const dispatch = {
    tenant_id: TENANT, dispatch_id: "d".repeat(64), job_id: `goal:${ID}`,
    round: 1, status: "claimed", issue_ref: null, result_ref: null,
    result_hash: null, result_payload: null, failure_code: null,
    claimed_at: NOW, mirrored_at: null, result_ready_at: null, consumed_at: null,
    failed_at: null, issue_closed_at: null,
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    return { rows: [dispatch] };
  } });
  assert.deepEqual(await store.claimSymphony({ uid: TENANT }), dispatch);
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.claim_lm_symphony_job\(\$1\)/i);
  assert.deepEqual(calls[0].values, [TENANT]);

  const empty = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [] }) });
  assert.equal(await empty.claimSymphony({ uid: TENANT }), null);
  const foreign = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [{ ...dispatch, tenant_id: "tenant-b" }] }) });
  await assert.rejects(foreign.claimSymphony({ uid: TENANT }), /readback/i);

  const globalCalls = [];
  const global = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    globalCalls.push({ sql, values });
    return globalCalls.length === 1 ? { rows: [{ tenant_id: TENANT }] } : { rows: [dispatch] };
  } });
  assert.deepEqual(await global.claimSymphonyNext(), dispatch);
  assert.match(globalCalls[0].sql, /FROM public\.lm_symphony_dispatches dispatches[\s\S]*status IN \('claimed', 'mirrored', 'result_ready', 'consumed'\)[\s\S]*UNION ALL[\s\S]*FROM public\.lm_runtime_jobs jobs[\s\S]*ORDER BY priority, ready_at, tenant_id/i);
  assert.deepEqual(globalCalls[1].values, [TENANT]);
});

test("R11 Symphony claim recovers the oldest claimed dispatch before selecting queued work", () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  const start = migration.indexOf("CREATE OR REPLACE FUNCTION public.claim_lm_symphony_job");
  const end = migration.indexOf("REVOKE ALL ON FUNCTION public.claim_lm_symphony_job", start);
  assert.ok(start >= 0 && end > start);
  const functionBody = migration.slice(start, end);
  const claimedRecovery = functionBody.search(/FROM public\.lm_symphony_dispatches[\s\S]*?status = 'claimed'/i);
  const queuedClaim = functionBody.search(/FROM public\.lm_runtime_jobs[\s\S]*?status = 'queued'/i);
  assert.ok(claimedRecovery >= 0 && queuedClaim > claimedRecovery, "claimed recovery must precede queued claim");
  assert.match(functionBody.slice(claimedRecovery), /ORDER BY dispatches\.claimed_at, dispatches\.dispatch_id[\s\S]*FOR UPDATE/i);
  assert.match(functionBody.slice(claimedRecovery, queuedClaim), /IF FOUND THEN[\s\S]*RETURN NEXT v_dispatch;[\s\S]*RETURN;/i);
});

test("S07 additive migration fences issue closure and recovers every unclosed durable status", () => {
  const migration = fs.readFileSync(CLOSE_RECOVERY_MIGRATION, "utf8");
  assert.match(migration, /ALTER TABLE public\.lm_symphony_dispatches[\s\S]*ADD COLUMN IF NOT EXISTS issue_closed_at timestamptz/i);
  assert.match(migration, /CHECK \(issue_closed_at IS NULL OR status = 'consumed'\)/i);
  const start = migration.indexOf("CREATE OR REPLACE FUNCTION public.claim_lm_symphony_job");
  const end = migration.indexOf("REVOKE ALL ON FUNCTION public.claim_lm_symphony_job", start);
  assert.ok(start >= 0 && end > start);
  const claim = migration.slice(start, end);
  assert.match(claim, /status IN \('claimed', 'mirrored', 'result_ready', 'consumed'\)/i);
  assert.match(claim, /issue_closed_at IS NULL[\s\S]*ORDER BY dispatches\.claimed_at/i);
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.ack_lm_symphony_issue_closed\(/i);
  assert.match(migration, /status = 'consumed'[\s\S]*issue_ref = p_issue_ref[\s\S]*result_ref = p_result_ref[\s\S]*result_hash = p_result_hash/i);
  assert.match(migration, /issue_closed_at = clock_timestamp\(\)/i);
  assert.match(migration, /REVOKE ALL ON FUNCTION public\.ack_lm_symphony_issue_closed\(text,text,text,text,text\) FROM PUBLIC, anon, authenticated/i);
  assert.match(migration, /GRANT EXECUTE ON FUNCTION public\.ack_lm_symphony_issue_closed\(text,text,text,text,text\) TO service_role/i);
});

test("R04 Symphony issue readback is idempotent and conflict-fenced", async () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.record_lm_symphony_issue\(\s*p_tenant_id text,\s*p_dispatch_id text,\s*p_issue_ref text\s*\)/i);
  assert.match(migration, /status = 'mirrored'[\s\S]*issue_ref = p_issue_ref[\s\S]*mirrored_at = clock_timestamp\(\)/i);
  assert.match(migration, /symphony issue conflict/i);

  const issueRef = "github-issue://Daisuke134/life-manager-workrooms/1";
  const dispatch = {
    tenant_id: TENANT, dispatch_id: "d".repeat(64), job_id: `goal:${ID}`,
    round: 1, status: "mirrored", issue_ref: issueRef, result_ref: null,
    result_hash: null, result_payload: null, failure_code: null,
    claimed_at: NOW, mirrored_at: NOW, result_ready_at: null, consumed_at: null,
    failed_at: null, issue_closed_at: null,
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    return { rows: [dispatch] };
  } });
  const input = { uid: TENANT, dispatchId: dispatch.dispatch_id, issueRef };
  assert.deepEqual(await store.recordSymphonyIssue(input), dispatch);
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.record_lm_symphony_issue\(\$1, \$2, \$3\)/i);
  assert.deepEqual(calls[0].values, [TENANT, dispatch.dispatch_id, issueRef]);
  await assert.rejects(store.recordSymphonyIssue({ ...input, issueRef: "https://github.com/Daisuke134/life-manager-workrooms/issues/1" }), /issue/i);
  const foreign = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [{ ...dispatch, tenant_id: "tenant-b" }] }) });
  await assert.rejects(foreign.recordSymphonyIssue(input), /readback/i);
});

test("R05 Symphony result is stored once while the same job remains waiting_agent", async () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.record_lm_symphony_result\(/i);
  assert.match(migration, /status = 'result_ready'[\s\S]*result_ref = p_result_ref[\s\S]*result_hash = p_result_hash[\s\S]*result_payload = p_result_payload/i);
  assert.match(migration, /status = 'waiting_agent'/i);
  assert.match(migration, /symphony result conflict/i);
  for (const field of ["protocol", "tenant_id", "dispatch_id", "job_id", "status", "execution_id", "reason_code", "question"]) {
    assert.match(migration, new RegExp(`jsonb_typeof\\(p_result_payload->'${field}'\\)\\s*<>\\s*'string'`));
  }

  const resultRef = "github-comment://Daisuke134/life-manager-workrooms/1/2";
  const payload = {
    protocol: "LM_RESULT_V1", tenant_id: TENANT, dispatch_id: "d".repeat(64),
    job_id: `goal:${ID}`, status: "completed", execution_id: "codex-round-1", artifact_refs: [],
  };
  const dispatch = {
    tenant_id: TENANT, dispatch_id: payload.dispatch_id, job_id: payload.job_id,
    round: 1, status: "result_ready",
    issue_ref: "github-issue://Daisuke134/life-manager-workrooms/1",
    result_ref: resultRef, result_hash: "e".repeat(64), result_payload: payload, failure_code: null,
    claimed_at: NOW, mirrored_at: NOW, result_ready_at: NOW, consumed_at: null,
    failed_at: null, issue_closed_at: null,
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    return { rows: [dispatch] };
  } });
  const input = { uid: TENANT, dispatchId: dispatch.dispatch_id, resultRef, resultHash: dispatch.result_hash, payload };
  assert.deepEqual(await store.recordSymphonyResult(input), dispatch);
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.record_lm_symphony_result\(\$1, \$2, \$3, \$4, \$5\)/i);
  assert.deepEqual(calls[0].values, [TENANT, dispatch.dispatch_id, resultRef, dispatch.result_hash, JSON.stringify(payload)]);
  await assert.rejects(store.recordSymphonyResult({ ...input, payload: { ...payload, status: "unknown" } }), /result/i);
});

test("R12 runtime store accepts exact result readback at result_ready or consumed only", async () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /IF v_dispatch\.status IN \('result_ready', 'consumed'\)[\s\S]*result_ref = p_result_ref[\s\S]*result_hash = p_result_hash[\s\S]*result_payload = p_result_payload/i);
  const resultRef = "github-comment://Daisuke134/life-manager-workrooms/1/2";
  const payload = {
    protocol: "LM_RESULT_V1", tenant_id: TENANT, dispatch_id: "d".repeat(64),
    job_id: `goal:${ID}`, status: "completed", execution_id: "codex-round-1", artifact_refs: [],
  };
  const input = { uid: TENANT, dispatchId: payload.dispatch_id, resultRef, resultHash: "e".repeat(64), payload };
  for (const status of ["result_ready", "consumed"]) {
    const row = {
      tenant_id: TENANT, dispatch_id: input.dispatchId, job_id: payload.job_id, round: 1, status,
      issue_ref: "github-issue://Daisuke134/life-manager-workrooms/1", result_ref: resultRef,
      result_hash: input.resultHash, result_payload: payload, failure_code: null,
      claimed_at: NOW, mirrored_at: NOW, result_ready_at: NOW, consumed_at: status === "consumed" ? NOW : null,
      failed_at: null, issue_closed_at: null,
    };
    const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [row] }) });
    assert.deepEqual(await store.recordSymphonyResult(input), row);
  }
  const invalid = {
    tenant_id: TENANT, dispatch_id: input.dispatchId, job_id: payload.job_id, round: 1, status: "mirrored",
    issue_ref: "github-issue://Daisuke134/life-manager-workrooms/1", result_ref: resultRef,
    result_hash: input.resultHash, result_payload: payload, failure_code: null,
  };
  const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [invalid] }) });
  await assert.rejects(store.recordSymphonyResult(input), /readback/i);
});

test("S07 claim accepts coherent durable recovery rows and rejects incoherent fields", async () => {
  const issueRef = "github-issue://Daisuke134/life-manager-workrooms/1";
  const resultRef = "github-comment://Daisuke134/life-manager-workrooms/1/2";
  const payload = {
    protocol: "LM_RESULT_V1", tenant_id: TENANT, dispatch_id: "d".repeat(64),
    job_id: `goal:${ID}`, status: "completed", execution_id: "codex-1", artifact_refs: [],
  };
  const common = {
    tenant_id: TENANT, dispatch_id: "d".repeat(64), job_id: `goal:${ID}`, round: 1,
    issue_ref: issueRef, result_ref: resultRef, result_hash: "e".repeat(64),
    result_payload: payload, failure_code: null,
    claimed_at: NOW, mirrored_at: NOW, result_ready_at: NOW, consumed_at: NOW, failed_at: null,
    issue_closed_at: null,
  };
  for (const status of ["claimed", "mirrored", "result_ready", "consumed"]) {
    const row = status === "claimed"
      ? { ...common, status, issue_ref: null, result_ref: null, result_hash: null, result_payload: null, mirrored_at: null, result_ready_at: null, consumed_at: null }
      : status === "mirrored"
        ? { ...common, status, result_ref: null, result_hash: null, result_payload: null, result_ready_at: null, consumed_at: null }
        : { ...common, status, consumed_at: status === "consumed" ? NOW : null, issue_closed_at: null };
    const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [row] }) });
    assert.deepEqual(await store.claimSymphony({ uid: TENANT }), row);
  }
  for (const row of [
    { ...common, status: "mirrored", result_ref: resultRef },
    { ...common, status: "result_ready", result_payload: null },
    { ...common, status: "result_ready", issue_closed_at: NOW },
    { ...common, status: "consumed", issue_closed_at: NOW },
    { ...common, status: "consumed", consumed_at: null },
    { ...common, status: "failed", issue_ref: null, result_ref: null, result_hash: null, result_payload: null },
  ]) {
    const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [row] }) });
    await assert.rejects(store.claimSymphony({ uid: TENANT }), /claim readback/i);
  }
});

test("S07 runtime store acknowledges one exact closed dispatch with strict readback", async () => {
  const input = {
    uid: TENANT,
    dispatchId: "d".repeat(64),
    issueRef: "github-issue://Daisuke134/life-manager-workrooms/1",
    resultRef: "github-comment://Daisuke134/life-manager-workrooms/1/2",
    resultHash: "e".repeat(64),
  };
  const row = {
    tenant_id: TENANT, dispatch_id: input.dispatchId, job_id: `goal:${ID}`, round: 1,
    status: "consumed", issue_ref: input.issueRef, result_ref: input.resultRef,
    result_hash: input.resultHash,
    result_payload: { protocol: "LM_RESULT_V1", status: "completed" },
    failure_code: null, claimed_at: NOW, mirrored_at: NOW, result_ready_at: NOW,
    consumed_at: NOW, failed_at: null, issue_closed_at: NOW,
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({
    query: async (sql, values) => { calls.push({ sql, values }); return { rows: [row] }; },
  });
  assert.deepEqual(await store.acknowledgeSymphonyIssueClosed(input), row);
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.ack_lm_symphony_issue_closed\(\$1, \$2, \$3, \$4, \$5\)/i);
  assert.deepEqual(calls[0].values, [TENANT, input.dispatchId, input.issueRef, input.resultRef, input.resultHash]);
  for (const invalid of [
    { ...input, issueRef: "github-issue://other/repo/1" },
    { ...input, resultRef: "not-a-comment" },
    { ...input, resultHash: "x".repeat(64) },
  ]) {
    await assert.rejects(store.acknowledgeSymphonyIssueClosed(invalid), /close invalid/i);
  }
  for (const invalidRow of [
    { ...row, issue_closed_at: null },
    { ...row, status: "result_ready" },
    { ...row, issue_ref: "github-issue://Daisuke134/life-manager-workrooms/2" },
  ]) {
    const invalidStore = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [invalidRow] }) });
    await assert.rejects(invalidStore.acknowledgeSymphonyIssueClosed(input), /close readback/i);
  }
});

test("Symphony result input rejects numeric identity fields before the query", async () => {
  const payload = {
    protocol: "LM_RESULT_V1", tenant_id: TENANT, dispatch_id: "d".repeat(64),
    job_id: `goal:${ID}`, status: "needs_human", execution_id: "codex-human-1",
    artifact_refs: [], reason_code: "provider_interview", question: "Complete the provider interview.",
    required_format: { type: "confirmation" },
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({
    query: async (...args) => {
      calls.push(args);
      return { rows: [] };
    },
  });
  for (const [field, value] of [["job_id", 42], ["execution_id", 42], ["reason_code", 42]]) {
    await assert.rejects(
      store.recordSymphonyResult({
        uid: TENANT, dispatchId: payload.dispatch_id,
        resultRef: "github-comment://Daisuke134/life-manager-workrooms/1/2",
        resultHash: "e".repeat(64), payload: { ...payload, [field]: value },
      }),
      /result invalid/i,
    );
  }
  assert.equal(calls.length, 0);
});

test("R06 completed dispatch consume is single-use and rejects replay", async () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.consume_lm_symphony_completed\(/i);
  assert.match(migration, /result_payload->>'status' <> 'completed'/i);
  assert.match(migration, /UPDATE public\.lm_money_opportunities[\s\S]*SET status = 'QUALIFIED'/i);
  assert.match(migration, /UPDATE public\.lm_runtime_jobs[\s\S]*SET status = 'completed'[\s\S]*attempt = GREATEST\(attempt, 1\)/i);
  assert.match(migration, /INSERT INTO public\.lm_runtime_job_receipts[\s\S]*'completed'/i);
  assert.match(migration, /SET status = 'consumed'[\s\S]*consumed_at = clock_timestamp\(\)/i);
  assert.match(migration, /IF v_dispatch\.status = 'consumed'[\s\S]*RAISE EXCEPTION 'symphony completion conflict'/i);

  const dispatch = {
    tenant_id: TENANT, dispatch_id: "d".repeat(64), job_id: `goal:${ID}`,
    round: 1, status: "consumed",
    issue_ref: "github-issue://Daisuke134/life-manager-workrooms/1",
    result_ref: "github-comment://Daisuke134/life-manager-workrooms/1/2",
    result_hash: "e".repeat(64),
    result_payload: {
      protocol: "LM_RESULT_V1", tenant_id: TENANT, dispatch_id: "d".repeat(64),
      job_id: `goal:${ID}`, status: "completed", execution_id: "codex-round-1", artifact_refs: [],
    },
    failure_code: null,
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    throw new Error("symphony completion conflict");
  } });
  await assert.rejects(
    store.consumeSymphonyCompleted({ uid: TENANT, dispatchId: dispatch.dispatch_id }),
    /symphony completion conflict/i,
  );
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.consume_lm_symphony_completed\(\$1, \$2\)/i);
  assert.deepEqual(calls[0].values, [TENANT, dispatch.dispatch_id]);
});

test("R07 a needs_human result reuses the existing atomic HumanTask transition", async () => {
  const migration = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(migration, /CREATE OR REPLACE FUNCTION public\.consume_lm_symphony_human_task\(/i);
  assert.match(migration, /result_payload->>'status' <> 'needs_human'/i);
  assert.match(migration, /SET status = 'queued'[\s\S]*status = 'waiting_agent'/i);
  assert.match(migration, /FROM public\.create_lm_human_task\(/i);
  assert.match(migration, /SET status = 'consumed'[\s\S]*consumed_at = clock_timestamp\(\)/i);

  const task = {
    uid: TENANT, task_id: "b".repeat(64), job_id: `goal:${ID}`, version: 1,
    question: "Complete the provider interview.", required_format: { type: "confirmation" },
    reason_code: "provider_interview", resume_ref: `runtime-job://${TENANT}/goal%3A${ID}`,
    context_refs: { goal_ref: `intent-entry://${TENANT}/${ID}` },
    human_boundary_ref: `human-boundary://sha256/${"c".repeat(64)}`,
    status: "open",
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    return { rows: [task] };
  } });
  assert.deepEqual(await store.consumeSymphonyHumanTask({ uid: TENANT, dispatchId: "d".repeat(64) }), task);
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.consume_lm_symphony_human_task\(\$1, \$2\)/i);
  assert.deepEqual(calls[0].values, [TENANT, "d".repeat(64)]);
});

test("R08 answering a HumanTask requeues the same job for the next Symphony round", () => {
  const human = fs.readFileSync(HUMAN_TASK_MIGRATION, "utf8");
  const symphony = fs.readFileSync(SYMPHONY_MIGRATION, "utf8");
  assert.match(human, /UPDATE public\.lm_runtime_jobs[\s\S]*SET status = 'queued'[\s\S]*WHERE tenant_id = p_uid[\s\S]*job_id = v_task\.job_id[\s\S]*status = 'waiting_human'/i);
  assert.match(symphony, /COALESCE\(MAX\(dispatches\.round\), 0\) \+ 1/i);
  assert.match(symphony, /dispatches\.status IN \('claimed', 'mirrored', 'result_ready'\)/i);
  assert.doesNotMatch(symphony, /dispatches\.status IN \('claimed', 'mirrored', 'result_ready', 'consumed'\)/i);
});

function opportunity() {
  return {
    uid: TENANT, opportunity_id: ID, source_url: "https://public.example/opportunity",
    title: "Public opportunity", goal_statement: "Complete it.", value_minor: "50000",
    currency: "JPY", status: "DISCOVERED", goal_ref: `intent-entry://${TENANT}/${ID}`,
    job_id: `goal:${ID}`, observed_at: NOW,
  };
}

test("runtime store uses parameterized RPCs and tenant-bound reads", async () => {
  const calls = [];
  const task = {
    uid: TENANT, task_id: "b".repeat(64), job_id: `goal:${ID}`, version: 1, question: "Approve?",
    required_format: "approval", reason_code: "model_boundary", resume_ref: `runtime-job://${TENANT}/job-1`,
    status: "open", created_at: NOW, updated_at: NOW,
  };
  const snapshotTask = {
    uid: TENANT, task_id: task.task_id, job_id: task.job_id, reason_code: task.reason_code,
    version: task.version, status: task.status, created_at: task.created_at, updated_at: task.updated_at,
  };
  const store = createMoneyPrinterRuntimeStore({
    query: async (sql, values) => {
      calls.push({ sql, values });
      if (sql.includes("create_lm_money_opportunity")) return { rows: [opportunity()] };
      if (sql.includes("answer_lm_human_task")) return { rows: [{ ...task, status: "answered", answer_ref: "vault-answer://tenant-a/answer-1" }] };
      if (sql.includes("FROM public.lm_money_opportunities")) return { rows: [opportunity()] };
      if (sql.includes("FROM public.lm_runtime_jobs")) return { rows: [{ tenant_id: TENANT, job_id: `goal:${ID}`, status: "queued", created_at: NOW, updated_at: NOW }] };
      if (sql.includes("FROM public.lm_human_tasks")) {
        return { rows: [/ORDER BY updated_at DESC/.test(sql) ? snapshotTask : task] };
      }
      if (sql.includes("FROM public.lm_runtime_job_receipts")) return { rows: [{ tenant_id: TENANT, job_id: `goal:${ID}`, attempt: 1, outcome: "completed", created_at: NOW, receipt: { record_type: "application_receipt" } }] };
      throw new Error("unexpected query");
    },
  });

  assert.deepEqual(await store.createOpportunity(opportunity()), opportunity());
  assert.deepEqual(await store.readNext({ uid: TENANT }), task);
  assert.equal((await store.answerOnce({ uid: TENANT, taskId: task.task_id, version: 1, answerRef: "vault-answer://tenant-a/answer-1" })).status, "answered");
  const snapshot = await store.readRuntimeSnapshot(TENANT);
  assert.equal(snapshot.opportunities.length, 1);
  assert.equal(snapshot.runtimeJobs.length, 1);
  assert.deepEqual(snapshot.humanTasks, [snapshotTask]);
  assert.equal(snapshot.humanTasks[0].question, undefined);
  assert.equal(snapshot.humanTasks[0].required_format, undefined);
  assert.equal(snapshot.humanTasks[0].answer_ref, undefined);
  assert.equal(snapshot.humanTasks[0].context_refs, undefined);
  assert.equal(snapshot.receipts.length, 1);
  const humanTaskSnapshotQuery = calls.find((call) => /FROM public\.lm_human_tasks/.test(call.sql) && /ORDER BY updated_at DESC/.test(call.sql));
  assert.ok(humanTaskSnapshotQuery);
  const selectedHumanTaskColumns = humanTaskSnapshotQuery.sql
    .match(/SELECT([\s\S]*?)FROM public\.lm_human_tasks/i)[1]
    .replace(/\s+/g, " ")
    .trim();
  assert.equal(selectedHumanTaskColumns, "uid, task_id, job_id, reason_code, version, status, created_at, updated_at");
  for (const privateColumn of ["question", "required_format", "answer_ref", "context_refs", "resume_ref", "human_boundary_ref"]) {
    assert.doesNotMatch(selectedHumanTaskColumns, new RegExp(`\\b${privateColumn}\\b`, "i"));
  }

  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.create_lm_money_opportunity\(\$1, \$2, \$3, \$4, \$5, \$6, \$7, \$8, \$9\)/i);
  assert.deepEqual(calls[0].values, [TENANT, ID, "https://public.example/opportunity", "Public opportunity", "Complete it.", "50000", "JPY", NOW, `intent-entry://${TENANT}/${ID}`]);
  assert.match(calls[1].sql, /^\s*SELECT uid, task_id, version, question, required_format, reason_code, resume_ref, status, created_at, updated_at\s+FROM public\.lm_human_tasks/i);
  assert.deepEqual(calls[1].values, [TENANT]);
  assert.match(calls[2].sql, /^\s*SELECT \* FROM public\.answer_lm_human_task\(\$1, \$2, \$3, \$4\)/i);
  assert.deepEqual(calls[2].values, [TENANT, task.task_id, 1, "vault-answer://tenant-a/answer-1"]);
  for (const call of calls) assert.ok(Array.isArray(call.values));
});

test("runtime store normalizes a Postgres Date observed_at without changing other fields", async () => {
  const persisted = { ...opportunity(), observed_at: new Date(NOW) };
  const store = createMoneyPrinterRuntimeStore({
    query: async (sql) => {
      if (sql.includes("create_lm_money_opportunity")) return { rows: [persisted] };
      throw new Error("unexpected query");
    },
  });

  assert.deepEqual(await store.createOpportunity(opportunity()), { ...persisted, observed_at: NOW });
});

test("runtime store creates one human task through the atomic pause RPC", async () => {
  const calls = [];
  const task = {
    uid: TENANT, task_id: "b".repeat(64), job_id: `goal:${ID}`, version: 1,
    question: "Complete the assessment.", required_format: { type: "confirmation" },
    reason_code: "identity_assessment", resume_ref: `runtime-job://${TENANT}/goal%3A${ID}`,
    context_refs: { goal_ref: `intent-entry://${TENANT}/${ID}` },
    human_boundary_ref: `human-boundary://sha256/${"c".repeat(64)}`,
    status: "open", created_at: NOW, updated_at: NOW,
  };
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    return { rows: [task] };
  } });

  assert.deepEqual(await store.createOnce(task), task);
  assert.match(calls[0].sql, /^\s*SELECT \* FROM public\.create_lm_human_task\(\$1, \$2, \$3, \$4, \$5, \$6, \$7, \$8, \$9\)/i);
  assert.deepEqual(calls[0].values, [
    task.uid, task.task_id, task.job_id, task.reason_code, task.question,
    JSON.stringify(task.required_format), task.resume_ref, JSON.stringify(task.context_refs), task.human_boundary_ref,
  ]);
});

test("runtime store reads answered references only for the same tenant job", async () => {
  const answered = {
    uid: TENANT, job_id: `goal:${ID}`, reason_code: "identity_assessment",
    answer_ref: `vault-answer://${TENANT}/answer-1`,
    human_boundary_ref: `human-boundary://sha256/${"c".repeat(64)}`,
    version: 1, updated_at: NOW,
  };
  const calls = [];
  const store = createMoneyPrinterRuntimeStore({ query: async (sql, values) => {
    calls.push({ sql, values });
    return { rows: [answered] };
  } });

  assert.deepEqual(await store.readAnsweredForJob({ tenant_id: TENANT, job_id: `goal:${ID}` }), [answered]);
  assert.match(calls[0].sql, /FROM public\.lm_human_tasks[\s\S]*status = 'answered'/i);
  assert.deepEqual(calls[0].values, [TENANT, `goal:${ID}`]);
});

test("runtime store rejects an invalid create opportunity timestamp", async () => {
  const store = createMoneyPrinterRuntimeStore({
    query: async (sql) => {
      if (sql.includes("create_lm_money_opportunity")) {
        return { rows: [{ ...opportunity(), observed_at: new Date("invalid") }] };
      }
      throw new Error("unexpected query");
    },
  });

  await assert.rejects(store.createOpportunity(opportunity()), /observed|time|readback/i);
});

test("runtime store rejects unavailable query and foreign or ambiguous readback", async () => {
  assert.throws(() => createMoneyPrinterRuntimeStore({}), /runtime store unavailable/);
  const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [{ ...opportunity(), uid: "tenant-b" }, opportunity()] }) });
  await assert.rejects(store.createOpportunity(opportunity()), /readback/);
});

test("runtime store reads and idempotently qualifies one exact opportunity", async () => {
  const calls = [];
  const qualified = { ...opportunity(), status: "QUALIFIED" };
  const expected = {
    tenant_id: TENANT,
    opportunity_id: ID,
    goal_ref: `intent-entry://${TENANT}/${ID}`,
  };
  const store = createMoneyPrinterRuntimeStore({
    query: async (sql, values) => {
      calls.push({ sql, values });
      if (sql.includes("WITH updated AS")) return { rows: [qualified] };
      if (sql.includes("FROM public.lm_money_opportunities")) return { rows: [opportunity()] };
      throw new Error("unexpected query");
    },
  });

  assert.deepEqual(await store.readOpportunity(expected), opportunity());
  assert.deepEqual(await store.updateOpportunity(expected, "QUALIFIED"), qualified);
  assert.deepEqual(await store.updateOpportunity(expected, "QUALIFIED"), qualified);

  assert.match(calls[0].sql, /SELECT uid, opportunity_id, source_url, title, goal_statement, value_minor, currency, status, goal_ref, observed_at\s+FROM public\.lm_money_opportunities/i);
  assert.match(calls[0].sql, /WHERE uid = \$1 AND opportunity_id = \$2 AND goal_ref = \$3/i);
  assert.deepEqual(calls[0].values, [TENANT, ID, expected.goal_ref]);
  for (const call of calls.slice(1)) {
    assert.match(call.sql, /UPDATE public\.lm_money_opportunities/i);
    assert.match(call.sql, /status IN \('DISCOVERED', 'QUALIFYING'\)/i);
    assert.match(call.sql, /status = 'QUALIFIED'/i);
    assert.match(call.sql, /NOT EXISTS \(SELECT 1 FROM updated\)/i);
    assert.deepEqual(call.values, [TENANT, ID, expected.goal_ref, "QUALIFIED"]);
  }
});

test("runtime store heals an already-poisoned title containing a newline on read", async () => {
  const expected = {
    tenant_id: TENANT,
    opportunity_id: ID,
    goal_ref: `intent-entry://${TENANT}/${ID}`,
  };
  const store = createMoneyPrinterRuntimeStore({
    query: async () => ({ rows: [{ ...opportunity(), title: "Biohub \n Cell  Tracking" }] }),
  });
  const row = await store.readOpportunity(expected);
  assert.equal(row.title, "Biohub Cell Tracking");
});

test("runtime store rejects non-QUALIFIED or non-exact opportunity qualification", async () => {
  const expected = {
    tenant_id: TENANT,
    opportunity_id: ID,
    goal_ref: `intent-entry://${TENANT}/${ID}`,
  };
  const rejectedRows = [
    [],
    [opportunity(), opportunity()],
    [{ ...opportunity(), uid: "tenant-b" }],
    [{ ...opportunity(), opportunity_id: "b".repeat(64) }],
    [{ ...opportunity(), goal_ref: `intent-entry://${TENANT}/${"b".repeat(64)}` }],
  ];
  for (const rows of rejectedRows) {
    const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows }) });
    await assert.rejects(store.readOpportunity(expected), /readback/);
    await assert.rejects(store.updateOpportunity(expected, "QUALIFIED"), /readback/);
  }
  const store = createMoneyPrinterRuntimeStore({ query: async () => ({ rows: [opportunity()] }) });
  await assert.rejects(store.updateOpportunity(expected, "DISCOVERED"), /status/);
});

test("runtime store reads at most one full opportunity by tenant-scoped canonical source URL", async () => {
  const calls = [];
  const persisted = buildOpportunity({
    tenantId: TENANT, sourceUrl: "https://public.example/opportunity", title: "Public opportunity",
    goalStatement: "Complete it.", valueMinor: "50000", currency: "JPY", observedAt: NOW,
  });
  const store = createMoneyPrinterRuntimeStore({
    query: async (sql, values) => {
      calls.push({ sql, values });
      return { rows: [persisted] };
    },
  });
  assert.deepEqual(await store.readOpportunityBySource({
    tenant_id: TENANT, source_url: "https://public.example/opportunity",
  }), persisted);
  assert.match(calls[0].sql, /SELECT uid, opportunity_id, source_url, title, goal_statement, value_minor, currency, status, goal_ref, observed_at/i);
  assert.match(calls[0].sql, /WHERE uid = \$1 AND source_url = \$2/i);
  assert.deepEqual(calls[0].values, [TENANT, "https://public.example/opportunity"]);

  for (const rows of [[persisted, persisted], [{ ...persisted, uid: "tenant-b" }], [{ ...persisted, status: null }]]) {
    const invalid = createMoneyPrinterRuntimeStore({ query: async () => ({ rows }) });
    await assert.rejects(invalid.readOpportunityBySource({ tenant_id: TENANT, source_url: "https://public.example/opportunity" }), /readback/);
  }
});

test("runtime store source lookup normalizes a Postgres Date and rejects an invalid timestamp", async () => {
  const persisted = buildOpportunity({
    tenantId: TENANT, sourceUrl: "https://public.example/date", title: "Public opportunity",
    goalStatement: "Complete it.", valueMinor: "50000", currency: "JPY", observedAt: NOW,
  });
  const store = createMoneyPrinterRuntimeStore({
    query: async () => ({ rows: [{ ...persisted, observed_at: new Date(NOW) }] }),
  });
  assert.deepEqual(await store.readOpportunityBySource({
    tenant_id: TENANT, source_url: "https://public.example/date",
  }), persisted);
  const invalid = createMoneyPrinterRuntimeStore({
    query: async () => ({ rows: [{ ...persisted, observed_at: new Date("invalid") }] }),
  });
  await assert.rejects(invalid.readOpportunityBySource({
    tenant_id: TENANT, source_url: "https://public.example/date",
  }), /readback|observed|time/i);
});
