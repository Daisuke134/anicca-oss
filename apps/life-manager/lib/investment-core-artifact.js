"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const CORE_DIR = path.resolve(__dirname, "../investment-core");
const CORE_FILES = Object.freeze([
  "allocator.py", "alpaca_cli.py", "campaign.py", "control.py", "effect_store.py",
  "live_canary.py", "live_close.py", "parity_core.py", "performance.py",
  "performance_gate.py", "position_manager.py", "repeatability.py", "reporter.py",
  "review_status.py", "risk_day.py", "risk_policy.py", "run.py", "telegram.py",
  "telegram_outbox.py",
]);

function readInvestmentCoreArtifact(coreDir = CORE_DIR) {
  const files = CORE_FILES.map((name) => {
    const bytes = fs.readFileSync(path.join(coreDir, name));
    return Object.freeze({ name, sha256: crypto.createHash("sha256").update(bytes).digest("hex") });
  });
  const canonical = files.map(({ name, sha256 }) => `${name}\0${sha256}\n`).join("");
  const digest = crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
  return Object.freeze({
    schema_version: 1,
    digest,
    ref: `investment-core://sha256/${digest}`,
    files: Object.freeze(files),
  });
}

async function runInvestmentParityCore(fixture, opts = {}) {
  const coreDir = opts.coreDir || CORE_DIR;
  const python = opts.python || process.env.LM_INVESTMENT_PYTHON || "python3";
  const script = [
    "import json,sys",
    "from parity_core import run_parity_core",
    "json.dump(run_parity_core(json.load(sys.stdin)),sys.stdout,separators=(',',':'),sort_keys=True)",
  ].join(";");
  const execute = opts.spawn || ((command, args, options) => new Promise((resolve, reject) => {
    const child = spawn(command, args, options);
    const stdout = [];
    const stderr = [];
    let size = 0;
    child.stdout.on("data", (chunk) => {
      size += chunk.length;
      if (size > 1024 * 1024) child.kill();
      else stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => code === 0 && size <= 1024 * 1024
      ? resolve({ stdout: Buffer.concat(stdout).toString("utf8") })
      : reject(new Error(`investment core failed (${code}): ${Buffer.concat(stderr).toString("utf8").trim()}`)));
    child.stdin.end(JSON.stringify(fixture));
  }));
  const { stdout } = await execute(python, ["-c", script], { cwd: coreDir, stdio: ["pipe", "pipe", "pipe"] });
  return JSON.parse(stdout);
}

module.exports = { CORE_DIR, CORE_FILES, readInvestmentCoreArtifact, runInvestmentParityCore };
