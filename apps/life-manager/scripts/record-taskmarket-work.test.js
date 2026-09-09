"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");

const {
  fetchTaskIds,
  main,
  parseArgs,
} = require("./record-taskmarket-work.js");

const WORKER = "0xd7Db94062AFec8a86F70250B931C77619acf8937";
const TASK_ID = `0x${"ab".repeat(32)}`;

function response(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    async text() { return JSON.stringify(body); },
  };
}

test("discovers bounded unique task IDs from the worker submission feed", async () => {
  const ids = await fetchTaskIds({
    workerAddress: WORKER,
    fetchImpl: async () => response([
      { taskId: TASK_ID },
      { taskId: TASK_ID },
    ]),
    apiUrl: "https://api.taskmarket.dev",
  });
  assert.deepEqual(ids, [TASK_ID]);
});

test("rejects malformed, oversized, and failed TaskMarket discovery responses", async () => {
  await assert.rejects(() => fetchTaskIds({
    workerAddress: WORKER,
    fetchImpl: async () => ({ ok: true, status: 200, async text() { return "not-json"; } }),
  }), /JSON/);
  await assert.rejects(() => fetchTaskIds({
    workerAddress: WORKER,
    fetchImpl: async () => ({ ok: true, status: 200, async text() { return "x".repeat(2_000_001); } }),
  }), /bounded/);
  await assert.rejects(() => fetchTaskIds({
    workerAddress: WORKER,
    fetchImpl: async () => response({ error: "down" }, { ok: false, status: 503 }),
  }), /503/);
});

test("parses only explicit complete CLI arguments", () => {
  assert.deepEqual(parseArgs([
    "--worker", WORKER,
    "--self-wallet", WORKER,
    "--task", TASK_ID,
    "--api", "https://example.test",
    "--rpc", "https://rpc.test",
  ]), {
    workerAddress: WORKER,
    selfWallets: [WORKER],
    taskIds: [TASK_ID],
    apiUrl: "https://example.test",
    rpcUrl: "https://rpc.test",
  });
  assert.throws(() => parseArgs(["--worker"]), /incomplete/);
  assert.throws(() => parseArgs(["--wat"]), /unknown/);
});

test("live-shaped open submission produces a truthful zero-write result", async () => {
  const writes = [];
  let output = "";
  const result = await main({
    fetchImpl: async (url) => {
      if (url.includes("/api/submissions/mine")) return response([{ taskId: TASK_ID }]);
      if (url.endsWith(`/api/tasks/${TASK_ID}`)) {
        return response({
          id: TASK_ID,
          requester: "0xa4d897959211c8e565F862080913b45Cc761Ac6A",
          status: "open",
          selfAward: false,
          awardCount: 0,
          awards: [],
        });
      }
      throw new Error(`unexpected URL ${url}`);
    },
    rpcCall: async () => { throw new Error("RPC must not run before an award"); },
    recordEntry: async (row) => { writes.push(row); return { ok: true, duplicate: false }; },
    now: () => new Date("2026-07-28T06:30:00.000Z"),
    writeOutput: (text) => { output += text; },
  }, ["--worker", WORKER, "--self-wallet", WORKER]);
  assert.equal(result.ok, true);
  assert.equal(result.tasks_seen, 1);
  assert.equal(result.pending, 1);
  assert.equal(result.recorded, 0);
  assert.equal(writes.length, 0);
  assert.equal(JSON.parse(output).recorded, 0);
});

test("the registry alone owns the separate five-minute Taskmarket loop", () => {
  const root = join(__dirname, "..");
  const boot = readFileSync(join(__dirname, "taskmarket-work-ledger-boot.sh"), "utf8");
  const registry = JSON.parse(readFileSync(join(root, "..", "..", "config", "loop-registry.json"), "utf8"));
  const loop = registry.loops["life-manager-taskmarket-ledger"];
  assert.match(boot, /record-taskmarket-work\.js/);
  assert.match(boot, /handoff-taskmarket-awards\.js/);
  assert.match(boot, /mktemp/);
  assert.match(boot, /SCRIPT_DIR/);
  assert.match(boot, /timeout 180/);
  assert.match(boot, /timeout 55/);
  assert.doesNotMatch(boot, /anicca\/apps\/life-manager/);
  assert.match(boot, /TASKMARKET_SELF_WALLETS_MODULE/);
  assert.match(boot, /REPO_ROOT.*skills\/earn\/x402-sell\/lib\/self-wallets\.mjs/);
  assert.doesNotMatch(boot, /\$\{HOME\}\/anicca/);
  assert.equal(loop.adapter, "exec");
  assert.deepEqual(loop.command, []);
  assert.equal(loop.entrypoint, "apps/life-manager/scripts/taskmarket-work-ledger-boot.sh");
  assert.deepEqual(loop.cadence, { start_interval_seconds: 300 });
  assert.equal(existsSync(join(__dirname, "install-taskmarket-work-ledger-launchd.sh")), false);
  assert.equal(existsSync(join(root, "launchd", "ai.anicca.life-manager-taskmarket-ledger.plist.template")), false);
});
