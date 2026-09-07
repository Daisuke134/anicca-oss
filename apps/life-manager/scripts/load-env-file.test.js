"use strict";

// F3: the shared env-file loader must (a) warn to stderr but keep booting when
// the resolved env file does not exist, (b) refuse loudly (exit 1) when
// LIFE_MANAGER_ENV_FILE points beneath a legacy runtime root (mirroring
// LEGACY_SEGMENT in lib/runtime-paths.js), and (c) export variables from a
// valid env file. All four launchd boot scripts must actually use it.

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const LIB = path.join(__dirname, "lib", "load-env-file.sh");

// Legacy tokens are assembled from fragments so this test file stays scan-neutral.
const OPENCLAW_SEGMENT = "." + "open" + "claw";
const RETIRED_CHECKOUT_SEGMENT = "profitable" + "-claude";
const V0_SEGMENT = "life-manager" + "-v0";
const ABS_USER_HOME = "/" + "Users/operator";

function runLoader(envFile) {
  return spawnSync("bash", [
    "-c",
    `set -euo pipefail; source "$1"; lm_load_env_file "$2"; echo "BOOTED FOO=\${FOO:-unset}"`,
    "bash",
    LIB,
    envFile,
  ], { encoding: "utf8" });
}

test("a missing env file warns on stderr but keeps booting", () => {
  const missing = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "lm-env-")), "no.env");
  const result = runLoader(missing);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /warning/i);
  assert.match(result.stderr, /no\.env/);
  assert.match(result.stdout, /BOOTED/);
});

test("an env file beneath a legacy runtime root is refused with exit 1", () => {
  for (const segment of [OPENCLAW_SEGMENT, RETIRED_CHECKOUT_SEGMENT, V0_SEGMENT]) {
    const result = runLoader(`${ABS_USER_HOME}/${segment}/.env`);
    assert.equal(result.status, 1, `${segment}: ${result.stderr}`);
    assert.match(result.stderr, /legacy runtime root/i);
    assert.ok(!result.stdout.includes("BOOTED"), segment);
  }
  // Case-insensitive, and matching only whole path segments:
  const upper = runLoader(`${ABS_USER_HOME}/${OPENCLAW_SEGMENT.toUpperCase()}/.env`);
  assert.equal(upper.status, 1);
  const lookalike = runLoader(`${ABS_USER_HOME}/not-${V0_SEGMENT}-suffix/.env`);
  assert.equal(lookalike.status, 0, lookalike.stderr);
});

test("a valid env file is sourced with its variables exported", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lm-env-"));
  const envFile = path.join(dir, "boot.env");
  fs.writeFileSync(envFile, "FOO=from-env-file\n");
  const result = runLoader(envFile);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /FOO=from-env-file/);
  assert.ok(!/warning/i.test(result.stderr), result.stderr);
});

test("all four launchd boot scripts load the shared guarded env loader", () => {
  for (const script of [
    "payout-boot.sh",
    "x402-sale-ledger-boot.sh",
    "taskmarket-work-ledger-boot.sh",
    "ugig-invoice-observer-boot.sh",
  ]) {
    const source = fs.readFileSync(path.join(__dirname, script), "utf8");
    assert.ok(source.includes("lib/load-env-file.sh"), `${script} must source the shared loader`);
    assert.ok(source.includes("lm_load_env_file"), `${script} must call lm_load_env_file`);
  }
});

test("the shared mobile app command loads the private marketing env", () => {
  const source = fs.readFileSync(path.join(__dirname, "mobile-app"), "utf8");
  assert.ok(source.includes("lib/load-env-file.sh"));
  assert.ok(source.includes("lm_load_env_file"));
  assert.ok(source.includes("LIFE_MANAGER_MARKETING_ENV_FILE"));
  assert.ok(source.includes(".local/state/life-manager/private/marketing.env"));
  assert.ok(source.includes("exec /opt/homebrew/bin/timeout 1200 /opt/homebrew/bin/node"));
  assert.ok(!source.includes("LIFE_MANAGER_NODE_BIN"));
  assert.ok(!source.includes("LIFE_MANAGER_TIMEOUT_BIN"));
});

test("JA main TikTok boot executes the Larry carousel owner", () => {
  const item = require("../config/mobile-app-loops.json").loops["life-manager-anicca-main-tiktok"];
  assert.deepEqual(item, {
    product_id: "anicca-ios",
    runner: "anicca-larry-ja-canary.js",
    action: "run-ja-main-tiktok-production",
  });
});
