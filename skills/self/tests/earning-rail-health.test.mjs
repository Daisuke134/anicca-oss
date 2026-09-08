import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  aggregatePortfolio,
  buildHealthReport,
  classifySlotProbe,
  observeSlot,
  resolveProbePath,
} from "../earning-rail-health.mjs";


const NOW = Date.parse("2026-07-28T04:30:00Z");
const REGISTRY_PATH = fileURLToPath(new URL("../earning-health-registry.json", import.meta.url));


test("fresh trace with a normal wait is operational", () => {
  const result = classifySlotProbe({
    slot: { id: "earn/polymarket-trade", probe: { kind: "trace", maxAgeSeconds: 3600 } },
    observation: {
      exists: true,
      mtimeMs: NOW - 60_000,
      killPresent: false,
      barren: false,
      lastAction: "wait",
    },
    nowMs: NOW,
  });
  assert.equal(result.state, "operational");
  assert.equal(result.reason, "fresh_trace");
});


test("intentional KILL is frozen, while stale or barren trace is degraded", () => {
  const slot = { id: "earn/sol-trade", probe: { kind: "trace", maxAgeSeconds: 3600 } };
  assert.equal(classifySlotProbe({
    slot,
    observation: { exists: true, mtimeMs: NOW, killPresent: true, barren: true },
    nowMs: NOW,
  }).state, "frozen");
  assert.equal(classifySlotProbe({
    slot,
    observation: { exists: true, mtimeMs: NOW - 3_601_000, killPresent: false, barren: false },
    nowMs: NOW,
  }).state, "degraded");
  assert.equal(classifySlotProbe({
    slot,
    observation: { exists: true, mtimeMs: NOW, killPresent: false, barren: true },
    nowMs: NOW,
  }).reason, "sustained_mechanism_failure");
});


test("HTTP readiness requires a bounded successful 2xx probe", () => {
  const slot = { id: "x402_sell", probe: { kind: "http" } };
  assert.equal(classifySlotProbe({
    slot,
    observation: { status: 200, checkedAtMs: NOW },
    nowMs: NOW,
  }).state, "operational");
  const failed = classifySlotProbe({
    slot,
    observation: { status: 503, checkedAtMs: NOW, error: "upstream unavailable" },
    nowMs: NOW,
  });
  assert.equal(failed.state, "degraded");
  assert.equal(failed.evidence.httpStatus, 503);
  assert.equal("error" in failed.evidence, false);
});


test("heartbeat distinguishes not-live, degraded, and operational", () => {
  const slot = { id: "economy/gig", probe: { kind: "heartbeat", maxAgeSeconds: 5400 } };
  assert.equal(classifySlotProbe({
    slot,
    observation: { enabled: false, alive: false, exists: false },
    nowMs: NOW,
  }).state, "not-live");
  assert.equal(classifySlotProbe({
    slot,
    observation: { enabled: true, alive: false, exists: true, mtimeMs: NOW },
    nowMs: NOW,
  }).state, "degraded");
  assert.equal(classifySlotProbe({
    slot,
    observation: { enabled: true, alive: true, exists: true, mtimeMs: NOW - 60_000 },
    nowMs: NOW,
  }).state, "operational");
  assert.equal(classifySlotProbe({
    slot,
    observation: { enabled: true, alive: true, exists: true, mtimeMs: NOW - 5_401_000 },
    nowMs: NOW,
  }).reason, "stale_heartbeat");
});


test("funded account is operational at positive balance and not-live at zero", () => {
  const slot = { id: "hl_trade", probe: { kind: "funded-account" } };
  assert.equal(classifySlotProbe({
    slot,
    observation: { ok: true, balanceUsd: 12.5, checkedAtMs: NOW },
    nowMs: NOW,
  }).state, "operational");
  const empty = classifySlotProbe({
    slot,
    observation: { ok: true, balanceUsd: 0, checkedAtMs: NOW },
    nowMs: NOW,
  });
  assert.equal(empty.state, "not-live");
  assert.equal(empty.reason, "unfunded");
  assert.equal(empty.evidence.balanceUsd, 0);
  assert.equal(classifySlotProbe({
    slot,
    observation: { ok: false, checkedAtMs: NOW },
    nowMs: NOW,
  }).state, "degraded");
});


test("explicit activation is not-live until enabled", () => {
  const slot = { id: "token_launch", probe: { kind: "explicit" } };
  assert.equal(classifySlotProbe({
    slot,
    observation: { enabled: false, checkedAtMs: NOW },
    nowMs: NOW,
  }).state, "not-live");
  assert.equal(classifySlotProbe({
    slot,
    observation: { enabled: true, checkedAtMs: NOW },
    nowMs: NOW,
  }).state, "operational");
});


test("portfolio aggregation uses deterministic state priority without hiding members", () => {
  const members = [
    { id: "pm", state: "operational" },
    { id: "sol", state: "frozen" },
    { id: "hl", state: "not-live" },
  ];
  const live = aggregatePortfolio({ id: "CAPITAL", memberIds: ["pm", "sol", "hl"] }, members, NOW);
  assert.equal(live.state, "operational");
  assert.deepEqual(live.evidence.members, {
    pm: "operational",
    sol: "frozen",
    hl: "not-live",
  });

  assert.equal(aggregatePortfolio(
    { id: "WORK", memberIds: ["gig", "clip"] },
    [{ id: "gig", state: "degraded" }, { id: "clip", state: "not-live" }],
    NOW,
  ).state, "degraded");
  assert.equal(aggregatePortfolio(
    { id: "CAPITAL", memberIds: ["sol", "hl"] },
    [{ id: "sol", state: "frozen" }, { id: "hl", state: "not-live" }],
    NOW,
  ).state, "frozen");
  assert.equal(aggregatePortfolio(
    { id: "WORK", memberIds: ["clip", "video"] },
    [{ id: "clip", state: "not-live" }, { id: "video", state: "not-live" }],
    NOW,
  ).state, "not-live");
});


test("malformed or unknown probes fail closed to degraded", () => {
  const missing = classifySlotProbe({
    slot: { id: "broken", probe: { kind: "http" } },
    observation: null,
    nowMs: NOW,
  });
  assert.equal(missing.state, "degraded");
  assert.equal(missing.reason, "probe_unavailable");
  assert.equal(classifySlotProbe({
    slot: { id: "unknown", probe: { kind: "magic" } },
    observation: {},
    nowMs: NOW,
  }).state, "degraded");
});


test("production registry has zero instrumentation gaps and exactly four valid portfolios", () => {
  const registry = JSON.parse(fs.readFileSync(REGISTRY_PATH, "utf8"));
  assert.equal(registry.$schema, "earning-health-registry v2");
  assert.equal(registry.slots.length, 8);
  assert.deepEqual(
    registry.slots.filter((slot) => slot.instrumented !== true).map((slot) => slot.id),
    [],
  );
  assert.deepEqual(
    registry.slots.filter((slot) => !slot.probe?.kind).map((slot) => slot.id),
    [],
  );
  assert.deepEqual(
    registry.portfolios.map((portfolio) => portfolio.id).sort(),
    ["CAPITAL", "PM", "WORK", "x402"],
  );
  const slotIds = new Set(registry.slots.map((slot) => slot.id));
  const invalidMembers = registry.portfolios.flatMap((portfolio) =>
    portfolio.memberIds.filter((id) => !slotIds.has(id)).map((id) => `${portfolio.id}:${id}`));
  assert.deepEqual(invalidMembers, []);
});


test("buildHealthReport probes every slot once and aggregates all portfolios", async () => {
  const registry = {
    slots: [
      { id: "pm", instrumented: true, probe: { kind: "trace", maxAgeSeconds: 60 } },
      { id: "gig", instrumented: true, probe: { kind: "heartbeat", maxAgeSeconds: 60 } },
    ],
    portfolios: [
      { id: "PM", memberIds: ["pm"] },
      { id: "WORK", memberIds: ["gig"] },
    ],
  };
  const calls = [];
  const report = await buildHealthReport({
    registry,
    nowMs: NOW,
    observe: async (slot) => {
      calls.push(slot.id);
      return slot.id === "pm"
        ? { exists: true, mtimeMs: NOW, killPresent: false, barren: false, lastAction: "wait" }
        : { enabled: false, alive: false, exists: false };
    },
  });
  assert.deepEqual(calls, ["pm", "gig"]);
  assert.deepEqual(report.slots.map(({ id, state }) => ({ id, state })), [
    { id: "pm", state: "operational" },
    { id: "gig", state: "not-live" },
  ]);
  assert.deepEqual(report.portfolios.map(({ id, state }) => ({ id, state })), [
    { id: "PM", state: "operational" },
    { id: "WORK", state: "not-live" },
  ]);
  assert.equal(report.notInstrumentedCount, 0);
  assert.equal(report.generatedAt, new Date(NOW).toISOString());
});


test("probe paths are instance-relative for runtime state and home-relative for loop heartbeats", () => {
  assert.equal(
    resolveProbePath("skills/earn/state/pm-trade.trace.jsonl", {
      base: "runtime",
      runtimeRoot: "/instance",
      homeDir: "/human",
    }),
    "/instance/skills/earn/state/pm-trade.trace.jsonl",
  );
  assert.equal(
    resolveProbePath("gig/.last-pass", {
      base: "home",
      runtimeRoot: "/instance",
      homeDir: "/human",
    }),
    "/human/gig/.last-pass",
  );
  assert.throws(
    () => resolveProbePath("../../secret", {
      base: "runtime",
      runtimeRoot: "/instance",
      homeDir: "/human",
    }),
    /escape/,
  );
});


test("observeSlot performs bounded read-only probes without returning raw diagnostics", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "earn-hc-observe-"));
  const runtimeRoot = path.join(dir, "runtime");
  const homeDir = path.join(dir, "home");
  try {
    const tracePath = path.join(runtimeRoot, "skills/earn/state/pm.jsonl");
    const heartbeatPath = path.join(homeDir, "gig/.last-pass");
    const activationPath = path.join(runtimeRoot, "skills/earn/token-launch/LIVE");
    const hlPython = path.join(runtimeRoot, "skills/earn/hl-trade/.venv/bin/python");
    const hlScript = path.join(runtimeRoot, "skills/earn/hl-trade/hl.py");
    fs.mkdirSync(path.dirname(tracePath), { recursive: true });
    fs.mkdirSync(path.dirname(heartbeatPath), { recursive: true });
    fs.mkdirSync(path.dirname(activationPath), { recursive: true });
    fs.mkdirSync(path.dirname(hlPython), { recursive: true });
    fs.writeFileSync(tracePath, '{"action":"wait","reason":"no demand"}\n');
    fs.writeFileSync(heartbeatPath, "");
    fs.writeFileSync(hlPython, "");
    fs.writeFileSync(hlScript, "");

    const trace = await observeSlot({
      id: "pm",
      minRun: 20,
      probe: {
        kind: "trace",
        tracePath: "skills/earn/state/pm.jsonl",
        killPath: ".local/state/life-manager/polymarket/KILL",
        killPathBase: "home",
      },
    }, {
      runtimeRoot,
      homeDir,
      barrenImpl: () => false,
    });
    assert.equal(trace.exists, true);
    assert.equal(trace.killPresent, false);
    assert.equal(trace.lastAction, "wait");

    const http = await observeSlot({
      id: "x402",
      probe: { kind: "http", url: "https://example.test/health", timeoutMs: 10 },
    }, {
      runtimeRoot,
      homeDir,
      fetchImpl: async () => ({ status: 200 }),
    });
    assert.equal(http.status, 200);

    const heartbeat = await observeSlot({
      id: "gig",
      probe: {
        kind: "heartbeat",
        heartbeatPath: "gig/.last-pass",
        sessionSocket: "/tmp/fake.sock",
        sessionName: "gig",
        launchdLabel: "ai.example.gig",
      },
    }, {
      runtimeRoot,
      homeDir,
      spawnSyncImpl: (command) => ({ status: command === "tmux" || command === "launchctl" ? 0 : 1 }),
    });
    assert.equal(heartbeat.enabled, true);
    assert.equal(heartbeat.alive, true);
    assert.equal(heartbeat.exists, true);

    const account = await observeSlot({
      id: "hl",
      probe: { kind: "funded-account", adapter: "hyperliquid" },
    }, {
      runtimeRoot,
      homeDir,
      spawnSyncImpl: () => ({
        status: 0,
        stdout: JSON.stringify({ account_value_usd: 0, private_key: "must-not-escape" }),
      }),
    });
    assert.deepEqual(account, { ok: true, balanceUsd: 0, checkedAtMs: account.checkedAtMs });
    assert.equal(JSON.stringify(account).includes("must-not-escape"), false);

    const explicit = await observeSlot({
      id: "token",
      probe: { kind: "explicit", activationPath: "skills/earn/token-launch/LIVE" },
    }, { runtimeRoot, homeDir });
    assert.equal(explicit.enabled, false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
