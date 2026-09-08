"use strict";

const { createHash } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const PROVIDER = /^[a-z][a-z0-9_-]{1,31}$/;

function invalid() { throw new Error("Connector reconciliation store invalid"); }
function text(value, max) {
  const result = String(value == null ? "" : value).trim();
  if (!result || result.length > max || /[\x00-\x1f\x7f]/.test(result)) invalid();
  return result;
}
function instant(value) {
  const result = text(value, 40);
  if (!Number.isFinite(Date.parse(result)) || new Date(Date.parse(result)).toISOString() !== result) invalid();
  return result;
}
function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function normalize(candidate, observedAt) {
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) invalid();
  const provider = text(candidate.provider, 32);
  if (!PROVIDER.test(provider)) invalid();
  const canonicalUrl = text(candidate.canonical_url, 2_000);
  if (!canonicalUrl.startsWith("https://")) invalid();
  const startsAt = instant(candidate.starts_at);
  const endsAt = instant(candidate.ends_at);
  if (Date.parse(endsAt) <= Date.parse(startsAt)) invalid();
  const core = {
    provider,
    event_ref: text(candidate.event_ref, 500),
    canonical_url: canonicalUrl,
    title: text(candidate.title, 500),
    starts_at: startsAt,
    ends_at: endsAt,
    venue_name: text(candidate.venue_address || candidate.address || candidate.venue || candidate.venue_name || "See event page", 2_000),
    observed_at: instant(observedAt),
  };
  return Object.freeze({ reconciliation_id: createHash("sha256").update(stable(core)).digest("hex"), ...core });
}

function createConnectorReconciliationStore(options = {}) {
  const file = path.resolve(String(options.path || ""));
  if (!path.isAbsolute(file) || file === path.parse(file).root) invalid();
  function read() {
    let source;
    try {
      const stat = fs.statSync(file);
      if (stat.size > 1_000_000) invalid();
      source = fs.readFileSync(file, "utf8");
    } catch (error) {
      if (error && error.code === "ENOENT") return [];
      throw error;
    }
    let document;
    try { document = JSON.parse(source); } catch { invalid(); }
    if (!document || document.schema_version !== 1 || !Array.isArray(document.entries) || document.entries.length > 100) invalid();
    return document.entries.map((entry) => {
      const normalized = normalize(entry, entry.observed_at);
      if (entry.reconciliation_id !== normalized.reconciliation_id) invalid();
      return normalized;
    });
  }
  function write(entries) {
    const parent = path.dirname(file);
    fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
    const temporary = path.join(parent, `.${path.basename(file)}.${process.pid}.tmp`);
    fs.writeFileSync(temporary, `${JSON.stringify({ schema_version: 1, entries })}\n`, { mode: 0o600 });
    fs.renameSync(temporary, file);
    fs.chmodSync(file, 0o600);
  }
  return Object.freeze({
    list(provider) {
      const expected = text(provider, 32);
      if (!PROVIDER.test(expected)) invalid();
      return Object.freeze(read().filter((entry) => entry.provider === expected));
    },
    save(candidate, observedAt) {
      const entry = normalize(candidate, observedAt);
      const retained = read().filter((item) => !(item.provider === entry.provider && item.event_ref === entry.event_ref));
      if (retained.length >= 100) invalid();
      retained.push(entry);
      retained.sort((a, b) => a.observed_at.localeCompare(b.observed_at));
      write(retained);
      return entry;
    },
    remove(provider, eventRef) {
      const expectedProvider = text(provider, 32);
      const expectedRef = text(eventRef, 500);
      const entries = read();
      const retained = entries.filter((entry) => !(entry.provider === expectedProvider && entry.event_ref === expectedRef));
      if (retained.length !== entries.length) write(retained);
      return retained.length !== entries.length;
    },
  });
}

module.exports = { createConnectorReconciliationStore };
