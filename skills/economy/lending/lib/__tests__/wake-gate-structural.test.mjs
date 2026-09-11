// node:test — economy/lending run.sh + scripts/wake-gate.mjs + registry.json (Phase 2a RED,
// anicca-agent-lending sprint-3, REQ-117, PROP-117a/b/d). Structural/Tier-0 checks only -- no
// runtime execution of scripts/wake-gate.mjs's own runWakeGate (see the SEPARATE wake-gate.test.mjs
// for behavioral coverage). This file deliberately does NOT `import` scripts/wake-gate.mjs -- it reads
// run.sh/wake-gate.mjs/registry.json as plain text/JSON via fs, so each check below fails with a clear,
// individually-diagnosable reason (ENOENT / wrong shape / missing entry) rather than one blanket
// module-not-found crash for the whole file.
//
// RED phase: run.sh and scripts/wake-gate.mjs do not exist yet, and registry.json has no
// "economy/lending" entry yet -- every test below is written to FAIL against today's real repo state,
// and to PASS once Phase 2b lands run.sh + scripts/wake-gate.mjs + the registry.json entry exactly as
// REQ-117/PROP-117a/PROP-117b/PROP-117d specify.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { earnSkillRelPath } from "../../../../../runtime/loop/earn-slot.mjs";
import { liveSlotNames } from "../../../../../runtime/loop/prompt.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../../../../"); // the canonical checkout
const RUN_SH_PATH = path.resolve(__dirname, "../../run.sh");
const WAKE_GATE_PATH = path.resolve(__dirname, "../../scripts/wake-gate.mjs");
const REGISTRY_PATH = path.resolve(__dirname, "../../../../registry.json");

// Strips `#`/`//` line comments before grepping -- mirrors self/spawn/lib/__tests__/wake-gate.test.mjs's
// own stripLineComments convention (a header docs comment legitimately naming what NOT to do is not
// itself a live reference/call).
function stripLineComments(src, marker) {
  return src
    .split("\n")
    .map((line) => {
      const i = line.indexOf(marker);
      return i === -1 ? line : line.slice(0, i);
    })
    .join("\n");
}

// ---------------------------------------------------------------------------
// run.sh's own contract: byte-for-byte structural mirror of self/spawn/run.sh's own shape.
// ---------------------------------------------------------------------------

test("run.sh exists, is executable, and structurally mirrors self/spawn/run.sh's own shape (env-load via the shared _shared/lib/load-instance-env.sh helper, execs scripts/wake-gate.mjs, no eligibility/sizing/servicing logic of its own)", () => {
  const stat = fs.statSync(RUN_SH_PATH);
  assert.ok(stat.mode & 0o111, "run.sh must be executable");
  const code = stripLineComments(fs.readFileSync(RUN_SH_PATH, "utf8"), "#");
  assert.match(code, /SKILL_DIR="\$\(cd "\$\(dirname "\$0"\)" && pwd\)"/, "run.sh must resolve its own SKILL_DIR exactly like self/spawn/run.sh");
  // anicca-spawn-identity-resolution-fix REQ-003/FIND-004: env-loading now delegates to the ONE
  // shared helper (never a hand-copied `set -a`/`.openclaw/.env` inline block per script) so this
  // parity claim is enforced by construction instead of needing independent re-verification per file.
  assert.match(code, /_shared\/lib\/load-instance-env\.sh/, "run.sh must delegate env-loading to the shared _shared/lib/load-instance-env.sh helper, exactly like self/spawn/run.sh");
  const sharedHelperCode = fs.readFileSync(path.resolve(REPO_ROOT, "skills/_shared/lib/load-instance-env.sh"), "utf8");
  assert.match(sharedHelperCode, /set -a/, "the shared helper must load env under set -a");
  assert.match(sharedHelperCode, /set \+a/, "the shared helper must close the env-load block with set +a");
  assert.match(sharedHelperCode, /LIFE_MANAGER_ENV_FILE/, "the shared helper must load the canonical Life Manager env");
  assert.doesNotMatch(sharedHelperCode, /\.hermes\/\.env|\.openclaw\/\.env/, "the shared helper must not read a legacy harness env");
  assert.match(code, /exec\s+"\$NODE"\s+"\$SKILL_DIR\/scripts\/wake-gate\.mjs"\s+"\$@"/, "run.sh's own final line must exec scripts/wake-gate.mjs, handing off ALL real work");
  assert.ok(
    !/balanceUsd|surplusUsd|isBorrowerEligible|computeLenderAvailableUsd|decideLoan/.test(code),
    "run.sh must contain no eligibility/sizing/servicing decision logic of its own"
  );
});

// ---------------------------------------------------------------------------
// PROP-117b: the registry.json economy/lending slot entry is genuinely discoverable and resolvable by
// the SAME real resolution chain (prompt.mjs::liveSlotNames / earn-slot.mjs::earnSkillRelPath /
// index.mjs's own path-join logic) every OTHER live slot already uses.
// ---------------------------------------------------------------------------

test('PROP-117b: registry.json\'s "economy/lending" slot has status:"live", dir:"skills/economy/lending", and resolves via the REAL earnSkillRelPath/liveSlotNames chain to the REAL run.sh file', () => {
  const registry = JSON.parse(fs.readFileSync(REGISTRY_PATH, "utf8"));
  const entry = registry.slots && registry.slots["economy/lending"];
  assert.ok(entry, 'registry.json must have a "economy/lending" entry in its own slots object');
  assert.equal(entry.status, "live");
  assert.equal(entry.dir, "skills/economy/lending");

  assert.ok(liveSlotNames(registry).includes("economy/lending"), "liveSlotNames (REQ-117's own Dependencies, gates activeSkillSlots) must include economy/lending now that status is live");

  const rel = earnSkillRelPath("economy/lending");
  assert.equal(rel, "economy/lending/run.sh", "economy/lending is not a legacy EARN_ACTION slot -- earnSkillRelPath must return <slot>/run.sh unchanged");
  const skillsDir = path.dirname(REGISTRY_PATH);
  const resolvedRunShPath = path.join(skillsDir, ...rel.split("/"));
  assert.equal(resolvedRunShPath, RUN_SH_PATH, "the REAL path-join logic index.mjs::runSkillWithKillRef already uses for every other live slot must resolve to the SAME run.sh file this REQ's own Acceptance Criteria require to exist");
  assert.ok(fs.existsSync(resolvedRunShPath), "the resolved run.sh path must be a real, existing file");
});

// ---------------------------------------------------------------------------
// PROP-117a: scripts/wake-gate.mjs is the ONLY production call site anywhere in the repository that
// invokes executeLoanIssuanceAttempt/executeDefaultDetectionSweep; executeRepaymentClaim is invoked
// from NO production call site this sprint (REQ-117's own explicit, documented scope exclusion).
// ---------------------------------------------------------------------------

const EXCLUDED_DIR_NAMES = new Set(["node_modules", ".git", "__tests__", "__pycache__"]);

function walkSourceFiles(dir, files = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return files;
  }
  for (const entry of entries) {
    if (EXCLUDED_DIR_NAMES.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walkSourceFiles(full, files);
    else if (/\.m?js$/.test(entry.name)) files.push(full);
  }
  return files;
}

// A production (non-test) file both IMPORTS `identifier` from a path ending in
// lending-orchestrator.mjs AND CALLS it (as `identifier(`) somewhere outside that import statement.
function findProductionCallSites(repoRoot, identifier) {
  const files = walkSourceFiles(repoRoot);
  const hits = [];
  for (const file of files) {
    let src;
    try {
      src = fs.readFileSync(file, "utf8");
    } catch {
      continue;
    }
    const importRe = new RegExp(`import\\s*\\{[^}]*\\b${identifier}\\b[^}]*\\}\\s*from\\s*["'][^"']*lending-orchestrator\\.mjs["']`);
    const importMatch = src.match(importRe);
    if (!importMatch) continue;
    const bodyWithoutImport = src.slice(0, importMatch.index) + src.slice(importMatch.index + importMatch[0].length);
    const callRe = new RegExp(`\\b${identifier}\\s*\\(`);
    if (callRe.test(bodyWithoutImport)) hits.push(path.relative(repoRoot, file));
  }
  return hits;
}

test("PROP-117a structural: executeLoanIssuanceAttempt/executeDefaultDetectionSweep are imported+called from exactly one production file (scripts/wake-gate.mjs) anywhere in the repo; executeRepaymentClaim from zero", () => {
  const issuanceHits = findProductionCallSites(REPO_ROOT, "executeLoanIssuanceAttempt");
  const sweepHits = findProductionCallSites(REPO_ROOT, "executeDefaultDetectionSweep");
  const claimHits = findProductionCallSites(REPO_ROOT, "executeRepaymentClaim");

  assert.deepEqual(issuanceHits, ["skills/economy/lending/scripts/wake-gate.mjs"], "executeLoanIssuanceAttempt must be reachable from exactly one production call site: scripts/wake-gate.mjs");
  assert.deepEqual(sweepHits, ["skills/economy/lending/scripts/wake-gate.mjs"], "executeDefaultDetectionSweep must be reachable from exactly one production call site: scripts/wake-gate.mjs");
  assert.deepEqual(claimHits, [], "executeRepaymentClaim must be invoked from zero production call sites this sprint -- REQ-117's own explicit, documented scope exclusion (no external repayment-claim channel exists)");
});

// ---------------------------------------------------------------------------
// scripts/wake-gate.mjs's own source-level contract: PROP-117d (no re-implemented eligibility/sizing
// logic, no $ANICCA_ARGS read), the required import list, and the deliberate exit-code divergence from
// self/spawn/scripts/wake-gate.mjs (FIND-1001).
// ---------------------------------------------------------------------------

test("PROP-117d structural: scripts/wake-gate.mjs contains no re-implemented eligibility/sizing/kill-switch arithmetic of its own, and never reads process.env.ANICCA_ARGS", () => {
  const code = stripLineComments(fs.readFileSync(WAKE_GATE_PATH, "utf8"), "//");
  assert.ok(
    !/balanceUsd\s*[<>]|surplusUsd\s*[<>=]|defaultRateUsd\s*[<>=]|repaymentRate\s*[<>=]/.test(code),
    "eligibility/sizing/default-detection arithmetic belongs exclusively to lending-gate.mjs's own already-exported functions, called BY this script, never re-implemented inside it"
  );
  assert.ok(!/ANICCA_ARGS/.test(code), "scripts/wake-gate.mjs must never read process.env.ANICCA_ARGS (or a parsed $ANICCA_ARGS value) anywhere");
});

test("scripts/wake-gate.mjs imports the required real primitives, and imports NOTHING from self/spawn/scripts/wake-gate.mjs itself", () => {
  const code = fs.readFileSync(WAKE_GATE_PATH, "utf8");
  for (const name of ["computeLenderAvailableUsd", "sumOutstandingPrincipalUsd", "sumRecentGojoGiftsUsd", "isBorrowerEligible"]) {
    assert.match(code, new RegExp(`\\b${name}\\b`), `must import ${name} from lending-gate.mjs`);
  }
  assert.match(code, /lending-gate\.mjs/);
  for (const name of ["executeLoanIssuanceAttempt", "executeDefaultDetectionSweep"]) {
    assert.match(code, new RegExp(`\\b${name}\\b`), `must import ${name} from lending-orchestrator.mjs`);
  }
  assert.match(code, /lending-orchestrator\.mjs/);
  assert.match(code, /readGojoLogRows/);
  assert.match(code, /gojo-read\.mjs/);
  assert.match(code, /isSelfFunded/);
  assert.match(code, /is-self-funded\.mjs/);
  assert.match(code, /usdcBalance/);
  assert.match(code, /_shared\/lib\/usdc\.mjs/);
  assert.match(code, /registry-path\.mjs/);
  assert.match(code, /citizens-registry\.mjs/);
  assert.ok(!/self\/spawn\/scripts\/wake-gate\.mjs/.test(code), "scripts/wake-gate.mjs must never import anything from self/spawn/scripts/wake-gate.mjs itself -- that file's own balance-fetch helper is private/non-exported");
});

test("Corrected (Phase 1c iteration-1, FIND-1001) structural: scripts/wake-gate.mjs's own CLI entrypoint deliberately diverges from self/spawn/scripts/wake-gate.mjs's own exit-code convention -- a refused issuance attempt stays exit 0, never mirrors self/spawn's own `result.status !== \"active\"` -> exit 1 check", () => {
  const code = fs.readFileSync(WAKE_GATE_PATH, "utf8");
  assert.ok(!/issuance(?:Result)?\.status\s*!==\s*["']active["']/.test(code), "must NOT copy self/spawn/scripts/wake-gate.mjs's own result.status !== \"active\" -> exit 1 check verbatim -- a routine per-pair refusal must stay exit 0");
  assert.match(code, /process\.exitCode\s*=\s*1/, "a genuine, unexpected in-process error (top-level .catch) must still set exit code 1");
});
