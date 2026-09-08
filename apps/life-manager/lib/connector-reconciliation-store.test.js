"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { createConnectorReconciliationStore } = require("./connector-reconciliation-store.js");

function candidate(id = "400028") { return { provider: "connpass", event_ref: `connpass-event://event/${id}`, canonical_url: `https://example.connpass.com/event/${id}/`, title: "AI Builders", starts_at: "2026-09-12T10:00:00.000Z", ends_at: "2026-09-12T11:00:00.000Z", venue_address: "Tokyo" }; }

test("persists one private candidate until evidence completion removes it", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-reconcile-"));
  const file = path.join(root, "pending.json");
  try {
    const store = createConnectorReconciliationStore({ path: file });
    store.save(candidate(), "2026-09-08T19:13:34.232Z");
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.equal(store.list("connpass").length, 1);
    assert.equal(store.remove("connpass", candidate().event_ref), true);
    assert.deepEqual(store.list("connpass"), []);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("replaces the same provider event and rejects tampered state", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-reconcile-"));
  const file = path.join(root, "pending.json");
  try {
    const store = createConnectorReconciliationStore({ path: file });
    store.save(candidate(), "2026-09-08T19:00:00.000Z");
    store.save({ ...candidate(), title: "Updated AI Builders" }, "2026-09-08T19:01:00.000Z");
    assert.equal(store.list("connpass").length, 1);
    const document = JSON.parse(fs.readFileSync(file));
    document.entries[0].title = "tampered";
    fs.writeFileSync(file, JSON.stringify(document));
    assert.throws(() => store.list("connpass"), /invalid/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("refuses a 101st distinct candidate without corrupting the readable hundred", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-reconcile-"));
  const file = path.join(root, "pending.json");
  try {
    const store = createConnectorReconciliationStore({ path: file });
    for (let index = 0; index < 100; index += 1) store.save(candidate(String(500000 + index)), "2026-09-08T19:00:00.000Z");
    assert.throws(() => store.save(candidate("999999"), "2026-09-08T19:00:00.000Z"), /invalid/);
    assert.equal(store.list("connpass").length, 100);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
