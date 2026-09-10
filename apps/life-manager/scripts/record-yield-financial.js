#!/usr/bin/env node
"use strict";

const os = require("node:os");
const path = require("node:path");
const { createJsonlFinancialRecordStore } = require("../lib/financial-record-store.js");
const { yieldResultToFinancialRecord } = require("../lib/yield-financial-record.js");

async function main(deps = {}, argv = process.argv.slice(2)) {
  const env = deps.env || process.env;
  const result = JSON.parse(argv[0] || "null");
  const timestamp = result?.occurred_at || new Date().toISOString();
  const record = yieldResultToFinancialRecord(result, {
    subjectId: env.LM_CFO_SUBJECT_ID || env.LM_UID || "local",
    occurredAt: timestamp, recordedAt: timestamp,
  });
  if (!record) return { ok: true, skipped: true };
  const store = deps.store || createJsonlFinancialRecordStore({ directoryPath:
    env.LM_FINANCIAL_RECORDS_DIR || path.join(env.CFO_STATE_DIR
      || path.join(os.homedir(), ".local", "state", "life-manager", "life-manager-cfo-hourly"), "financial-records") });
  const write = await store.append(record);
  return { ok: true, duplicate: write.created === false, record_id: record.record_id };
}

if (require.main === module) main().then((value) => process.stdout.write(`${JSON.stringify(value)}\n`))
  .catch((error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });

module.exports = { main };
