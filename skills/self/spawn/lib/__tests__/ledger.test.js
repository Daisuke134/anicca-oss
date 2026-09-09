const { test } = require("node:test");
const assert = require("node:assert");
const os = require("node:os");
const fs = require("node:fs");
const path = require("node:path");
const { appendChild, appendCitizenLifecycleReceipt, readChildren } = require("../ledger");

function tmpFile() {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), "spawn-ledger-")), "children.jsonl");
}

test("readChildren returns [] when the file does not exist", () => {
  const f = path.join(os.tmpdir(), "definitely-missing-" + Date.now(), "children.jsonl");
  assert.deepStrictEqual(readChildren(f), []);
});

test("appendChild then readChildren round-trips one row", () => {
  const f = tmpFile();
  const row = { child_id: "anicca-c001", wallet: "0xCHILD", spawned_ms: 123 };
  appendChild(f, row);
  const rows = readChildren(f);
  assert.strictEqual(rows.length, 1);
  assert.deepStrictEqual(rows[0], row);
});

test("appendChild is append-only (preserves prior rows)", () => {
  const f = tmpFile();
  appendChild(f, { child_id: "anicca-c001" });
  appendChild(f, { child_id: "anicca-c002" });
  const rows = readChildren(f);
  assert.deepStrictEqual(rows.map((r) => r.child_id), ["anicca-c001", "anicca-c002"]);
});

test("readChildren skips blank lines and is resilient to a trailing newline", () => {
  const f = tmpFile();
  fs.writeFileSync(f, '{"child_id":"a"}\n\n{"child_id":"b"}\n');
  assert.deepStrictEqual(readChildren(f).map((r) => r.child_id), ["a", "b"]);
});

test("appendChild creates parent dirs if needed", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "spawn-ledger-deep-"));
  const f = path.join(dir, "nested", "state", "children.jsonl");
  appendChild(f, { child_id: "anicca-c001" });
  assert.strictEqual(readChildren(f).length, 1);
});

test("appendCitizenLifecycleReceipt writes a durable, self-describing receipt", () => {
  const f = tmpFile();
  const row = { child_id: "anicca-c001", status: "active", attempted_ms: 123, active_since: 124 };
  const receipt = appendCitizenLifecycleReceipt(f, row);

  assert.deepStrictEqual(row, { child_id: "anicca-c001", status: "active", attempted_ms: 123, active_since: 124 });
  assert.strictEqual(receipt.schema, "life-manager.citizen-lifecycle-receipt.v1");
  assert.strictEqual(receipt.event_type, "citizen.lifecycle");
  assert.strictEqual(receipt.citizen_id, "anicca-c001");
  assert.strictEqual(receipt.lifecycle_status, "active");
  assert.strictEqual(receipt.occurred_at_ms, 124);
  assert.match(receipt.receipt_id, /^[a-f0-9]{64}$/);
  assert.deepStrictEqual(readChildren(f), [receipt]);
});

test("lifecycle receipt id is deterministic and changes with the event", () => {
  const first = appendCitizenLifecycleReceipt(tmpFile(), { status: "failed", child_id: "anicca-c001", attempted_ms: 123 });
  const reordered = appendCitizenLifecycleReceipt(tmpFile(), { attempted_ms: 123, child_id: "anicca-c001", status: "failed" });
  const active = appendCitizenLifecycleReceipt(tmpFile(), { child_id: "anicca-c001", status: "active", attempted_ms: 123 });

  assert.strictEqual(first.receipt_id, reordered.receipt_id);
  assert.notStrictEqual(first.receipt_id, active.receipt_id);
});

test("replaying a persisted lifecycle receipt preserves its receipt id", () => {
  const firstFile = tmpFile();
  const first = appendCitizenLifecycleReceipt(firstFile, {
    child_id: "anicca-c001", status: "active", attempted_ms: 123, optional: undefined,
  });
  const persisted = readChildren(firstFile)[0];
  const replayed = appendCitizenLifecycleReceipt(tmpFile(), persisted);

  assert.strictEqual(replayed.receipt_id, first.receipt_id);
});

test("lifecycle receipt rejects an event that cannot be replayed", () => {
  assert.throws(
    () => appendCitizenLifecycleReceipt(tmpFile(), null),
    /requires child_id, status, and attempted_ms or active_since/,
  );
  assert.throws(
    () => appendCitizenLifecycleReceipt(tmpFile(), { child_id: "anicca-c001", status: "active" }),
    /requires child_id, status, and attempted_ms or active_since/,
  );
});
