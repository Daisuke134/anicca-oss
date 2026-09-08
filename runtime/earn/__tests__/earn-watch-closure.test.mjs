import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const root = resolve(import.meta.dirname, "../../..");
const watch = join(root, "runtime/earn/earn-watch.sh");
const trade = join(root, "skills/earn/polymarket-trade/run.sh");

test("earn-watch uses only install-owned identity, state, Python, and redeem paths", () => {
  const work = mkdtempSync(join(tmpdir(), "earn-watch-"));
  const walletHome = join(work, "wallet");
  const bin = join(work, "bin");
  const curlArgs = join(work, "curl-args");
  const python = join(bin, "python");
  const pythonArgs = join(work, "python-args");
  const pythonKey = join(work, "python-key");
  const borrowedKey = join(work, "borrowed-key");
  const envFile = join(work, "life-manager.env");
  mkdirSync(join(walletHome, ".automaton"), { recursive: true });
  mkdirSync(bin);
  writeFileSync(envFile, "");
  writeFileSync(join(walletHome, ".automaton", "wallet.json"), JSON.stringify({ privateKey: `0x${"a".repeat(64)}` }));
  writeFileSync(join(bin, "curl"), "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CURL_ARGS_LOG\"\ncase \"$*\" in *data-api*) echo '[{\"redeemable\":true}]' ;; *) echo '{\"result\":\"0x0\"}' ;; esac\n");
  writeFileSync(python, "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$PYTHON_ARGS_LOG\"\nprintf '%s\\n' \"$POLYGON_WALLET_PRIVATE_KEY\" > \"$PYTHON_KEY_LOG\"\nprintf '%s\\n' \"${BORROWED_KEY-}\" > \"$BORROWED_KEY_LOG\"\n");
  const chmod = spawnSync("chmod", ["755", join(bin, "curl")]);
  assert.equal(chmod.status, 0);
  assert.equal(spawnSync("chmod", ["755", python]).status, 0);

  const result = spawnSync("bash", [watch], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      LIFE_MANAGER_WALLET_HOME: walletHome,
      LIFE_MANAGER_PYTHON: python,
      LIFE_MANAGER_STATE_ROOT: join(work, "state"),
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
      EARN_WATCH_PAYEE: `0x${"d".repeat(40)}`,
      CURL_ARGS_LOG: curlArgs,
      PYTHON_ARGS_LOG: pythonArgs,
      PYTHON_KEY_LOG: pythonKey,
      BORROWED_KEY_LOG: borrowedKey,
      LIFE_MANAGER_ENV_FILE: envFile,
      HOME: work,
      PKVAR: "BORROWED_KEY",
      BORROWED_KEY: `0x${"e".repeat(64)}`,
      POLYGON_WALLET_PRIVATE_KEY: `0x${"c".repeat(64)}`,
    },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(readFileSync(pythonArgs, "utf8"), new RegExp(`${root.replaceAll("/", "\\/")}\\/skills\\/earn\\/polymarket-trade\\/redeem\\.py`));
  assert.doesNotMatch(result.stdout, /\.blockrun|\.anicca-founder/);
  assert.match(readFileSync(curlArgs, "utf8"), new RegExp(`user=0x${"b".repeat(40)}`));
  assert.equal(readFileSync(pythonKey, "utf8").trim(), `0x${"a".repeat(64)}`);
  assert.equal(readFileSync(borrowedKey, "utf8").trim(), "");
});

test("earn-watch rejects an invalid install configuration before an external action", () => {
  const work = mkdtempSync(join(tmpdir(), "earn-watch-invalid-"));
  const walletHome = join(work, "wallet");
  const envFile = join(work, "life-manager.env");
  mkdirSync(walletHome);
  writeFileSync(envFile, "");
  const result = spawnSync("bash", [watch], {
    encoding: "utf8",
    env: { ...process.env, HOME: work, LIFE_MANAGER_WALLET_HOME: walletHome, PM_DEPOSIT_WALLET: "not-an-address", EARN_WATCH_PAYEE: `0x${"d".repeat(40)}`, LIFE_MANAGER_ENV_FILE: envFile },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PM_DEPOSIT_WALLET must be a 0x-prefixed 40-hex address/);
});

test("earn-watch rejects a relative wallet home before an external action", () => {
  const work = mkdtempSync(join(tmpdir(), "earn-watch-relative-"));
  const envFile = join(work, "life-manager.env");
  writeFileSync(envFile, "");
  const result = spawnSync("bash", [watch], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: work,
      LIFE_MANAGER_WALLET_HOME: ".",
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
      EARN_WATCH_PAYEE: `0x${"d".repeat(40)}`,
      LIFE_MANAGER_ENV_FILE: envFile,
    },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /existing absolute LIFE_MANAGER_WALLET_HOME/);
});

test("earn-watch rejects state inside the repository before an external action", () => {
  const work = mkdtempSync(join(tmpdir(), "earn-watch-state-"));
  const walletHome = join(work, "wallet");
  const envFile = join(work, "life-manager.env");
  mkdirSync(walletHome);
  writeFileSync(envFile, "");
  const result = spawnSync("bash", [watch], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: work,
      LIFE_MANAGER_WALLET_HOME: walletHome,
      LIFE_MANAGER_STATE_ROOT: join(root, "runtime/earn/state"),
      LIFE_MANAGER_PYTHON: "/bin/echo",
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
      EARN_WATCH_PAYEE: `0x${"d".repeat(40)}`,
      LIFE_MANAGER_ENV_FILE: envFile,
    },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /resolve outside the repository/);
});

test("earn-watch rejects relative state and KILL configuration", () => {
  const work = mkdtempSync(join(tmpdir(), "earn-watch-relative-state-"));
  const walletHome = join(work, "wallet");
  const envFile = join(work, "life-manager.env");
  mkdirSync(walletHome);
  writeFileSync(envFile, "");
  const commonEnv = {
    ...process.env,
    HOME: work,
    LIFE_MANAGER_WALLET_HOME: walletHome,
    LIFE_MANAGER_PYTHON: "/usr/bin/python3",
    PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
    EARN_WATCH_PAYEE: `0x${"d".repeat(40)}`,
    LIFE_MANAGER_ENV_FILE: envFile,
  };
  let result = spawnSync("bash", [watch], { encoding: "utf8", cwd: work, env: { ...commonEnv, LIFE_MANAGER_STATE_ROOT: "state" } });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /LIFE_MANAGER_STATE_ROOT must be absolute/);
  result = spawnSync("bash", [watch], { encoding: "utf8", cwd: work, env: { ...commonEnv, LIFE_MANAGER_STATE_ROOT: join(work, "state"), PM_KILL_SWITCH: "KILL" } });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PM_KILL_SWITCH must be absolute/);
});

test("earn-watch rejects state and KILL symlinks that resolve inside the repository", () => {
  const work = mkdtempSync(join(tmpdir(), "earn-watch-symlink-"));
  const walletHome = join(work, "wallet");
  const envFile = join(work, "life-manager.env");
  mkdirSync(walletHome);
  writeFileSync(envFile, "");
  const commonEnv = {
    ...process.env,
    HOME: work,
    LIFE_MANAGER_WALLET_HOME: walletHome,
    LIFE_MANAGER_PYTHON: "/usr/bin/python3",
    PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
    EARN_WATCH_PAYEE: `0x${"d".repeat(40)}`,
    LIFE_MANAGER_ENV_FILE: envFile,
  };
  const stateLink = join(work, "state-link");
  symlinkSync(join(root, "runtime/earn"), stateLink);
  let result = spawnSync("bash", [watch], {
    encoding: "utf8",
    env: { ...commonEnv, LIFE_MANAGER_STATE_ROOT: stateLink },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /LIFE_MANAGER_STATE_ROOT must resolve outside the repository/);

  const killLink = join(work, "kill-link");
  symlinkSync(join(root, "runtime/earn/forbidden-kill"), killLink);
  result = spawnSync("bash", [watch], {
    encoding: "utf8",
    env: { ...commonEnv, LIFE_MANAGER_STATE_ROOT: join(work, "state"), PM_KILL_SWITCH: killLink },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PM_KILL_SWITCH must resolve outside the repository/);
});

test("Polymarket trade reads the same mutable kill switch that redeem writes", () => {
  const work = mkdtempSync(join(tmpdir(), "pm-kill-switch-"));
  const state = join(work, "state");
  const kill = join(work, ".local/state/life-manager/polymarket/KILL");
  mkdirSync(resolve(kill, ".."), { recursive: true });
  writeFileSync(kill, "halt\n");
  const result = spawnSync("bash", [trade], {
    encoding: "utf8",
    env: { ...process.env, HOME: work, POLYMARKET_STATE_ROOT: state },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(readFileSync(join(state, "pm-trade.trace.jsonl"), "utf8"), /"reason":"kill-switch"/);
});

test("Polymarket trade rejects a KILL path that resolves inside the repository", () => {
  const work = mkdtempSync(join(tmpdir(), "pm-kill-switch-invalid-"));
  const result = spawnSync("bash", [trade], {
    encoding: "utf8",
    env: { ...process.env, HOME: work, PM_KILL_SWITCH: join(root, "runtime/earn/KILL") },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PM_KILL_SWITCH must resolve outside the repository/);
});

test("Polymarket trade rejects repo-contained KILL through a symlinked repository path", () => {
  const work = mkdtempSync(join(tmpdir(), "pm-kill-repo-link-"));
  const repoLink = join(work, "repo-link");
  symlinkSync(root, repoLink);
  const result = spawnSync("bash", [join(repoLink, "skills/earn/polymarket-trade/run.sh")], {
    encoding: "utf8",
    cwd: work,
    env: { ...process.env, HOME: work, PM_KILL_SWITCH: join(root, "runtime/earn/KILL") },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PM_KILL_SWITCH must resolve outside the repository/);
});
