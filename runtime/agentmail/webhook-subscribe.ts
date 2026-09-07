// SPDX-License-Identifier: MIT
// runtime/agentmail/webhook-subscribe.ts — one-shot: register the webhook URL with
// AgentMail so external mail triggers POST to our :8810 receiver.
//
// Spec 10 — microtask 10.T3 + 10.T4.
//
// Public URL precedence:
//   1. process.env.WEBHOOK_PUBLIC_URL   (static — Netlify Function, fly.io, etc.)
//   2. Live `cloudflared tunnel --url http://localhost:8810` (auto-extract trycloudflare URL)
//
// Idempotent: passes clientId=spec-10-agentmail-webhook so re-running updates instead of
// duplicating.
//
// On success prints both the public URL and the signing secret (whsec_...) so it
// can be saved in ~/.local/state/life-manager/.env as AGENTMAIL_WEBHOOK_SECRET.
import { AgentMailClient } from "agentmail";
import { spawn } from "node:child_process";

const API_KEY = process.env.AGENTMAIL_API_KEY;
if (!API_KEY) {
  console.error("AGENTMAIL_API_KEY missing");
  process.exit(1);
}

const PORT = Number.parseInt(process.env.AGENTMAIL_WEBHOOK_PORT ?? "8810", 10);
const STATIC_URL = process.env.WEBHOOK_PUBLIC_URL;

async function obtainPublicUrl(): Promise<string> {
  if (STATIC_URL) {
    console.log(`using static WEBHOOK_PUBLIC_URL=${STATIC_URL}`);
    return STATIC_URL.replace(/\/+$/, "") + "/agentmail";
  }

  // Spin up a quick-tunnel via cloudflared. Output goes to stderr.
  console.log(`spawning cloudflared quick-tunnel → http://localhost:${PORT}`);
  const cf = spawn("cloudflared", ["tunnel", "--url", `http://localhost:${PORT}`], {
    stdio: ["ignore", "pipe", "pipe"],
  });

  return new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => {
      cf.kill("SIGTERM");
      reject(new Error("cloudflared did not print a public URL within 30s"));
    }, 30_000);

    const onChunk = (buf: Buffer): void => {
      const text = buf.toString("utf8");
      process.stdout.write(text);
      const match = text.match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/i);
      if (match) {
        clearTimeout(timer);
        // We intentionally leave the tunnel running. Caller stops it.
        resolve(match[0].replace(/\/+$/, "") + "/agentmail");
      }
    };
    cf.stdout?.on("data", onChunk);
    cf.stderr?.on("data", onChunk);
    cf.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

const publicUrl = await obtainPublicUrl();
console.log(`\npublic URL → ${publicUrl}`);

const client = new AgentMailClient({ apiKey: API_KEY });

try {
  const webhook = await client.webhooks.create({
    url: publicUrl,
    eventTypes: ["message.received"],
    clientId: "spec-10-agentmail-webhook",
  });

  const hook = webhook as { webhookId?: string; secret?: string; url?: string };
  console.log(`\n+ webhook registered`);
  console.log(`  webhook_id: ${hook.webhookId ?? "?"}`);
  console.log(`  url:        ${hook.url ?? publicUrl}`);
  if (hook.secret) {
    console.log(`  secret:     ${hook.secret}`);
    console.log(`\n→ add to ~/.local/state/life-manager/.env:`);
    console.log(`     AGENTMAIL_WEBHOOK_SECRET=${hook.secret}`);
  } else {
    console.log(`  secret:     <not returned — fetch via client.webhooks.get(id)>`);
  }
} catch (err) {
  console.error(`webhooks.create failed: ${(err as Error).message}`);
  process.exit(2);
}

// Confirm via list.
try {
  const list = await client.webhooks.list();
  const items = (list as { webhooks?: Array<{ url?: string; webhookId?: string }> }).webhooks ?? [];
  console.log(`\nregistered webhooks (${items.length}):`);
  for (const w of items) console.log(`  - ${w.url ?? w.webhookId}`);
} catch (err) {
  console.error(`webhooks.list failed: ${(err as Error).message}`);
}
