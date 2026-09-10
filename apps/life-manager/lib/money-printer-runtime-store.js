"use strict";

const { canonicalOpportunityInput, normalizeOpportunityTitle } = require("./money-printer-opportunity.js");

const TENANT_ID = /^[a-z0-9][a-z0-9._-]{0,199}$/;
const OPPORTUNITY_ID = /^[0-9a-f]{64}$/;
const JOB_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/;
const GITHUB_ISSUE_REF = /^github-issue:\/\/Daisuke134\/life-manager-workrooms\/[1-9][0-9]*$/;
const GITHUB_COMMENT_REF = /^github-comment:\/\/Daisuke134\/life-manager-workrooms\/[1-9][0-9]*\/[1-9][0-9]*$/;
const URI_REF = /^[a-z][a-z0-9+.-]{1,31}:\/\/[A-Za-z0-9][A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{0,999}$/;
const EXECUTION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;

function unavailable() { throw new Error("money printer runtime store unavailable"); }

function tenant(value) {
  const uid = String(value && typeof value === "object" ? value.uid : value == null ? "" : value).trim();
  if (!TENANT_ID.test(uid)) throw new Error("money printer runtime store tenant invalid");
  return uid;
}

function oneRow(result, label, uid) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  if (rows.length !== 1 || !rows[0] || typeof rows[0] !== "object" || Array.isArray(rows[0])) {
    throw new Error(`money printer runtime store ${label} readback invalid`);
  }
  const rowUid = rows[0].uid == null ? rows[0].tenant_id : rows[0].uid;
  if (String(rowUid || "") !== uid) throw new Error(`money printer runtime store ${label} readback invalid`);
  return rows[0];
}

function scopedRows(result, uid, label) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  if (rows.some((row) => !row || typeof row !== "object" || Array.isArray(row)
    || String((row.uid == null ? row.tenant_id : row.uid) || "") !== uid)) {
    throw new Error(`money printer runtime store ${label} readback invalid`);
  }
  return rows;
}

function validTimestamp(value) {
  if (value instanceof Date) return Number.isFinite(value.getTime());
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function optionalTimestamp(value) {
  return value == null || validTimestamp(value);
}

function requiredTimestamp(value) {
  return validTimestamp(value);
}

function coherentSymphonyDispatch(row, { allowClosedTimestamp = false } = {}) {
  const issue = row.issue_ref;
  const result = row.result_ref;
  const hash = row.result_hash;
  const payload = row.result_payload;
  const failure = row.failure_code;
  const claimed = row.status === "claimed";
  const mirrored = row.status === "mirrored";
  const ready = row.status === "result_ready";
  const consumed = row.status === "consumed";
  if (![claimed, mirrored, ready, consumed].some(Boolean)
    || !requiredTimestamp(row.claimed_at)
    || !optionalTimestamp(row.mirrored_at)
    || !optionalTimestamp(row.result_ready_at)
    || !optionalTimestamp(row.consumed_at)
    || !optionalTimestamp(row.failed_at)
    || !optionalTimestamp(row.issue_closed_at)) return false;
  if (claimed) {
    return issue == null && result == null && hash == null && payload == null && failure == null
      && row.mirrored_at == null && row.result_ready_at == null
      && row.consumed_at == null && row.failed_at == null && row.issue_closed_at == null;
  }
  if (mirrored) {
    return typeof issue === "string" && GITHUB_ISSUE_REF.test(issue)
      && result == null && hash == null && payload == null && failure == null
      && requiredTimestamp(row.mirrored_at) && row.result_ready_at == null
      && row.consumed_at == null && row.failed_at == null && row.issue_closed_at == null;
  }
  const resultPayloadValid = payload && typeof payload === "object" && !Array.isArray(payload)
    && ["completed", "needs_human"].includes(payload.status);
  if (ready) {
    return typeof issue === "string" && GITHUB_ISSUE_REF.test(issue)
      && typeof result === "string" && GITHUB_COMMENT_REF.test(result)
      && typeof hash === "string" && OPPORTUNITY_ID.test(hash)
      && resultPayloadValid && failure == null
      && requiredTimestamp(row.mirrored_at) && requiredTimestamp(row.result_ready_at)
      && row.consumed_at == null && row.failed_at == null && row.issue_closed_at == null;
  }
  return typeof issue === "string" && GITHUB_ISSUE_REF.test(issue)
    && typeof result === "string" && GITHUB_COMMENT_REF.test(result)
    && typeof hash === "string" && OPPORTUNITY_ID.test(hash)
    && resultPayloadValid && failure == null
    && requiredTimestamp(row.mirrored_at) && requiredTimestamp(row.result_ready_at)
    && requiredTimestamp(row.consumed_at) && row.failed_at == null
    && (allowClosedTimestamp || row.issue_closed_at == null);
}

function claimedSymphonyDispatch(result, uid) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  if (rows.length === 0) return null;
  if (rows.length !== 1) throw new Error("money printer runtime store Symphony claim readback invalid");
  const row = rows[0];
  if (!row || typeof row !== "object" || Array.isArray(row)
    || row.tenant_id !== uid || !OPPORTUNITY_ID.test(String(row.dispatch_id || ""))
    || !JOB_ID.test(String(row.job_id || "")) || !Number.isInteger(row.round) || row.round < 1
    || !coherentSymphonyDispatch(row)) {
    throw new Error("money printer runtime store Symphony claim readback invalid");
  }
  return row;
}

function symphonyCloseInput(value) {
  const uid = tenant(value && value.uid);
  const dispatchId = String(value && value.dispatchId || "").trim();
  const issueRef = String(value && value.issueRef || "").trim();
  const resultRef = String(value && value.resultRef || "").trim();
  const resultHash = String(value && value.resultHash || "").trim();
  if (!OPPORTUNITY_ID.test(dispatchId) || !GITHUB_ISSUE_REF.test(issueRef)
    || !GITHUB_COMMENT_REF.test(resultRef) || !OPPORTUNITY_ID.test(resultHash)) {
    throw new Error("money printer runtime store Symphony issue close invalid");
  }
  return Object.freeze({ uid, dispatchId, issueRef, resultRef, resultHash });
}

function closedSymphonyDispatch(result, expected) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  const row = rows.length === 1 ? rows[0] : null;
  if (!row || rows.length !== 1 || typeof row !== "object" || Array.isArray(row)
    || row.tenant_id !== expected.uid || row.dispatch_id !== expected.dispatchId
    || !JOB_ID.test(String(row.job_id || "")) || row.status !== "consumed"
    || row.issue_ref !== expected.issueRef || row.result_ref !== expected.resultRef
    || row.result_hash !== expected.resultHash || !row.result_payload
    || Array.isArray(row.result_payload) || row.failure_code != null
    || !validTimestamp(row.issue_closed_at) || !coherentSymphonyDispatch(row, { allowClosedTimestamp: true })) {
    throw new Error("money printer runtime store Symphony issue close readback invalid");
  }
  return row;
}

function mirroredSymphonyDispatch(result, expected) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  const row = rows.length === 1 ? rows[0] : null;
  if (!row || rows.length !== 1 || typeof row !== "object" || Array.isArray(row)
    || row.tenant_id !== expected.uid || row.dispatch_id !== expected.dispatchId
    || !JOB_ID.test(String(row.job_id || "")) || !Number.isInteger(row.round) || row.round < 1
    || row.status !== "mirrored" || row.issue_ref !== expected.issueRef
    || row.result_ref != null || row.result_hash != null || row.result_payload != null
    || row.failure_code != null) {
    throw new Error("money printer runtime store Symphony issue readback invalid");
  }
  return row;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function resultReadySymphonyDispatch(result, expected) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  const row = rows.length === 1 ? rows[0] : null;
  if (!row || rows.length !== 1 || typeof row !== "object" || Array.isArray(row)
    || row.tenant_id !== expected.uid || row.dispatch_id !== expected.dispatchId
    || row.job_id !== expected.payload.job_id || !["result_ready", "consumed"].includes(row.status)
    || row.result_ref !== expected.resultRef || row.result_hash !== expected.resultHash
    || stableJson(row.result_payload) !== stableJson(expected.payload) || row.failure_code != null) {
    throw new Error("money printer runtime store Symphony result readback invalid");
  }
  return row;
}

function consumedSymphonyDispatch(result, uid, dispatchId) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  const row = rows.length === 1 ? rows[0] : null;
  if (!row || rows.length !== 1 || typeof row !== "object" || Array.isArray(row)
    || row.tenant_id !== uid || row.dispatch_id !== dispatchId
    || !JOB_ID.test(String(row.job_id || "")) || row.status !== "consumed"
    || !row.result_payload || row.result_payload.status !== "completed"
    || row.failure_code != null) {
    throw new Error("money printer runtime store Symphony completion readback invalid");
  }
  return row;
}

function symphonyHumanTask(result, uid) {
  const row = oneRow(result, "Symphony human task", uid);
  if (!OPPORTUNITY_ID.test(String(row.task_id || ""))
    || !JOB_ID.test(String(row.job_id || "")) || row.status !== "open"
    || !Number.isInteger(row.version) || row.version < 1
    || typeof row.resume_ref !== "string" || !URI_REF.test(row.resume_ref)
    || !/^human-boundary:\/\/sha256\/[0-9a-f]{64}$/.test(String(row.human_boundary_ref || ""))) {
    throw new Error("money printer runtime store Symphony human task readback invalid");
  }
  return row;
}

function symphonyResultInput(value) {
  const uid = tenant(value && value.uid);
  const dispatchId = String(value && value.dispatchId || "").trim();
  const resultRef = String(value && value.resultRef || "").trim();
  const resultHash = String(value && value.resultHash || "").trim();
  const payload = value && value.payload;
  const base = ["artifact_refs", "dispatch_id", "execution_id", "job_id", "protocol", "status", "tenant_id"];
  const human = [...base, "question", "reason_code", "required_format"].sort();
  const keys = payload && typeof payload === "object" && !Array.isArray(payload) ? Object.keys(payload).sort() : [];
  if (!OPPORTUNITY_ID.test(dispatchId) || !GITHUB_COMMENT_REF.test(resultRef)
    || !OPPORTUNITY_ID.test(resultHash) || !payload || Array.isArray(payload)
    || payload.protocol !== "LM_RESULT_V1" || payload.tenant_id !== uid
    || payload.dispatch_id !== dispatchId || typeof payload.job_id !== "string" || !JOB_ID.test(payload.job_id)
    || !new Set(["completed", "needs_human"]).has(payload.status)
    || typeof payload.execution_id !== "string" || !EXECUTION_ID.test(payload.execution_id)
    || !Array.isArray(payload.artifact_refs) || payload.artifact_refs.length > 100
    || payload.artifact_refs.some((ref) => typeof ref !== "string" || !URI_REF.test(ref))
    || (payload.status === "completed" && stableJson(keys) !== stableJson(base.sort()))
    || (payload.status === "needs_human" && (
      stableJson(keys) !== stableJson(human)
      || typeof payload.reason_code !== "string" || !JOB_ID.test(payload.reason_code)
      || typeof payload.question !== "string" || !payload.question.trim() || payload.question.length > 2000
      || !["string", "object"].includes(typeof payload.required_format) || payload.required_format == null
    ))) {
    throw new Error("money printer runtime store Symphony result invalid");
  }
  return Object.freeze({ uid, dispatchId, resultRef, resultHash, payload });
}

function expectedOpportunity(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("money printer runtime store opportunity expected invalid");
  }
  const uid = tenant(value.uid == null ? value.tenant_id : value.uid);
  if (value.uid != null && value.tenant_id != null && String(value.tenant_id).trim() !== uid) {
    throw new Error("money printer runtime store opportunity expected invalid");
  }
  const opportunityId = String(value.opportunity_id || "").trim();
  const goalRef = String(value.goal_ref || "").trim();
  if (!OPPORTUNITY_ID.test(opportunityId) || goalRef !== `intent-entry://${uid}/${opportunityId}`) {
    throw new Error("money printer runtime store opportunity expected invalid");
  }
  return Object.freeze({ uid, opportunityId, goalRef });
}

function expectedOpportunitySource(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("money printer runtime store opportunity source expected invalid");
  }
  const uid = tenant(value.uid == null ? value.tenant_id : value.uid);
  if (value.uid != null && value.tenant_id != null && String(value.tenant_id).trim() !== uid) {
    throw new Error("money printer runtime store opportunity source expected invalid");
  }
  const sourceUrl = String(value.source_url == null ? "" : value.source_url).trim();
  const canonical = canonicalOpportunityInput({
    tenantId: uid,
    sourceUrl,
    title: "source lookup",
    goalStatement: "source lookup",
    valueMinor: "0",
    currency: "USD",
    observedAt: "2026-01-01T00:00:00.000Z",
  });
  return Object.freeze({ uid, sourceUrl: canonical.source_url });
}

function expectedHumanJob(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("money printer runtime store human job expected invalid");
  }
  const uid = tenant(value.uid == null ? value.tenant_id : value.uid);
  const jobId = String(value.job_id || "").trim();
  if (!JOB_ID.test(jobId)) throw new Error("money printer runtime store human job expected invalid");
  return Object.freeze({ uid, jobId });
}

function opportunityRow(result, expected, label) {
  const row = oneRow(result, label, expected.uid);
  if (
    String(row.opportunity_id || "") !== expected.opportunityId
    || String(row.goal_ref || "") !== expected.goalRef
  ) throw new Error(`money printer runtime store ${label} readback invalid`);
  return row.title == null ? row : { ...row, title: normalizeOpportunityTitle(row.title) };
}

function normalizeOpportunityReadback(row) {
  if (row.observed_at instanceof Date) {
    if (!Number.isFinite(row.observed_at.getTime())) throw new Error("money printer runtime store opportunity readback invalid");
    return { ...row, observed_at: row.observed_at.toISOString() };
  }
  if (typeof row.observed_at !== "string" || !Number.isFinite(Date.parse(row.observed_at))) {
    throw new Error("money printer runtime store opportunity readback invalid");
  }
  return row;
}

function sourceOpportunityRow(result, expected) {
  const rows = result && Array.isArray(result.rows) ? result.rows : [];
  if (rows.length === 0) return null;
  if (rows.length !== 1 || !rows[0] || typeof rows[0] !== "object" || Array.isArray(rows[0])) {
    throw new Error("money printer runtime store opportunity source readback invalid");
  }
  const row = normalizeOpportunityReadback(rows[0]);
  const actual = canonicalOpportunityInput({
    tenantId: row.uid == null ? row.tenant_id : row.uid,
    sourceUrl: row.source_url,
    title: row.title,
    goalStatement: row.goal_statement,
    valueMinor: row.value_minor,
    currency: row.currency,
    observedAt: row.observed_at,
  });
  if (
    actual.uid !== expected.uid || actual.source_url !== expected.sourceUrl
    || String(row.opportunity_id || "") !== actual.opportunity_id
    || String(row.goal_ref || "") !== actual.goal_ref
    || typeof row.status !== "string" || !row.status.trim()
  ) throw new Error("money printer runtime store opportunity source readback invalid");
  return row.title === actual.title ? row : { ...row, title: actual.title };
}

function createMoneyPrinterRuntimeStore({ query } = {}) {
  if (typeof query !== "function") unavailable();

  return Object.freeze({
    async claimSymphonyNext() {
      const candidates = (await query(`
        WITH candidates AS (
          SELECT dispatches.tenant_id, min(dispatches.claimed_at) AS ready_at, 0 AS priority
          FROM public.lm_symphony_dispatches dispatches
          WHERE dispatches.status IN ('claimed', 'mirrored', 'result_ready', 'consumed')
            AND dispatches.issue_closed_at IS NULL
          GROUP BY dispatches.tenant_id
          UNION ALL
          SELECT jobs.tenant_id, min(jobs.available_at) AS ready_at, 1 AS priority
          FROM public.lm_runtime_jobs jobs
          WHERE jobs.status = 'queued' AND jobs.capability = 'general-agent.work'
            AND jobs.available_at <= clock_timestamp()
            AND (SELECT count(*) FROM public.lm_symphony_dispatches dispatches
              WHERE dispatches.tenant_id = jobs.tenant_id AND dispatches.issue_closed_at IS NULL) < 2
          GROUP BY jobs.tenant_id
        )
        SELECT tenant_id FROM candidates
        ORDER BY priority, ready_at, tenant_id
        LIMIT 1
      `)).rows;
      if (candidates.length === 0) return null;
      if (candidates.length !== 1) throw new Error("money printer runtime store Symphony claim readback invalid");
      const uid = tenant(candidates[0].tenant_id);
      return claimedSymphonyDispatch(await query(`
        SELECT * FROM public.claim_lm_symphony_job($1)
      `, [uid]), uid);
    },
    async claimSymphony(value) {
      const uid = tenant(value);
      return claimedSymphonyDispatch(await query(`
        SELECT * FROM public.claim_lm_symphony_job($1)
      `, [uid]), uid);
    },
    async acknowledgeSymphonyIssueClosed(value) {
      const expected = symphonyCloseInput(value);
      return closedSymphonyDispatch(await query(`
        SELECT * FROM public.ack_lm_symphony_issue_closed($1, $2, $3, $4, $5)
      `, [expected.uid, expected.dispatchId, expected.issueRef, expected.resultRef, expected.resultHash]), expected);
    },
    async recordSymphonyIssue(value) {
      const uid = tenant(value && value.uid);
      const dispatchId = String(value && value.dispatchId || "").trim();
      const issueRef = String(value && value.issueRef || "").trim();
      if (!OPPORTUNITY_ID.test(dispatchId) || !GITHUB_ISSUE_REF.test(issueRef)) {
        throw new Error("money printer runtime store Symphony issue invalid");
      }
      return mirroredSymphonyDispatch(await query(`
        SELECT * FROM public.record_lm_symphony_issue($1, $2, $3)
      `, [uid, dispatchId, issueRef]), { uid, dispatchId, issueRef });
    },
    async recordSymphonyResult(value) {
      const expected = symphonyResultInput(value);
      return resultReadySymphonyDispatch(await query(`
        SELECT * FROM public.record_lm_symphony_result($1, $2, $3, $4, $5)
      `, [expected.uid, expected.dispatchId, expected.resultRef, expected.resultHash, JSON.stringify(expected.payload)]), expected);
    },
    async consumeSymphonyCompleted(value) {
      const uid = tenant(value && value.uid);
      const dispatchId = String(value && value.dispatchId || "").trim();
      if (!OPPORTUNITY_ID.test(dispatchId)) {
        throw new Error("money printer runtime store Symphony completion invalid");
      }
      return consumedSymphonyDispatch(await query(`
        SELECT * FROM public.consume_lm_symphony_completed($1, $2)
      `, [uid, dispatchId]), uid, dispatchId);
    },
    async consumeSymphonyHumanTask(value) {
      const uid = tenant(value && value.uid);
      const dispatchId = String(value && value.dispatchId || "").trim();
      if (!OPPORTUNITY_ID.test(dispatchId)) {
        throw new Error("money printer runtime store Symphony human task invalid");
      }
      return symphonyHumanTask(await query(`
        SELECT * FROM public.consume_lm_symphony_human_task($1, $2)
      `, [uid, dispatchId]), uid);
    },
    async createOpportunity(canonical) {
      const uid = tenant(canonical && canonical.uid);
      const row = oneRow(await query(`
        SELECT * FROM public.create_lm_money_opportunity($1, $2, $3, $4, $5, $6, $7, $8, $9)
      `, [
        uid, canonical.opportunity_id, canonical.source_url, canonical.title, canonical.goal_statement,
        canonical.value_minor, canonical.currency, canonical.observed_at, canonical.goal_ref,
      ]), "opportunity", uid);
      if (row.opportunity_id !== canonical.opportunity_id) throw new Error("money printer runtime store opportunity readback invalid");
      return normalizeOpportunityReadback(row);
    },
    async readOpportunity(value) {
      const expected = expectedOpportunity(value);
      return opportunityRow(await query(`
        SELECT uid, opportunity_id, source_url, title, goal_statement, value_minor, currency, status, goal_ref, observed_at
        FROM public.lm_money_opportunities
        WHERE uid = $1 AND opportunity_id = $2 AND goal_ref = $3
        LIMIT 2
      `, [expected.uid, expected.opportunityId, expected.goalRef]), expected, "opportunity");
    },
    async readOpportunityBySource(value) {
      const expected = expectedOpportunitySource(value);
      return sourceOpportunityRow(await query(`
        SELECT uid, opportunity_id, source_url, title, goal_statement, value_minor, currency, status, goal_ref, observed_at
        FROM public.lm_money_opportunities
        WHERE uid = $1 AND source_url = $2
        LIMIT 2
      `, [expected.uid, expected.sourceUrl]), expected);
    },
    async updateOpportunity(value, status) {
      const expected = expectedOpportunity(value);
      if (status !== "QUALIFIED") throw new Error("money printer runtime store opportunity status invalid");
      const row = opportunityRow(await query(`
        WITH updated AS (
          UPDATE public.lm_money_opportunities
          SET status = $4, updated_at = clock_timestamp()
          WHERE uid = $1 AND opportunity_id = $2 AND goal_ref = $3
            AND status IN ('DISCOVERED', 'QUALIFYING')
          RETURNING uid, opportunity_id, source_url, title, goal_statement, value_minor, currency, status, goal_ref, observed_at
        )
        SELECT * FROM updated
        UNION ALL
        SELECT uid, opportunity_id, source_url, title, goal_statement, value_minor, currency, status, goal_ref, observed_at
        FROM public.lm_money_opportunities
        WHERE uid = $1 AND opportunity_id = $2 AND goal_ref = $3
          AND status = 'QUALIFIED'
          AND NOT EXISTS (SELECT 1 FROM updated)
      `, [expected.uid, expected.opportunityId, expected.goalRef, status]), expected, "opportunity");
      if (row.status !== status) throw new Error("money printer runtime store opportunity readback invalid");
      return row;
    },
    async createOnce(task) {
      const uid = tenant(task && task.uid);
      const row = oneRow(await query(`
        SELECT * FROM public.create_lm_human_task($1, $2, $3, $4, $5, $6, $7, $8, $9)
      `, [
        uid, task.task_id, task.job_id, task.reason_code, task.question,
        JSON.stringify(task.required_format), task.resume_ref, JSON.stringify(task.context_refs), task.human_boundary_ref,
      ]), "human task", uid);
      if (row.task_id !== task.task_id || row.job_id !== task.job_id || row.status !== "open") {
        throw new Error("money printer runtime store human task readback invalid");
      }
      return row;
    },
    async readAnsweredForJob(value) {
      const expected = expectedHumanJob(value);
      const rows = scopedRows(await query(`
        SELECT uid, job_id, reason_code, answer_ref, human_boundary_ref, version, updated_at
        FROM public.lm_human_tasks
        WHERE uid = $1 AND job_id = $2 AND status = 'answered'
        ORDER BY updated_at ASC, task_id ASC
      `, [expected.uid, expected.jobId]), expected.uid, "answered human tasks");
      return rows.map((row) => {
        if (row.job_id !== expected.jobId
          || !JOB_ID.test(String(row.reason_code || ""))
          || !String(row.answer_ref || "").startsWith(`vault-answer://${expected.uid}/`)
          || !/^human-boundary:\/\/sha256\/[0-9a-f]{64}$/.test(String(row.human_boundary_ref || ""))
          || !Number.isInteger(row.version) || !Number.isFinite(Date.parse(row.updated_at))) {
          throw new Error("money printer runtime store answered human tasks readback invalid");
        }
        return Object.freeze({
          uid: expected.uid, job_id: expected.jobId, reason_code: row.reason_code,
          answer_ref: row.answer_ref, human_boundary_ref: row.human_boundary_ref,
          version: row.version, updated_at: row.updated_at,
        });
      });
    },
    async readNext(scope) {
      const uid = tenant(scope);
      const rows = scopedRows(await query(`
        SELECT uid, task_id, version, question, required_format, reason_code, resume_ref, status, created_at, updated_at
        FROM public.lm_human_tasks
        WHERE uid = $1 AND status = 'open'
        ORDER BY created_at ASC, task_id ASC
        LIMIT 1
      `, [uid]), uid, "human task");
      if (rows.length > 1) throw new Error("money printer runtime store human task readback invalid");
      return rows[0] || null;
    },
    async answerOnce(answer) {
      const uid = tenant(answer && answer.uid);
      const row = oneRow(await query(`
        SELECT * FROM public.answer_lm_human_task($1, $2, $3, $4)
      `, [uid, answer.taskId, answer.version, answer.answerRef]), "human task", uid);
      if (row.task_id !== answer.taskId || row.answer_ref !== answer.answerRef || row.status !== "answered") {
        throw new Error("money printer runtime store human task readback invalid");
      }
      return row;
    },
    async readRuntimeSnapshot(value) {
      const uid = tenant(value);
      const [opportunities, runtimeJobs, humanTasks, receipts] = await Promise.all([
        query(`
          SELECT uid, opportunity_id, source_url, title, value_minor, currency, status, goal_ref, observed_at
          FROM public.lm_money_opportunities WHERE uid = $1
          ORDER BY updated_at DESC, opportunity_id ASC
        `, [uid]),
        query(`
          SELECT tenant_id, job_id, status, created_at, updated_at
          FROM public.lm_runtime_jobs WHERE tenant_id = $1
          ORDER BY updated_at DESC, job_id ASC
        `, [uid]),
        query(`
          SELECT uid, task_id, job_id, reason_code, version, status, created_at, updated_at
          FROM public.lm_human_tasks WHERE uid = $1
          ORDER BY updated_at DESC, task_id ASC
        `, [uid]),
        query(`
          SELECT tenant_id, job_id, attempt, outcome, created_at, receipt
          FROM public.lm_runtime_job_receipts WHERE tenant_id = $1
          ORDER BY created_at DESC, job_id ASC, attempt DESC
        `, [uid]),
      ]);
      return Object.freeze({
        opportunities: scopedRows(opportunities, uid, "opportunities"),
        runtimeJobs: scopedRows(runtimeJobs, uid, "runtime jobs"),
        humanTasks: scopedRows(humanTasks, uid, "human tasks"),
        receipts: scopedRows(receipts, uid, "runtime receipts"),
      });
    },
  });
}

module.exports = { createMoneyPrinterRuntimeStore };
