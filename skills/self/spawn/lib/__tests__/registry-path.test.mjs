// VCSDD anicca-agent-spawn, Phase 2a (RED). REQ-103/REQ-105 registry-path.mjs:
// CITIZENS_REGISTRY_PATH (durable, out-of-git-tree, resolveStateDir-based) and COORDINATOR_HOME
// (os.homedir()-derived, Effectful Shell — resolves FIND-1002's "Pure Core" mislabeling).
// registry-path.mjs does not exist yet — expected to fail at import time (module-not-found).
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { CITIZENS_REGISTRY_PATH, COORDINATOR_HOME } from "../registry-path.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// skills/self/spawn/lib/__tests__ -> up 4 levels -> the the canonical checkout repo root
const REPO_ROOT = path.resolve(__dirname, "../../../../..");

test("PROP-105k structural: CITIZENS_REGISTRY_PATH resolves outside the the canonical checkout git working tree", () => {
  assert.ok(
    !CITIZENS_REGISTRY_PATH.startsWith(REPO_ROOT + path.sep),
    `CITIZENS_REGISTRY_PATH (${CITIZENS_REGISTRY_PATH}) must not live inside the git working tree (${REPO_ROOT})`
  );
  assert.equal(path.basename(CITIZENS_REGISTRY_PATH), "citizens.json");
});

test("PROP-105k structural: CITIZENS_REGISTRY_PATH is never /tmp-rooted (reuses resolveStateDir's fail-closed guard)", () => {
  assert.ok(!/^\/(private\/)?tmp(\/|$)/.test(CITIZENS_REGISTRY_PATH));
});

test("PROP-105m Tier 0/1: COORDINATOR_HOME equals a direct os.homedir() call taken in this SAME process", () => {
  assert.equal(COORDINATOR_HOME, os.homedir());
});

test("PROP-105m Tier 2 (live, no mock): COORDINATOR_HOME genuinely tracks the real process environment — a child process launched with a DIFFERENT real HOME resolves its OWN os.homedir(), never the parent's cached value", () => {
  const fixtureHome = fs.mkdtempSync(path.join(os.tmpdir(), "anicca-coordinator-home-fixture-"));
  try {
    const modulePath = pathToFileURL(path.resolve(__dirname, "../registry-path.mjs")).href;
    const script = `import(${JSON.stringify(modulePath)}).then((m) => { process.stdout.write(m.COORDINATOR_HOME); });`;
    const out = execFileSync(process.execPath, ["--input-type=module", "-e", script], {
      env: { ...process.env, HOME: fixtureHome },
    }).toString();
    assert.equal(out, fixtureHome);
    assert.notEqual(out, COORDINATOR_HOME);
  } finally {
    fs.rmSync(fixtureHome, { recursive: true, force: true });
  }
});

test("PROP-105k Tier 2 (live, no mock, scoped to worktree add|remove — see sprint-1 report for the git-checkout/pull scope note): a real `git worktree add`/`git worktree remove` cycle leaves an isolated fixture registry byte-identical", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.homedir(), ".lm-citizens-registry-test-"));
  const previousStateDir = process.env.ANICCA_STATE_DIR;
  process.env.ANICCA_STATE_DIR = stateDir;
  const modulePath = pathToFileURL(path.resolve(__dirname, "../registry-path.mjs")).href;
  const isolated = await import(`${modulePath}?test=${Date.now()}`);
  const registryPath = isolated.CITIZENS_REGISTRY_PATH;
  const fixtureContent = JSON.stringify([{ id: "sprint1-red-phase-fixture-" + Date.now() }]);
  fs.writeFileSync(registryPath, fixtureContent);
  const wtDir = fs.mkdtempSync(path.join(os.tmpdir(), "anicca-registry-path-worktree-"));
  fs.rmdirSync(wtDir); // git worktree add requires the target path not to already exist
  try {
    execFileSync("git", ["worktree", "add", "--detach", wtDir], { cwd: REPO_ROOT });
    execFileSync("git", ["worktree", "remove", "--force", wtDir], { cwd: REPO_ROOT });
    assert.equal(fs.readFileSync(registryPath, "utf8"), fixtureContent);
  } finally {
    fs.rmSync(wtDir, { recursive: true, force: true });
    fs.rmSync(stateDir, { recursive: true, force: true });
    if (previousStateDir === undefined) delete process.env.ANICCA_STATE_DIR;
    else process.env.ANICCA_STATE_DIR = previousStateDir;
  }
});
