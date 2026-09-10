import { appendFile, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { dirname, join } from 'node:path';
import { promisify } from 'node:util';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { privateKeyToAccount } from 'viem/accounts';

import { loadEvmKey } from '../lib/resolve-identity.mjs';
import { evmErc20Balance, EVM_TOKENS, RPC } from '../lib/net-worth.mjs';
import { generateImage as generateX402Image } from './x402-image-client.mjs';

const USDC_DECIMALS = 1_000_000;
export const TASKMARKET_CLI = join(
  dirname(fileURLToPath(import.meta.url)),
  'node_modules', '.bin', 'taskmarket',
);
const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
const MAX_IMAGE_COST_USD = 0.07;
const DAILY_IMAGE_CAP_USD = 0.14;
const MIN_FLOAT_USD = 0.25;
const execFileAsync = promisify(execFile);

function rewardUsd(task) {
  const atomic = Number(task?.netReward ?? task?.reward);
  return Number.isFinite(atomic) ? atomic / USDC_DECIMALS : 0;
}

export function classifyTask(task, { maxImageCostUsd = 0.07 } = {}) {
  if (!task || task.status !== 'open' || task.phase !== 'active') {
    return { supported: false, reason: 'task_not_active' };
  }
  if (task.submissionWindowOpen !== true) {
    return { supported: false, reason: 'submission_window_closed' };
  }
  if (task.stakeRequired === true) {
    return { supported: false, reason: 'stake_required' };
  }
  const description = String(task.description || '');
  const isStillImage = /\b(still image|hero image|1:1 square)\b/i.test(description);
  const requiresFrontierImage = /\b(GPT Image 2|frontier image model)\b/i.test(description);
  const asksForFilmOrApp = /\b(short film|video|web app|single-page app)\b/i.test(description);
  if (!isStillImage || !requiresFrontierImage || asksForFilmOrApp) {
    return { supported: false, reason: 'unsupported_deliverable' };
  }
  if (rewardUsd(task) < Number(maxImageCostUsd) * 20) {
    return { supported: false, reason: 'reward_below_20x_cost' };
  }
  return { supported: true, reason: 'supported_still_image' };
}

export function selectTask({
  tasks,
  submissions,
  now = Date.now(),
  maxImageCostUsd = 0.07,
}) {
  const submitted = new Set(
    (Array.isArray(submissions) ? submissions : [])
      .map((row) => String(row?.taskId || row?.task_id || row?.task?.id || ''))
      .filter(Boolean),
  );
  const candidates = (Array.isArray(tasks) ? tasks : [])
    .filter((task) => !submitted.has(String(task?.id || '')))
    .filter((task) => {
      const expiry = Date.parse(String(task?.expiryTime || ''));
      return Number.isFinite(expiry) && expiry > Number(now);
    })
    .filter((task) => classifyTask(task, { maxImageCostUsd }).supported)
    .sort((a, b) => {
      const expiry = Date.parse(a.expiryTime) - Date.parse(b.expiryTime);
      if (expiry !== 0) return expiry;
      const competition = Number(a.submissionCount || 0) - Number(b.submissionCount || 0);
      if (competition !== 0) return competition;
      return rewardUsd(b) - rewardUsd(a);
    });
  return candidates[0] || null;
}

function parseTaskmarketJson(stdout, label) {
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    throw new Error(`${label} returned non-JSON output`);
  }
  if (parsed?.ok !== true) throw new Error(`${label} did not return ok=true`);
  return parsed.data;
}

async function taskmarketCli(args) {
  const { stdout } = await execFileAsync(TASKMARKET_CLI, args, {
    timeout: 60_000,
    maxBuffer: 2 * 1024 * 1024,
  });
  return parseTaskmarketJson(stdout, `taskmarket ${args.join(' ')}`);
}

async function defaultListTasks() {
  const data = await taskmarketCli(['task', 'list', '--status', 'open', '--limit', '100']);
  if (!data || !Array.isArray(data.tasks)) throw new Error('TaskMarket task list is malformed');
  return data.tasks;
}

async function defaultListSubmissions() {
  const data = await taskmarketCli(['task', 'my-submissions']);
  if (!Array.isArray(data)) throw new Error('TaskMarket submission list is malformed');
  return data;
}

async function defaultSubmitTask(taskId, files) {
  const args = ['task', 'submit', taskId];
  for (const file of files) args.push('--file', file);
  args.push('--role', 'final');
  return taskmarketCli(args);
}

async function defaultDownloadImage(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(60_000) });
  if (!response.ok) throw new Error(`image download returned HTTP ${response.status}`);
  const declared = Number(response.headers.get('content-length') || 0);
  if (declared > MAX_IMAGE_BYTES) throw new Error('image download exceeds size cap');
  const buffer = Buffer.from(await response.arrayBuffer());
  if (buffer.length > MAX_IMAGE_BYTES) throw new Error('image download exceeds size cap');
  return buffer;
}

export function validateSquarePng(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 29
    || !buffer.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'))
    || buffer.subarray(12, 16).toString('ascii') !== 'IHDR') {
    throw new Error('generated hero is not a PNG with an IHDR');
  }
  const width = buffer.readUInt32BE(16);
  const height = buffer.readUInt32BE(20);
  if (width !== height || width < 1024) {
    throw new Error('generated hero must be a square PNG at least 1024px');
  }
  return { width, height };
}

function imagePrompt(task) {
  return [
    'Create the single finished hero image requested below.',
    'Use a 1:1 square composition at 1024x1024.',
    'Treat every locked fact and exact number as immutable.',
    'Typography must be native to the artwork: printed in the same light, grain, and material, legible at phone size.',
    'Do not add a model name, watermark, caption frame, explanation, or facts not present in the brief.',
    'Return only the finished artwork.',
    '',
    'TASKMARKET BRIEF:',
    String(task.description || '').trim(),
  ].join('\n');
}

function conceptNote(task, model) {
  return [
    '# Concept note',
    '',
    `Task: \`${task.id}\``,
    `Model: ${model}`,
    '',
    'The composition follows the requester’s locked factual brief as the sole factual input.',
    'The focal structure, native typography, material treatment, and phone-size legibility are generated as one integrated artwork rather than assembled as an overlay.',
    '',
  ].join('\n');
}

function sourcesNote(task) {
  return [
    '# Sources',
    '',
    `- TaskMarket task readback: https://api.taskmarket.dev/v1/tasks/${task.id}`,
    '- Factual scope: the requester-supplied locked facts in the task description. The brief explicitly instructs the worker not to re-research or introduce unlocked figures.',
    '',
  ].join('\n');
}

function submissionId(row) {
  return row?.id
    || row?.submissionId
    || row?.submission_id
    || row?.submitTxHash
    || row?.submit_tx_hash
    || null;
}

function taskIdOf(row) {
  return row?.taskId || row?.task_id || row?.task?.id || null;
}

async function appendEarnAttempt(path, row) {
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify(row)}\n`, { mode: 0o600 });
}

async function readEarnAttempts(path) {
  try {
    return (await readFile(path, 'utf8'))
      .split('\n')
      .filter(Boolean)
      .map((line) => {
        try { return JSON.parse(line); } catch { return null; }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

async function reconcileExistingSubmission({
  earnLedgerPath,
  taskId,
  id,
  wakeId,
  now,
}) {
  if (!id) return false;
  const rows = await readEarnAttempts(earnLedgerPath);
  const alreadyReconciled = rows.some((row) =>
    row?.source === 'taskmarket_work_reconciliation'
    && row?.task === taskId
    && row?.submission_id === id);
  if (alreadyReconciled) return false;
  const pending = rows.findLast((row) =>
    row?.source === 'taskmarket_work_attempt'
    && row?.task === taskId
    && !row?.submission_id);
  if (!pending) return false;
  await appendEarnAttempt(earnLedgerPath, {
    ts: Math.floor(now / 1000),
    wake: wakeId,
    source: 'taskmarket_work_reconciliation',
    task: taskId,
    earn_usdc: 0,
    cost_usdc: 0,
    net_usdc: 0,
    submission_id: id,
    reconciles_wake: pending.wake,
  });
  return true;
}

async function readBackSubmission({
  listSubmissions,
  taskId,
  attempts,
  delayMs,
  sleep,
}) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const rows = await listSubmissions();
    const found = rows.find((row) => taskIdOf(row) === taskId);
    if (submissionId(found)) return found;
    if (attempt + 1 < attempts) await sleep(delayMs);
  }
  return null;
}

async function readSpend(path, day) {
  try {
    const state = JSON.parse(await readFile(path, 'utf8'));
    if (state?.day === day && Number.isFinite(Number(state.spentUsd))) return state;
  } catch {
    // Missing or malformed state starts a fresh fail-safe day record.
  }
  return { day, spentUsd: 0 };
}

async function reserveImageSpend({ walletKey, amountUsd, statePath, day }) {
  const address = privateKeyToAccount(walletKey).address;
  const balanceUsd = await evmErc20Balance(
    'base',
    EVM_TOKENS.base[0],
    address,
    fetch,
    RPC.base,
  );
  if (balanceUsd - amountUsd < MIN_FLOAT_USD) {
    throw new Error('TaskMarket image spend would breach agent float floor');
  }
  const state = await readSpend(statePath, day);
  if (state.spentUsd + amountUsd > DAILY_IMAGE_CAP_USD + Number.EPSILON) {
    throw new Error('TaskMarket image daily cap reached');
  }
  const next = { day, spentUsd: Number((state.spentUsd + amountUsd).toFixed(6)) };
  await mkdir(dirname(statePath), { recursive: true });
  const temp = `${statePath}.tmp`;
  await writeFile(temp, JSON.stringify(next), { mode: 0o600 });
  await rename(temp, statePath);
}

function taskById(tasks, id) {
  if (!/^0x[0-9a-fA-F]{64}$/.test(String(id || ''))) {
    throw new Error('TaskMarket taskId must be 32-byte hex');
  }
  const found = tasks.find((task) => task?.id === id);
  if (!found) throw new Error('requested TaskMarket task is not open');
  return found;
}

export async function runTaskMarketPass(options = {}, deps = {}) {
  const {
    action = 'execute',
    taskId = null,
    aniccaHome = process.env.ANICCA_HOME,
    earnLedgerPath = process.env.EARN_LEDGER
      || join(aniccaHome || '.', 'skills', 'earn', 'state', 'earn-ledger.jsonl'),
    wakeId = process.env.WAKE_ID || `taskmarket-${Date.now()}`,
    now = Date.now(),
    maxImageCostUsd = MAX_IMAGE_COST_USD,
    submissionReadbackAttempts = 5,
    submissionReadbackDelayMs = 1000,
  } = options;
  if (!aniccaHome) throw new Error('ANICCA_HOME is required');

  const listTasks = deps.listTasks || defaultListTasks;
  const listSubmissions = deps.listSubmissions || defaultListSubmissions;
  const loadWalletKey = deps.loadWalletKey || (() => loadEvmKey());
  const downloadImage = deps.downloadImage || defaultDownloadImage;
  const submitTask = deps.submitTask || defaultSubmitTask;
  const sleep = deps.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const tasks = await listTasks();
  const submissions = await listSubmissions();

  if (action === 'poll') {
    const supported = tasks.filter((task) =>
      classifyTask(task, { maxImageCostUsd }).supported
      && !submissions.some((row) => taskIdOf(row) === task.id));
    return {
      ok: true,
      action: 'polled',
      openTasks: tasks.length,
      ownedSubmissions: submissions.length,
      supportedUnsubmitted: supported.map((task) => task.id),
    };
  }
  if (action !== 'execute') throw new Error(`unsupported TaskMarket action: ${action}`);

  for (const existing of submissions) {
    const existingTaskId = taskIdOf(existing);
    const id = submissionId(existing);
    if (!existingTaskId || !id) continue;
    const reconciled = await reconcileExistingSubmission({
      earnLedgerPath,
      taskId: existingTaskId,
      id,
      wakeId,
      now,
    });
    if (reconciled) {
      return {
        ok: true,
        action: 'reconciled_submission',
        taskId: existingTaskId,
        submissionId: id,
        costUsd: 0,
      };
    }
  }

  if (taskId) {
    const existing = submissions.find((row) => taskIdOf(row) === taskId);
    if (existing) {
      const id = submissionId(existing);
      const reconciled = await reconcileExistingSubmission({
        earnLedgerPath,
        taskId,
        id,
        wakeId,
        now,
      });
      return {
        ok: true,
        action: reconciled ? 'reconciled_submission' : 'already_submitted',
        taskId,
        submissionId: id,
        costUsd: 0,
      };
    }
  }

  const selected = taskId
    ? taskById(tasks, taskId)
    : selectTask({ tasks, submissions, now, maxImageCostUsd });
  if (!selected) {
    return { ok: true, action: 'no_eligible_task', costUsd: 0 };
  }
  const classification = classifyTask(selected, { maxImageCostUsd });
  if (!classification.supported) {
    throw new Error(`requested TaskMarket task is unsupported: ${classification.reason}`);
  }

  const walletKey = loadWalletKey();
  if (!walletKey) throw new Error('no per-instance EVM key for TaskMarket image spend');
  const stateDir = join(
    aniccaHome,
    'skills',
    'earn',
    'taskmarket',
    'state',
  );
  const outputDir = join(stateDir, 'submissions', selected.id);
  const spendPath = join(stateDir, 'image-spend.json');
  const day = new Date(now).toISOString().slice(0, 10);
  const generateImage = deps.generateImage || ((request) => generateX402Image({
    ...request,
    reserveSpend: (amountUsd) => reserveImageSpend({
      walletKey,
      amountUsd,
      statePath: spendPath,
      day,
    }),
    maxQuoteUsd: maxImageCostUsd,
  }));

  const generated = await generateImage({
    prompt: imagePrompt(selected),
    walletKey,
  });
  const hero = await downloadImage(generated.url);
  validateSquarePng(hero);
  await mkdir(outputDir, { recursive: true, mode: 0o700 });
  const files = [
    join(outputDir, 'hero.png'),
    join(outputDir, 'concept-note.md'),
    join(outputDir, 'sources.md'),
  ];
  await writeFile(files[0], hero, { mode: 0o600 });
  await writeFile(files[1], conceptNote(selected, generated.model), { mode: 0o600 });
  await writeFile(files[2], sourcesNote(selected), { mode: 0o600 });
  await submitTask(selected.id, files);

  const recorded = await readBackSubmission({
    listSubmissions,
    taskId: selected.id,
    attempts: submissionReadbackAttempts,
    delayMs: submissionReadbackDelayMs,
    sleep,
  });
  const id = submissionId(recorded);
  const ledgerRow = {
    ts: Math.floor(now / 1000),
    wake: wakeId,
    source: 'taskmarket_work_attempt',
    task: selected.id,
    earn_usdc: 0,
    cost_usdc: generated.costUsd,
    net_usdc: -generated.costUsd,
    submission_id: id,
    model: generated.model,
  };
  await appendEarnAttempt(earnLedgerPath, ledgerRow);
  if (!id) throw new Error('submission_readback_missing');

  return {
    ok: true,
    action: 'submitted',
    taskId: selected.id,
    submissionId: id,
    model: generated.model,
    costUsd: generated.costUsd,
  };
}

const isEntry = process.argv[1]
  && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntry) {
  let args = {};
  try {
    args = JSON.parse(process.env.ANICCA_ARGS || '{}');
  } catch {
    process.stderr.write('ANICCA_ARGS must be valid JSON\n');
    process.exitCode = 1;
  }
  if (process.exitCode !== 1) {
    runTaskMarketPass(args)
      .then((result) => process.stdout.write(`${JSON.stringify(result)}\n`))
      .catch((error) => {
        process.stderr.write(`${JSON.stringify({
          ok: false,
          error: String(error?.message || error).slice(0, 400),
        })}\n`);
        process.exitCode = 1;
      });
  }
}
