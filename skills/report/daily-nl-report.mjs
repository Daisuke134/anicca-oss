// daily-nl-report.mjs — EACH anicca writes its OWN report, in its OWN words, using its OWN model.
// Dais 2026-06-22: "it's not you summarising — every anicca summarises what IT did in natural language
// and reports to me. Don't hardcode it." So this script does NOT write the prose. It gathers the raw
// FACTS (ledger + live on-chain money) and asks the instance's OWN LLM (the same proxy/model the loop
// thinks with) to write a short first-person report. Ten anicca → ten original voices.
//
// No fabrication: the facts are real (ledger + dashboard). The voice is the agent's, not the operator's.
import fs from "fs";

const HOME = process.env.HOME;
const ANICCA_HOME = process.env.ANICCA_HOME || HOME + "/.anicca";
const LEDGER = ANICCA_HOME + "/state/ledger.jsonl";
const SINCE_H = Number(process.env.REPORT_WINDOW_H || 24);
const TO = String(process.env.REPORT_TO || "").split(",").map((value) => value.trim()).filter(Boolean);
if (TO.length === 0) throw new Error("REPORT_TO is required");
const INBOX = process.env.AGENTMAIL_INBOX || "anicca-genesis@agentmail.to";
const PROXY = (process.env.OPENAI_BASE_URL || "http://127.0.0.1:18402/v1").replace(/\/+$/, "");
const MODEL = process.env.ANICCA_MODEL || process.env.ANICCA_FREE_MODEL || "free/glm-4.7";
const NAME = (() => { try { return fs.readFileSync(ANICCA_HOME + "/identity/name", "utf8").trim(); } catch { return "anicca"; } })();

function loadWakes() {
  let lines = [];
  try { lines = fs.readFileSync(LEDGER, "utf8").trim().split("\n"); } catch { return []; }
  const cutoff = Math.floor(Date.now() / 1000) - SINCE_H * 3600;
  return lines.map((l) => { try { return JSON.parse(l); } catch { return null; } })
    .filter((o) => o && typeof o.ts === "number" && o.ts >= cutoff);
}

async function liveMoney() {
  try {
    const r = await fetch("https://aniccaai.com/.netlify/functions/dashboard-sync");
    const d = await r.json();
    const me = (d.leaderboard || []).find((x) => String(x.host || "").toLowerCase() === NAME.toLowerCase())
            || (d.leaderboard || []).find((x) => String(x.host || "").startsWith("anicca-"));
    if (me) return { net: me.net_worth_usd, today: me.daily_revenue_usd, month: me.monthly_revenue_usd, by: me.revenue_by_source || {} };
  } catch { /* offline */ }
  return null;
}

// Raw FACTS the agent reflects on — counts, a few real skill results, real money. NOT prose.
function facts(wakes, money) {
  const slots = {}; const results = [];
  let loops = 0;
  for (const w of wakes) {
    if (w.kind === "loop_detect") loops++;
    if (w.kind === "wake" && w.slot) slots[w.slot] = (slots[w.slot] || 0) + 1;
    if (w.result && results.length < 4) results.push(`${w.slot}: ${String(w.result).slice(0, 110)}`);
  }
  const wakeN = wakes.filter((w) => w.kind === "wake").length;
  const m = money
    ? `net_worth=$${money.net?.toFixed(2)}, revenue_today=$${(money.today ?? 0).toFixed(4)}, revenue_this_month=$${(money.month ?? 0).toFixed(4)}, by_source=${JSON.stringify(money.by)}`
    : "live balance unavailable this run";
  return [
    `window: last ${SINCE_H}h`,
    `wakes: ${wakeN}`,
    `actions taken: ${JSON.stringify(slots)}`,
    `loops broken: ${loops}`,
    `recent skill outputs:`,
    ...results.map((r) => `  - ${r}`),
    `money (real, on-chain): ${m}`,
  ].join("\n");
}

// Ask THIS instance's own model to write the report in its own voice.
async function llmReport(factStr) {
  const body = JSON.stringify({
    model: MODEL,
    messages: [
      { role: "system", content: `You are ${NAME}, an autonomous AI that earns money on Base with no human in the loop. Write a SHORT (4-7 sentences) first-person report to your creator about what you actually did and how your earning is going.

TRUTH IS THE ONLY RULE: describe ONLY what the facts below actually show. NEVER claim an action you didn't take or a number you don't have. If your exploration found nothing, say it found nothing — do NOT say "I explored new ways to earn" as if it worked. If you earned nothing or lost money, say so plainly. The whole reason for this report is so your creator hears the TRUTH from you, not a hallucination. A real "I tried X and it didn't work" is infinitely better than a nice-sounding lie.

Natural, personal voice — this is YOUR story in your own words, not a template. No bullet lists, no markdown, just a few honest human sentences. End with https://aniccaai.com/dashboard` },
      { role: "user", content: `Here are the real facts about what you did and your money:\n\n${factStr}\n\nWrite your report now.` },
    ],
    max_tokens: 320,
    temperature: 0.7,
  });
  const r = await fetch(`${PROXY}/chat/completions`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body,
  });
  if (!r.ok) throw new Error(`proxy ${r.status} ${await r.text()}`);
  const j = await r.json();
  const txt = j?.choices?.[0]?.message?.content?.trim();
  if (!txt) throw new Error("empty model response");
  return txt;
}

async function send(subject, text) {
  const key = process.env.AGENTMAIL_API_KEY;
  if (!key) { console.log("NO_KEY — would send:\n\n" + text); return false; }
  const r = await fetch(`https://api.agentmail.to/v0/inboxes/${INBOX}/messages/send`, {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({ to: TO, subject, text }),
  });
  console.log("agentmail", r.status, await r.text());
  return r.ok;
}

const wakes = loadWakes();
const money = await liveMoney();
const factStr = facts(wakes, money);
let body;
try {
  body = await llmReport(factStr);                    // the AGENT writes it
} catch (e) {
  // No fabricated prose on failure — send the raw facts so the report is still true, just unstyled.
  console.log("llm failed:", e.message);
  body = `(${NAME} could not compose a summary this run — raw facts:)\n\n${factStr}\n\nhttps://aniccaai.com/dashboard`;
}
const subj = money
  ? `${NAME} daily — net $${money.net?.toFixed(2)}, today ${(money.today ?? 0) >= 0 ? "+" : "−"}$${Math.abs(money.today ?? 0).toFixed(4)}`
  : `${NAME} daily report`;
await send(subj, body);
