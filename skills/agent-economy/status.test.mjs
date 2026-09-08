import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveStatusPaths, summarizeEconomyStatus } from "./status.mjs";
import { normalizeRevenueReceipt } from "./lib/revenue-receipt.mjs";
import { reconcileRevenueReceipts } from "./lib/money-truth.mjs";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const NOW = Date.parse("2026-08-21T00:00:00Z");
const VERIFIED = normalizeRevenueReceipt({
  provider: "gig",
  payer: "0x1111111111111111111111111111111111111111",
  recipient: "0x2222222222222222222222222222222222222222",
  gross: "15.000000",
  fee: "0",
  refund: "0",
  asset: "USDC",
  proof: { chain_id: 8453, tx_hash: `0x${"55".repeat(32)}`, log_index: 0, verified: true },
  terminal_state: "settled",
  occurred_at: "2026-08-19T00:00:00.000Z",
});

test("status sums verified external net, compute, and shelter costs over the last 30 days", () => {
  const result = summarizeEconomyStatus({
    nowMs: NOW,
    earnRows: [
      VERIFIED,
      { ts: Math.floor((NOW - 40 * 86400000) / 1000), source: "gig", net_usdc: 99, external: true, tx: "0x2", status: "0x1" },
    ],
    corrections: [],
    computeRows: [
      { ts: (NOW - 3 * 86400000) / 1000, cost_usd: 6 },
      { ts: (NOW - 40 * 86400000) / 1000, cost_usd: 99 },
    ],
    shelterRows: [{ ts: NOW - 4 * 86400000, settledLeaseCostUsd: 4 }],
    liquidRunwayDays: 30,
    humanPaidInference30d: 0,
  });
  assert.equal(result.external_realized_net_30d, 15);
  assert.equal(result.compute_cost_30d, 6);
  assert.equal(result.shelter_cost_30d, 4);
  assert.equal(result.graduation.eligible, true);
});

test("status counts compute receipt cost_usdc and defaults to the Life Manager journal", () => {
  const result = summarizeEconomyStatus({
    nowMs: NOW,
    earnRows: [],
    corrections: [],
    computeRows: [{ ts: (NOW - 86400000) / 1000, cost_usdc: 0.002 }],
    shelterRows: [],
  });
  assert.equal(result.compute_cost_30d, 0.002);
  const paths = resolveStatusPaths({
    env: { HOME: "/tmp/owner" },
  });
  assert.equal(paths.computePath, "/tmp/owner/.local/state/life-manager/agent-economy/compute-receipts.jsonl");
  const overridden = resolveStatusPaths({
    env: {
      HOME: "/tmp/owner",
      COMPUTE_COST_LOG: "/tmp/explicit-compute.jsonl",
    },
  });
  assert.equal(overridden.computePath, "/tmp/explicit-compute.jsonl");
});

test("status-only status=0x1 rows contribute zero by default", () => {
  const result = summarizeEconomyStatus({
    nowMs: NOW,
    earnRows: [{ ts: NOW / 1000, source: "gig", net_usdc: 15, external: true, tx: "0x1", status: "0x1" }],
    corrections: [],
    computeRows: [],
    shelterRows: [],
  });
  assert.equal(result.external_realized_net_30d, 0);
  assert.equal(result.verified_external_rows_30d, 0);
});

test("status keeps graduation ineligible when runway or human-fuel evidence is missing", () => {
  const result = summarizeEconomyStatus({
    nowMs: NOW,
    earnRows: [{ ts: NOW / 1000, source: "gig", net_usdc: 15, external: true, tx: "0x1", status: "0x1" }],
    corrections: [],
    computeRows: [{ ts: NOW / 1000, cost_usd: 6 }],
    shelterRows: [{ ts: NOW, settledLeaseCostUsd: 4 }],
  });
  assert.equal(result.graduation.eligible, false);
  assert.equal(result.graduation.reason, "invalid-input");
});

test("canonical receipt journal rows are consumed by status", async () => {
  const dir = await mkdtemp(join(tmpdir(), "agent-economy-status-"));
  const journalPath = join(dir, "revenue-journal.jsonl");
  await reconcileRevenueReceipts({ journalPath, receipts: [normalizeRevenueReceipt({
    ...VERIFIED,
    gross: "2.000000",
    gross_decimal: undefined,
    fee_decimal: undefined,
    refund_decimal: undefined,
    signed_net_decimal: undefined,
    proof: { provider_receipt_id: "status-flow-1", verified: true },
    idempotency_key: undefined,
    signed_net: undefined,
  })] });
  const rows = (await readFile(journalPath, "utf8")).trim().split("\n").map((line) => JSON.parse(line));
  const result = summarizeEconomyStatus({ nowMs: NOW, earnRows: rows, corrections: [], computeRows: [], shelterRows: [] });
  assert.equal(result.external_realized_net_30d, 2);
  assert.equal(result.verified_external_rows_30d, 1);
});

test("inbox -> reconcile CLI -> journal -> status CLI is replay-zero", async () => {
  const home = await mkdtemp(join(tmpdir(), "agent-economy-flow-"));
  const state = join(home, "skills", "earn", "state");
  const ledgerPath = join(state, "earn-ledger.jsonl");
  const correctionPath = join(state, "receipt-reconciliations.jsonl");
  const inboxPath = join(state, "revenue-receipts.inbox.jsonl");
  const journalPath = join(state, "revenue-receipts.jsonl");
  const computePath = join(home, "compute.jsonl");
  const shelterPath = join(home, "shelter.jsonl");
  await mkdir(state, { recursive: true });
  await Promise.all([
    writeFile(ledgerPath, ""),
    writeFile(computePath, ""),
    writeFile(shelterPath, ""),
    writeFile(inboxPath, `${JSON.stringify({
      ...VERIFIED,
      schema_version: undefined,
      kind: undefined,
      signed_net: undefined,
      gross_decimal: undefined,
      fee_decimal: undefined,
      refund_decimal: undefined,
      signed_net_decimal: undefined,
      idempotency_key: undefined,
      gross: "2.000000",
      proof: { provider_receipt_id: "subprocess-flow-1", verified: true },
    })}\n`),
  ]);
  const args = ["skills/agent-economy/reconcile-receipts.mjs", ledgerPath, correctionPath, inboxPath, journalPath];
  const first = JSON.parse((await execFileAsync(process.execPath, args, { cwd: process.cwd(), timeout: 5_000, env: { ...process.env, ANICCA_HOME: home, HOME: home } })).stdout.trim());
  assert.equal(first.receipt_candidates_seen, 1);
  assert.equal(first.receipt_accepted, 1);
  const second = JSON.parse((await execFileAsync(process.execPath, args, { cwd: process.cwd(), timeout: 5_000, env: { ...process.env, ANICCA_HOME: home, HOME: home } })).stdout.trim());
  assert.equal(second.receipt_accepted, 0);
  assert.equal(second.receipt_duplicates, 1);
  assert.equal((await readFile(journalPath, "utf8")).trim().split("\n").length, 1);
  const status = JSON.parse((await execFileAsync(process.execPath, ["skills/agent-economy/status.mjs", ledgerPath, correctionPath, computePath, shelterPath, journalPath], { cwd: process.cwd(), timeout: 5_000, env: { ...process.env, ANICCA_HOME: home, HOME: home } })).stdout.trim());
  assert.equal(status.external_realized_net_30d, 2);
  const run = JSON.parse((await execFileAsync("bash", ["skills/agent-economy/run.sh"], { cwd: process.cwd(), timeout: 5_000, env: { ...process.env, AGENT_ECONOMY_STATE_ROOT: state, HOME: home } })).stdout.trim());
  assert.equal(run.external_realized_net_30d, 2);
  assert.equal((await readFile(journalPath, "utf8")).trim().split("\n").length, 1);
});

test("status rejects malformed nonblank JSONL and does not echo its sentinel", async () => {
  const dir = await mkdtemp(join(tmpdir(), "agent-economy-status-corrupt-"));
  const ledgerPath = join(dir, "earn.jsonl");
  const correctionPath = join(dir, "corrections.jsonl");
  const computePath = join(dir, "compute.jsonl");
  const shelterPath = join(dir, "shelter.jsonl");
  const journalPath = join(dir, "journal.jsonl");
  const sentinel = "STATUS_SECRET_SENTINEL";
  await writeFile(ledgerPath, `${JSON.stringify({ status: "0x1" })}\n${sentinel}\n`);
  await Promise.all([writeFile(correctionPath, ""), writeFile(computePath, ""), writeFile(shelterPath, ""), writeFile(journalPath, "")]);
  await assert.rejects(
    () => execFileAsync(process.execPath, ["skills/agent-economy/status.mjs", ledgerPath, correctionPath, computePath, shelterPath, journalPath], { cwd: process.cwd(), timeout: 5_000, env: { ...process.env } }),
    (error) => error && error.code !== 0 && !String(error.stderr || "").includes(sentinel),
  );
});

test("status default journal follows a custom earn ledger directory, while explicit journal wins", async () => {
  const home = await mkdtemp(join(tmpdir(), "agent-economy-status-path-"));
  const customDir = join(home, "custom-state");
  await mkdir(customDir, { recursive: true });
  const ledgerPath = join(customDir, "earn.jsonl");
  const correctionPath = join(customDir, "corrections.jsonl");
  const computePath = join(home, "compute.jsonl");
  const shelterPath = join(home, "shelter.jsonl");
  const defaultJournal = join(customDir, "revenue-receipts.jsonl");
  const explicitJournal = join(home, "explicit-journal.jsonl");
  await Promise.all([writeFile(ledgerPath, ""), writeFile(correctionPath, ""), writeFile(computePath, ""), writeFile(shelterPath, "")]);
  const receipt = normalizeRevenueReceipt({
    provider: "status-path",
    payer: "0x1111111111111111111111111111111111111111",
    recipient: "0x2222222222222222222222222222222222222222",
    gross: "3.000000", fee: "0", refund: "0", asset: "USDC",
    proof: { provider_receipt_id: "status-path-1", verified: true },
    terminal_state: "settled", occurred_at: "2026-08-20T00:00:00.000Z",
  });
  await writeFile(defaultJournal, `${JSON.stringify(receipt)}\n`);
  const baseArgs = ["skills/agent-economy/status.mjs", ledgerPath, correctionPath, computePath, shelterPath];
  const defaultResult = JSON.parse((await execFileAsync(process.execPath, baseArgs, { cwd: process.cwd(), timeout: 5_000, env: { ...process.env, ANICCA_HOME: join(home, "different-home") } })).stdout.trim());
  assert.equal(defaultResult.external_realized_net_30d, 3);
  await writeFile(explicitJournal, `${JSON.stringify(normalizeRevenueReceipt({ ...receipt, proof: { provider_receipt_id: "status-path-2", verified: true }, idempotency_key: undefined }))}\n`);
  const explicitResult = JSON.parse((await execFileAsync(process.execPath, [...baseArgs, explicitJournal], { cwd: process.cwd(), timeout: 5_000, env: { ...process.env, ANICCA_HOME: join(home, "different-home") } })).stdout.trim());
  assert.equal(explicitResult.external_realized_net_30d, 3);
});

test("reconcile CLI errors contain only a stable class, never candidate values", async () => {
  const dir = await mkdtemp(join(tmpdir(), "agent-economy-cli-sanitize-"));
  const ledgerPath = join(dir, "earn.jsonl");
  const correctionPath = join(dir, "corrections.jsonl");
  const inboxPath = join(dir, "inbox.jsonl");
  const journalPath = join(dir, "journal.jsonl");
  const sentinel = "CLI_SECRET_SENTINEL";
  await Promise.all([writeFile(ledgerPath, ""), writeFile(correctionPath, ""), writeFile(journalPath, ""), writeFile(inboxPath, `${JSON.stringify({
    provider: "cli", payer: "0x1111111111111111111111111111111111111111", recipient: "0x2222222222222222222222222222222222222222",
    gross: "1", fee: "0", refund: "0", asset: "USDC", proof: { provider_receipt_id: "cli-invalid", verified: true },
    terminal_state: sentinel, occurred_at: "2026-08-20T00:00:00.000Z",
  })}\n`)]);
  await assert.rejects(
    () => execFileAsync(process.execPath, ["skills/agent-economy/reconcile-receipts.mjs", ledgerPath, correctionPath, inboxPath, journalPath], { cwd: process.cwd(), timeout: 5_000, env: { ...process.env } }),
    (error) => error && error.code !== 0 && !String(error.stderr || "").includes(sentinel) && /validation|reconcile|invalid/i.test(String(error.stderr || "")),
  );
});

test("status discovers its ledger from AGENT_ECONOMY_STATE_ROOT when no paths are supplied", async () => {
  const home = await mkdtemp(join(tmpdir(), "agent-economy-status-home-"));
  const state = join(home, "agent-economy");
  await mkdir(state, { recursive: true });
  await writeFile(join(state, "earn-ledger.jsonl"), `${JSON.stringify(VERIFIED)}\n`);
  await writeFile(join(state, "revenue-receipts.jsonl"), "");

  const result = JSON.parse((await execFileAsync(process.execPath, ["skills/agent-economy/status.mjs"], {
    cwd: process.cwd(),
    timeout: 5_000,
    env: { ...process.env, AGENT_ECONOMY_STATE_ROOT: state, HOME: home },
  })).stdout.trim());
  assert.equal(result.external_realized_net_30d, 15);
});

test("status discovers compute and shelter from the shared Life Manager state", async () => {
  const root = await mkdtemp(join(tmpdir(), "agent-economy-status-owner-"));
  const state = join(root, "life-manager", "agent-economy");
  const ownerCompute = join(state, "compute-receipts.jsonl");
  const ownerShelter = join(state, "shelter-cost.jsonl");
  await mkdir(state, { recursive: true });
  await Promise.all([
    writeFile(join(state, "earn-ledger.jsonl"), ""),
    writeFile(join(state, "revenue-receipts.jsonl"), ""),
    writeFile(ownerCompute, `${JSON.stringify({ ts: NOW / 1000, cost_usd: 6 })}\n`),
    writeFile(ownerShelter, `${JSON.stringify({ ts: NOW, settledLeaseCostUsd: 4 })}\n`),
  ]);
  const result = JSON.parse((await execFileAsync(process.execPath, ["skills/agent-economy/status.mjs"], {
    cwd: process.cwd(),
    timeout: 5_000,
    env: { ...process.env, HOME: root, AGENT_ECONOMY_STATE_ROOT: state },
  })).stdout.trim());
  assert.equal(result.compute_cost_30d, 6);
  assert.equal(result.shelter_cost_30d, 4);
});

test("status with neither explicit paths nor a state root exits 2 with a stable secret-free config diagnostic", async () => {
  const env = { ...process.env };
  delete env.HOME;
  delete env.LIFE_MANAGER_STATE_ROOT;
  delete env.AGENT_ECONOMY_STATE_ROOT;
  await assert.rejects(
    () => execFileAsync(process.execPath, ["skills/agent-economy/status.mjs"], {
      cwd: process.cwd(),
      timeout: 5_000,
      env,
    }),
    (error) => error && error.code === 2
      && String(error.stderr || "").trim() === "status: STATUS_CONFIG_MISSING"
      && !String(error.stderr || "").includes("undefined"),
  );
});

test("status rejects discovery without HOME or an explicit state root", () => {
  assert.throws(
    () => resolveStatusPaths({ env: {} }),
    (error) => error?.code === "STATUS_CONFIG_MISSING",
  );
});

test("status combines an explicit earn path with the standard cost paths", () => {
  const standard = resolveStatusPaths({ args: ["/tmp/earn-ledger.jsonl"], env: { HOME: "/tmp/owner" } });
  assert.equal(standard.computePath, "/tmp/owner/.local/state/life-manager/agent-economy/compute-receipts.jsonl");
  const paths = resolveStatusPaths({
    args: ["/tmp/earn-ledger.jsonl"],
    env: { COMPUTE_COST_LOG: "/tmp/compute.jsonl", SHELTER_COST_LEDGER: "/tmp/shelter.jsonl" },
  });
  assert.equal(paths.earnPath, "/tmp/earn-ledger.jsonl");
  assert.equal(paths.computePath, "/tmp/compute.jsonl");
  assert.equal(paths.shelterPath, "/tmp/shelter.jsonl");
});

test("run.sh counts cost_usdc from the Life Manager compute journal by default", async () => {
  const root = await mkdtemp(join(tmpdir(), "agent-economy-run-instance-compute-"));
  const state = join(root, "agent-economy");
  const instanceCompute = join(state, "compute-receipts.jsonl");
  const owner = join(root, "owner-home");
  await mkdir(state, { recursive: true });
  await Promise.all([
    writeFile(join(state, "earn-ledger.jsonl"), ""),
    writeFile(join(state, "receipt-reconciliations.jsonl"), ""),
    writeFile(join(state, "revenue-receipts.inbox.jsonl"), ""),
    writeFile(join(state, "revenue-receipts.jsonl"), ""),
    writeFile(instanceCompute, `${JSON.stringify({ ts: Date.now() / 1000, cost_usdc: 0.002 })}\n`),
  ]);
  const env = { ...process.env, AGENT_ECONOMY_STATE_ROOT: state, HOME: owner };
  delete env.COMPUTE_COST_LOG;
  delete env.SHELTER_COST_LEDGER;
  const result = JSON.parse((await execFileAsync("bash", ["skills/agent-economy/run.sh"], {
    cwd: process.cwd(), timeout: 5_000, env,
  })).stdout.trim());
  assert.equal(result.compute_cost_30d, 0.002);
});
