"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createJsonlFinancialRecordStore } = require("../lib/financial-record-store.js");
const { createMoneytreeObservationStore } = require("../lib/moneytree-observation-store.js");
const { ingestFinancialRecords, splitPaths } = require("../lib/financial-manager-ingest.js");
const {
  runFinancialManager,
} = require("../lib/financial-manager-runtime.js");
const { renderFinancialManagerTelegram } = require("../lib/financial-manager-report.js");
const { notifyViaLocalOutbox } = require("../lib/financial-transition-local.js");

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function reportingDate(now) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(now);
}

function agentReceiptPathsFromEnv(env) {
  const agentStateRoot = env.LM_AGENT_ECONOMY_STATE_ROOT
    || path.join(os.homedir(), ".local/state/life-manager/agent-economy");
  const defaultJournal = path.join(agentStateRoot, "revenue-receipts.jsonl");
  return splitPaths(
    env.LM_CFO_AGENT_ECONOMY_RECEIPTS || env.REVENUE_RECEIPT_JOURNAL || defaultJournal,
  );
}

function readSnapshot(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    return value && value.schemaVersion === 1 && /^[a-f0-9]{64}$/.test(value.digest)
      ? value : null;
  }
  catch (error) {
    if (error && error.code === "ENOENT") return null;
    return null;
  }
}

function writeSnapshot(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.chmodSync(path.dirname(file), 0o700);
  const temporary = `${file}.${crypto.randomUUID()}.tmp`;
  let descriptor;
  try {
    descriptor = fs.openSync(temporary, "wx", 0o600);
    fs.writeFileSync(descriptor, `${JSON.stringify(value)}\n`);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporary, file);
    fs.chmodSync(file, 0o600);
    const directory = fs.openSync(path.dirname(file), "r");
    try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    try { fs.unlinkSync(temporary); } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
    }
  }
}

async function runHourlyCfo(options = {}) {
  const now = new Date(typeof options.now === "function" ? options.now() : (options.now || new Date()));
  if (!Number.isFinite(now.getTime())) throw new Error("CFO clock invalid");
  const date = reportingDate(now);
  if (typeof options.stateDir !== "string" || !options.stateDir.trim()) {
    throw new Error("CFO configuration invalid");
  }
  const stateDir = path.resolve(options.stateDir);
  const subjectId = String(options.subjectId || "").trim();
  if (!stateDir || stateDir === path.parse(stateDir).root || !ID.test(subjectId)) {
    throw new Error("CFO configuration invalid");
  }
  const store = options.store || createJsonlFinancialRecordStore({
    directoryPath: path.join(stateDir, "financial-records"),
  });
  const ingest = options.ingest || ingestFinancialRecords;
  const snapshotFile = path.join(stateDir, "last-delivered-snapshot.json");
  const notify = options.notify || ((input) => notifyViaLocalOutbox(input, options));
  const pending = readSnapshot(snapshotFile);
  if (pending?.status === "pending" && pending.reportingDate === date && pending.report) {
    const delivery = await notify({ eventKey: `cfo:${subjectId}:${date}`,
      observedAt: now.toISOString(), message: renderFinancialManagerTelegram(pending.report) });
    if (delivery?.delivery !== "delivered" || !String(delivery.provider_message_id || "").trim()) {
      return { status: "failed", reason: "telegram_delivery_uncertain", reportingDate: date,
        recordCount: pending.report.verifiedRecordCount || 0, delivered: false };
    }
    writeSnapshot(snapshotFile, { ...pending, status: "delivered",
      delivery: { delivery: "delivered", provider_message_id: String(delivery.provider_message_id) },
      deliveredAt: now.toISOString() });
    const duplicate = Number(delivery.attempted) === 0;
    return { status: duplicate ? "quiet" : "sent", reason: duplicate ? "unchanged" : null,
      reportingDate: date, recordCount: pending.report.verifiedRecordCount || 0,
      delivered: !duplicate, providerMessageId: String(delivery.provider_message_id) };
  }
  const result = await runFinancialManager({
    subjectId, reportingDate: date, timezone: "Asia/Tokyo", now, store,
    ingest: () => ingest({
      store, subjectId, now,
      moneytreeEvidenceStore: options.moneytreeEvidenceStore || createMoneytreeObservationStore({
        directoryPath: path.join(stateDir, "evidence", "moneytree"),
      }),
      agentReceiptPaths: options.agentReceiptPaths || [],
      marketplaceReceiptPaths: options.marketplaceReceiptPaths || [],
      pythonBin: options.pythonBin || "python3",
    }),
    deliveryStore: {
      lookup: () => {
        const previous = readSnapshot(snapshotFile);
        const previousDate = previous && (previous.reportingDate || previous.report?.reportingDate);
        return previousDate === date && previous.status !== "pending" ? previous : null;
      },
      claim: async ({ digest, report, observedAt }) => {
        writeSnapshot(snapshotFile, { schemaVersion: 1, status: "pending", reportingDate: date,
          digest, report, createdAt: observedAt });
        return { claimed: true };
      },
      markDelivered: ({ digest, report, delivery, observedAt }) => writeSnapshot(snapshotFile, {
        schemaVersion: 1, status: "delivered", digest, report,
        reportingDate: date,
        delivery: { delivery: "delivered", provider_message_id: delivery.providerMessageId },
        deliveredAt: observedAt,
      }),
    },
    eventKey: () => `cfo:${subjectId}:${date}`,
    notify: async (input) => {
      const delivery = await notify(input);
      return {
        delivered: delivery && delivery.delivery === "delivered",
        providerMessageId: delivery && delivery.provider_message_id,
      };
    },
  });
  // Local callers consume the compact, stable boundary rather than report internals.
  const { report, digest, duplicate, ...publicResult } = result;
  return publicResult;
}

async function main(env = process.env) {
  const stateDir = env.CFO_STATE_DIR || env.LIFE_MANAGER_STATE_ROOT
    || path.join(os.homedir(), ".local/state/life-manager/life-manager-cfo-hourly");
  try {
    const result = await runHourlyCfo({
      stateDir,
      subjectId: env.LM_CFO_SUBJECT_ID || env.LM_CFO_UID || env.LM_UID,
      pythonBin: env.CFO_PYTHON_BIN || "python3",
      database: env.CFO_TELEGRAM_OUTBOX || path.join(stateDir, "telegram-outbox.sqlite3"),
      chatId: env.TELEGRAM_ALERT_CHAT_ID || env.LM_CFO_TELEGRAM_CHAT_ID || env.LM_ADMIN_TELEGRAM_CHAT_ID,
      envFile: env.LIFE_MANAGER_ENV_FILE || path.join(os.homedir(), ".local/state/life-manager/.env"),
      agentReceiptPaths: agentReceiptPathsFromEnv(env),
      marketplaceReceiptPaths: splitPaths(env.LM_CFO_MARKETPLACE_RECEIPTS),
    });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return ["sent", "quiet"].includes(result.status) ? 0 : 1;
  } catch {
    process.stdout.write(`${JSON.stringify({
      status: "failed", reason: "cfo_boundary_failed", reportingDate: null,
      recordCount: 0, delivered: false,
    })}\n`);
    return 1;
  }
}

if (require.main === module) main().then((code) => { process.exitCode = code; });

module.exports = { agentReceiptPathsFromEnv, main, runHourlyCfo };
