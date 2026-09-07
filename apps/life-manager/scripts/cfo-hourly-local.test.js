"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { main, runHourlyCfo } = require("./cfo-hourly-local.js");

test("CFO source closure loads and fails closed when required runtime configuration is absent", async () => {
  const result = await runHourlyCfo({
    env: {},
    now: new Date("2026-09-07T00:00:00.000Z"),
  });

  assert.deepEqual(result, {
    status: "failed",
    reportingDate: "2026-09-07",
    revision: null,
    appended: false,
    delivered: false,
    recovered: false,
    providerDataFreshness: "unknown",
    latestReturnedTransactionDate: null,
  });
});

test("CLI boundary emits one redacted terminal summary without provider configuration", async () => {
  const lines = [];
  const result = await main({
    env: {},
    now: new Date("2026-09-07T00:00:00.000Z"),
    stdout: (line) => lines.push(line),
    runLocalAgentUsageCollection: async () => ({ status: "skipped" }),
  });

  assert.equal(result.exitCode, 1);
  assert.equal(lines.length, 1);
  assert.deepEqual(JSON.parse(lines[0]), {
    status: "failed",
    reportingDate: "2026-09-07",
    revision: null,
    appended: false,
    delivered: false,
    recovered: false,
    providerDataFreshness: "unknown",
    latestReturnedTransactionDate: null,
    providerBilling: {
      status: "unavailable",
      confirmedCount: 0,
      unresolvedCount: 0,
      unavailableCount: 1,
    },
  });
});
