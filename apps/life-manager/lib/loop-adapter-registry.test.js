"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createConfiguredLoopAdapterRegistry,
  createLoopAdapterRegistry,
  loadLoopAdapterManifest,
} = require("./loop-adapter-registry.js");
const { CAPABILITY: CONNECTOR_COVERAGE_CAPABILITY } = require("./connector-coverage-job.js");
const {
  CAPABILITY: FINANCIAL_REPORT_CAPABILITY,
  createFinancialReportLoopAdapter,
} = require("./report-job-adapter.js");
const {
  CAPABILITY: MARKETING_DAILY_CAPABILITY,
} = require("./marketing-daily-adapter.js");
const {
  CAPABILITY: MARKETING_GENERATION_CAPABILITY,
} = require("./marketing-daily-generation-adapter.js");
const {
  CAPABILITY: MARKETING_OBSERVATION_CAPABILITY,
} = require("./marketing-observation-adapter.js");
const {
  CAPABILITY: MARKETING_VIDEO_GENERATION_CAPABILITY,
} = require("./marketing-video-generation-adapter.js");
const {
  CAPABILITY: MARKETING_LIVENESS_CAPABILITY,
} = require("./marketing-liveness-adapter.js");
const {
  CAPABILITY: LUMA_RSVP_CAPABILITY,
} = require("./luma-rsvp-adapter.js");
const {
  CAPABILITY: MARKETPLACE_APPLICATION_CAPABILITY,
} = require("./marketplace-application-adapter.js");
const {
  CAPABILITY: GENERAL_AGENT_WORK_CAPABILITY,
} = require("./general-agent-work-adapter.js");
const {
  CAPABILITY: MONEY_PRINTER_SCOUT_CAPABILITY,
} = require("./money-printer-scout.js");

const MANIFEST_PATH = path.join(
  __dirname,
  "../config/loop-adapters.json",
);
const INVENTORY_PATH = path.join(
  __dirname,
  "../../../docs/migrations/openclaw/runtime-inventory.json",
);

function validAdapter(overrides = {}) {
  return {
    plan: async () => [],
    execute: async () => ({ receipt: { kind: "fixture" } }),
    reconcile: async () => ({ state: "unknown" }),
    verify: () => true,
    report: () => ({ status: "ok" }),
    ...overrides,
  };
}

function validDefinition(overrides = {}) {
  return {
    adapter_id: "fixture-adapter",
    loop_id: "fixture.loop",
    capability: "fixture.execute",
    effect_classes: ["message"],
    module_ref: "lib/fixture-adapter.js",
    factory_export: "createFixtureAdapter",
    ...overrides,
  };
}

test("registry requires the complete plan/execute/reconcile/verify/report contract", async () => {
  const definition = validDefinition();
  const reconciled = [];
  const adapter = validAdapter({
    reconcile: async (effect) => {
      reconciled.push(effect);
      return { state: "unknown" };
    },
  });
  const registry = createLoopAdapterRegistry({
    definitions: [definition],
    adapters: { [definition.adapter_id]: adapter },
  });

  assert.equal(registry.getByCapability("fixture.execute"), adapter);
  assert.equal(registry.getByLoopId("fixture.loop"), adapter);
  assert.deepEqual(registry.list(), [definition]);
  const effectAdapter = registry.getReconciliationAdapter("fixture.execute");
  assert.deepEqual(
    await effectAdapter.inspectEffect({ effectKey: "fixture:effect" }),
    { state: "unknown" },
  );
  assert.deepEqual(reconciled, [{ effectKey: "fixture:effect" }]);

  for (const missing of ["plan", "execute", "reconcile", "verify", "report"]) {
    const broken = validAdapter();
    delete broken[missing];
    assert.throws(
      () => createLoopAdapterRegistry({
        definitions: [definition],
        adapters: { [definition.adapter_id]: broken },
      }),
      new RegExp(missing, "i"),
    );
  }
});

test("registry rejects duplicate routing, absolute paths, and credential-shaped config", () => {
  const definition = validDefinition();
  const adapter = validAdapter();
  const make = (definitions) => createLoopAdapterRegistry({
    definitions,
    adapters: Object.fromEntries(
      definitions.map((item) => [item.adapter_id, adapter]),
    ),
  });

  assert.throws(
    () => make([
      definition,
      validDefinition({ adapter_id: "duplicate", loop_id: "other.loop" }),
    ]),
    /capability.*duplicate/i,
  );
  assert.throws(
    () => make([
      definition,
      validDefinition({ adapter_id: "duplicate", capability: "other.execute" }),
    ]),
    /loop.*duplicate/i,
  );
  assert.throws(
    () => make([
      validDefinition({
        module_ref: ["", "Users", "example", ".openclaw", "skills", "fixture.js"].join("/"),
      }),
    ]),
    /module.*portable/i,
  );
  assert.throws(
    () => make([
      validDefinition({ api_key: "raw-provider-secret" }),
    ]),
    /credential/i,
  );
  assert.throws(
    () => make([
      validDefinition({ module_ref: "secret://provider/raw-token" }),
    ]),
    /module.*portable/i,
  );
});

test("committed manifest is portable and registers financial report first and Connector coverage last", () => {
  const manifest = loadLoopAdapterManifest(MANIFEST_PATH);
  assert.equal(manifest.schema_version, 1);
  assert.equal(manifest.adapters.length, 13);
  assert.equal(
    manifest.adapters[0].capability,
    FINANCIAL_REPORT_CAPABILITY,
  );
  assert.equal(manifest.adapters[0].adapter_id, "financial-report-telegram");
  assert.equal(manifest.adapters[1].capability, MARKETING_DAILY_CAPABILITY);
  assert.equal(manifest.adapters[1].adapter_id, "marketing-life-manager-daily");
  assert.equal(manifest.adapters[2].capability, MARKETING_GENERATION_CAPABILITY);
  assert.equal(
    manifest.adapters[2].adapter_id,
    "marketing-life-manager-daily-generation",
  );
  assert.equal(manifest.adapters[3].capability, MARKETING_OBSERVATION_CAPABILITY);
  assert.equal(
    manifest.adapters[3].adapter_id,
    "marketing-platform-observation",
  );
  assert.equal(
    manifest.adapters[4].capability,
    MARKETING_VIDEO_GENERATION_CAPABILITY,
  );
  assert.equal(
    manifest.adapters[4].adapter_id,
    "marketing-video-generation",
  );
  assert.equal(manifest.adapters[5].capability, "marketing.video.publish");
  assert.equal(manifest.adapters[5].adapter_id, "marketing-video-publication");
  assert.equal(manifest.adapters[6].capability, MARKETING_LIVENESS_CAPABILITY);
  assert.equal(manifest.adapters[6].adapter_id, "marketing-liveness-telegram");
  assert.equal(manifest.adapters[7].capability, LUMA_RSVP_CAPABILITY);
  assert.equal(manifest.adapters[7].adapter_id, "outbound-luma-rsvp");
  assert.equal(manifest.adapters[8].capability, GENERAL_AGENT_WORK_CAPABILITY);
  assert.equal(manifest.adapters[8].adapter_id, "general-agent-work");
  assert.equal(manifest.adapters[9].capability, MONEY_PRINTER_SCOUT_CAPABILITY);
  assert.equal(manifest.adapters[9].adapter_id, "money-printer-scout");
  assert.equal(manifest.adapters[10].capability, MARKETPLACE_APPLICATION_CAPABILITY);
  assert.equal(manifest.adapters[10].adapter_id, "marketplace-application");
  assert.equal(manifest.adapters[11].capability, "agent-economy.start");
  assert.equal(manifest.adapters[11].adapter_id, "agent-economy-cloud");
  assert.equal(manifest.adapters[12].capability, CONNECTOR_COVERAGE_CAPABILITY);
  assert.equal(manifest.adapters[12].adapter_id, "connector-coverage-refresh");
  assert.doesNotMatch(
    fs.readFileSync(MANIFEST_PATH, "utf8"),
    /\.openclaw|profitable-claude|life-manager-v0|\/Users\/|api[_-]?key|password|token\s*":/i,
  );
});

test("configured registry exposes marketplace application without enabling a worker", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
  });
  assert.equal(registry.hasCapability(MARKETPLACE_APPLICATION_CAPABILITY), true);
  const adapter = registry.getByCapability(MARKETPLACE_APPLICATION_CAPABILITY);
  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("configured registry exposes bounded general-agent work without enabling a worker", () => {
  const registry = createConfiguredLoopAdapterRegistry({ appRoot: path.join(__dirname, "..") });
  assert.equal(registry.hasCapability(GENERAL_AGENT_WORK_CAPABILITY), true);
  const adapter = registry.getByCapability(GENERAL_AGENT_WORK_CAPABILITY);
  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("financial report inventory row is owned while its legacy rollback remains explicit", () => {
  const inventory = JSON.parse(fs.readFileSync(INVENTORY_PATH, "utf8"));
  const row = inventory.jobs.find(
    (job) => job.legacy_id === "ai.anicca.life-manager-financial-report",
  );
  const unclassified = inventory.jobs.filter(
    (job) => job.disposition === "unclassified",
  ).length;

  assert.equal(row.disposition, "migrate");
  assert.equal(row.owner, "life-manager-runtime");
  assert.equal(row.target_adapter, "financial-report-telegram");
  assert.equal(row.effect_class, "message");
  assert.match(row.verify_command, /test:runtime-adapters/);
  assert.match(row.rollback_action, /Keep .* loaded until seven expected/i);
  assert.equal(inventory.summary.unclassified, unclassified);
});

test("Life Manager daily marketing inventory row is owned without disabling its rollback", () => {
  const inventory = JSON.parse(fs.readFileSync(INVENTORY_PATH, "utf8"));
  const row = inventory.jobs.find(
    (job) => job.legacy_id === "ai.anicca.life-manager-daily",
  );
  const unclassified = inventory.jobs.filter(
    (job) => job.disposition === "unclassified",
  ).length;

  assert.equal(row.disposition, "migrate");
  assert.equal(row.owner, "life-manager-runtime");
  assert.equal(row.target_adapter, "marketing-life-manager-daily");
  assert.deepEqual(row.supporting_adapters, [
    "marketing-life-manager-daily-generation",
    "marketing-platform-observation",
  ]);
  assert.equal(row.effect_class, "publish");
  assert.match(row.verify_command, /test:runtime-adapters/);
  assert.match(row.rollback_action, /Keep .* loaded until seven expected/i);
  assert.equal(inventory.summary.unclassified, unclassified);
});

test("Honne JA ReelClaw is assigned to the shared video adapter without cutting over launchd", () => {
  const inventory = JSON.parse(fs.readFileSync(INVENTORY_PATH, "utf8"));
  const row = inventory.jobs.find(
    (job) => job.legacy_id === "ai.anicca.reelclaw-honne-ja",
  );
  const unclassified = inventory.jobs.filter(
    (job) => job.disposition === "unclassified",
  ).length;

  assert.equal(row.disposition, "migrate");
  assert.equal(row.owner, "life-manager-runtime");
  assert.equal(row.target_adapter, "marketing-video-generation");
  assert.deepEqual(row.supporting_adapters, [
    "marketing-platform-observation",
  ]);
  assert.equal(row.effect_class, "publish");
  assert.match(row.verify_command, /test:runtime-adapters/);
  assert.match(row.rollback_action, /Keep .* loaded .* seven expected/i);
  assert.equal(inventory.summary.unclassified, unclassified);
});

test("configured registry loads the committed financial adapter implementation", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
    servicesByAdapter: {
      "financial-report-telegram": {
        secretProvider: { get: async () => "unused" },
      },
    },
  });
  const adapter = registry.getByCapability(FINANCIAL_REPORT_CAPABILITY);

  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("configured registry loads the portable Life Manager daily marketing adapter", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
  });
  const adapter = registry.getByCapability(MARKETING_DAILY_CAPABILITY);

  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("configured registry loads the portable Life Manager daily generation adapter", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
  });
  const adapter = registry.getByCapability(MARKETING_GENERATION_CAPABILITY);

  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("configured registry loads the portable marketing observation adapter", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
  });
  const adapter = registry.getByCapability(MARKETING_OBSERVATION_CAPABILITY);

  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("configured registry loads the generic marketing video generation adapter", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
  });
  const adapter = registry.getByCapability(MARKETING_VIDEO_GENERATION_CAPABILITY);

  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("configured registry loads the portable Luma RSVP adapter", () => {
  const registry = createConfiguredLoopAdapterRegistry({
    appRoot: path.join(__dirname, ".."),
  });
  const adapter = registry.getByCapability(LUMA_RSVP_CAPABILITY);

  for (const method of ["plan", "execute", "reconcile", "verify", "report"]) {
    assert.equal(typeof adapter[method], "function");
  }
});

test("financial report loop adapter plans, verifies, reports, and reconciles fail-closed", async () => {
  const adapter = createFinancialReportLoopAdapter({
    secretProvider: { get: async () => "unused" },
    inspectEffect: async ({ effectKey }) => ({
      state: "present",
      receipt: {
        schema_version: 1,
        kind: "telegram_financial_report",
        status: "sent",
        message_id: 432,
        chat_id_hash: "a".repeat(64),
        snapshot_hash: "b".repeat(64),
        sent_at: "2026-07-29T12:19:57.000Z",
        source_freshness: {
          report_cutoff_at: "2026-07-29T12:19:00.000Z",
          earnings_latest_at: null,
          costs_latest_at: null,
          balance_observed_at: "2026-07-29T12:19:00.000Z",
        },
        checked_effect_key: effectKey,
      },
    }),
  });
  const jobs = await adapter.plan({
    tenantId: "tenant-a",
    nowMs: Date.parse("2026-07-29T12:19:00.000Z"),
    telegramTokenRef: "secret://telegram/bot-token",
  });

  assert.equal(jobs.length, 1);
  assert.deepEqual(
    jobs.map((job) => job.capability),
    [FINANCIAL_REPORT_CAPABILITY],
  );

  const sentReceipt = {
    schema_version: 1,
    kind: "telegram_financial_report",
    status: "sent",
    message_id: 432,
    chat_id_hash: "a".repeat(64),
    snapshot_hash: "b".repeat(64),
    sent_at: "2026-07-29T12:19:57.000Z",
    source_freshness: {
      report_cutoff_at: "2026-07-29T12:19:00.000Z",
      earnings_latest_at: null,
      costs_latest_at: null,
      balance_observed_at: "2026-07-29T12:19:00.000Z",
    },
  };
  assert.equal(adapter.verify(sentReceipt), true);
  assert.deepEqual(adapter.report(sentReceipt), {
    status: "sent",
    message_id: 432,
    snapshot_hash: "b".repeat(64),
    sent_at: "2026-07-29T12:19:57.000Z",
    source_freshness: sentReceipt.source_freshness,
  });
  assert.doesNotMatch(JSON.stringify(adapter.report(sentReceipt)), /token|chat_id/i);

  assert.deepEqual(
    await adapter.reconcile({ effectKey: "telegram:financial:fixture" }),
    {
      state: "present",
      receipt: {
        ...sentReceipt,
        checked_effect_key: "telegram:financial:fixture",
      },
    },
  );
  assert.equal(
    (await createFinancialReportLoopAdapter({}).reconcile({
      effectKey: "telegram:financial:unknown",
    })).state,
    "unknown",
  );
  assert.equal(adapter.verify({ kind: "telegram_financial_report", status: "sent" }), false);
});
