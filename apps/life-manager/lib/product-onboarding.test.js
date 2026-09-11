"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const { planProductOnboarding, readProductLoopCatalog } = require("./product-onboarding.js");

const ROOT = path.resolve(__dirname, "../../..");

test("one catalog describes all 14 public product loops on Local and Cloud", () => {
  const catalog = readProductLoopCatalog();
  assert.equal(catalog.loops.length, 14);
  assert.deepEqual(catalog.host_requirements, { local: ["telegram_credentials"], cloud: [] });
  assert.equal(new Set(catalog.loops.map((loop) => loop.id)).size, 14);
  for (const loop of catalog.loops) {
    assert.ok(loop.description.length > 20, loop.id);
    assert.ok(loop.hosts.local, `${loop.id} local`);
    assert.ok(loop.hosts.cloud, `${loop.id} cloud`);
  }
  const english = fs.readFileSync(path.join(ROOT, "README.md"), "utf8");
  const japanese = fs.readFileSync(path.join(ROOT, "README.ja.md"), "utf8");
  for (const loop of catalog.loops) {
    assert.match(english, new RegExp(loop.name.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"), "u"));
    assert.match(japanese, new RegExp(loop.name.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"), "u"));
  }
});

test("the catalog loads from the standalone Cloud application artifact", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-cloud-onboarding-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "lib"), { recursive: true });
  fs.mkdirSync(path.join(root, "config"), { recursive: true });
  fs.copyFileSync(path.join(ROOT, "apps/life-manager/lib/product-onboarding.js"),
    path.join(root, "lib/product-onboarding.js"));
  fs.copyFileSync(path.join(ROOT, "apps/life-manager/config/product-loop-catalog.json"),
    path.join(root, "config/product-loop-catalog.json"));
  const packaged = require(path.join(root, "lib/product-onboarding.js"));
  assert.equal(packaged.readProductLoopCatalog().loops.length, 14);
  assert.equal(packaged.planProductOnboarding({
    host: "cloud", selected_loop_ids: ["agent-economy"],
  }).loops[0].state, "ready_to_start");
});

test("Local plans only selected loops and makes missing setup explicit", () => {
  const economy = planProductOnboarding({ host: "local", selected_loop_ids: ["agent-economy"],
    verified_requirements: ["telegram_credentials"] });
  assert.deepEqual(economy.selected_loop_ids, ["agent-economy"]);
  assert.equal(economy.loops[0].state, "ready_to_start");
  assert.deepEqual(economy.loops[0].command, ["./install.sh"]);
  assert.equal(economy.starts_automatically, false);
  assert.deepEqual(economy.external_effects, []);

  const connector = planProductOnboarding({ host: "local", selected_loop_ids: ["connector"],
    verified_requirements: ["telegram_credentials", "calendar", "telegram"] });
  assert.equal(connector.loops[0].state, "setup_required");
  assert.deepEqual(connector.loops[0].missing, ["provider_login_when_required"]);
  assert.deepEqual(connector.loops[0].command, ["./install.sh", "connector"]);
});

test("Cloud /start uses the same catalog while unsupported host adapters stay setup_required", () => {
  const economy = planProductOnboarding({ host: "cloud", selected_loop_ids: ["agent-economy"] });
  assert.equal(economy.loops[0].state, "ready_to_start");
  assert.deepEqual(economy.loops[0].command, ["/start"]);

  const mobile = planProductOnboarding({ host: "cloud", selected_loop_ids: ["mobile-apps"],
    verified_requirements: ["mobile_opportunity"] });
  assert.equal(mobile.loops[0].state, "setup_required");
  assert.deepEqual(mobile.loops[0].missing, ["host_adapter"]);
  assert.equal(mobile.loops[0].command, null);

  const investment = planProductOnboarding({ host: "cloud", selected_loop_ids: ["investment"],
    verified_requirements: ["alpaca_credentials", "investment_mode"] });
  assert.equal(investment.loops[0].state, "setup_required");
  assert.deepEqual(investment.loops[0].missing, ["host_adapter"]);
  assert.equal(investment.loops[0].command, null);
});

test("there is no start-all plan or Local enable-all endpoint", () => {
  for (const selected_loop_ids of [[], ["all"], ["connector", "connector"], ["unknown"]]) {
    assert.throws(() => planProductOnboarding({ host: "local", selected_loop_ids }));
  }
  const ui = fs.readFileSync(path.join(ROOT, "apps/oss-onboarding/index.html"), "utf8");
  const server = fs.readFileSync(path.join(ROOT, "scripts/life-manager-onboarding-server.py"), "utf8");
  assert.doesNotMatch(ui, /enableAll|Enable all|api\/enable-all/u);
  assert.doesNotMatch(server, /api\/enable-all/u);
});

test("the Local install entrypoint emits a zero-effect selected-loop plan", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-onboarding-plan-"));
  const result = spawnSync("bash", [path.join(ROOT, "install.sh"), "plan", "--loop", "agent-economy"], {
    cwd: ROOT, encoding: "utf8", env: { ...process.env, HOME: home },
  });
  fs.rmSync(home, { recursive: true, force: true });
  assert.equal(result.status, 0, result.stderr);
  const plan = JSON.parse(result.stdout);
  assert.equal(plan.host, "local");
  assert.deepEqual(plan.selected_loop_ids, ["agent-economy"]);
  assert.equal(plan.loops[0].state, "setup_required");
  assert.deepEqual(plan.loops[0].missing, ["telegram_credentials"]);
  assert.deepEqual(plan.external_effects, []);
});

test("Cloud provisioning is gated by the shared product model", () => {
  const source = fs.readFileSync(path.join(ROOT, "apps/life-manager/server.js"), "utf8");
  assert.match(source, /planProductOnboarding\(\{ host: "cloud", selected_loop_ids: \["agent-economy"\] \}\)/u);
  assert.match(source, /onboarding\.loops\[0\]\.command\?\.\[0\] !== "\/start"/u);
});
