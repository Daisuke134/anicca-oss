"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const path = require("node:path");

const { makeProductionDeps, runBrowserAuthProductionE2E } = require("./browser-auth-production-e2e.js");

const REQUIRED_ENV = {
  BROWSER_AUTH_PRODUCTION_ORIGIN: 'https://app.example.test',
  BROWSER_AUTH_TENANT_A_UID: 'tenant-a',
  BROWSER_AUTH_TENANT_B_UID: 'tenant-b',
  LM_BROWSER_SESSION_KEY: 'ab'.repeat(32),
  LM_FEEDBACK_DATABASE_URL: 'postgresql://user:password@db.example.test/life_manager',
  LM_TELEGRAM_BOT_TOKEN: 'telegram-bot-token-secret',
  BROWSER_AUTH_TELEGRAM_CHAT_ID: '12345',
};
const RAW_VALUES = [
  'opaque-tenant-marker-a',
  'opaque-tenant-marker-b',
  'cookie-a-secret',
  'provider-body-secret',
];
const OUTPUT_KEYS = [
  'mode', 'tenant_count', 'origin', 'context_hashes', 'job_ids',
  'steel_session_ids', 'telegram_evidence_ids', 'released',
];

test('runtime image allowlists the browser auth production entrypoint', () => {
  const dockerignore = readFileSync(path.join(__dirname, '..', '.dockerignore'), 'utf8');
  assert.match(dockerignore, /^!scripts\/browser-auth-production-e2e\.js$/m);
});

function terminalFor({ id, markerHash, mode, tenant }) {
  return {
    id,
    uid: tenant,
    status: mode === 'verify-expired-handoff' ? 'handoff_required' : 'completed',
    auth_marker_hash: markerHash,
    receipt: {
      auth_marker_hash: markerHash,
      session_id: `steel-${tenant}`,
      evidence_message_id: `telegram-${tenant}-${id}`,
      steel_released: mode !== 'verify-expired-handoff',
      provider_receipt: {
        status: mode === 'verify-expired-handoff' ? 'login_required' : 'authenticated',
        confirmed: mode !== 'verify-expired-handoff',
        handoff_required: mode === 'verify-expired-handoff',
        handoff_reason: mode === 'verify-expired-handoff' ? 'login' : null,
      },
    },
    trace: [{ stage: 'auth_context_loaded', meta: { loaded: true } }],
  };
}

function makeDeps({ mutateTerminal } = {}) {
  const calls = [];
  const jobs = new Map();
  let sequence = 0;
  const call = (name, value) => {
    calls.push({ name, value });
    return value;
  };
  return {
    calls,
    durableQueue: {
      async enqueue({ tenant, uid, markerHash, mode }) {
        assert.equal(typeof markerHash, 'string');
        assert.match(markerHash, /^[a-f0-9]{64}$/);
        const id = `job-${tenant}-${++sequence}`;
        jobs.set(id, { id, uid, tenant, mode, auth_marker_hash: markerHash, status: 'queued' });
        return call('durableQueue.enqueue', { id, auth_marker_hash: markerHash });
      },
      async read({ id }) {
        return call('durableQueue.read', jobs.get(id) || null);
      },
    },
    executor: {
      async run({ jobId }) {
        const job = jobs.get(jobId);
        call('executor.claim', { id: jobId });
        call('executor.execute', { id: jobId });
        const terminal = terminalFor({
          id: jobId,
          markerHash: job.auth_marker_hash,
          mode: job.mode,
          tenant: job.tenant,
        });
        jobs.set(jobId, mutateTerminal ? mutateTerminal(terminal, job) : terminal);
        return call('executor.finish', { id: jobId });
      },
    },
  };
}

function makeProductionAdapterBoundaries() {
  const sqlCalls = [];
  let row = null;
  const query = async (sql, params) => {
    sqlCalls.push({ sql, params });
    if (/INSERT INTO public\.lm_browser_jobs/i.test(sql)) {
      row = {
        id: 'job-production-adapter-1',
        uid: params[0],
        telegram_chat_id: params[1],
        telegram_message_id: params[2],
        telegram_update_id: params[3],
        prompt_hash: params[4],
        goal: params[5],
        locale: params[6],
        action_kind: params[7],
        requires_login: params[8],
        principal_kind: params[9],
        auth_marker_hash: params[10],
        status: params[11],
        trace: [],
      };
      return { rows: [row] };
    }
    if (/claim_lm_browser_job_by_id/i.test(sql)) {
      assert.equal(params[0], row.id);
      assert.equal(row.status, 'queued');
      row.status = 'claimed';
      return { rows: [row] };
    }
    if (/append_lm_browser_job_trace/i.test(sql)) {
      row.trace.push({ stage: params[1], meta: JSON.parse(params[2]) });
      return { rows: [row] };
    }
    if (/finish_lm_browser_job/i.test(sql)) {
      assert.equal(params[0], row.id);
      assert.equal(row.status, 'claimed');
      row.status = params[1];
      row.receipt = JSON.parse(params[2]);
      return { rows: [row] };
    }
    if (/SELECT id, uid, status, auth_marker_hash, receipt, trace, telegram_result_message_id FROM public\.lm_browser_jobs/i.test(sql)) {
      assert.equal(params[0], row.id);
      return { rows: [row] };
    }
    throw new Error(`unexpected SQL: ${sql}`);
  };
  const driver = {
    async openSession() { return { id: 'steel-production-adapter-1' }; },
    async discoverAndAct() {
      return {
        selectedUrl: 'https://app.example.test/dashboard',
        selectedOrigin: 'https://app.example.test',
        selectionReason: 'existing provider page',
        action: 'read authenticated state',
        sideEffectStarted: false,
      };
    },
    async readProviderReceipt() {
      return { confirmed: false, status: 'login_required', handoffRequired: true, handoffReason: 'login' };
    },
    async captureEvidence() { return { bytes: Buffer.from('evidence') }; },
    async releaseSession(_id, { providerReceipt }) {
      assert.equal(providerReceipt.handoff_reason, 'login');
      return {
        released: true,
        origin: 'https://app.example.test',
        principal_kind: 'agent_owned',
        auth_context_loaded: true,
        auth_context_invalidated: true,
      };
    },
  };
  return {
    sqlCalls,
    query,
    driver,
    async sendMessage() { return { ok: true, result: { message_id: 11 } }; },
    async sendPhoto() { return { ok: true, result: { message_id: 12 } }; },
  };
}

function assertBoundedResult(result, mode) {
  assert.deepEqual(Object.keys(result).sort(), OUTPUT_KEYS.slice().sort());
  assert.equal(result.mode, mode);
  assert.equal(result.tenant_count, 2);
  assert.equal(result.origin, REQUIRED_ENV.BROWSER_AUTH_PRODUCTION_ORIGIN);
  assert.equal(result.context_hashes.length, 2);
  assert.equal(new Set(result.context_hashes).size, 2);
  assert.equal(result.job_ids.length, 2);
  assert.equal(result.steel_session_ids.length, 2);
  assert.equal(result.telegram_evidence_ids.length, 2);
  assert.equal(result.released, mode !== 'verify-expired-handoff');
}

function assertNoSecrets(output) {
  for (const [name, secret] of Object.entries(REQUIRED_ENV)) {
    if (name === 'BROWSER_AUTH_PRODUCTION_ORIGIN' || name.endsWith('_UID') || name === 'BROWSER_AUTH_TELEGRAM_CHAT_ID') continue;
    assert.doesNotMatch(output, new RegExp(secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  for (const secret of RAW_VALUES) {
    assert.doesNotMatch(output, new RegExp(secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
}

test('rejects an unknown mode without reflecting its value', async () => {
  const mode = `unknown-${RAW_VALUES[2]}`;
  await assert.rejects(
    runBrowserAuthProductionE2E({ mode, env: REQUIRED_ENV, deps: makeDeps() }),
    (error) => {
      assert.equal(error.message, 'Unknown browser auth production E2E mode');
      assertNoSecrets(error.message);
      return true;
    },
  );
});

test('rejects missing required environment variables by name only', async () => {
  const env = { ...REQUIRED_ENV };
  delete env.LM_BROWSER_SESSION_KEY;
  delete env.BROWSER_AUTH_TENANT_B_UID;
  await assert.rejects(
    runBrowserAuthProductionE2E({ mode: 'seed-two-tenant-contexts', env, deps: makeDeps() }),
    (error) => {
      assert.match(error.message, /BROWSER_AUTH_TENANT_B_UID/);
      assert.match(error.message, /LM_BROWSER_SESSION_KEY/);
      assertNoSecrets(error.message);
      return true;
    },
  );
});

test('CLI never reflects a secret-like unknown mode to stdout or stderr', () => {
  const child = spawnSync(
    process.execPath,
    [path.join(__dirname, "browser-auth-production-e2e.js"), `unknown-${RAW_VALUES[2]}`],
    { encoding: 'utf8', env: { ...process.env, ...REQUIRED_ENV } },
  );
  assert.equal(child.status, 1);
  assert.equal(child.stdout, '');
  assert.equal(child.stderr, 'Unknown browser auth production E2E mode\n');
  assertNoSecrets(`${child.stdout}${child.stderr}`);
});

test('production adapter uses real durable store and browser-job runtime from claim through persisted terminal readback', async () => {
  const boundaries = makeProductionAdapterBoundaries();
  const deps = makeProductionDeps(REQUIRED_ENV, boundaries);
  const markerHash = 'a'.repeat(64);
  const queued = await deps.durableQueue.enqueue({
    tenant: 'tenant-a', markerHash, origin: REQUIRED_ENV.BROWSER_AUTH_PRODUCTION_ORIGIN,
  });
  assert.deepEqual(queued, { id: 'job-production-adapter-1', auth_marker_hash: markerHash });

  const execution = await deps.executor.run({ jobId: queued.id });
  assert.equal(execution.trace_id, queued.id);
  const terminal = await deps.durableQueue.read({ id: queued.id });

  assert.equal(terminal.status, 'handoff_required');
  assert.equal(terminal.auth_marker_hash, markerHash);
  assert.equal(terminal.receipt.auth_marker_hash, markerHash);
  assert.equal(terminal.receipt.provider_receipt.handoff_required, true);
  assert.equal(terminal.receipt.steel_released, false);
  assert.equal(terminal.receipt.evidence_message_id, '12');
  assert.equal(terminal.trace.some((entry) => entry.stage === 'auth_context_invalidated'), false);
  const steps = boundaries.sqlCalls.map(({ sql }) => {
    if (/INSERT INTO public\.lm_browser_jobs/i.test(sql)) return 'enqueue';
    if (/claim_lm_browser_job_by_id/i.test(sql)) return 'claim';
    if (/finish_lm_browser_job/i.test(sql)) return 'finish';
    if (/SELECT id, uid, status, auth_marker_hash, receipt, trace, telegram_result_message_id FROM public\.lm_browser_jobs/i.test(sql)) return 'read';
    return 'trace';
  });
  assert.ok(steps.indexOf('enqueue') < steps.indexOf('claim'));
  assert.ok(steps.indexOf('claim') < steps.indexOf('finish'));
  assert.ok(steps.indexOf('finish') < steps.lastIndexOf('read'));
});

test('executes the durable claim → runtime → finish → terminal readback path before emitting hashes', async () => {
  const deps = makeDeps();
  const result = await runBrowserAuthProductionE2E({
    mode: 'verify-two-tenant-contexts', env: REQUIRED_ENV, deps,
  });

  assertBoundedResult(result, 'verify-two-tenant-contexts');
  assert.deepEqual(
    deps.calls.map(({ name }) => name),
    [
      'durableQueue.enqueue', 'executor.claim', 'executor.execute', 'executor.finish', 'durableQueue.read',
      'durableQueue.enqueue', 'executor.claim', 'executor.execute', 'executor.finish', 'durableQueue.read',
    ],
  );
  assertNoSecrets(JSON.stringify(result));
});

test('verifies one real provider context without requiring a second provider account', async () => {
  const env = { ...REQUIRED_ENV };
  delete env.BROWSER_AUTH_TENANT_B_UID;
  const deps = makeDeps();

  const result = await runBrowserAuthProductionE2E({
    mode: 'verify-provider-context',
    env,
    deps,
  });

  assert.equal(result.mode, 'verify-provider-context');
  assert.equal(result.tenant_count, 1);
  assert.equal(result.context_hashes.length, 1);
  assert.equal(result.job_ids.length, 1);
  assert.equal(result.steel_session_ids.length, 1);
  assert.equal(result.telegram_evidence_ids.length, 1);
  assert.equal(result.released, true);
  assert.deepEqual(
    deps.calls.map(({ name }) => name),
    [
      'durableQueue.enqueue',
      'executor.claim',
      'executor.execute',
      'executor.finish',
      'durableQueue.read',
    ],
  );
  assertNoSecrets(JSON.stringify(result));
});

test('provider verification rejects a released but unauthenticated terminal job', async () => {
  await assert.rejects(
    runBrowserAuthProductionE2E({
      mode: 'verify-provider-context',
      env: { ...REQUIRED_ENV, BROWSER_AUTH_TENANT_B_UID: undefined },
      deps: makeDeps({
        mutateTerminal: (terminal) => ({
          ...terminal,
          status: 'handoff_required',
          receipt: {
            ...terminal.receipt,
            provider_receipt: {
              status: 'login_required',
              confirmed: false,
              handoff_required: true,
              handoff_reason: 'login',
            },
          },
        }),
      }),
    }),
    /authenticated provider readback/,
  );
});

test('rejects nonterminal jobs, missing provider receipts, and unreleased Steel rows', async () => {
  const cases = [
    ['nonterminal', (terminal) => ({ ...terminal, status: 'claimed' })],
    ['missing receipt', (terminal) => ({ ...terminal, receipt: null })],
    ['unreleased Steel', (terminal) => ({ ...terminal, receipt: { ...terminal.receipt, steel_released: false } })],
  ];
  for (const [label, mutateTerminal] of cases) {
    await assert.rejects(
      runBrowserAuthProductionE2E({
        mode: 'verify-two-tenant-contexts', env: REQUIRED_ENV, deps: makeDeps({ mutateTerminal }),
      }),
      new RegExp(label === 'nonterminal' ? 'terminal' : label === 'missing receipt' ? 'provider receipt' : 'Steel release'),
    );
  }
});

test('rejects body-only login text and requires a structured expired handoff with a held session', async () => {
  await assert.rejects(
    runBrowserAuthProductionE2E({
      mode: 'verify-expired-handoff',
      env: REQUIRED_ENV,
      deps: makeDeps({
        mutateTerminal: (terminal) => ({
          ...terminal,
          receipt: {
            ...terminal.receipt,
            provider_receipt: { status: `login ${RAW_VALUES[3]}`, body: 'login', handoff_required: false },
          },
        }),
      }),
    }),
    /structured provider login handoff/,
  );
  await assert.rejects(
    runBrowserAuthProductionE2E({
      mode: 'verify-expired-handoff',
      env: REQUIRED_ENV,
      deps: makeDeps({
        mutateTerminal: (terminal) => ({
          ...terminal,
          receipt: { ...terminal.receipt, steel_released: true },
        }),
      }),
    }),
    /session was not held/,
  );
});

test('rejects missing, same, or unbound durable terminal marker hashes', async () => {
  const cases = [
    ['missing', (terminal) => ({ ...terminal, auth_marker_hash: null })],
    ['unbound', (terminal) => ({ ...terminal, receipt: { ...terminal.receipt, auth_marker_hash: 'f'.repeat(64) } })],
    ['same', (terminal) => ({ ...terminal, auth_marker_hash: 'e'.repeat(64), receipt: { ...terminal.receipt, auth_marker_hash: 'e'.repeat(64) } })],
  ];
  for (const [label, mutateTerminal] of cases) {
    await assert.rejects(
      runBrowserAuthProductionE2E({
        mode: 'verify-two-tenant-contexts', env: REQUIRED_ENV, deps: makeDeps({ mutateTerminal }),
      }),
      new RegExp(label === 'same' ? 'isolated' : 'marker hash'),
    );
  }
});

test('handles an expired login only after terminal provider readback and evidence while holding Steel for handoff', async () => {
  const deps = makeDeps();
  const result = await runBrowserAuthProductionE2E({
    mode: 'verify-expired-handoff', env: REQUIRED_ENV, deps,
  });
  assertBoundedResult(result, 'verify-expired-handoff');
  assertNoSecrets(JSON.stringify(result));
});
