"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { buildRollingEventCoverage } = require("./rolling-event-coverage.js");
const { collectLumaInventory } = require("./luma-discovery.js");
const { normalizeLumaEventDetail } = require("./luma-event-detail.js");
const { buildLumaDateInventory } = require("./luma-date-inventory.js");
const { validateEventPreferenceRanking } = require("./event-preference-ranking.js");
const { readConnectorProfile } = require("./connector-profile.js");
const { isVerifiedEventGoalSerendipity } = require("./event-goal-serendipity.js");
const { runConnectorLunaJudgment, runLocalAgentRunner } = require("./connector-luna-judgment.js");

async function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-luna-"));
  const profilePath = path.join(root, "profile.json");
  fs.writeFileSync(profilePath, JSON.stringify({
    schema_version: 1,
    tenant_id: "dais-local",
    timezone: "Asia/Tokyo",
    preferences: "AI founderとの対面eventを優先する。",
    goals: "Life Managerを成長させ、founderとengineerとの接点を増やす。",
    spend_policy: { limits: [] },
    identity_ref: "identity://dais/local",
    browser_profile_ref: "browser-profile://cloakbrowser/daily-driver",
    calendar_ref: "calendar://google/dais-local",
  }), { mode: 0o600 });
  const profile = readConnectorProfile({ tenantId: "dais-local", path: profilePath });
  const coverage = buildRollingEventCoverage({
    tenantId: "dais-local", timeZone: "Asia/Tokyo",
    now: "2026-08-01T16:00:00.000Z", resolvedDays: [],
  });
  let round = 0;
  const inventory = await collectLumaInventory({
    readSnapshot: async () => (++round === 1 ? [{
      href: "https://luma.com/ai-founder-night", title: "AI Founder Night",
      cardText: "AI Founder Night 18:00", timelineText: "8月2日 日曜日",
    }] : []),
    advance: async () => ({ atEnd: true, scrollHeight: 100 }), stableEndRounds: 1,
  });
  const details = [normalizeLumaEventDetail({
    canonicalUrl: "https://luma.com/ai-founder-night",
    jsonLd: [{
      "@type": "Event", name: "AI Founder Night",
      description: "AI founders demonstrate products with engineers.",
      startDate: "2026-08-02T09:00:00.000Z", endDate: "2026-08-02T11:00:00.000Z",
      eventAttendanceMode: "https://schema.org/OfflineEventAttendanceMode",
      eventStatus: "https://schema.org/EventScheduled",
      organizer: [{ name: "Tokyo Startup Community" }],
      location: { name: "Shibuya Startup Hub", address: "Shibuya, Tokyo" },
    }], controls: ["Register"],
  })];
  const dateInventory = buildLumaDateInventory({
    coverage, inventory, details, now: "2026-08-02T01:00:00.000Z",
  });
  const eventRef = dateInventory.days.find((day) => day.date === "2026-08-02").events[0].event_ref;
  const preferenceRanking = validateEventPreferenceRanking({ ranked_events: [{
    event_ref: eventRef, preference_fit: "strong", preference_reason: "AI founderへの関心と合います。",
  }] }, { dateInventory, date: "2026-08-02", preferences: profile.preferences });
  const value = { ranked_events: [{
    event_ref: eventRef,
    goal_alignment: "strong",
    serendipity_potential: "high",
    goal_reason: "Life Managerを見せられるfounderとの接点に合います。",
    serendipity_reason: "product demoから予期しない協力関係が生まれ得ます。",
  }] };
  const preferenceValue = { ranked_events: [{
    event_ref: eventRef, preference_fit: "strong", preference_reason: "AI founderへの関心と合います。",
  }] };
  return { root, input: {
    dateInventory, preferenceRanking, profile,
    evidenceDir: path.join(root, "evidence"), repoRoot: path.resolve(__dirname, "../../.."),
  }, value, preferenceValue };
}

test("Connector judgment accepts only a Terra-pinned structured runner result", async () => {
  const { input, value } = await fixture();
  const result = await runConnectorLunaJudgment(input, {
    runAgentRunner: async ({ prompt, schema }) => ({
      summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" },
      value, prompt, schema,
    }),
  });
  assert.equal(isVerifiedEventGoalSerendipity(result), true);
  assert.equal(result.ranked_events[0].event_ref, value.ranked_events[0].event_ref);
});

test("Connector judgment rejects fallback models and unverified profiles", async () => {
  const { input, value } = await fixture();
  await assert.rejects(runConnectorLunaJudgment(input, {
    runAgentRunner: async () => ({
      summary: { status: "success", selected_provider: "claude-direct", selected_model: "sonnet" }, value,
    }),
  }), /Connector Luna judgment unavailable/);
  await assert.rejects(runConnectorLunaJudgment({ ...input, profile: { ...input.profile } }, {
    runAgentRunner: async () => ({
      summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value,
    }),
  }), /Connector Luna judgment unavailable/);
});

test("local runner pins Codex Terra and enforces timeout cancellation and token bounds", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-luna-runner-"));
  const evidenceDir = path.join(root, "evidence");
  const controller = new AbortController();
  let invocation;
  const result = await runLocalAgentRunner({
    prompt: "x".repeat(200),
    schema: { type: "object", properties: { ranked_events: { type: "array" } }, required: ["ranked_events"] },
    timeoutMs: 120_000,
    signal: controller.signal,
    readOnly: true,
    tokenBudget: 4_096,
    budgetScopeId: "connector-step-target-1",
    evidenceDir,
    repoRoot: path.resolve(__dirname, "../../.."),
    runnerPath: path.join(root, "agent_runner.py"),
  }, {
    spawnSync: (command, args, options) => {
      invocation = { command, args, options };
      const resultPath = path.join(evidenceDir, "attempt-01.result.json");
      fs.writeFileSync(resultPath, JSON.stringify({ ranked_events: [] }), { mode: 0o600 });
      return {
        status: 0,
        stdout: JSON.stringify({
          status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra", result_path: resultPath,
        }),
        stderr: "",
      };
    },
    isRunnerFile: () => true,
  });
  assert.deepEqual(result.value, { ranked_events: [] });
  assert.equal(invocation.command, "python3");
  assert.deepEqual(invocation.args.slice(0, 3), [path.join(root, "agent_runner.py"), "--task-class", "repeatable-agent"]);
  assert.equal(invocation.options.env.AGENT_RUNNER_PROVIDER, undefined);
  assert.equal(invocation.options.env.ANICCA_BUDGET_REQUIRED, "1");
  assert.equal(invocation.options.env.ANICCA_BUDGET_SCOPE_ID, "connector-step-target-1");
  assert.equal(invocation.options.env.ANICCA_PASS_TOKEN_BUDGET, "4096");
  assert.equal(invocation.options.signal, controller.signal);
  assert.equal(invocation.options.timeout, 125_000);
  assert.deepEqual(invocation.args.slice(-3), ["--timeout-seconds", "120", "--read-only"]);
  assert.equal(invocation.options.input, "x".repeat(200));

  await assert.doesNotReject(() => runLocalAgentRunner({
    prompt: "x".repeat(200),
    schema: { type: "object", properties: { ranked_events: { type: "array" } }, required: ["ranked_events"] },
    timeoutMs: 120_000,
    evidenceDir,
    repoRoot: path.resolve(__dirname, "../../.."),
    runnerPath: path.join(root, "agent_runner.py"),
  }, {
    spawnSync: invocation && (() => ({
      status: 0,
      stdout: JSON.stringify({
        status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra",
        result_path: path.join(evidenceDir, "attempt-01.result.json"),
      }),
      stderr: "",
    })),
    isRunnerFile: () => true,
  }));
});

test("local browser runner supplies the required explicit escalation reason", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-browser-runner-"));
  const evidenceDir = path.join(root, "evidence");
  let args;
  await runLocalAgentRunner({
    prompt: "x".repeat(200),
    schema: { type: "object", properties: { control: { type: "string" } }, required: ["control"] },
    taskClass: "browser-lane-agent", timeoutMs: 30_000, evidenceDir,
    repoRoot: path.resolve(__dirname, "../../.."), runnerPath: path.join(root, "agent_runner.py"),
  }, {
    spawnSync: (_command, suppliedArgs) => {
      args = suppliedArgs;
      const resultPath = path.join(evidenceDir, "attempt-01.result.json");
      fs.writeFileSync(resultPath, JSON.stringify({ control: "continue_button" }), { mode: 0o600 });
      return { status: 0, stdout: JSON.stringify({ status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra", result_path: resultPath }), stderr: "" };
    },
    isRunnerFile: () => true,
  });
  const index = args.indexOf("--escalation-reason");
  assert.notEqual(index, -1);
  assert.equal(args[index + 1], "unknown event registration UI requires bounded visual judgment");
});

test("local runner aborts its real child process instead of orphaning it", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "connector-luna-cancel-"));
  const runnerPath = path.join(root, "agent_runner.py");
  fs.writeFileSync(runnerPath, "import time\ntime.sleep(60)\n", { mode: 0o700 });
  const controller = new AbortController();
  const startedAt = Date.now();
  setTimeout(() => controller.abort(), 20);

  await assert.rejects(runLocalAgentRunner({
    prompt: "x".repeat(200),
    schema: { type: "object", properties: {}, required: [] },
    timeoutMs: 1_000,
    signal: controller.signal,
    evidenceDir: path.join(root, "evidence"),
    repoRoot: path.resolve(__dirname, "../../.."),
    runnerPath,
  }), /Connector Luna judgment unavailable/);

  assert.ok(Date.now() - startedAt < 1_000);
});

test("Luna creates the preference ranking before goal and serendipity judgment", async () => {
  const { input, value, preferenceValue } = await fixture();
  const calls = [];
  const result = await runConnectorLunaJudgment({
    ...input, preferenceRanking: undefined, date: "2026-08-02",
  }, {
    runAgentRunner: async ({ prompt }) => {
      calls.push(prompt);
      return {
        summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" },
        value: calls.length === 1 ? preferenceValue : value,
      };
    },
  });
  assert.equal(isVerifiedEventGoalSerendipity(result), true);
  assert.equal(calls.length, 2);
  assert.match(calls[0], /Preferences affect ordering/);
  assert.match(calls[1], /grounded serendipity potential/);
});
