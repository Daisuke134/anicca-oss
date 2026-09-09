#!/usr/bin/env node
import { appendFileSync, chmodSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { runAcquisitionCycle } from './lib/acquisition-controller.mjs';
import { openThe402Inbox } from './lib/the402-inbox.mjs';
import { resolveThe402ConfigRoot, resolveX402StateDir } from './state-paths.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));

function serviceId(path) {
  const body = JSON.parse(readFileSync(path, 'utf8'));
  const id = body.service_id || body.id;
  if (typeof id !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(id)) throw new Error('invalid service state');
  return id;
}

function actionAppender(path) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  return (row) => {
    appendFileSync(path, `${JSON.stringify(row)}\n`, { mode: 0o600 });
    chmodSync(path, 0o600);
  };
}

async function main() {
  const configRoot = resolveThe402ConfigRoot();
  const stateDir = resolveX402StateDir();
  const credentials = JSON.parse(readFileSync(join(configRoot, 'the402-credentials.json'), 'utf8'));
  if (typeof credentials.api_key !== 'string' || credentials.api_key.length < 16) throw new Error('invalid credentials');
  const inbox = openThe402Inbox(join(stateDir, 'the402-inbox.sqlite'));
  try {
    const result = await runAcquisitionCycle({
      inbox,
      apiKey: credentials.api_key,
      researchServiceId: serviceId(join(configRoot, 'the402-service.json')),
      explainerServiceId: serviceId(join(configRoot, 'the402-service-http402.json')),
      appendAction: actionAppender(join(stateDir, 'x402-acquisition-actions.jsonl')),
    });
    process.stdout.write(`${JSON.stringify({
      observed_at: new Date().toISOString(),
      ...result,
      inbox: inbox.stats(),
    })}\n`);
  } finally {
    inbox.close();
  }
}

const isEntry = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntry) {
  main().catch(() => {
    process.stderr.write('{"ok":false,"error":"acquisition_controller_failed"}\n');
    process.exitCode = 1;
  });
}

export { HERE };
