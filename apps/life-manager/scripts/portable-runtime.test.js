const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const scripts = __dirname;
const launchers = [
  "financial-report-boot.sh",
  "instagram-metrics-production-boot.sh",
  "tiktok-metrics-production-boot.sh",
  "taskmarket-work-ledger-boot.sh",
  "ugig-invoice-observer-boot.sh",
  "x402-sale-ledger-boot.sh",
];

test("production launchers share portable node and timeout resolution", () => {
  const helper = fs.readFileSync(path.join(scripts, "lib/portable-runtime.sh"), "utf8");
  assert.match(helper, /command -v node/);
  assert.match(helper, /command -v python3/);
  assert.match(helper, /runtime\/run-with-timeout\.py/);
  assert.doesNotMatch(helper, /\/opt\/homebrew\/bin/);
  for (const launcher of launchers) {
    const source = fs.readFileSync(path.join(scripts, launcher), "utf8");
    assert.match(source, /portable-runtime\.sh/, launcher);
    assert.doesNotMatch(source, /\/opt\/homebrew\/bin\/(?:node|timeout)/, launcher);
  }
});
