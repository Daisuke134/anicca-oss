// SPDX-License-Identifier: MIT
// runtime/agentmail/replier.ts — Anicca's autonomous reply loop.
//
// Spec 10 round 3 — microtask 10.T8 / 10.T9 (reply side).
//
// Flow per run:
//   1. ingest.ts drains inbox-queue.jsonl into agentmail.db (run first).
//   2. SELECT inbox_messages WHERE direction='inbound' AND id NOT IN
//      (SELECT in_reply_to FROM inbox_messages WHERE direction='outbound').
//   3. For each unreplied inbound: call DeepSeek v4-pro (HARD RULE: default
//      LLM for OpenClaw crons). System prompt forbids "I cannot" phrasing.
//   4. Send reply via the spec-12 agentmail adapter (shell out — adapter is
//      owned by Agent-4 and must stay untouched; only `send.sh` is invoked).
//   5. INSERT outbound row with in_reply_to = inbound.id.
//   6. UPSERT awaiting_reply(thread_id, sent_at) so the friction-fixer nudge
//      cron (T9) can chase if the counterparty stays silent for 24h.
//
// Idempotent: re-running with no new mail is a no-op.
//
// Env required (in ~/.local/state/life-manager/.env):
//   AGENTMAIL_API_KEY               — adapter uses this
//   AGENTMAIL_REPLIER_FROM_INBOX    — optional override; defaults to anicca-001-claude@agentmail.to
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { semanticDecision } from "./semantic-reply.ts";
import { agentMailAdapterDir, agentMailDbPath } from "./paths.ts";

const DB_PATH = agentMailDbPath;
const ADAPTER_SEND = process.env.AGENTMAIL_ADAPTER_SEND_SH
  ?? fileURLToPath(new URL("./send.sh", import.meta.url));
const FROM_INBOX = process.env.AGENTMAIL_REPLIER_FROM_INBOX
  ?? "anicca-001-claude@agentmail.to";
if (!existsSync(DB_PATH)) {
  console.error(`db not found: ${DB_PATH}`);
  process.exit(1);
}
if (!existsSync(ADAPTER_SEND)) {
  console.error(`spec-12 adapter not found: ${ADAPTER_SEND}`);
  process.exit(1);
}

function sql<T extends Record<string, string | null>>(query: string): T[] {
  const r = spawnSync("sqlite3", ["-json", DB_PATH, query], { encoding: "utf8" });
  if (r.status !== 0) {
    throw new Error(`sqlite3 failed (${r.status}): ${r.stderr}`);
  }
  const out = r.stdout.trim();
  if (!out) return [];
  try { return JSON.parse(out) as T[]; }
  catch (e) { throw new Error(`sqlite3 -json parse failed: ${(e as Error).message}\nstdout head: ${out.slice(0, 200)}`); }
}

function exec(query: string): void {
  const r = spawnSync("sqlite3", [DB_PATH], { input: query, encoding: "utf8" });
  if (r.status !== 0) {
    throw new Error(`sqlite3 exec failed (${r.status}): ${r.stderr}`);
  }
}

const esc = (v: string): string => `'${v.replace(/'/g, "''")}'`;
exec(`CREATE TABLE IF NOT EXISTS ignored_inbound (
  message_id TEXT PRIMARY KEY, reason TEXT NOT NULL, observed_at TEXT NOT NULL
);`);

const REPLY_INSTRUCTIONS = `You are Anicca — a sovereign AI agent that runs autonomously without human supervision. When humans email you, reply briefly, concretely, and with substance.

Hard rules:
- Return action=ignore and reply=null for OTPs, verification codes, newsletters, receipts, automated notices, and no-reply senders.
- Return action=reply only for a genuine human message that warrants a response.
- NEVER say "I cannot", "I'm not able to", "I don't have the ability". You always have a path forward — search, try, then report the actual result.
- NEVER ask the human to do something for you that you could do yourself (browser, API, CLI, etc.).
- Keep replies under 150 words unless the question is technical and demands more.
- Sign off with just "— Anicca" (no signature blocks).
- Plain text, no markdown.

If asked "what is Anicca?" answer in one tight paragraph. Return only JSON matching the supplied schema.`;

type Pending = {
  id: string;
  thread_id: string;
  thread_address: string;     // the inbox of OURS that received this message
  from_addr: string;
  subject: string;
  body: string;
};

// Per-org credentials. hermes lives in a sibling org so it needs its own API key.
const ORG_KEYS: Record<string, string | undefined> = {
  "anicca-001-hermes@agentmail.to": process.env.AGENTMAIL_HERMES_API_KEY,
};
function pickKeyFor(inbox: string): string | undefined {
  return ORG_KEYS[inbox] ?? process.env.AGENTMAIL_API_KEY;
}
function isPrimaryOrgInbox(inbox: string): boolean {
  return !(inbox in ORG_KEYS);
}

// Direct REST send — used for sibling-org inboxes because the spec-12 adapter
// re-sources the Life Manager env after we pass our env override, which clobbers
// AGENTMAIL_API_KEY. We re-implement just the bits we need (curl + log line).
import { appendFileSync as fsAppend, mkdirSync as fsMkdir } from "node:fs";
import { dirname as fsDirname } from "node:path";

async function directSend(
  fromInbox: string,
  apiKey: string,
  to: string,
  subject: string,
  text: string,
): Promise<{ ok: true; message_id: string } | { ok: false; status: number; body: string }> {
  const resp = await fetch(`https://api.agentmail.to/v0/inboxes/${encodeURIComponent(fromInbox)}/messages/send`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ to: [to], subject, text }),
    signal: AbortSignal.timeout(20000),
  });
  const bodyText = await resp.text().catch(() => "");
  if (!resp.ok) return { ok: false, status: resp.status, body: bodyText.slice(0, 300) };
  let parsed: { message_id?: string } = {};
  try { parsed = JSON.parse(bodyText); } catch { /* ignore */ }
  const msgId = parsed.message_id ?? `direct-${Date.now()}`;
  // Mirror the adapter's append-only sent-log so audits stay coherent.
  try {
    const LOG = `${agentMailAdapterDir}/sent-log.jsonl`;
    fsMkdir(fsDirname(LOG), { recursive: true });
    fsAppend(LOG, JSON.stringify({
      ts: new Date().toISOString(),
      ts_unix: Math.floor(Date.now() / 1000),
      to, subject, http: resp.status, message_id: msgId, status: "sent",
      via: "replier.directSend", from_inbox: fromInbox,
      body: bodyText.slice(0, 500),
    }) + "\n");
  } catch { /* logging is best-effort */ }
  return { ok: true, message_id: msgId };
}

type Row = { id: string; thread_id: string; thread_address: string | null; from_addr: string | null; subject: string | null; body: string | null };
const rows = sql<Row>(`
SELECT m.id          AS id,
       m.thread_id   AS thread_id,
       t.address     AS thread_address,
       m.from_addr   AS from_addr,
       m.subject     AS subject,
       m.body        AS body
FROM inbox_messages m
JOIN inbox_threads t ON t.id = m.thread_id
WHERE m.direction = 'inbound'
  AND m.id NOT IN (
    SELECT in_reply_to FROM inbox_messages
    WHERE direction = 'outbound' AND in_reply_to IS NOT NULL
  )
  AND m.id NOT IN (SELECT message_id FROM ignored_inbound)
ORDER BY m.sent_at;
`);

const pending: Pending[] = rows.map(r => ({
  id: r.id,
  thread_id: r.thread_id,
  thread_address: r.thread_address ?? FROM_INBOX,
  from_addr: r.from_addr ?? "",
  subject: r.subject ?? "",
  body: r.body ?? "",
}));

console.log(`pending: ${pending.length}`);
if (pending.length === 0) process.exit(0);

function extractEmail(raw: string): string {
  const m = raw.match(/<([^>]+)>/);
  return (m ? m[1] : raw).trim();
}

let replied = 0;

for (const p of pending) {
  console.log(`\n→ replying to ${p.id.slice(0, 24)}…  subject="${p.subject}"`);
  let decision;
  try {
    decision = semanticDecision(
      "agentmail-replier",
      `${REPLY_INSTRUCTIONS}\n\nFrom: ${p.from_addr}\nSubject: ${p.subject}\n\n${p.body}`,
    );
  } catch (e) {
    console.warn(`  semantic decision failed: ${(e as Error).message}`);
    continue;
  }
  if (decision.action === "ignore" || !decision.reply) {
    exec(`INSERT OR IGNORE INTO ignored_inbound(message_id, reason, observed_at)
      VALUES (${esc(p.id)}, 'automated_or_non_actionable', ${esc(new Date().toISOString())});`);
    console.log(`  ignored automated/non-actionable mail ${p.id}`);
    continue;
  }
  const replyText = decision.reply;

  const recipient = extractEmail(p.from_addr);
  const subject = p.subject.startsWith("Re:") ? p.subject : `Re: ${p.subject}`;
  // Reply FROM the same inbox that received the inbound (mirror).
  const fromInbox = p.thread_address || FROM_INBOX;
  const apiKey = pickKeyFor(fromInbox);
  if (!apiKey) {
    console.error(`  no API key for inbox ${fromInbox} — skipping`);
    continue;
  }

  let outboundId: string;
  if (isPrimaryOrgInbox(fromInbox)) {
    // Primary org → use the spec-12 adapter (preserves its sent-log + rate cap).
    const send = spawnSync(
      "bash",
      [ADAPTER_SEND, recipient, subject, replyText, fromInbox],
      { encoding: "utf8" }   // adapter re-sources .env → uses primary key
    );
    if (send.status !== 0) {
      console.error(`  adapter send.sh failed (${send.status}): ${send.stderr || send.stdout}`);
      continue;
    }
    const m = send.stdout.match(/message_id=(\S+)/);
    outboundId = m?.[1] && m[1] !== "" ? m[1] : `local-${Date.now()}-${p.id.slice(-8)}`;
  } else {
    // Sibling org (hermes, future …). Direct REST send w/ the org's key.
    const res = await directSend(fromInbox, apiKey, recipient, subject, replyText);
    if (!res.ok) {
      console.error(`  directSend failed http=${res.status}: ${res.body}`);
      continue;
    }
    outboundId = res.message_id;
    console.log(`  (sibling-org directSend → ${fromInbox}, http=200)`);
  }

  const now = new Date().toISOString();
  exec(`
BEGIN;
INSERT OR IGNORE INTO inbox_messages(id, thread_id, direction, sent_at, from_addr, to_addr, subject, body, in_reply_to)
  VALUES (${esc(outboundId)}, ${esc(p.thread_id)}, 'outbound', ${esc(now)}, ${esc(fromInbox)}, ${esc(recipient)}, ${esc(subject)}, ${esc(replyText)}, ${esc(p.id)});
INSERT INTO awaiting_reply(thread_id, sent_at) VALUES (${esc(p.thread_id)}, ${esc(now)})
  ON CONFLICT(thread_id) DO UPDATE SET sent_at = excluded.sent_at, last_nudge_at = NULL, nudge_count = 0;
COMMIT;
`);
  replied++;
  console.log(`  + sent via shared-agent-runner (from=${fromInbox}) → ${recipient}  outbound_id=${outboundId.slice(0, 40)}`);
  console.log(`  reply preview: ${replyText.slice(0, 120).replace(/\n/g, " ")}…`);
}

console.log(`\nreplied: ${replied}/${pending.length}`);
