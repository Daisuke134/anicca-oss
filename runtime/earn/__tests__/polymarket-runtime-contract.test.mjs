import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const root = resolve(import.meta.dirname, "../../..");
const runtimeEnv = join(root, "runtime/earn/polymarket-runtime-env.sh");
const decisionWrapper = join(root, "skills/earn/polymarket-trade/run_decision_loop.sh");
const liveWrapper = join(root, "skills/earn/polymarket-trade/run.sh");

test("live wrapper uses only the managed node for the genome", () => {
  const source = readFileSync(liveWrapper, "utf8");
  assert.match(source, /"\$LIFE_MANAGER_NODE" "\$GENOME_LIB" --maybe-mutate/);
  assert.doesNotMatch(source, /command -v node|\bnode "\$GENOME_LIB"/);
});

function fixture() {
  const home = mkdtempSync(join(tmpdir(), "pm-runtime-"));
  const bin = join(home, "bin");
  const wallet = join(home, "wallet");
  const state = join(home, "state");
  const envFile = join(home, "life-manager.env");
  const python = join(bin, "python");
  const node = join(bin, "node");
  mkdirSync(bin);
  mkdirSync(wallet);
  writeFileSync(envFile, "");
  writeFileSync(node, `#!/bin/sh\nprintf '0x%s\\n' '${"a".repeat(64)}'\n`);
  writeFileSync(python, `#!/bin/sh\nif [ "$1" = -c ]; then exec /usr/bin/python3 "$@"; fi\nprintf '__CALL__%s\\n' "$*"\nprintf '__BORROWED__%s\\n' "\${BORROWED_KEY-}"\nprintf '__BASE__%s\\n' "\${BASE_CHAIN_WALLET_KEY-}"\nprintf '__BLOCKRUN__%s\\n' "\${BLOCKRUN_WALLET_KEY-}"\n`);
  chmodSync(node, 0o755);
  chmodSync(python, 0o755);
  return { home, wallet, state, envFile, python, node };
}

test("shared Polymarket runtime owns Python, state, wallet, and signer", () => {
  const f = fixture();
  writeFileSync(f.envFile, [
    `LIFE_MANAGER_PYTHON=/borrowed/python`,
    `LIFE_MANAGER_NODE=/borrowed/node`,
    `LIFE_MANAGER_STATE_ROOT=${join(f.home, "borrowed-state")}`,
    `PM_KILL_SWITCH=${join(f.home, "borrowed-kill")}`,
    `LIFE_MANAGER_EARN_LEDGER_PATH=${join(f.home, "borrowed-ledger")}`,
    `LIFE_MANAGER_RELAYER_CACHE=${join(f.home, "borrowed-relayer")}`,
    `LIFE_MANAGER_WALLET_HOME=${f.wallet}`,
    `PM_DEPOSIT_WALLET=0x${"b".repeat(40)}`,
  ].join("\n") + "\n");
  const result = spawnSync("bash", ["-c", `source "${runtimeEnv}" && printf '%s\\n' "$LIFE_MANAGER_STATE_ROOT" "$LIFE_MANAGER_EARN_LEDGER_PATH" "$PM_KILL_SWITCH" "$LIFE_MANAGER_RELAYER_CACHE" "$LIFE_MANAGER_PYTHON" "$LIFE_MANAGER_NODE" "$ANICCA_HOME" "$POLYGON_WALLET_PRIVATE_KEY" "$BLOCKRUN_WALLET_KEY" "\${BASE_CHAIN_WALLET_KEY-}" "\${BORROWED_KEY-}"`], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: f.home,
      LIFE_MANAGER_ENV_FILE: f.envFile,
      LIFE_MANAGER_PYTHON: f.python,
      LIFE_MANAGER_NODE: f.node,
      LIFE_MANAGER_STATE_ROOT: f.state,
      LIFE_MANAGER_WALLET_HOME: f.wallet,
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
      PKVAR: "BORROWED_KEY",
      BORROWED_KEY: `0x${"c".repeat(64)}`,
      BASE_CHAIN_WALLET_KEY: `0x${"e".repeat(64)}`,
      POLYGON_WALLET_PRIVATE_KEY: `0x${"d".repeat(64)}`,
    },
  });
  assert.equal(result.status, 0, result.stderr);
  const lines = result.stdout.trimEnd().split("\n");
  assert.equal(lines[0], realpathSync(f.home) + "/state");
  assert.equal(lines[1], realpathSync(f.home) + "/.local/state/life-manager/earn/earn-ledger.jsonl");
  assert.equal(lines[2], realpathSync(f.home) + "/.local/state/life-manager/polymarket/KILL");
  assert.equal(lines[3], realpathSync(f.home) + "/state/relayer-apikey");
  assert.equal(lines[4], f.python);
  assert.equal(lines[5], f.node);
  assert.equal(lines[6], f.wallet);
  assert.equal(lines[7], `0x${"a".repeat(64)}`);
  assert.equal(lines[8], `0x${"a".repeat(64)}`);
  assert.doesNotMatch(result.stdout, new RegExp(`0x${"c".repeat(64)}`));
  assert.doesNotMatch(result.stdout, new RegExp(`0x${"e".repeat(64)}`));
});

test("decision wrapper executes exact repository script with managed Python", () => {
  const f = fixture();
  const result = spawnSync("bash", [decisionWrapper], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: f.home,
      LIFE_MANAGER_ENV_FILE: f.envFile,
      LIFE_MANAGER_PYTHON: f.python,
      LIFE_MANAGER_NODE: f.node,
      LIFE_MANAGER_STATE_ROOT: f.state,
      LIFE_MANAGER_WALLET_HOME: f.wallet,
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
      BORROWED_KEY: `0x${"c".repeat(64)}`,
      BASE_CHAIN_WALLET_KEY: `0x${"e".repeat(64)}`,
      PKVAR: "BORROWED_KEY",
    },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /__CALL__decision_loop\.py/);
  assert.match(result.stdout, /^__BORROWED__$/m);
  assert.match(result.stdout, /^__BASE__$/m);
  assert.match(result.stdout, new RegExp(`__BLOCKRUN__0x${"a".repeat(64)}`));
});

test("reserved PKVAR cannot delete the resolved signer", () => {
  const f = fixture();
  const result = spawnSync("bash", ["-c", `source "${runtimeEnv}" && printf '%s\\n' "$POLYGON_WALLET_PRIVATE_KEY" "$BLOCKRUN_WALLET_KEY"`], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: f.home,
      LIFE_MANAGER_ENV_FILE: f.envFile,
      LIFE_MANAGER_PYTHON: f.python,
      LIFE_MANAGER_NODE: f.node,
      LIFE_MANAGER_STATE_ROOT: f.state,
      LIFE_MANAGER_WALLET_HOME: f.wallet,
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
      PKVAR: "POLYGON_WALLET_PRIVATE_KEY",
      POLYGON_WALLET_PRIVATE_KEY: `0x${"d".repeat(64)}`,
    },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.stdout.trim().split("\n"), [
    `0x${"a".repeat(64)}`,
    `0x${"a".repeat(64)}`,
  ]);
});

test("shared runtime rejects state resolving inside the repository", () => {
  const f = fixture();
  const result = spawnSync("bash", ["-c", `source "${runtimeEnv}"`], {
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: f.home,
      LIFE_MANAGER_ENV_FILE: f.envFile,
      LIFE_MANAGER_PYTHON: f.python,
      LIFE_MANAGER_NODE: f.node,
      LIFE_MANAGER_STATE_ROOT: join(root, "runtime/earn/state"),
      LIFE_MANAGER_WALLET_HOME: f.wallet,
      PM_DEPOSIT_WALLET: `0x${"b".repeat(40)}`,
    },
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /LIFE_MANAGER_STATE_ROOT must resolve outside the repository/);
});
