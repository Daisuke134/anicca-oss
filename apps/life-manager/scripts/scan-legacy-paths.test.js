"use strict";

// Order 5 exit evidence: the Life Manager runtime source contains no
// non-allowlisted reference to a legacy runtime root. Scope and allowlist are
// defined in scan-legacy-paths.js (see its header): apps/life-manager plus the
// skills the runtime actually spawns (daily-lm-video, lm-distribution,
// telegram-user, life-manager) plus runtime/. Legacy tokens in this test are
// assembled from fragments so the test file itself stays scan-neutral.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  scanLegacyPaths,
  verifyAllowlist,
  ALLOWLIST,
  SCAN_ROOTS,
} = require("./scan-legacy-paths.js");

const LEGACY_STATE_LINE = 'STATE="${HOME}/' + ".open" + 'claw/state/example"';
const LEGACY_ANICCA_HOME_LINE = 'DIR="${HOME}/' + "anicca" + '/skills/earn/x402-sell/state"';
const LEGACY_ANICCA_TILDE_LINE = "DIR=~/" + "anicca" + "/skills/earn/x402-sell/state";
const LEGACY_ANICCA_OSS_LINE = 'START="${HOME}/' + "anicca" + '-oss/services/facilitator/start.sh"';
const ABS_HOME = "/" + "Users/dais";
const LEGACY_ANICCA_ABS_LINE = `DIR="${ABS_HOME}/` + "anicca" + '/skills/earn/x402-sell/state"';
const LEGACY_ANICCA_ABS_OSS_LINE = `START="${ABS_HOME}/` + "anicca" + '-oss/services/facilitator/start.sh"';
const HOME_SKILL_SOURCE_LINE = 'TOOL="${HOME}/.' + 'claude/skills/example/run.py"';

function plantedRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-legacy-scan-"));
  fs.mkdirSync(path.join(root, "src"), { recursive: true });
  return root;
}

test("the Life Manager runtime scope has zero non-allowlisted legacy path references", () => {
  const result = scanLegacyPaths();
  assert.ok(result.scannedFiles > 200, `expected a real scan, saw ${result.scannedFiles} files`);
  assert.deepEqual(
    result.violations,
    [],
    `legacy path references found:\n${result.violations
      .map((violation) => `${violation.file}:${violation.line} ${violation.text}`)
      .join("\n")}`,
  );
});

test("the scanner detects a planted legacy path reference (it is not vacuous)", () => {
  const root = plantedRepo();
  fs.writeFileSync(path.join(root, "src", "boot.sh"), `#!/bin/bash\n${LEGACY_STATE_LINE}\n`);
  const result = scanLegacyPaths({ root, roots: ["src"] });
  assert.equal(result.violations.length, 1);
  assert.equal(result.violations[0].file, path.join("src", "boot.sh"));
  assert.equal(result.violations[0].line, 2);
});

test("the scanner detects legacy anicca code-root references (HOME, tilde, oss)", () => {
  const root = plantedRepo();
  fs.writeFileSync(
    path.join(root, "src", "boot.sh"),
    `#!/bin/bash\n${LEGACY_ANICCA_HOME_LINE}\n${LEGACY_ANICCA_TILDE_LINE}\n${LEGACY_ANICCA_OSS_LINE}\n`,
  );
  // "anicca" as a plain word, username, or product path is NOT a code root.
  fs.writeFileSync(
    path.join(root, "src", "benign.sh"),
    `A="/${"Users"}/anicca/anicca-project/x"\nB="run the anicca product"\n`,
  );
  const result = scanLegacyPaths({ root, roots: ["src"] });
  assert.deepEqual(
    result.violations.map((violation) => [violation.file, violation.line]),
    [
      [path.join("src", "boot.sh"), 2],
      [path.join("src", "boot.sh"), 3],
      [path.join("src", "boot.sh"), 4],
    ],
  );
});

test("the scanner detects absolute home literals of the legacy anicca code roots", () => {
  const root = plantedRepo();
  fs.writeFileSync(
    path.join(root, "src", "boot.sh"),
    `#!/bin/bash\n${LEGACY_ANICCA_ABS_LINE}\n${LEGACY_ANICCA_ABS_OSS_LINE}\n`,
  );
  // The username segment and the products monorepo are NOT legacy code roots:
  // A products-monorepo path (even under an anicca username) must not match.
  fs.writeFileSync(
    path.join(root, "src", "benign.sh"),
    `A="/${"Users"}/anicca/anicca-project/apps/life-manager"\nB="/${"Users"}/dais/anicca-project/x"\n`,
  );
  const result = scanLegacyPaths({ root, roots: ["src"] });
  assert.deepEqual(
    result.violations.map((violation) => [violation.file, violation.line, violation.pattern]),
    [
      [path.join("src", "boot.sh"), 2, "legacy-anicca-home-root"],
      [path.join("src", "boot.sh"), 3, "legacy-oss-code-root"],
    ],
  );
});

test("the scanner detects a home-directory skill source dependency", () => {
  const root = plantedRepo();
  fs.writeFileSync(path.join(root, "src", "boot.sh"), `#!/bin/bash\n${HOME_SKILL_SOURCE_LINE}\n`);
  const result = scanLegacyPaths({ root, roots: ["src"] });
  assert.deepEqual(
    result.violations.map((violation) => violation.pattern),
    ["home-skill-source"],
  );
});

test("the allowlist is pinned to exact file plus line content, not blanket files", () => {
  for (const entry of ALLOWLIST) {
    assert.ok(entry.file && entry.lineIncludes && entry.reason, JSON.stringify(entry));
  }
  const root = plantedRepo();
  // Same line content as an allowlisted denial line, but in a different file:
  // it must still be flagged.
  const allowedLine = `const LEGACY_SEGMENT = /${"\\.open" + "claw"}/;`;
  fs.writeFileSync(path.join(root, "src", "other-module.js"), `${allowedLine}\n`);
  const result = scanLegacyPaths({ root, roots: ["src"] });
  assert.equal(result.violations.length, 1);
});

test("test files and fixtures are excluded while runtime sources are scanned", () => {
  const root = plantedRepo();
  fs.writeFileSync(path.join(root, "src", "module.test.js"), `${LEGACY_STATE_LINE}\n`);
  fs.mkdirSync(path.join(root, "src", "tests"));
  fs.writeFileSync(path.join(root, "src", "tests", "helper.sh"), `${LEGACY_STATE_LINE}\n`);
  fs.writeFileSync(path.join(root, "src", "module.js"), `${LEGACY_STATE_LINE}\n`);
  const result = scanLegacyPaths({ root, roots: ["src"] });
  assert.deepEqual(
    result.violations.map((violation) => violation.file),
    [path.join("src", "module.js")],
  );
});

test("the scan scope covers the runtime roots the Life Manager actually loads", () => {
  for (const scanRoot of [
    "apps/life-manager",
    "skills/video/daily-lm-video",
    "skills/video/lm-distribution",
    "skills/tools/telegram-user",
    "skills/life-manager",
    "skills/earn/marketing-engine",
    "skills/earn/capafy-marketing",
    "skills/earn/clip",
    "skills/earn/x402-sell",
    "runtime",
  ]) {
    assert.ok(SCAN_ROOTS.includes(scanRoot), `missing scan root: ${scanRoot}`);
  }
});

test("the runtime has no tracked pre-migration holes", () => {
  const tracked = ALLOWLIST.filter((entry) => entry.order);
  assert.deepEqual(tracked, []);
});

test("every allowlist entry is alive: it matches a current pattern-bearing line", () => {
  assert.deepEqual(
    verifyAllowlist(),
    [],
    "stale or moved allowlist entries must be re-pinned or removed",
  );
});

test("an allowlisted line that moves or changes fails verification", () => {
  const root = plantedRepo();
  fs.writeFileSync(
    path.join(root, "src", "boot.sh"),
    `#!/bin/bash\n# moved down one line\n${LEGACY_ANICCA_OSS_LINE}\n`,
  );
  const allowlist = [
    // Pinned to line 2, but the line now lives at line 3: must be reported.
    { file: path.join("src", "boot.sh"), line: 2, lineIncludes: "START=", reason: "x", order: "Order 12" },
    // Content no longer present anywhere: must be reported.
    { file: path.join("src", "boot.sh"), lineIncludes: "DELETED_CONTENT=", reason: "x" },
  ];
  const issues = verifyAllowlist({ root, allowlist });
  assert.equal(issues.length, 2, JSON.stringify(issues));
  // And the scanner must NOT honor the mispinned entry: the moved line is a violation.
  const scan = scanLegacyPaths({ root, roots: ["src"], allowlist });
  assert.equal(scan.violations.length, 1);
  assert.equal(scan.violations[0].line, 3);
});
