"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SHA256 = /^[a-f0-9]{64}$/;
const TOOLS = new Set(["moneytree.show-accounts", "moneytree.show-transactions"]);

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function exactInstant(value, label) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) {
    throw new Error(`${label} invalid`);
  }
  return new Date(value).toISOString();
}

function projectRead(value, expectedTool) {
  if (!value || value.provider !== "moneytree" || value.mcp_server !== "codex_apps"
    || value.tool !== expectedTool || !TOOLS.has(value.tool) || !SHA256.test(value.payload_sha256)) {
    throw new Error("Moneytree observation provenance invalid");
  }
  return {
    provider: "moneytree",
    mcp_server: "codex_apps",
    tool: value.tool,
    retrieved_at: exactInstant(value.retrieved_at, "Moneytree retrieval time"),
    payload_sha256: value.payload_sha256,
  };
}

function buildMoneytreeObservation({ accounts, transactions, accountRead, transactionRead, observedAt }) {
  const core = {
    schema_version: 1,
    evidence_type: "authenticated_connector_observation",
    provider: "moneytree",
    observed_at: exactInstant(observedAt, "Moneytree observation time"),
    reads: [
      projectRead(accountRead, "moneytree.show-accounts"),
      projectRead(transactionRead, "moneytree.show-transactions"),
    ],
    account_count: accounts.length,
    transaction_count: transactions.length,
    normalized_payload_sha256: sha256(canonicalJson({ accounts, transactions })),
  };
  const digest = sha256(canonicalJson(core));
  return Object.freeze({
    evidence_ref: `moneytree-observation://sha256/${digest}`,
    document: Object.freeze({ evidence_id: `moneytree-observation:${digest}`, ...core }),
  });
}

function createMoneytreeObservationStore({ directoryPath } = {}) {
  if (typeof directoryPath !== "string" || !path.isAbsolute(directoryPath)) {
    throw new Error("Moneytree observation directory must be absolute");
  }
  return Object.freeze({
    record(observation) {
      const match = /^moneytree-observation:\/\/sha256\/([a-f0-9]{64})$/.exec(observation?.evidence_ref || "");
      if (!match || observation.document?.evidence_id !== `moneytree-observation:${match[1]}`) {
        throw new Error("Moneytree observation invalid");
      }
      const { evidence_id: evidenceId, ...core } = observation.document;
      if (sha256(canonicalJson(core)) !== match[1]) throw new Error("Moneytree observation digest invalid");
      fs.mkdirSync(directoryPath, { recursive: true, mode: 0o700 });
      fs.chmodSync(directoryPath, 0o700);
      const target = path.join(directoryPath, `${match[1]}.json`);
      const temporary = path.join(directoryPath, `.${match[1]}.${crypto.randomUUID()}.tmp`);
      let descriptor;
      try {
        descriptor = fs.openSync(temporary, "wx", 0o600);
        fs.writeFileSync(descriptor, `${JSON.stringify({ evidence_id: evidenceId, ...core })}\n`);
        fs.fsyncSync(descriptor);
        fs.closeSync(descriptor);
        descriptor = undefined;
        try { fs.linkSync(temporary, target); }
        catch (error) {
          if (!error || error.code !== "EEXIST") throw error;
          if (fs.readFileSync(target, "utf8") !== fs.readFileSync(temporary, "utf8")) {
            throw new Error("Moneytree observation collision");
          }
        }
        fs.chmodSync(target, 0o600);
        return observation.evidence_ref;
      } finally {
        if (descriptor !== undefined) fs.closeSync(descriptor);
        try { fs.unlinkSync(temporary); } catch (error) {
          if (!error || error.code !== "ENOENT") throw error;
        }
      }
    },
  });
}

module.exports = { buildMoneytreeObservation, canonicalJson, createMoneytreeObservationStore, sha256 };
