#!/usr/bin/env node
"use strict";

const { createHash } = require("node:crypto");

const { buildRuntimeJob, enqueueJob } = require("./runtime-job-store.js");
const { financialRecordId } = require("./financial-record-contract.js");
const { createPostgresFinancialRecordStore } = require("./financial-record-store.js");
const { usdMicrosFromDecimal } = require("./financial-money.js");
const { runFinancialManager } = require("./financial-manager-runtime.js");
const { hashChatId, sendMessage } = require("./telegram.js");

const CAPABILITY = "report.financial.telegram";
const LOOP_ID = "financial.report";
const SECRET_REF = /^secret:\/\/[a-z0-9][a-z0-9._-]*(?:\/[a-z0-9][a-z0-9._-]*)*$/i;
const REPORT_KINDS = new Set(["daily", "weekly"]);
const HASH = /^[0-9a-f]{64}$/;

function legacyFinancialRuntime() {
  // Transitional persistence helpers stay lazy: the cloud worker has the
  // production dependency set, while portable adapter-contract tests do not.
  return require("./financial-report-runtime.js");
}

function walletLedgerRuntime() {
  return require("./payout-runtime.js");
}

function iso(value, label) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error(`${label} is invalid`);
  return date.toISOString();
}

function reportingDateForZone(nowMs, timezone = "Asia/Tokyo") {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(nowMs)).filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function reportBounds(kind, nowMs, timezone) {
  const today = reportingDateForZone(nowMs, timezone);
  const addDays = (key, days) => {
    const [year, month, day] = key.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
  };
  const midnight = (key) => {
    const [year, month, day] = key.split("-").map(Number);
    const wallUtc = Date.UTC(year, month - 1, day);
    let instant = wallUtc;
    for (let pass = 0; pass < 2; pass += 1) {
      const rendered = reportingDateForZone(instant, timezone);
      const clock = Object.fromEntries(new Intl.DateTimeFormat("en", {
        timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
      }).formatToParts(new Date(instant)).filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]));
      const represented = Date.UTC(
        ...rendered.split("-").map(Number).map((value, index) => index === 1 ? value - 1 : value),
        Number(clock.hour), Number(clock.minute), Number(clock.second),
      );
      instant = wallUtc - (represented - instant);
    }
    return new Date(instant).toISOString();
  };
  let start = today;
  let periodKey = today;
  if (kind === "weekly") {
    const [year, month, day] = today.split("-").map(Number);
    const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay() || 7;
    start = addDays(today, -(weekday - 1));
    const monday = new Date(`${start}T00:00:00.000Z`);
    const thursday = new Date(monday.getTime() + 3 * 86_400_000);
    const weekYear = thursday.getUTCFullYear();
    const fourth = new Date(Date.UTC(weekYear, 0, 4));
    const firstMonday = new Date(fourth.getTime() - (((fourth.getUTCDay() || 7) - 1) * 86_400_000));
    periodKey = `${weekYear}-W${String(Math.floor((monday - firstMonday) / 604_800_000) + 1).padStart(2, "0")}`;
  }
  return { period_key: periodKey, period_start: midnight(start), period_end: new Date(nowMs).toISOString() };
}

function dueConsolidatedReport(kind, nowMs, timezone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en", {
    timeZone: timezone, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(nowMs)).filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
  const minuteOfDay = Number(parts.hour) * 60 + Number(parts.minute);
  if (kind === "daily") return minuteOfDay >= 20 * 60;
  return parts.weekday === "Sun" && minuteOfDay >= (20 * 60) + 5;
}

function latest(rows, field, cutoff) {
  const cutoffMs = Date.parse(cutoff);
  return (Array.isArray(rows) ? rows : []).reduce((value, row) => {
    const candidate = String(row && row[field] || "");
    const at = Date.parse(candidate);
    return Number.isFinite(at) && at <= cutoffMs && (!value || at > Date.parse(value))
      ? new Date(at).toISOString() : value;
  }, null);
}

function completeSentReceipt(receipt) {
  return Boolean(
    receipt && receipt.status === "sent"
    && Number.isInteger(Number(receipt.telegram_message_id))
    && Number(receipt.telegram_message_id) > 0
    && HASH.test(String(receipt.snapshot_hash || ""))
    && Number.isFinite(Date.parse(String(receipt.sent_at || ""))),
  );
}

function record({ subjectId, key, scope, kind, direction, amountMinor, currency, occurredAt, recordedAt, provider, sourceType, externalRef, verification = "verified" }) {
  const idempotencyKey = String(key);
  return {
    schema_version: 1, record_type: "financial_record",
    record_id: financialRecordId(subjectId, idempotencyKey), subject_id: subjectId,
    scope, kind, direction, amount_minor: amountMinor, currency,
    occurred_at: iso(occurredAt, "Financial Manager source occurrence"),
    recorded_at: iso(recordedAt, "Financial Manager source observation"),
    idempotency_key: idempotencyKey,
    source: { provider, source_type: sourceType, external_ref: externalRef },
    verification: verification === "verified"
      ? {
        status: "verified", observed_at: iso(recordedAt, "Financial Manager source observation"),
        evidence_refs: [`financial-source://${provider}/${createHash("sha256").update(String(externalRef)).digest("hex")}`],
      }
      : { status: "unverified", observed_at: iso(recordedAt, "Financial Manager source observation"), evidence_refs: [] },
  };
}

function stableRecordedAt(row, occurrence, label) {
  if (row && row.recorded_at != null) {
    try { return iso(row.recorded_at, label); } catch { /* use immutable source occurrence */ }
  }
  return iso(occurrence, label);
}

function ledgerRecord(subjectId, row, observedAt) {
  const key = String(row && row.entry_key || "").trim();
  const mapping = {
    financial_external_income: ["business_revenue", "credit"],
    financial_realized_loss: ["business_cost", "debit"],
    financial_fee: ["fee", "debit"],
    financial_user_transfer: ["payout", "credit"],
    financial_self_funding: ["transfer", "credit"],
    financial_deposit: ["transfer", "credit"],
    financial_internal_move: ["transfer", "credit"],
    financial_unverified: ["business_revenue", "credit", "unverified"],
  }[row && row.kind];
  if (!key || !mapping) throw new Error("Financial Manager wallet ledger row is unsupported");
  const hasMinor = row && row.amount_minor != null;
  const hasAtomic = row && (row.amount_atomic != null || row.amount_decimals != null);
  if (hasMinor === hasAtomic) throw new Error("Financial Manager wallet ledger amount is invalid");
  const currency = String(row && row.currency || "");
  if (!/^[A-Z]{3}$/.test(currency)) throw new Error("Financial Manager wallet ledger currency is invalid");
  let amountMinor;
  let projectedCurrency;
  if (hasMinor) {
    const raw = String(row.amount_minor);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
      throw new Error("Financial Manager wallet ledger minor amount is invalid");
    }
    amountMinor = Number(raw);
    projectedCurrency = currency;
  } else {
    if (currency !== "USD" || !/^\d+$/.test(String(row.amount_atomic))) {
      throw new Error("Financial Manager wallet ledger atomic amount is invalid");
    }
    const decimals = Number(row.amount_decimals);
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 6) {
      throw new Error("Financial Manager wallet ledger atomic decimals are invalid");
    }
    const normalizedUsdcAtomic = BigInt(row.amount_atomic) * (10n ** BigInt(6 - decimals));
    if (normalizedUsdcAtomic > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new Error("Financial Manager wallet ledger atomic amount exceeds FinancialRecord range");
    }
    amountMinor = Number(normalizedUsdcAtomic);
    projectedCurrency = "USDC";
  }
  const provider = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(String(row.source || ""))
    ? String(row.source) : "wallet-ledger";
  const occurredAt = iso(row.occurred_at, "Financial Manager wallet ledger occurrence");
  return record({
    subjectId, key: `wallet-ledger:${key}`, scope: "business", kind: mapping[0], direction: mapping[1],
    amountMinor, currency: projectedCurrency, occurredAt,
    recordedAt: stableRecordedAt(row, occurredAt, "Financial Manager wallet ledger record time"),
    provider, sourceType: "wallet", externalRef: key,
    verification: mapping[2] || "verified",
  });
}

// API usage is an estimate. It is kept unverified until a provider receipt is
// available, so it cannot make a cloud Financial Manager report claim a cost.
function apiCostRecord(subjectId, row, observedAt) {
  const amount = String(row && row.est_usd != null ? row.est_usd : "").trim();
  const key = String(row && row.id != null ? row.id : "").trim();
  let micros;
  try {
    micros = usdMicrosFromDecimal(amount);
  } catch {
    throw new Error("Financial Manager API cost amount is invalid");
  }
  if (!key) throw new Error("Financial Manager API cost id is required");
  const minorBig = (micros + 9_999n) / 10_000n;
  if (minorBig > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("Financial Manager API cost amount is invalid");
  const minor = Number(minorBig);
  const occurredAt = iso(row.ts, "Financial Manager API cost occurrence");
  return record({
    subjectId, key: `api-cost:${key}`, scope: "business", kind: "business_cost", direction: "debit",
    amountMinor: minor, currency: "USD", occurredAt,
    recordedAt: stableRecordedAt(row, occurredAt, "Financial Manager API cost record time"),
    provider: "api-cost", sourceType: "manual", externalRef: key, verification: "unverified",
  });
}

function baseBalanceRecord(subjectId, walletAddress, balanceAtomic, observedAt) {
  const amount = Number(balanceAtomic);
  if (!/^\d+$/.test(String(balanceAtomic)) || !Number.isSafeInteger(amount)) {
    throw new Error("Financial Manager Base USDC balance is invalid");
  }
  return record({
    subjectId, key: `base-usdc:${walletAddress}:${amount}:${observedAt}`, scope: "personal",
    kind: "asset_balance", direction: "snapshot", amountMinor: amount, currency: "USDC",
    occurredAt: observedAt, recordedAt: observedAt, provider: "base-usdc", sourceType: "wallet",
    externalRef: String(walletAddress),
  });
}

async function appendCloudFinancialRecords({ store, subjectId, walletAddress, ledgerRows, costRows, balanceAtomic, observedAt }) {
  const expectedWallet = String(walletAddress || "").toLowerCase();
  if ((Array.isArray(ledgerRows) ? ledgerRows : []).some((row) => (
    String(row && row.wallet_address || "").toLowerCase() !== expectedWallet
  ))) {
    throw new Error("Financial Manager wallet ledger tenant scope mismatch");
  }
  const rows = [
    ...(Array.isArray(ledgerRows) ? ledgerRows.map((row) => ledgerRecord(subjectId, row, observedAt)) : []),
    ...(Array.isArray(costRows) ? costRows.map((row) => apiCostRecord(subjectId, row, observedAt)) : []),
    baseBalanceRecord(subjectId, walletAddress, balanceAtomic, observedAt),
  ].filter(Boolean);
  let created = 0;
  for (const item of rows) {
    if ((await store.append(item)).created) created += 1;
  }
  return { observed: rows.length, created, sources: { wallet: "observed_verified", apiCosts: "observed_unverified" } };
}

async function runCloudFinancialManagerReport(request = {}, deps = {}) {
  const uid = requiredText(request.uid, "financial report uid");
  const kind = String(request.kind || "");
  const nowMs = Number(request.nowMs);
  if (!REPORT_KINDS.has(kind) || !Number.isFinite(nowMs)) throw new Error("financial report request is invalid");
  const readTenant = deps.readTenant || ((tenantId) => (
    legacyFinancialRuntime().readFinancialTenant(tenantId, deps)
  ));
  const tenant = await readTenant(uid);
  if (!tenant || String(tenant.uid || "") !== uid) throw new Error("financial report tenant scope mismatch");
  if (tenant.notifications_enabled === false) return { status: "skipped", reason: "notifications_disabled", report_kind: kind };
  if (!String(tenant.telegram_chat_id || "")) return { status: "skipped", reason: "telegram_unbound", report_kind: kind };
  if (!String(tenant.agent_wallet_address || "")) return { status: "skipped", reason: "agent_wallet_unbound", report_kind: kind };

  const timezone = String(tenant.call_time_zone || "Asia/Tokyo");
  if (!request.force && !dueConsolidatedReport(kind, nowMs, timezone)) {
    return { status: "skipped", reason: "not_due", report_kind: kind };
  }
  const reportingDate = reportingDateForZone(nowMs, timezone);
  const bounds = reportBounds(kind, nowMs, timezone);
  const periodKey = bounds.period_key;
  const identity = { uid, report_kind: kind, period_key: periodKey };
  const readReceipt = deps.readReceipt || ((input) => (
    legacyFinancialRuntime().readFinancialReceipt(input, deps)
  ));
  const existing = await readReceipt(identity);
  if (existing && existing.status === "sent") {
    if (!completeSentReceipt(existing)) {
      return { status: "failed", reason: "financial_delivery_receipt_incomplete", report_kind: kind, unknownEffect: true };
    }
    return {
      status: "duplicate", report_kind: kind, period_key: periodKey,
      telegram_message_id: Number(existing.telegram_message_id), snapshot_hash: String(existing.snapshot_hash),
      chat_id_hash: hashChatId(tenant.telegram_chat_id), sent_at: String(existing.sent_at),
      source_freshness: existing.source_freshness || {
        report_cutoff_at: String(existing.period_end), earnings_latest_at: null,
        costs_latest_at: null, balance_observed_at: null,
      },
    };
  }
  const store = deps.financialStore || createPostgresFinancialRecordStore({ query: deps.query });
  const readLedger = deps.readLedger || ((wallet) => walletLedgerRuntime().readWalletLedger(wallet, deps));
  const readCosts = deps.readCosts || ((tenantId, range) => (
    legacyFinancialRuntime().readCostLedger(tenantId, { ...deps, ...range })
  ));
  if (typeof deps.readBalance !== "function") throw new Error("Financial Manager Base balance reader is required");
  const observedAt = new Date(nowMs).toISOString();
  const [ledgerRows, costRows, balanceAtomic] = await Promise.all([
    readLedger(tenant.agent_wallet_address),
    readCosts(uid, { since: bounds.period_start }),
    deps.readBalance(tenant.agent_wallet_address),
  ]);
  let claimedDigest = null;
  const result = await runFinancialManager({
    subjectId: uid, reportingDate, timezone, now: observedAt, store,
    ingest: () => appendCloudFinancialRecords({
      store, subjectId: uid, walletAddress: tenant.agent_wallet_address,
      ledgerRows, costRows, balanceAtomic, observedAt,
    }),
    deliveryStore: {
      claim: async ({ digest, report }) => {
        claimedDigest = digest;
        const claimReceipt = deps.claimReceipt || ((value) => (
          legacyFinancialRuntime().claimFinancialReceipt(value, deps)
        ));
        return claimReceipt({
          ...identity, timezone, period_start: bounds.period_start,
          period_end: bounds.period_end, snapshot: report, snapshot_hash: digest, status: "pending",
        });
      },
      readAfterClaim: () => readReceipt(identity),
      verifyDelivered: completeSentReceipt,
      markDelivered: async ({ delivery }) => {
        const mark = deps.markReceiptSent || ((value, messageId) => (
          legacyFinancialRuntime().markFinancialReceiptSent(value, messageId, { ...deps, nowMs })
        ));
        return mark({ ...identity, snapshot_hash: claimedDigest }, Number(delivery.providerMessageId));
      },
      markFailed: async () => {
        const mark = deps.markReceiptFailed || ((value, code) => (
          legacyFinancialRuntime().markFinancialReceiptFailed(value, code, { ...deps, nowMs })
        ));
        return mark({ ...identity, snapshot_hash: claimedDigest }, "telegram_rejected");
      },
    },
    notify: async ({ message }) => {
      const send = deps.sendTelegram || sendMessage;
      const delivered = await send(deps.telegramToken || process.env.LM_TELEGRAM_BOT_TOKEN, String(tenant.telegram_chat_id), message);
      return {
        delivered: Boolean(delivered && delivered.ok === true),
        providerMessageId: delivered && delivered.result && delivered.result.message_id,
      };
    },
  });
  if (result.status === "quiet") {
    if (result.reason === "unchanged" && completeSentReceipt(result.duplicate)) {
      return {
        status: "duplicate", report_kind: kind, period_key: periodKey,
        telegram_message_id: Number(result.duplicate.telegram_message_id),
        snapshot_hash: String(result.duplicate.snapshot_hash),
        chat_id_hash: hashChatId(tenant.telegram_chat_id), sent_at: String(result.duplicate.sent_at),
        source_freshness: result.duplicate.source_freshness || {
          report_cutoff_at: String(result.duplicate.period_end), earnings_latest_at: null,
          costs_latest_at: null, balance_observed_at: null,
        },
      };
    }
    return result.reason === "unchanged"
      ? { status: "failed", reason: "financial_delivery_claim_unresolved", report_kind: kind, unknownEffect: true }
      : { status: "skipped", reason: result.reason, report_kind: kind, period_key: periodKey };
  }
  if (result.status === "failed") return { ...result, report_kind: kind, period_key: periodKey };
  return {
    status: "sent", report_kind: kind, period_key: periodKey,
    telegram_message_id: Number(result.providerMessageId), snapshot: result.report,
    snapshot_hash: result.digest,
    source_freshness: {
      report_cutoff_at: observedAt, earnings_latest_at: latest(ledgerRows, "occurred_at", observedAt),
      costs_latest_at: latest(costRows, "ts", observedAt), balance_observed_at: observedAt,
    },
  };
}

function requiredText(value, label) {
  const text = String(value == null ? "" : value).trim();
  if (!text) throw new Error(`${label} is required`);
  return text;
}

function financialReportRef({ tenantId, kind, nowMs, force = false }) {
  const tenant = requiredText(tenantId, "financial report tenant");
  if (!REPORT_KINDS.has(kind)) throw new Error("financial report kind is invalid");
  const instant = Number(nowMs);
  if (!Number.isFinite(instant)) throw new Error("financial report instant is invalid");
  const ref = `financial-report://${encodeURIComponent(tenant)}/${kind}/${encodeURIComponent(
    new Date(instant).toISOString(),
  )}`;
  return force ? `${ref}?force=true` : ref;
}

function parseFinancialReportRef(ref) {
  let parsed;
  try {
    parsed = new URL(requiredText(ref, "financial report ref"));
  } catch {
    throw new Error("financial report ref is invalid");
  }
  const tenantId = decodeURIComponent(parsed.hostname);
  const segments = parsed.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (
    parsed.protocol !== "financial-report:"
    || segments.length !== 2
    || !REPORT_KINDS.has(segments[0])
  ) {
    throw new Error("financial report ref is invalid");
  }
  const nowMs = Date.parse(segments[1]);
  if (!Number.isFinite(nowMs)) throw new Error("financial report ref is invalid");
  return {
    tenantId,
    kind: segments[0],
    nowMs,
    force: parsed.searchParams.get("force") === "true",
  };
}

function buildFinancialReportJob(input = {}) {
  const tenantId = requiredText(input.tenantId, "financial report tenant");
  const telegramTokenRef = requiredText(input.telegramTokenRef, "Telegram secret reference");
  if (!SECRET_REF.test(telegramTokenRef)) {
    throw new Error("Telegram token must be a secret reference");
  }
  const reportRef = financialReportRef(input);
  const digest = createHash("sha256")
    .update(`${tenantId}\n${reportRef}`, "utf8")
    .digest("hex");
  return buildRuntimeJob({
    jobId: `financial-report:${digest}`,
    tenantId,
    loopId: LOOP_ID,
    capability: CAPABILITY,
    effectClass: "message",
    effectKey: `telegram:financial:${digest}`,
    inputRefs: {
      financial_report_ref: reportRef,
      telegram_token_ref: telegramTokenRef,
    },
    maxAttempts: 3,
  });
}

function sentAt(providerResult, fallbackMs) {
  const providerSeconds = Number(providerResult && providerResult.result && providerResult.result.date);
  if (Number.isSafeInteger(providerSeconds) && providerSeconds > 0) {
    return new Date(providerSeconds * 1000).toISOString();
  }
  return new Date(fallbackMs).toISOString();
}

async function executeFinancialReportJob(job, deps = {}) {
  if (!job || job.capability !== CAPABILITY || job.effect_class !== "message") {
    throw new Error("financial report job contract mismatch");
  }
  const inputRefs = job.input_refs || {};
  const request = parseFinancialReportRef(inputRefs.financial_report_ref);
  if (request.tenantId !== job.tenant_id) {
    throw new Error("financial report tenant scope mismatch");
  }
  if (!deps.secretProvider || typeof deps.secretProvider.get !== "function") {
    throw new Error("financial report secret provider is required");
  }
  const telegramToken = await deps.secretProvider.get(
    job.tenant_id,
    inputRefs.telegram_token_ref,
  );
  const execute = deps.runReport || runCloudFinancialManagerReport;
  const providerSend = deps.sendTelegram || sendMessage;
  let providerResult;
  let telegramChatId;
  let telegramDispatchStarted = false;
  let result;
  try {
    result = await execute({
      uid: job.tenant_id,
      kind: request.kind,
      nowMs: request.nowMs,
      force: request.force,
    }, {
      ...deps,
      telegramToken,
      sendTelegram: async (token, chatId, body, extra) => {
        telegramChatId = String(chatId);
        telegramDispatchStarted = true;
        providerResult = await providerSend(token, chatId, body, extra);
        return providerResult;
      },
    });
  } catch (error) {
    if (telegramDispatchStarted && error && typeof error === "object") {
      error.unknownEffect = true;
    }
    throw error;
  }
  if (result.status === "failed") {
    const error = new Error(`Financial Manager report failed: ${result.reason || "unknown"}`);
    // A Telegram call with no verifiable provider id is an uncertain effect:
    // retrying it blindly can duplicate the user-facing report.
    error.unknownEffect = telegramDispatchStarted || result.unknownEffect === true;
    throw error;
  }
  if (result.status !== "sent") {
    const reconciled = result.status === "duplicate"
      && Number.isInteger(result.telegram_message_id)
      && /^[0-9a-f]{64}$/.test(String(result.snapshot_hash || ""))
      && /^[0-9a-f]{64}$/.test(String(result.chat_id_hash || ""));
    return {
      receipt: {
        schema_version: 1,
        kind: "telegram_financial_report",
        status: result.status,
        report_kind: result.report_kind,
        period_key: result.period_key || null,
        ...(reconciled
          ? {
            chat_id_hash: result.chat_id_hash,
            message_id: result.telegram_message_id,
            snapshot_hash: result.snapshot_hash,
            sent_at: result.sent_at,
            source_freshness: result.source_freshness,
          }
          : {}),
      },
      result,
    };
  }
  if (!telegramChatId || !Number.isInteger(result.telegram_message_id)) {
    throw new Error("financial report provider receipt is incomplete");
  }
  return {
    receipt: {
      schema_version: 1,
      kind: "telegram_financial_report",
      status: "sent",
      chat_id_hash: hashChatId(telegramChatId),
      message_id: result.telegram_message_id,
      snapshot_hash: result.snapshot_hash,
      sent_at: sentAt(providerResult, request.nowMs),
      source_freshness: result.source_freshness,
    },
    result,
  };
}

function validIso(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function validFreshness(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  for (const key of [
    "report_cutoff_at",
    "earnings_latest_at",
    "costs_latest_at",
    "balance_observed_at",
  ]) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) return false;
    if (value[key] !== null && !validIso(value[key])) return false;
  }
  return value.report_cutoff_at !== null;
}

function verifyFinancialReportReceipt(receipt) {
  if (
    !receipt
    || typeof receipt !== "object"
    || Array.isArray(receipt)
    || receipt.schema_version !== 1
    || receipt.kind !== "telegram_financial_report"
  ) {
    return false;
  }
  if (["sent", "duplicate"].includes(receipt.status)) {
    return Number.isInteger(receipt.message_id)
      && receipt.message_id > 0
      && HASH.test(String(receipt.chat_id_hash || ""))
      && HASH.test(String(receipt.snapshot_hash || ""))
      && validIso(receipt.sent_at)
      && validFreshness(receipt.source_freshness);
  }
  return receipt.status === "skipped"
    && REPORT_KINDS.has(receipt.report_kind);
}

function safeFinancialReportSummary(receipt) {
  if (!verifyFinancialReportReceipt(receipt)) {
    throw new Error("financial report receipt verification failed");
  }
  if (["sent", "duplicate"].includes(receipt.status)) {
    return {
      status: receipt.status,
      message_id: receipt.message_id,
      snapshot_hash: receipt.snapshot_hash,
      sent_at: receipt.sent_at,
      source_freshness: receipt.source_freshness,
    };
  }
  return {
    status: receipt.status,
    report_kind: receipt.report_kind,
    period_key: receipt.period_key || null,
  };
}

function createFinancialReportLoopAdapter(deps = {}) {
  return Object.freeze({
    async plan(context = {}) {
      const kinds = context.kind == null ? ["daily"] : [context.kind];
      return kinds.map((kind) => buildFinancialReportJob({
        ...context,
        kind,
      }));
    },
    execute(job, services = {}) {
      return executeFinancialReportJob(job, { ...deps, ...services });
    },
    async reconcile(effect) {
      if (typeof deps.inspectEffect !== "function") return { state: "unknown" };
      const proof = await deps.inspectEffect(effect);
      if (!proof || !["present", "absent", "unknown"].includes(proof.state)) {
        throw new Error("financial report reconciliation proof invalid");
      }
      if (proof.state === "unknown") return { state: "unknown" };
      if (
        !proof.receipt
        || typeof proof.receipt !== "object"
        || Array.isArray(proof.receipt)
      ) {
        throw new Error("financial report reconciliation receipt invalid");
      }
      if (proof.state === "present" && !verifyFinancialReportReceipt(proof.receipt)) {
        throw new Error("financial report reconciliation receipt verification failed");
      }
      return proof;
    },
    verify: verifyFinancialReportReceipt,
    report: safeFinancialReportSummary,
  });
}

function parseEnqueueArgs(argv) {
  const args = Array.isArray(argv) ? argv : [];
  if (args[0] !== "enqueue") throw new Error("usage: report-job-adapter.js enqueue --uid <tenant>");
  const uidIndex = args.indexOf("--uid");
  const tenantId = uidIndex >= 0 ? String(args[uidIndex + 1] || "").trim() : "";
  if (!tenantId || tenantId.startsWith("--")) throw new Error("--uid <tenant> is required");
  const forceIndex = args.indexOf("--force");
  let force = null;
  if (forceIndex >= 0) {
    force = String(args[forceIndex + 1] || "");
    if (!["daily", "weekly", "all"].includes(force)) {
      throw new Error("--force must be daily, weekly, or all");
    }
  }
  return { tenantId, force };
}

async function enqueueFinancialReportJobs(argv, env = process.env, deps = {}) {
  const { tenantId, force } = parseEnqueueArgs(argv);
  const nowMs = deps.nowMs == null ? Date.now() : Number(deps.nowMs);
  const telegramTokenRef = String(
    env.LM_TELEGRAM_TOKEN_REF || "secret://telegram/bot-token",
  );
  const enqueue = deps.enqueueJob || enqueueJob;
  const kind = force === "weekly" ? "weekly" : "daily";
  const job = buildFinancialReportJob({
    tenantId,
    kind,
    nowMs,
    // `all` now means one forced consolidated report, not two identical sends.
    force: force !== null,
    telegramTokenRef,
  });
  const queued = await enqueue({
    jobId: job.job_id,
    tenantId: job.tenant_id,
    loopId: job.loop_id,
    capability: job.capability,
    effectClass: job.effect_class,
    effectKey: job.effect_key,
    inputRefs: job.input_refs,
    maxAttempts: job.max_attempts,
  });
  const results = [{
    created: queued.created === true,
    job_id: job.job_id,
    report_kind: kind,
  }];
  (deps.stdout || process.stdout).write(`${JSON.stringify(results)}\n`);
  return results;
}

if (require.main === module) {
  enqueueFinancialReportJobs(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`[financial-report-enqueue] ${error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  CAPABILITY,
  LOOP_ID,
  financialReportRef,
  parseFinancialReportRef,
  buildFinancialReportJob,
  executeFinancialReportJob,
  runCloudFinancialManagerReport,
  appendCloudFinancialRecords,
  verifyFinancialReportReceipt,
  safeFinancialReportSummary,
  createFinancialReportLoopAdapter,
  parseEnqueueArgs,
  enqueueFinancialReportJobs,
};
