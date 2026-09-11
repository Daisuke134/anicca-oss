import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const helper = path.resolve(here, "../load-instance-env.sh");

function run(env) {
  return spawnSync("bash", ["-c", `. "${helper}"\nprintf '%s\\n' "ANICCA_HOME=$ANICCA_HOME" "ONLY_LM=$ONLY_LM" "LEGACY=$LEGACY"`], {
    env,
    encoding: "utf8",
  });
}

test("loads only the canonical Life Manager env and preserves instance identity", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-env-"));
  const lmEnv = path.join(home, "instance.env");
  const callerHome = path.join(home, "citizen");
  fs.mkdirSync(path.join(home, ".hermes"), { recursive: true });
  fs.writeFileSync(lmEnv, `ANICCA_HOME=${path.join(home, "wrong")}\nONLY_LM=yes\n`);
  fs.writeFileSync(path.join(home, ".hermes", ".env"), "LEGACY=must-not-load\n");

  const result = run({
    HOME: home,
    PATH: process.env.PATH,
    ANICCA_HOME: callerHome,
    LIFE_MANAGER_ENV_FILE: lmEnv,
    ONLY_LM: "",
    LEGACY: "",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`^ANICCA_HOME=${callerHome}$`, "m"));
  assert.match(result.stdout, /^ONLY_LM=yes$/m);
  assert.match(result.stdout, /^LEGACY=$/m);
  fs.rmSync(home, { recursive: true, force: true });
});

test("missing canonical env is a no-op", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "lm-env-"));
  const callerHome = path.join(home, "citizen");
  const result = run({ HOME: home, PATH: process.env.PATH, ANICCA_HOME: callerHome, ONLY_LM: "", LEGACY: "" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`^ANICCA_HOME=${callerHome}$`, "m"));
  fs.rmSync(home, { recursive: true, force: true });
});
