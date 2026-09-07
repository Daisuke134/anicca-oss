// SPDX-License-Identifier: MIT
// runtime/agentmail/webhook-server.ts — Express receiver for AgentMail webhooks.
//
// Spec 10 — microtask 10.T2 + 10.T5.
//
// Behaviour:
//   - POST /agentmail   : verify Svix HMAC, enqueue event JSON to inbox-queue.jsonl
//   - GET  /healthz     : liveness probe
//
// Anti-human-loop (HARD RULE #-2): on signature failure or malformed body we LOG
// and still return 200 so AgentMail's Svix retries do not pile up. The error is
// captured in the queue file with `status: "rejected"` so a downstream daemon
// (anicca-friction-fixer / Conway) can attribute it without a human prompt.
import express, { type Request, type Response } from "express";
import bodyParser from "body-parser";
import { Webhook, WebhookVerificationError } from "svix";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { agentMailQueuePath } from "./paths.ts";

const PORT = Number.parseInt(process.env.AGENTMAIL_WEBHOOK_PORT ?? "8810", 10);
const QUEUE_PATH = agentMailQueuePath;

// Multi-org / multi-secret support. AgentMail issues one Svix secret per webhook
// subscription. We may have several orgs (e.g. anicca-001-claude lives in the
// primary org, anicca-001-hermes in a sibling org). Each org has its own secret.
// Layout (one secret per env var, all suffix-coded so launchd just inherits them):
//   AGENTMAIL_WEBHOOK_SECRET           — primary (claude + openclaw + genesis)
//   AGENTMAIL_WEBHOOK_SECRET_HERMES    — hermes org
//   AGENTMAIL_WEBHOOK_SECRET_<NAME>    — any future org
// We try each in turn; first match wins. Empty secrets are skipped.
const SECRETS: Array<{ name: string; secret: string }> = Object.entries(process.env)
  .filter(([k, v]) => k.startsWith("AGENTMAIL_WEBHOOK_SECRET") && typeof v === "string" && v.length > 0)
  .map(([k, v]) => ({
    name: k === "AGENTMAIL_WEBHOOK_SECRET" ? "primary" : k.replace(/^AGENTMAIL_WEBHOOK_SECRET_/, "").toLowerCase(),
    secret: v as string,
  }));

mkdirSync(dirname(QUEUE_PATH), { recursive: true });

type QueueRecord = {
  received_at: string;
  status: "verified" | "rejected" | "unverified";
  reason?: string;
  org?: string;            // which secret bucket verified the event
  svix_id?: string;
  svix_timestamp?: string;
  event_type?: string;
  payload: unknown;
};

function enqueue(record: QueueRecord): void {
  try {
    appendFileSync(QUEUE_PATH, JSON.stringify(record) + "\n", { encoding: "utf8" });
  } catch (err) {
    // Last resort: dump to stderr so launchd captures it; never throw to caller.
    process.stderr.write(`[agentmail-webhook] enqueue failed: ${(err as Error).message}\n`);
  }
}

const app = express();

app.get("/healthz", (_req, res) => {
  res.json({
    ok: true,
    port: PORT,
    queue: QUEUE_PATH,
    signed: SECRETS.length > 0,
    secret_buckets: SECRETS.map(s => s.name),
  });
});

// Raw body required for Svix HMAC verification (express.json() would mutate it).
app.post(
  "/agentmail",
  bodyParser.raw({ type: "*/*", limit: "10mb" }),
  (req: Request, res: Response) => {
    const rawBuf: Buffer = Buffer.isBuffer(req.body) ? req.body : Buffer.from("");
    const rawStr = rawBuf.toString("utf8");

    const svixId = String(req.header("svix-id") ?? "");
    const svixTimestamp = String(req.header("svix-timestamp") ?? "");
    const svixSignature = String(req.header("svix-signature") ?? "");

    let verified: unknown;
    let status: QueueRecord["status"] = "unverified";
    let reason: string | undefined;
    let org: string | undefined;

    if (SECRETS.length > 0) {
      const headers = {
        "svix-id": svixId,
        "svix-timestamp": svixTimestamp,
        "svix-signature": svixSignature,
      };
      const reasons: string[] = [];
      for (const candidate of SECRETS) {
        try {
          const wh = new Webhook(candidate.secret);
          verified = wh.verify(rawStr, headers);
          status = "verified";
          org = candidate.name;
          break;
        } catch (err) {
          reasons.push(`${candidate.name}: ${err instanceof WebhookVerificationError ? err.message : (err as Error).message}`);
        }
      }
      if (status !== "verified") {
        status = "rejected";
        reason = `svix-verify: tried [${reasons.join(" | ")}]`;
        try { verified = JSON.parse(rawStr); } catch { verified = rawStr; }
      }
    } else {
      // No secret configured (local smoke test). Best-effort parse, mark unverified.
      reason = "AGENTMAIL_WEBHOOK_SECRET* unset — skipping HMAC";
      try { verified = JSON.parse(rawStr); } catch { verified = rawStr; }
    }

    const payload = verified as { event_type?: string; type?: string } | string;
    const eventType = typeof payload === "object" && payload !== null
      ? (payload.event_type ?? payload.type)
      : undefined;

    const record: QueueRecord = {
      received_at: new Date().toISOString(),
      status,
      reason,
      org,
      svix_id: svixId || undefined,
      svix_timestamp: svixTimestamp || undefined,
      event_type: eventType,
      payload: verified,
    };
    enqueue(record);

    process.stdout.write(
      `[agentmail-webhook] ${status} org=${org ?? "-"} event=${eventType ?? "?"} id=${svixId || "-"}\n`
    );

    // Always 200 — retry storms harm us more than silently dropping bad sigs.
    res.status(200).json({ ok: true, status });
  }
);

const server = app.listen(PORT, () => {
  process.stdout.write(`[agentmail-webhook] listening :${PORT} queue=${QUEUE_PATH}\n`);
});

function shutdown(signal: string): void {
  process.stdout.write(`[agentmail-webhook] ${signal} — shutting down\n`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT",  () => shutdown("SIGINT"));
