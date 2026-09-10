"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { CORE_DIR, CORE_FILES, readInvestmentCoreArtifact } = require("./investment-core-artifact.js");

test("cloud artifact packages byte-identical shared Telegram transport", () => {
  const root = path.resolve(__dirname, "../../..");
  assert.equal(Buffer.compare(fs.readFileSync(path.join(CORE_DIR, "telegram.py")), fs.readFileSync(path.join(root, "skills/_shared/telegram.py"))), 0);
  assert.equal(Buffer.compare(fs.readFileSync(path.join(CORE_DIR, "telegram_outbox.py")), fs.readFileSync(path.join(root, "skills/_shared/marketplace-core/scripts/telegram_outbox.py"))), 0);
  assert.equal(CORE_FILES.includes("telegram.py"), true);
  assert.equal(CORE_FILES.includes("telegram_outbox.py"), true);
  assert.match(readInvestmentCoreArtifact().digest, /^[a-f0-9]{64}$/);
});

test("packaged reporter falls back to its local transport without repo shared paths", () => {
  const script = [
    "from pathlib import Path", "import reporter", "reporter.REPO=Path('/definitely-absent')",
    "assert reporter._load_outbox().__file__.endswith('telegram_outbox.py')",
    "print('ok')",
  ].join(";");
  const output = require("node:child_process").execFileSync("python3", ["-c", script], { cwd: CORE_DIR, encoding: "utf8" });
  assert.equal(output.trim(), "ok");
});
