"use strict";

const { CFO_STRINGS } = require("./i18n.js");
// The HTTP webhook/detail owner remains cfo-telegram-callback.js. This module only renders
// hourly snapshots and delegates the exported callback helper to that canonical owner.
const { handleCfoTelegramCallback } = require("./cfo-telegram-callback.js");

const STATES = new Set(["complete", "partial", "recovered", "action_required"]);
const VIEWS = new Set(["summary", "accounts", "accuracy", "why", "business", "ai_cost"]);
const STATUSES = new Set(["fresh", "stale", "unavailable"]);
const EVIDENCE = Object.freeze({
  provider_billed: { ja: "確定", en: "Confirmed" },
  provider_reported: { ja: "実測", en: "Measured" },
  locally_estimated: { ja: "推定", en: "Estimated" },
  unavailable: { ja: "不明", en: "Unknown" },
});
const ROOT_KEYS = new Set(["schemaVersion", "reportingDate", "revision", "state", "currency", "totals", "sources", "excluded", "repair", "action", "aiCost", "business"]);
const AI_COST_KEYS = ["provider", "plan", "amount", "currency", "billingPeriodStart", "billingPeriodEnd", "evidenceStatus", "unavailableProviders"];
const TOTAL_KEYS = new Set(["assetsMinor", "liabilitiesMinor", "netWorthMinor", "changeMinor"]);
const SOURCE_KEYS = new Set(["sourceId", "label", "status", "asOf", "amountMinor", "verificationStatus"]);
const EXCLUDED_KEYS = new Set(["label", "reason"]);
const REPAIR_KEYS = new Set(["sourceLabel", "freshReread", "reconciled"]);
const ACTION_KEYS = new Set(["kind", "sourceLabel", "retryLabel", "nextRetryAt"]); const ACTION_LABELS = Object.freeze({ reconsent: "接続後に自動再確認", provider_outage: "30分後に自動再確認" }); const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const TRANSACTION_KEYS = new Set(["schemaVersion", "sourceId", "asOf", "pagePartial", "categoryCoverage", "latestBookingDate", "transactions"]);
const TRANSACTION_ROW_KEYS = new Set(["bookingDate", "amountMinor", "flow", "verificationStatus", "category"]);
const TRANSACTION_FLOWS = new Set(["inflow", "outflow", "neutral"]);
const CATEGORY_COVERAGE = new Set(["unavailable", "partial", "provider_reported"]);
const BUTTONS = Object.freeze({
  // Keep emitted buttons inside the canonical callback owner's view set. AI-cost text remains
  // available in the summary/detail renderer, but no unsupported callback route is generated.
  summary: [["accounts", "口座を見る", "View accounts"], ["business", "仕事を見る", "View businesses"], ["accuracy", "正確さを見る", "View accuracy"]],
  accounts: [["summary", "概要に戻る", "Back to summary"], ["accuracy", "正確さを見る", "View accuracy"]],
  accuracy: [["summary", "概要に戻る", "Back to summary"], ["why", "なぜこの金額？", "Why this amount?"]],
  why: [["summary", "概要に戻る", "Back to summary"]],
  business: [["summary", "概要に戻る", "Back to summary"], ["accuracy", "数字の確かさ", "Evidence"]],
  ai_cost: [["summary", "概要に戻る", "Back to summary"]],
});
const CALLBACK = /^cfo:(summary|accounts|accuracy|why|business|ai_cost):(\d{8}):([1-9]\d*)$/;
const RETRY_TOAST = "読み込めませんでした。もう一度お試しください";

function fail(reason) { throw new Error(`cfo_telegram_invalid:${reason}`); }
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function validateAiCost(snapshot) { try { if (snapshot === null || (typeof snapshot !== "object" && typeof snapshot !== "function")) return; if (!Reflect.ownKeys(snapshot).includes("aiCost")) return; const root = Object.getOwnPropertyDescriptor(snapshot, "aiCost"); if (!root || !("value" in root)) throw Error(); const value = root.value, keys = Reflect.ownKeys(value); if (Object.getPrototypeOf(value) !== Object.prototype || keys.length !== AI_COST_KEYS.length || keys.some((key) => !AI_COST_KEYS.includes(key))) throw Error(); for (const key of keys) if (!("value" in Object.getOwnPropertyDescriptor(value, key))) throw Error(); if (value.provider !== "anthropic" || value.plan !== "max_20x" || value.amount !== "220.00" || value.currency !== "USD" || value.evidenceStatus !== "provider_receipt") throw Error(); date(value.billingPeriodStart); date(value.billingPeriodEnd); if (!(Date.parse(`${value.billingPeriodEnd}T00:00:00Z`) > Date.parse(`${value.billingPeriodStart}T00:00:00Z`))) throw Error(); const unavailable = Object.getOwnPropertyDescriptor(value, "unavailableProviders").value, arrayKeys = Reflect.ownKeys(unavailable); if (!Array.isArray(unavailable) || Object.getPrototypeOf(unavailable) !== Array.prototype || arrayKeys.length !== 2 || arrayKeys.some((key) => key !== "0" && key !== "length")) throw Error(); for (const key of arrayKeys) if (!("value" in Object.getOwnPropertyDescriptor(unavailable, key))) throw Error(); if (Object.getOwnPropertyDescriptor(unavailable, "length").value !== 1 || Object.getOwnPropertyDescriptor(unavailable, "0").value !== "openai") throw Error(); } catch { fail("invalid_ai_cost"); } }
function sanitize(snapshot) {
  if (!plain(snapshot)) return snapshot;
  const strip = (value) => Object.fromEntries(Object.entries(value).filter(([key]) => !/account|raw|credential|token|secret|password|cookie|oauth/i.test(key)));
  const safe = strip(snapshot);
  if (Array.isArray(snapshot.sources)) safe.sources = snapshot.sources.map((source) => plain(source) ? strip(source) : source);
  return safe;
}
function exact(value, allowed, required = []) {
  if (!plain(value)) fail("invalid_object");
  if (Object.keys(value).some((key) => !allowed.has(key))) fail("unknown_key");
  if (required.some((key) => !Object.prototype.hasOwnProperty.call(value, key))) fail("missing_key");
}
function label(value) { if (typeof value !== "string" || value.length === 0) fail("invalid_label"); }
function safeAmount(value) { return value === null || Number.isSafeInteger(value); }
function date(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail("invalid_reporting_date");
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) fail("invalid_reporting_date");
}
function timestamp(value) {
  const m = typeof value === "string" && RFC3339.exec(value); if (!m) return false; const year = +m[1], month = +m[2], day = +m[3], hour = +m[4], minute = +m[5], second = +m[6], zone = m[8], zh = zone === "Z" ? 0 : +zone.slice(1, 3), zm = zone === "Z" ? 0 : +zone.slice(4), local = new Date(0), end = new Date(0), fraction = m[7] ? +(m[7].slice(1) + "000").slice(0, 3) : 0; local.setUTCFullYear(year, month - 1, day); local.setUTCHours(hour, minute, second, fraction); end.setUTCFullYear(year, month, 0); if (month < 1 || month > 12 || day < 1 || day > end.getUTCDate() || hour > 23 || minute > 59 || second > 59 || zh > 23 || zm > 59) return false; const offset = zone === "Z" ? 0 : (zone[0] === "-" ? -1 : 1) * (zh * 60 + zm), ms = local.getTime() - offset * 60000; return Number.isFinite(ms) && Date.parse(value) === ms;
}
function validateTransactions(value) {
  if (value === null) return null;
  exact(value, TRANSACTION_KEYS, [...TRANSACTION_KEYS]);
  if (value.schemaVersion !== 1 || value.sourceId !== "moneytree_mufg" || !timestamp(value.asOf) || typeof value.pagePartial !== "boolean" || !CATEGORY_COVERAGE.has(value.categoryCoverage) || (value.latestBookingDate !== null && (typeof value.latestBookingDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value.latestBookingDate))) || !Array.isArray(value.transactions)) fail("invalid_transactions");
  value.transactions.forEach((row) => {
    exact(row, TRANSACTION_ROW_KEYS, [...TRANSACTION_ROW_KEYS]);
    date(row.bookingDate);
    if (!Number.isSafeInteger(row.amountMinor) || !TRANSACTION_FLOWS.has(row.flow) || row.verificationStatus !== "provider_reported" || (row.category !== null && (typeof row.category !== "string" || row.category.length === 0 || row.category.length > 128 || row.category.trim() !== row.category))) fail("invalid_transaction");
  });
  return value;
}
function escapeHtml(value) { return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function formatAmount(locale, value) {
  if (value === null) return CFO_STRINGS[locale].unknown;
  const number = new Intl.NumberFormat(locale === "ja" ? "ja-JP" : "en-US").format(value);
  return locale === "ja" ? `¥${number}` : `JPY ${number}`;
}
function formatChange(locale, value) {
  if (value === null) return CFO_STRINGS[locale].unknown;
  return `${value >= 0 ? "+" : "-"}${formatAmount(locale, Math.abs(value))}`;
}
function transactionText(locale, value, reportingDate) {
  if (value === null) return locale === "ja"
    ? "\n\n💳 最近の取引\n取得できませんでした。現在の取引は不明です。"
    : "\n\n💳 Recent transactions\nCould not read them. Current activity is unknown.";
  const direction = locale === "ja" ? { inflow: "入金", outflow: "支出", neutral: "中立" } : { inflow: "In", outflow: "Out", neutral: "Neutral" };
  const rows = value.transactions.length === 0
    ? (locale === "ja" ? "最近の取引はありません" : "No recent transactions")
    : value.transactions.map((row) => `${direction[row.flow]}\t${escapeHtml(row.bookingDate)}\t${formatAmount(locale, Math.abs(row.amountMinor))}\t${row.category === null ? (locale === "ja" ? "カテゴリ: 未取得" : "Category: unavailable") : `${locale === "ja" ? "カテゴリ: " : "Category: "}${escapeHtml(row.category)}`}`).join("\n");
  const page = value.pagePartial
    ? (locale === "ja" ? "表示は取得範囲の一部です（続きあり）" : "This is a partial page (more may exist)")
    : (locale === "ja" ? "取得範囲内の取引を表示しています" : "All transactions in the retrieved range are shown");
  const latestDate = value.latestBookingDate;
  const lagDays = latestDate && /^\d{4}-\d{2}-\d{2}$/.test(reportingDate || "")
    ? Math.floor((Date.parse(`${reportingDate}T00:00:00Z`) - Date.parse(`${latestDate}T00:00:00Z`)) / 86400000)
    : null;
  const latest = latestDate === null ? "" : locale === "ja"
    ? `\n⚠️ 接続済みMoneytreeプラグインの読み取りは成功しています。Moneytree返却データの最新取引は${escapeHtml(latestDate)}${Number.isSafeInteger(lagDays) && lagDays > 0 ? `（${lagDays}日前）` : ""}。これはプロバイダーのスナップショットで、残高はリアルタイムではありません。銀行側更新時刻は不明です。ループは次回の毎時読み取りで再確認します。ループは追加のログインを要求しません。`
    : `\n⚠️ The connected Moneytree plugin read succeeded. The latest transaction returned by Moneytree is ${escapeHtml(latestDate)}${Number.isSafeInteger(lagDays) && lagDays > 0 ? ` (${lagDays} days old)` : ""}. This is a provider snapshot. The balance is not realtime; bank-side update time is unknown. The loop will recheck on the next hourly read. The loop does not request an additional login.`;
  const title = locale === "ja" ? "💳 最近の取引（実測）" : "💳 Recent transactions (Measured)";
  return `\n\n${title}\n${rows}\n${page}${latest}`;
}
function validateSnapshot(snapshot) {
  exact(snapshot, ROOT_KEYS, [...ROOT_KEYS].filter((key) => key !== "aiCost" && key !== "business"));
  if (snapshot.schemaVersion !== 1) fail("invalid_schema_version");
  date(snapshot.reportingDate);
  if (!Number.isSafeInteger(snapshot.revision) || snapshot.revision < 1) fail("invalid_revision");
  if (!STATES.has(snapshot.state)) fail("unsupported_state");
  if (snapshot.currency !== "JPY") fail("unsupported_currency");
  exact(snapshot.totals, TOTAL_KEYS, [...TOTAL_KEYS]);
  if (Object.values(snapshot.totals).some((value) => !safeAmount(value))) fail("invalid_amount");
  if (!Array.isArray(snapshot.sources) || snapshot.sources.length === 0) fail("missing_sources");
  snapshot.sources.forEach((source) => {
    exact(source, SOURCE_KEYS, [...SOURCE_KEYS]);
    [source.sourceId, source.label, source.asOf].forEach(label);
    if (!STATUSES.has(source.status) || !Object.prototype.hasOwnProperty.call(EVIDENCE, source.verificationStatus)) fail("invalid_source");
    if (!safeAmount(source.amountMinor)) fail("invalid_amount");
    if (source.amountMinor !== null && ["locally_estimated", "unavailable"].includes(source.verificationStatus)) fail("unconfirmed_amount");
    if (source.status === "unavailable" && (source.amountMinor !== null || source.verificationStatus !== "unavailable")) fail("inconsistent_source");
    if (source.status === "fresh" && (source.amountMinor === null || source.verificationStatus === "unavailable")) fail("inconsistent_source");
  });
  if (!Array.isArray(snapshot.excluded)) fail("invalid_excluded");
  validateBusiness(snapshot.business);
  snapshot.excluded.forEach((item) => { exact(item, EXCLUDED_KEYS, ["label"]); label(item.label); if (item.reason !== undefined) label(item.reason); });
  if (snapshot.repair !== null) {
    exact(snapshot.repair, REPAIR_KEYS, [...REPAIR_KEYS]);
    label(snapshot.repair.sourceLabel);
    if (typeof snapshot.repair.freshReread !== "boolean" || typeof snapshot.repair.reconciled !== "boolean") fail("invalid_repair");
  }
  if (snapshot.action !== null) {
    exact(snapshot.action, ACTION_KEYS, [...ACTION_KEYS]);
    if (!Object.prototype.hasOwnProperty.call(ACTION_LABELS, snapshot.action.kind) || snapshot.action.sourceLabel !== "Moneytree" || snapshot.action.retryLabel !== ACTION_LABELS[snapshot.action.kind] || !timestamp(snapshot.action.nextRetryAt)) fail("invalid_action");
  }
  const { state, totals, sources, excluded, repair, action } = snapshot;
  if (["complete", "partial", "recovered"].includes(state) && sources.some((source) => source.status !== "fresh")) fail("source_not_fresh");
  if (state === "recovered" && (!repair || repair.freshReread !== true || repair.reconciled !== true)) fail("recovery_unproven"); if (state === "complete" && [totals.assetsMinor, totals.liabilitiesMinor, totals.netWorthMinor, totals.changeMinor].some((value) => value === null)) fail("inconsistent_totals");
  if (state === "complete" && totals.assetsMinor - totals.liabilitiesMinor !== totals.netWorthMinor) fail("inconsistent_totals");
  if (state === "complete" && excluded.length) fail("inconsistent_state");
  if (["partial", "recovered"].includes(state) && (totals.netWorthMinor !== null || excluded.length === 0)) fail(totals.netWorthMinor === null ? "partial_excluded_required" : "partial_net_worth_forbidden");
  if (state === "action_required" && (totals.netWorthMinor !== null || action === null)) fail(totals.netWorthMinor === null ? "action_required_missing_action" : "action_required_net_worth_forbidden");
  if (state !== "recovered" && repair !== null) fail("inconsistent_repair");
  if (state !== "action_required" && action !== null) fail("inconsistent_action");
  return snapshot;
}
function evidenceLabel(locale, verificationStatus) {
  if (!CFO_STRINGS[locale]) fail("unsupported_locale");
  if (!EVIDENCE[verificationStatus]) fail("unsupported_evidence");
  return EVIDENCE[verificationStatus][locale];
}
function callbackData({ view, reportingDate, revision }) {
  if (!VIEWS.has(view) || !/^\d{4}-\d{2}-\d{2}$/.test(reportingDate) || !Number.isInteger(revision) || revision < 1) fail("callback");
  const value = `cfo:${view}:${reportingDate.replaceAll("-", "")}:${revision}`;
  if (Buffer.byteLength(value, "utf8") > 64) fail("callback_too_long");
  return value;
}
function validateBusiness(value) {
  if (value === undefined) return;
  exact(value, new Set(["schemaVersion", "observedAt", "status", "evidenceStatus", "businesses", "exceptions", "recommendation"]), ["schemaVersion", "observedAt", "status", "evidenceStatus", "businesses", "exceptions"]);
  if (value.schemaVersion !== 1 || !timestamp(value.observedAt) || value.status !== "partial" || !["partial", "unknown"].includes(value.evidenceStatus) || !Array.isArray(value.businesses) || !Array.isArray(value.exceptions)) fail("invalid_business");
  value.exceptions.forEach((item) => { if (typeof item !== "string" || item.length === 0 || item.length > 128) fail("invalid_business"); });
  if (value.recommendation !== undefined && (!plain(value.recommendation) || value.recommendation.schemaVersion !== 1 || !timestamp(value.recommendation.observedAt) || !["increase", "hold", "repair", "stop-review"].includes(value.recommendation.kind) || typeof value.recommendation.reason !== "string" || value.recommendation.execute !== false || typeof value.recommendation.ownerActionRequired !== "boolean" || !Array.isArray(value.recommendation.coverageExceptions))) fail("invalid_business");
  const ids = new Set();
  value.businesses.forEach((business) => {
    exact(business, new Set(["financialUnitId", "label", "providerReceiptStatus", "providerReceiptCount", "activity", "landedCashStatus", "costStatus", "contributionProfit", "roi"]), ["financialUnitId", "label", "providerReceiptStatus", "providerReceiptCount", "activity", "landedCashStatus", "costStatus", "contributionProfit", "roi"]);
    if (ids.has(business.financialUnitId) || typeof business.financialUnitId !== "string" || business.financialUnitId.length === 0 || typeof business.label !== "string" || business.label.length === 0 || !["observed", "unknown"].includes(business.providerReceiptStatus) || !Number.isSafeInteger(business.providerReceiptCount) || business.providerReceiptCount < 0 || !Array.isArray(business.activity) || business.landedCashStatus !== "unknown" || business.costStatus !== "unknown" || business.contributionProfit !== null || business.roi !== null) fail("invalid_business");
    ids.add(business.financialUnitId);
    business.activity.forEach((item) => { exact(item, new Set(["kind", "currency", "amountDecimal", "evidenceStatus"]), ["kind", "currency", "amountDecimal", "evidenceStatus"]); if (!["external_income", "realized_pnl", "fee"].includes(item.kind) || item.currency !== "USD" || !/^-?(?:0|[1-9]\d*)(?:\.\d{1,8})?$/.test(item.amountDecimal) || item.evidenceStatus !== "verified_append_only_ledger") fail("invalid_business"); });
  });
}
function parseCfoCallback(value) {
  const match = CALLBACK.exec(String(value || ""));
  if (!match) return null;
  const reportingDate = `${match[2].slice(0, 4)}-${match[2].slice(4, 6)}-${match[2].slice(6)}`;
  const revision = Number(match[3]);
  try { date(reportingDate); } catch { return null; }
  if (!Number.isSafeInteger(revision) || revision < 1 || String(revision) !== match[3]) return null;
  return { view: match[1], reportingDate, revision };
}
function nonEmpty(value) { return typeof value === "string" && value.length > 0 && value.trim() === value; }
// parseCfoCallback is retained for pure callback-data tests; HTTP handling belongs to the
// canonical cfo-telegram-callback.js module imported above.
function aiCostText(locale, value, detail = false) { if (!detail) return locale === "ja" ? `AI費用\nClaude $${value.amount} / 月（領収書確認済み）\nCodex 請求額未確認` : `AI costs\nClaude $${value.amount} / month (receipt confirmed)\nCodex amount not confirmed`; return locale === "ja" ? `AI費用\nClaude Max 20x\n支払 $${value.amount}\n期間 ${value.billingPeriodStart}〜${value.billingPeriodEnd}\n根拠 領収書確認済み\nCodex 請求額未確認\nAPI換算 まだ計算していません` : `AI costs\nClaude Max 20x\nPaid $${value.amount}\nPeriod ${value.billingPeriodStart}–${value.billingPeriodEnd}\nEvidence receipt confirmed\nCodex amount not confirmed\nAPI equivalent not calculated yet`; }
function businessText(locale, value) {
  if (value === undefined) return locale === "ja" ? "💼 仕事の収支\nまだbusiness readを接続していません" : "💼 Business P&L\nBusiness read is not connected yet";
  const unknown = locale === "ja" ? "不明" : "Unknown";
  const rows = value.businesses.map((business) => {
    const activity = business.activity.length ? business.activity.map((item) => `${item.kind}:${item.amountDecimal} USD`).join(" / ") : unknown;
    return `${escapeHtml(business.label)}\t${activity}\t${unknown}`;
  }).join("\n");
  const header = locale === "ja" ? "💼 今月の仕事（実測範囲）" : "💼 Businesses (measured scope)";
  const recommendation = value.recommendation ? `\nCFO判断：${value.recommendation.kind === "repair" ? "修復（証拠不足）" : value.recommendation.kind}` : "";
  const note = locale === "ja" ? `\n利益：${unknown}\nROI：${unknown}\n根拠：${value.evidenceStatus === "partial" ? "一部実測・照合未完了" : unknown}${recommendation}` : `\nProfit: ${unknown}\nROI: ${unknown}\nEvidence: ${value.evidenceStatus}${recommendation}`;
  return `${header}\n${rows}${note}`;
}
function extra(locale, snapshot, view) {
  return { reply_markup: { inline_keyboard: BUTTONS[view].filter(([next]) => next !== "ai_cost" || Object.prototype.hasOwnProperty.call(snapshot, "aiCost")).map(([next, ja, en]) => [{ text: locale === "ja" ? ja : en, callback_data: callbackData({ view: next, reportingDate: snapshot.reportingDate, revision: snapshot.revision }) }]) } };
}
function renderCfoTelegram({ locale, view, snapshot, transactions }) {
  validateAiCost(snapshot);
  if (!CFO_STRINGS[locale]) fail("unsupported_locale");
  if (!VIEWS.has(view)) fail("unsupported_view");
  snapshot = sanitize(snapshot);
  validateSnapshot(snapshot);
  if (transactions !== undefined) validateTransactions(transactions);
  const strings = CFO_STRINGS[locale];
  const hasAiCost = Object.prototype.hasOwnProperty.call(snapshot, "aiCost");
  if (view === "ai_cost") { if (!hasAiCost) fail("invalid_ai_cost"); return { text: aiCostText(locale, snapshot.aiCost, true), extra: extra(locale, snapshot, view) }; }
  if (view === "business") return { text: businessText(locale, snapshot.business), extra: extra(locale, snapshot, view) };
  const ai = hasAiCost ? aiCostText(locale, snapshot.aiCost) : "";
  const business = Object.prototype.hasOwnProperty.call(snapshot, "business") ? businessText(locale, snapshot.business) : "";
  if (snapshot.state === "action_required" && view === "summary") return { text: `${strings.actionTitle}\n\n${strings.actionBody}${ai ? `\n\n${ai}` : ""}\n${strings.actionRetry}`, extra: extra(locale, snapshot, view) };
  const freshness = locale === "ja"
    ? { fresh: "取得済み／銀行側データの新しさは不明", stale: "取得済み／データが古い可能性・銀行側データの新しさは不明", unavailable: "取得できず／銀行側データの新しさは不明" }
    : { fresh: "Retrieved; bank-side freshness unknown", stale: "Retrieved; may be stale; bank-side freshness unknown", unavailable: "Unavailable; bank-side freshness unknown" };
  const marks = locale === "ja" ? { colon: "：", open: "（", close: "）", join: "、" } : { colon: ": ", open: " (", close: ")", join: ", " };
  const safeLabel = (value) => escapeHtml(String(value).replace(/\d[\d -]{2,}\d/g, "••••"));
  // A successful plugin read proves retrieval, not bank-side freshness. Keep the
  // warning visible even when the internal source status is `fresh` so the report
  // cannot be mistaken for a realtime bank balance.
  const sourceText = snapshot.sources.map((source) => `⚠️ Moneytree${locale === "ja" ? "（" : " ("}${safeLabel(source.label)}${locale === "ja" ? "）" : ")"} ${escapeHtml(source.asOf)}${strings.updated}`).join("\n");
  const totals = `${strings.confirmedAssets}\t${formatAmount(locale, snapshot.totals.assetsMinor)}\n${strings.confirmedLiabilities}\t${formatAmount(locale, snapshot.totals.liabilitiesMinor)}\n${strings.confirmedDifference}\t${formatAmount(locale, snapshot.totals.netWorthMinor)}\n${strings.change}\t${formatChange(locale, snapshot.totals.changeMinor)}`;
  const title = ["partial", "recovered"].includes(snapshot.state) ? strings.partialTitle : strings.title;
  const excludedItems = [...snapshot.excluded, ...snapshot.sources.filter((source) => source.status !== "fresh").map((source) => ({ label: source.label }))];
  const excluded = [...new Map(excludedItems.map((item) => [item.label, item])).values()].map((item) => `${safeLabel(item.label)}${item.reason ? `${marks.open}${escapeHtml(item.reason)}${marks.close}` : ""}`).join(marks.join) || (locale === "ja" ? "なし" : "None");
  const exclusions = ["partial", "recovered"].includes(snapshot.state) ? `\n${strings.excluded}${marks.colon}${excluded}` : "";
  const repair = snapshot.state === "recovered" ? `\n${strings.recovered}` : "";
  const accounts = snapshot.sources.map((source) => `${safeLabel(source.label)}\t${formatAmount(locale, source.amountMinor)}${marks.open}${freshness[source.status]}${marks.close}`).join("\n");
  const evidence = snapshot.sources.map((source) => `${evidenceLabel(locale, source.verificationStatus)} ${escapeHtml(source.asOf)}${strings.updated}`).join("\n");
  const why = `${strings.confirmedAssets} − ${strings.confirmedLiabilities} = ${strings.confirmedDifference} ${formatAmount(locale, snapshot.totals.netWorthMinor)}\n${strings.excluded}${marks.colon}${excluded}`;
  const activity = view === "summary" && transactions !== undefined ? transactionText(locale, transactions, snapshot.reportingDate) : "";
  const text = view === "summary" ? `${title}\n\n${totals}${exclusions}\n\n${sourceText}${repair}${ai ? `\n${ai}` : ""}${business ? `\n\n${business}` : ""}\n${strings.noAction}${activity}`
    : view === "accounts" ? `${title}\n\n${accounts}\n${snapshot.state === "action_required" ? strings.actionBody : strings.noAction}`
      : view === "accuracy" ? `${title}\n\n${evidence}\n${strings.excluded}${marks.colon}${excluded}` : `${title}\n\n${why}`;
  return { text, extra: extra(locale, snapshot, view) };
}

module.exports = { renderCfoTelegram, callbackData, evidenceLabel, handleCfoTelegramCallback };
