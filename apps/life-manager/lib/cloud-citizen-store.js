"use strict";

const crypto = require("node:crypto");
const { generateAgentWallet, deriveAddress } = require("./agent-wallet.js");

function citizenContract() {
  return require("../../../runtime/contracts/citizen-identity.cjs");
}

function encryptionKey(value) {
  const source = String(value || process.env.LM_CLOUD_CITIZEN_ENCRYPTION_KEY || "").trim();
  let key;
  if (/^[0-9a-fA-F]{64}$/.test(source)) key = Buffer.from(source, "hex");
  else {
    try { key = Buffer.from(source, "base64"); } catch { key = Buffer.alloc(0); }
  }
  if (key.length !== 32) throw new Error("cloud citizen encryption key unavailable");
  return key;
}

function database(opts) {
  if (typeof opts.query === "function") return opts.query;
  throw new Error("cloud citizen store unavailable");
}

function bytea(value) {
  return `\\x${Buffer.from(value).toString("hex")}`;
}

function supabaseProvision(opts) {
  const base = String(opts.supaUrl || process.env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = String(opts.supaKey || process.env.SUPABASE_SERVICE_ROLE_KEY || "").trim();
  const request = opts.fetch || global.fetch;
  if (!base || !key || typeof request !== "function") return null;
  return async (identity, sealed) => {
    const response = await request(`${base}/rest/v1/rpc/provision_lm_cloud_citizen`, {
      method: "POST",
      headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        p_tenant_id: identity.tenant_id,
        p_citizen_id: identity.citizen_id,
        p_instance_id: identity.instance_id,
        p_wallet_address: identity.wallet.address,
        p_ciphertext: bytea(sealed.ciphertext),
        p_iv: bytea(sealed.iv),
        p_tag: bytea(sealed.tag),
      }),
    });
    if (!response.ok) throw new Error("cloud citizen provisioning failed");
    const rows = await response.json();
    return Array.isArray(rows) ? rows : [];
  };
}

function aad(identity) {
  return Buffer.from(
    `${identity.tenant_id}\0${identity.citizen_id}\0${identity.instance_id}`,
    "utf8",
  );
}

function encryptSigner(privateKey, identity, key) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(aad(identity));
  const ciphertext = Buffer.concat([cipher.update(privateKey, "utf8"), cipher.final()]);
  return { ciphertext, iv, tag: cipher.getAuthTag() };
}

function decryptSigner(row, identity, key) {
  try {
    const decipher = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(row.iv));
    decipher.setAAD(aad(identity));
    decipher.setAuthTag(Buffer.from(row.tag));
    return Buffer.concat([
      decipher.update(Buffer.from(row.ciphertext)), decipher.final(),
    ]).toString("utf8");
  } catch {
    throw new Error("cloud citizen signer unavailable");
  }
}

function identityFromRow(row) {
  return citizenContract().createCitizenIdentity({
    tenantId: row.tenant_id,
    citizenId: row.citizen_id,
    instanceId: row.instance_id,
    walletAddress: row.wallet_address,
  });
}

async function provisionCloudCitizen(input, opts = {}) {
  const tenantId = String(input && input.tenantId || "").trim();
  const wallet = generateAgentWallet(opts.entropySource);
  const identity = citizenContract().createCitizenIdentity({
    tenantId,
    citizenId: `citizen-${crypto.randomUUID()}`,
    instanceId: `instance-${crypto.randomUUID()}`,
    walletAddress: wallet.address,
  });
  const sealed = encryptSigner(wallet.privateKey, identity, encryptionKey(opts.encryptionKey));
  const rpc = supabaseProvision(opts);
  const rows = rpc ? await rpc(identity, sealed) : (await database(opts)(`
      SELECT * FROM public.provision_lm_cloud_citizen($1,$2,$3,$4,$5,$6,$7)
    `, [
      identity.tenant_id, identity.citizen_id, identity.instance_id,
      identity.wallet.address, sealed.ciphertext, sealed.iv, sealed.tag,
    ])).rows;
  if (rows.length !== 1) throw new Error("cloud citizen provisioning failed");
  return Object.freeze({ created: rows[0].created === true, identity: identityFromRow(rows[0]) });
}

function createCloudCitizenStore(opts = {}) {
  return Object.freeze({
    provision(tenantId) {
      return provisionCloudCitizen({ tenantId }, opts);
    },
    async readPublic(tenantId) {
      const id = String(tenantId || "").trim();
      let rows;
      const base = String(opts.supaUrl || process.env.SUPABASE_URL || "").replace(/\/$/, "");
      const key = String(opts.supaKey || process.env.SUPABASE_SERVICE_ROLE_KEY || "").trim();
      const request = opts.fetch || global.fetch;
      if (base && key && typeof request === "function") {
        const response = await request(`${base}/rest/v1/lm_cloud_citizens?tenant_id=eq.${encodeURIComponent(id)}&select=tenant_id,citizen_id,instance_id,wallet_address&limit=1`, {
          headers: { apikey: key, Authorization: `Bearer ${key}` },
        });
        if (!response.ok) throw new Error("cloud citizen read failed");
        rows = await response.json();
      } else {
        rows = (await database(opts)(`
          SELECT tenant_id, citizen_id, instance_id, wallet_address
          FROM public.lm_cloud_citizens WHERE tenant_id = $1 LIMIT 1
        `, [id])).rows;
      }
      if (!Array.isArray(rows) || rows.length !== 1) throw new Error("cloud citizen unavailable");
      return identityFromRow(rows[0]);
    },
    readSigner(identity) {
      return readCloudCitizenSigner(identity, opts);
    },
  });
}

async function readCloudCitizenSigner(identityValue, opts = {}) {
  const identity = citizenContract().projectCitizenIdentity(identityValue);
  const rows = (await database(opts)(`
    SELECT ciphertext, iv, tag
    FROM private.lm_cloud_citizen_signers
    WHERE tenant_id = $1 AND citizen_id = $2 AND instance_id = $3
    LIMIT 1
  `, [identity.tenant_id, identity.citizen_id, identity.instance_id])).rows;
  if (rows.length !== 1) throw new Error("cloud citizen signer unavailable");
  const privateKey = decryptSigner(rows[0], identity, encryptionKey(opts.encryptionKey));
  citizenContract().assertSignerAddress(identity, deriveAddress(privateKey));
  return Object.freeze({ privateKey });
}

module.exports = { createCloudCitizenStore, provisionCloudCitizen, readCloudCitizenSigner };
