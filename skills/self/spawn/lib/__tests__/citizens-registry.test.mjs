// VCSDD anicca-agent-spawn, Phase 2a (RED). REQ-105 one-time bootstrap of CITIZENS_REGISTRY_PATH —
// a SINGLE atomic POSIX exclusive-create (fs.open(path, "wx")), never a check-then-act "exists? then
// copy" pair. citizens-registry.mjs does not exist yet — expected to fail at import (module-not-found).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { bootstrapCitizensRegistry, ensureCitizensRegistry, CITIZENS_SEED_PATH } from "../citizens-registry.mjs";

function tmpRegistryPath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "anicca-citizens-registry-test-"));
  return path.join(dir, "citizens.json");
}

test("PROP-105l Tier 0/2: a single first-access bootstrap creates the file verbatim from the seed content", async () => {
  const registryPath = tmpRegistryPath();
  const seed = JSON.stringify([{ id: "a" }]);
  const result = await bootstrapCitizensRegistry({ registryPath, seedContent: seed });
  assert.equal(result.created, true);
  assert.equal(fs.readFileSync(registryPath, "utf8"), seed);
});

test("PROP-105l: two concurrent first-access callers -> exactly ONE succeeds, the other reads existing content, never overwrites/double-writes", async () => {
  const registryPath = tmpRegistryPath();
  const seed = JSON.stringify([{ id: "a" }]);
  const [r1, r2] = await Promise.all([
    bootstrapCitizensRegistry({ registryPath, seedContent: seed }),
    bootstrapCitizensRegistry({ registryPath, seedContent: seed }),
  ]);
  const createdCount = [r1, r2].filter((r) => r.created).length;
  assert.equal(createdCount, 1);
  assert.equal(fs.readFileSync(registryPath, "utf8"), seed);
});

test("PROP-105l: a late bootstrap attempt racing a real, already-completed REQ-305 append fails EEXIST and never overwrites the appended record", async () => {
  const registryPath = tmpRegistryPath();
  const seed = JSON.stringify([{ id: "seedOnly" }]);
  await bootstrapCitizensRegistry({ registryPath, seedContent: seed });
  // Simulate a real REQ-305 append happening after the bootstrap winner's write.
  const appended = JSON.stringify([{ id: "seedOnly" }, { id: "newChild" }]);
  fs.writeFileSync(registryPath, appended);
  const late = await bootstrapCitizensRegistry({ registryPath, seedContent: seed });
  assert.equal(late.created, false);
  assert.equal(fs.readFileSync(registryPath, "utf8"), appended);
});

test("PROP-105l structural: the bootstrap implementation performs no separate fs.existsSync/fs.stat check preceding a separate write call", () => {
  const src = fs.readFileSync(new URL("../citizens-registry.mjs", import.meta.url), "utf8");
  assert.ok(!/existsSync|fs\.stat\(/.test(src), "bootstrap must use a single exclusive-create, not check-then-act");
  assert.ok(/wx/.test(src), "bootstrap must use POSIX O_CREAT|O_EXCL ('wx') exclusive-create");
});

// REQ-105's real one-time-bootstrap caller: ensureCitizensRegistry reads the REAL, git-tracked
// citizens.seed.json (CITIZENS_SEED_PATH, no override) -- not a synthetic fixture -- and writes its
// verbatim content into the durable registryPath via bootstrapCitizensRegistry.
test("ensureCitizensRegistry: bootstraps the durable registry from the REAL citizens.seed.json, byte-identical", async () => {
  const registryPath = tmpRegistryPath();
  const realSeedContent = fs.readFileSync(CITIZENS_SEED_PATH, "utf8");
  const result = await ensureCitizensRegistry({ registryPath });
  assert.equal(result.created, true);
  assert.equal(fs.readFileSync(registryPath, "utf8"), realSeedContent);
  const parsed = JSON.parse(fs.readFileSync(registryPath, "utf8"));
  assert.deepEqual(parsed, []); // AE-UX-02: provisioning creates the first bound citizen; no named fallback.
});

// PROP-105j: ensureCitizensRegistry only ever READS citizens.seed.json (fs.readFile) -- it never
// opens the seed path itself for writing.
test("PROP-105j: ensureCitizensRegistry never opens CITIZENS_SEED_PATH for writing", async () => {
  const registryPath = tmpRegistryPath();
  const before = fs.readFileSync(CITIZENS_SEED_PATH, "utf8");
  await ensureCitizensRegistry({ registryPath });
  const after = fs.readFileSync(CITIZENS_SEED_PATH, "utf8");
  assert.equal(before, after, "citizens.seed.json must be byte-identical before/after bootstrap (read-only)");
});
