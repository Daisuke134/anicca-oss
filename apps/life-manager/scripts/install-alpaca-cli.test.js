"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const test = require("node:test");

const script = path.join(__dirname, "install-alpaca-cli.sh");

test("installer fetches and verifies the pinned official Alpaca CLI", { timeout: 30_000 }, () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "alpaca-cli-install-"));
  const destination = path.join(directory, "alpaca");
  execFileSync("bash", [script, destination], {
    env: { ...process.env, ALPACA_CLI_INSTALL_VERIFY_ONLY: "true" }, stdio: "pipe",
  });
  assert.equal(fs.statSync(destination).mode & 0o111, 0o111);
  assert.equal(fs.readFileSync(destination).subarray(0, 4).toString("hex"), "7f454c46");
});

test("nixpacks installs the pinned CLI before starting the service", () => {
  const config = fs.readFileSync(path.join(__dirname, "..", "nixpacks.toml"), "utf8");
  const dockerignore = fs.readFileSync(path.join(__dirname, "..", ".dockerignore"), "utf8");
  assert.match(config, /scripts\/install-alpaca-cli\.sh \.bin\/alpaca/);
  assert.match(config, /ALPACA_CLI="\/app\/\.bin\/alpaca"/);
  assert.match(dockerignore, /!scripts\/install-alpaca-cli\.sh/);
  assert.match(dockerignore, /!scripts\/cloud-investment-agent-runner\.js/);
  assert.match(dockerignore, /!scripts\/investment-cutover-state\.js/);
});
