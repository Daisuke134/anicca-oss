// lib/slash-command.js — spec §12.1 row 4: the generic slash-command router for the LM Telegram bot.
//
// The legacy telegram_bot.py understood /start /help /status /where /stop /subscribe /connect
// /payout /reset (plus the live-location stream). LM's webhook only routed /start and /panel; every
// other /command fell through to the onboarding text handler and was answered with a setup nudge —
// dishonest routing. This module closes that gap:
//
//   parseSlashCommand  — recognises any /command (optional @BotName suffix, optional args)
//   slashAliasText     — /connect maps onto the SAME parsed-control flow as the natural-language
//                        "connect calendar" (Gmail stays intentionally OFF, so calendar only)
//   handleSlashCommand — /help /status /where /stop /subscribe /payout /reset are handled here with
//                        injected deps; /start and /panel PASS THROUGH untouched (their existing
//                        server.js branches stay the owner); an unknown /command gets an honest
//                        "unknown command" reply instead of an onboarding/feedback/browser-task
//                        misroute.
//
// HONESTY RULES carried over from the payout/feedback siblings: every reply is built only from data
// this process actually read (a location column that does not exist — accuracy — is never invented),
// a store operation we could not confirm is reported as a failure, and the reply for "nothing stored"
// says so instead of dressing up a no-op as a delete.
"use strict";

const { sendMessage } = require("./telegram.js");
const { parseUserCommand } = require("./user-command.js");
const { getLiveLocation, deleteLiveLocation } = require("./late-notice.js");
const { computeStage, setStage } = require("./telegram-onboard.js");
const { askPayoutQuestion } = require("./payout-question.js");
const { getLastWakeMiss, wakeMissLine } = require("./wake-miss.js");
const { TZ_ROW_KEYS } = require("./user-tz.js");
const { buildInvestmentReply, telegramExtra, validInvestmentSnapshot } = require("./investment-chat.js");
const { buildGigReply, validGigSnapshot } = require("./gig-chat.js");
const { paymentLink } = require("./payment-link.js");
const { economyReply } = require("./agent-economy-control.js");

// Every /command this bot understands. start/panel are listed for /help but owned elsewhere.
const KNOWN_COMMANDS = Object.freeze([
  "start", "panel", "help", "status", "where", "stop", "subscribe", "connect", "payout", "reset", "invest",
  "gig", "crowd", "economy",
]);
// /gig and /crowd share one handler shape (read state, build a platform reply, send, report the same
// result contract as /invest) but each is pinned to exactly one marketplace platform — never guessed
// from user input, so there is no way to ask this handler for the "wrong" platform's state.
const GIG_PLATFORM_BY_COMMAND = Object.freeze({ gig: "coconala", crowd: "crowdworks" });
// INTENTIONAL CHANGE: matching is EXACT, so "/startfoo" is its own (unknown) command rather than the
// start it used to be under telegram.js's startsWith("/start") check — Telegram deep links always
// deliver the payload space-separated ("/start airplane", "/start@bot spaceship"), never glued on.
const PASSTHROUGH = new Set(["start", "panel"]);
// The slash aliases that re-enter the existing parsed-control flow instead of being handled here.
const NL_ALIASES = Object.freeze({ connect: "connect calendar" });
// The argument spellings /connect may forward to "connect calendar" — mirrors user-command.js's
// /^(connect|reconnect) (my )?(google )?calendar$/ plus the obvious short forms. Anything else is a
// provider we cannot connect, and is answered instead of being silently rewritten to the calendar.
const CALENDAR_ARG = /^(my |your )?(google ?)?(calendar|gcal|cal)$|^(google ?)?カレンダー$/;

function parseSlashCommand(text) {
  const value = String(text || "").trim();
  const match = /^\/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+([\s\S]*))?$/.exec(value);
  if (!match) return null;
  return { name: match[1].toLowerCase(), args: String(match[2] || "").trim() };
}

function isCalendarArg(args) {
  const value = String(args || "").trim().toLowerCase().replace(/\s+/g, " ");
  return value === "" || CALENDAR_ARG.test(value);
}

function slashAliasText(parsed) {
  if (!parsed || !NL_ALIASES[parsed.name]) return null;
  if (parsed.name === "connect" && !isCalendarArg(parsed.args)) return null;
  return NL_ALIASES[parsed.name];
}

function helpMessage() {
  // kind:"help" wiring: parseUserCommand computes availableActions for unrecognised text; server.js
  // used to drop them. /help is where that computation finally reaches the user.
  const help = parseUserCommand("");
  return [
    "Commands:",
    "  /start — begin (or resume) onboarding",
    "  /panel — open your dashboard panel",
    "  /help — this message",
    "  /status — your Life Manager health snapshot",
    "  /where — show the live location I currently know",
    "  /stop — delete your stored live location",
    "  /subscribe — get the subscription link",
    "  /connect — connect Google Calendar",
    "  /payout — set or change your payout destination",
    "  /reset — restart the onboarding announcements",
    "  /invest — open Investment Loop",
    "  /gig — open the Coconala gig lane",
    "  /crowd — open the CrowdWorks gig lane",
    "  /economy — Agent Economy status and emergency control",
    "",
    "You can also type:",
    ...help.availableActions.map((action) => `  ${action}`),
  ].join("\n");
}

function setupFirstMessage(name) {
  return `Complete Life Manager setup with /start before using /${name}.`;
}

function coarseCoord(value) {
  return Number(value).toFixed(2);
}

function secondsAgo(iso, nowMs) {
  return Math.max(0, Math.round((nowMs - Date.parse(iso)) / 1000));
}

function minutesUntil(iso, nowMs) {
  return Math.max(0, Math.ceil((Date.parse(iso) - nowMs) / 60_000));
}

// Privacy-safe: coordinates rounded to ~1km, no full-precision echo into chat history, and no
// accuracy field — the lm_user_locations schema does not store one, so we never fabricate it.
function whereMessage(location, nowMs) {
  return [
    "📍 Last live fix (rounded to ~1km):",
    `  lat: ${coarseCoord(location.latitude)}`,
    `  lon: ${coarseCoord(location.longitude)}`,
    `  observed: ${secondsAgo(location.observed_at, nowMs)}s ago`,
    `  expires: in ${minutesUntil(location.expires_at, nowMs)} min`,
  ].join("\n");
}

const NO_LOCATION_MESSAGE =
  "I don't have a fresh live location for you. Share Live Location in Telegram and I'll track it.";

function payoutStatusLine(destination) {
  if (!destination || typeof destination !== "object") return "not set";
  if (destination.status === "usable" || destination.status === "ready") return "registered";
  if (destination.status === "awaiting_address") return "awaiting typed address";
  if (destination.status === "awaiting_details") return "rail chosen, awaiting details";
  return "unknown";
}

function subscriptionLine(row) {
  return row.paid === true ? "Plus active" : "free monthly allowance";
}

// The IANA zone this row carries, or null. Deliberately no fallback: lib/user-tz.js' rule is that a
// zone we do not have is not evidence the user lives in Tokyo, so wakeMissLine labels the time UTC
// instead of printing a wall clock that belongs to somebody else.
function rowTimeZone(row) {
  for (const key of TZ_ROW_KEYS) {
    const value = row && row[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

// The health snapshot is a projection of what the webhook can actually read: the lm_users row it was
// handed, one live-location read, and one lm_wake_miss read. Cron/daemon health still lives in stores
// this HTTP path cannot cheaply reach and stays OMITTED rather than fabricated — but the missed-call
// line is NOT in that category (spec §3 row 1b, §5.5): it is a plain Supabase read like the location
// one, and leaving it out is what let three days of unrung calls look like nothing had happened.
function statusMessage(row, location, nowMs, env, miss) {
  // Same env + clock as the subscription line, so the stage and the paywall never disagree.
  const stage = computeStage(row, { env, now: nowMs });
  return [
    "🩺 Life Manager status",
    stage === "done" ? "🚀 Onboarding: done" : `🚀 Onboarding: at the "${stage}" step`,
    `📅 Calendar: ${row.calendar_provider === "composio_gcal" ? "connected" : "not connected"}`,
    `📱 Phone: ${row.phone ? "on file" : "not set"}`,
    `⭐ Monthly allowance: ${subscriptionLine(row)}`,
    `💸 Payout: ${payoutStatusLine(row.payout_destination)}`,
    location
      ? `📍 Location: fresh (observed ${secondsAgo(location.observed_at, nowMs)}s ago)`
      : "📍 Location: not available (share Live Location in Telegram)",
    wakeMissLine(miss, nowMs, { timeZone: rowTimeZone(row) }),
  ].join("\n");
}

async function handleSlashCommand(parsed, row, deps = {}) {
  if (!parsed || PASSTHROUGH.has(parsed.name)) return { handled: false };
  const name = parsed.name;
  const send = deps.send || sendMessage;
  const log = deps.log || console.log;
  const nowMs = deps.nowMs == null ? Date.now() : deps.nowMs;
  const chatId = String(deps.chatId || "");
  const env = deps.env || process.env;

  if (!KNOWN_COMMANDS.includes(name)) {
    await send(deps.token, chatId, `Unknown command: /${name}. Send /help to see what I understand.`);
    return { handled: true, action: "unknown" };
  }

  if (name === "help") {
    await send(deps.token, chatId, helpMessage());
    return { handled: true, action: "help" };
  }

  if (name === "connect") {
    // Only reachable when slashAliasText DECLINED the alias, i.e. the argument named something other
    // than the calendar. Google Calendar is the one connectable provider (Gmail is intentionally off),
    // so say that instead of connecting the calendar the user did not ask for. The argument itself is
    // never echoed: sendMessage posts parse_mode HTML, so echoing raw user text is an injection and a
    // 400 risk. Needs no row — it is a statement about this bot, not about the account.
    await send(deps.token, chatId,
      "I can only connect Google Calendar right now — Gmail and other providers are intentionally off. Send /connect with no argument to connect your calendar.");
    return { handled: true, action: "connect", ok: false, reason: "unsupported_provider" };
  }

  if (name === "subscribe") {
    if (row && row.paid === true) {
      await send(deps.token, chatId, "⭐ Your Life Manager subscription is already active.");
      return { handled: true, action: "subscribe", ok: true, alreadyActive: true };
    }
    if (!row || !row.uid || !chatId) {
      await send(deps.token, chatId, "先に /start からライフマネージャーを始めてください。");
      return { handled: true, action: "subscribe", ok: false, reason: "unlinked" };
    }
    const checkout = paymentLink({ stripePaymentLink: deps.stripePaymentLink }, { uid: row.uid });
    if (!checkout) {
      await send(deps.token, chatId, "現在、購読リンクを開けません。少し時間をおいてもう一度お試しください。");
      return { handled: true, action: "subscribe", ok: false, reason: "link_unavailable" };
    }
    await send(deps.token, chatId,
      "今月の利用を続ける場合は、月額$29でライフマネージャーを続けられます。いつでも解約できます。",
      { reply_markup: { inline_keyboard: [[{ text: "月額$29で続ける", url: checkout }]] } });
    return { handled: true, action: "subscribe", ok: true };
  }

  // Everything below reads or mutates THIS user's account and needs the linked row.
  if (!row || !row.uid) {
    await send(deps.token, chatId, setupFirstMessage(name));
    return { handled: true, action: name, ok: false, reason: "unlinked" };
  }

  if (name === "invest") {
    let snapshot;
    try {
      snapshot = deps.getInvestmentState ? await deps.getInvestmentState(row.uid) : { lifecycle: "unknown" };
    } catch {
      snapshot = { lifecycle: "unknown" };
    }
    const ok = validInvestmentSnapshot(snapshot);
    const reply = buildInvestmentReply(ok ? snapshot : { lifecycle: "unknown" });
    const delivery = await send(deps.token, chatId, reply.text, telegramExtra(reply));
    const providerMessageId = delivery && delivery.ok === true &&
      Number.isSafeInteger(delivery.result?.message_id) && delivery.result.message_id > 0
      ? delivery.result.message_id : null;
    if (delivery?.ok === false) {
      return { handled: true, action: "invest", ok: false, reason: "delivery_failed" };
    }
    if (providerMessageId == null) {
      return { handled: true, action: "invest", ok: false, reason: "delivery_unconfirmed" };
    }
    return {
      handled: true,
      action: "invest",
      ok,
      ...(providerMessageId == null ? {} : { providerMessageId }),
      ...(ok ? {} : { reason: "state_unavailable" }),
    };
  }

  if (name === "economy") {
    let snapshot;
    try {
      snapshot = deps.getEconomyState ? await deps.getEconomyState(row.uid) : null;
    } catch {
      snapshot = null;
    }
    const reply = economyReply(snapshot);
    const delivery = await send(deps.token, chatId, reply.text, reply.extra);
    const providerMessageId = delivery && delivery.ok === true
      && Number.isSafeInteger(delivery.result?.message_id) && delivery.result.message_id > 0
      ? delivery.result.message_id : null;
    if (delivery?.ok === false) return { handled: true, action: "economy", ok: false, reason: "delivery_failed" };
    if (providerMessageId == null) return { handled: true, action: "economy", ok: false, reason: "delivery_unconfirmed" };
    return { handled: true, action: "economy", ok: true, providerMessageId };
  }

  if (name === "gig" || name === "crowd") {
    const platform = GIG_PLATFORM_BY_COMMAND[name];
    let snapshot;
    try {
      snapshot = deps.getGigState ? await deps.getGigState(platform, row.uid) : { lifecycle: "unknown" };
    } catch {
      snapshot = { lifecycle: "unknown" };
    }
    const ok = validGigSnapshot(snapshot);
    const reply = buildGigReply(platform, ok ? snapshot : { lifecycle: "unknown" });
    const delivery = await send(deps.token, chatId, reply.text, telegramExtra(reply));
    const providerMessageId = delivery && delivery.ok === true &&
      Number.isSafeInteger(delivery.result?.message_id) && delivery.result.message_id > 0
      ? delivery.result.message_id : null;
    if (delivery?.ok === false) {
      return { handled: true, action: name, ok: false, reason: "delivery_failed" };
    }
    if (providerMessageId == null) {
      return { handled: true, action: name, ok: false, reason: "delivery_unconfirmed" };
    }
    return {
      handled: true,
      action: name,
      ok,
      ...(providerMessageId == null ? {} : { providerMessageId }),
      ...(ok ? {} : { reason: "state_unavailable" }),
    };
  }

  if (name === "where") {
    const location = await (deps.getLiveLocation || ((uid) => getLiveLocation(uid, nowMs, deps)))(row.uid);
    if (!location) {
      await send(deps.token, chatId, NO_LOCATION_MESSAGE);
      return { handled: true, action: "where", ok: true, stored: false };
    }
    await send(deps.token, chatId, whereMessage(location, nowMs));
    return { handled: true, action: "where", ok: true };
  }

  if (name === "stop") {
    const outcome = await (deps.deleteLiveLocation || ((uid) => deleteLiveLocation(uid, deps)))(row.uid);
    // Audit names the decision and the outcome, never the coordinates.
    log(`[location] /stop uid=${String(row.uid).slice(0, 12)} deleted=${outcome ? outcome.deleted : "error"}`);
    if (!outcome) {
      await send(deps.token, chatId, "I couldn't delete your stored location. Please try again.");
      return { handled: true, action: "stop", ok: false, reason: "delete_failed" };
    }
    await send(deps.token, chatId, outcome.deleted > 0
      ? "Stopped. Your stored live location was deleted. Share Live Location again any time to resume."
      : "You had no stored live location. Nothing was deleted.");
    return { handled: true, action: "stop", ok: true, deleted: outcome.deleted };
  }

  if (name === "reset") {
    // Restart the onboarding announcements: the stage itself is COMPUTED from the row's real data
    // (calendar/phone/paid), so /reset never wipes those — it rewinds the announced stage so /start
    // and the nudge loop re-walk the flow from the beginning.
    //
    // COUPLING, NAMED ON PURPOSE: tg_onboard_stage is not only the announcement bookmark, it is also
    // the browser-task gate — lib/browser-task-intake.js accepts a task only while the column reads
    // "done". Rewinding it here therefore PAUSES browser-task intake until the stage is announced as
    // complete again (/start re-announces and re-persists it immediately; otherwise the 2-minute nudge
    // loop does, subject to its 30-minute per-user cooldown). That is a real user-visible side effect,
    // so the confirmation below discloses it rather than letting tasks go quiet unexplained.
    await (deps.setStage || setStage)(row.uid, "calendar", deps.supaUrl, deps.supaKey);
    await send(deps.token, chatId, [
      "Onboarding announcements were reset. Send /start to walk through setup again.",
      "",
      "⚠️ Browser tasks are paused until your onboarding is announced as complete again — I only accept them at the \"done\" stage. Nothing else was deleted.",
    ].join("\n"));
    return { handled: true, action: "reset", ok: true };
  }

  if (name === "payout") {
    // askPayoutQuestion is the idempotence: it reads payout_destination first, replies "already
    // registered" itself when one exists, and only then re-opens the picker — so /payout can never
    // stack a second pending question flow on top of the callback one.
    const outcome = await (deps.askPayoutQuestion || askPayoutQuestion)({
      uid: row.uid, chatId, token: deps.token,
      supaUrl: deps.supaUrl, supaKey: deps.supaKey, sendMessage: send,
    });
    log(`[payout] slash reopen asked=${outcome.asked}${outcome.reason ? ` reason=${outcome.reason}` : ""}`);
    if (outcome.asked || outcome.reason === "already_answered") {
      return { handled: true, action: "payout", ok: true, ...outcome };
    }
    await send(deps.token, chatId, "I couldn't open the payout registration right now. Please try again shortly.");
    return { handled: true, action: "payout", ok: false, ...outcome };
  }

  // /status
  const location = await (deps.getLiveLocation || ((uid) => getLiveLocation(uid, nowMs, deps)))(row.uid);
  // A ledger read we could not complete returns null, which prints "no missed call recorded". That is
  // the one honesty compromise here and it is bounded: the scheduler ALSO logs every miss, so an
  // unreachable ledger degrades this line to silence rather than to a fabricated failure.
  const miss = await (deps.getLastWakeMiss || ((uid) => getLastWakeMiss(uid, deps)))(row.uid);
  await send(deps.token, chatId, statusMessage(row, location, nowMs, env, miss));
  return { handled: true, action: "status", ok: true };
}

module.exports = {
  KNOWN_COMMANDS,
  parseSlashCommand,
  slashAliasText,
  helpMessage,
  handleSlashCommand,
};
