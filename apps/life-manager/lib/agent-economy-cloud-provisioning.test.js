"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { provisionAndStartAgentEconomy } = require("./agent-economy-cloud-provisioning.js");
const { buildAgentEconomyStartJob } = require("./agent-economy-cloud-adapter.js");

const IDENTITY = Object.freeze({
  schema_version: 1,
  record_type: "citizen_identity",
  tenant_id: "tenant-a",
  citizen_id: "primary",
  instance_id: "cloud",
  wallet: { chain: "eip155:8453", address: `0x${"1".repeat(40)}` },
});

test("provisions one citizen and starts one reference-only Agent Economy job", async () => {
  const calls = [];
  const result = await provisionAndStartAgentEconomy({ tenantId: "tenant-a" }, {
    citizenStore: {
      async provision(tenantId) {
        calls.push(["provision", tenantId]);
        return { created: true, identity: IDENTITY };
      },
    },
    async enqueueJob(job) {
      calls.push(["enqueue", job]);
      return { created: true, job };
    },
  });

  assert.equal(calls[0][0], "provision");
  assert.equal(calls[1][0], "enqueue");
  const expected = buildAgentEconomyStartJob({ tenantId: "tenant-a", citizenId: "primary", instanceId: "cloud" });
  assert.deepEqual(calls[1][1], {
    jobId: expected.job_id,
    tenantId: "tenant-a",
    loopId: "agent-economy",
    capability: "agent-economy.start",
    effectClass: "money",
    effectKey: expected.effect_key,
    inputRefs: {
      identity_ref: "citizen://tenant-a/primary",
      instance_ref: "citizen-instance://tenant-a/primary/cloud",
      signer_ref: "citizen-signer://tenant-a/primary/cloud/base",
      cycle_ref: "agent-economy-cycle://tenant-a/0",
    },
    maxAttempts: 1,
  });
  assert.deepEqual(result, {
    citizen_created: true,
    job_created: true,
    identity: IDENTITY,
    job_ref: `runtime-job://tenant-a/${encodeURIComponent(expected.job_id)}`,
  });
  assert.doesNotMatch(JSON.stringify(result), /private|cipher|mnemonic|seed|secret/i);
});

test("replayed or concurrent start converges on the same citizen and job", async () => {
  const jobs = new Set();
  let provisions = 0;
  const deps = {
    citizenStore: {
      async provision() {
        provisions += 1;
        return { created: provisions === 1, identity: IDENTITY };
      },
    },
    async enqueueJob(job) {
      const created = !jobs.has(job.jobId);
      jobs.add(job.jobId);
      return { created, job };
    },
  };
  const [first, replay] = await Promise.all([
    provisionAndStartAgentEconomy({ tenantId: "tenant-a" }, deps),
    provisionAndStartAgentEconomy({ tenantId: "tenant-a" }, deps),
  ]);

  assert.equal(jobs.size, 1);
  assert.equal(first.identity.wallet.address, replay.identity.wallet.address);
  assert.equal(Number(first.citizen_created) + Number(replay.citizen_created), 1);
  assert.equal(Number(first.job_created) + Number(replay.job_created), 1);
});

test("rejects a cross-tenant identity before enqueue", async () => {
  let enqueues = 0;
  await assert.rejects(() => provisionAndStartAgentEconomy({ tenantId: "tenant-b" }, {
    citizenStore: { provision: async () => ({ created: true, identity: IDENTITY }) },
    enqueueJob: async () => { enqueues += 1; },
  }), /tenant/i);
  assert.equal(enqueues, 0);
});
