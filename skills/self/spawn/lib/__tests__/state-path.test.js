const { test } = require("node:test");
const assert = require("node:assert");
const { resolveStateDir } = require("../state-path");

// gap 3 root cause: the live E2E wrote children.jsonl under /tmp, which is tmp-cleaned and gone.
// resolveStateDir is fail-closed: it REFUSES any /tmp-rooted path so the colony ledger is never lost.

test("defaults to the Life Manager Agent Economy instance state", () => {
  assert.strictEqual(
    resolveStateDir({ env: {}, home: "/home/anicca" }),
    "/home/anicca/.local/state/life-manager/agent-economy/instance/state"
  );
});

test("honors an explicit durable ANICCA_STATE_DIR", () => {
  assert.strictEqual(
    resolveStateDir({ env: { ANICCA_STATE_DIR: "/var/lib/anicca" }, home: "/root" }),
    "/var/lib/anicca"
  );
});

test("REFUSES a /tmp state dir (tmp-cleaned => ledger lost)", () => {
  assert.throws(
    () => resolveStateDir({ env: { ANICCA_STATE_DIR: "/tmp/spawn-live-state" }, home: "/root" }),
    /durable|tmp/i
  );
});

test("REFUSES /private/tmp too (macOS tmp symlink)", () => {
  assert.throws(
    () => resolveStateDir({ env: { ANICCA_STATE_DIR: "/private/tmp/x" }, home: "/root" }),
    /durable|tmp/i
  );
});
