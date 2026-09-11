import assert from "node:assert/strict";
import childProcess from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const earnDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const reinvest = fs.readFileSync(path.join(earnDir, "reinvest.sh"), "utf8");

test("reinvest executes the repository-owned yield entrypoint", () => {
  const entrypoint = path.join(earnDir, "execute-yield.mjs");

  assert.ok(fs.existsSync(entrypoint), "the release must contain execute-yield.mjs");
  assert.doesNotMatch(reinvest, /\.blockrun\/skills\/earn/);
  assert.match(reinvest, /SCRIPT_DIR=\$\(CDPATH= cd -- "\$\(dirname -- "\$0"\)" && pwd\)/);
  assert.match(reinvest, /"\$\{LIFE_MANAGER_NODE:-node\}" "\$SCRIPT_DIR\/execute-yield\.mjs"/);
  assert.doesNotMatch(reinvest, /export ANICCA_HOME=\/Users\//);
});

test("reinvest fails before Node unless its wallet home is explicitly configured", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-reinvest-test-"));
  try {
    const result = childProcess.spawnSync("bash", [path.join(earnDir, "reinvest.sh")], {
      encoding: "utf8",
      env: {
        HOME: home,
        LIFE_MANAGER_ENV_FILE: path.join(home, "missing.env"),
        PATH: "/usr/bin:/bin",
      },
    });
    assert.equal(result.status, 78);
    assert.match(result.stderr, /REINVEST_ANICCA_HOME must be an absolute path/);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("reinvest rejects a relative wallet home before Node", () => {
  const result = childProcess.spawnSync("bash", [path.join(earnDir, "reinvest.sh")], {
    encoding: "utf8",
    env: {
      HOME: "/tmp",
      LIFE_MANAGER_ENV_FILE: "/nonexistent/life-manager.env",
      PATH: "/usr/bin:/bin",
      REINVEST_ANICCA_HOME: "relative/wallet",
    },
  });
  assert.equal(result.status, 78);
  assert.match(result.stderr, /REINVEST_ANICCA_HOME must be an absolute path/);
});

test("reinvest child cannot inherit another loop's signing-key overrides", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-reinvest-test-"));
  try {
    const capture = path.join(home, "child-env.txt");
    const nodeStub = path.join(home, "node-stub.sh");
    fs.writeFileSync(nodeStub, [
      "#!/bin/bash",
      "printf 'ANICCA_HOME=%s\\n' \"${ANICCA_HOME-}\" > \"${REINVEST_TEST_CAPTURE}\"",
      "printf 'PKVAR=%s\\n' \"${PKVAR-}\" >> \"${REINVEST_TEST_CAPTURE}\"",
      "printf 'BLOCKRUN_WALLET_KEY=%s\\n' \"${BLOCKRUN_WALLET_KEY-}\" >> \"${REINVEST_TEST_CAPTURE}\"",
      "printf 'ANICCA_EVM_PRIVATE_KEY=%s\\n' \"${ANICCA_EVM_PRIVATE_KEY-}\" >> \"${REINVEST_TEST_CAPTURE}\"",
      "printf 'BORROWED_KEY=%s\\n' \"${BORROWED_KEY-}\" >> \"${REINVEST_TEST_CAPTURE}\"",
      "printf '{\"ok\":true}\\n'",
      "",
    ].join("\n"));
    fs.chmodSync(nodeStub, 0o700);
    const walletHome = path.join(home, "wallet-owner");
    fs.mkdirSync(walletHome);
    const result = childProcess.spawnSync("bash", [path.join(earnDir, "reinvest.sh")], {
      encoding: "utf8",
      env: {
        HOME: home,
        LIFE_MANAGER_ENV_FILE: path.join(home, "missing.env"),
        LIFE_MANAGER_NODE: nodeStub,
        REINVEST_ANICCA_HOME: walletHome,
        REINVEST_TEST_CAPTURE: capture,
        PATH: "/usr/bin:/bin",
        PKVAR: "BORROWED_KEY",
        BORROWED_KEY: "borrowed-secret",
        BLOCKRUN_WALLET_KEY: "borrowed-secret",
        ANICCA_EVM_PRIVATE_KEY: "borrowed-secret",
      },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(
      fs.readFileSync(capture, "utf8"),
      `ANICCA_HOME=${walletHome}\nPKVAR=\nBLOCKRUN_WALLET_KEY=\nANICCA_EVM_PRIVATE_KEY=\nBORROWED_KEY=\n`,
    );
    assert.doesNotMatch(result.stdout + result.stderr, /borrowed-secret/);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("legacy yield entrypoint delegates to the one canonical repository implementation", () => {
  const entrypoint = path.join(earnDir, "execute-yield.mjs");
  assert.ok(fs.existsSync(entrypoint), "the release must contain execute-yield.mjs");

  const source = fs.readFileSync(entrypoint, "utf8");
  assert.match(source, /import\("\.\.\/\.\.\/skills\/earn\/execute-yield\.mjs"\)/);
  const canonical = fs.readFileSync(path.resolve(earnDir, "../../skills/earn/execute-yield.mjs"), "utf8");
  assert.match(canonical, /from "\.\/lib\/resolve-identity\.mjs"/);
  assert.match(canonical, /from "\.\/lib\/cost-basis\.mjs"/);
  assert.match(canonical, /from "\.\/lib\/deposit-guard\.mjs"/);
  assert.doesNotMatch(source, /\.blockrun\/skills/);
  assert.doesNotMatch(canonical, /\.blockrun\/skills/);
});
