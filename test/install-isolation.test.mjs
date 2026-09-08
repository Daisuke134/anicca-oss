import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdtempSync,
  readFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const INSTALLER = join(REPO_ROOT, "install.sh");

function runInstaller(home, runtime) {
  const result = spawnSync("bash", [INSTALLER], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      HOME: home,
      LIFE_MANAGER_HOME: runtime,
      LIFE_MANAGER_INSTALL_DAEMON: "0",
      LIFE_MANAGER_INSTALL_DEPS: "0",
      npm_config_cache: join(home, ".npm-cache"),
    },
    timeout: 120_000,
  });
  assert.equal(
    result.status,
    0,
    `installer failed\nstdout=${result.stdout}\nstderr=${result.stderr}`,
  );
}

test("daemon-free install stays inside LIFE_MANAGER_HOME and preserves user env on rerun", () => {
  const root = mkdtempSync(join(tmpdir(), "life-manager-install-"));
  const home = join(root, "home");
  const runtime = join(root, "runtime");

  runInstaller(home, runtime);

  assert.equal(existsSync(join(runtime, ".env")), true);
  assert.equal(existsSync(join(runtime, "identity", "genesis.md")), true);
  assert.equal(existsSync(join(runtime, "skills")), false);
  assert.equal(existsSync(join(runtime, "state", "skills", "earn")), true);
  assert.equal(existsSync(join(home, "Library", "LaunchAgents")), false);
  assert.equal(existsSync(join(home, ".anicca")), false);

  const marker = "\nINSTALL_TEST_SENTINEL=preserve-me\n";
  appendFileSync(join(runtime, ".env"), marker, "utf8");
  runInstaller(home, runtime);

  assert.equal(readFileSync(join(runtime, ".env"), "utf8").includes(marker.trim()), true);
  assert.equal(existsSync(join(home, "Library", "LaunchAgents")), false);
  assert.equal(existsSync(join(home, ".anicca")), false);
});
