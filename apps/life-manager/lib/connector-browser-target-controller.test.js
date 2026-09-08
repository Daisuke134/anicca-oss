"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const {
  createConnectorBrowserTargetController,
  dailyDriverEndpoint,
} = require("./connector-browser-target-controller.js");

test("resolves only the shared loopback daily-driver endpoint", () => {
  assert.equal(dailyDriverEndpoint("http://127.0.0.1:9222"), "http://127.0.0.1:9222");
  assert.equal(dailyDriverEndpoint("http://[::1]:9222"), "http://[::1]:9222");
  for (const endpoint of [
    "http://127.0.0.1:9223", "https://127.0.0.1:9222", "http://example.com:9222",
    "http://user:secret@127.0.0.1:9222", "http://127.0.0.1:9222/json/version",
  ]) assert.throws(() => dailyDriverEndpoint(endpoint), /endpoint invalid/i);
});

test("fresh Connector modules consume one configured endpoint and exact websocket origin", () => {
  const script = `
    const controller = require(${JSON.stringify(require.resolve("./connector-browser-target-controller.js"))});
    const runner = require(${JSON.stringify(require.resolve("./connector-minimal-runner.js"))});
    process.stdout.write(JSON.stringify({
      endpoint: controller.CONNECTOR_CDP_ENDPOINT,
      target: controller.connectorPageWebsocketTargetId("ws://[::1]:9222/devtools/page/TARGET123"),
      runner_loaded: typeof runner.runMinimalConnectorWake === "function",
    }));
  `;
  const result = spawnSync(process.execPath, ["-e", script], {
    env: { ...process.env, CLOAK_CDP_BASE_URL: "http://[::1]:9222" },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    endpoint: "http://[::1]:9222",
    target: "TARGET123",
    runner_loaded: true,
  });
});

function fixture({ baselineCount = 1, delayedOwnedInsertion = false } = {}) {
  const calls = [];
  const baselinePages = Array.from({ length: baselineCount }, (_, index) => ({
    targetId: index === 0 ? "BASELINE" : `BASELINE_${index}`,
    async evaluate() { return 1; },
  }));
  const baseline = baselinePages[0];
  const owned = { targetId: "OWNED123", async evaluate() { calls.push(["evaluate", "OWNED123"]); return 1; } };
  const pages = [...baselinePages];
  const liveTargetIds = new Set(baselinePages.map((page) => page.targetId));
  let targetCreated = false;
  let pagesAfterCreate = 0;
  let targetInfosOverride;
  function addOwnedPage() {
    if (liveTargetIds.has("OWNED123")) return;
    pages.push(owned);
    liveTargetIds.add("OWNED123");
  }
  const context = {
    pages() {
      calls.push(["pages"]);
      if (targetCreated && delayedOwnedInsertion) {
        pagesAfterCreate += 1;
        if (pagesAfterCreate === 2) addOwnedPage();
      }
      return [...pages];
    },
    async newCDPSession(page) {
      calls.push(["page-session", page.targetId]);
      return {
        async send(method) {
          calls.push(["page-send", page.targetId, method]);
          return { targetInfo: { targetId: page.targetId } };
        },
        async detach() { calls.push(["page-detach", page.targetId]); },
      };
    },
  };
  const browser = {
    contexts() { calls.push(["contexts"]); return [context]; },
    async newBrowserCDPSession() {
      calls.push(["browser-session"]);
      return {
        async send(method, params) {
          calls.push(["browser-send", method, params]);
          if (method === "Target.createTarget") {
            targetCreated = true;
            if (!delayedOwnedInsertion) addOwnedPage();
            return { targetId: "OWNED123" };
          }
          if (method === "Target.getTargets") {
            return {
              targetInfos: targetInfosOverride === undefined
                ? [...liveTargetIds].map((targetId) => ({ targetId })) : targetInfosOverride,
            };
          }
          if (method === "Target.closeTarget") {
            liveTargetIds.delete(params.targetId);
            return { success: true };
          }
          throw new Error(`unexpected ${method}`);
        },
        async detach() { calls.push(["browser-detach"]); },
      };
    },
  };
  return {
    baseline,
    browser,
    calls,
    owned,
    removeOwnedTarget() { liveTargetIds.delete("OWNED123"); },
    setTargetInventory(value) { targetInfosOverride = value; },
  };
}

test("creates exactly one default-context target and binds only its exact Playwright page", async () => {
  const fx = fixture();
  const controller = createConnectorBrowserTargetController({ browser: fx.browser });

  const result = await controller.create();

  assert.equal(result.target_id, "OWNED123");
  assert.equal(result.page_websocket, "ws://127.0.0.1:9222/devtools/page/OWNED123");
  assert.equal(result.page, fx.owned);
  assert.equal(fx.calls.filter(([name, method]) => name === "browser-send" && method === "Target.createTarget").length, 1);
  assert.deepEqual(
    fx.calls.find(([name, method]) => name === "browser-send" && method === "Target.createTarget"),
    ["browser-send", "Target.createTarget", { url: "about:blank" }],
  );
  assert.equal(fx.calls.some(([name]) => name === "new-page"), false);
});

test("binds a delayed owned page without scanning the baseline page set", async () => {
  const fx = fixture({ baselineCount: 463, delayedOwnedInsertion: true });
  const controller = createConnectorBrowserTargetController({ browser: fx.browser });

  const result = await controller.create();

  assert.equal(result.page, fx.owned);
  assert.deepEqual(
    fx.calls.filter(([name]) => name === "page-session").map(([, targetId]) => targetId),
    ["OWNED123"],
  );
});

test("probes and closes only the exact Connector target", async () => {
  const fx = fixture();
  const controller = createConnectorBrowserTargetController({ browser: fx.browser });
  const target = await controller.create();

  assert.equal(await controller.probe(target.page_websocket), true);
  assert.equal(await controller.close(target.target_id), true);
  assert.deepEqual(
    fx.calls.filter(([name, method]) => name === "browser-send" && method === "Target.closeTarget"),
    [["browser-send", "Target.closeTarget", { targetId: "OWNED123" }]],
  );
});

test("closing an already-disappeared exact target is idempotent", async () => {
  const fx = fixture();
  const controller = createConnectorBrowserTargetController({ browser: fx.browser });
  const target = await controller.create();
  fx.removeOwnedTarget();

  assert.equal(await controller.close(target.target_id), true);
  assert.deepEqual(
    fx.calls.filter(([name, method]) => name === "browser-send" && method === "Target.closeTarget"),
    [],
  );
});

test("closing with a malformed target inventory rejects before Target.closeTarget", async () => {
  const fx = fixture();
  const controller = createConnectorBrowserTargetController({ browser: fx.browser });
  const target = await controller.create();

  for (const targetInfos of [[null], [{}], [{ targetId: "invalid target id" }]]) {
    fx.setTargetInventory(targetInfos);
    await assert.rejects(() => controller.close(target.target_id), /Connector target ID invalid/);
  }
  assert.deepEqual(
    fx.calls.filter(([name, method]) => name === "browser-send" && method === "Target.closeTarget"),
    [],
  );
});

test("refuses another port, malformed target IDs, and ambiguous browser contexts", async () => {
  const fx = fixture();
  assert.throws(
    () => createConnectorBrowserTargetController({ browser: fx.browser, endpoint: "http://[::1]:9222" }),
    /endpoint/i,
  );
  assert.throws(
    () => createConnectorBrowserTargetController({ browser: fx.browser, endpoint: "http://127.0.0.1:9223" }),
    /endpoint/i,
  );
  assert.throws(
    () => createConnectorBrowserTargetController({
      browser: { ...fx.browser, contexts: () => [{}, {}] },
    }),
    /context/i,
  );
});
