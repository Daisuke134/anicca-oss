# x402 endpoint

This directory contains the repository-owned cloud x402 service. Railway builds
it with Nixpacks from `railway.toml` and starts `src/server.js`; Docker and local
static launchd plists are not part of the supported runtime.

## Run locally

```bash
cd services/x402-endpoint
npm ci
npm start
```

The service exposes `/health` and the paid x402 routes implemented by
`src/server.js`. Credentials and mutable state belong outside Git.

## Public reachability

The Mac host's currently supported public bridge is the independently owned
`ai.anicca.tsbridge` service. The retired Cloudflare quick-tunnel and Slack-tail
jobs are intentionally absent; do not recreate their OpenClaw cron or static
plists. Life Manager loop lifecycle changes go through `bin/lm-loop`.

## Deployment

`railway.toml` is the deployment declaration and selects Nixpacks. Its start
command is:

```text
npx prisma generate && node src/server.js
```

Provider effects must retain official receipts and idempotency evidence in the
external durable state owned by the deployment.
