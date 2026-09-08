// SPDX-License-Identifier: MIT
// runtime/agentmail/ingest.ts — drain inbox-queue.jsonl into agentmail.db.
//
// Spec 10 — microtask 10.T5 partial. Reads the JSONL append-log written by
// webhook-server.ts and upserts the corresponding rows into inbox_threads +
// inbox_messages. Idempotent (PRIMARY KEY = AgentMail thread_id / message_id).
//
// Run once, or wrap in a launchd timer / cron heartbeat. Intentionally simple:
// no peer-api POST yet (that pairs with Agent-3's heartbeat receiver, deferred
// to the merge integration step).
//
// Usage:
//   node ingest.ts                      # default queue + db paths
//   AGENTMAIL_DB_PATH=/path/to/x.db AGENTMAIL_QUEUE_PATH=/path/to/q.jsonl node ingest.ts
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { agentMailDbPath, agentMailQueuePath } from "./paths.ts";

const QUEUE_PATH = agentMailQueuePath;
const DB_PATH = agentMailDbPath;
const CURSOR_PATH = `${QUEUE_PATH}.cursor`;

mkdirSync(dirname(DB_PATH), { recursive: true });

if (!existsSync(QUEUE_PATH)) {
  console.error(`queue not found: ${QUEUE_PATH}`);
  process.exit(0);
}

const lines = readFileSync(QUEUE_PATH, "utf8").split("\n").filter(Boolean);
const cursor = existsSync(CURSOR_PATH)
  ? Number.parseInt(readFileSync(CURSOR_PATH, "utf8").trim() || "0", 10)
  : 0;

console.log(`queue: ${lines.length} lines, cursor: ${cursor}`);

type QueueRow = {
  status: string;
  payload: {
    event_type?: string;
    message?: {
      message_id?: string;
      thread_id?: string;
      inbox_id?: string;
      from?: string;
      to?: string[];
      subject?: string;
      text?: string;
      timestamp?: string;
    };
  };
};

const sqlStatements: string[] = [];
let upserted = 0;

for (let i = cursor; i < lines.length; i++) {
  let row: QueueRow;
  try { row = JSON.parse(lines[i]) as QueueRow; }
  catch { continue; }
  if (row.status === "rejected") continue;
  const m = row.payload?.message;
  if (!m?.message_id || !m.thread_id) continue;

  const escape = (v: string | undefined): string =>
    v == null ? "NULL" : `'${v.replace(/'/g, "''")}'`;

  sqlStatements.push(`INSERT OR IGNORE INTO inbox_threads(id, address, subject, last_message_at) VALUES (${escape(m.thread_id)}, ${escape(m.inbox_id)}, ${escape(m.subject)}, ${escape(m.timestamp ?? new Date().toISOString())});`);
  sqlStatements.push(`UPDATE inbox_threads SET last_message_at = ${escape(m.timestamp ?? new Date().toISOString())}, subject = COALESCE(subject, ${escape(m.subject)}) WHERE id = ${escape(m.thread_id)};`);
  sqlStatements.push(`INSERT OR IGNORE INTO inbox_messages(id, thread_id, direction, sent_at, from_addr, to_addr, subject, body) VALUES (${escape(m.message_id)}, ${escape(m.thread_id)}, 'inbound', ${escape(m.timestamp ?? new Date().toISOString())}, ${escape(m.from)}, ${escape((m.to ?? []).join(", "))}, ${escape(m.subject)}, ${escape(m.text)});`);
  upserted++;
}

if (sqlStatements.length === 0) {
  console.log("nothing to ingest");
  writeFileSync(CURSOR_PATH, String(lines.length));
  process.exit(0);
}

const script = "BEGIN;\n" + sqlStatements.join("\n") + "\nCOMMIT;\n";
const result = spawnSync("sqlite3", [DB_PATH], { input: script, encoding: "utf8" });
if (result.status !== 0) {
  console.error(`sqlite3 failed (exit ${result.status}): ${result.stderr}`);
  process.exit(2);
}

writeFileSync(CURSOR_PATH, String(lines.length));
console.log(`+ upserted ${upserted} message(s); cursor → ${lines.length}`);

// Show what's in the table now.
const summary = spawnSync(
  "sqlite3",
  [DB_PATH, "SELECT direction, sent_at, from_addr, subject FROM inbox_messages ORDER BY sent_at;"],
  { encoding: "utf8" }
);
console.log("--- inbox_messages ---");
process.stdout.write(summary.stdout);
