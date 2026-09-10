#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { generatePrivateKey, privateKeyToAccount } = require("viem/accounts");
const {
  createCitizenIdentity, projectCitizenIdentity, assertSignerAddress,
} = require("./contracts/citizen-identity.cjs");

function writePrivateJsonAtomic(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  fs.chmodSync(path.dirname(target), 0o700);
  const temporary = `${target}.tmp-${process.pid}-${crypto.randomUUID()}`;
  const handle = fs.openSync(temporary, "wx", 0o600);
  try {
    fs.writeFileSync(handle, `${JSON.stringify(value, null, 2)}\n`);
    fs.fsyncSync(handle);
  } finally {
    fs.closeSync(handle);
  }
  fs.renameSync(temporary, target);
  fs.chmodSync(target, 0o600);
}

function readJson(target, label) {
  try {
    return JSON.parse(fs.readFileSync(target, "utf8"));
  } catch {
    throw new Error(`${label} is unreadable or invalid: ${target}`);
  }
}

function requirePrivateRegularFile(target, label) {
  const stat = fs.lstatSync(target);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`${label} must be a regular file`);
  fs.chmodSync(target, 0o600);
}

function existsNoFollow(target) {
  try {
    fs.lstatSync(target);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

function acquireLock(lockPath) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      fs.mkdirSync(lockPath, { mode: 0o700 });
      return;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
    }
  }
  throw new Error(`citizen bootstrap lock is busy: ${lockPath}`);
}

function bootstrapLocalCitizen(runtimeHome) {
  if (!path.isAbsolute(runtimeHome)) throw new Error("LIFE_MANAGER_HOME must be absolute");
  const economyRoot = path.join(runtimeHome, "agent-economy");
  const instanceHome = path.join(economyRoot, "instance");
  const walletPath = path.join(instanceHome, ".automaton", "wallet.json");
  const identityPath = path.join(instanceHome, "identity", "citizen.json");
  const lockPath = path.join(economyRoot, ".citizen-bootstrap.lock");
  fs.mkdirSync(economyRoot, { recursive: true, mode: 0o700 });
  fs.chmodSync(economyRoot, 0o700);
  acquireLock(lockPath);
  try {
    const walletExists = existsNoFollow(walletPath);
    const identityExists = existsNoFollow(identityPath);
    let identity;
    if (identityExists) {
      requirePrivateRegularFile(identityPath, "citizen identity");
      identity = projectCitizenIdentity(readJson(identityPath, "citizen identity"));
      if (!walletExists) throw new Error("citizen wallet missing for existing identity");
    }

    let wallet;
    if (walletExists) {
      requirePrivateRegularFile(walletPath, "citizen wallet");
      wallet = readJson(walletPath, "citizen wallet");
      if (typeof wallet.privateKey !== "string") throw new Error("citizen wallet private key missing");
      const derived = privateKeyToAccount(wallet.privateKey).address;
      if (typeof wallet.address !== "string" || wallet.address.toLowerCase() !== derived.toLowerCase()) {
        throw new Error("citizen wallet address does not match its signer");
      }
    } else {
      const privateKey = generatePrivateKey();
      wallet = { address: privateKeyToAccount(privateKey).address, privateKey };
    }

    if (identityExists) {
      assertSignerAddress(identity, wallet.address);
    } else {
      const suffix = crypto.randomUUID();
      identity = createCitizenIdentity({
        tenantId: process.env.LIFE_MANAGER_TENANT_ID || "local",
        citizenId: `citizen-${suffix}`,
        instanceId: `instance-${suffix}`,
        walletAddress: wallet.address,
      });
    }
    if (!walletExists) writePrivateJsonAtomic(walletPath, wallet);
    if (!identityExists) writePrivateJsonAtomic(identityPath, identity);
    return { created: true, identityPath, walletPath, identity };
  } finally {
    fs.rmSync(lockPath, { recursive: true });
  }
}

if (require.main === module) {
  try {
    const runtimeHome = process.argv[2] || process.env.LIFE_MANAGER_HOME;
    if (!runtimeHome) throw new Error("LIFE_MANAGER_HOME is required");
    const result = bootstrapLocalCitizen(runtimeHome);
    process.stdout.write(JSON.stringify({
      ok: true,
      citizen_id: result.identity.citizen_id,
      instance_id: result.identity.instance_id,
      wallet_address: result.identity.wallet.address,
    }));
  } catch (error) {
    process.stderr.write(`local citizen bootstrap failed: ${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { bootstrapLocalCitizen };
