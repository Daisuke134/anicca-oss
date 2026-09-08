"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createConnectorTabOwner } = require("./connector-tab-owner.js");

test("claims exactly the matching :9222 Luma page and writes a private ownership receipt", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-tab-owner-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const receiptPath = path.join(directory, "tab-owner.json");
  const owner = createConnectorTabOwner({
    endpoint: "http://127.0.0.1:9222",
    listTargets: async () => [
      {
        id: "DEVTOOLS",
        type: "page",
        url: "chrome://newtab/",
        webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/DEVTOOLS",
      },
      {
        id: "BASELINE",
        type: "page",
        url: "https://luma.com/home",
        webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/BASELINE",
      },
      {
        id: "OWNED123",
        type: "page",
        url: "https://luma.com/tokyo-ai?tk=invite#registration",
        webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/OWNED123",
      },
    ],
    ownerToken: () => "connector-owner-token",
    now: () => new Date("2026-08-06T01:02:03.000Z"),
  });

  const receipt = await owner.claim({
    canonicalUrl: "https://luma.com/tokyo-ai",
    baselineTargetIds: ["BASELINE"],
    receiptPath,
  });

  assert.deepEqual(receipt, {
    schema_version: 1,
    endpoint: "http://127.0.0.1:9222",
    owner_token: "connector-owner-token",
    target_id: "OWNED123",
    page_websocket: "ws://127.0.0.1:9222/devtools/page/OWNED123",
    baseline_target_ids: ["BASELINE"],
    canonical_url: "https://luma.com/tokyo-ai",
    observed_at: "2026-08-06T01:02:03.000Z",
  });
  assert.deepEqual(JSON.parse(fs.readFileSync(receiptPath, "utf8")), receipt);
  assert.equal(fs.statSync(receiptPath).mode & 0o777, 0o600);
});

test("refuses another browser owner and ambiguous matching tabs", async () => {
  assert.throws(
    () => createConnectorTabOwner({
      endpoint: "http://[::1]:9222",
      listTargets: async () => [],
    }),
    /:9222/,
  );
  assert.throws(
    () => createConnectorTabOwner({
      endpoint: "http://127.0.0.1:9223",
      listTargets: async () => [],
    }),
    /:9222/,
  );

  const owner = createConnectorTabOwner({
    endpoint: "http://127.0.0.1:9222",
    listTargets: async () => [
      {
        id: "ONE",
        type: "page",
        url: "https://luma.com/tokyo-ai",
        webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/ONE",
      },
      {
        id: "TWO",
        type: "page",
        url: "https://luma.com/tokyo-ai?tk=other",
        webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/TWO",
      },
    ],
  });

  await assert.rejects(
    owner.claim({ canonicalUrl: "https://luma.com/tokyo-ai", baselineTargetIds: [] }),
    /exactly one owned event page/i,
  );
});

test("does not issue an ownership receipt until the durable target lease claims the exact page", async () => {
  const calls = [];
  const fence = Object.freeze({
    schema_version: 1,
    owner_token: "connector-owner-token",
    generation: 7,
    target_id: "OWNED123",
    page_websocket: "ws://127.0.0.1:9222/devtools/page/OWNED123",
    canonical_url: "https://luma.com/tokyo-ai",
    claimed_at: "2026-08-06T01:02:03.000Z",
    heartbeat_at: "2026-08-06T01:02:03.000Z",
  });
  const owner = createConnectorTabOwner({
    listTargets: async () => [{
      id: "OWNED123",
      type: "page",
      url: "https://luma.com/tokyo-ai?tk=invite",
      webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/OWNED123",
    }],
    targetLease: {
      async claim(input) {
        calls.push(input);
        return fence;
      },
    },
  });

  const receipt = await owner.claim({
    canonicalUrl: "https://luma.com/tokyo-ai",
    baselineTargetIds: [],
  });

  assert.deepEqual(calls, [{
    targetId: "OWNED123",
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNED123",
    canonicalUrl: "https://luma.com/tokyo-ai",
  }]);
  assert.equal(receipt.owner_token, fence.owner_token);
  assert.equal(receipt.generation, 7);
  assert.equal(receipt.target_id, fence.target_id);
});

test("claims a parent-created exact target and delegates its fenced lifecycle", async () => {
  const calls = [];
  const fence = Object.freeze({
    schema_version: 1,
    owner_token: "connector-owner-token",
    generation: 3,
    target_id: "PARENT_TARGET",
    page_websocket: "ws://127.0.0.1:9222/devtools/page/PARENT_TARGET",
    canonical_url: "https://luma.com/tokyo-ai",
    claimed_at: "2026-08-06T01:02:03.000Z",
    heartbeat_at: "2026-08-06T01:02:03.000Z",
  });
  const targetLease = {
    async claim(input) { calls.push(["claim", input]); return fence; },
    async heartbeat(input) { calls.push(["heartbeat", input]); return fence; },
    async probe(input) { calls.push(["probe", input]); return true; },
    async release(input) { calls.push(["release", input]); return true; },
    async reapStale(input) { calls.push(["reap", input]); return { reaped_target_ids: [], retained_target_ids: [] }; },
  };
  const owner = createConnectorTabOwner({ targetLease, listTargets: async () => [] });

  const receipt = await owner.claimExact({
    canonicalUrl: "https://luma.com/tokyo-ai",
    targetId: "PARENT_TARGET",
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/PARENT_TARGET",
  });
  assert.equal(receipt.generation, 3);
  assert.deepEqual(receipt.baseline_target_ids, []);
  assert.equal(await owner.probe(receipt), true);
  assert.equal(await owner.release(receipt), true);
  assert.deepEqual(calls.map(([name]) => name), ["claim", "probe", "release"]);
});

test("claims a parent-created Connpass target but rejects an arbitrary origin", async () => {
  const canonicalUrl = "https://tokyo-builders.connpass.com/event/101";
  const fence = Object.freeze({
    schema_version: 1, owner_token: "connector-owner-token", generation: 1,
    target_id: "CONNPASS_TARGET",
    page_websocket: "ws://127.0.0.1:9222/devtools/page/CONNPASS_TARGET",
    canonical_url: canonicalUrl,
    claimed_at: "2026-08-06T01:02:03.000Z", heartbeat_at: "2026-08-06T01:02:03.000Z",
  });
  const owner = createConnectorTabOwner({
    targetLease: {
      async claim(input) { assert.equal(input.canonicalUrl, canonicalUrl); return fence; },
      async heartbeat() { return fence; }, async probe() { return true; },
      async release() { return true; }, async reapStale() { return {}; },
    },
    listTargets: async () => [],
  });
  const receipt = await owner.claimExact({
    canonicalUrl: `${canonicalUrl}/?ref=connector`, targetId: "CONNPASS_TARGET",
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/CONNPASS_TARGET",
  });
  assert.equal(receipt.canonical_url, canonicalUrl);
  await assert.rejects(owner.claimExact({
    canonicalUrl: "https://example.com/event/101", targetId: "CONNPASS_TARGET",
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/CONNPASS_TARGET",
  }), /public event URL/i);
});
