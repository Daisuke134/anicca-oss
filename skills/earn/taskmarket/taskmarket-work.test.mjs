import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { TASKMARKET_CLI, classifyTask, runTaskMarketPass, selectTask } from './taskmarket-work.mjs';

const NOW = Date.parse('2026-07-28T08:00:00Z');
const IMAGE_BRIEF = [
  'Make one still image, 1:1 square.',
  'FLOOR: made with a real frontier image model such as GPT Image 2 or better.',
  'DELIVER: one finished hero image, plus concept-note.md and sources.md.',
].join(' ');

test('TaskMarket uses the repository-local CLI on every operating system', () => {
  assert.equal(TASKMARKET_CLI, join(import.meta.dirname, 'node_modules', '.bin', 'taskmarket'));
  assert.doesNotMatch(TASKMARKET_CLI, /homebrew/);
});

function task(overrides = {}) {
  return {
    id: '0x' + '1'.repeat(64),
    status: 'open',
    phase: 'active',
    submissionWindowOpen: true,
    stakeRequired: false,
    description: IMAGE_BRIEF,
    reward: '5000000',
    netReward: '4625000',
    submissionCount: 20,
    expiryTime: '2026-07-29T08:00:00Z',
    ...overrides,
  };
}

test('classifyTask accepts one unstaked frontier still-image delivery', () => {
  assert.deepEqual(classifyTask(task(), { maxImageCostUsd: 0.06 }), {
    supported: true,
    reason: 'supported_still_image',
  });
});

test('classifyTask rejects closed, staked, unsupported, and uneconomic tasks', () => {
  assert.equal(classifyTask(task({ submissionWindowOpen: false }), { maxImageCostUsd: 0.06 }).reason, 'submission_window_closed');
  assert.equal(classifyTask(task({ stakeRequired: true }), { maxImageCostUsd: 0.06 }).reason, 'stake_required');
  assert.equal(classifyTask(task({ description: 'Produce one short film with three scenes.' }), { maxImageCostUsd: 0.06 }).reason, 'unsupported_deliverable');
  assert.equal(classifyTask(task({ netReward: '1100000' }), { maxImageCostUsd: 0.06 }).reason, 'reward_below_20x_cost');
});

test('selectTask excludes owned submissions and ranks expiry, competition, then reward', () => {
  const submittedId = '0x' + '2'.repeat(64);
  const laterLowCompetition = task({
    id: '0x' + '3'.repeat(64),
    expiryTime: '2026-07-30T08:00:00Z',
    submissionCount: 1,
    netReward: '9000000',
  });
  const earliestCrowded = task({
    id: '0x' + '4'.repeat(64),
    expiryTime: '2026-07-29T07:00:00Z',
    submissionCount: 50,
  });
  const earliestSparse = task({
    id: '0x' + '5'.repeat(64),
    expiryTime: '2026-07-29T07:00:00Z',
    submissionCount: 10,
    netReward: '4000000',
  });
  const alreadySubmitted = task({ id: submittedId, expiryTime: '2026-07-28T09:00:00Z' });

  const selected = selectTask({
    tasks: [laterLowCompetition, earliestCrowded, alreadySubmitted, earliestSparse],
    submissions: [{ id: 'sub_1', taskId: submittedId }],
    now: NOW,
    maxImageCostUsd: 0.06,
  });

  assert.equal(selected.id, earliestSparse.id);
});

test('selectTask returns null for expired or unsupported inventory', () => {
  const selected = selectTask({
    tasks: [
      task({ expiryTime: '2026-07-28T07:59:59Z' }),
      task({ description: 'Build a single-page flashcard web app.' }),
    ],
    submissions: [],
    now: NOW,
    maxImageCostUsd: 0.06,
  });
  assert.equal(selected, null);
});

function squarePng(width = 1024, height = 1024) {
  const png = Buffer.alloc(33);
  Buffer.from('89504e470d0a1a0a', 'hex').copy(png, 0);
  png.writeUInt32BE(13, 8);
  Buffer.from('IHDR').copy(png, 12);
  png.writeUInt32BE(width, 16);
  png.writeUInt32BE(height, 20);
  png[24] = 8;
  png[25] = 2;
  return png;
}

test('runTaskMarketPass generates three files, submits once, retries bounded readback, and records cost', async () => {
  const root = mkdtempSync(join(tmpdir(), 'taskmarket-work-'));
  const ledger = join(root, 'earn-ledger.jsonl');
  const selected = task({
    id: '0x' + 'a'.repeat(64),
    description: [
      'The Maya mathematician who signed his own formula.',
      IMAGE_BRIEF,
      'Locked facts: 260, 365, 584, 780. His name is Sak Tahn Waax.',
    ].join(' '),
  });
  let submissionReads = 0;
  const submitCalls = [];
  const prompts = [];
  const sleeps = [];

  const result = await runTaskMarketPass({
    action: 'execute',
    aniccaHome: root,
    earnLedgerPath: ledger,
    wakeId: 'wake-taskmarket-1',
    now: NOW,
  }, {
    listTasks: async () => [selected],
    listSubmissions: async () => {
      submissionReads += 1;
      return submissionReads < 4
        ? []
        : [{ id: 'sub_taskmarket_1', taskId: selected.id }];
    },
    sleep: async (ms) => { sleeps.push(ms); },
    loadWalletKey: () => '0x' + '1'.repeat(64),
    generateImage: async ({ prompt }) => {
      prompts.push(prompt);
      return {
        url: 'https://cdn.blockrun.example/maya.png',
        model: 'openai/gpt-image-2',
        costUsd: 0.065,
        created: 1785230000,
      };
    },
    downloadImage: async () => squarePng(),
    submitTask: async (taskId, files) => {
      submitCalls.push({ taskId, files });
      return { ok: true };
    },
  });

  assert.deepEqual(result, {
    ok: true,
    action: 'submitted',
    taskId: selected.id,
    submissionId: 'sub_taskmarket_1',
    model: 'openai/gpt-image-2',
    costUsd: 0.065,
  });
  assert.equal(prompts.length, 1);
  assert.equal(submissionReads, 4);
  assert.deepEqual(sleeps, [1000, 1000]);
  assert.match(prompts[0], /Sak Tahn Waax/);
  assert.match(prompts[0], /260, 365, 584, 780/);
  assert.equal(submitCalls.length, 1);
  assert.equal(submitCalls[0].taskId, selected.id);
  assert.deepEqual(submitCalls[0].files.map((path) => path.split('/').at(-1)), [
    'hero.png',
    'concept-note.md',
    'sources.md',
  ]);
  assert.match(readFileSync(submitCalls[0].files[1], 'utf8'), /openai\/gpt-image-2/);
  assert.match(readFileSync(submitCalls[0].files[2], 'utf8'), new RegExp(selected.id));
  const rows = readFileSync(ledger, 'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(rows, [{
    ts: Math.floor(NOW / 1000),
    wake: 'wake-taskmarket-1',
    source: 'taskmarket_work_attempt',
    task: selected.id,
    earn_usdc: 0,
    cost_usdc: 0.065,
    net_usdc: -0.065,
    submission_id: 'sub_taskmarket_1',
    model: 'openai/gpt-image-2',
  }]);
});

test('runTaskMarketPass does not generate or submit an already-owned task', async () => {
  const root = mkdtempSync(join(tmpdir(), 'taskmarket-work-existing-'));
  const selected = task({ id: '0x' + 'b'.repeat(64) });
  let generated = false;
  let submitted = false;

  const result = await runTaskMarketPass({
    action: 'execute',
    taskId: selected.id,
    aniccaHome: root,
    earnLedgerPath: join(root, 'earn-ledger.jsonl'),
    wakeId: 'wake-taskmarket-2',
    now: NOW,
  }, {
    listTasks: async () => [selected],
    listSubmissions: async () => [{ id: 'sub_existing', taskId: selected.id }],
    loadWalletKey: () => '0x' + '1'.repeat(64),
    generateImage: async () => { generated = true; },
    downloadImage: async () => squarePng(),
    submitTask: async () => { submitted = true; },
  });

  assert.deepEqual(result, {
    ok: true,
    action: 'already_submitted',
    taskId: selected.id,
    submissionId: 'sub_existing',
    costUsd: 0,
  });
  assert.equal(generated, false);
  assert.equal(submitted, false);
});

test('runTaskMarketPass reconciles an existing official submit tx exactly once without new image cost', async () => {
  const root = mkdtempSync(join(tmpdir(), 'taskmarket-work-reconcile-'));
  const ledger = join(root, 'earn-ledger.jsonl');
  const selected = task({ id: '0x' + 'd'.repeat(64) });
  const submitTxHash = '0x' + 'e'.repeat(64);
  await import('node:fs/promises').then(({ writeFile }) => writeFile(ledger, `${JSON.stringify({
    ts: Math.floor(NOW / 1000),
    wake: 'wake-original',
    source: 'taskmarket_work_attempt',
    task: selected.id,
    earn_usdc: 0,
    cost_usdc: 0.065,
    net_usdc: -0.065,
    submission_id: null,
    model: 'openai/gpt-image-2',
  })}\n`));
  let generated = false;
  let submitted = false;
  const deps = {
    listTasks: async () => [selected],
    listSubmissions: async () => [{ taskId: selected.id, submitTxHash }],
    loadWalletKey: () => '0x' + '1'.repeat(64),
    generateImage: async () => { generated = true; },
    downloadImage: async () => squarePng(),
    submitTask: async () => { submitted = true; },
  };

  const first = await runTaskMarketPass({
    action: 'execute',
    aniccaHome: root,
    earnLedgerPath: ledger,
    wakeId: 'wake-reconcile-1',
    now: NOW,
  }, deps);
  const second = await runTaskMarketPass({
    action: 'execute',
    taskId: selected.id,
    aniccaHome: root,
    earnLedgerPath: ledger,
    wakeId: 'wake-reconcile-2',
    now: NOW + 1,
  }, deps);

  assert.deepEqual(first, {
    ok: true,
    action: 'reconciled_submission',
    taskId: selected.id,
    submissionId: submitTxHash,
    costUsd: 0,
  });
  assert.deepEqual(second, {
    ok: true,
    action: 'already_submitted',
    taskId: selected.id,
    submissionId: submitTxHash,
    costUsd: 0,
  });
  assert.equal(generated, false);
  assert.equal(submitted, false);
  const rows = readFileSync(ledger, 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[1], {
    ts: Math.floor(NOW / 1000),
    wake: 'wake-reconcile-1',
    source: 'taskmarket_work_reconciliation',
    task: selected.id,
    earn_usdc: 0,
    cost_usdc: 0,
    net_usdc: 0,
    submission_id: submitTxHash,
    reconciles_wake: 'wake-original',
  });
});

test('runTaskMarketPass fails closed after bounded retries when submit has no official readback', async () => {
  const root = mkdtempSync(join(tmpdir(), 'taskmarket-work-readback-'));
  const selected = task({ id: '0x' + 'c'.repeat(64) });
  let submissionReads = 0;
  const sleeps = [];
  await assert.rejects(
    runTaskMarketPass({
      action: 'execute',
      aniccaHome: root,
      earnLedgerPath: join(root, 'earn-ledger.jsonl'),
      wakeId: 'wake-taskmarket-3',
      now: NOW,
    }, {
      listTasks: async () => [selected],
      listSubmissions: async () => { submissionReads += 1; return []; },
      sleep: async (ms) => { sleeps.push(ms); },
      loadWalletKey: () => '0x' + '1'.repeat(64),
      generateImage: async () => ({
        url: 'https://cdn.blockrun.example/missing.png',
        model: 'openai/gpt-image-2',
        costUsd: 0.065,
      }),
      downloadImage: async () => squarePng(),
      submitTask: async () => ({ ok: true }),
    }),
    /submission_readback_missing/,
  );
  assert.equal(submissionReads, 6);
  assert.deepEqual(sleeps, [1000, 1000, 1000, 1000]);
});
