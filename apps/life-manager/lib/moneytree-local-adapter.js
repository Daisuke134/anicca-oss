"use strict";

const { createHash } = require("node:crypto");
const { spawn } = require("node:child_process");
const { validateFinancialRecord } = require("./financial-organ-schema.js");
const { financialRecordId } = require("../../../runtime/contracts/common-record.cjs");
const { canonicalJson, sha256 } = require("./moneytree-observation-store.js");

const COMMON_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MONEYTREE_OBSERVATION = Symbol.for("life-manager.moneytree.observation");

function commonId(value, label) {
  if (typeof value !== "string" || !COMMON_ID.test(value)) throw new Error(`${label} is not a common ID`);
  return value;
}

function instant(value, label) {
  const text = String(value || "");
  const parsed = /^\d{4}-\d{2}-\d{2}$/.test(text) ? new Date(`${text}T00:00:00.000Z`) : new Date(text);
  if (!Number.isFinite(parsed.getTime())) throw new Error(`${label} is invalid`);
  return parsed.toISOString();
}

function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

function sourceIdentity(value) {
  if (typeof value !== "string" || !/^moneytree:[a-f0-9]{64}$/.test(value)) {
    throw new Error("Moneytree source_ref is invalid");
  }
  return value;
}

function commonBase(record, {
  subjectId, recordedAt, evidenceRef = null, evidenceObservedAt = null,
}) {
  const observedAt = instant(record.observed_at || recordedAt, "Moneytree observed time");
  const subject = commonId(subjectId, "Moneytree subject id");
  const sourceRef = sourceIdentity(record.source_ref);
  const scopedSource = `moneytree:${hash(`${subject}\n${sourceRef}`)}`;
  return {
    schema_version: 1,
    record_type: "financial_record",
    record_id: `moneytree:${hash(`${subject}\n${commonId(record.id, "Moneytree record id")}`).slice(0, 24)}`,
    subject_id: subject,
    scope: "personal",
    currency: "JPY",
    recorded_at: instant(recordedAt || observedAt, "Moneytree recorded time"),
    idempotency_key: scopedSource,
    source: { provider: "moneytree", source_type: "moneytree", external_ref: scopedSource },
    verification: evidenceRef
      ? {
        status: "verified",
        observed_at: instant(evidenceObservedAt, "Moneytree evidence observation time"),
        evidence_refs: [evidenceRef],
      }
      : { status: "unverified", observed_at: observedAt, evidence_refs: [] },
  };
}

function accountToFinancialRecord(account, options) {
  validateFinancialRecord("account", account);
  const liability = account.balance_jpy < 0;
  const observedAt = instant(account.observed_at, "Moneytree account time");
  const base = commonBase(account, options);
  const snapshotHash = hash(`${base.subject_id}\n${base.source.external_ref}\n${account.kind}\n${account.balance_jpy}\n${observedAt}`);
  const idempotencyKey = `moneytree-account:${snapshotHash}`;
  return {
    ...base,
    record_id: financialRecordId(base.subject_id, idempotencyKey),
    kind: liability ? "liability_balance" : "asset_balance",
    direction: "snapshot",
    amount_minor: Math.abs(account.balance_jpy),
    occurred_at: observedAt,
    idempotency_key: idempotencyKey,
  };
}

function transactionToFinancialRecord(transaction, options) {
  validateFinancialRecord("transaction", transaction);
  const stableObservedAt = instant(transaction.occurred_at, "Moneytree transaction time");
  const base = commonBase(
    { ...transaction, observed_at: stableObservedAt },
    { ...options, recordedAt: stableObservedAt },
  );
  const transfer = Boolean(transaction.transfer_id);
  const idempotencyKey = base.idempotency_key;
  return {
    ...base,
    record_id: financialRecordId(base.subject_id, idempotencyKey),
    kind: transfer ? "transfer" : transaction.amount_jpy >= 0 ? "personal_income" : "personal_expense",
    direction: transaction.amount_jpy >= 0 ? "credit" : "debit",
    amount_minor: Math.abs(transaction.amount_jpy),
    occurred_at: instant(transaction.occurred_at, "Moneytree transaction time"),
    idempotency_key: idempotencyKey,
  };
}

function normalizeAccounts(toolResult, observedAt) {
  const data = toolResult?.structuredContent?.data;
  if (!data || data.baseCurrency !== "JPY") throw new Error("Moneytree JPY account data is unavailable");
  observedAt = instant(observedAt, "Moneytree account observation");
  const groups = [...(data.accountGroups?.banks || []), ...(data.accountGroups?.investments || [])];
  return groups.flatMap((group) => (group.accounts || []).map((account) => {
    const balance = account.current_balance_in_base ?? account.current_balance;
    const key = `${group.institutionKey}:${account.id}`;
    return validateFinancialRecord("account", {
      id: `moneytree:${createHash("sha256").update(key).digest("hex").slice(0, 24)}`,
      source: "moneytree",
      source_ref: `moneytree:${createHash("sha256").update(`source:${key}`).digest("hex")}`,
      name: "Moneytree account",
      kind: account.account_subtype || "account",
      balance_jpy: balance,
      observed_at: observedAt,
    });
  }));
}

function normalizeTransactions(toolResult) {
  const rows = toolResult?.structuredContent?.data?.transactions;
  if (!Array.isArray(rows)) throw new Error("Moneytree transaction data is unavailable");
  return rows.map((row) => {
    const key = `${row.account_id}:${row.id}`;
    const transaction = {
      id: `moneytree:${createHash("sha256").update(key).digest("hex").slice(0, 24)}`,
      source_ref: `moneytree:${createHash("sha256").update(`source:${key}`).digest("hex")}`,
      account_id: `moneytree:${createHash("sha256").update(String(row.account_id)).digest("hex").slice(0, 24)}`,
      amount_jpy: row.amount_in_base ?? row.amount,
      occurred_at: row.date,
      merchant: row.description,
      category: row.category_name || "未分類",
    };
    if (row.category_name === "振替") transaction.transfer_id = `moneytree:${row.id}`;
    return validateFinancialRecord("transaction", transaction);
  });
}

function callTool(tool, args, { codexBin = "codex", cwd = process.cwd(), timeoutMs = 20_000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(codexBin, ["app-server", "--stdio"], { cwd, stdio: ["pipe", "pipe", "ignore"] });
    let buffer = "";
    const finish = (error, value) => {
      clearTimeout(timer);
      child.kill();
      error ? reject(error) : resolve(value);
    };
    const timer = setTimeout(() => finish(new Error("Moneytree app-server timeout")), timeoutMs);
    const send = (message) => child.stdin.write(`${JSON.stringify(message)}\n`);

    child.on("error", (error) => finish(error));
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let message;
        try { message = JSON.parse(line); } catch { continue; }
        if (message.error) return finish(new Error(message.error.message || "Moneytree app-server error"));
        if (message.id === 1) {
          send({ method: "initialized", params: {} });
          send({ id: 2, method: "thread/start", params: { ephemeral: true, cwd } });
        } else if (message.id === 2) {
          send({
            id: 3,
            method: "mcpServer/tool/call",
            params: {
              threadId: message.result.thread.id,
              server: "codex_apps",
              tool,
              arguments: args,
            },
          });
        } else if (message.id === 3) {
          const result = message.result;
          if (result?.isError) return finish(new Error("Moneytree tool returned an error"));
          return finish(null, result);
        }
      }
    });
    send({
      id: 1,
      method: "initialize",
      params: { clientInfo: { name: "life_manager", title: "Life Manager", version: "0.1.0" } },
    });
  });
}

function readAccounts(options = {}) {
  return callTool("moneytree.show-accounts", { locale: "ja" }, options)
    .then((result) => {
      const retrievedAt = new Date().toISOString();
      const records = normalizeAccounts(result, retrievedAt);
      Object.defineProperty(records, MONEYTREE_OBSERVATION, {
        enumerable: false, value: Object.freeze({
          provider: "moneytree", mcp_server: "codex_apps", tool: "moneytree.show-accounts",
          retrieved_at: retrievedAt,
          payload_sha256: sha256(canonicalJson(result.structuredContent?.data)),
        }),
      });
      return records;
    });
}

function readTransactions({ startDate, endDate, limit = 1000, ...options }) {
  return callTool("moneytree.show-transactions", {
    locale: "ja", start_date: startDate, end_date: endDate, limit, sort_key: "date", sort_order: "desc",
  }, options).then((result) => {
    const records = normalizeTransactions(result);
    Object.defineProperty(records, MONEYTREE_OBSERVATION, {
      enumerable: false, value: Object.freeze({
        provider: "moneytree", mcp_server: "codex_apps", tool: "moneytree.show-transactions",
        retrieved_at: new Date().toISOString(),
        payload_sha256: sha256(canonicalJson(result.structuredContent?.data)),
      }),
    });
    return records;
  });
}

if (require.main === module) {
  readAccounts().then((records) => {
    process.stdout.write(`${JSON.stringify({ connected: true, accounts: records.length })}\n`);
  }).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  MONEYTREE_OBSERVATION,
  accountToFinancialRecord,
  normalizeAccounts,
  normalizeTransactions,
  readAccounts,
  readTransactions,
  transactionToFinancialRecord,
};
