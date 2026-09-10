"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const { bootstrapLocalCitizen } = require("./bootstrap-local-citizen.cjs");
const execFileAsync = promisify(execFile);

test("clean Local bootstrap creates one isolated citizen and preserves it on replay", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-local-citizen-"));
  const first = bootstrapLocalCitizen(home);
  const wallet = JSON.parse(fs.readFileSync(first.walletPath, "utf8"));
  const identityText = fs.readFileSync(first.identityPath, "utf8");
  const second = bootstrapLocalCitizen(home);

  assert.deepEqual(second.identity, first.identity);
  assert.equal(JSON.parse(fs.readFileSync(first.walletPath, "utf8")).privateKey, wallet.privateKey);
  assert.equal(identityText.includes(wallet.privateKey), false);
  assert.equal(fs.statSync(first.walletPath).mode & 0o777, 0o600);
  assert.equal(fs.statSync(first.identityPath).mode & 0o777, 0o600);
});

test("different Local homes never share a citizen or wallet", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-local-citizens-"));
  const left = bootstrapLocalCitizen(path.join(root, "left"));
  const right = bootstrapLocalCitizen(path.join(root, "right"));
  assert.notEqual(left.identity.citizen_id, right.identity.citizen_id);
  assert.notEqual(left.identity.instance_id, right.identity.instance_id);
  assert.notEqual(left.identity.wallet.address, right.identity.wallet.address);
});

test("existing wallet/identity mismatch fails closed without replacement", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-local-mismatch-"));
  const original = bootstrapLocalCitizen(home);
  const wallet = JSON.parse(fs.readFileSync(original.walletPath, "utf8"));
  const identity = JSON.parse(fs.readFileSync(original.identityPath, "utf8"));
  identity.wallet.address = `0x${"b".repeat(40)}`;
  fs.writeFileSync(original.identityPath, JSON.stringify(identity), { mode: 0o600 });
  assert.throws(() => bootstrapLocalCitizen(home), /signer_address invalid/);
  assert.equal(JSON.parse(fs.readFileSync(original.walletPath, "utf8")).privateKey, wallet.privateKey);
});

test("wallet symlinks are rejected instead of adopting another identity", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-local-symlink-"));
  const source = bootstrapLocalCitizen(path.join(root, "source"));
  const targetHome = path.join(root, "target");
  const targetWallet = path.join(targetHome, "agent-economy/instance/.automaton/wallet.json");
  fs.mkdirSync(path.dirname(targetWallet), { recursive: true });
  fs.symlinkSync(source.walletPath, targetWallet);
  assert.throws(() => bootstrapLocalCitizen(targetHome), /must be a regular file/);
});

test("concurrent installers converge on the same citizen and wallet", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-local-concurrent-"));
  const script = path.join(__dirname, "bootstrap-local-citizen.cjs");
  const [left, right] = await Promise.all([
    execFileAsync(process.execPath, [script, home]),
    execFileAsync(process.execPath, [script, home]),
  ]);
  assert.deepEqual(JSON.parse(left.stdout), JSON.parse(right.stdout));
});

test("existing identity with a missing wallet fails without generating a replacement", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-local-missing-wallet-"));
  const original = bootstrapLocalCitizen(home);
  fs.unlinkSync(original.walletPath);
  assert.throws(() => bootstrapLocalCitizen(home), /wallet missing for existing identity/);
  assert.equal(fs.existsSync(original.walletPath), false);
});
