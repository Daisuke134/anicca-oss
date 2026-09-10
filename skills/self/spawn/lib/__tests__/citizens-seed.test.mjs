// VCSDD anicca-agent-spawn, Phase 5 (Formal Hardening) — proves PROP-105a/PROP-105c/PROP-105d/
// PROP-105f/PROP-105h against the real, git-tracked seed template
// `__REPO_ROOT__/skills/self/spawn/registry/citizens.seed.json` (created this session, closing a
// missing-deliverable gap: `specs/verification-architecture.md`'s own Purity Boundary Map declared
// this file as part of the sprint, but it did not exist in the tree until now).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const SEED_PATH = new URL("../../registry/citizens.seed.json", import.meta.url);
const seed = JSON.parse(fs.readFileSync(SEED_PATH, "utf8"));

test("a clean installation has no named, human, Franklin, sibling, wallet, or home fallback", () => {
  assert.ok(Array.isArray(seed));
  assert.deepEqual(seed, []);
});
