"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { sealRuntimeBundle } = require("../lib/investment-runtime-state-store.js");

const FILES = Object.freeze([
  "control.json", "risk-day.json", "receipts.jsonl", "live-owned-position.json",
  "telegram-outbox.sqlite3", "telegram-latest.json",
]);
const REQUIRED = Object.freeze(["risk-day.json", "receipts.jsonl", "telegram-outbox.sqlite3"]);

function safeDirectory(directory) {
  const resolved = path.resolve(String(directory || ""));
  const info = fs.lstatSync(resolved);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("investment state directory invalid");
  return resolved;
}

function exportState({ stateDir, accountId, cutover, now = new Date().toISOString() }) {
  const directory = safeDirectory(stateDir);
  const identifier = String(accountId || "").trim();
  if (!identifier || identifier.length > 500) throw new Error("official account id invalid");
  for (const name of REQUIRED) {
    if (!fs.existsSync(path.join(directory, name))) throw new Error(`investment cutover required file missing: ${name}`);
  }
  const files = {};
  for (const name of FILES) {
    const filename = path.join(directory, name);
    if (!fs.existsSync(filename)) continue;
    const info = fs.lstatSync(filename);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error("investment cutover file invalid");
    files[name] = fs.readFileSync(filename).toString("base64");
  }
  if (!files["control.json"]) {
    files["control.json"] = Buffer.from(`${JSON.stringify({ paused: false, killed: false,
      revision: 1, updated_at: now, last_action: "resume" })}\n`).toString("base64");
  }
  return sealRuntimeBundle({
    schema_version: 1, exported_at: now,
    cutover,
    account_binding: {
      provider: "alpaca", endpoint: "live",
      account_id_hash: crypto.createHash("sha256").update(identifier).digest("hex"),
    },
    files,
  });
}

function importState({ stateDir, sealed }) {
  if (!sealed || typeof sealed !== "object") throw new Error("investment cutover bundle invalid");
  const verified = sealRuntimeBundle(sealed.bundle);
  if (verified.digest !== sealed.digest) throw new Error("investment cutover digest mismatch");
  const directory = path.resolve(String(stateDir || ""));
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
  for (const [name, encoded] of Object.entries(verified.bundle.files)) {
    const target = path.join(directory, name);
    const temporary = `${target}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, Buffer.from(encoded, "base64"), { mode: 0o600, flag: "wx" });
    fs.renameSync(temporary, target);
    fs.chmodSync(target, 0o600);
  }
  return verified;
}

module.exports = { exportState, importState };
