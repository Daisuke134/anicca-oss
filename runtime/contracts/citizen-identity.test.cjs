"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createCitizenIdentity, projectCitizenIdentity, citizenIdentityRefs,
  assertCitizenBinding, assertCitizenCollection, assertSignerAddress,
} = require("./citizen-identity.cjs");

const ADDRESS = `0x${"a".repeat(40)}`;
const ZERO_ADDRESS = `0x${"0".repeat(40)}`;

function identity(overrides = {}) {
  return createCitizenIdentity({
    tenantId: "tenant-1", citizenId: "citizen-1", instanceId: "instance-1",
    walletAddress: ADDRESS, ...overrides,
  });
}

test("the host-neutral record survives serialization and derives stable references", () => {
  const original = identity();
  const restored = projectCitizenIdentity(JSON.parse(JSON.stringify(original)));
  assert.deepEqual(restored, original);
  assert.deepEqual(citizenIdentityRefs(original), {
    identity_ref: "citizen://tenant-1/citizen-1",
    instance_ref: "citizen-instance://tenant-1/citizen-1/instance-1",
    signer_ref: "citizen-signer://tenant-1/citizen-1/instance-1/base",
  });
});

test("a sibling tenant, citizen, instance, or wallet cannot reuse the binding", () => {
  const expected = identity();
  for (const observed of [
    identity({ tenantId: "tenant-2" }), identity({ citizenId: "citizen-2" }),
    identity({ instanceId: "instance-2" }), identity({ walletAddress: `0x${"b".repeat(40)}` }),
  ]) assert.throws(() => assertCitizenBinding(expected, observed), /binding invalid/);
});

test("human, Franklin, ambient-secret, and legacy-home fallbacks are not contract fields", () => {
  const base = identity();
  for (const extra of [
    { owner_wallet: ADDRESS }, { franklin_wallet: ADDRESS },
    { private_key: `0x${"c".repeat(64)}` }, { legacy_home: "/home/example/.blockrun" },
  ]) assert.throws(() => projectCitizenIdentity({ ...base, ...extra }), /invalid/);
});

test("collection and signer checks prevent shared wallets and signer substitution", () => {
  const base = identity();
  assert.throws(() => assertCitizenCollection([base, identity({ citizenId: "citizen-2" })]), /duplicate_wallet invalid/);
  assert.throws(() => assertCitizenCollection([base, base]), /duplicate_identity invalid/);
  assert.doesNotThrow(() => assertSignerAddress(base, ADDRESS.toUpperCase().replace("0X", "0x")));
  assert.throws(() => assertSignerAddress(base, `0x${"b".repeat(40)}`), /signer_address invalid/);
  assert.throws(() => projectCitizenIdentity({
    ...base, wallet: { ...base.wallet, source: "environment" },
  }), /wallet.source invalid/);
});

test("zero address is never a ready citizen wallet", () => {
  assert.throws(() => identity({ walletAddress: ZERO_ADDRESS }), /wallet invalid/);
});
