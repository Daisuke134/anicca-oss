"use strict";

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const BASE_ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const ZERO_ADDRESS = `0x${"0".repeat(40)}`;
const EXACT_KEYS = new Set([
  "schema_version", "record_type", "tenant_id", "citizen_id", "instance_id",
  "wallet",
]);
const EXACT_WALLET_KEYS = new Set(["chain", "address"]);

function invalid(label) {
  throw new Error(`CitizenIdentity ${label} invalid`);
}

function id(value, label) {
  if (typeof value !== "string" || !ID.test(value)) invalid(label);
  return value;
}

function exactKeys(value, allowed, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid(label);
  for (const key of Object.keys(value)) if (!allowed.has(key)) invalid(`${label}.${key}`);
}

function citizenIdentityRefs(value) {
  const identity = projectCitizenIdentity(value);
  return {
    identity_ref: `citizen://${identity.tenant_id}/${identity.citizen_id}`,
    instance_ref: `citizen-instance://${identity.tenant_id}/${identity.citizen_id}/${identity.instance_id}`,
    signer_ref: `citizen-signer://${identity.tenant_id}/${identity.citizen_id}/${identity.instance_id}/base`,
  };
}

function projectCitizenIdentity(value) {
  exactKeys(value, EXACT_KEYS, "record");
  if (value.schema_version !== 1 || value.record_type !== "citizen_identity") invalid("record");
  const tenantId = id(value.tenant_id, "tenant_id");
  const citizenId = id(value.citizen_id, "citizen_id");
  const instanceId = id(value.instance_id, "instance_id");
  exactKeys(value.wallet, EXACT_WALLET_KEYS, "wallet");
  if (value.wallet.chain !== "eip155:8453" || !BASE_ADDRESS.test(value.wallet.address)
    || value.wallet.address.toLowerCase() === ZERO_ADDRESS) {
    invalid("wallet");
  }
  return {
    schema_version: 1,
    record_type: "citizen_identity",
    tenant_id: tenantId,
    citizen_id: citizenId,
    instance_id: instanceId,
    wallet: {
      chain: "eip155:8453",
      address: value.wallet.address.toLowerCase(),
    },
  };
}

function createCitizenIdentity({ tenantId, citizenId, instanceId, walletAddress }) {
  return projectCitizenIdentity({
    schema_version: 1,
    record_type: "citizen_identity",
    tenant_id: tenantId,
    citizen_id: citizenId,
    instance_id: instanceId,
    wallet: {
      chain: "eip155:8453",
      address: walletAddress,
    },
  });
}

function assertCitizenCollection(values) {
  if (!Array.isArray(values)) invalid("collection");
  const identities = values.map(projectCitizenIdentity);
  const tuples = new Set();
  const wallets = new Set();
  for (const identity of identities) {
    const tuple = `${identity.tenant_id}\u0000${identity.citizen_id}\u0000${identity.instance_id}`;
    if (tuples.has(tuple)) invalid("duplicate_identity");
    if (wallets.has(identity.wallet.address)) invalid("duplicate_wallet");
    tuples.add(tuple);
    wallets.add(identity.wallet.address);
  }
  return identities;
}

function assertSignerAddress(identity, signerAddress) {
  const projected = projectCitizenIdentity(identity);
  if (typeof signerAddress !== "string" || !BASE_ADDRESS.test(signerAddress)
    || signerAddress.toLowerCase() !== projected.wallet.address) invalid("signer_address");
  return projected;
}

function assertCitizenBinding(expected, observed) {
  const left = projectCitizenIdentity(expected);
  const right = projectCitizenIdentity(observed);
  if (left.tenant_id !== right.tenant_id || left.citizen_id !== right.citizen_id
    || left.instance_id !== right.instance_id || left.wallet.address !== right.wallet.address) {
    invalid("binding");
  }
  return right;
}

module.exports = {
  createCitizenIdentity, projectCitizenIdentity, citizenIdentityRefs,
  assertCitizenBinding, assertCitizenCollection, assertSignerAddress,
};
