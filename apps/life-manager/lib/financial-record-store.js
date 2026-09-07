"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const { isDeepStrictEqual } = require("node:util");
const { projectFinancialRecord } = require("../../../runtime/contracts/common-record.cjs");

function instant(value, label) {
  if (value == null) return null;
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) {
    throw new Error(`${label} invalid`);
  }
  return new Date(value).toISOString();
}

function readInput(input = {}) {
  const subjectId = String(input.subjectId || "").trim();
  if (!subjectId) throw new Error("FinancialRecord subjectId invalid");
  const scope = input.scope == null ? null : String(input.scope);
  if (scope !== null && !["personal", "business"].includes(scope)) {
    throw new Error("FinancialRecord scope invalid");
  }
  return { subjectId, scope, since: instant(input.since, "FinancialRecord since") };
}

function select(records, input) {
  return records.filter((record) => (
    record.subject_id === input.subjectId
    && (input.scope === null || record.scope === input.scope)
    && (input.since === null || record.occurred_at >= input.since)
  )).sort((left, right) => (
    left.occurred_at.localeCompare(right.occurred_at)
    || left.record_id.localeCompare(right.record_id)
  ));
}

function createJsonlFinancialRecordStore({ directoryPath } = {}) {
  if (typeof directoryPath !== "string" || !path.isAbsolute(directoryPath)) {
    throw new Error("FinancialRecord JSONL directory must be absolute");
  }
  function all() {
    let names;
    try { names = fs.readdirSync(directoryPath).filter((name) => name.endsWith(".jsonl")).sort(); }
    catch (error) {
      if (error && error.code === "ENOENT") return [];
      throw error;
    }
    return names.map((name) => {
      const text = fs.readFileSync(path.join(directoryPath, name), "utf8");
      if (!text.endsWith("\n") || text.indexOf("\n") !== text.length - 1) {
        throw new Error("FinancialRecord JSONL shard invalid");
      }
      return projectFinancialRecord(JSON.parse(text.slice(0, -1)));
    });
  }
  return Object.freeze({
    async append(value) {
      const record = projectFinancialRecord(value);
      fs.mkdirSync(directoryPath, { recursive: true, mode: 0o700 });
      fs.chmodSync(directoryPath, 0o700);
      const records = all();
      const existing = records.find((item) => (
        item.subject_id === record.subject_id
        && (item.idempotency_key === record.idempotency_key
          || item.record_id === record.record_id)
      ));
      if (existing) {
        if (!isDeepStrictEqual(existing, record)) {
          throw new Error("FinancialRecord idempotency collision");
        }
        return { created: false, record: existing };
      }
      const digest = crypto.createHash("sha256")
        .update(`${record.subject_id}\0${record.idempotency_key}`)
        .digest("hex");
      const target = path.join(directoryPath, `${digest}.jsonl`);
      const temporary = path.join(directoryPath, `.${digest}.${crypto.randomUUID()}.tmp`);
      let descriptor;
      try {
        descriptor = fs.openSync(temporary, "wx", 0o600);
        fs.writeFileSync(descriptor, `${JSON.stringify(record)}\n`);
        fs.fsyncSync(descriptor);
        fs.closeSync(descriptor);
        descriptor = undefined;
        try { fs.linkSync(temporary, target); }
        catch (error) {
          if (!error || error.code !== "EEXIST") throw error;
          const stored = projectFinancialRecord(
            JSON.parse(fs.readFileSync(target, "utf8").trimEnd()),
          );
          if (!isDeepStrictEqual(stored, record)) {
            throw new Error("FinancialRecord idempotency collision");
          }
          return { created: false, record: stored };
        }
        return { created: true, record };
      } finally {
        if (descriptor !== undefined) fs.closeSync(descriptor);
        try { fs.unlinkSync(temporary); } catch (error) {
          if (!error || error.code !== "ENOENT") throw error;
        }
      }
    },
    async read(input) { return select(all(), readInput(input)); },
  });
}

function createPostgresFinancialRecordStore({ query } = {}) {
  if (typeof query !== "function") throw new Error("FinancialRecord Postgres query required");
  return Object.freeze({
    async append(value) {
      const record = projectFinancialRecord(value);
      const inserted = (await query(`
        INSERT INTO public.lm_financial_records
          (record_id, subject_id, idempotency_key, occurred_at, record)
        VALUES ($1, $2, $3, $4::timestamptz, $5::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING record
      `, [
        record.record_id, record.subject_id, record.idempotency_key,
        record.occurred_at, JSON.stringify(record),
      ])).rows;
      if (inserted.length > 1) throw new Error("FinancialRecord append failed");
      const rows = inserted.length ? inserted : (await query(`
        SELECT record FROM public.lm_financial_records
        WHERE subject_id = $1
          AND (record_id = $2 OR idempotency_key = $3)
        LIMIT 2
      `, [record.subject_id, record.record_id, record.idempotency_key])).rows;
      if (rows.length !== 1) throw new Error("FinancialRecord append collision");
      const stored = projectFinancialRecord(rows[0].record);
      if (!isDeepStrictEqual(stored, record)) throw new Error("FinancialRecord idempotency collision");
      return { created: inserted.length === 1, record: stored };
    },
    async read(raw) {
      const input = readInput(raw);
      const rows = (await query(`
        SELECT record FROM public.lm_financial_records
        WHERE subject_id = $1
          AND ($2::text IS NULL OR record->>'scope' = $2)
          AND ($3::timestamptz IS NULL OR occurred_at >= $3::timestamptz)
        ORDER BY occurred_at, record_id
      `, [input.subjectId, input.scope, input.since])).rows;
      return rows.map((row) => projectFinancialRecord(row.record));
    },
  });
}

module.exports = { createJsonlFinancialRecordStore, createPostgresFinancialRecordStore };
