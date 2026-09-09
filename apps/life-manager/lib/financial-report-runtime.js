"use strict";

const { createHash } = require("node:crypto");
const canonicalize = require("canonicalize");

const {
  buildFinancialSnapshot,
  periodBounds,
  usdMicrosFromDecimal,
} = require("./financial-report-snapshot.js");
const { FINANCIAL_STRINGS } = require("./i18n.js");
const { readWalletLedger } = require("./payout-runtime.js");
const { hashChatId, sendMessage } = require("./telegram.js");

const PAGE_SIZE = 1000;
const MAX_COST_ROWS = 10_000;

function credentials(opts = {}) {
  const supaUrl = opts.supaUrl || process.env.SUPABASE_URL;
  const supaKey = opts.supaKey || process.env.SUPABASE_SERVICE_ROLE_KEY;
  const fetchImpl = opts.fetchImpl || globalThis.fetch;
  if (!supaUrl || !supaKey) throw new Error("financial report runtime needs Supabase credentials");
  if (typeof fetchImpl !== "function") throw new Error("financial report runtime needs fetch");
  return { supaUrl: String(supaUrl).replace(/\/$/, ""), supaKey, fetchImpl };
}

function headers(key, extra = {}) {
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

async function jsonRows(response, label) {
  if (!response || !response.ok) {
    throw new Error(`${label} failed (${response ? response.status : "no response"})`);
  }
  const rows = await response.json();
  if (!Array.isArray(rows)) throw new Error(`${label} returned a non-array body`);
  return rows;
}

async function readFinancialTenant(uid, opts = {}) {
  const tenantUid = String(uid == null ? "" : uid).trim();
  if (!tenantUid) throw new Error("financial report tenant uid is required");
  const { supaUrl, supaKey, fetchImpl } = credentials(opts);
  const userUrl = `${supaUrl}/rest/v1/lm_users?uid=eq.${encodeURIComponent(tenantUid)}` +
    "&select=uid,telegram_chat_id,agent_wallet_address&limit=1";
  const users = await jsonRows(await fetchImpl(userUrl, {
    headers: headers(supaKey),
  }), "financial report tenant read");
  if (users.length !== 1) throw new Error("financial report tenant lookup did not resolve exactly one row");
  const prefUrl = `${supaUrl}/rest/v1/lm_panel_preferences?uid=eq.${encodeURIComponent(tenantUid)}` +
    "&select=notifications_enabled,call_time_zone&limit=1";
  const preferences = await jsonRows(await fetchImpl(prefUrl, {
    headers: headers(supaKey),
  }), "financial report preferences read");
  if (preferences.length > 1) throw new Error("financial report preferences returned more than one row");
  return {
    notifications_enabled: true,
    call_time_zone: "Asia/Tokyo",
    ...users[0],
    ...(preferences[0] || {}),
  };
}

async function readCostLedger(uid, opts = {}) {
  const tenantUid = String(uid == null ? "" : uid).trim();
  if (!tenantUid) throw new Error("financial report tenant uid is required");
  const { supaUrl, supaKey, fetchImpl } = credentials(opts);
  const query = `uid=eq.${encodeURIComponent(tenantUid)}` +
    "&select=id,ts,kind,quantity,unit,est_usd,meta&order=ts.asc,id.asc";
  const rows = [];
  for (let start = 0; start <= MAX_COST_ROWS; start += PAGE_SIZE) {
    const end = start === MAX_COST_ROWS ? start : start + PAGE_SIZE - 1;
    const page = await jsonRows(await fetchImpl(`${supaUrl}/rest/v1/lm_api_cost?${query}`, {
      headers: headers(supaKey, { Range: `${start}-${end}` }),
    }), "financial report cost ledger read");
    if (start === MAX_COST_ROWS) {
      if (page.length > 0) throw new Error(`financial report cost ledger exceeds ${MAX_COST_ROWS} rows`);
      break;
    }
    rows.push(...page);
    if (page.length < PAGE_SIZE) break;
  }
  return rows;
}

async function readFinancialCostTotals(uid, period, opts = {}) {
  const tenantUid = String(uid == null ? "" : uid).trim();
  if (!tenantUid) throw new Error("financial report tenant uid is required");
  const periodStart = String(period && period.period_start || "");
  const periodEnd = String(period && period.period_end || "");
  if (!Number.isFinite(Date.parse(periodStart))
    || !Number.isFinite(Date.parse(periodEnd))
    || Date.parse(periodStart) >= Date.parse(periodEnd)) {
    throw new Error("financial report cost period is invalid");
  }
  const { supaUrl, supaKey, fetchImpl } = credentials(opts);
  const rows = await jsonRows(await fetchImpl(
    `${supaUrl}/rest/v1/rpc/lm_financial_cost_totals`,
    {
      method: "POST",
      headers: headers(supaKey),
      body: JSON.stringify({
        p_uid: tenantUid,
        p_period_start: periodStart,
        p_period_end: periodEnd,
      }),
    },
  ), "financial report cost totals read");
  if (rows.length !== 1) throw new Error("financial report cost totals returned an invalid row count");
  const row = rows[0] || {};
  const result = {
    period_est_usd: String(row.period_est_usd == null ? "" : row.period_est_usd),
    all_time_est_usd: String(row.all_time_est_usd == null ? "" : row.all_time_est_usd),
    period_rows: Number(row.period_rows),
    all_time_rows: Number(row.all_time_rows),
  };
  // Reuse the same conservative decimal parser that the pure snapshot uses.
  usdMicrosFromDecimal(result.period_est_usd);
  usdMicrosFromDecimal(result.all_time_est_usd);
  if (!Number.isSafeInteger(result.period_rows) || result.period_rows < 0
    || !Number.isSafeInteger(result.all_time_rows) || result.all_time_rows < 0) {
    throw new Error("financial report cost totals returned invalid counts");
  }
  return result;
}

function receiptQuery(uid, kind, periodKey) {
  return `uid=eq.${encodeURIComponent(uid)}` +
    `&report_kind=eq.${encodeURIComponent(kind)}` +
    `&period_key=eq.${encodeURIComponent(periodKey)}`;
}

async function readFinancialReceipt(identity, opts = {}) {
  const { supaUrl, supaKey, fetchImpl } = credentials(opts);
  const query = receiptQuery(identity.uid, identity.report_kind, identity.period_key);
  const rows = await jsonRows(await fetchImpl(
    `${supaUrl}/rest/v1/lm_financial_report_receipts?${query}&select=*&limit=1`,
    { headers: headers(supaKey) },
  ), "financial report receipt read");
  return rows[0] || null;
}

async function claimFinancialReceipt(receipt, opts = {}) {
  const { supaUrl, supaKey, fetchImpl } = credentials(opts);
  const response = await fetchImpl(`${supaUrl}/rest/v1/lm_financial_report_receipts`, {
    method: "POST",
    headers: headers(supaKey, {
      Prefer: "resolution=ignore-duplicates,return=representation",
    }),
    body: JSON.stringify(receipt),
  });
  const rows = await jsonRows(response, "financial report receipt claim");
  return { claimed: rows.length === 1 };
}

async function patchFinancialReceipt(identity, patch, opts = {}) {
  const { supaUrl, supaKey, fetchImpl } = credentials(opts);
  const query = `${receiptQuery(identity.uid, identity.report_kind, identity.period_key)}` +
    `&status=eq.pending&snapshot_hash=eq.${encodeURIComponent(identity.snapshot_hash)}`;
  const response = await fetchImpl(`${supaUrl}/rest/v1/lm_financial_report_receipts?${query}`, {
    method: "PATCH",
    headers: headers(supaKey, { Prefer: "return=representation" }),
    body: JSON.stringify(patch),
  });
  const rows = await jsonRows(response, "financial report receipt update");
  if (rows.length !== 1) throw new Error("financial report receipt update lost its claim");
  return true;
}

function markFinancialReceiptSent(identity, messageId, opts = {}) {
  if (!Number.isInteger(messageId) || messageId <= 0) {
    throw new Error("financial report needs a positive Telegram message id");
  }
  return patchFinancialReceipt(identity, {
    status: "sent",
    telegram_message_id: messageId,
    failure_code: null,
    sent_at: new Date(opts.nowMs == null ? Date.now() : opts.nowMs).toISOString(),
    updated_at: new Date(opts.nowMs == null ? Date.now() : opts.nowMs).toISOString(),
  }, opts);
}

function markFinancialReceiptFailed(identity, code, opts = {}) {
  const failureCode = String(code || "");
  if (!/^[a-z0-9_]{1,128}$/.test(failureCode)) {
    throw new Error("financial report failure code is not bounded");
  }
  return patchFinancialReceipt(identity, {
    status: "failed",
    telegram_message_id: null,
    failure_code: failureCode,
    sent_at: null,
    updated_at: new Date(opts.nowMs == null ? Date.now() : opts.nowMs).toISOString(),
  }, opts);
}

function localClock(nowMs, timezone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(nowMs))
    .filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
  const weekday = new Date(Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
  )).getUTCDay();
  return { weekday, hour: Number(parts.hour), minute: Number(parts.minute) };
}

function dueReportKinds(nowMs, timezone) {
  const clock = localClock(nowMs, timezone);
  const minuteOfDay = clock.hour * 60 + clock.minute;
  const kinds = [];
  if (minuteOfDay >= 20 * 60) kinds.push("daily");
  if (clock.weekday === 0 && minuteOfDay >= 20 * 60 + 5) kinds.push("weekly");
  return kinds;
}

function integerText(value, label) {
  const raw = String(value == null ? "" : value);
  if (!/^-?\d+$/.test(raw)) throw new Error(`${label} must be integer money`);
  return BigInt(raw);
}

function formatScaled(value, scale, minimumDecimals) {
  const amount = integerText(value, "money");
  const negative = amount < 0n;
  const absolute = negative ? -amount : amount;
  const divisor = 10n ** BigInt(scale);
  const whole = absolute / divisor;
  const rawFraction = String(absolute % divisor).padStart(scale, "0");
  let decimals = rawFraction.replace(/0+$/, "");
  if (decimals.length < minimumDecimals) decimals = rawFraction.slice(0, minimumDecimals);
  const body = `$${whole}.${decimals}`;
  return negative ? `-${body}` : body;
}

function formatUsdMicros(value, { signed = false } = {}) {
  const formatted = formatScaled(value, 6, 2);
  if (signed && !formatted.startsWith("-")) return `+${formatted}`;
  return formatted;
}

function formatUsdcAtomic(value) {
  return formatScaled(value, 6, 2);
}

function fill(template, values) {
  return template.replace(/\{(\w+)\}/g, (_match, key) => String(values[key]));
}

function renderFinancialReport(snapshot, locale = "ja") {
  const strings = FINANCIAL_STRINGS[locale] && FINANCIAL_STRINGS[locale].reports;
  if (!strings) throw new Error(`financial report has no copy for locale ${locale}`);
  if (!snapshot || (snapshot.kind !== "daily" && snapshot.kind !== "weekly")) {
    throw new Error("financial report needs a daily or weekly snapshot");
  }
  if (snapshot.kind === "daily") {
    const cost = integerText(snapshot.realized_loss_usd_micros, "loss")
      + integerText(snapshot.financial_fee_usd_micros, "fee")
      + integerText(snapshot.api_cost_usd_micros, "api cost");
    const state = strings.states[snapshot.stop_reason];
    if (!state) throw new Error(`financial report has no state copy for ${snapshot.stop_reason}`);
    return [
      strings.dailyHeader,
      fill(strings.balance, { balance: formatUsdcAtomic(snapshot.balance_usdc_atomic) }),
      fill(strings.gross, { gross: formatUsdMicros(snapshot.gross_usd_micros, { signed: true }) }),
      fill(strings.cost, { cost: formatUsdMicros(cost) }),
      fill(strings.net, { net: formatUsdMicros(snapshot.operating_net_usd_micros, { signed: true }) }),
      fill(strings.state, { state }),
    ].join("\n");
  }
  const railLines = (Array.isArray(snapshot.rail_pnl) ? snapshot.rail_pnl : [])
    .map((row) => fill(strings.rail, {
      rail: row.rail,
      net: formatUsdMicros(row.net_usd_micros, { signed: true }),
    }));
  const ratio = snapshot.self_funded_bps == null
    ? "未計測"
    : `${(snapshot.self_funded_bps / 100).toFixed(2)}%`;
  return [
    strings.weeklyHeader,
    ...railLines,
    fill(strings.selfFunded, { ratio }),
    fill(strings.distributable, { amount: formatUsdcAtomic(snapshot.distributable_usdc_atomic) }),
  ].join("\n");
}

function snapshotHash(snapshot) {
  const canonical = canonicalize(snapshot);
  if (typeof canonical !== "string") {
    throw new Error("financial report snapshot is not canonicalizable JSON");
  }
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

function latestTimestamp(rows, field, cutoff) {
  const cutoffMs = Date.parse(cutoff);
  let latest = null;
  for (const row of Array.isArray(rows) ? rows : []) {
    const value = String(row && row[field] || "");
    const at = Date.parse(value);
    if (!Number.isFinite(at) || at > cutoffMs) continue;
    if (latest == null || at > Date.parse(latest)) latest = new Date(at).toISOString();
  }
  return latest;
}

function sourceFreshness(snapshot, ledgerRows, costRows) {
  return Object.freeze({
    report_cutoff_at: snapshot.period_end,
    earnings_latest_at: latestTimestamp(ledgerRows, "occurred_at", snapshot.period_end),
    costs_latest_at: latestTimestamp(costRows, "ts", snapshot.period_end),
    balance_observed_at: snapshot.period_end,
  });
}

async function runFinancialReport(request = {}, deps = {}) {
  const uid = String(request.uid == null ? "" : request.uid).trim();
  const kind = String(request.kind == null ? "" : request.kind);
  const nowMs = request.nowMs == null ? Date.now() : Number(request.nowMs);
  if (!uid) throw new Error("financial report uid is required");
  if (kind !== "daily" && kind !== "weekly") throw new Error("financial report kind must be daily or weekly");
  if (!Number.isFinite(nowMs)) throw new Error("financial report nowMs must be an instant");

  const readTenant = deps.readTenant || ((targetUid) => readFinancialTenant(targetUid, deps));
  const tenant = await readTenant(uid);
  if (!tenant || String(tenant.uid || "") !== uid) throw new Error("financial report tenant scope mismatch");
  if (tenant.notifications_enabled === false) {
    return { status: "skipped", reason: "notifications_disabled", report_kind: kind };
  }
  if (!String(tenant.telegram_chat_id || "")) {
    return { status: "skipped", reason: "telegram_unbound", report_kind: kind };
  }
  if (!String(tenant.agent_wallet_address || "")) {
    return { status: "skipped", reason: "agent_wallet_unbound", report_kind: kind };
  }
  const timezone = String(tenant.call_time_zone || "Asia/Tokyo");
  if (!request.force && !dueReportKinds(nowMs, timezone).includes(kind)) {
    return { status: "skipped", reason: "not_due", report_kind: kind };
  }

  const bounds = periodBounds({ kind, nowMs, timezone });
  const identity = { uid, report_kind: kind, period_key: bounds.period_key };
  const readReceipt = deps.readReceipt || ((key) => readFinancialReceipt(key, deps));
  const existing = await readReceipt(identity);
  if (existing) {
    const reportCutoff = String(
      existing.period_end || (existing.snapshot && existing.snapshot.period_end) || "",
    );
    return {
      status: existing.status === "sent" ? "duplicate" : existing.status,
      report_kind: kind,
      period_key: bounds.period_key,
      ...(existing.telegram_message_id == null
        ? {}
        : { telegram_message_id: Number(existing.telegram_message_id) }),
      ...(existing.snapshot_hash == null
        ? {}
        : { snapshot_hash: String(existing.snapshot_hash) }),
      ...(existing.status === "sent"
        ? {
          chat_id_hash: hashChatId(tenant.telegram_chat_id),
          sent_at: String(existing.sent_at),
          source_freshness: {
            report_cutoff_at: reportCutoff,
            earnings_latest_at: null,
            costs_latest_at: null,
            balance_observed_at: null,
          },
        }
        : {}),
    };
  }

  const readLedger = deps.readLedger || ((wallet) => readWalletLedger(wallet, deps));
  if (typeof deps.readBalance !== "function") {
    throw new Error("financial report runtime needs a measured Base balance reader");
  }
  const [ledgerRows, costs, onchainUsdcAtomic] = await Promise.all([
    readLedger(tenant.agent_wallet_address),
    deps.readCosts
      ? Promise.resolve(deps.readCosts(uid)).then((rows) => ({
        periodRows: rows,
        allTimeRows: rows,
      }))
      : readFinancialCostTotals(uid, bounds, deps).then((totals) => {
        const aggregateAt = new Date(Date.parse(bounds.period_start) + 1).toISOString();
        return {
          periodRows: [{
            ts: aggregateAt,
            kind: "period_aggregate",
            est_usd: totals.period_est_usd,
          }],
          allTimeRows: [{
            ts: aggregateAt,
            kind: "all_time_aggregate",
            est_usd: totals.all_time_est_usd,
          }],
        };
      }),
    deps.readBalance(tenant.agent_wallet_address),
  ]);
  const snapshot = buildFinancialSnapshot({
    kind,
    nowMs,
    timezone,
    walletAddress: tenant.agent_wallet_address,
    onchainUsdcAtomic,
    earningsRows: ledgerRows,
    costRows: costs.periodRows,
    allTimeEarningsRows: ledgerRows,
    allTimeCostRows: costs.allTimeRows,
    reserveAtomic: request.reserveAtomic,
  });
  const hash = snapshotHash(snapshot);
  const receipt = {
    ...identity,
    timezone,
    period_start: snapshot.period_start,
    period_end: snapshot.period_end,
    snapshot,
    snapshot_hash: hash,
    status: "pending",
  };
  const claimReceipt = deps.claimReceipt || ((row) => claimFinancialReceipt(row, deps));
  const claim = await claimReceipt(receipt);
  if (!claim || claim.claimed !== true) {
    return { status: "duplicate", report_kind: kind, period_key: bounds.period_key };
  }

  const claimedIdentity = { ...identity, snapshot_hash: hash };
  const telegramToken = deps.telegramToken || process.env.LM_TELEGRAM_BOT_TOKEN;
  if (!telegramToken) throw new Error("financial report needs a Telegram token");
  const sendTelegram = deps.sendTelegram || sendMessage;
  const markFailed = deps.markReceiptFailed
    || ((key, code) => markFinancialReceiptFailed(key, code, { ...deps, nowMs }));
  let sent;
  try {
    sent = await sendTelegram(
      telegramToken,
      String(tenant.telegram_chat_id),
      renderFinancialReport(snapshot),
    );
    if (!sent || sent.ok !== true || !Number.isInteger(sent.result && sent.result.message_id)) {
      throw new Error("Telegram rejected financial report");
    }
  } catch (error) {
    await markFailed(claimedIdentity, "telegram_rejected");
    throw new Error(`Telegram financial report failed: ${error && error.message ? error.message : error}`);
  }
  const messageId = sent.result.message_id;
  const markSent = deps.markReceiptSent
    || ((key, id) => markFinancialReceiptSent(key, id, { ...deps, nowMs }));
  await markSent(claimedIdentity, messageId);
  return {
    status: "sent",
    report_kind: kind,
    period_key: bounds.period_key,
    telegram_message_id: messageId,
    snapshot,
    snapshot_hash: hash,
    source_freshness: sourceFreshness(snapshot, ledgerRows, costs.periodRows),
  };
}

module.exports = {
  PAGE_SIZE,
  MAX_COST_ROWS,
  readFinancialTenant,
  readCostLedger,
  readFinancialCostTotals,
  readFinancialReceipt,
  claimFinancialReceipt,
  markFinancialReceiptSent,
  markFinancialReceiptFailed,
  dueReportKinds,
  formatUsdMicros,
  renderFinancialReport,
  snapshotHash,
  sourceFreshness,
  runFinancialReport,
};
