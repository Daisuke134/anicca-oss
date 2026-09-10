"use strict";

const crypto = require("node:crypto");
const { buildRuntimeJob } = require("./runtime-job-store.js");
const {
  citizenIdentityRefs,
  projectCitizenIdentity,
} = require("../../../runtime/contracts/citizen-identity.cjs");

function sameRefs(left, right) {
  const keys = Object.keys(left || {}).sort();
  const expectedKeys = Object.keys(right || {}).sort();
  return keys.length === expectedKeys.length
    && keys.every((key, index) => key === expectedKeys[index] && left[key] === right[key]);
}

function buildAgentEconomyStartJob({ tenantId, citizenId, instanceId, cycle = 0 }) {
  const identityRef = `citizen://${tenantId}/${citizenId}`;
  const instanceRef = `citizen-instance://${tenantId}/${citizenId}/${instanceId}`;
  const signerRef = `citizen-signer://${tenantId}/${citizenId}/${instanceId}/base`;
  const cycleRef = `agent-economy-cycle://${encodeURIComponent(tenantId)}/${cycle}`;
  const jobId = `agent-economy:${crypto.createHash("sha256").update(`${identityRef}\n${instanceRef}\n${cycleRef}`).digest("hex")}`;
  return buildRuntimeJob({
    jobId,
    tenantId,
    loopId: "agent-economy",
    capability: "agent-economy.start",
    effectClass: "money",
    effectKey: `agent-economy-wake://${crypto.createHash("sha256").update(cycleRef).digest("hex")}`,
    inputRefs: {
      identity_ref: identityRef,
      instance_ref: instanceRef,
      signer_ref: signerRef,
      cycle_ref: cycleRef,
    },
    maxAttempts: 1,
  });
}

function createAgentEconomyCloudLoopAdapter(services = {}) {
  const now = services.now || (() => new Date().toISOString());
  return Object.freeze({
    plan: async (input) => [buildAgentEconomyStartJob(input)],
    async execute(job) {
      if (!services.citizenStore || typeof services.citizenStore.readPublic !== "function") {
        throw new Error("Agent Economy Cloud citizen store unavailable");
      }
      const expected = buildAgentEconomyStartJob({
        tenantId: job.tenant_id,
        citizenId: String(job.input_refs.identity_ref || "").split("/").at(-1),
        instanceId: String(job.input_refs.instance_ref || "").split("/").at(-1),
        cycle: Number(String(job.input_refs.cycle_ref || "").split("/").at(-1)),
      });
      if (!sameRefs(job.input_refs, expected.input_refs)) {
        throw new Error("Agent Economy start reference mismatch");
      }
      const identity = projectCitizenIdentity(await services.citizenStore.readPublic(job.tenant_id));
      const refs = citizenIdentityRefs(identity);
      if (!sameRefs(refs, job.input_refs)) {
        const { cycle_ref: ignored, ...boundRefs } = job.input_refs;
        if (!sameRefs(refs, boundRefs)) {
          throw new Error("Agent Economy citizen binding mismatch");
        }
      }
      const cycle = Number(String(job.input_refs.cycle_ref).split("/").at(-1));
      if (typeof services.isPaused === "function" && await services.isPaused(identity.tenant_id)) {
        return { receipt: {
          schema_version: 1,
          kind: "agent_economy_paused",
          status: "completed",
          tenant_id: identity.tenant_id,
          identity_ref: refs.identity_ref,
          instance_ref: refs.instance_ref,
          wallet_address: identity.wallet.address,
          cycle,
          completed_at: now(),
        } };
      }
      if (typeof services.runSharedWake !== "function") {
        throw new Error("Agent Economy shared wake unavailable");
      }
      const wake = await services.runSharedWake(identity);
      const paused = typeof services.isPaused === "function" && await services.isPaused(identity.tenant_id);
      const baseAt = Date.parse(String(job.available_at || job.created_at || ""));
      if (!Number.isFinite(baseAt)) throw new Error("Agent Economy wake schedule unavailable");
      const next = buildAgentEconomyStartJob({ tenantId: identity.tenant_id,
        citizenId: identity.citizen_id, instanceId: identity.instance_id, cycle: cycle + 1 });
      const continuation = { job: next,
        availableAt: new Date(baseAt + 5 * 60 * 1000).toISOString() };
      return { receipt: {
        schema_version: 1,
        kind: "agent_economy_wake",
        status: "completed",
        tenant_id: identity.tenant_id,
        identity_ref: refs.identity_ref,
        instance_ref: refs.instance_ref,
        wallet_address: identity.wallet.address,
        cycle,
        wake,
        next_job_ref: null,
        completed_at: now(),
      }, ...(paused ? {} : { continuation }) };
    },
    reconcile: async () => ({ status: "not_applicable" }),
    verify: async (receipt) => Boolean(receipt && new Set(["agent_economy_wake", "agent_economy_paused"]).has(receipt.kind) && receipt.status === "completed"),
    report: async (receipt) => receipt,
  });
}

module.exports = { buildAgentEconomyStartJob, createAgentEconomyCloudLoopAdapter };
