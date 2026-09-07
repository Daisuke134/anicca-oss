"use strict";

const os = require("node:os");
const path = require("node:path");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const { Resolver } = require("node:dns").promises;
const { readMoneytreeViaCodex, readMoneytreeBundleViaCodex } = require("./cfo-moneytree-codex-read.js");
const { recoverMoneytreeRead } = require("../lib/cfo-moneytree-recovery.js");
const { resolveCfoDailyRun } = require("../lib/cfo-daily-run.js");
const { buildCfoDailyReportFromRecovery } = require("../lib/cfo-recovery-snapshot.js");
const { renderCfoTelegram } = require("../lib/cfo-telegram.js");
const { deliverCfoTelegram } = require("../lib/cfo-telegram-send.js");
const { createCfoSupabaseRpc } = require("../lib/cfo-supabase-rpc.js");
const { runLocalAgentUsageCollection } = require("../lib/cfo-local-agent-usage-runner.js");
const { makeGogMail } = require("../lib/transport/mail-gog.js");
const { captureLatestGoogleCloudInvoice } = require("../lib/cfo-google-invoice-local-source.js");
const { captureLatestAnthropicSubscriptionReceipt } = require("../lib/cfo-anthropic-receipt-local-source.js");
const { requestMoneytreeRefresh, hasMoneytreeLinkCredentials } = require("../lib/cfo-moneytree-refresh.js");
const { observeCfoBusiness } = require("../lib/cfo-business-observer.js");
const { decideSpendingGuardian } = require("../lib/cfo-spending-guardian.js");
const { decideCfoRecommendation } = require("../lib/cfo-recommendation.js");

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const rpc = createCfoSupabaseRpc("cfo_hourly_local_failed:");
const AI_COST_KEYS = ["provider", "plan", "amount", "currency", "billingPeriodStart", "billingPeriodEnd", "evidenceStatus", "unavailableProviders"];
const RECEIPT_KEYS = ["schema_version", "provider", "plan", "billing_period_start", "billing_period_end", "subtotal", "tax", "total", "currency", "paid_date", "source_hash", "evidence_status"];

const DNS_FAILURES = new Set(["ENOTFOUND", "EAI_AGAIN", "EAI_FAIL", "EAI_NODATA"]);
let cfoDnsFetch = null;
let cfoDnsEnv = process.env;

function dnsFailure(error) { for (let current = error; current; current = current.cause) if (DNS_FAILURES.has(current.code) || (typeof current.message === "string" && [...DNS_FAILURES].some(code => current.message.includes(code)))) return true; return false; }
function requestHeaders(value) {
  const result = {};
  if (!value) return result;
  if (typeof value.forEach === "function") value.forEach((item, key) => { result[key] = item; });
  else if (Array.isArray(value)) value.forEach(([key, item]) => { result[key] = item; });
  else Object.entries(value).forEach(([key, item]) => { result[key] = item; });
  return result;
}
function requestBody(value) {
  if (value == null) return null;
  if (typeof value === "string" || Buffer.isBuffer(value) || value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return Buffer.from(value);
  throw new Error("cfo_dns_body");
}
function resolvedRequest(target, address, init = {}) {
  const method = String(init.method || "GET").toUpperCase(), headers = requestHeaders(init.headers), body = requestBody(init.body);
  if (!Object.keys(headers).some(key => key.toLowerCase() === "host")) headers.host = target.host;
  const transport = target.protocol === "https:" ? https : target.protocol === "http:" ? http : null;
  if (!transport) throw new Error("cfo_dns_protocol");
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error, response) => { if (settled) return; settled = true; error ? reject(error) : resolve(response); };
    let request;
    try {
      request = transport.request({ protocol: target.protocol, hostname: address, port: target.port || undefined, path: `${target.pathname}${target.search}`, method, headers, ...(target.protocol === "https:" ? { servername: target.hostname } : {}) }, response => {
        const chunks = [];
        response.on("data", chunk => chunks.push(Buffer.from(chunk)));
        response.once("error", error => finish(error));
        response.once("end", () => {
          const text = Buffer.concat(chunks).toString("utf8"), status = Number(response.statusCode) || 0;
          finish(null, { ok: status >= 200 && status < 300, status, text: async () => text, json: async () => JSON.parse(text) });
        });
      });
      request.once("error", error => finish(error));
      request.end(body == null ? undefined : body);
    } catch (error) { finish(error); }
  });
}
async function resolvedFetch(input, init, env) {
  const target = new URL(typeof input === "string" || input instanceof URL ? input : input && input.url);
  const resolver = new Resolver(), configured = String(env && env.LM_CFO_DNS_SERVERS || "").split(",").map(value => value.trim()).filter(Boolean);
  resolver.setServers(configured.length ? configured : ["1.1.1.1", "8.8.8.8"]);
  const addresses = await resolver.resolve4(target.hostname);
  if (!addresses.length) throw new Error("cfo_dns_request");
  return resolvedRequest(target, addresses[0], init);
}
function makeDnsFetch(nativeFetch) {
  return async (input, init) => {
    try { return await nativeFetch(input, init); } catch (error) {
      if (!dnsFailure(error)) throw error;
      return resolvedFetch(input, init, cfoDnsEnv);
    }
  };
}
function installDnsFetch(env) {
  cfoDnsEnv = env || process.env;
  if (typeof globalThis.fetch !== "function") return globalThis.fetch;
  if (globalThis.fetch === cfoDnsFetch) return cfoDnsFetch;
  cfoDnsFetch = makeDnsFetch(globalThis.fetch);
  globalThis.fetch = cfoDnsFetch;
  return cfoDnsFetch;
}

function pick(options, name, fallback) { return typeof options[name] === "function" ? options[name] : fallback; }
function ownerDate(value) { const parts = new Intl.DateTimeFormat("en", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(value); const get = (type) => parts.find((part) => part.type === type).value; return `${get("year")}-${get("month")}-${get("day")}`; }
function ownerHour(value) { const parts = new Intl.DateTimeFormat("en", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23" }).formatToParts(value); const get = (type) => parts.find((part) => part.type === type).value; return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}`; }
function refreshQuotaPath(env) { return path.join(env.CFO_STATE_DIR || path.join(os.homedir(), ".local", "state", "life-manager", "cfo-hourly"), "moneytree-refresh-quota.json"); }
function readRefreshQuota(env, date) {
  const file = refreshQuotaPath(env);
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!value || value.date !== date) return { file, count: 0 };
    if (!Number.isSafeInteger(value.acceptedCount) || value.acceptedCount < 0 || value.acceptedCount > 4) return { file, count: null };
    return { file, count: value.acceptedCount };
  } catch (error) { return error && error.code === "ENOENT" ? { file, count: 0 } : { file, count: null }; }
}
function recordRefreshQuota(quota, date) {
  const directory = path.dirname(quota.file), temporary = `${quota.file}.${process.pid}.tmp`;
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.writeFileSync(temporary, `${JSON.stringify({ date, acceptedCount: quota.count + 1 })}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  try { fs.renameSync(temporary, quota.file); } catch (error) { try { fs.unlinkSync(temporary); } catch {} throw error; }
}
async function maybeRefreshMoneytree(env, fetchImpl, clock, options = {}) {
  const configured = String(env.MONEYTREE_LINK_REFRESH_ENABLED || "").toLowerCase();
  const credentialFile = env.MONEYTREE_LINK_CREDENTIALS_PATH;
  if (configured === "false") return { status: "disabled", reason: "refresh_disabled" };
  if (configured !== "true" && !hasMoneytreeLinkCredentials(credentialFile)) return { status: "not_enabled", reason: "refresh_opt_in_required" };
  const date = ownerDate(clock), quota = readRefreshQuota(env, date);
  if (quota.count === null) return { status: "unavailable", reason: "refresh_quota_unknown" };
  // MUFG's official policy is at most once per day for paid personal accounts;
  // the local guard is stricter than LINK's generic four-call guest quota.
  if (quota.count >= 1) return { status: "not_requested", reason: "provider_daily_policy_guard" };
  const result = await (options.requestMoneytreeRefresh || requestMoneytreeRefresh)({
    accessToken: env.MONEYTREE_LINK_ACCESS_TOKEN,
    baseUrl: env.MONEYTREE_LINK_BASE_URL,
    credentialsPath: credentialFile,
    dailyRequestCount: quota.count,
    observedAt: clock.toISOString(),
    fetchImpl,
  });
  if (result.status === "accepted") {
    try { recordRefreshQuota(quota, date); } catch { return { status: "accepted", reason: "provider_refresh_queued_quota_persist_failed", httpStatus: 202 }; }
  }
  return result;
}
function config(options, env, fallbackFetch) {
  const value = { uid: options.uid || options.ownerUid || env.LM_CFO_UID || env.LM_UID || env.LM_OWNER_UID, chatId: options.chatId || options.telegramChatId || env.TELEGRAM_ALERT_CHAT_ID || env.LM_CFO_TELEGRAM_CHAT_ID || env.LM_ADMIN_TELEGRAM_CHAT_ID, telegramToken: options.telegramToken || env.TELEGRAM_BOT_TOKEN || env.LM_TELEGRAM_BOT_TOKEN, supaUrl: options.supaUrl || env.SUPABASE_URL, supaKey: options.supaKey || env.SUPABASE_SERVICE_ROLE_KEY, uidSecret: env.LM_UID_SECRET, fetchImpl: options.fetchImpl || fallbackFetch || globalThis.fetch };
  if (Object.values(value).some((item) => typeof item !== "string" && item !== value.fetchImpl) || typeof value.fetchImpl !== "function") throw new Error("config");
  if (!value.uid || !value.chatId || !value.telegramToken || !value.supaUrl || !value.supaKey || typeof value.uidSecret !== "string" || value.uidSecret.length < 32) throw new Error("config");
  return value;
}
function providerDataState(reportingDate, transactions) {
  const latest = transactions && typeof transactions.latestBookingDate === "string" ? transactions.latestBookingDate : null;
  if (!latest || !/^\d{4}-\d{2}-\d{2}$/.test(reportingDate)) return { providerDataFreshness: "unknown", latestReturnedTransactionDate: latest };
  return { providerDataFreshness: latest < reportingDate ? "stale" : "unknown", latestReturnedTransactionDate: latest };
}
function summary(status, reportingDate, revision, appended = false, delivered = false, recovered = false, providerData = {}) { return { status, reportingDate, revision, appended, delivered, recovered, ...providerData }; }
function ordered(value) { return Array.isArray(value) ? value.map(ordered) : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])])) : value; }
function facts(report) { return JSON.stringify(ordered({ state: report.state, action: report.action && { kind: report.action.kind }, currency: report.currency, totals: report.totals, sources: report.sources.map(({ sourceId, status, amountMinor, verificationStatus }) => ({ sourceId, status, amountMinor, verificationStatus })), excluded: report.excluded, aiCost: report.aiCost || null, business: report.business || null })); }
function sameFacts(left, right) { return facts(left) === facts(right); }
function validateRow(row, date, render = renderCfoTelegram) {
  const keys = row && typeof row === "object" && !Array.isArray(row) ? Object.keys(row) : [];
  if (keys.length !== 6 || keys.some((key) => !["public_ref", "reporting_date", "run_id", "revision", "created_at", "report_payload"].includes(key)) || !UUID.test(row.public_ref) || !UUID.test(row.run_id) || row.reporting_date !== date || !Number.isSafeInteger(row.revision) || row.revision < 1 || typeof row.created_at !== "string" || !rpc.timestamp(row.created_at) || !row.report_payload || typeof row.report_payload !== "object") throw new Error("snapshot");
  if (row.report_payload.reportingDate !== date || row.report_payload.revision !== row.revision) throw new Error("snapshot");
  render({ locale: "ja", view: "summary", snapshot: row.report_payload });
  return row;
}
async function latestSnapshot(value, render = renderCfoTelegram) {
  const endpoint = `${value.supaUrl.replace(/\/+$/, "")}/rest/v1/lm_cfo_daily_snapshots?uid=eq.${encodeURIComponent(value.uid)}&reporting_date=eq.${value.reportingDate}&select=public_ref,reporting_date,run_id,revision,created_at,report_payload&order=revision.desc&limit=1`;
  const response = await value.fetchImpl(endpoint, { method: "GET", headers: { apikey: value.supaKey, Authorization: `Bearer ${value.supaKey}` } });
  if (!response || response.ok !== true || typeof response.json !== "function") throw new Error("snapshot");
  const rows = await response.json();
  if (!Array.isArray(rows) || rows.length > 1) throw new Error("snapshot");
  return rows.length ? validateRow(rows[0], value.reportingDate, render) : null;
}
async function latestAiCostSnapshot(value, render = renderCfoTelegram) {
  const endpoint = `${value.supaUrl.replace(/\/+$/, "")}/rest/v1/lm_cfo_daily_snapshots?uid=eq.${encodeURIComponent(value.uid)}&reporting_date=lt.${encodeURIComponent(value.reportingDate)}&report_payload->aiCost=not.is.null&select=public_ref,reporting_date,run_id,revision,created_at,report_payload&order=created_at.desc&limit=1`;
  const response = await value.fetchImpl(endpoint, { method: "GET", headers: { apikey: value.supaKey, Authorization: `Bearer ${value.supaKey}` } });
  if (!response || response.ok !== true || typeof response.json !== "function") throw new Error("snapshot");
  const rows = await response.json(); if (!Array.isArray(rows) || rows.length > 1) throw new Error("snapshot");
  if (!rows.length) return null; const row = rows[0]; return validateRow(row, row.reporting_date, render);
}
async function appendRevision(input, value) {
  const options = { supaUrl: value.supaUrl, supaKey: value.supaKey, fetchImpl: value.fetchImpl };
  const parsed = await rpc.postRpc(rpc.validateOptions(options), "lm_append_cfo_daily_snapshot_revision", { p_uid: input.uid, p_reporting_date: input.reportingDate, p_run_id: input.runId, p_revision: input.revision, p_supersedes_revision: input.supersedesRevision, p_report_payload: input.report, p_source_bundle: input.sourceBundle });
  const keys = parsed && typeof parsed === "object" ? Object.keys(parsed) : [];
  if (keys.length !== 6 || keys.some((key) => !["public_ref", "reporting_date", "run_id", "revision", "supersedes_revision", "created_at"].includes(key)) || !UUID.test(parsed.public_ref) || parsed.reporting_date !== input.reportingDate || parsed.run_id !== input.runId || parsed.revision !== input.revision || parsed.supersedes_revision !== input.supersedesRevision || typeof parsed.created_at !== "string" || !rpc.timestamp(parsed.created_at)) throw new Error("receipt");
  return parsed;
}
async function runHourlyCfo(options = {}) {
  let reportingDate = null;
  try {
    const env = options.env || process.env, clock = new Date(typeof options.now === "function" ? options.now() : (options.now || new Date()));
    if (!Number.isFinite(clock.getTime())) throw new Error("clock");
    reportingDate = ownerDate(clock);
    const value = config(options, env, installDnsFetch(env)), rpcOptions = { supaUrl: value.supaUrl, supaKey: value.supaKey, fetchImpl: value.fetchImpl };
    let moneytreeRefresh;
    try { moneytreeRefresh = await maybeRefreshMoneytree(env, value.fetchImpl, clock, options); } catch { moneytreeRefresh = { status: "failed", reason: "refresh_boundary_failed" }; }
    const reader = pick(options, "readMoneytreeViaCodex", readMoneytreeViaCodex), customBundleReader = typeof options.readMoneytreeBundleViaCodex === "function" ? options.readMoneytreeBundleViaCodex : null;
    const legacyReaderRequested = typeof options.readMoneytreeViaCodex === "function" || typeof options.callAppServer === "function";
    const bundleReader = customBundleReader || (!legacyReaderRequested ? readMoneytreeBundleViaCodex : null);
    let transactionView;
    const recovery = await (pick(options, "recoverMoneytreeRead", recoverMoneytreeRead))({ reportingDate, observedAt: clock.toISOString() }, { read: async () => {
      try {
        if (bundleReader) {
          const bundle = await bundleReader({ env, now: () => clock });
          if (!bundle || !bundle.moneytreeRead) throw new Error("moneytree_bundle");
          transactionView = bundle.transactions === undefined ? null : bundle.transactions;
          return { ok: true, moneytreeRead: bundle.moneytreeRead };
        }
        return { ok: true, moneytreeRead: await reader({ env, now: () => clock }) };
      } catch { return { ok: false, kind: "timeout" }; }
    }, repair: pick(options, "repair", async () => true), wait: pick(options, "wait", (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds))) });
    const resolve = pick(options, "resolveCfoDailyRun", resolveCfoDailyRun), run = await resolve({ uid: value.uid }, rpcOptions);
    if (!run || run.reporting_date !== reportingDate || !UUID.test(run.run_id)) throw new Error("run");
    const render = pick(options, "renderCfoTelegram", renderCfoTelegram), send = pick(options, "deliverCfoTelegram", deliverCfoTelegram);
    const renderSnapshot = (snapshot) => {
      const input = { locale: "ja", view: "summary", snapshot };
      if (transactionView !== undefined) input.transactions = transactionView;
      render(input);
    };
    const delivery = (input) => {
      if (transactionView !== undefined) input.transactions = transactionView;
      return send(input, rpcOptions);
    };
    const latestRaw = typeof options.latestSnapshot === "function" ? await options.latestSnapshot({ uid: value.uid, reportingDate, runId: run.run_id, ...rpcOptions }) : await latestSnapshot({ uid: value.uid, reportingDate, ...value }, render);
    const latest = latestRaw ? validateRow(latestRaw, reportingDate, render) : null;
    if (latest && (latest.run_id !== run.run_id || latest.reporting_date !== reportingDate)) throw new Error("snapshot");
    let spendingGuardian;
    try {
      spendingGuardian = decideSpendingGuardian({
        reportingDate,
        observedAt: clock.toISOString(),
        transactions: transactionView && Array.isArray(transactionView.transactions) ? transactionView.transactions : [],
        budgets: {},
        protectedCash: { status: "unknown", amountMinor: null, floorMinor: null },
        history: [],
      });
    } catch { spendingGuardian = { decision: "suppress", reason: "guardian_boundary_failed", suggestion: null }; }
    const nextRevision = latest ? latest.revision + 1 : 1;
    const currentBundle = buildCfoDailyReportFromRecovery({ revision: nextRevision, recovery });
    const aiCost = await selectAiCost(options, { ...value, reportingDate }, latest, render);
    let business = null;
    try { business = await (options.observeCfoBusiness || observeCfoBusiness)({ supaUrl: value.supaUrl, supaKey: value.supaKey, fetchImpl: value.fetchImpl, observedAt: clock.toISOString() }); } catch {}
    let recommendation;
    try {
      recommendation = decideCfoRecommendation({
        observedAt: clock.toISOString(),
        profit: { status: "partial", contribution_profit: null, roi: null, coverage_exceptions: business && Array.isArray(business.exceptions) ? business.exceptions : ["business_observer_unknown"] },
        reconciliation: { reconciliation_status: "incomplete_fleet_read" },
        guardian: spendingGuardian,
      });
      if (business) business = { ...business, recommendation };
    } catch { recommendation = { schemaVersion: 1, observedAt: clock.toISOString(), kind: "repair", reason: "recommendation_boundary_failed", execute: false, ownerActionRequired: true, coverageExceptions: ["recommendation_boundary_failed"] }; }
    const providerData = { ...providerDataState(reportingDate, transactionView), moneytreeRefresh, spendingGuardian, recommendation };
    if (!latest && recovery.status === "action_required") return summary("retry", reportingDate, null, false, false, false, providerData);
    const candidateReport = reportWithAiCost(currentBundle.report, aiCost, business);
    if (latest && sameFacts(latest.report_payload, candidateReport) && ownerHour(new Date(latest.created_at)) === ownerHour(clock)) {
      const delivered = await delivery({ uid: value.uid, telegramToken: value.telegramToken, chatId: value.chatId, snapshotPublicRef: latest.public_ref, snapshot: latest.report_payload });
      if (!delivered || !["sent", "already_sent", "reconcile"].includes(delivered.status)) throw new Error("delivery");
      return summary(delivered.status === "sent" ? "sent" : delivered.status === "reconcile" ? "retry" : "quiet", reportingDate, latest.revision, false, delivered.status === "sent", recovery.status === "recovered", providerData);
    }
    const report = candidateReport;
    renderSnapshot(report);
    const append = latest ? pick(options, "appendCfoDailySnapshotRevision", appendRevision) : pick(options, "appendCfoDailySnapshot", appendInitial);
    const receipt = await append(latest ? { uid: value.uid, reportingDate, runId: run.run_id, revision: nextRevision, supersedesRevision: latest.revision, report, sourceBundle: currentBundle.sourceBundle } : { uid: value.uid, reportingDate, runId: run.run_id, moneytreeRead: recovery.moneytreeRead, report, sourceBundle: currentBundle.sourceBundle }, rpcOptions);
    if (!receipt || receipt.reporting_date !== reportingDate || receipt.run_id !== run.run_id || receipt.revision !== nextRevision || !UUID.test(receipt.public_ref)) throw new Error("receipt");
    const delivered = await delivery({ uid: value.uid, telegramToken: value.telegramToken, chatId: value.chatId, snapshotPublicRef: receipt.public_ref, snapshot: report });
    if (!delivered || !["sent", "already_sent", "reconcile"].includes(delivered.status)) throw new Error("delivery");
    return summary(delivered.status === "sent" ? "sent" : delivered.status === "reconcile" ? "retry" : "quiet", reportingDate, nextRevision, true, delivered.status === "sent", recovery.status === "recovered", providerData);
  } catch { return summary("failed", reportingDate, null, false, false, false, providerDataState(reportingDate, null)); }
}
const unavailableBilling = Object.freeze({ status: "unavailable", confirmedCount: 0, unresolvedCount: 0, unavailableCount: 1 });
const exactObject = (value, keys) => { try { return value && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype && Reflect.ownKeys(value).length === keys.length && keys.every(key => { const descriptor = Object.getOwnPropertyDescriptor(value, key); return descriptor && descriptor.enumerable && Object.hasOwn(value, key) && Object.hasOwn(descriptor, "value"); }); } catch { return false; } };
function validDate(value) { if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false; const date = new Date(`${value}T00:00:00Z`); return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value; }
function nextMonth(value) { const [year, month, day] = value.split("-").map(Number), next = `${String(month === 12 ? year + 1 : year).padStart(4, "0")}-${String(month === 12 ? 1 : month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`; return validDate(next) ? next : null; }
function activePeriod(start, end, date) { return validDate(start) && validDate(end) && validDate(date) && start < end && start <= date && date < end; }
function copyAiCost(value, date) { try { if (!exactObject(value, AI_COST_KEYS) || value.provider !== "anthropic" || value.plan !== "max_20x" || value.amount !== "220.00" || value.currency !== "USD" || value.evidenceStatus !== "provider_receipt" || !activePeriod(value.billingPeriodStart, value.billingPeriodEnd, date)) return null; const unavailable = value.unavailableProviders, keys = Array.isArray(unavailable) && Object.getPrototypeOf(unavailable) === Array.prototype ? Reflect.ownKeys(unavailable) : [], zero = keys.length === 2 && Object.getOwnPropertyDescriptor(unavailable, "0"), length = keys.length === 2 && Object.getOwnPropertyDescriptor(unavailable, "length"); if (!zero || !length || !Object.hasOwn(zero, "value") || !Object.hasOwn(length, "value") || !zero.enumerable || zero.value !== "openai" || length.value !== 1) return null; return Object.freeze({ provider: "anthropic", plan: "max_20x", amount: "220.00", currency: "USD", billingPeriodStart: value.billingPeriodStart, billingPeriodEnd: value.billingPeriodEnd, evidenceStatus: "provider_receipt", unavailableProviders: ["openai"] }); } catch { return null; } }
function receiptAiCost(value, date) { try { if (!exactObject(value, ["status", "record_id", "confirmed"]) || !["appended", "existing"].includes(value.status) || typeof value.record_id !== "string" || !exactObject(value.confirmed, RECEIPT_KEYS)) return null; const record = value.confirmed, start = record.billing_period_start, end = record.billing_period_end, subtotal = /^\d+\.\d{2}$/.test(record.subtotal) ? BigInt(record.subtotal.replace(".", "")) : -1n, tax = /^\d+\.\d{2}$/.test(record.tax) ? BigInt(record.tax.replace(".", "")) : -1n, total = /^\d+\.\d{2}$/.test(record.total) ? BigInt(record.total.replace(".", "")) : -1n; if (record.schema_version !== "lm_subscription_receipt_v1" || record.provider !== "anthropic" || record.plan !== "max_20x" || record.currency !== "USD" || record.evidence_status !== "provider_receipt" || record.paid_date !== start || record.source_hash !== value.record_id || !/^sha256:[0-9a-f]{64}$/.test(record.source_hash) || subtotal !== 20000n || tax !== 2000n || total !== 22000n || subtotal + tax !== total || end !== nextMonth(start) || !activePeriod(start, end, date)) return null; return copyAiCost({ provider: "anthropic", plan: "max_20x", amount: "220.00", currency: "USD", billingPeriodStart: start, billingPeriodEnd: end, evidenceStatus: "provider_receipt", unavailableProviders: ["openai"] }, date); } catch { return null; } }
function reportWithAiCost(report, aiCost, business = null) { const copy = structuredClone(report); if (aiCost) copy.aiCost = aiCost; if (business) copy.business = business; return copy; }
async function selectAiCost(options, value, latest, render) { const current = copyAiCost(options.aiCost, value.reportingDate) || (latest && copyAiCost(latest.report_payload.aiCost, value.reportingDate)); if (current) return current; try { const candidate = typeof options.latestAiCost === "function" ? await options.latestAiCost({ uid: value.uid, reportingDate: value.reportingDate, supaUrl: value.supaUrl, supaKey: value.supaKey, fetchImpl: value.fetchImpl }) : await latestAiCostSnapshot(value, render); const row = candidate && candidate.report_payload ? validateRow(candidate, candidate.reporting_date, render) : null; return row ? copyAiCost(row.report_payload.aiCost, value.reportingDate) : copyAiCost(candidate, value.reportingDate); } catch { return null; } }
async function appendInitial(input, options) { const config = rpc.validateOptions(options), parsed = await rpc.postRpc(config, "lm_append_cfo_daily_snapshot", { p_uid: input.uid, p_reporting_date: input.reportingDate, p_run_id: input.runId, p_report_payload: input.report, p_source_bundle: input.sourceBundle }), keys = exactObject(parsed, ["public_ref", "reporting_date", "run_id", "revision", "created_at"]) ? 5 : exactObject(parsed, ["public_ref", "reporting_date", "run_id", "revision", "created_at", "supersedes_revision"]) && parsed.supersedes_revision === null ? 6 : 0; if (!keys || !UUID.test(parsed.public_ref) || parsed.reporting_date !== input.reportingDate || parsed.run_id !== input.runId || parsed.revision !== 1 || typeof parsed.created_at !== "string" || !rpc.timestamp(parsed.created_at)) throw new Error("receipt"); return { public_ref: parsed.public_ref, reporting_date: parsed.reporting_date, run_id: parsed.run_id, revision: parsed.revision, created_at: parsed.created_at }; }
function billingCounts(receipt) {
  const c = receipt && receipt.confirmed, scope = c && c.scope, amount = c && c.amount;
  const confirmed = exactObject(receipt, ["status", "record_id", "confirmed"]) && (receipt.status === "appended" || receipt.status === "existing") && typeof receipt.record_id === "string" && /^sha256:[0-9a-f]{64}$/.test(receipt.record_id) && exactObject(c, ["schema_version", "provider", "billing_period", "scope", "amount", "source", "source_document_ref", "observed_at", "evidence_status"]) && c.schema_version === 1 && c.provider === "google_cloud" && /^(?:\d{4}(?:0[1-9]|1[0-2]))$/.test(c.billing_period) && exactObject(scope, ["kind", "ref"]) && scope.kind === "billing_account" && /^sha256:[0-9a-f]{64}$/.test(scope.ref) && exactObject(amount, ["value", "currency"]) && amount.currency === "JPY" && typeof amount.value === "string" && /^(?:0|[1-9]\d*)$/.test(amount.value) && c.source === "provider_invoice_pdf" && c.source_document_ref === receipt.record_id && /^sha256:[0-9a-f]{64}$/.test(c.source_document_ref) && typeof c.observed_at === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(c.observed_at) && Number.isFinite(Date.parse(c.observed_at)) && c.evidence_status === "provider_billed";
  return confirmed ? Object.freeze({ status: "confirmed_unresolved", confirmedCount: 1, unresolvedCount: 1, unavailableCount: 0 }) : unavailableBilling;
}
async function main(options = {}) {
  const usage = options && typeof options.runLocalAgentUsageCollection === "function" ? options.runLocalAgentUsageCollection : runLocalAgentUsageCollection;
  try {
    const sourceEnv = options.env || process.env, descriptor = Object.getOwnPropertyDescriptor(sourceEnv, "LIFE_MANAGER_STATE_HOME");
    const env = descriptor && Object.hasOwn(descriptor, "value") ? { LIFE_MANAGER_STATE_HOME: descriptor.value } : {};
    await usage({ env });
  } catch {}
  const sourceEnv = options.env || process.env, clock = new Date(typeof options.now === "function" ? options.now() : (options.now || new Date())); let providerBilling = unavailableBilling, aiCost = null;
  if (sourceEnv.GOG_ACCOUNT) { let mail = null; try { mail = (typeof options.makeGogMail === "function" ? options.makeGogMail : makeGogMail)({ account: sourceEnv.GOG_ACCOUNT }); } catch {}
    if (mail) { try { const capture = typeof options.captureLatestGoogleCloudInvoice === "function" ? options.captureLatestGoogleCloudInvoice : captureLatestGoogleCloudInvoice; aiCost = null; providerBilling = billingCounts(await capture({ stateRoot: sourceEnv.LIFE_MANAGER_STATE_HOME || path.join(os.homedir(), ".local", "state", "life-manager"), observedAt: clock.toISOString(), mail })); } catch {}
      try { const capture = typeof options.captureLatestAnthropicSubscriptionReceipt === "function" ? options.captureLatestAnthropicSubscriptionReceipt : captureLatestAnthropicSubscriptionReceipt; aiCost = receiptAiCost(await capture({ stateRoot: sourceEnv.LIFE_MANAGER_STATE_HOME || path.join(os.homedir(), ".local", "state", "life-manager"), observedAt: clock.toISOString(), mail }), ownerDate(clock)); } catch {} }
  }
  const result = await runHourlyCfo({ ...options, aiCost, now: clock }); const stdout = options && typeof options.stdout === "function" ? options.stdout : (line) => process.stdout.write(`${line}\n`); stdout(JSON.stringify({ ...result, providerBilling })); return { exitCode: ["sent", "quiet"].includes(result.status) ? 0 : 1, summary: result, providerBilling }; }

if (require.main === module) main().then((result) => { process.exitCode = result.exitCode; });

module.exports = { runHourlyCfo, main };
