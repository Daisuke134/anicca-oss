"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const REPO_ROOT = path.resolve(__dirname, "../../..");
const RENDERER = path.join(REPO_ROOT, "skills/connector/render-launchd.sh");

test("production renderer emits one 30-minute Connector owner and no retry sidecars", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-launchd-"));
  try {
    const outputDir = path.join(directory, "rendered");
    const lifeManagerHome = path.join(directory, "life-manager-home");
    const connectorEnvFile = path.join(directory, "secrets", "connector.env");
    fs.mkdirSync(path.dirname(connectorEnvFile), { recursive: true });
    fs.writeFileSync(connectorEnvFile, "LM_FIXTURE=1\n", { mode: 0o600 });
    const missingEnvFile = spawnSync("bash", [
      RENDERER,
      "--output-dir", outputDir,
      "--repo-root", REPO_ROOT,
      "--life-manager-home", lifeManagerHome,
      "--connector-env-file", path.join(directory, "secrets", "missing.env"),
    ], { encoding: "utf8", env: { ...process.env, HOME: directory } });
    assert.notEqual(missingEnvFile.status, 0);
    const result = spawnSync("bash", [
      RENDERER,
      "--output-dir", outputDir,
      "--repo-root", REPO_ROOT,
      "--life-manager-home", lifeManagerHome,
      "--connector-env-file", connectorEnvFile,
    ], { encoding: "utf8", env: { ...process.env, HOME: directory } });

    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(fs.readdirSync(outputDir), [
      "ai.anicca.life-manager-connector-native.plist",
    ]);
    const plist = fs.readFileSync(path.join(
      outputDir, "ai.anicca.life-manager-connector-native.plist",
    ), "utf8");
    assert.match(plist, /<key>StartInterval<\/key>\s*<integer>1800<\/integer>/);
    assert.doesNotMatch(plist, /<key>StartCalendarInterval<\/key>/);
    assert.equal((plist.match(/<key>Label<\/key>/g) || []).length, 1);
    assert.match(plist, /<key>EnvironmentVariables<\/key>/);
    assert.match(plist, /<key>LM_CONNECTOR_SHARED_ENV_FILE<\/key>/);
    assert.match(plist, /<key>LIFE_MANAGER_STATE_HOME<\/key>/);
    assert.match(plist, new RegExp(connectorEnvFile.replaceAll("&", "&amp;")));
    assert.match(plist, new RegExp(lifeManagerHome.replaceAll("&", "&amp;")));
    assert.doesNotMatch(plist, /healthcheck|healer|host-bridge|:9223/i);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
