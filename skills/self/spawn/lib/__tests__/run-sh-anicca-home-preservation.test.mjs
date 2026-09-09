// Regression test for the 2026-07-08 production incident: Franklin's real wake-cycle daemon
// (ai.anicca.franklin-loop) picked self/spawn for real, the money gate said eligible:true, but the
// spawn attempt then failed with "no resolvable per-instance identity (ANICCA_HOME/HOME) -- cannot
// determine drivingCitizenWallet". Root cause (confirmed by direct reproduction of the exact real
// subprocess chain): run.sh's own env-loading preamble does
//   set -a
//   [ -f "$HOME/.hermes/.env" ]   && . "$HOME/.hermes/.env"
//   [ -f "$HOME/.local/state/life-manager/.env" ] && . "$HOME/.local/state/life-manager/.env"
//   set +a
// -- `$HOME` is the real, SHARED macOS home (all instances on one machine run as the same OS user),
// not per-instance. `$HOME/.local/state/life-manager/.env` is the OpenClaw automaton's own shared secrets file and
// always sets its OWN `ANICCA_HOME` (e.g. `~/.local/state/life-manager`). `set -a` (allexport) means
// sourcing that file's `ANICCA_HOME=...` assignment silently overwrote the CALLER's correct
// per-instance `ANICCA_HOME` (e.g. Franklin's `/home/life-manager/.blockrun`, inherited correctly from the
// launchd plist through index.mjs's buildSkillEnv) with the automaton's — so
// resolve-identity.mjs's resolveEvmPrivateKey/resolveSolanaSecret then looked in the WRONG home,
// found no wallet file there, and fail-closed exactly as designed (the resolvers themselves were
// never buggy; run.sh handed them the wrong ANICCA_HOME).
//
// This test drives the REAL, unmodified skills/self/spawn/run.sh (not a copy) through its actual
// env-loading preamble, using a throwaway HOME with its own fixture `.local/state/life-manager/.env` that sets a
// DIFFERENT ANICCA_HOME (mirroring the real production conflict) plus one shared-secret var
// (mirroring the real reason run.sh sources that file at all: Akash signing key etc.). A stand-in
// `node` shim on PATH intercepts run.sh's final `exec "$NODE" ...` call and reports which env
// survived, instead of running the real wake-gate.mjs (zero side effects: no citizens.json write,
// no spawn attempt, no money movement).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const RUN_SH_PATH = path.resolve(__dirname, "../../run.sh");

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "anicca-run-sh-env-"));
}

// A fake `node` that reports the env run.sh actually handed it, instead of running wake-gate.mjs.
const FAKE_NODE_SCRIPT = `#!/usr/bin/env bash
echo "ANICCA_HOME=$ANICCA_HOME"
echo "AKASH_KEY_NAME=$AKASH_KEY_NAME"
echo "SOME_HERMES_SECRET=$SOME_HERMES_SECRET"
`;

function makeFakeNodeBin(dir) {
  const binDir = path.join(dir, "bin");
  fs.mkdirSync(binDir, { recursive: true });
  const nodePath = path.join(binDir, "node");
  fs.writeFileSync(nodePath, FAKE_NODE_SCRIPT, { mode: 0o755 });
  return binDir;
}

test("run.sh preserves the CALLER's ANICCA_HOME across $HOME/.local/state/life-manager/.env sourcing (regression for the 2026-07-08 drivingCitizenWallet incident)", () => {
  const home = tmpDir();
  const callerAniccaHome = path.join(home, ".blockrun"); // the per-instance identity run.sh is invoked with (mirrors Franklin's real ANICCA_HOME)
  fs.mkdirSync(callerAniccaHome, { recursive: true });

  // Fixture mirrors the REAL ~/.local/state/life-manager/.env: it is a SHARED secrets file that always defines its
  // OWN ANICCA_HOME (the automaton's home) plus unrelated shared secrets (e.g. Akash signing key).
  const lifeManagerDir = path.join(home, ".local", "state", "life-manager");
  fs.mkdirSync(lifeManagerDir, { recursive: true });
  fs.writeFileSync(
    path.join(lifeManagerDir, ".env"),
    `ANICCA_HOME=${lifeManagerDir}\nAKASH_KEY_NAME=test-akash-key\n`
  );

  const fakeNodeBin = makeFakeNodeBin(home);

  const result = spawnSync(RUN_SH_PATH, [], {
    env: {
      HOME: home,
      ANICCA_HOME: callerAniccaHome,
      PATH: `${fakeNodeBin}:${process.env.PATH}`,
    },
    encoding: "utf8",
  });

  assert.equal(result.status, 0, `run.sh should exit 0; stderr: ${result.stderr}`);
  const out = result.stdout;
  assert.match(
    out,
    new RegExp(`^ANICCA_HOME=${callerAniccaHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`, "m"),
    `ANICCA_HOME must survive as the caller's ${callerAniccaHome}, not be clobbered by $HOME/.local/state/life-manager/.env's own ANICCA_HOME. Got:\n${out}`
  );
  // The whole point of sourcing $HOME/.local/state/life-manager/.env is shared secrets (Akash key, etc.) -- confirm
  // the fix didn't throw that baby out with the bathwater.
  assert.match(out, /^AKASH_KEY_NAME=test-akash-key$/m, `shared secrets from $HOME/.local/state/life-manager/.env must still flow through. Got:\n${out}`);

  fs.rmSync(home, { recursive: true, force: true });
});

// FIND-001 (fresh-adversary review, iteration 1): the original test only exercised a conflicting
// ANICCA_HOME coming from $HOME/.local/state/life-manager/.env. run.sh sources $HOME/.hermes/.env FIRST, with the
// identical `set -a` mechanism -- a conflict originating there was never independently exercised.
// This test proves the fix protects against EITHER shared file, not just the one that happens to be
// populated on this particular machine today.
test("run.sh preserves the CALLER's ANICCA_HOME across $HOME/.hermes/.env sourcing too (not just .openclaw/.env)", () => {
  const home = tmpDir();
  const callerAniccaHome = path.join(home, ".blockrun");
  fs.mkdirSync(callerAniccaHome, { recursive: true });

  // Fixture: this time the CONFLICT comes from .hermes/.env (sourced first in run.sh), with
  // .openclaw/.env absent so only the .hermes path is exercised.
  const hermesDir = path.join(home, ".hermes");
  fs.mkdirSync(hermesDir, { recursive: true });
  fs.writeFileSync(
    path.join(hermesDir, ".env"),
    `ANICCA_HOME=${path.join(home, ".hermes")}\nSOME_HERMES_SECRET=test-hermes-secret\n`
  );

  const fakeNodeBin = makeFakeNodeBin(home);

  const result = spawnSync(RUN_SH_PATH, [], {
    env: {
      HOME: home,
      ANICCA_HOME: callerAniccaHome,
      PATH: `${fakeNodeBin}:${process.env.PATH}`,
    },
    encoding: "utf8",
  });

  assert.equal(result.status, 0, `run.sh should exit 0; stderr: ${result.stderr}`);
  const out = result.stdout;
  assert.match(
    out,
    new RegExp(`^ANICCA_HOME=${callerAniccaHome.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`, "m"),
    `ANICCA_HOME must survive as the caller's ${callerAniccaHome}, not be clobbered by $HOME/.hermes/.env's own ANICCA_HOME. Got:\n${out}`
  );
  // FIND-002 (fresh-adversary review, iteration 3): the fixture set SOME_HERMES_SECRET but the
  // original assertion never checked it flowed through -- REQ-001's "other variables still pass
  // through" guarantee was only actually verified for the .openclaw/.env path, not this one.
  assert.match(out, /^SOME_HERMES_SECRET=test-hermes-secret$/m, `shared secrets from $HOME/.hermes/.env must still flow through. Got:\n${out}`);

  fs.rmSync(home, { recursive: true, force: true });
});
