import { createServer } from 'node:http';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Readable } from 'node:stream';

import { openThe402Inbox } from './lib/the402-inbox.mjs';
import { handleThe402WebhookRequest } from './lib/the402-webhook-handler.mjs';
import { resolveThe402ConfigRoot, resolveThe402PublicOrigin, resolveX402StateDir } from './state-paths.mjs';

const HOST = '127.0.0.1';
const PORT = 8096;
const ROUTE = '/webhooks/the402';
const PUBLIC_ORIGIN = resolveThe402PublicOrigin();
const PUBLIC_URL = `${PUBLIC_ORIGIN}${ROUTE}`;
const CONFIG_ROOT = resolveThe402ConfigRoot();
const CREDENTIALS_PATH = join(CONFIG_ROOT, 'the402-credentials.json');
const SERVICE_PATH = join(CONFIG_ROOT, 'the402-service.json');
const EXPLAINER_SERVICE_PATH = join(CONFIG_ROOT, 'the402-service-http402.json');
const INBOX_PATH = join(resolveX402StateDir(), 'the402-inbox.sqlite');

const credentials = JSON.parse(readFileSync(CREDENTIALS_PATH, 'utf8'));
const service = JSON.parse(readFileSync(SERVICE_PATH, 'utf8'));
const explainerService = existsSync(EXPLAINER_SERVICE_PATH)
  ? JSON.parse(readFileSync(EXPLAINER_SERVICE_PATH, 'utf8'))
  : null;
const inbox = openThe402Inbox(INBOX_PATH);

function sendJson(response, status, body, extraHeaders = {}) {
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    ...extraHeaders,
  });
  response.end(JSON.stringify(body));
}

const server = createServer(async (request, response) => {
  const path = new URL(request.url || '/', PUBLIC_URL).pathname;
  if (path !== ROUTE) {
    sendJson(response, 404, { ok: false, error: 'not_found' });
    return;
  }
  if (request.method === 'GET') {
    sendJson(response, 200, {
      ok: true,
      participantId: credentials.participant_id,
      inbox: inbox.stats(),
    });
    return;
  }

  try {
    const webRequest = new Request(PUBLIC_URL, {
      method: request.method,
      headers: request.headers,
      body: Readable.toWeb(request),
      duplex: 'half',
    });
    const result = await handleThe402WebhookRequest(webRequest, {
      inbox,
      apiKey: credentials.api_key,
      webhookSecret: credentials.webhook_secret,
      // The current provider guide authenticates dispatches with X-Platform-Secret.
      // Keep strict HMAC verification as the fallback when that header is absent.
      allowApiKeyOnly: true,
      allowUnsignedTestProbe: true,
      expectedTestServiceId: [
        service.service_id || service.id,
        explainerService?.service_id || explainerService?.id,
      ].filter(Boolean),
      onRejected: (reason) => process.stderr.write(`${reason}\n`),
    });
    const headers = Object.fromEntries(result.headers.entries());
    response.writeHead(result.status, headers);
    response.end(Buffer.from(await result.arrayBuffer()));
  } catch {
    sendJson(response, 503, { ok: false, error: 'temporarily_unavailable' });
  }
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`${JSON.stringify({ ready: true, host: HOST, port: PORT, route: ROUTE })}\n`);
});

function shutdown() {
  server.close(() => {
    inbox.close();
    process.exit(0);
  });
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
