"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildAgentEconomyStartJob,
  createAgentEconomyCloudLoopAdapter,
} = require("./agent-economy-cloud-adapter.js");

const ADDRESS = `0x${"a".repeat(40)}`;

test("Cloud adapter verifies shared identity refs and emits a secret-free wake receipt", async () => {
  const canonical = buildAgentEconomyStartJob({
    tenantId: "tenant-a", citizenId: "primary", instanceId: "cloud",
  });
  const job = { ...canonical,
    input_refs: Object.fromEntries(Object.entries(canonical.input_refs).reverse()),
    available_at: "2026-09-11T00:00:00.000Z" };
  const adapter = createAgentEconomyCloudLoopAdapter({
    citizenStore: {
      async readPublic(tenantId) {
        assert.equal(tenantId, "tenant-a");
        return { schema_version: 1, record_type: "citizen_identity", tenant_id: tenantId,
          citizen_id: "primary", instance_id: "cloud",
          wallet: { chain: "eip155:8453", address: ADDRESS } };
      },
    },
    runSharedWake: async () => ({ wake_id: "wake-1", kind: "idle", slot: null, profitable: false }),
    now: () => "2026-09-11T00:00:00.000Z",
  });
  const execution = await adapter.execute(job);
  assert.deepEqual(execution.receipt, {
    schema_version: 1,
    kind: "agent_economy_wake",
    status: "completed",
    tenant_id: "tenant-a",
    identity_ref: "citizen://tenant-a/primary",
    instance_ref: "citizen-instance://tenant-a/primary/cloud",
    wallet_address: ADDRESS,
    cycle: 0,
    wake: { wake_id: "wake-1", kind: "idle", slot: null, profitable: false },
    next_job_ref: null,
    completed_at: "2026-09-11T00:00:00.000Z",
  });
  assert.doesNotMatch(JSON.stringify(execution.receipt), /private|cipher|mnemonic|seed|secret|signer_ref/i);
});

test("Cloud start adapter fails closed on mismatched refs", async () => {
  const job = buildAgentEconomyStartJob({ tenantId: "tenant-a", citizenId: "primary", instanceId: "cloud" });
  const adapter = createAgentEconomyCloudLoopAdapter({
    citizenStore: { readPublic: async () => ({ schema_version: 1, record_type: "citizen_identity",
      tenant_id: "tenant-a", citizen_id: "other", instance_id: "cloud",
      wallet: { chain: "eip155:8453", address: ADDRESS } }) },
  });
  await assert.rejects(() => adapter.execute(job), /binding|reference/i);
});

test("Cloud wake uses the shared economy policy and plans exactly one deterministic next wake", async () => {
  const job = { ...buildAgentEconomyStartJob({ tenantId: "tenant-a", citizenId: "primary", instanceId: "cloud" }),
    available_at: "2026-09-11T00:00:00.000Z" };
  const adapter = createAgentEconomyCloudLoopAdapter({
    citizenStore: { readPublic: async () => ({ schema_version: 1, record_type: "citizen_identity",
      tenant_id: "tenant-a", citizen_id: "primary", instance_id: "cloud",
      wallet: { chain: "eip155:8453", address: ADDRESS } }) },
    runSharedWake: async (identity) => {
      assert.equal(identity.tenant_id, "tenant-a");
      return { wake_id: "wake-2", kind: "acted", slot: "earn", profitable: true };
    },
    now: () => "2026-09-11T00:00:01.000Z",
  });
  const result = await adapter.execute(job);
  assert.equal(result.continuation.job.input_refs.cycle_ref, "agent-economy-cycle://tenant-a/1");
  assert.equal(result.continuation.availableAt, "2026-09-11T00:05:00.000Z");
  assert.equal(result.receipt.wake.profitable, true);
  assert.equal(result.receipt.next_job_ref, null);
});

test("Cloud wake does not plan another job when the shared loop fails", async () => {
  const job = { ...buildAgentEconomyStartJob({ tenantId: "tenant-a", citizenId: "primary", instanceId: "cloud" }),
    available_at: "2026-09-11T00:00:00.000Z" };
  const adapter = createAgentEconomyCloudLoopAdapter({
    citizenStore: { readPublic: async () => ({ schema_version: 1, record_type: "citizen_identity",
      tenant_id: "tenant-a", citizen_id: "primary", instance_id: "cloud",
      wallet: { chain: "eip155:8453", address: ADDRESS } }) },
    runSharedWake: async () => { throw new Error("wake failed"); },
  });
  await assert.rejects(() => adapter.execute(job), /wake failed/);
});

test("maximum-length tenant uses a bounded hashed job id", () => {
  const job = buildAgentEconomyStartJob({ tenantId: `t${"a".repeat(127)}`, citizenId: "primary", instanceId: "cloud" });
  assert.ok(job.job_id.length <= 200);
  assert.equal(job.effect_class, "money");
  assert.equal(job.max_attempts, 1);
});
