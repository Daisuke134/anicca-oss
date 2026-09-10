"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  createCloudCitizenStore,
  provisionCloudCitizen,
  readCloudCitizenSigner,
} = require("./cloud-citizen-store.js");
const { deriveAddress } = require("./agent-wallet.js");

const KEY = Buffer.alloc(32, 7).toString("base64");

function memoryDatabase() {
  const rows = new Map();
  const privateRows = new Map();
  return {
    rows,
    async query(sql, params) {
      if (/provision_lm_cloud_citizen/i.test(sql)) {
        const [tenantId, citizenId, instanceId, address, ciphertext, iv, tag] = params;
        let row = rows.get(tenantId);
        let created = false;
        if (!row) {
          row = { tenant_id: tenantId, citizen_id: citizenId, instance_id: instanceId,
            wallet_address: address, created: true };
          rows.set(tenantId, row);
          privateRows.set(tenantId, { ciphertext, iv, tag });
          created = true;
        }
        return { rows: [{ ...row, created }] };
      }
      if (/private\.lm_cloud_citizen_signers/i.test(sql)) {
        const row = privateRows.get(params[0]);
        return { rows: row ? [row] : [] };
      }
      if (/FROM public\.lm_cloud_citizens/i.test(sql)) {
        const row = rows.get(params[0]);
        return { rows: row ? [row] : [] };
      }
      throw new Error("unexpected query");
    },
  };
}

test("provisioning converges on one public identity per tenant and isolates tenants", async () => {
  const db = memoryDatabase();
  const sameTenant = await Promise.all(Array.from({ length: 8 }, () =>
    provisionCloudCitizen({ tenantId: "tenant-a" }, { query: db.query, encryptionKey: KEY })));
  assert.equal(db.rows.size, 1);
  assert.equal(new Set(sameTenant.map((result) => result.identity.wallet.address)).size, 1);
  assert.equal(sameTenant.filter((result) => result.created).length, 1);

  const other = await provisionCloudCitizen(
    { tenantId: "tenant-b" }, { query: db.query, encryptionKey: KEY },
  );
  assert.notEqual(other.identity.wallet.address, sameTenant[0].identity.wallet.address);
});

test("factory uses the explicit runtime database even when Supabase is configured", async () => {
  const db = memoryDatabase();
  const store = createCloudCitizenStore({
    query: db.query,
    supaUrl: "https://database.example",
    supaKey: "service-secret",
    encryptionKey: KEY,
    fetch: async () => { throw new Error("Supabase must not be called"); },
  });
  const result = await store.provision("tenant-a");
  assert.equal(result.created, true);
  assert.equal(result.identity.tenant_id, "tenant-a");
  assert.equal("privateKey" in result, false);
  assert.deepEqual(await store.readPublic("tenant-a"), result.identity);
});

test("factory can provision through the Supabase service RPC without exposing plaintext", async () => {
  let request;
  const store = createCloudCitizenStore({
    supaUrl: "https://database.example/",
    supaKey: "service-secret",
    encryptionKey: KEY,
    fetch: async (url, init) => {
      request = { url, init, body: JSON.parse(init.body) };
      return { ok: true, async json() { return [{
        tenant_id: "tenant-a", citizen_id: "citizen-a", instance_id: "instance-a",
        wallet_address: request.body.p_wallet_address, created: true,
      }]; } };
    },
  });
  const result = await store.provision("tenant-a");
  assert.equal(result.created, true);
  assert.match(request.url, /\/rest\/v1\/rpc\/provision_lm_cloud_citizen$/);
  assert.match(request.body.p_ciphertext, /^\\x[0-9a-f]+$/);
  assert.equal(JSON.stringify(request.body).includes("privateKey"), false);
  assert.equal(request.init.headers.Authorization, "Bearer service-secret");
});

test("readPublic selects only the public projection through Supabase", async () => {
  let request;
  const store = createCloudCitizenStore({
    supaUrl: "https://database.example",
    supaKey: "service-secret",
    fetch: async (url, init) => {
      request = { url, init };
      return { ok: true, async json() { return [{
        tenant_id: "tenant-a", citizen_id: "citizen-a", instance_id: "instance-a",
        wallet_address: "0x1111111111111111111111111111111111111111",
      }]; } };
    },
  });
  const identity = await store.readPublic("tenant-a");
  assert.equal(identity.wallet.address, "0x1111111111111111111111111111111111111111");
  assert.match(request.url, /select=tenant_id,citizen_id,instance_id,wallet_address/);
  assert.doesNotMatch(request.url, /cipher|private|signer/i);
  assert.equal(JSON.stringify(identity).includes("privateKey"), false);
});

test("private signer decrypts only at the private boundary and matches public identity", async () => {
  const db = memoryDatabase();
  const provisioned = await provisionCloudCitizen(
    { tenantId: "tenant-a" }, { query: db.query, encryptionKey: KEY },
  );
  const signer = await readCloudCitizenSigner(
    provisioned.identity, { query: db.query, encryptionKey: KEY },
  );
  assert.equal(deriveAddress(signer.privateKey).toLowerCase(), provisioned.identity.wallet.address);
  assert.equal(JSON.stringify(provisioned).includes(signer.privateKey), false);
  assert.deepEqual(Object.keys(provisioned.identity).sort(),
    ["citizen_id", "instance_id", "record_type", "schema_version", "tenant_id", "wallet"]);
});

test("encryption uses identity binding as AAD and fails closed for another identity", async () => {
  const db = memoryDatabase();
  const first = await provisionCloudCitizen(
    { tenantId: "tenant-a" }, { query: db.query, encryptionKey: KEY },
  );
  await assert.rejects(readCloudCitizenSigner({
    ...first.identity,
    instance_id: "instance-tampered",
  }, { query: db.query, encryptionKey: KEY }), /signer unavailable/i);
});

test("migration keeps signer material private and serializes tenant provisioning", () => {
  const sql = fs.readFileSync(path.join(
    __dirname, "../migrations/2026-09-11-lm-cloud-citizens.sql",
  ), "utf8");
  assert.match(sql, /CREATE SCHEMA IF NOT EXISTS private/i);
  assert.match(sql, /pg_advisory_xact_lock\s*\(\s*hashtextextended\(p_tenant_id/i);
  assert.match(sql, /UNIQUE\s*\(tenant_id\)/i);
  assert.match(sql, /REVOKE ALL ON (?:SCHEMA private|TABLE private\.lm_cloud_citizen_signers)[\s\S]*FROM PUBLIC, anon, authenticated/i);
  assert.match(sql, /GRANT EXECUTE ON FUNCTION public\.provision_lm_cloud_citizen[\s\S]*TO service_role/i);
  assert.match(sql, /complete_lm_runtime_job_and_enqueue/i);
  assert.match(sql, /SELECT \* INTO completed FROM public\.complete_lm_runtime_job/i);
  assert.doesNotMatch(sql, /GRANT[^;]+private\.lm_cloud_citizen_signers[^;]+(?:anon|authenticated)/i);
});
