// Append-only JSONL colony ledger. The only file in this skill that touches the filesystem.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const CITIZEN_LIFECYCLE_SCHEMA = "life-manager.citizen-lifecycle-receipt.v1";

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item) ?? "null").join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.keys(value).sort().flatMap((key) => {
      const encoded = canonicalJson(value[key]);
      return encoded === undefined ? [] : [`${JSON.stringify(key)}:${encoded}`];
    });
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

function readChildren(file) {
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (e) {
    if (e && e.code === "ENOENT") return [];
    throw e;
  }
  return raw
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0)
    .map((l) => JSON.parse(l));
}

function appendChild(file, row) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, JSON.stringify(row) + "\n");
  return row;
}

function appendCitizenLifecycleReceipt(file, row) {
  if (!row || typeof row !== "object") {
    throw new TypeError("citizen lifecycle receipt requires child_id, status, and attempted_ms or active_since");
  }
  const occurredAtMs = row.active_since ?? row.attempted_ms;
  if (typeof row.child_id !== "string" || !row.child_id || typeof row.status !== "string" || !row.status || !Number.isFinite(occurredAtMs)) {
    throw new TypeError("citizen lifecycle receipt requires child_id, status, and attempted_ms or active_since");
  }
  const payload = { ...row };
  delete payload.receipt_id;
  const event = {
    ...payload,
    schema: CITIZEN_LIFECYCLE_SCHEMA,
    event_type: "citizen.lifecycle",
    citizen_id: row.child_id,
    lifecycle_status: row.status,
    occurred_at_ms: occurredAtMs,
  };
  const receipt = {
    ...event,
    receipt_id: crypto.createHash("sha256").update(canonicalJson(event)).digest("hex"),
  };
  return appendChild(file, receipt);
}

module.exports = { readChildren, appendChild, appendCitizenLifecycleReceipt };
