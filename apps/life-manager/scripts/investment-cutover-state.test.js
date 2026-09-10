"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { exportState, importState } = require("./investment-cutover-state.js");

test("export/import preserves only the cutover allowlist and seals the live account binding", () => {
  const source = fs.mkdtempSync(path.join(os.tmpdir(), "investment-export-"));
  const target = fs.mkdtempSync(path.join(os.tmpdir(), "investment-import-"));
  fs.writeFileSync(path.join(source, "risk-day.json"), '{"ny_day":"2026-09-10"}\n', { mode: 0o600 });
  fs.writeFileSync(path.join(source, "receipts.jsonl"), '{"client_order_id":"lm-ai-0123456789abcdef01234567","status":"started"}\n', { mode: 0o600 });
  fs.writeFileSync(path.join(source, "control.json"), '{"paused":false,"killed":false}\n', { mode: 0o600 });
  fs.writeFileSync(path.join(source, "observation-latest.json"), '{"must":"not migrate"}\n');
  const sealed = exportState({ stateDir: source, accountId: "official-account-id", now: "2026-09-10T12:00:00.000Z" });
  assert.equal(sealed.bundle.account_binding.endpoint, "live");
  assert.equal(sealed.bundle.account_binding.account_id_hash.length, 64);
  assert.deepEqual(Object.keys(sealed.bundle.files).sort(), ["control.json", "receipts.jsonl", "risk-day.json"]);
  const imported = importState({ stateDir: target, sealed });
  assert.equal(imported.digest, sealed.digest);
  assert.equal(fs.readFileSync(path.join(target, "receipts.jsonl"), "utf8").includes("lm-ai-"), true);
  assert.equal(fs.existsSync(path.join(target, "observation-latest.json")), false);
  assert.equal(fs.statSync(path.join(target, "receipts.jsonl")).mode & 0o777, 0o600);
});

test("export fails closed without receipts, risk baseline, or official account id", () => {
  const source = fs.mkdtempSync(path.join(os.tmpdir(), "investment-export-invalid-"));
  assert.throws(() => exportState({ stateDir: source, accountId: "account" }), /required/);
  fs.writeFileSync(path.join(source, "risk-day.json"), "{}\n");
  fs.writeFileSync(path.join(source, "receipts.jsonl"), "");
  assert.throws(() => exportState({ stateDir: source, accountId: "" }), /account/);
});
