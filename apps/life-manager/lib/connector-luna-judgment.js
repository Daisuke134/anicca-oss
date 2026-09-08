"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const { inferEventGoalSerendipity } = require("./event-goal-serendipity.js");
const { inferEventPreferenceRanking } = require("./event-preference-ranking.js");
const { isVerifiedConnectorProfile } = require("./connector-profile.js");

function unavailable() {
  throw new Error("Connector Luna judgment unavailable");
}

function absoluteDirectory(value) {
  const directory = path.resolve(String(value == null ? "" : value));
  if (!path.isAbsolute(directory) || directory === path.parse(directory).root) unavailable();
  return directory;
}

function containedFile(root, value) {
  const file = path.resolve(String(value == null ? "" : value));
  if (!file.startsWith(`${root}${path.sep}`) || file === root) unavailable();
  let stat;
  try { stat = fs.statSync(file); } catch { unavailable(); }
  if (!stat.isFile() || stat.size < 2 || stat.size > 1_000_000) unavailable();
  return file;
}

function runChild(command, args, options, deps) {
  if (typeof deps.spawnSync === "function") {
    return Promise.resolve(deps.spawnSync(command, args, options));
  }
  return new Promise((resolve, reject) => {
    const { input, maxBuffer, encoding: _encoding, ...spawnOptions } = options;
    const child = (typeof deps.spawn === "function" ? deps.spawn : spawn)(
      command, args, { ...spawnOptions, stdio: ["pipe", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    let settled = false;
    const fail = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    const append = (current, chunk) => {
      const next = current + String(chunk);
      if (Buffer.byteLength(next) > maxBuffer) {
        child.kill("SIGTERM");
        throw new Error("agent-runner output exceeded limit");
      }
      return next;
    };
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { try { stdout = append(stdout, chunk); } catch (error) { fail(error); } });
    child.stderr.on("data", (chunk) => { try { stderr = append(stderr, chunk); } catch (error) { fail(error); } });
    child.once("error", fail);
    child.once("close", (code) => {
      if (settled) return;
      settled = true;
      resolve({ status: code, stdout, stderr });
    });
    child.stdin.end(input);
  });
}

async function runLocalAgentRunner(input = {}, deps = {}) {
  try {
    const prompt = String(input.prompt == null ? "" : input.prompt);
    const taskClass = input.taskClass == null ? "repeatable-agent" : String(input.taskClass);
    const schema = input.schema;
    const timeoutMs = Number(input.timeoutMs);
    const signal = input.signal;
    const readOnly = input.readOnly === true;
    const tokenBudget = input.tokenBudget == null ? null : Number(input.tokenBudget);
    const budgetScopeId = input.budgetScopeId == null ? "" : String(input.budgetScopeId);
    const evidenceDir = absoluteDirectory(input.evidenceDir);
    const repoRoot = absoluteDirectory(input.repoRoot);
    const runnerPath = path.resolve(String(
      input.runnerPath || path.join(repoRoot, "runtime", "agent-runner", "agent_runner.py"),
    ));
    if (
      prompt.trim().length < 100 || prompt.length > 100_000
      || !schema || typeof schema !== "object" || Array.isArray(schema)
      || !["repeatable-agent", "browser-lane-agent"].includes(taskClass)
      || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 900_000
      || (signal != null && (typeof signal !== "object" || typeof signal.aborted !== "boolean"))
      || ((tokenBudget == null) !== (budgetScopeId === ""))
      || (tokenBudget != null && (!Number.isSafeInteger(tokenBudget) || tokenBudget < 1 || tokenBudget > 1_000_000))
      || (budgetScopeId && !/^[A-Za-z0-9._:-]{1,200}$/.test(budgetScopeId))
    ) unavailable();
    const isRunnerFile = typeof deps.isRunnerFile === "function"
      ? deps.isRunnerFile
      : (file) => fs.statSync(file).isFile();
    if (!path.isAbsolute(runnerPath) || !isRunnerFile(runnerPath)) unavailable();
    fs.mkdirSync(evidenceDir, { mode: 0o700, recursive: true });
    fs.chmodSync(evidenceDir, 0o700);
    const schemaPath = path.join(evidenceDir, "schema.json");
    fs.writeFileSync(schemaPath, `${JSON.stringify(schema)}\n`, { encoding: "utf8", mode: 0o600, flag: "w" });
    const args = [
      runnerPath,
      "--task-class", taskClass,
      "--prompt-stdin",
      "--schema", schemaPath,
      "--evidence-dir", evidenceDir,
      "--task-label", taskClass === "browser-lane-agent"
        ? "connector-event-application" : "connector-event-judgment",
      "--loop", "connector",
      "--workdir", repoRoot,
      "--timeout-seconds", String(Math.ceil(timeoutMs / 1_000)),
      ...(taskClass === "browser-lane-agent" ? [
        "--escalation-reason", "unknown event registration UI requires bounded visual judgment",
      ] : []),
      ...(readOnly ? ["--read-only"] : []),
    ];
    const env = { ...process.env };
    if (tokenBudget != null) Object.assign(env, {
      ANICCA_BUDGET_REQUIRED: "1",
      ANICCA_BUDGET_SCOPE_ID: budgetScopeId,
      ANICCA_PASS_TOKEN_BUDGET: String(tokenBudget),
    });
    const completed = await runChild("python3", args, {
      input: prompt,
      encoding: "utf8",
      timeout: timeoutMs + 5_000,
      maxBuffer: 1_000_000,
      env,
      signal,
    }, deps);
    if (!completed || completed.status !== 0) unavailable();
    let summary;
    try { summary = JSON.parse(String(completed.stdout || "").trim()); }
    catch { unavailable(); }
    if (
      !summary || summary.status !== "success"
      || summary.selected_provider !== "codex"
      || summary.selected_model !== "gpt-5.6-terra"
    ) unavailable();
    const resultPath = containedFile(evidenceDir, summary.result_path);
    let value;
    try { value = JSON.parse(fs.readFileSync(resultPath, "utf8")); }
    catch { unavailable(); }
    return Object.freeze({ summary: Object.freeze({
      status: summary.status,
      selected_provider: summary.selected_provider,
      selected_model: summary.selected_model,
    }), value });
  } catch {
    unavailable();
  }
}

async function runConnectorLunaJudgment(input = {}, deps = {}) {
  try {
    if (!input || typeof input !== "object" || Array.isArray(input)) unavailable();
    if (!isVerifiedConnectorProfile(input.profile)) unavailable();
    const runAgentRunner = typeof deps.runAgentRunner === "function"
      ? deps.runAgentRunner
      : (request) => runLocalAgentRunner(request, deps);
    const evidenceRoot = absoluteDirectory(input.evidenceDir);
    if (typeof deps.runAgentRunner !== "function") {
      fs.mkdirSync(evidenceRoot, { mode: 0o700, recursive: true });
      fs.chmodSync(evidenceRoot, 0o700);
    }
    const invokeLuna = async ({ prompt, schema, timeoutMs }, stage) => {
      const result = await runAgentRunner({
        prompt, schema, timeoutMs,
        evidenceDir: path.join(evidenceRoot, stage),
        repoRoot: input.repoRoot,
        runnerPath: input.runnerPath,
      });
      if (
        !result || !result.summary
        || result.summary.status !== "success"
        || result.summary.selected_provider !== "codex"
        || result.summary.selected_model !== "gpt-5.6-terra"
      ) unavailable();
      return result.value;
    };
    const preferenceRanking = input.preferenceRanking || await inferEventPreferenceRanking({
      dateInventory: input.dateInventory,
      date: input.date,
      preferences: input.profile.preferences,
    }, {
      generateDecision: (request) => invokeLuna(request, "preference"),
    });
    return await inferEventGoalSerendipity({
      dateInventory: input.dateInventory,
      preferenceRanking,
      goals: input.profile.goals,
    }, {
      generateDecision: (request) => invokeLuna(request, "goal"),
    });
  } catch {
    unavailable();
  }
}

module.exports = { runConnectorLunaJudgment, runLocalAgentRunner };
