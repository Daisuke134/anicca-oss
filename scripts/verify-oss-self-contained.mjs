#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync,
  readFileSync,
  readlinkSync,
  realpathSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const REQUIRED_SOURCES = [
  "life-manager",
  "life-manager-v0",
  "anicca.ai",
  "anicca-products",
  "profitable-claude",
  "anicca-dais",
  "local-source-folders",
];

const ACTIVE_ROOTS = [
  "apps/life-manager",
  "runtime",
  "scripts",
  "services",
  "skills",
  "install.sh",
  "start-local.sh",
];

const GENERATED_EXTENSIONS = new Set([
  ".gif", ".jpeg", ".jpg", ".log", ".mov", ".mp4", ".png", ".sqlite", ".webp",
]);

const PORTABLE_RUNTIME_FILES = new Set([
  ".gitignore",
  ".gitkeep",
]);

const VERIFIED_VENDOR_ROOTS = [
  "skills/capafy-autopublish/vendor",
];
const BASELINE_PATH = ".oss-self-contained-baseline.json";

function git(root, args, encoding = "utf8") {
  return execFileSync("git", args, {
    cwd: root,
    encoding,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function trackedEntries(root) {
  const raw = git(root, ["ls-files", "-s", "-z"], "buffer");
  return raw
    .toString("utf8")
    .split("\0")
    .filter(Boolean)
    .map((entry) => {
      const match = entry.match(/^(\d{6}) ([0-9a-f]{40,64}) (\d)\t([\s\S]+)$/);
      if (!match) {
        throw new Error(`unparseable git index entry: ${entry.slice(0, 80)}`);
      }
      return { mode: match[1], object: match[2], stage: Number(match[3]), path: match[4] };
    });
}

function isActivePath(path) {
  return ACTIVE_ROOTS.some((root) => path === root || path.startsWith(`${root}/`));
}

function isVerifiedVendorPath(path) {
  return VERIFIED_VENDOR_ROOTS.some((root) => path === root || path.startsWith(`${root}/`));
}

const LEGACY_LITERAL_FIXTURES = new Set([
  "apps/life-manager/lib/runtime-paths.test.js",
  "apps/life-manager/scripts/inventory-legacy-jobs.test.js",
  "runtime/loop/tests/fixtures/macos-loop-jobs.json",
  "runtime/migrate-legacy-self-improve-state.py",
  "skills/earn/hl-trade/tests/test_reconcile.py",
]);

// A tracked image is product source, not a build artifact, when a tracked JSON contract declares
// its digest. Undeclared binaries stay violations, so a stray render cannot enter by sitting in an
// assets folder.
function declaredAssetDigests(root, entries) {
  const digests = new Set();
  for (const entry of entries) {
    if (!entry.path.endsWith(".json") || !isActivePath(entry.path)) continue;
    const absolute = resolve(root, entry.path);
    if (!existsSync(absolute)) continue;
    for (const match of readFileSync(absolute, "utf8").matchAll(/\b[0-9a-f]{64}\b/gu)) {
      digests.add(match[0]);
    }
  }
  return digests;
}

function isWithinRoot(root, candidate) {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel));
}

function sourceRootViolation(text) {
  const forbidden = [
    /\/Users\/[^/\s"'`]+/u,
    /\/home\/[^/\s"'`]+\/(?:profitable-claude|anicca(?:-oss|-project)?|anicca-dais|\.openclaw)(?=[/}\s"'`]|$)/u,
    /(?:~|\$HOME)\/anicca(?:-oss)?(?=[/}\s"'`]|$)/u,
    /(?:~|\$HOME)\/profitable-claude(?=[/}\s"'`]|$)/u,
    /(?:~|\$HOME)\/anicca-project(?=[/}\s"'`]|$)/u,
    /(?:~|\$HOME)\/\.openclaw(?=[/}\s"'`]|$)/u,
    /Path\.home\(\)\s*\/\s*["']\.openclaw["']/u,
    /\/opt\/life-manager(?=[/}\s"'`]|$)/u,
  ];
  return forbidden.some((pattern) => pattern.test(text));
}

function personalRuntimeDefaultViolation(text) {
  const forbidden = [
    /--target(?:=|\s+)(?:"|')?\d{6,}/u,
    /(?:TELEGRAM|CHAT)[A-Z0-9_]*(?:=|:-)(?:"|')?\d{6,}/u,
    /\b(?!(?:you|user|example)@)[A-Za-z0-9._%+-]+@gmail\.com\b/iu,
  ];
  return forbidden.some((pattern) => pattern.test(text));
}

function looksTextual(path, bytes) {
  if (bytes.includes(0)) return false;
  const extension = path.includes(".") ? path.slice(path.lastIndexOf(".")).toLowerCase() : "";
  return !GENERATED_EXTENSIONS.has(extension);
}

function isRuntimeCopy(path) {
  const basename = path.slice(path.lastIndexOf("/") + 1);
  if (PORTABLE_RUNTIME_FILES.has(basename) || /\.example(?:\.|$)/u.test(basename)) return false;
  return /(^|\/)(?:state|logs?|output|work|sessions?)(\/|$)/u.test(path);
}

function generatedArtifact(path) {
  const extension = path.includes(".") ? path.slice(path.lastIndexOf(".")).toLowerCase() : "";
  return GENERATED_EXTENSIONS.has(extension);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function evidenceDigest(root, path, entries) {
  const exact = entries.find((entry) => entry.path === path);
  if (exact && exact.mode !== "120000" && exact.mode !== "160000") {
    const absolute = resolve(root, path);
    if (existsSync(absolute)) return sha256(readFileSync(absolute));
  }
  const inventory = entries
    .filter((entry) => entry.path.startsWith(`${path}/`))
    .map((entry) => `${entry.mode}\0${entry.object}\0${entry.path}\n`)
    .sort()
    .join("");
  return sha256(inventory || `missing\0${path}`);
}

function applyBaseline(root, entries, violations) {
  const absolute = resolve(root, BASELINE_PATH);
  if (!existsSync(absolute)) return violations;
  let baseline;
  try {
    baseline = JSON.parse(readFileSync(absolute, "utf8"));
  } catch {
    return [...violations, { code: "baseline_invalid", path: BASELINE_PATH, detail: "invalid JSON" }];
  }
  if (baseline.version !== 1 || !Array.isArray(baseline.entries)) {
    return [...violations, { code: "baseline_invalid", path: BASELINE_PATH, detail: "invalid contract" }];
  }
  const accepted = new Set();
  for (const entry of baseline.entries) {
    if (!entry || typeof entry.code !== "string" || typeof entry.path !== "string"
      || !/^[0-9a-f]{64}$/u.test(String(entry.evidence_sha256 || ""))) {
      return [...violations, { code: "baseline_invalid", path: BASELINE_PATH, detail: "invalid entry" }];
    }
    accepted.add(`${entry.code}\0${entry.path}\0${entry.evidence_sha256}`);
  }
  return violations.filter((violation) => !accepted.has(
    `${violation.code}\0${violation.path}\0${evidenceDigest(root, violation.path, entries)}`,
  ));
}

function readManifest(root, violations, entries) {
  const manifestPath = "docs/manifests/oss-merge-1-sources.json";
  const absolute = resolve(root, manifestPath);
  if (!existsSync(absolute)) {
    violations.push({ code: "manifest_missing", path: manifestPath, detail: "required source manifest" });
    return;
  }
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(absolute, "utf8"));
  } catch {
    violations.push({ code: "manifest_invalid", path: manifestPath, detail: "invalid JSON" });
    return;
  }
  const ids = new Set(
    Array.isArray(manifest.sources)
      ? manifest.sources.map((source) => source && source.id).filter(Boolean)
      : [],
  );
  for (const id of REQUIRED_SOURCES) {
    if (!ids.has(id)) {
      violations.push({ code: "manifest_source_missing", path: id, detail: "source not classified" });
    }
  }
  for (const source of Array.isArray(manifest.sources) ? manifest.sources : []) {
    const absorbedFiles = Array.isArray(source && source.absorbed_files) ? source.absorbed_files : [];
    const absorbedRoots = Array.isArray(source && source.absorbed_roots) ? source.absorbed_roots : [];
    if (source && source.disposition === "absorbed" && absorbedFiles.length === 0) {
      violations.push({
        code: "manifest_mapping_missing",
        path: String(source.id || "unknown"),
        detail: "absorbed source has no target mapping",
      });
    }
    for (const mapping of absorbedFiles) {
      const target = mapping && typeof mapping.target === "string" ? mapping.target : "";
      const absoluteTarget = resolve(root, target);
      if (!target || !isWithinRoot(root, absoluteTarget) || !existsSync(absoluteTarget)) {
        violations.push({
          code: "manifest_target_missing",
          path: target || String(source.id || "unknown"),
          detail: "absorbed target is absent or outside checkout",
        });
        continue;
      }
      if (!/^[0-9a-f]{64}$/u.test(String(mapping.sha256 || ""))
        || sha256(readFileSync(absoluteTarget)) !== mapping.sha256) {
        violations.push({
          code: "manifest_hash_mismatch",
          path: target,
          detail: "absorbed target differs from classified digest",
        });
      }
    }
    for (const mapping of absorbedRoots) {
      const target = mapping && typeof mapping.target === "string" ? mapping.target : "";
      const rootEntries = entries
        .filter((entry) => entry.mode !== "120000" && entry.mode !== "160000")
        .filter((entry) => entry.path === target || entry.path.startsWith(`${target}/`))
        .filter((entry) => existsSync(resolve(root, entry.path)))
        .sort((left, right) => left.path.localeCompare(right.path));
      const inventory = rootEntries.map((entry) =>
        `${sha256(readFileSync(resolve(root, entry.path)))}  ${entry.path}\n`).join("");
      if (!target || rootEntries.length !== Number(mapping.files)
        || !/^[0-9a-f]{64}$/u.test(String(mapping.inventory_sha256 || ""))
        || sha256(inventory) !== mapping.inventory_sha256) {
        violations.push({
          code: "manifest_inventory_mismatch",
          path: target || String(source.id || "unknown"),
          detail: "absorbed root differs from classified inventory",
        });
      }
    }
  }
}

export function verifyRepository(inputRoot) {
  const root = realpathSync(resolve(inputRoot));
  const violations = [];
  const entries = trackedEntries(root);
  const declaredDigests = declaredAssetDigests(root, entries);

  for (const entry of entries) {
    const absolute = resolve(root, entry.path);
    if (entry.mode === "160000") {
      violations.push({ code: "gitlink", path: entry.path, detail: "Git submodule dependency" });
      continue;
    }
    if (entry.mode === "120000") {
      let target;
      try {
        target = readlinkSync(absolute);
      } catch {
        target = git(root, ["show", `:${entry.path}`]).trim();
      }
      const resolvedTarget = resolve(dirname(absolute), target);
      if (!isWithinRoot(root, resolvedTarget)) {
        violations.push({ code: "symlink_escape", path: entry.path, detail: "target leaves checkout" });
      }
      continue;
    }
    if (!isActivePath(entry.path) || !existsSync(absolute)) continue;

    if (isRuntimeCopy(entry.path)) {
      violations.push({ code: "runtime_copy", path: entry.path, detail: "runtime data is tracked" });
      continue;
    }
    if (generatedArtifact(entry.path)) {
      if (!declaredDigests.has(sha256(readFileSync(absolute)))) {
        violations.push({ code: "generated_artifact", path: entry.path, detail: "generated binary is tracked" });
      }
      continue;
    }
    const bytes = readFileSync(absolute);
    const isFirstPartyText =
      !isVerifiedVendorPath(entry.path)
      && !LEGACY_LITERAL_FIXTURES.has(entry.path)
      && looksTextual(entry.path, bytes);
    const text = isFirstPartyText ? bytes.toString("utf8") : "";
    if (isFirstPartyText && sourceRootViolation(text)) {
      violations.push({
        code: "forbidden_source_root",
        path: entry.path,
        detail: "active source depends on a developer-local root",
      });
    }
    if (isFirstPartyText && personalRuntimeDefaultViolation(text)) {
      violations.push({
        code: "personal_runtime_default",
        path: entry.path,
        detail: "active source embeds a user's messaging or mail destination",
      });
    }
  }

  const runnerPath = "runtime/agent-runner/agent_runner.py";
  if (!existsSync(resolve(root, runnerPath))) {
    violations.push({ code: "required_file_missing", path: runnerPath, detail: "canonical runner absent" });
  }
  const duplicateRunnerPath = "skills/agent-runner/agent_runner.py";
  if (existsSync(resolve(root, duplicateRunnerPath))) {
    violations.push({
      code: "duplicate_runner",
      path: duplicateRunnerPath,
      detail: "second agent runner diverges from canonical runtime engine",
    });
  }

  readManifest(root, violations, entries);
  const remaining = applyBaseline(root, entries, violations);
  remaining.sort((left, right) =>
    left.path.localeCompare(right.path) || left.code.localeCompare(right.code));
  return { ok: remaining.length === 0, violations: remaining };
}

function parseArgs(argv) {
  let root = process.cwd();
  let json = false;
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--root") {
      root = argv[index + 1] || "";
      index += 1;
    } else if (arg === "--json") {
      json = true;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (!root) throw new Error("--root requires a path");
  return { root, json };
}

function main() {
  const { root, json } = parseArgs(process.argv.slice(2));
  const result = verifyRepository(root);
  if (json) {
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } else if (result.ok) {
    process.stdout.write("OSS self-contained verification: PASS\n");
  } else {
    process.stderr.write(`OSS self-contained verification: FAIL (${result.violations.length})\n`);
    for (const violation of result.violations) {
      process.stderr.write(`${violation.code}\t${violation.path}\n`);
    }
  }
  process.exitCode = result.ok ? 0 : 1;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main();
}
