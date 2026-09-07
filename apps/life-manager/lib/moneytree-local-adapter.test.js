"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  accountToFinancialRecord,
  normalizeAccounts,
  normalizeTransactions,
  transactionToFinancialRecord,
} = require("./moneytree-local-adapter.js");

const observedAt = "2026-09-07T06:00:00.000Z";

test("Moneytree balances and transactions project to distinct personal FinancialRecords", () => {
  const [account] = normalizeAccounts({ structuredContent: { data: {
    baseCurrency: "JPY",
    accountGroups: { banks: [{ institutionKey: "bank", accounts: [{ id: "a1", current_balance: -5000 }] }] },
  } } }, observedAt);
  const [expense, transfer] = normalizeTransactions({ structuredContent: { data: { transactions: [
    { id: "t1", account_id: "a1", amount: -1200, date: "2026-09-06", description: "Shop", category_name: "Food" },
    { id: "t2", account_id: "a1", amount: 3000, date: "2026-09-06", description: "Transfer", category_name: "振替" },
  ] } } });
  const options = { subjectId: "user-1", recordedAt: observedAt };

  assert.deepEqual(
    { kind: accountToFinancialRecord(account, options).kind, amount: accountToFinancialRecord(account, options).amount_minor },
    { kind: "liability_balance", amount: 5000 },
  );
  assert.deepEqual(
    { kind: transactionToFinancialRecord(expense, options).kind, direction: transactionToFinancialRecord(expense, options).direction },
    { kind: "personal_expense", direction: "debit" },
  );
  assert.deepEqual(
    { kind: transactionToFinancialRecord(transfer, options).kind, direction: transactionToFinancialRecord(transfer, options).direction },
    { kind: "transfer", direction: "credit" },
  );
  for (const record of [accountToFinancialRecord(account, options), transactionToFinancialRecord(expense, options)]) {
    assert.equal(record.scope, "personal");
    assert.equal(record.source.source_type, "moneytree");
    assert.equal(record.verification.status, "unverified");
    assert.deepEqual(record.verification.evidence_refs, []);
  }
  assert.deepEqual(accountToFinancialRecord(account, options), accountToFinancialRecord(account, options));
  const otherSubject = accountToFinancialRecord(account, { ...options, subjectId: "user-2" });
  assert.notEqual(otherSubject.record_id, accountToFinancialRecord(account, options).record_id);
  assert.notEqual(otherSubject.source.external_ref, accountToFinancialRecord(account, options).source.external_ref);
  assert.notEqual(
    accountToFinancialRecord({ ...account, balance_jpy: -5001 }, options).record_id,
    accountToFinancialRecord(account, options).record_id,
  );
  assert.notEqual(
    accountToFinancialRecord({ ...account, observed_at: "2026-09-07T07:00:00.000Z" }, {
      ...options, recordedAt: "2026-09-07T07:00:00.000Z",
    }).record_id,
    accountToFinancialRecord(account, options).record_id,
  );
});

test("Moneytree FinancialRecord projection fails closed without portable identity and observation", () => {
  assert.throws(() => accountToFinancialRecord({
    id: "bad id", source: "moneytree", source_ref: `moneytree:${"a".repeat(64)}`,
    name: "Account", kind: "bank", balance_jpy: 1, observed_at: observedAt,
  }, { subjectId: "user-1", recordedAt: observedAt }), /common ID/);
  assert.throws(() => transactionToFinancialRecord({
    id: "moneytree:t1", source_ref: `moneytree:${"b".repeat(64)}`,
    account_id: "moneytree:a1", amount_jpy: 1, occurred_at: "bad",
  }, { subjectId: "user-1", recordedAt: observedAt }), /transaction time is invalid/);
  assert.throws(() => transactionToFinancialRecord({
    id: "moneytree:t1", source_ref: {}, account_id: "moneytree:a1",
    amount_jpy: 1, occurred_at: observedAt,
  }, { subjectId: "user-1", recordedAt: observedAt }), /source_ref is invalid/);
});
