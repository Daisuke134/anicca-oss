#!/usr/bin/env node
"use strict";

const { execFileSync } = require("node:child_process");
const {
  runPersonalizedAction,
  selectManagedAccount,
  hasCompletedAction,
} = require("../lib/personalized-action");
const { sendMessage } = require("../lib/telegram");

const PROJECT = "f9c524cb-ba4a-43bb-9639-ff736afd9ec1";
const SERVICE = "ca978c74-639a-4fa1-af22-9cdd53c3f615";

function command(file, args) {
  return execFileSync(file, args, {
    encoding: "utf8",
    maxBuffer: 4 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function jsonCommand(file, args) {
  return JSON.parse(command(file, args));
}

function findValue(value, predicate) {
  if (!value || typeof value !== "object") return null;
  if (predicate(value)) return value;
  for (const child of Object.values(value)) {
    if (Array.isArray(child)) {
      for (const item of child) {
        const hit = findValue(item, predicate);
        if (hit) return hit;
      }
    } else {
      const hit = findValue(child, predicate);
      if (hit) return hit;
    }
  }
  return null;
}

function providerId(value) {
  if (Array.isArray(value)) {
    for (const item of value) {
      const hit = providerId(item);
      if (hit) return hit;
    }
    return null;
  }
  if (!value || typeof value !== "object") return null;
  for (const key of ["messageId", "message_id", "eventId", "event_id", "id"]) {
    if (typeof value[key] === "string" && value[key].trim()) return value[key];
  }
  for (const child of Object.values(value)) {
    const hit = providerId(child);
    if (hit) return hit;
  }
  return null;
}

function rfcMessageId(value) {
  const header = findValue(value, (item) =>
    typeof item.name === "string"
    && item.name.toLowerCase() === "message-id"
    && typeof item.value === "string");
  return header && /^<[^<>\s]+>$/.test(header.value) ? header.value : null;
}

function isoStart(event) {
  const raw = event?.start?.dateTime || event?.start?.date || event?.start;
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

async function main() {
  const variables = jsonCommand("railway", [
    "variable", "list", "-p", PROJECT, "-s", SERVICE, "-e", "production", "--json",
  ]);
  const usersResponse = await fetch(
    `${variables.SUPABASE_URL}/rest/v1/lm_users?select=email,telegram_chat_id,paid,calendar_provider&paid=eq.true`,
    {
      headers: {
        apikey: variables.SUPABASE_SERVICE_ROLE_KEY,
        Authorization: `Bearer ${variables.SUPABASE_SERVICE_ROLE_KEY}`,
      },
      signal: AbortSignal.timeout(15_000),
    },
  );
  if (!usersResponse.ok) throw new Error("production_context_read_failed");
  const users = await usersResponse.json();
  const user = users.find((row) =>
    typeof row.telegram_chat_id === "string"
    && row.telegram_chat_id
    && row.calendar_provider);
  if (!user) throw new Error("production_context_unavailable");
  const auth = jsonCommand("gog", ["auth", "list", "--json"]);
  const account = selectManagedAccount(user, auth.accounts);
  if (!account) throw new Error("managed_google_account_unavailable");
  const completed = jsonCommand("gog", [
    "calendar", "events",
    "--account", account,
    "--days", "730",
    "--private-prop-filter", "life_manager_action=10i",
    "--max", "10",
    "--json",
    "--results-only",
    "--fields", "items(id)",
  ]);
  if (hasCompletedAction(completed)) throw new Error("personalized_action_already_completed");

  const events = jsonCommand("gog", [
    "calendar", "events",
    "--account", account,
    "--days", "60",
    "--query", "イベント",
    "--max", "50",
    "--json",
    "--results-only",
    "--fields", "items(id,start,end)",
  ]);
  const candidates = Array.isArray(events) ? events : events.items || [];
  const realEvent = candidates
    .map((event) => ({ event, startMs: isoStart(event) }))
    .filter(({ startMs }) => startMs && startMs > Date.now())
    .sort((a, b) => a.startMs - b.startMs)[0];
  if (!realEvent || !providerId(realEvent.event)) throw new Error("real_calendar_candidate_unavailable");

  const now = Date.now();
  const prepStart = Math.max(now + 60 * 60 * 1000, realEvent.startMs - 24 * 60 * 60 * 1000);
  const prepEnd = prepStart + 15 * 60 * 1000;
  const intents = [
    {
      id: "goal-10i",
      uid: "current-user",
      kind: "explicit_goal",
      statement: "Complete one personalized action from current context",
      provenance: {
        source: "user_message",
        evidence: "explicit atomic 10i instruction",
        observedAt: new Date(now).toISOString(),
      },
      confidenceTier: "explicit",
      confidence: 0.9,
      expiresAt: new Date(now + 24 * 60 * 60 * 1000).toISOString(),
      status: "active",
      supersedes: null,
    },
    {
      id: "delegation-10i",
      uid: "current-user",
      kind: "delegation",
      statement: "Delegate one reversible web or email action and post-action reports",
      provenance: {
        source: "user_message",
        evidence: "explicit atomic 10i instruction",
        observedAt: new Date(now).toISOString(),
      },
      confidenceTier: "explicit",
      confidence: 0.9,
      expiresAt: new Date(now + 24 * 60 * 60 * 1000).toISOString(),
      status: "active",
      supersedes: null,
    },
  ];
  const candidate = {
    id: `upcoming-event-prep:${providerId(realEvent.event)}`,
    category: "life_admin",
    description: "Send one preparation brief for a real upcoming calendar event",
    benefit: "medium",
    urgency: "medium",
    cost: "low",
    risk: "low",
    reversible: true,
    supportsIntentIds: ["goal-10i", "delegation-10i"],
    violatesIntentIds: [],
    delegationId: "delegation-10i",
    materialPreference: false,
    previouslyAsked: false,
  };

  const receipt = await runPersonalizedAction({
    intents,
    candidate,
    nowMs: now,
    executeEmail: async () => {
      const sent = jsonCommand("gog", [
        "gmail", "send",
        "--account", account,
        "--to", account,
        "--subject", "Life Manager: upcoming event prep",
        "--body", "次の予定に向けた準備メモです。会場・アクセス、必要な持ち物、開始時刻と移動余裕を前日までに確認してください。",
        "--json",
        "--results-only",
        "--no-input",
      ]);
      const gmailId = providerId(sent);
      if (!gmailId) throw new Error("email_provider_receipt_missing");
      const readback = jsonCommand("gog", [
        "gmail", "get", gmailId,
        "--account", account,
        "--json",
        "--results-only",
      ]);
      const messageId = rfcMessageId(readback);
      if (!messageId) throw new Error("rfc_message_id_readback_missing");
      return { provider_id: gmailId, message_id: messageId };
    },
    createCalendarReport: async ({ honestFailure }) => {
      const created = jsonCommand("gog", [
        "calendar", "create", "primary",
        "--account", account,
        "--summary", "Life Manager: event prep",
        "--from", new Date(prepStart).toISOString(),
        "--to", new Date(prepEnd).toISOString(),
        "--description", honestFailure
          ? "メール送信に失敗しました。準備事項を手動で確認してください。"
          : "準備メールを送信済みです。会場・アクセス、持ち物、移動余裕を確認してください。",
        "--transparency", "free",
        "--send-updates", "none",
        "--private-prop", "life_manager_action=10i",
        "--json",
        "--results-only",
        "--no-input",
      ]);
      const id = providerId(created);
      if (!id) throw new Error("calendar_provider_receipt_missing");
      return { provider_id: id };
    },
    sendTelegramReport: async ({ honestFailure }) => {
      const sent = await sendMessage(
        variables.LM_TELEGRAM_BOT_TOKEN,
        user.telegram_chat_id,
        honestFailure
          ? "📨 予定の準備メールは送れませんでした。カレンダーに確認枠を入れ、失敗を記録しました。"
          : "📨 次の予定に向けた準備メモをメールで送り、カレンダーにも確認枠を入れておきました。",
      );
      const id = sent?.ok ? sent.result?.message_id : null;
      if (!id) throw new Error("telegram_provider_receipt_missing");
      return { provider_id: String(id) };
    },
  });
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
  if (receipt.outcome !== "completed") process.exitCode = 1;
}

main().catch((error) => {
  process.stderr.write(`personalized-action-e2e: ${error.message}\n`);
  process.exitCode = 1;
});
