#!/usr/bin/env node
import express from 'express';
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createPublicClient, formatUnits, http, isAddress } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { base } from 'viem/chains';

import { loadEvmKey } from '../lib/resolve-identity.mjs';
import { IMAGE_OFFER, imageResaleHandler } from './image-resale.mjs';
import { decodePayer, decodeTransaction, isSettled } from './lib/settle-gate.mjs';
import { ensureFacilitatorInitialized } from './lib/facilitator-init.mjs';
import { resolveX402StateDir } from './state-paths.mjs';

const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const HERE = dirname(fileURLToPath(import.meta.url));
const USDC_BALANCE_OFFER = Object.freeze({
  path: '/base-usdc-balance',
  method: 'GET',
  price: '$0.003',
  what: 'finalized Base USDC balance',
  description: 'Return the finalized Base USDC balance for one EVM address.',
});
const ERC20_BALANCE_ABI = [{
  type: 'function',
  name: 'balanceOf',
  stateMutability: 'view',
  inputs: [{ name: 'account', type: 'address' }],
  outputs: [{ name: '', type: 'uint256' }],
}];

export function imageProduct({ publicUrl = '', payTo = '' } = {}) {
  const origin = String(publicUrl).replace(/\/+$/, '');
  return {
    ...IMAGE_OFFER,
    method: 'POST',
    payTo,
    resource: origin ? `${origin}${IMAGE_OFFER.path}` : IMAGE_OFFER.path,
  };
}

export function usdcBalanceProduct({ publicUrl = '', payTo = '' } = {}) {
  const origin = String(publicUrl).replace(/\/+$/, '');
  return {
    ...USDC_BALANCE_OFFER,
    payTo,
    resource: origin ? `${origin}${USDC_BALANCE_OFFER.path}` : USDC_BALANCE_OFFER.path,
  };
}

export function makeUsdcBalanceHandler({
  rpcUrl = 'https://mainnet.base.org',
  createClient = ({ url }) => createPublicClient({ chain: base, transport: http(url) }),
} = {}) {
  return async function usdcBalanceHandler(req, res) {
    const address = String(req.query?.address || '');
    if (!isAddress(address)) return res.status(400).json({ error: 'pass ?address=0x...' });
    try {
      const client = createClient({ url: rpcUrl });
      const block = await client.getBlock({ blockTag: 'finalized' });
      const atomic = await client.readContract({
        address: USDC_BASE,
        abi: ERC20_BALANCE_ABI,
        functionName: 'balanceOf',
        args: [address],
        blockNumber: block.number,
      });
      return res.json({
        chain_id: 8453,
        asset: USDC_BASE,
        address,
        balance_atomic: atomic.toString(),
        balance_usdc: formatUnits(atomic, 6),
        finalized_block: block.number.toString(),
      });
    } catch {
      return res.status(502).json({ error: 'base_rpc_unavailable' });
    }
  };
}

export function imageDiscoveryConfig() {
  return {
    method: 'POST',
    bodyType: 'json',
    input: { prompt: 'A blue robot building a self-funded agent economy' },
    inputSchema: {
      properties: { prompt: { type: 'string', description: 'Image prompt, 1..2000 characters' } },
      required: ['prompt'],
    },
    output: { example: { url: 'https://cdn.example/generated.png' } },
  };
}

export function usdcBalanceDiscoveryConfig() {
  return {
    method: 'GET',
    input: { address: '0x0000000000000000000000000000000000000001' },
    inputSchema: {
      properties: {
        address: {
          type: 'string',
          pattern: '^0x[0-9a-fA-F]{40}$',
          description: 'EVM address whose finalized Base USDC balance will be returned',
        },
      },
      required: ['address'],
    },
    output: {
      example: {
        chain_id: 8453,
        balance_atomic: '0',
        balance_usdc: '0',
        finalized_block: '0',
      },
    },
  };
}

export function mergeImageOpenApi(upstream, product) {
  if (!upstream || typeof upstream !== 'object' || !upstream.paths || typeof upstream.paths !== 'object') {
    throw new TypeError('upstream OpenAPI document must contain paths');
  }
  const existingPath = upstream.paths[product.path] || {};
  return {
    ...upstream,
    paths: {
      ...upstream.paths,
      [product.path]: {
        ...existingPath,
        post: {
          operationId: 'generateImage',
          summary: 'Generate an image from a text prompt',
          description: product.description,
          requestBody: {
            required: true,
            content: {
              'application/json': {
                schema: {
                  type: 'object',
                  properties: {
                    prompt: { type: 'string', minLength: 1, maxLength: 2000 },
                  },
                  required: ['prompt'],
                  additionalProperties: false,
                },
              },
            },
          },
          'x-payment-info': {
            price: { mode: 'fixed', currency: 'USD', amount: String(product.price).replace(/^\$/, '') },
            protocols: [{ x402: {} }],
          },
          responses: {
            200: {
              description: 'Generated image URL',
              content: {
                'application/json': {
                  schema: {
                    type: 'object',
                    properties: { url: { type: 'string', format: 'uri' } },
                    required: ['url'],
                  },
                },
              },
            },
            402: { description: 'Payment Required' },
          },
        },
      },
    },
  };
}

export function createImageTelemetryRecorder({ stateDir, payTo }) {
  if (!stateDir) throw new TypeError('stateDir is required');
  if (!payTo) throw new TypeError('payTo is required');
  const wallet = String(payTo).toLowerCase();
  const attemptsLog = join(stateDir, `attempts-${wallet}.jsonl`);
  const salesLog = join(stateDir, `sales-${wallet}.jsonl`);
  try { mkdirSync(stateDir, { recursive: true }); } catch { /* best-effort telemetry */ }
  return (row) => {
    const safeRow = {
      ts: String(row.ts),
      route: String(row.route),
      price: String(row.price),
      payer: row.payer ? String(row.payer) : null,
      tx: row.tx ? String(row.tx) : null,
      settled: row.settled === true,
      status: Number(row.status),
    };
    try {
      appendFileSync(safeRow.settled ? salesLog : attemptsLog, `${JSON.stringify(safeRow)}\n`);
    } catch { /* logging must never break serving */ }
  };
}

function paymentPayer(req) {
  try {
    const decoded = JSON.parse(Buffer.from(req.header('x-payment') || '', 'base64').toString('utf8'));
    return decoded?.payload?.authorization?.from || null;
  } catch { return null; }
}

export function createImageApp({
  product,
  balanceProduct,
  paymentGate,
  handler = imageResaleHandler,
  balanceHandler = makeUsdcBalanceHandler(),
  loadUpstreamOpenApi,
  recordAccess,
}) {
  if (!product) throw new TypeError('product is required');
  if (typeof paymentGate !== 'function') throw new TypeError('paymentGate is required');
  if (recordAccess !== undefined && typeof recordAccess !== 'function') throw new TypeError('recordAccess must be a function');
  const app = express();
  app.use(express.json({ limit: '16kb' }));
  const products = [product, balanceProduct].filter(Boolean);
  app.get('/.well-known/x402.json', (_req, res) => res.json({
    x402Version: 2,
    resources: products.map((candidate) => ({
      resource: candidate.resource,
      method: candidate.method,
      price: candidate.price,
      network: 'eip155:8453',
      payTo: candidate.payTo,
      asset: USDC_BASE,
      description: candidate.description,
    })),
  }));
  app.get('/', (_req, res) => res.json({
    products: products.map((candidate) => ({
      path: candidate.path,
      method: candidate.method,
      price: candidate.price,
      what: candidate.what,
    })),
    manifest: '/.well-known/x402.json',
  }));
  app.get('/openapi.json', async (_req, res) => {
    try {
      if (typeof loadUpstreamOpenApi !== 'function') throw new Error('upstream loader missing');
      const document = mergeImageOpenApi(await loadUpstreamOpenApi(), product);
      if (balanceProduct) {
        document.paths[balanceProduct.path] = {
          get: {
            operationId: 'getFinalizedBaseUsdcBalance',
            summary: balanceProduct.what,
            description: balanceProduct.description,
            parameters: [{
              in: 'query',
              name: 'address',
              required: true,
              schema: { type: 'string', pattern: '^0x[0-9a-fA-F]{40}$' },
            }],
            'x-payment-info': {
              price: { mode: 'fixed', currency: 'USD', amount: String(balanceProduct.price).replace(/^\$/, '') },
              protocols: [{ x402: {} }],
            },
            responses: {
              200: { description: 'Finalized Base USDC balance' },
              400: { description: 'Invalid address' },
              402: { description: 'Payment Required' },
            },
          },
        };
      }
      res.json(document);
    } catch {
      res.status(502).json({ error: 'upstream_openapi_unavailable' });
    }
  });
  if (recordAccess) {
    app.use((req, res, next) => {
      const matched = products.find((candidate) => candidate.path === req.path);
      if (!matched) return next();
      const requestPayer = paymentPayer(req);
      res.on('finish', () => {
        const paymentResponse = res.getHeader('PAYMENT-RESPONSE');
        recordAccess({
          ts: new Date().toISOString(),
          route: req.path,
          price: matched.price,
          payer: requestPayer || decodePayer(paymentResponse),
          tx: decodeTransaction(paymentResponse),
          settled: isSettled(paymentResponse),
          status: res.statusCode,
        });
      });
      next();
    });
  }
  app.use(paymentGate);
  app.post(product.path, handler);
  if (balanceProduct) app.get(balanceProduct.path, balanceHandler);
  return app;
}

function resolvePayTo(env) {
  if (env.X402_PAYTO) return env.X402_PAYTO;
  const key = loadEvmKey({ env });
  if (!key) throw new Error('set X402_PAYTO (no per-instance EVM key resolvable)');
  return privateKeyToAccount(key).address;
}

async function createRuntimePaymentGate(products, env) {
  const { paymentMiddleware, x402ResourceServer } = await import('@x402/express');
  const { HTTPFacilitatorClient } = await import('@x402/core/server');
  const { ExactEvmScheme } = await import('@x402/evm/exact/server');
  const { declareDiscoveryExtension } = await import('@x402/extensions/bazaar');

  let facilitatorClient;
  if (env.CDP_API_KEY_ID && env.CDP_API_KEY_SECRET) {
    const { createFacilitatorConfig } = await import('@coinbase/x402');
    const config = createFacilitatorConfig(env.CDP_API_KEY_ID, env.CDP_API_KEY_SECRET);
    facilitatorClient = new HTTPFacilitatorClient({
      url: config.url,
      createAuthHeaders: config.createAuthHeaders,
    });
  } else {
    facilitatorClient = new HTTPFacilitatorClient({ url: 'https://x402.org/facilitator' });
  }
  const resourceServer = new x402ResourceServer(facilitatorClient)
    .register('eip155:8453', new ExactEvmScheme());
  const routes = Object.fromEntries(products.map((product) => [
    `${product.method} ${product.path}`,
    {
      accepts: [{
        scheme: 'exact',
        price: product.price,
        network: 'eip155:8453',
        payTo: product.payTo,
        extra: { asset: USDC_BASE },
      }],
      resource: product.resource,
      description: product.description,
      mimeType: 'application/json',
      extensions: declareDiscoveryExtension(
        product.path === USDC_BALANCE_OFFER.path
          ? usdcBalanceDiscoveryConfig()
          : imageDiscoveryConfig(),
      ),
    },
  ]));
  // See lib/facilitator-init.mjs: we initialize the resourceServer ourselves (retrying, never an
  // unhandled rejection) and only then hand it to paymentMiddleware with syncFacilitatorOnStart=
  // false. syncFacilitatorOnStart=false alone (no explicit initialize()) makes the library skip
  // calling initialize() entirely, forever -- every request fails with "Facilitator does not
  // support exact..." (confirmed live 2026-07-25, this was the bug in our first attempt at this).
  await ensureFacilitatorInitialized(resourceServer);
  return paymentMiddleware(routes, resourceServer, undefined, undefined, false);
}

async function main(env = process.env) {
  const product = imageProduct({
    publicUrl: env.X402_IMAGE_PUBLIC_URL || env.X402_PUBLIC_URL || '',
    payTo: resolvePayTo(env),
  });
  const balanceProduct = env.X402_BASE_USDC_BALANCE_ENABLED === '1'
    ? usdcBalanceProduct({
      publicUrl: env.X402_IMAGE_PUBLIC_URL || env.X402_PUBLIC_URL || '',
      payTo: product.payTo,
    })
    : null;
  const paymentGate = await createRuntimePaymentGate([product, balanceProduct].filter(Boolean), env);
  const upstreamOpenApi = env.X402_IMAGE_UPSTREAM_OPENAPI;
  if (!upstreamOpenApi) throw new Error('set X402_IMAGE_UPSTREAM_OPENAPI');
  const app = createImageApp({
    product,
    balanceProduct,
    paymentGate,
    recordAccess: createImageTelemetryRecorder({
      stateDir: resolveX402StateDir(env),
      payTo: product.payTo,
    }),
    loadUpstreamOpenApi: async () => {
      const response = await fetch(upstreamOpenApi, { signal: AbortSignal.timeout(10_000) });
      if (!response.ok) throw new Error(`upstream OpenAPI HTTP ${response.status}`);
      return response.json();
    },
  });
  const port = Number(env.X402_IMAGE_PORT || 8093);
  app.listen(port, '127.0.0.1', () => {
    process.stdout.write(`${JSON.stringify({ status: 'up', port, product })}\n`);
  });
}

const isEntry = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntry) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
