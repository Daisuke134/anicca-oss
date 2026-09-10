"use strict";

const {
  citizenIdentityRefs,
  projectCitizenIdentity,
} = require("../../../runtime/contracts/citizen-identity.cjs");
const { buildAgentEconomyStartJob } = require("./agent-economy-cloud-adapter.js");

function dependencies(value) {
  if (!value || !value.citizenStore || typeof value.citizenStore.provision !== "function"
    || typeof value.enqueueJob !== "function") {
    throw new Error("Agent Economy cloud provisioning dependencies unavailable");
  }
  return value;
}

async function provisionAndStartAgentEconomy(input = {}, injected = {}) {
  const deps = dependencies(injected);
  const tenantId = String(input.tenantId || "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(tenantId)) {
    throw new Error("Agent Economy tenant invalid");
  }
  const provisioned = await deps.citizenStore.provision(tenantId);
  if (!provisioned || typeof provisioned.created !== "boolean") {
    throw new Error("Agent Economy citizen provisioning receipt invalid");
  }
  const identity = projectCitizenIdentity(provisioned.identity);
  if (identity.tenant_id !== tenantId) throw new Error("Agent Economy tenant mismatch");
  citizenIdentityRefs(identity);
  const job = buildAgentEconomyStartJob({
    tenantId,
    citizenId: identity.citizen_id,
    instanceId: identity.instance_id,
  });
  const queued = await deps.enqueueJob({
    jobId: job.job_id,
    tenantId: job.tenant_id,
    loopId: job.loop_id,
    capability: job.capability,
    effectClass: job.effect_class,
    effectKey: job.effect_key,
    inputRefs: job.input_refs,
    maxAttempts: job.max_attempts,
  });
  if (!queued || typeof queued.created !== "boolean") {
    throw new Error("Agent Economy start enqueue receipt invalid");
  }
  return Object.freeze({
    citizen_created: provisioned.created,
    job_created: queued.created,
    identity,
    job_ref: `runtime-job://${encodeURIComponent(tenantId)}/${encodeURIComponent(job.job_id)}`,
  });
}

module.exports = { provisionAndStartAgentEconomy };
