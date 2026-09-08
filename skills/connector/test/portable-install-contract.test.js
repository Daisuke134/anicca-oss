"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { readConnectorProfile } = require("../../../apps/life-manager/lib/connector-profile.js");
const { runNativePass } = require("../native-pass.js");

const REPO_ROOT = path.resolve(__dirname, "../../..");
const SKILL_ROOT = path.join(REPO_ROOT, "skills", "connector");
const LABEL = "ai.anicca.life-manager-connector-native.plist";

test("public Connector package installs, renders, runs one no-effect wake, and uninstalls in an isolated home", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "connector-public-home-"));
  try {
    const profile = readConnectorProfile({
      tenantId: "public-sample",
      path: path.join(SKILL_ROOT, "examples", "public-profile.json"),
    });
    assert.equal(profile.timezone, "Asia/Tokyo");
    assert.match(profile.preferences, /YC.*lightning-talk.*AI.*crypto.*startup/i);

    const stateHome = path.join(home, ".local", "state", "life-manager");
    const rendered = path.join(stateHome, "rendered-launchd");
    const envFile = path.join(stateHome, ".env");
    const launchAgents = path.join(home, "Library", "LaunchAgents");
    fs.mkdirSync(stateHome, { recursive: true, mode: 0o700 });
    fs.mkdirSync(launchAgents, { recursive: true, mode: 0o700 });
    fs.writeFileSync(envFile, "LM_CONNECTOR_TENANT_ID=public-sample\n", { mode: 0o600 });
    fs.chmodSync(envFile, 0o600);

    const render = spawnSync("bash", [
      path.join(SKILL_ROOT, "render-launchd.sh"),
      "--output-dir", rendered,
      "--repo-root", REPO_ROOT,
      "--life-manager-home", stateHome,
      "--connector-env-file", envFile,
    ], { encoding: "utf8", env: { ...process.env, HOME: home } });
    assert.equal(render.status, 0, render.stderr);
    const renderedPlist = path.join(rendered, LABEL);
    const installedPlist = path.join(launchAgents, LABEL);
    fs.copyFileSync(renderedPlist, installedPlist, fs.constants.COPYFILE_EXCL);
    const plist = fs.readFileSync(installedPlist, "utf8");
    assert.match(plist, /<key>StartInterval<\/key>\s*<integer>1800<\/integer>/);
    assert.equal((plist.match(/<key>Label<\/key>/g) || []).length, 1);

    let wakeCalls = 0;
    const result = await runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(stateHome, "connector-native"),
      ownerToken: "public-isolated-no-effect-owner",
      dependencies: Object.freeze({ fixture: "no-effect" }),
      async runWake(input, dependencies) {
        wakeCalls += 1;
        assert.equal(input.maxWakeMs, 600_000);
        assert.equal(dependencies.fixture, "no-effect");
        return Object.freeze({ status: "completed_no_effect", safe_reason: "isolated_fixture" });
      },
    });
    assert.deepEqual(result, { status: "completed_no_effect", safe_reason: "isolated_fixture" });
    assert.equal(wakeCalls, 1);

    fs.rmSync(installedPlist);
    assert.equal(fs.existsSync(installedPlist), false);
    assert.equal(fs.existsSync(renderedPlist), true);

    const publicFiles = ["README.md", "SKILL.md", "WORKER-CONTRACT.md", "examples/public-profile.json"];
    const source = publicFiles.map((file) => fs.readFileSync(path.join(SKILL_ROOT, file), "utf8")).join("\n");
    assert.doesNotMatch(source, /\/Users\/|\.local\/state\/life-manager\/connector-native|connpass-action-boundary-deliveries|telegram_provider_id/);
    assert.equal(fs.readdirSync(path.join(SKILL_ROOT, "examples")).join(","), "public-profile.json");
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});
