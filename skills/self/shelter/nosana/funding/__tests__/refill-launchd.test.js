import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../../../..");
const runnerPath = path.join(repoRoot, "bin", "citizen-refill-launchd");
test("the scheduled refill runner loads the managed environment and executes the real live rail", () => {
  const runner = fs.readFileSync(runnerPath, "utf8");
  assert.match(runner, /LIFE_MANAGER_ENV_FILE=.*\.env/);
  assert.match(runner, /LIFE_MANAGER_WALLET_HOME/);
  assert.match(runner, /agent-economy\/instance/);
  assert.match(runner, /exec "\$NODE_BIN" "\$REPO_ROOT\/bin\/citizen-refill" --live/);
  assert.doesNotMatch(runner, /openclaw|hermes|\/opt\/homebrew/);
  assert.doesNotMatch(runner, /--dry/);
});
