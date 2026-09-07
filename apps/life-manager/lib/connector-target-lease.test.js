"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createConnectorTargetLease } = require("./connector-target-lease.js");

function fixture(t, overrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-target-lease-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const calls = [];
  const lease = createConnectorTargetLease({
    ledgerPath: path.join(directory, "target-leases.json"),
    now: () => new Date("2026-08-06T12:00:00.000Z"),
    ownerToken: () => "connector-owner-token-0001",
    probeTarget: async (pageWebsocket) => {
      calls.push(["probe", pageWebsocket]);
      return true;
    },
    closeTarget: async (targetId) => {
      calls.push(["close", targetId]);
      return true;
    },
    ...overrides,
  });
  return { calls, directory, lease, ledgerPath: path.join(directory, "target-leases.json") };
}

function claimInput(targetId = "TARGET_A") {
  return {
    targetId,
    pageWebsocket: `ws://[::1]:9222/devtools/page/${targetId}`,
    canonicalUrl: "https://luma.com/tokyo-ai",
  };
}

function writeLegacyLock(ledgerPath, ageMs) {
  const lockPath = `${ledgerPath}.lock`;
  fs.writeFileSync(lockPath, "", { mode: 0o600 });
  const mtimeMs = Date.now() - ageMs;
  fs.utimesSync(lockPath, mtimeMs / 1000, mtimeMs / 1000);
  return lockPath;
}

test("durably fences one Connector target and writes only private safe ownership fields", async (t) => {
  const fx = fixture(t);
  const fence = await fx.lease.claim(claimInput());

  assert.deepEqual(fence, {
    schema_version: 1,
    owner_token: "connector-owner-token-0001",
    generation: 1,
    target_id: "TARGET_A",
    page_websocket: "ws://[::1]:9222/devtools/page/TARGET_A",
    canonical_url: "https://luma.com/tokyo-ai",
    claimed_at: "2026-08-06T12:00:00.000Z",
    heartbeat_at: "2026-08-06T12:00:00.000Z",
  });
  assert.equal(fs.statSync(fx.ledgerPath).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(fs.readFileSync(fx.ledgerPath, "utf8")), {
    schema_version: 1,
    targets: { TARGET_A: fence },
  });

  await assert.rejects(fx.lease.claim(claimInput()), /already claimed/i);
});

test("rejects stale fences and releases only the exactly fenced target", async (t) => {
  let tokenNumber = 0;
  const fx = fixture(t, { ownerToken: () => `connector-owner-token-000${++tokenNumber}` });
  const first = await fx.lease.claim(claimInput("TARGET_A"));
  const second = await fx.lease.claim(claimInput("TARGET_B"));

  await assert.rejects(
    fx.lease.heartbeat({ ...first, owner_token: "wrong-owner-token-0000" }),
    /fence mismatch/i,
  );
  await assert.rejects(
    fx.lease.release({ ...first, generation: 2 }),
    /fence mismatch/i,
  );
  assert.deepEqual(fx.calls, []);

  assert.equal(await fx.lease.release(first), true);
  assert.deepEqual(fx.calls, [["close", "TARGET_A"]]);
  const ledger = JSON.parse(fs.readFileSync(fx.ledgerPath, "utf8"));
  assert.deepEqual(Object.keys(ledger.targets), ["TARGET_B"]);
  assert.equal(ledger.targets.TARGET_B.owner_token, second.owner_token);
});

test("reports renderer death without closing or releasing the live ownership fence", async (t) => {
  const fx = fixture(t, {
    probeTarget: async (pageWebsocket) => {
      fx.calls.push(["probe", pageWebsocket]);
      return false;
    },
  });
  const fence = await fx.lease.claim(claimInput());

  assert.equal(await fx.lease.probe(fence), false);
  assert.deepEqual(fx.calls, [["probe", fence.page_websocket]]);
  const ledger = JSON.parse(fs.readFileSync(fx.ledgerPath, "utf8"));
  assert.equal(ledger.targets.TARGET_A.owner_token, fence.owner_token);
});

test("rejects a heartbeat that moves the common lease clock backwards", async (t) => {
  let clock = new Date("2026-08-06T12:00:00.000Z");
  const fx = fixture(t, { now: () => clock });
  const fence = await fx.lease.claim(claimInput());
  clock = new Date("2026-08-06T11:59:59.000Z");

  await assert.rejects(fx.lease.heartbeat(fence), /heartbeat moved backwards/i);
  const ledger = JSON.parse(fs.readFileSync(fx.ledgerPath, "utf8"));
  assert.equal(ledger.targets.TARGET_A.heartbeat_at, fence.heartbeat_at);
});

test("keeps the ownership fence when exact target close fails", async (t) => {
  const fx = fixture(t, { closeTarget: async () => false });
  const fence = await fx.lease.claim(claimInput());

  await assert.rejects(fx.lease.release(fence), /target close failed/i);
  const ledger = JSON.parse(fs.readFileSync(fx.ledgerPath, "utf8"));
  assert.deepEqual(ledger.targets.TARGET_A, fence);
});

test("refuses non-Connector websocket endpoints and credential-bearing event URLs", async (t) => {
  const fx = fixture(t);
  await assert.rejects(fx.lease.claim({
    ...claimInput(),
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TARGET_A",
  }), /page websocket/i);
  await assert.rejects(fx.lease.claim({
    ...claimInput(),
    pageWebsocket: "ws://127.0.0.1:9223/devtools/page/TARGET_A",
  }), /page websocket/i);
  await assert.rejects(fx.lease.claim({
    ...claimInput(),
    canonicalUrl: "https://user:secret@luma.com/tokyo-ai",
  }), /canonical url/i);
  assert.equal(fs.existsSync(fx.ledgerPath), false);
});

test("recovers a stale zero-byte legacy lock", async (t) => {
  const fx = fixture(t);
  writeLegacyLock(fx.ledgerPath, 11 * 60 * 1000);

  const fence = await fx.lease.claim(claimInput());

  assert.equal(fence.target_id, "TARGET_A");
  assert.equal(fs.existsSync(`${fx.ledgerPath}.lock`), false);
});

test("keeps a fresh zero-byte legacy lock busy", async (t) => {
  const fx = fixture(t);
  writeLegacyLock(fx.ledgerPath, 0);

  await assert.rejects(fx.lease.claim(claimInput()), /ledger busy/i);
  assert.equal(fs.existsSync(`${fx.ledgerPath}.lock`), true);
});

test("keeps a stale lock busy when its recorded owner PID is alive", async (t) => {
  const fx = fixture(t);
  const lockPath = `${fx.ledgerPath}.lock`;
  fs.writeFileSync(lockPath, JSON.stringify({
    schema_version: 1,
    pid: process.pid,
    owner_token: "live-owner-token",
    acquired_at_ms: Date.now() - 11 * 60 * 1000,
  }), { mode: 0o600 });

  await assert.rejects(fx.lease.claim(claimInput()), /ledger busy/i);
  assert.equal(fs.existsSync(lockPath), true);
});

test("does not unlink a replacement lock from the prior owner's release path", async (t) => {
  let lockPath;
  let acquiredMetadata;
  const fx = fixture(t, {
    probeTarget: async () => {
      acquiredMetadata = JSON.parse(fs.readFileSync(lockPath, "utf8"));
      assert.deepEqual(Object.keys(acquiredMetadata).sort(), [
        "acquired_at_ms",
        "owner_token",
        "pid",
        "schema_version",
      ]);
      assert.equal(acquiredMetadata.schema_version, 1);
      assert.equal(acquiredMetadata.pid, process.pid);
      assert.match(acquiredMetadata.owner_token, /^[0-9a-f-]{36}$/);
      assert.equal(fs.statSync(lockPath).mode & 0o777, 0o600);
      fs.writeFileSync(lockPath, JSON.stringify({
        schema_version: 1,
        pid: process.pid,
        owner_token: "replacement-owner-token",
        acquired_at_ms: Date.now(),
      }), { mode: 0o600 });
      return true;
    },
  });
  lockPath = `${fx.ledgerPath}.lock`;
  const fence = await fx.lease.claim(claimInput());

  assert.equal(await fx.lease.probe(fence), true);
  assert.ok(acquiredMetadata.acquired_at_ms > 0);
  assert.equal(fs.existsSync(lockPath), true);
  assert.equal(JSON.parse(fs.readFileSync(lockPath, "utf8")).owner_token, "replacement-owner-token");
});

test("reaps only heartbeat-expired Connector targets while preserving a fresh owner", async (t) => {
  let clock = new Date("2026-08-06T10:00:00.000Z");
  let tokenNumber = 0;
  const fx = fixture(t, {
    now: () => clock,
    ownerToken: () => `connector-owner-token-000${++tokenNumber}`,
  });
  await fx.lease.claim(claimInput("STALE_TARGET"));
  clock = new Date("2026-08-06T11:50:00.000Z");
  const fresh = await fx.lease.claim(claimInput("FRESH_TARGET"));
  clock = new Date("2026-08-06T12:00:00.000Z");

  assert.deepEqual(await fx.lease.reapStale({ maxIdleMs: 30 * 60 * 1000 }), {
    reaped_target_ids: ["STALE_TARGET"],
    retained_target_ids: ["FRESH_TARGET"],
  });
  assert.deepEqual(fx.calls, [["close", "STALE_TARGET"]]);
  const ledger = JSON.parse(fs.readFileSync(fx.ledgerPath, "utf8"));
  assert.deepEqual(Object.keys(ledger.targets), ["FRESH_TARGET"]);
  assert.equal(ledger.targets.FRESH_TARGET.owner_token, fresh.owner_token);
});
