"use strict";

const { createHmac } = require("node:crypto");
const { validateFinancialSourceResult } = require("./cfo-financial-source.js");
const ERROR_PREFIX = "moneytree_adapter_invalid:";
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/;
const REF_PREFIXES = { account: "source_account:mt_", transaction: "transaction:mt_", evidence: "evidence:mt_" };

function plain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}
function fail(reason) {
  throw new Error(`${ERROR_PREFIX}${reason}`);
}

function parseJson(value, expectedType) {
  if (typeof value !== "string") fail("invalid_json");
  let parsed;
  try { parsed = JSON.parse(value); } catch { fail("invalid_json"); }
  if (!plain(parsed) || !Object.hasOwn(parsed, "type") || !Object.hasOwn(parsed, "data")) fail("invalid_root");
  if (parsed.type !== expectedType) fail("wrong_type");
  if (!plain(parsed.data)) fail("invalid_data");
  return parsed;
}

function keyBytes(referenceKey) {
  if (typeof referenceKey !== "string" || Buffer.byteLength(referenceKey, "utf8") < 32) fail("weak_reference_key");
}

function digest(referenceKey, message) {
  return createHmac("sha256", referenceKey).update(message).digest("hex").slice(0, 24);
}

function opaqueRef(kind, referenceKey, providerId) {
  keyBytes(referenceKey);
  if (!Number.isSafeInteger(providerId)) fail("invalid_provider_id");
  if (!REF_PREFIXES[kind]) fail("invalid_ref_kind");
  return `${REF_PREFIXES[kind]}${digest(referenceKey, `moneytree:${kind}:${providerId}`)}`;
}
function evidenceRef(referenceKey, observedAt, parsed) {
  keyBytes(referenceKey);
  return `evidence:mt_${digest(referenceKey, `moneytree:evidence:${observedAt}:${JSON.stringify(parsed)}`)}`;
}

function adaptMoneytreeAccounts(input) {
  try {
    if (!plain(input)) fail("invalid_input");
    const { accountsJson, observedAt, referenceKey } = input;
    if (typeof observedAt !== "string" || !RFC3339.test(observedAt)) fail("invalid_observed_at");
    const parsed = parseJson(accountsJson, "accounts");
    if (parsed.data.baseCurrency !== "JPY") fail("unsupported_base_currency");
    const groups = parsed.data.accountGroups;
    if (!plain(groups) || !Array.isArray(groups.banks)) fail("invalid_account_groups");
    const selected = [];
    for (const group of groups.banks) {
      if (!plain(group)) fail("invalid_bank_group");
      if (group.institutionKey !== "mufg_bank") continue;
      if (!Array.isArray(group.accounts)) fail("invalid_accounts");
      selected.push(...group.accounts);
    }
    if (selected.length === 0) fail("no_mufg_accounts");
    const providerIds = new Set();
    const accounts = selected.map((account) => {
      if (!plain(account)) fail("invalid_account");
      if (!Number.isSafeInteger(account.id)) fail("invalid_provider_id");
      if (providerIds.has(account.id)) fail("duplicate_provider_id");
      providerIds.add(account.id);
      if (account.currency !== "JPY") fail("unsupported_account_currency");
      if (!Number.isSafeInteger(account.current_balance)) fail("invalid_balance");
      const savings = account.account_subtype === "savings";
      return {
        accountRef: opaqueRef("account", referenceKey, account.id),
        label: savings ? "MUFG 普通預金" : "MUFG 口座",
        kind: savings ? "deposit" : "other",
        currency: "JPY",
        balanceMinor: account.current_balance,
        verificationStatus: "provider_reported",
      };
    });

    return validateFinancialSourceResult({
      schemaVersion: 1,
      sourceId: "moneytree_mufg",
      consent: "valid",
      freshness: "fresh",
      asOf: observedAt,
      accounts,
      liabilities: [],
      evidenceRef: evidenceRef(referenceKey, observedAt, parsed),
      partial: true,
      actionRequired: null,
    });
  } catch (error) {
    if (error && typeof error.message === "string" && error.message.startsWith(ERROR_PREFIX)) throw error;
    throw new Error(`${ERROR_PREFIX}invalid_payload`);
  }
}

function bookingDate(value) {
  const match = typeof value === "string" && RFC3339.exec(value);
  if (!match) fail("invalid_booking_date");
  const monthEnd = new Date(0);
  monthEnd.setUTCFullYear(Number(match[1]), Number(match[2]), 0);
  if (Number(match[2]) < 1 || Number(match[2]) > 12 || Number(match[3]) < 1 || Number(match[3]) > monthEnd.getUTCDate() || Number(match[4]) > 23 || Number(match[5]) > 59 || Number(match[6]) > 59 || (match[7] !== "Z" && (Number(match[9]) > 23 || Number(match[10]) > 59)) || !Number.isFinite(Date.parse(value))) fail("invalid_booking_date");
  return `${match[1]}-${match[2]}-${match[3]}`;
}

function transactionAccountRefs(parsed, referenceKey) {
  const refs = new Map();
  for (const group of parsed.data.accountGroups.banks) {
    if (group.institutionKey !== "mufg_bank") continue;
    for (const account of group.accounts) {
      if (refs.has(account.id)) fail("duplicate_provider_id");
      refs.set(account.id, opaqueRef("account", referenceKey, account.id));
    }
  }
  return refs;
}
function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value);
  Object.values(value).forEach((child) => deepFreeze(child, seen));
  return Object.freeze(value);
}

function adaptMoneytreeTransactions(input) {
  try {
    if (!plain(input)) fail("invalid_input");
    const { accountsJson, transactionsJson, observedAt, referenceKey } = input;
    const accountInput = { accountsJson, observedAt, referenceKey };
    adaptMoneytreeAccounts(accountInput);
    const accounts = parseJson(accountsJson, "accounts");
    const accountRefs = transactionAccountRefs(accounts, referenceKey);
    const parsed = parseJson(transactionsJson, "transactions");
    const rows = parsed.data.transactions;
    if (!Array.isArray(rows)) fail("invalid_transactions");
    const totalCount = parsed.data.totalCount;
    if (!Number.isSafeInteger(totalCount) || totalCount < rows.length) fail("invalid_total_count");
    const transactionRefs = new Set();
    const transactions = rows.map((row) => {
      if (!plain(row) || !Number.isSafeInteger(row.id)) fail("invalid_transaction_id");
      if (!Number.isSafeInteger(row.account_id) || !accountRefs.has(row.account_id)) fail("invalid_account_id");
      if (!Number.isSafeInteger(row.amount)) fail("invalid_amount");
      if (row.currency !== "JPY") fail("unsupported_currency");
      const transactionRef = opaqueRef("transaction", referenceKey, row.id);
      if (transactionRefs.has(transactionRef)) fail("duplicate_transaction_ref");
      transactionRefs.add(transactionRef);
      return {
        transactionRef, accountRef: accountRefs.get(row.account_id), bookingDate: bookingDate(row.date),
        amountMinor: row.amount, currency: "JPY", flow: row.amount > 0 ? "inflow" : row.amount < 0 ? "outflow" : "neutral",
        verificationStatus: "provider_reported",
      };
    });
    const result = {
      schemaVersion: 1, sourceId: "moneytree_mufg", asOf: observedAt, transactions,
      evidenceRef: evidenceRef(referenceKey, observedAt, { accounts, transactions: parsed }), pagePartial: totalCount > rows.length,
    };
    return deepFreeze(structuredClone(result));
  } catch (error) {
    if (error && typeof error.message === "string" && error.message.startsWith(ERROR_PREFIX)) throw error;
    throw new Error(`${ERROR_PREFIX}invalid_payload`);
  }
}

module.exports = { adaptMoneytreeAccounts, adaptMoneytreeTransactions };
