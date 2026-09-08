// SPDX-License-Identifier: MIT
// runtime/agentmail/inboxes.ts — provisions custom inboxes for each Anicca instance.
//
// Live SDK shape (agentmail@0.5.8, verified 2026-06-03 against
// node_modules/agentmail/.../inboxes/types/CreateInboxRequest.d.ts):
//   client.inboxes.create({ username, domain?, displayName?, clientId?, metadata? })
//
// `clientId` is the idempotency key — running this script twice does not duplicate
// inboxes; the API returns the existing inbox.
//
// Spec 10 — Wave 1, microtask 10.T1.
import { AgentMailClient } from "agentmail";

const API_KEY = process.env.AGENTMAIL_API_KEY;
if (!API_KEY) {
  console.error("AGENTMAIL_API_KEY missing in env (see ~/.local/state/life-manager/.env)");
  process.exit(1);
}

const client = new AgentMailClient({ apiKey: API_KEY });

// NB: AgentMail rejects parentheses + a few other punctuation chars in display_name.
const TARGETS: ReadonlyArray<{ username: string; displayName: string }> = [
  { username: "anicca-001-claude",   displayName: "Anicca 001 Claude" },
  { username: "anicca-001-openclaw", displayName: "Anicca 001 OpenClaw" },
  { username: "anicca-001-hermes",   displayName: "Anicca 001 Hermes" },
];

const results: Array<{ username: string; status: "created" | "exists" | "limit" | "error"; address?: string; inboxId?: string; error?: string }> = [];

for (const target of TARGETS) {
  try {
    const inbox = await client.inboxes.create({
      username: target.username,
      displayName: target.displayName,
      clientId: `spec-10-${target.username}`,
    });
    const address = (inbox as { email?: string; inboxId?: string }).email
      ?? (inbox as { inboxId?: string }).inboxId
      ?? `${target.username}@agentmail.to`;
    const inboxId = (inbox as { inboxId?: string }).inboxId;
    console.log(`+ created ${address}`);
    results.push({ username: target.username, status: "created", address, inboxId });
  } catch (e) {
    const msg = String((e as Error)?.message ?? e);
    if (/already exists|IsTaken|409|conflict/i.test(msg)) {
      console.log(`= ${target.username} already exists`);
      results.push({ username: target.username, status: "exists" });
    } else if (/LimitExceeded|Inbox limit/i.test(msg)) {
      console.warn(`! ${target.username} skipped — AgentMail free-tier inbox cap reached`);
      results.push({ username: target.username, status: "limit", error: msg });
    } else {
      console.error(`x ${target.username}: ${msg}`);
      results.push({ username: target.username, status: "error", error: msg });
    }
  }
}

// Confirm count by listing.
try {
  const list = await client.inboxes.list();
  const inboxes = (list as { inboxes?: Array<{ email?: string; inboxId?: string }> }).inboxes ?? [];
  console.log(`\nTotal inboxes in org: ${inboxes.length}`);
  for (const ib of inboxes) {
    console.log(`  - ${ib.email ?? ib.inboxId}`);
  }
} catch (e) {
  console.error(`list() failed: ${String((e as Error)?.message ?? e)}`);
}

const fatal = results.filter(r => r.status === "error");
const limited = results.filter(r => r.status === "limit");
if (limited.length > 0) {
  console.warn(`\n${limited.length} inbox(es) deferred to plan upgrade (free-tier cap = 3 inboxes per org)`);
}
if (fatal.length > 0) {
  console.error(`\n${fatal.length} inbox(es) failed for non-quota reasons`);
  process.exit(2);
}
