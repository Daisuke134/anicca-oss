"use strict";

// F2a: migrate-legacy-state.sh copies (never moves/deletes) the legacy state
// dirs {lm-video, life-manager-dev} into <data root>/state/, idempotently, with
// post-copy readback verification (file count + byte sizes). These tests run
// exclusively against fake temp source/dest dirs via LM_LEGACY_STATE_ROOT and
// LM_DATA_DIR overrides — never the real legacy store.

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SCRIPT = path.join(__dirname, "migrate-legacy-state.sh");

function fakeStores() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-migrate-"));
  const legacyRoot = path.join(root, "legacy-store", "state");
  const dataRoot = path.join(root, "data-root");
  fs.mkdirSync(path.join(legacyRoot, "lm-video", "recordings"), { recursive: true });
  fs.mkdirSync(path.join(legacyRoot, "life-manager-dev"), { recursive: true });
  fs.writeFileSync(path.join(legacyRoot, "lm-video", "daily-render-state.jsonl"), '{"date":"2026-07-20"}\n');
  fs.writeFileSync(path.join(legacyRoot, "lm-video", "recordings", "call.mp3"), Buffer.alloc(2048, 7));
  fs.writeFileSync(path.join(legacyRoot, "life-manager-dev", "done.jsonl"), '{"issue":41}\n{"issue":42}\n');
  return { legacyRoot, dataRoot };
}

function runMigration(legacyRoot, dataRoot) {
  return spawnSync("bash", [SCRIPT], {
    encoding: "utf8",
    env: { ...process.env, LM_LEGACY_STATE_ROOT: legacyRoot, LM_DATA_DIR: dataRoot },
  });
}

test("copies both legacy state dirs into <data root>/state with verified sizes", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  const result = runMigration(legacyRoot, dataRoot);
  assert.equal(result.status, 0, result.stderr);

  const copies = [
    ["lm-video/daily-render-state.jsonl", '{"date":"2026-07-20"}\n'],
    ["life-manager-dev/done.jsonl", '{"issue":41}\n{"issue":42}\n'],
  ];
  for (const [relative, expected] of copies) {
    const copied = path.join(dataRoot, "state", relative);
    assert.equal(fs.readFileSync(copied, "utf8"), expected, relative);
  }
  const audio = path.join(dataRoot, "state", "lm-video", "recordings", "call.mp3");
  assert.equal(fs.statSync(audio).size, 2048);
  assert.match(result.stdout, /verified/i);

  // COPY, not move: the legacy source must be fully intact afterwards.
  assert.equal(
    fs.readFileSync(path.join(legacyRoot, "life-manager-dev", "done.jsonl"), "utf8"),
    '{"issue":41}\n{"issue":42}\n',
  );
  assert.equal(fs.statSync(path.join(legacyRoot, "lm-video", "recordings", "call.mp3")).size, 2048);
});

test("re-running is idempotent and never clobbers files the new loop already owns", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  assert.equal(runMigration(legacyRoot, dataRoot).status, 0);

  // The new loop appends to its copy; a re-run must not overwrite it.
  const devLedger = path.join(dataRoot, "state", "life-manager-dev", "done.jsonl");
  fs.appendFileSync(devLedger, '{"issue":43}\n');
  const rerun = runMigration(legacyRoot, dataRoot);
  assert.equal(rerun.status, 0, rerun.stderr);
  assert.equal(
    fs.readFileSync(devLedger, "utf8"),
    '{"issue":41}\n{"issue":42}\n{"issue":43}\n',
  );
  assert.match(rerun.stdout, /skipped/i);
});

test("a missing legacy dir is reported and skipped without failing", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  fs.rmSync(path.join(legacyRoot, "life-manager-dev"), { recursive: true });
  const result = runMigration(legacyRoot, dataRoot);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /skip/i);
  assert.ok(fs.existsSync(path.join(dataRoot, "state", "lm-video", "daily-render-state.jsonl")));
});

test("a differing opaque file on readback fails loudly", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  // Pre-plant a truncated destination file, then force the copier to treat it
  // as already-copied is NOT enough — verification must compare sizes of every
  // migrated file, so a corrupt pre-existing copy must fail the readback.
  const dest = path.join(dataRoot, "state", "lm-video", "recordings");
  fs.mkdirSync(dest, { recursive: true });
  fs.writeFileSync(path.join(dest, "call.mp3"), Buffer.alloc(3, 1));
  const result = runMigration(legacyRoot, dataRoot);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /no safe merge rule exists/i);
});

test("pre-existing mutable dev state passes only when it semantically contains legacy state", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  const legacy = path.join(legacyRoot, "life-manager-dev");
  const current = path.join(dataRoot, "state", "life-manager-dev");
  fs.mkdirSync(current, { recursive: true });
  fs.writeFileSync(path.join(legacy, "issues.json"), JSON.stringify([{ number: 41 }]));
  fs.writeFileSync(path.join(current, "issues.json"), JSON.stringify([{ number: 41 }, { number: 42 }]));
  fs.writeFileSync(path.join(legacy, "seven-day-status.json"), JSON.stringify({
    schema_version: 1, evaluated_at: "2026-07-29T19:11:39.660Z", ready: false,
  }));
  fs.writeFileSync(path.join(current, "seven-day-status.json"), JSON.stringify({
    schema_version: 1, evaluated_at: "2026-09-07T19:17:32.123Z", ready: true,
  }));
  fs.writeFileSync(path.join(current, "done.jsonl"), '{"issue":41}\n{"issue":42}\n{"issue":43}\n');

  const result = runMigration(legacyRoot, dataRoot);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /verified by content readback/i);
});

test("pre-existing JSONL missing a legacy row fails loudly", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  const current = path.join(dataRoot, "state", "life-manager-dev");
  fs.mkdirSync(current, { recursive: true });
  fs.writeFileSync(path.join(current, "done.jsonl"), '{"issue":42}\n{"issue":43}\n');
  const result = runMigration(legacyRoot, dataRoot);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /does not contain every legacy JSONL row/i);
});

test("an older seven-day snapshot fails loudly", () => {
  const { legacyRoot, dataRoot } = fakeStores();
  const legacy = path.join(legacyRoot, "life-manager-dev");
  const current = path.join(dataRoot, "state", "life-manager-dev");
  fs.mkdirSync(current, { recursive: true });
  fs.copyFileSync(path.join(legacy, "done.jsonl"), path.join(current, "done.jsonl"));
  fs.writeFileSync(path.join(legacy, "seven-day-status.json"), JSON.stringify({
    schema_version: 1, evaluated_at: "2026-09-07T19:17:32.123Z",
  }));
  fs.writeFileSync(path.join(current, "seven-day-status.json"), JSON.stringify({
    schema_version: 1, evaluated_at: "2026-07-29T19:11:39.660Z",
  }));
  const result = runMigration(legacyRoot, dataRoot);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /is older than or incompatible/i);
});
