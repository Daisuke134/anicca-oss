#!/usr/bin/env node
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { summarizeRealizedRevenue } from "./lib/money-truth.mjs";
import { graduationGate } from "./lib/treasury-policy.mjs";

const WINDOW_MS = 30 * 86400000;

export class StatusConfigError extends Error {
  constructor() {
    super("status configuration is missing");
    this.name = "StatusConfigError";
    this.code = "STATUS_CONFIG_MISSING";
  }
}

function configuredPath(value) {
  return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}

/**
 * Resolve CLI paths without manufacturing an undefined path. Explicit positional paths win;
 * AGENT_ECONOMY_STATE_ROOT supplies the portable mutable-data root.
 */
export function resolveStatusPaths({ args = [], env = process.env } = {}) {
  const values = Array.isArray(args) ? args : [];
  const e = env || {};
  const ownerHome = configuredPath(e.HOME);
  const lifeManagerState = configuredPath(e.LIFE_MANAGER_STATE_ROOT)
    || (ownerHome && path.join(ownerHome, ".local", "state", "life-manager"));
  const state = configuredPath(e.AGENT_ECONOMY_STATE_ROOT)
    || (lifeManagerState && path.join(lifeManagerState, "agent-economy"));
  const explicit = values.some((value) => configuredPath(value));
  if ((!explicit && !state) || (!state && !configuredPath(values[0]))) throw new StatusConfigError();

  const earnPath = configuredPath(values[0]) || (state && path.join(state, "earn-ledger.jsonl"));
  const correctionPath = configuredPath(values[1]) || (state && path.join(state, "receipt-reconciliations.jsonl"));
  const explicitComputePath = configuredPath(values[2]) || configuredPath(e.COMPUTE_COST_LOG);
  const explicitShelterPath = configuredPath(values[3]) || configuredPath(e.SHELTER_COST_LEDGER);
  if (!state && explicit && (!explicitComputePath || !explicitShelterPath)) throw new StatusConfigError();
  const computePath = explicitComputePath
    || (state && path.join(state, "compute-receipts.jsonl"));
  const shelterPath = explicitShelterPath
    || (state && path.join(state, "shelter-cost.jsonl"));
  const journalPath = configuredPath(values[4]) || configuredPath(e.REVENUE_RECEIPT_JOURNAL)
    || (earnPath && path.join(path.dirname(earnPath), "revenue-receipts.jsonl"));
  return { earnPath, correctionPath, computePath, shelterPath, journalPath };
}

function timestampMs(row) {
  const raw = row?.ts ?? row?.timestamp ?? row?.occurred_at;
  const value = Number(raw);
  if (Number.isFinite(value)) return value > 1e12 ? value : value * 1000;
  const parsed = Date.parse(String(raw ?? ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function recent(rows, startMs, endMs) {
  return (Array.isArray(rows) ? rows : []).filter((row) => {
    const at = timestampMs(row);
    return at !== null && at >= startMs && at < endMs;
  });
}

function sumField(rows, fields) {
  return Math.round((Array.isArray(rows) ? rows : []).reduce((sum, row) => {
    const value = fields.map((field) => row?.[field]).find((candidate) => candidate !== undefined);
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? sum + number : sum;
  }, 0) * 1e6) / 1e6;
}

export function summarizeEconomyStatus({
  earnRows = [],
  corrections = [],
  computeRows = [],
  shelterRows = [],
  nowMs = Date.now(),
  liquidRunwayDays,
  humanPaidInference30d,
} = {}) {
  const now = Number(nowMs);
  if (!Number.isFinite(now)) throw new TypeError("nowMs must be finite");
  const start = now - WINDOW_MS;
  const earn30 = recent(earnRows, start, now);
  const compute30 = recent(computeRows, start, now);
  const shelter30 = recent(shelterRows, start, now);
  // Production status requires the same verified proof gate as the reconcile loop.  Legacy rows that
  // merely claim status=0x1 remain visible as unverified but never become realized revenue.
  const revenue = summarizeRealizedRevenue(earn30, corrections);
  const computeCost = sumField(compute30, ["cost_usdc", "cost_usd", "costUsd", "est_usd"]);
  const shelterCost = sumField(shelter30, ["settledLeaseCostUsd", "shelter_cost_usd"]);
  const graduation = graduationGate({
    externalRealizedNet30d: revenue.external_net_usdc,
    computeCost30d: computeCost,
    shelterCost30d: shelterCost,
    liquidRunwayDays,
    humanPaidInference30d,
  });
  return {
    window_days: 30,
    external_realized_net_30d: revenue.external_net_usdc,
    verified_external_rows_30d: revenue.verified_external_rows,
    unverified_external_rows_30d: revenue.unverified_external_rows,
    compute_cost_30d: computeCost,
    shelter_cost_30d: shelterCost,
    graduation,
  };
}

async function readJsonl(file) {
  if (!file) return [];
  try {
    const raw = await fs.readFile(file, "utf8");
    const rows = [];
    for (const [index, line] of raw.split("\n").entries()) {
      if (!line.trim()) continue;
      try { rows.push(JSON.parse(line)); }
      catch {
        const error = new Error("status JSONL invalid");
        error.code = "STATUS_JSONL_INVALID";
        error.line = index + 1;
        throw error;
      }
    }
    return rows;
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) {
  try {
    const {
      earnPath,
      correctionPath,
      computePath,
      shelterPath,
      journalPath,
    } = resolveStatusPaths({ args: process.argv.slice(2), env: process.env });
    const effectiveLedgerPath = earnPath;
    const defaultJournalPath = journalPath;
    const [earnRows, corrections, computeRows, shelterRows, journalRows] = await Promise.all([
      readJsonl(effectiveLedgerPath), readJsonl(correctionPath), readJsonl(computePath), readJsonl(shelterPath),
      readJsonl(journalPath || defaultJournalPath),
    ]);
    const receiptKeys = new Set();
    const combinedEarnRows = [...earnRows, ...journalRows].filter((row) => {
      const key = row?.kind === "revenue_receipt" && typeof row.idempotency_key === "string" ? row.idempotency_key : null;
      if (!key) return true;
      if (receiptKeys.has(key)) return false;
      receiptKeys.add(key);
      return true;
    });
    process.stdout.write(`${JSON.stringify(summarizeEconomyStatus({
      earnRows: combinedEarnRows,
      corrections,
      computeRows,
      shelterRows,
      liquidRunwayDays: process.env.ANICCA_LIQUID_RUNWAY_DAYS === undefined
        ? undefined : Number(process.env.ANICCA_LIQUID_RUNWAY_DAYS),
      humanPaidInference30d: process.env.ANICCA_HUMAN_PAID_INFERENCE_30D === undefined
        ? undefined : Number(process.env.ANICCA_HUMAN_PAID_INFERENCE_30D),
    }))}\n`);
  } catch (error) {
    const code = /^[A-Z][A-Z0-9_]*$/.test(String(error?.code || "")) ? error.code : "STATUS_FAILED";
    process.stderr.write(`status: ${code}\n`);
    process.exitCode = error?.code === "STATUS_CONFIG_MISSING" ? 2 : 1;
  }
}
