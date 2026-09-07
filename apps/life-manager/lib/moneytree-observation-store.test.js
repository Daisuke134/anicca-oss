"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  buildMoneytreeObservation, createMoneytreeObservationStore,
} = require("./moneytree-observation-store.js");

const read = (tool, digest) => ({
  provider: "moneytree", mcp_server: "codex_apps", tool,
  retrieved_at: "2026-09-07T08:00:00.000Z", payload_sha256: digest.repeat(64),
});

test("stores a private immutable receipt binding both authenticated reads to normalized rows", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "moneytree-observation-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = createMoneytreeObservationStore({ directoryPath: root });
  const observation = buildMoneytreeObservation({
    accounts: [{ id: "account-1", balance_jpy: 42 }], transactions: [],
    accountRead: read("moneytree.show-accounts", "a"),
    transactionRead: read("moneytree.show-transactions", "b"),
    observedAt: "2026-09-07T08:01:00.000Z",
  });
  assert.equal(store.record(observation), observation.evidence_ref);
  assert.equal(store.record(observation), observation.evidence_ref);
  const [name] = fs.readdirSync(root);
  assert.match(name, /^[a-f0-9]{64}\.json$/);
  assert.equal(fs.statSync(path.join(root, name)).mode & 0o777, 0o600);
  const saved = JSON.parse(fs.readFileSync(path.join(root, name), "utf8"));
  assert.equal(saved.provider, "moneytree");
  assert.equal(saved.account_count, 1);
  assert.equal(Object.hasOwn(saved, "accounts"), false);
});

test("rejects missing or forged connector provenance", () => {
  assert.throws(() => buildMoneytreeObservation({
    accounts: [], transactions: [], accountRead: null,
    transactionRead: read("moneytree.show-transactions", "b"),
    observedAt: "2026-09-07T08:01:00.000Z",
  }), /provenance/);
});
