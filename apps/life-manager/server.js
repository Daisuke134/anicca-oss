// apps/life-call/server.js — Life Manager CLOUD wake-call service (Railway, always-on).
//
// Two things in one process:
//   1. A PERSISTENT Gemini-Charon bridge at  wss://<svc>.up.railway.app/ws  — multi-call, with
//      per-call context read from the WS upgrade URL query (?summary=&dateTime=&location=&urgency=).
//      Telnyx streams the call's RTP here; we bridge it bidirectionally to Gemini Live (voice Charon).
//   2. The 60-second SCHEDULER (scheduler.js) that finds users due for a T-15min wake and dials them
//      with stream_url pointing back at THIS service's /ws.
//
// Unlike the local runner-telnyx.mjs (ephemeral cloudflared tunnel + one bridge per call), this is a
// stable always-on server: Railway gives a permanent public wss, so no cloudflared, no Mac-mini.
"use strict";

const http = require("http");
const crypto = require("crypto");
const { URL } = require("url");
const WebSocket = require("ws");
const {
  routeTelnyxMessage,
  routeGeminiMessage,
  geminiSetupForEvent,
  buildTelnyxMediaFrame,
  carrierActionForGeminiKind,
  makeGeminiEndHandler,
} = require("./lib/call-bridge.cjs");
const {
  geminiLiveWsUrl,
  buildGeminiTurn,
  parseGeminiTranscripts,
} = require("./lib/call-logic.js");
const { startScheduler, startWakeLoop, startReminderLoop, startTravelLoop, startAskLoop, startOnboardLoop, startDiscoveryLoop, buildStreamUrl, langForPhone } = require("./scheduler.js");
const { openingTurnForLang, resolveCallLang } = require("./lib/call-language.js");
const { maybeStartLoops, inngestConfigured } = require("./lib/maybe-start-loops.js");
const { compBootLog } = require("./lib/comp-window.js");
const { selfHealWebhook } = require("./lib/webhook-selfheal.js");
const { serve: inngestServe } = require("inngest/node"); // raw Node http server (NOT express) → use the node adapter
const { inngest } = require("./inngest/client.js");
const { functions: inngestFunctions } = require("./inngest/functions.js");
const inngestHandler = inngestServe({ client: inngest, functions: inngestFunctions });
const { placeCall, startRecording } = require("./lib/dial.js");
const { recordTelnyxWakeReceipt } = require("./lib/telnyx-receipt.js");
const { amdEnabled, shouldMarkAnswered } = require("./lib/answered.js");
const { decodeCallClientState, encodeTestCallClientState, verifyTelnyxSignature } = require("./lib/telnyx-webhook.js");
const { parseUpdate, sendMessage, editMessageText, answerCallbackQuery, isPanelCommand, isPanelDeepLink, routeCallbackData, startReply } = require("./lib/telegram.js");
const { reflectAnswer } = require("./lib/telegram-callback-visibility.js");
const {
  createSupabaseLateApprovalStore,
  handleLateApprovalCallback,
} = require("./lib/late-approval.js");
const { sendPanelLink, handlePanelRequest, handleMoneyPrinterGuestRequest, panelDeviceCodeFromCommand, confirmPanelDeviceCode, cookieValue, sessionScope, panelScopeCookie, claimTelegramWebhookActor } = require("./lib/panel-auth.js");
const { handlePanelApiRequest, handlePanelOAuthCallback, handleTelegramOAuthCallback, composioCalendarStart, composioCalendarDisconnect } = require("./lib/panel-api.js");
const { createMoneyPrinterSource } = require("./lib/money-printer-source.js");
const { createMoneyPrinterRuntimeStore } = require("./lib/money-printer-runtime-store.js");
const { handleMoneyPrinterSymphonyApiRequest } = require("./lib/money-printer-symphony-api.js");
const { renderAlpacaPublicPage, resolveAlpacaPublicProjection } = require("./lib/alpaca-public.js");
const { createSupabaseCommandStore } = require("./lib/panel-api.js");
const { handleCalendarOnboardRequest } = require("./lib/calendar-onboard.js");
const { parseUserCommand, dispatchParsedControl, executeUserCommand } = require("./lib/user-command.js");
const { parseSlashCommand, slashAliasText, handleSlashCommand } = require("./lib/slash-command.js");
const { createInvestmentStateStore } = require("./lib/investment-state-store.js");
const { handleFeedbackMessage, createPostgresFeedbackStore } = require("./lib/feedback-intake.js");
const { resolveTelegramReply } = require("./lib/telegram-reply.js");
const { handleInboundReply, handleAskCallback, parseInboundRecipient } = require("./lib/ask.js");
const { isReplyToken } = require("./lib/reply-token.js");
const {
  rowByChatId, handleOnboardingText, handleGmailCallback,
} = require("./lib/telegram-onboard.js");
const { createHostedGmailLink } = require("./lib/gmail-onboard.js");
const { mailAvailable } = require("./lib/mail-availability.js");
const {
  markAnswered, applyAmdDetection, applyTestCallDetection, upsertLiveLocation,
} = require("./lib/late-notice.js");
const { handleDiscoveryCallback } = require("./lib/feature-discovery.js");
const { handleCfoTelegramCallback } = require("./lib/cfo-telegram-callback.js");
const { handlePayoutCallback } = require("./lib/payout-question.js");
const { handleDietCallback } = require("./lib/diet-runtime.js");
const { handlePreceptsCallback } = require("./lib/precepts-runtime.js");
const { handleTypedPayoutAddress } = require("./lib/payout-address-intake.js");
const { handleBrowserTaskMessage } = require("./lib/browser-task-intake.js");
const { completeBrowserHandoff, startBrowserJobLoop } = require("./lib/browser-job-runtime.js");
const { startInvestmentDryRunLoop } = require("./lib/investment-dry-run.js");
const { makeSteelCdpClient } = require("./lib/steel-cdp-client.js");
const { claimEvent, unclaimEvent, applyBilling } = require("./lib/billing.js");
const { constructStripeWebhookEvent, stripeWebhookAllowed } = require("./lib/stripe-webhook-signature.js");
const { recordCost } = require("./lib/ledger.js");
const { recordUsageEvent } = require("./lib/usage-event.js");
const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY || "sk_test_placeholder"); // apiKey unused by constructEvent
const SUPA_URL = process.env.SUPABASE_URL, SUPA_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
const COMPOSIO_KEY = process.env.COMPOSIO_API_KEY;
let moneyPrinterSource, moneyPrinterRuntimePool, moneyPrinterRuntimeStore, investmentStateStore;
function getMoneyPrinterRuntimeStore() {
  if (!moneyPrinterRuntimeStore) {
    const connectionString = String(process.env.LM_RUNTIME_DATABASE_URL || process.env.LM_FEEDBACK_DATABASE_URL || "").trim();
    if (!connectionString) throw new Error("money printer runtime store unavailable");
    moneyPrinterRuntimePool = new (require("pg").Pool)({ connectionString, max: 4 });
    moneyPrinterRuntimeStore = createMoneyPrinterRuntimeStore({ query: moneyPrinterRuntimePool.query.bind(moneyPrinterRuntimePool) });
  }
  return moneyPrinterRuntimeStore;
}
function getInvestmentStateStore() {
  getMoneyPrinterRuntimeStore();
  if (!investmentStateStore) {
    investmentStateStore = createInvestmentStateStore({ query: moneyPrinterRuntimePool.query.bind(moneyPrinterRuntimePool) });
  }
  return investmentStateStore;
}
async function readBrowserHandoff(uid) {
  getMoneyPrinterRuntimeStore();
  const rows = (await moneyPrinterRuntimePool.query(`
    SELECT id, uid, status, principal_kind, receipt, finished_at FROM public.lm_browser_jobs
    WHERE uid = $1 AND status = 'handoff_required' AND receipt->>'steel_released' = 'false'
      AND finished_at > clock_timestamp() - interval '10 minutes'
    ORDER BY finished_at DESC LIMIT 2
  `, [uid])).rows;
  if (rows.length === 0) return null;
  if (rows.length !== 1) throw new Error("browser handoff unavailable");
  const row = rows[0], sessionId = String(row.receipt && row.receipt.session_id || "");
  const finishedAt = Date.parse(row.finished_at);
  let origin;
  try { origin = new URL(row.receipt && row.receipt.selected_origin); } catch { throw new Error("browser handoff unavailable"); }
  if (row.uid !== uid || !/^[A-Za-z0-9._-]{1,200}$/.test(String(row.id || ""))
    || !/^[A-Za-z0-9._-]{1,200}$/.test(sessionId) || !Number.isFinite(finishedAt)
    || origin.protocol !== "https:" || origin.username || origin.password
    || !["agent_owned", "user_provided"].includes(row.principal_kind)) {
    throw new Error("browser handoff unavailable");
  }
  return { browser_job_id: String(row.id), session_id: sessionId, expired: Date.now() >= finishedAt + 10 * 60 * 1000 };
}
async function finishBrowserHandoff(uid, answer) {
  const handoff = await readBrowserHandoff(uid);
  if (!handoff || handoff.expired) throw new Error("browser handoff unavailable");
  const completed = await completeBrowserHandoff(handoff.session_id, answer);
  if (answer === "approve" && completed.providerReceipt.confirmed !== true) {
    const error = new Error("provider_unconfirmed"); error.status = 409; throw error;
  }
  const receipt = completed.providerReceipt;
  const safeReceipt = {
    confirmed: receipt.confirmed === true,
    status: String(receipt.status || "unknown").slice(0, 100),
    confirmation_id: receipt.confirmationId ? String(receipt.confirmationId).slice(0, 200) : null,
    current_url: receipt.currentUrl || null,
    handoff_required: receipt.handoffRequired === true,
    handoff_reason: receipt.handoffReason || null,
  };
  const rows = (await moneyPrinterRuntimePool.query(`
    UPDATE public.lm_browser_jobs
    SET receipt = jsonb_set(jsonb_set(receipt, '{provider_receipt}', $3::jsonb), '{steel_released}', 'true'::jsonb),
        updated_at = clock_timestamp()
    WHERE id = $1 AND uid = $2 AND status = 'handoff_required'
      AND receipt->>'session_id' = $4 AND receipt->>'steel_released' = 'false'
    RETURNING id
  `, [handoff.browser_job_id, uid, JSON.stringify(safeReceipt), handoff.session_id])).rows;
  if (rows.length !== 1) throw new Error("browser handoff unavailable");
  return true;
}
function getMoneyPrinterSource(scope) {
  if (!moneyPrinterSource) {
    moneyPrinterSource = createMoneyPrinterSource({
      supaUrl: SUPA_URL, supaKey: SUPA_KEY, fetchImpl: fetch,
      runtimeStore: getMoneyPrinterRuntimeStore(),
    });
  }
  return moneyPrinterSource(scope);
}
function panelApiOptions(path, options, getRuntimeStore = getMoneyPrinterRuntimeStore) {
  if (!path.startsWith("/api/panel/money-printer")) return options;
  const runtimeStore = getRuntimeStore();
  return {
    ...options,
    runtimeStore,
    opportunityStore: runtimeStore,
    humanTaskStore: runtimeStore,
    browserHandoffReader: readBrowserHandoff,
    completeBrowserHandoff: finishBrowserHandoff,
    moneyPrinterSource: getMoneyPrinterSource,
  };
}
const LM_INBOUND_SECRET = process.env.LM_INBOUND_SECRET || ""; // shared secret in the Resend inbound webhook URL

const LM_TG_TOKEN = process.env.LM_TELEGRAM_BOT_TOKEN || "";
const LM_TG_SECRET = process.env.LM_TELEGRAM_WEBHOOK_SECRET || "";
const LM_LATE_APPROVAL_CALLBACK_SECRET = process.env.LM_LATE_APPROVAL_CALLBACK_SECRET
  || process.env.LM_UID_SECRET || LM_TG_SECRET || undefined;
const PUBLIC_BASE = process.env.PUBLIC_BASE || "https://aniccaai.com";
const BROWSER_TAKEOVER_PATH = "/api/panel/money-printer/browser";
const BROWSER_CAST_PATH = `${BROWSER_TAKEOVER_PATH}/cast`;
const BROWSER_CAST_TTL_MS = 2 * 60 * 1000;
const BROWSER_CAST_SECRET = process.env.LM_BROWSER_SESSION_KEY || "";
const browserTakeoverSteel = makeSteelCdpClient();
function steelCastUrl(sessionId, viewer = {}) {
  const id = String(sessionId || "");
  if (!/^[A-Za-z0-9._-]{1,200}$/.test(id)) throw new Error("Steel cast session invalid");
  const entries = Object.entries(viewer || {}).filter(([, value]) => value != null && value !== "");
  if (entries.length > 1) throw new Error("Steel cast session invalid");
  const query = new URLSearchParams({ sessionId: id });
  if (entries.length === 1) {
    const [name, value] = entries[0];
    if (name === "tabInfo" && value === "true") query.set(name, value);
    else if (name === "pageId" && /^[A-Za-z0-9._-]{1,200}$/.test(value)) query.set(name, value);
    else if (name === "pageIndex" && /^(?:0|[1-9][0-9]{0,2})$/.test(value)) query.set(name, value);
    else throw new Error("Steel cast session invalid");
  }
  return `ws://steel-browser.railway.internal:8080/v1/sessions/cast?${query.toString()}`;
}
function createBrowserCastTicket(value, secret = BROWSER_CAST_SECRET) {
  const uid = String(value && value.uid || "");
  const browserJobId = String(value && value.browserJobId || "");
  const expiresAt = Number(value && value.expiresAt);
  if (!/^[a-z0-9][a-z0-9._-]{0,199}$/.test(uid)
    || !/^[A-Za-z0-9._-]{1,200}$/.test(browserJobId)
    || !Number.isSafeInteger(expiresAt)
    || typeof secret !== "string" || secret.length < 32) {
    throw new Error("browser cast ticket invalid");
  }
  const payload = Buffer.from(`v1\n${uid}\n${browserJobId}\n${expiresAt}`, "utf8").toString("base64url");
  const signature = crypto.createHmac("sha256", secret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}
function verifyBrowserCastTicket(ticket, secret = BROWSER_CAST_SECRET, now = Date.now()) {
  const match = typeof ticket === "string" ? ticket.match(/^([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]{43})$/) : null;
  if (!match || typeof secret !== "string" || secret.length < 32 || !Number.isFinite(now)) return null;
  const expected = crypto.createHmac("sha256", secret).update(match[1]).digest("base64url");
  const actualBytes = Buffer.from(match[2]), expectedBytes = Buffer.from(expected);
  if (actualBytes.length !== expectedBytes.length || !crypto.timingSafeEqual(actualBytes, expectedBytes)) return null;
  let parts;
  try { parts = Buffer.from(match[1], "base64url").toString("utf8").split("\n"); } catch { return null; }
  if (parts.length !== 4 || parts[0] !== "v1"
    || !/^[a-z0-9][a-z0-9._-]{0,199}$/.test(parts[1])
    || !/^[A-Za-z0-9._-]{1,200}$/.test(parts[2])
    || !/^[0-9]{13}$/.test(parts[3])) return null;
  const expiresAt = Number(parts[3]);
  if (!Number.isSafeInteger(expiresAt) || expiresAt > now + BROWSER_CAST_TTL_MS) return null;
  if (expiresAt <= now) return { expired: true };
  return { uid: parts[1], browserJobId: parts[2], expiresAt };
}
function browserCastPublicUrl(ticket, env = process.env) {
  const domain = String(env && env.RAILWAY_PUBLIC_DOMAIN || "").trim().toLowerCase();
  if (!/^[a-z0-9-]+\.up\.railway\.app$/.test(domain)
    || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$/.test(String(ticket || ""))) {
    throw new Error("browser cast public domain invalid");
  }
  return `wss://${domain}${BROWSER_CAST_PATH}/${encodeURIComponent(ticket)}`;
}
// The panel is served by this life-call HTTP service, not by the /lm onboarding site.
// Railway supplies RAILWAY_PUBLIC_DOMAIN; LM_PANEL_BASE_URL is the explicit override for custom domains.
const LM_PANEL_BASE = process.env.LM_PANEL_BASE_URL ||
  (process.env.RAILWAY_PUBLIC_DOMAIN ? `https://${process.env.RAILWAY_PUBLIC_DOMAIN}` : "");

function panelOriginForPath(path) {
  return String(path).startsWith("/api/panel/money-printer") ? PUBLIC_BASE : LM_PANEL_BASE;
}

async function browserTakeover(req) {
  const session = cookieValue(req.headers.cookie, "__Host-lm_panel_session")
    || cookieValue(req.headers.cookie, "lm_panel_session");
  const scope = await sessionScope(session, { supaUrl: SUPA_URL, supaKey: SUPA_KEY });
  if (!scope) return { status: 401 };
  const handoff = await readBrowserHandoff(scope.uid);
  if (!handoff) return { status: 404, scope };
  if (handoff.expired) return { status: 410, scope };
  await browserTakeoverSteel.assertLiveSession(handoff.session_id);
  return { status: 200, scope, handoff };
}

async function browserTakeoverTicket(req) {
  let parsed, ticket;
  try {
    parsed = new URL(req.url || BROWSER_CAST_PATH, "https://life-call.invalid");
    const prefix = `${BROWSER_CAST_PATH}/`;
    if (!parsed.pathname.startsWith(prefix)) return { status: 401 };
    ticket = decodeURIComponent(parsed.pathname.slice(prefix.length));
  }
  catch { return { status: 401 }; }
  if (!ticket || ticket.includes("/")) return { status: 401 };
  const viewer = {};
  for (const [name, value] of parsed.searchParams) {
    if (Object.prototype.hasOwnProperty.call(viewer, name)) return { status: 401 };
    viewer[name] = value;
  }
  try { steelCastUrl("probe", viewer); } catch { return { status: 401 }; }
  const claim = verifyBrowserCastTicket(ticket);
  if (!claim) return { status: 401 };
  if (claim.expired) return { status: 410 };
  const handoff = await readBrowserHandoff(claim.uid);
  if (!handoff) return { status: 410 };
  if (handoff.browser_job_id !== claim.browserJobId) return { status: 404 };
  await browserTakeoverSteel.assertLiveSession(handoff.session_id);
  return { status: 200, handoff, viewer };
}

function rejectUpgrade(socket, status) {
  const text = status === 401 ? "Unauthorized" : status === 404 ? "Not Found" : status === 410 ? "Gone" : "Bad Gateway";
  try { socket.end(`HTTP/1.1 ${status} ${text}\r\nConnection: close\r\n\r\n`); } catch { socket.destroy(); }
}

// inngestServeAllowed: pure helper — returns true when the /api/inngest route may serve requests.
// In dev (INNGEST_DEV=1) it always returns true; in prod it requires INNGEST_SIGNING_KEY.
// Exported for testing (FIND-005).
function inngestServeAllowed(env) {
  return inngestConfigured(env);
}

// dunningNotify(uid): best-effort ONE message when a subscription goes past_due (REQ-40). Telegram if we
// have the user's chat, else logged (email is a later enhancement). NEVER throws (dunning must not 500 the webhook).
async function dunningNotify(uid) {
  try {
    if (!SUPA_URL || !SUPA_KEY) return;
    const r = await fetch(`${SUPA_URL}/rest/v1/lm_users?uid=eq.${encodeURIComponent(uid)}&select=telegram_chat_id,email`,
      { headers: { apikey: SUPA_KEY, Authorization: `Bearer ${SUPA_KEY}` } });
    const d = await r.json().catch(() => []);
    const row = Array.isArray(d) && d[0] ? d[0] : null;
    const msg = "⚠️ Your Life Manager payment didn't go through. Update your card to keep your wake calls active.";
    if (row && row.telegram_chat_id && LM_TG_TOKEN) await sendMessage(LM_TG_TOKEN, row.telegram_chat_id, msg);
    else console.log("[stripe] dunning (no telegram channel) uid=", uid, "email=", row && row.email);
  } catch (e) { console.error("[stripe] dunning err", e.message); }
}

const LM_UID_SECRET = process.env.LM_UID_SECRET || "";
const LM_FEEDBACK_PROVENANCE_KEY = process.env.LM_FEEDBACK_PROVENANCE_KEY || LM_UID_SECRET;
const LM_FEEDBACK_STORE = process.env.LM_FEEDBACK_DATABASE_URL
  ? createPostgresFeedbackStore(process.env.LM_FEEDBACK_DATABASE_URL)
  : null;
function verifyUid(uid, sig) {
  if (!LM_UID_SECRET || !uid || !sig) return false;
  const expected = crypto.createHmac("sha256", LM_UID_SECRET).update(uid).digest("base64url");
  const a = Buffer.from(String(sig)), b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
// SERVER-SIDE rate-limit for /test-call. The dashboard "Call me now" button disables after one tap, but
// that gate is client-side ONLY — a page reload resets it, so a user could otherwise spam billed Charon
// calls. Every test call is real money, so we enforce the limit here (single always-on process → a Map is
// authoritative). Cooldown stops reload-spam; the daily cap bounds total cost per user.
const TEST_CALL_COOLDOWN_MS = 10 * 60 * 1000; // 1 test call per uid per 10 min
const TEST_CALL_DAILY_MAX = 5;                // hard ceiling per uid per rolling 24h
const _testCallLog = new Map();               // uid -> [epoch ms]
function testCallAllowed(uid, now = Date.now()) {
  const arr = (_testCallLog.get(uid) || []).filter((t) => now - t < 24 * 3600 * 1000);
  const last = arr[arr.length - 1];
  if (last !== undefined && now - last < TEST_CALL_COOLDOWN_MS) {
    return { ok: false, retryAfter: Math.ceil((TEST_CALL_COOLDOWN_MS - (now - last)) / 1000) };
  }
  if (arr.length >= TEST_CALL_DAILY_MAX) return { ok: false, retryAfter: 3600 };
  arr.push(now);
  _testCallLog.set(uid, arr);
  return { ok: true };
}
async function userForUid(uid) {
  const url = process.env.SUPABASE_URL, key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return null;
  // phone (to dial) + call_language (user-chosen call language, may be null → fall back to phone) +
  // name (so the call can address them by name).
  const r = await fetch(`${url}/rest/v1/lm_users?uid=eq.${encodeURIComponent(uid)}&select=phone,call_language,name,gmail_account_id`,
    { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  const d = await r.json().catch(() => []);
  return Array.isArray(d) && d[0] ? d[0] : null;
}
function readBody(req) {
  return new Promise((resolve) => {
    let b = ""; req.on("data", (c) => { b += c; if (b.length > 1e5) req.destroy(); });
    req.on("end", () => resolve(b)); req.on("error", () => resolve(""));
  });
}
// readRawBody: collect the EXACT bytes as a Buffer (no utf8 string concat, which corrupts multi-byte chars
// split across chunks → Stripe signature mismatch). Used for the Stripe webhook where constructEvent must
// hash the raw bytes (FIND-005).
function readRawBody(req) {
  return new Promise((resolve) => {
    const chunks = []; let len = 0;
    req.on("data", (c) => { chunks.push(c); len += c.length; if (len > 1e5) req.destroy(); });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", () => resolve(Buffer.alloc(0)));
  });
}

// One tag, two readers (/health and the boot line). It was written out twice before, so a deploy
// could report one build to curl and another to the logs — the pair of them is the only way to tell
// live code apart from a deploy that never happened, and a pair that can disagree proves nothing.
const FALLBACK_BUILD_TAG = "lm2a-webhook-retry-v1";
function buildTag(env) {
  const sha = String(env && env.RAILWAY_GIT_COMMIT_SHA || "");
  return /^[0-9a-f]{40}$/i.test(sha) ? sha : FALLBACK_BUILD_TAG;
}
const BUILD_TAG = buildTag(process.env);
const PORT = Number(process.env.PORT) || 8788;
const GEMINI_KEY = process.env.GEMINI_API_KEY;
const DEBUG_TRANSCRIPTS = process.env.DEBUG_TRANSCRIPTS === "1";
const MAX_CONCURRENT = Number(process.env.MAX_CONCURRENT_CALLS) || 8;
const VALID_URGENCY = new Set(["gentle", "firm", "harsh"]);
let liveCalls = 0;

// Build a GCal-shaped event ({summary,start:{dateTime},location}) + urgency from the /ws query —
// AND authenticate it. Each Telnyx media stream carries its own signed context; an unsigned or
// tampered connection is rejected before any Gemini socket opens (no budget drain, no prompt
// injection). Returns null when the HMAC (over summary|dateTime|location|urgency, keyed by
// LM_CALL_SECRET) does not verify.
function ctxFromReq(req) {
  let q;
  try {
    q = new URL(req.url, "http://x").searchParams;
  } catch {
    return null;
  }
  const summary = (q.get("summary") || "").slice(0, 200);
  const dateTime = (q.get("dateTime") || "").slice(0, 40);
  const location = (q.get("location") || "").slice(0, 200);
  let urgency = q.get("urgency") || "gentle";
  if (!VALID_URGENCY.has(urgency)) urgency = "gentle";
  let lang = q.get("lang");
  if (lang !== "ja" && lang !== "en") lang = "en"; // call language follows the user (JP→ja, else en)
  const name = (q.get("name") || "").slice(0, 60); // who to address on the call (already sanitized when signed)
  const wakeUid = (q.get("wakeUid") || "").slice(0, 100);
  const wakeEventKey = (q.get("wakeEventKey") || "").slice(0, 300);
  const sig = q.get("sig") || "";

  const secret = process.env.LM_CALL_SECRET || "";
  const expected = crypto.createHmac("sha256", secret).update([summary, dateTime, location, urgency, lang, name, wakeUid, wakeEventKey].join("\n")).digest("base64url");
  const a = Buffer.from(sig);
  const b = Buffer.from(expected);
  if (!secret || a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;

  return { event: { summary, start: { dateTime }, location }, urgency, lang, name, wakeUid, wakeEventKey };
}

const server = http.createServer(async (req, res) => {
  const path = (req.url || "").split("?")[0];
  if (path === "/alpaca") {
    if (req.method !== "GET") {
      res.writeHead(405, { "allow": "GET", "cache-control": "no-store" });
      res.end("method not allowed");
      return;
    }
    res.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    });
    res.end(renderAlpacaPublicPage());
    return;
  }
  if (path === "/api/life-manager/alpaca/public") {
    if (req.method !== "GET") {
      res.writeHead(405, { "allow": "GET", "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
      res.end(JSON.stringify({ error: "method_not_allowed" }));
      return;
    }
    try {
      const projection = await resolveAlpacaPublicProjection({ supaUrl: SUPA_URL, supaKey: SUPA_KEY });
      res.writeHead(200, {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
      });
      res.end(JSON.stringify(projection));
    } catch {
      res.writeHead(503, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
      res.end(JSON.stringify({ error: "alpaca_public_projection_unavailable" }));
    }
    return;
  }
  if (path === "/money-printer") {
    handleMoneyPrinterGuestRequest(req, res, {
      supaUrl: SUPA_URL,
      supaKey: SUPA_KEY,
    }).catch((error) => {
      console.error("[money-printer-guest] request failed", error.message);
      if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff" });
      res.end("money printer unavailable");
    });
    return;
  }
  if (path.startsWith("/api/internal/money-printer/symphony/")) {
    const handled = handleMoneyPrinterSymphonyApiRequest(req, res, {
      secret: process.env.LM_SYMPHONY_BRIDGE_SECRET,
      getRuntimeStore: getMoneyPrinterRuntimeStore,
    });
    if (handled !== false) {
      Promise.resolve(handled).catch(() => {
        if (!res.headersSent) {
          res.writeHead(500, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
          res.end(JSON.stringify({ error: "internal_error" }));
        }
      });
      return;
    }
  }
  if (path === BROWSER_TAKEOVER_PATH) {
    if (req.method !== "GET") {
      res.writeHead(405, { Allow: "GET", "cache-control": "no-store" }); res.end(); return;
    }
    browserTakeover(req).then(async (state) => {
      if (state.status !== 200) { res.writeHead(state.status, { "cache-control": "no-store" }); res.end(); return; }
      const ticket = createBrowserCastTicket({
        uid: state.scope.uid,
        browserJobId: state.handoff.browser_job_id,
        expiresAt: Date.now() + BROWSER_CAST_TTL_MS,
      });
      const publicCastUrl = browserCastPublicUrl(ticket);
      const publicOrigin = new URL(publicCastUrl);
      const html = await browserTakeoverSteel.getInteractiveDebugger(
        state.handoff.session_id,
        publicCastUrl,
      );
      const renewed = panelScopeCookie(state.scope);
      res.writeHead(200, {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "content-security-policy": `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src wss://${publicOrigin.host}; img-src data:; frame-ancestors 'self'`,
        "x-content-type-options": "nosniff",
        ...(renewed ? { "set-cookie": renewed } : {}),
      });
      res.end(html);
    }).catch(() => {
      if (!res.headersSent) res.writeHead(502, { "cache-control": "no-store" });
      res.end();
    });
    return;
  }
  if (path === "/api/panel/session/telegram" || path === "/api/panel/session/device") {
    handlePanelRequest(req, res, {
      supaUrl: SUPA_URL, supaKey: SUPA_KEY, token: LM_TG_TOKEN,
      panelOrigin: LM_PANEL_BASE, panelBaseUrl: LM_PANEL_BASE,
      botUsername: process.env.LM_TELEGRAM_BOT_USERNAME,
    }).catch((error) => {
      console.error("[panel-auth] request failed", error.message);
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
      res.end(JSON.stringify({ error: "panel_auth_unavailable" }));
    });
    return;
  }
  if (path === "/api/panel/onboarding/calendar/start" || path === "/api/panel/onboarding/calendar/status") {
    handleCalendarOnboardRequest(req, res, {
      supaUrl: SUPA_URL,
      supaKey: SUPA_KEY,
      panelOrigin: LM_PANEL_BASE,
      panelBaseUrl: LM_PANEL_BASE,
      sessionSecret: process.env.LM_PANEL_SESSION_ROTATION_SECRET || process.env.LM_UID_SECRET,
      composioKey: COMPOSIO_KEY,
      composioAuthConfig: process.env.COMPOSIO_GCAL_AUTH_CONFIG,
    }).catch(() => {
      if (!res.headersSent) res.writeHead(502, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
      res.end(JSON.stringify({ error: "calendar_unavailable" }));
    });
    return;
  }
  if (path.startsWith("/api/panel/")) {
    handlePanelApiRequest(req, res, panelApiOptions(path, {
      supaUrl: SUPA_URL,
      supaKey: SUPA_KEY,
      timeZone: process.env.LM_TIME_ZONE || "Asia/Tokyo",
      panelOrigin: panelOriginForPath(path),
      panelBaseUrl: LM_PANEL_BASE,
      composioKey: COMPOSIO_KEY,
      composioAuthConfig: process.env.COMPOSIO_GCAL_AUTH_CONFIG,
    })).catch((error) => {
      console.error("[panel-api] request failed", error.message);
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
      res.end(JSON.stringify({ error: "panel_unavailable" }));
    });
    return;
  }
  if (path === "/panel" || path === "/panel/onboarding" || path === "/panel/logout") {
    handlePanelRequest(req, res, { supaUrl: SUPA_URL, supaKey: SUPA_KEY, token: LM_TG_TOKEN, panelOrigin: LM_PANEL_BASE, panelBaseUrl: LM_PANEL_BASE, botUsername: process.env.LM_TELEGRAM_BOT_USERNAME }).catch((error) => {
      console.error("[panel] request failed", error.message);
      if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" });
      res.end("panel unavailable");
    });
    return;
  }
  if (path === "/panel/oauth/calendar") {
    handlePanelOAuthCallback(req, res, { supaUrl: SUPA_URL, supaKey: SUPA_KEY, composioKey: COMPOSIO_KEY }).catch(() => {
      if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain", "cache-control": "no-store" });
      res.end("oauth callback unavailable");
    });
    return;
  }
  if (path === "/telegram/oauth/calendar") {
    const botUsername = String(process.env.LM_TELEGRAM_BOT_USERNAME || "").replace(/^@/, "");
    handleTelegramOAuthCallback(req, res, {
      supaUrl: SUPA_URL, supaKey: SUPA_KEY, composioKey: COMPOSIO_KEY,
      sendMessage: (chatId, text) => sendMessage(LM_TG_TOKEN, chatId, text),
      telegramReturnUrl: `https://t.me/${botUsername}`,
    }).catch((error) => {
      console.error("[telegram-oauth] callback failed", error && error.message);
      if (!res.headersSent) res.writeHead(503, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" });
      res.end("Return to Telegram and send /start again.");
    });
    return;
  }
  if (path === "/health" || path === "/") {
    res.writeHead(200, { "content-type": "application/json" });
    // `build` lets any deploy be verified from outside (curl /health) — proves new code is live.
    res.end(JSON.stringify({ ok: true, service: "life-call", ws: "/ws", build: BUILD_TAG }));
    return;
  }
  // Telnyx Call Control webhook. Standard AMD produces call.machine.detection.ended with
  // data.payload.result=human|machine|not_sure. Only an authenticated, explicit human result may
  // mark the correlated wake row answered; call.answered and media start are not human proof.
  if (path === "/telnyx-events") {
    if (req.method !== "POST") { res.writeHead(405); res.end("method"); return; }
    if (!process.env.TELNYX_PUBLIC_KEY) { res.writeHead(503); res.end("telnyx public key not configured"); return; }
    (async () => {
      const rawBody = await readRawBody(req);
      const verified = verifyTelnyxSignature({
        rawBody,
        signature: req.headers["telnyx-signature-ed25519"],
        timestamp: req.headers["telnyx-timestamp"],
        publicKey: process.env.TELNYX_PUBLIC_KEY,
      });
      if (!verified) { res.writeHead(403); res.end("invalid signature"); return; }

      let event;
      try { event = JSON.parse(rawBody.toString("utf8")); }
      catch { res.writeHead(400); res.end("invalid json"); return; }
      const data = event && event.data;
      const payload = data && data.payload;
      const amdEvent = data && data.event_type === "call.machine.detection.ended";
      const hangupEvent = data && data.event_type === "call.hangup";
      if (!data || (!amdEvent && !hangupEvent) || !payload) {
        res.writeHead(200); res.end("ignored"); return;
      }
      const call = decodeCallClientState(payload.client_state);
      if (hangupEvent) {
        const wake = call && call.kind === "wake" && call.wakeClaimToken ? call : null;
        if (!wake) { res.writeHead(200); res.end("ignored"); return; }
        const eventId = data.id;
        let receipt;
        if (typeof eventId !== "string" || eventId.trim() === "" || eventId.length > 512) {
          receipt = { ok: false, matched: 0, error: "missing_event_id" };
        } else {
          try {
            receipt = await recordTelnyxWakeReceipt({
              uid: wake.wakeUid,
              eventKey: wake.wakeEventKey,
              claimToken: wake.wakeClaimToken,
              callControlId: payload.call_control_id,
              callSessionId: payload.call_session_id ?? null,
              callLegId: payload.call_leg_id ?? null,
              webhookEventId: eventId,
              amdResult: null,
            }, { supaUrl: SUPA_URL, supaKey: SUPA_KEY });
          } catch {
            receipt = { ok: false, matched: 0, error: "network_error" };
          }
        }
        if (!receipt || receipt.ok !== true) {
          console.error("[telnyx-events] call.hangup receipt failed");
          res.writeHead(503, { "content-type": "text/plain" });
          res.end("receipt failed; send it again");
          return;
        }
        res.writeHead(200);
        res.end(receipt.matched === 1 ? "recorded" : "receipt unmatched");
        return;
      }
      // spec §3 row 2d: a /test-call detection arrives here too, and it is handled BEFORE the wake
      // path because it has no lm_wake_log row to write on — the code below would PATCH nothing and
      // report matched=0 forever. It still costs the same money on a voicemail, so it still hangs up.
      if (call && call.kind === "test") {
        const detection = await applyTestCallDetection({
          result: payload.result, callControlId: payload.call_control_id,
        });
        const tag = `test=${call.testUid.slice(0, 12)} result=${detection.result || "missing"}`;
        // Logged apart from the wake path's writes for the same reason the wake hangup is: this fails
        // against Telnyx, not Supabase, and it costs money rather than evidence. Silence would put us
        // back at "we are paying for two minutes of voicemail and nothing says we tried to stop it".
        if (detection.hangup && !detection.hangup.ok) {
          console.error(`[telnyx-events] test-call hangup FAILED (provider_error) ${tag} — still speaking to a machine`);
        } else if (detection.hangup) {
          console.log(`[telnyx-events] test-call hung up on a ${detection.result} ${tag}`);
        } else {
          console.log(`[telnyx-events] test-call ${tag}; left running`);
        }
        res.writeHead(200); res.end(detection.hangup ? "test hangup" : "test noop"); return;
      }
      const wake = call && call.kind === "wake" ? call : null;
      if (!wake) {
        // Not one of our calls, or a client_state we cannot decode. Either way nothing correlates, and
        // saying so out loud beats writing amd_result onto no row at all.
        console.log(`[telnyx-events] AMD result=${payload.result || "missing"}; no wake context`);
        res.writeHead(200); res.end("no wake context"); return;
      }
      // spec §3 row 2: persist EVERY detection, not only human ones. amd_result='machine' is a
      // voicemail we reached; amd_result IS NULL is a webhook that never arrived. Before this, both
      // were answered_at IS NULL and a rotated signing key would have gone unnoticed forever.
      // spec §3 row 2b / §5.2.1: the same event that tells us it is a machine also carries the handle
      // needed to end the call. `data.payload.call_control_id` is on every call.machine.detection.ended
      // (Telnyx sample webhook, team-telnyx/demo-node-telnyx voicemail-detection/contentful.md) — the
      // same identifier placeCall returns as `ccid` and the bridge already uses for record_start.
      const detection = await applyAmdDetection(wake.wakeUid, wake.wakeEventKey, {
        result: payload.result, supaUrl: SUPA_URL, supaKey: SUPA_KEY,
        callControlId: payload.call_control_id,
        callSessionId: payload.call_session_id ?? null,
        callLegId: payload.call_leg_id ?? null,
        webhookEventId: data.id,
        claimToken: wake.wakeClaimToken,
      });
      const tag = `wake=${wake.wakeUid.slice(0, 12)} result=${detection.result || "missing"}`;
      // The three outcomes get three different lines, and only one of them is routine. A write that
      // matched no row and a write that never landed are different failures and must not share a log.
      const report = (what, r) => {
        if (!r.ok) console.error(`[telnyx-events] ${what} PATCH FAILED (${r.error}) ${tag} — lm_wake_log NOT updated`);
        else if (r.matched === 0) console.error(`[telnyx-events] ${what} matched NO ROW ${tag} — wake row missing or already latched`);
        else console.log(`[telnyx-events] ${what} written rows=${r.matched} ${tag}`);
      };
      report("amd_result", detection.amd);
      if (detection.answered) report("answered_at", detection.answered);
      // The hangup is best-effort and is logged apart from the writes above, because it fails for a
      // different reason (Telnyx, not Supabase) and costs a different thing: money, never evidence.
      // Silence here would put us back where we started — paying for two minutes of voicemail with
      // nothing anywhere saying we tried to stop it.
      if (detection.hangup && !detection.hangup.ok) {
        console.error(`[telnyx-events] hangup FAILED (provider_error) ${tag} — still speaking to a machine`);
      } else if (detection.hangup) {
        console.log(`[telnyx-events] hung up on a ${detection.result} ${tag}`);
      }
      // spec §3 row 2a: Telnyx reads 2xx as "it arrived" and redelivers ONLY when it gets something
      // else (developers.telnyx.com/development/api-fundamentals/webhooks/receiving-webhooks: "All
      // response codes outside this range... will indicate to Telnyx that you did not receive the
      // webhook"; up to 3 primary + 3 failover attempts, exponential backoff). This route used to
      // answer 200 no matter what happened, so a Supabase outage silently threw away the last copy of
      // a detection: the retry was ours to take and we declined it, and the row stayed NULL forever —
      // indistinguishable from a webhook that never arrived, which is the §1.3 failure class.
      //
      // The line drawn here is ONLY "did the write land" vs "did it land and match nothing", because
      // that is the only distinction patchWakeLog gives us. Be honest about how coarse that is:
      // {ok:false} folds together the transient (5xx, thrown fetch) and the permanent — an http_400
      // from schema drift, an http_401 from a rotated service-role key, unreadable_response,
      // missing_args, and recordAmdResult's missing_result (an empty AMD result, which late-notice.js
      // argues at length is OUR parse failure and not a verdict). All of those now ask for a resend
      // and will fail identically on all six attempts. The price of that bluntness: a wasted delivery
      // budget, the failover URL rung for nothing, and a schema typo that looks exactly like an
      // outage. It is accepted for now because the alternative — 200 on a real outage — destroys the
      // only copy of a detection, and a wasted retry destroys nothing. The proper fix is to make
      // patchWakeLog say WHICH kind of failure it had (retryable vs permanent) and to escalate only
      // the first; do that there, not by widening this branch, or the two will drift.
      //   * !ok → 5xx, and Telnyx brings the same event back. Reprocessing is safe because the
      //     payload is identical apart from meta.attempt: amd_result is written with no filter (last
      //     observation wins) and answered_at is an is.null latch (the first human proof wins).
      //   * matched === 0 = the write LANDED and correctly changed nothing: there is no row for this
      //     uid+event_key, and no future delivery can conjure one. 200 closes it. Retrying would
      //     spend six deliveries on nothing and bury the real outages above.
      if (!detection.amd.ok) {
        res.writeHead(503, { "content-type": "text/plain" });
        res.end("record failed; send it again");
        return;
      }
      // A failed answered_at write deliberately does NOT ask for a retry — and NOT because a later
      // write would be a no-op. It would not be: the latch is answered_at=is.null, so if the first
      // write never landed the column is still NULL and a resent write lands perfectly well. The real
      // reasons are three: (1) this only runs after amd_result succeeded, so a retry rewrites that
      // good amd_result once per attempt to chase a second column; (2) the timestamp it would write
      // is the retry's clock, minutes off down the exponential backoff — a worse answer than none;
      // (3) amd_result='human' already records that a human picked up, so the fact is not lost, only
      // its exact second is. report() above puts the failure in stderr.
      const outcome = detection.answered
        ? (detection.answered.matched > 0 ? "answered" : "answered_at unchanged")
        : "recorded";
      res.writeHead(200); res.end(outcome);
    })().catch((error) => {
      console.error("[telnyx-events] error", error && error.message);
      if (!res.headersSent) { res.writeHead(500); res.end("error"); }
    });
    return;
  }
  // GET /gmail-connect — signed Telegram deep link into the existing real Unipile hosted-auth flow.
  // A provider/config failure is an explicit error; this route never claims Gmail was connected.
  if (path === "/gmail-connect") {
    if (req.method !== "GET") { res.writeHead(405); res.end("method"); return; }
    (async () => {
      const q = new URL(req.url, "http://x").searchParams;
      const uid = q.get("uid") || "";
      if (!verifyUid(uid, q.get("sig") || "")) { res.writeHead(403); res.end("bad uid signature"); return; }
      const user = await userForUid(uid);
      if (!await mailAvailable(user)) { res.writeHead(503); res.end("Gmail integration is currently being prepared"); return; }
      const redirect = await createHostedGmailLink(uid, {
        dsn: process.env.UNIPILE_DSN, token: process.env.UNIPILE_TOKEN,
        notifySecret: process.env.UNIPILE_NOTIFY_SECRET,
        publicBase: PUBLIC_BASE,
      });
      if (!redirect) { res.writeHead(503); res.end("Gmail connection is temporarily unavailable"); return; }
      res.writeHead(302, { Location: redirect }); res.end();
    })().catch((error) => { console.error("[gmail-onboard]", error.message); res.writeHead(503); res.end("Gmail connection is temporarily unavailable"); });
    return;
  }
  // POST /test-call {uid,sig} — the dashboard "Call me now" button. Auth'd by the same HMAC uid+sig
  // the /lm app already holds; we look up the user's phone and place an immediate Charon call so they
  // hear, right then, that the wake calls work. CORS-open for aniccaai.com (the static /lm page).
  if (path === "/test-call") {
    res.setHeader("Access-Control-Allow-Origin", "https://aniccaai.com");
    res.setHeader("Access-Control-Allow-Headers", "content-type");
    res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
    if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
    if (req.method !== "POST") { res.writeHead(405); res.end("method"); return; }
    (async () => {
      const reply = (code, obj) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(obj)); };
      try {
        const body = JSON.parse((await readBody(req)) || "{}");
        if (!verifyUid(body.uid, body.sig)) return reply(403, { error: "bad uid signature" });
        const u = await userForUid(body.uid);
        const phone = u && u.phone;
        if (!phone) return reply(400, { error: "no phone on file" });
        // Cost guard: enforce the one-time/cooldown SERVER-SIDE (client gate resets on reload). 429 = too soon.
        const rl = testCallAllowed(body.uid);
        if (!rl.ok) return reply(429, { error: "rate_limited", retryAfter: rl.retryAfter });
        // Call language = the user's CHOICE (lm_users.call_language, set via the /lm toggle) if present,
        // else fall back to the phone country (+81 → ja, else en). Dais 2026-06-22.
        const lang = resolveCallLang({ callLanguage: u.call_language, phone });
        // Caller may pass a REAL event (summary/location/urgency) so the call + its recording are
        // postable content — NEVER hardcode "test" (the assistant reads the summary aloud). Default = a
        // real morning nudge in the USER's language, not a "test" label.
        const ev = {
          summary: (body.summary || (lang === "ja" ? "次のご予定" : "your next appointment")).toString().slice(0, 200),
          startIso: body.dateTime || new Date(Date.now() + 15 * 60000).toISOString(),
          location: (body.location || "").toString().slice(0, 200),
        };
        const urgency = ["gentle", "firm", "harsh"].includes(body.urgency) ? body.urgency : "gentle";
        const streamUrl = buildStreamUrl(ev, urgency, lang, u.name);
        // spec §3 row 2d: say who this call is. The stream URL cannot carry it — its query is signed
        // by signCtx over a fixed array the /ws bridge re-verifies — so the state rides beside it. An
        // unnamed call is what made the detection webhook return "no wake context" and let every test
        // call that hit a voicemail run to the carrier's 120-second recording limit.
        const result = await placeCall({
          to: phone, streamUrl, clientState: encodeTestCallClientState({ testUid: body.uid }),
        });
        return reply(result.ok ? 200 : 502, result);
      } catch (e) {
        return reply(502, { error: String(e) });
      }
    })();
    return;
  }
  // POST /telegram — the Life Manager bot webhook. Telegram echoes our secret in a header; reject
  // anything that doesn't match (so strangers can't post fake updates). /start hands the user to the
  // web onboarding (deep-linked with their chat id); any other text is treated as a reply to a
  // pending location ask and routed to the calendar.
  if (path === "/telegram") {
    if (req.method !== "POST") { res.writeHead(405); res.end("method"); return; }
    // Fail CLOSED: no secret configured → reject. Constant-time compare to avoid timing leaks.
    const hdr = String(req.headers["x-telegram-bot-api-secret-token"] || "");
    const ok = LM_TG_SECRET.length > 0 && hdr.length === LM_TG_SECRET.length &&
      crypto.timingSafeEqual(Buffer.from(hdr), Buffer.from(LM_TG_SECRET));
    if (!ok) { res.writeHead(401); res.end("unauthorized"); return; }
    (async () => {
      try {
        const update = JSON.parse((await readBody(req)) || "{}");
        const u = parseUpdate(update);
        if (u && LM_TG_TOKEN) {
          if (u.kind === "callback") {
            if (String(u.data || "").startsWith("cfo:")) {
              let uid = null;
              try {
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                uid = row && typeof row.uid === "string" ? row.uid : null;
              } catch {}
              await handleCfoTelegramCallback({
                data: u.data, uid, chatId: u.chatId, actorId: u.userId,
                messageId: u.messageId, callbackQueryId: u.callbackQueryId, telegramToken: LM_TG_TOKEN,
              }, { supaUrl: SUPA_URL, supaKey: SUPA_KEY });
              res.writeHead(200); res.end("ok");
              return;
            }
            await answerCallbackQuery(LM_TG_TOKEN, u.callbackQueryId, "Received");
            await routeCallbackData(u.data, { ask: async (data) => {
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                return handleAskCallback(data, {
                  uid: row && row.uid, chatId: u.chatId, actorId: u.userId,
                  messageId: u.messageId, messageText: u.messageText, callbackQueryId: u.callbackQueryId,
                  telegramToken: LM_TG_TOKEN,
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY, composioKey: COMPOSIO_KEY,
                  gmailAccountId: row && row.gmail_account_id,
                });
              }, gmail: async (data) => {
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                return handleGmailCallback(data, row, {
                  token: LM_TG_TOKEN, chatId: u.chatId, base: PUBLIC_BASE,
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY,
                });
              }, discovery: async (data) => {
                // FIN-b: the payout branch needs the uid to know whether this person already told us
                // where to send money, so the register button can be answered exactly once.
                const row = /^discovery:register:payout$/.test(String(data || ""))
                  ? await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY)
                  : null;
                const outcome = await handleDiscoveryCallback(data, {
                  token: LM_TG_TOKEN, chatId: u.chatId, uid: row && row.uid,
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY,
                });
                // Discovery answers were otherwise invisible: nothing recorded which gate the user
                // responded to, so an unlocked-gate announcement could not be audited after the fact.
                if (outcome && outcome.handled) {
                  console.log(`[discovery] callback action=${outcome.action} gate=${outcome.gate}`);
                }
                return outcome;
              }, payout: async (data) => {
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                const outcome = await handlePayoutCallback(data, {
                  uid: row && row.uid, chatId: u.chatId, actorId: u.userId,
                  // CB-1 (§10.0-15): the handler edits the tapped message into its answered state
                  // and replies visibly on a re-tap, which needs the bot token and the original text.
                  token: LM_TG_TOKEN, messageId: u.messageId, messageText: u.messageText,
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY,
                });
                // Same audit shape as discovery: name the decision, never the person. A failed write
                // is logged as a failure so a silent non-persist can never look like a registration.
                if (outcome && outcome.handled) {
                  console.log(`[payout] callback answer=${outcome.answer} ok=${outcome.ok}${outcome.reason ? ` reason=${outcome.reason}` : ""}`);
                }
                return outcome;
              }, diet: async (data) => {
                // H2 ORG-diet: the lunch tap. The row is the tenant boundary — handleDietCallback
                // re-verifies that it names THIS chat before writing, so a lookup bug upstream
                // fails there instead of filing one person's lunch under another person's uid.
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                const outcome = await handleDietCallback(data, {
                  row, chatId: u.chatId, actorId: u.userId,
                  // CB-1 (§10.0-15 ①): the handler edits the question into its answered state, which
                  // needs the bot token and the original text. No thank-you follows — that edit IS
                  // the visible response, and the flow does not continue.
                  token: LM_TG_TOKEN, messageId: u.messageId, messageText: u.messageText,
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY,
                });
                // Name the decision, never the person and never the meal in a way that identifies
                // them: the answer value is one of four fixed tokens, which is already public shape.
                if (outcome && outcome.handled) {
                  console.log(`[diet] callback answer=${outcome.answer} ok=${outcome.ok}${outcome.reason ? ` reason=${outcome.reason}` : ""}`);
                }
                return outcome;
              }, precepts: async (data) => {
                // H4 ORG-precepts: the bedtime tap. Same tenant boundary as the diet sibling —
                // handlePreceptsCallback re-verifies that the row names THIS chat before writing,
                // and this is the ledger where a mis-filed row would attach one person's private
                // evening to another person's uid.
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                const outcome = await handlePreceptsCallback(data, {
                  row, chatId: u.chatId, actorId: u.userId,
                  // CB-1 (§10.0-15 ①): the handler edits the question into its answered state, which
                  // needs the bot token and the original text. No thank-you follows — that edit IS
                  // the visible response, and the flow does not continue.
                  token: LM_TG_TOKEN, messageId: u.messageId, messageText: u.messageText,
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY,
                });
                // Name the DECISION, never the person. The answer value is one of five fixed tokens,
                // which is already public shape; the label the user read never reaches the log.
                if (outcome && outcome.handled) {
                  console.log(`[precepts] callback answer=${outcome.answer} ok=${outcome.ok}${outcome.reason ? ` reason=${outcome.reason}` : ""}`);
                }
                return outcome;
              }, late: async (data) => {
                // The row selected by chat id is the tenant boundary.  The signed button authenticates
                // the draft/action; this lookup authenticates which uid may consume it.  A callback
                // forwarded into another chat therefore reaches the state machine with the wrong uid
                // and cannot decide or claim the original draft.
                const row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
                const store = createSupabaseLateApprovalStore({
                  supaUrl: SUPA_URL, supaKey: SUPA_KEY,
                });
                const outcome = await handleLateApprovalCallback(data, {
                  callbackSecret: LM_LATE_APPROVAL_CALLBACK_SECRET,
                  owner: row,
                  chatId: u.chatId,
                  actorId: u.userId,
                  callbackQueryId: u.callbackQueryId,
                  messageId: u.messageId,
                  messageText: u.messageText,
                  token: LM_TG_TOKEN,
                  store,
                  supaUrl: SUPA_URL,
                  supaKey: SUPA_KEY,
                  reflectAnswer,
                  sendMessage,
                  editMessageText,
                  resendKey: process.env.RESEND_API_KEY,
                });
                if (outcome && outcome.handled) {
                  console.log(`[late] callback decision=${outcome.decision || "none"} ok=${outcome.ok} sent=${outcome.sent === true} reason=${outcome.reason || "none"}`);
                }
                return outcome;
              } });
            res.writeHead(200); res.end("ok");
            return;
          }
          let row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
          if (u.kind === "message" && u.isStart && !isPanelDeepLink(u.text)) {
            const claim = await claimTelegramWebhookActor({
              actorId: u.userId,
              chatId: u.chatId,
              updateId: update.update_id,
              profileName: [u.firstName, u.lastName].filter(Boolean).join(" "),
            }, { supaUrl: SUPA_URL, supaKey: SUPA_KEY });
            if (claim.status === "replayed") {
              res.writeHead(200); res.end("ok");
              return;
            }
            if (claim.status !== "claimed" || String(claim.chat_id) !== String(u.chatId)) throw new Error("telegram actor claim failed");
            row = await rowByChatId(u.chatId, SUPA_URL, SUPA_KEY);
            if (!row || !row.uid) throw new Error("telegram actor unavailable");
            const result = await executeUserCommand({ uid: row.uid, chatId: u.chatId }, {
              type: "connection.start", provider: "calendar",
            }, {
              store: createSupabaseCommandStore({ supaUrl: SUPA_URL, supaKey: SUPA_KEY }),
              idempotencyKey: `telegram-start:${u.messageId || update.update_id}`,
              composioKey: COMPOSIO_KEY,
              composioAuthConfig: process.env.COMPOSIO_GCAL_AUTH_CONFIG,
              panelBaseUrl: LM_PANEL_BASE,
              calendarCallbackPath: "/telegram/oauth/calendar",
              calendarCallbackParams: { lang: u.languageCode },
              startCalendarConnection: (scope) => composioCalendarStart(scope, { composioKey: COMPOSIO_KEY }),
            });
            if (result && result.state && result.state.redirectUrl) {
              const reply = startReply({ calendarUrl: result.state.redirectUrl, languageCode: u.languageCode });
              const sent = await sendMessage(LM_TG_TOKEN, u.chatId, reply.text, reply.extra);
              if (!sent || sent.ok !== true) throw new Error("Telegram onboarding send failed");
            } else {
              const sent = await sendMessage(LM_TG_TOKEN, u.chatId, /^ja(?:-|$)/i.test(u.languageCode)
                ? "Google Calendarは接続済みです。自宅の住所を教えてください。"
                : "Google Calendar is connected. What is your home address?");
              if (!sent || sent.ok !== true) throw new Error("Telegram onboarding send failed");
            }
            res.writeHead(200); res.end("ok");
            return;
          }
          // FIN-d (13d-a): a pending wallet-address intake claims the typed message BEFORE feedback
          // can swallow it — an address must never become a feedback ticket. The module returns
          // handled:false for everything that is not its intake (no marker, bot commands, other
          // chats), so feedback, /panel, and the ask-location reply flow below stay untouched.
          if (u.kind === "message" && u.text) {
            const intake = await handleTypedPayoutAddress(u.text, row, {
              token: LM_TG_TOKEN, chatId: u.chatId, actorId: u.userId,
              supaUrl: SUPA_URL, supaKey: SUPA_KEY,
            });
            if (intake.handled) {
              // Audit names the decision, never the address (it is payout PII-adjacent — log outcomes only).
              console.log(`[payout] typed intake ok=${intake.ok}${intake.action ? ` action=${intake.action}` : ""}${intake.reason ? ` reason=${intake.reason}` : ""}`);
              res.writeHead(200); res.end("ok");
              return;
            }
          }
          const feedback = await handleFeedbackMessage(u, row, {
            token: LM_TG_TOKEN,
            provenanceKey: LM_FEEDBACK_PROVENANCE_KEY,
            supaUrl: SUPA_URL,
            supaKey: SUPA_KEY,
            send: sendMessage,
            ...(LM_FEEDBACK_STORE ? { persist: LM_FEEDBACK_STORE.persist } : {}),
          });
          if (feedback.handled) {
            res.writeHead(200); res.end("ok");
            return;
          }
          // Spec §12.1 row 4: the generic slash router. /connect is an ALIAS — it re-enters the same
          // parsed-control flow as the natural-language "connect calendar" instead of being handled
          // in the slash branch, so both spellings share one implementation (Gmail stays OFF).
          const slash = u.kind === "message" ? parseSlashCommand(u.text) : null;
          const slashAlias = slashAliasText(slash);
          const parsedControl = parseUserCommand(slashAlias || u.text);
          if (isPanelCommand(u.text) || isPanelDeepLink(u.text) || parsedControl.kind === "panel") {
            const deviceCode = panelDeviceCodeFromCommand(u.text);
            if (!row) {
              await sendMessage(LM_TG_TOKEN, u.chatId, "Complete Life Manager setup with /start before opening your panel.");
            } else if (!LM_PANEL_BASE) {
              console.error("[panel] LM_PANEL_BASE_URL/RAILWAY_PUBLIC_DOMAIN not configured");
              await sendMessage(LM_TG_TOKEN, u.chatId, "The panel is temporarily unavailable. Please try again shortly.");
            } else if (deviceCode) {
              const confirmed = await confirmPanelDeviceCode({
                uid: row.uid, chatId: u.chatId, actorId: u.userId, code: deviceCode,
              }, { supaUrl: SUPA_URL, supaKey: SUPA_KEY });
              await sendMessage(LM_TG_TOKEN, u.chatId, confirmed
                ? "Browser confirmed. Return to the same panel tab."
                : "That browser code is invalid or no longer available. Reload /panel for a new code.");
            } else {
              await sendPanelLink({ uid: row.uid, chatId: u.chatId }, {
                token: LM_TG_TOKEN,
                supaUrl: SUPA_URL,
                supaKey: SUPA_KEY,
                panelBaseUrl: LM_PANEL_BASE,
                sendMessage,
              });
            }
            res.writeHead(200); res.end("ok");
            return;
          }
          // Slash commands other than the /connect alias are handled HERE, before the location /
          // parsed-control / browser-task / onboarding branches: a /command must never be classified
          // as a browser task, captured as onboarding text, or answered with a setup nudge. /start
          // and /panel return handled:false (their existing branches above/below stay the owner),
          // and an unknown /command gets an honest "unknown command + /help" reply.
          if (slash && !slashAlias) {
            const outcome = await handleSlashCommand(slash, row, {
              token: LM_TG_TOKEN, chatId: u.chatId, base: PUBLIC_BASE,
              supaUrl: SUPA_URL, supaKey: SUPA_KEY,
              // Tenant-bound control state only. Broker credentials remain behind secret:// refs,
              // and this read path cannot submit an order or start a scheduler.
              getInvestmentState: (uid) => {
                return getInvestmentStateStore().read(uid);
              },
            });
            if (outcome.handled) {
              console.log(`[slash] command=${slash.name} action=${outcome.action}${outcome.ok === false ? ` reason=${outcome.reason || "failed"}` : ""}${outcome.providerMessageId == null ? "" : ` provider_message_id=${outcome.providerMessageId}`}`);
              res.writeHead(200); res.end("ok");
              return;
            }
          }
          // A punctuation-prefixed /start lookalike is not a Telegram deep-link payload. Keep it
          // out of the free-text onboarding path, which would otherwise emit the legacy ?tg= URL.
          // The slash router already owns alphanumeric unknown commands such as /startfoo.
          if (u.kind === "message" && u.text && !u.isStart && /^\/start(?:@[A-Za-z0-9_]+)?(?!\s|$)/i.test(u.text)) {
            await sendMessage(LM_TG_TOKEN, u.chatId, "Unknown command. Send /help to see what I understand.");
            res.writeHead(200); res.end("ok");
            return;
          }
          if (u.kind === "location") {
            if (row) {
              const saved = await upsertLiveLocation(row.uid, u, { supaUrl: SUPA_URL, supaKey: SUPA_KEY });
              if (!saved) console.error(`[telegram] live location save failed uid=${row.uid.slice(0, 12)}`);
            }
            res.writeHead(200); res.end("ok");
            return;
          }
          const gmailConnectUrl = ""; // Gmail connect is honestly OFF; sendStage auto-skips without rendering OAuth.
          const opts = {
            token: LM_TG_TOKEN, base: PUBLIC_BASE, supaUrl: SUPA_URL, supaKey: SUPA_KEY, gmailConnectUrl,
            composioKey: COMPOSIO_KEY, geminiKey: GEMINI_KEY,
          };
          if (u.text && (parsedControl.kind === "command" || parsedControl.kind === "unavailable")) {
            if (parsedControl.kind === "command" && !row) {
              await sendMessage(LM_TG_TOKEN, u.chatId, "Complete Life Manager setup with /start before changing settings.");
            } else {
              try {
                const dispatched = await dispatchParsedControl(parsedControl, {
                  executeCommand: executeUserCommand,
                  scope: row ? { uid: row.uid, chatId: u.chatId } : null,
                  commandDeps: row ? {
                    store: createSupabaseCommandStore({ supaUrl: SUPA_URL, supaKey: SUPA_KEY }),
                    idempotencyKey: `telegram:${u.messageId || crypto.randomUUID()}`,
                    composioKey: COMPOSIO_KEY,
                    composioAuthConfig: process.env.COMPOSIO_GCAL_AUTH_CONFIG,
                    panelBaseUrl: LM_PANEL_BASE,
                    startCalendarConnection: (scope) => composioCalendarStart(scope, { composioKey: COMPOSIO_KEY }),
                    disconnectCalendar: (scope) => composioCalendarDisconnect(scope, { composioKey: COMPOSIO_KEY }),
                  } : null,
                });
                const result = dispatched.result;
                if (result && result.state && result.state.redirectUrl) {
                  await sendMessage(LM_TG_TOKEN, u.chatId, "Calendar needs your Google permission.", { reply_markup: { inline_keyboard: [[{ text: "Connect Calendar", url: result.state.redirectUrl }]] } });
                } else {
                  await sendMessage(LM_TG_TOKEN, u.chatId, result ? `✅ ${dispatched.message}` : dispatched.message);
                }
              } catch {
                await sendMessage(LM_TG_TOKEN, u.chatId, "I couldn't apply that change. Your previous setting is unchanged.");
              }
            }
            res.writeHead(200); res.end("ok");
            return;
          }
          if (u.kind === "message" && u.text && process.env.LM_BROWSER_TASKS_ENABLED === "1") {
            const browserTask = await handleBrowserTaskMessage({
              text: u.text,
              chatId: u.chatId,
              messageId: u.messageId,
              updateId: update.update_id,
              user: row,
            }, {
              telegramToken: LM_TG_TOKEN,
              geminiKey: GEMINI_KEY,
              supaUrl: SUPA_URL,
              supaKey: SUPA_KEY,
            });
            if (browserTask.handled) {
              console.log(`[browser-task] queued=${browserTask.queued} job=${browserTask.jobId}`);
              res.writeHead(200); res.end("ok");
              return;
            }
          }
          if (u.text) {
            // Native steps (name/phone) capture the typed value; web steps re-nudge; "done" → reply.
            const result = await handleOnboardingText(u.chatId, u.text, row, opts);
            if (result === "done") {
              const res2 = await resolveTelegramReply(u.chatId, u.text);
              await sendMessage(LM_TG_TOKEN, u.chatId,
                res2.filled ? `✅ Got it — set “${res2.event}” to ${res2.location}.`
                            : "Thanks! If that was an event location, reply to my question and I'll add it.");
            }
          }
        }
      } catch (e) { console.error("[telegram] err", e.message); }
      res.writeHead(200); res.end("ok"); // always 200 fast so Telegram doesn't retry
    })();
    return;
  }
  // POST /inbound-email?s=<secret> — Resend Inbound webhook. A web user replied to our "where is X?" email
  // (To: reply+<token>@reply.aniccaai.com). Auth = the shared secret in the URL; we pull the token out of the
  // recipient, resolve it to (uid,event) via lm_ask_log, extract the location the user gave, and patch the
  // calendar + remember it. We never read the user's Gmail — this is OUR inbound domain. Always 200 (so the
  // provider doesn't retry); a bad/unknown token is a no-op.
  if (path === "/inbound-email") {
    if (req.method !== "POST") { res.writeHead(405); res.end("method"); return; }
    (async () => {
      try {
        const q = new URL(req.url, "http://x").searchParams;
        if (!LM_INBOUND_SECRET || q.get("s") !== LM_INBOUND_SECRET) { res.writeHead(403); res.end("forbidden"); return; }
        const body = JSON.parse((await readBody(req)) || "{}");
        const { token, text } = parseInboundRecipient(body); // pure, unit-tested across Resend payload shapes
        if (!isReplyToken(token)) { res.writeHead(200); res.end("no-token"); return; } // not one of ours → ignore
        const r = await handleInboundReply(token, text, { composioKey: COMPOSIO_KEY, geminiKey: GEMINI_KEY, supaUrl: SUPA_URL, supaKey: SUPA_KEY });
        console.log(`[inbound-email] token=${token.slice(0, 8)} ok=${r.ok} ${r.ok ? `${(r.uid || "").slice(0, 12)} → ${r.location}` : r.reason}`);
        res.writeHead(200); res.end(r.ok ? "patched" : "noop");
      } catch (e) { console.error("[inbound-email] err", e.message); res.writeHead(200); res.end("err"); }
    })();
    return;
  }
  // POST/GET /api/inngest — Inngest durable function endpoint (always mounted, independent of LIFE_RUN_LOOPS).
  // Inngest cloud calls this to register functions and dispatch events. In dev, INNGEST_DEV=1 syncs with
  // the local Inngest dev server. In prod, INNGEST_SIGNING_KEY authenticates incoming requests.
  // FAIL-CLOSED: in production (INNGEST_DEV not "1"), if INNGEST_SIGNING_KEY is missing we return 503
  // rather than serve unauthenticated — matching the fail-closed convention of /telegram and /ws.
  // In dev (INNGEST_DEV=1) we serve without a signing key so the local dev server can sync.
  // Both the in-process LIFE_RUN_LOOPS path and the Inngest path coexist; C-H1 (claimWake/claimTravel)
  // makes concurrent executions race-safe so running both simultaneously is harmless.
  if (path === "/api/inngest") {
    if (!inngestServeAllowed(process.env)) {
      res.writeHead(503, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "inngest signing key not configured", service: "life-call" }));
      return;
    }
    return inngestHandler(req, res);
  }
  // POST /api/stripe/webhook — Stripe billing lifecycle = source of truth for lm_users.paid (HARD-3).
  // Verify the signature over the RAW body (REQ-35), dedup by event.id (REQ-36), then apply entitlement.
  // FAIL-CLOSED in prod when STRIPE_WEBHOOK_SECRET is missing (REQ-41).
  if (path === "/api/stripe/webhook") {
    if (req.method !== "POST") { res.writeHead(405); res.end("method"); return; }
    if (!stripeWebhookAllowed(process.env)) {
      res.writeHead(503, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "stripe webhook secret not configured", service: "life-call" }));
      return;
    }
    (async () => {
      const raw = await readRawBody(req); // EXACT bytes (Buffer) for signature verification (FIND-005)
      let event;
      try {
        event = constructStripeWebhookEvent(raw, req.headers["stripe-signature"], process.env, stripe);
      } catch (e) {
        console.error("[stripe] bad signature", e.message);
        res.writeHead(400); res.end("invalid signature"); return; // REQ-35: reject, no billing side effect
      }
      const claimed = await claimEvent(event.id, event.type, SUPA_URL, SUPA_KEY); // REQ-36 idempotency
      if (!claimed) { res.writeHead(200); res.end("duplicate"); return; }         // duplicate delivery → ack, no re-apply
      try {
        const result = await applyBilling(event, { supaUrl: SUPA_URL, supaKey: SUPA_KEY, notify: dunningNotify });
        console.log("[stripe]", event.type, JSON.stringify(result));
        res.writeHead(200); res.end("ok");
      } catch (e) {
        console.error("[stripe] apply failed", e.message);
        // FIND-006: release the claim so Stripe's redelivery re-processes. If THIS also fails, the event
        // stays claimed → the transition would stick; log a RECONCILE marker (writes are idempotent SETs).
        const released = await unclaimEvent(event.id, SUPA_URL, SUPA_KEY);
        if (!released) console.error("[stripe] RECONCILE: unclaim failed for", event.id, "— manual replay needed");
        res.writeHead(500); res.end("apply failed");
      }
    })();
    return;
  }

  res.writeHead(404);
  res.end("not found");
});

const wss = new WebSocket.Server({ noServer: true });
const browserTakeoverWss = new WebSocket.Server({ noServer: true, maxPayload: 8 * 1024 * 1024 });

server.on("upgrade", (req, socket, head) => {
  const upgradePath = String(req.url || "").split("?", 1)[0];
  if (upgradePath === "/ws") {
    wss.handleUpgrade(req, socket, head, (client) => wss.emit("connection", client, req));
    return;
  }
  if (!upgradePath.startsWith(`${BROWSER_CAST_PATH}/`)) { socket.destroy(); return; }
  browserTakeoverTicket(req).then((state) => {
    if (state.status !== 200) { rejectUpgrade(socket, state.status); return; }
    browserTakeoverWss.handleUpgrade(req, socket, head, (client) => {
      const upstream = new WebSocket(steelCastUrl(state.handoff.session_id, state.viewer), { maxPayload: 8 * 1024 * 1024 });
      client.on("message", (data, binary) => { if (upstream.readyState === WebSocket.OPEN) upstream.send(data, { binary }); });
      upstream.on("message", (data, binary) => { if (client.readyState === WebSocket.OPEN) client.send(data, { binary }); });
      const close = () => {
        if (client.readyState === WebSocket.OPEN) client.close();
        if (upstream.readyState === WebSocket.OPEN) upstream.close();
      };
      client.on("close", close); client.on("error", close);
      upstream.on("close", close); upstream.on("error", close);
    });
  }).catch(() => rejectUpgrade(socket, 502));
});

wss.on("connection", (carrierWs, req) => {
  if (!GEMINI_KEY) {
    console.error("[bridge] GEMINI_API_KEY missing — closing call");
    try { carrierWs.close(); } catch {}
    return;
  }
  // Auth gate: reject unsigned/tampered upgrades BEFORE opening a Gemini socket (cost + injection).
  const ctx = ctxFromReq(req);
  if (!ctx) {
    console.error("[bridge] rejected unauthenticated /ws connection");
    try { carrierWs.close(1008, "unauthorized"); } catch {}
    return;
  }
  if (liveCalls >= MAX_CONCURRENT) {
    console.error(`[bridge] at capacity (${liveCalls}/${MAX_CONCURRENT}) — rejecting`);
    try { carrierWs.close(1013, "busy"); } catch {}
    return;
  }
  liveCalls++;
  const { event, urgency, lang, name, wakeUid, wakeEventKey } = ctx;
  console.log(`[bridge] carrier connected urgency=${urgency} live=${liveCalls}`);
  const state = { streamSid: null, inFrames: 0, outFrames: 0, setupComplete: false };

  // C1 (VCSDD life-manager-cost-connect-reliability): Gemini Live is the DEFAULT — every answered call
  // is a two-way Charon conversation from the first second (no one-way clip). `liveWsOpened` is the
  // measurable Goal-1 invariant (now ≥1 on EVERY answered call, the inverse of the old escalation-only
  // invariant).
  let gemini = null;
  let callStartedAtMs = null;
  let liveWsOpened = 0;
  let gotAudio = false;       // has Gemini emitted any audio yet on this call?
  let geminiReconnects = 0;   // one-retry guard for a pre-audio socket drop
  const carrierSend = (o) => { if (carrierWs.readyState === WebSocket.OPEN) carrierWs.send(JSON.stringify(o)); };
  const geminiSend = (o) => { if (gemini && gemini.readyState === WebSocket.OPEN) gemini.send(JSON.stringify(o)); };

  // Open the Gemini Live bridge (billed, ~$0.023/min). Called on the Telnyx `start` frame (call
  // answered) — this IS the default path now, not an escalation. If the socket drops before any audio
  // was heard, retry ONCE; a second pre-audio failure ends the call cleanly (never silence, never a
  // clip fallback).
  function openGeminiLive() {
    if (gemini) return;
    liveWsOpened++;
    console.log(`[bridge] opening Gemini Live live_ws_opened=${liveWsOpened}`);
    gemini = new WebSocket(geminiLiveWsUrl(GEMINI_KEY));
    const geminiStartedAtMs = Date.now();
    let geminiCostRecorded = false;
    gemini.on("open", () => geminiSend(geminiSetupForEvent(event, urgency, lang, name)));
    gemini.on("message", (data) => {
      let msg;
      try { msg = JSON.parse(data.toString()); } catch { return; }
      const r = routeGeminiMessage(msg, state, carrierSend, buildTelnyxMediaFrame);
      if (r.kind === "setupComplete") geminiSend(buildGeminiTurn(openingTurnForLang(lang)));
      if (r.kind === "audio") gotAudio = true;
      // Barge-in: the caller spoke over Charon (Gemini server-VAD). Flush Telnyx's queued playback so
      // the caller is heard immediately instead of talked over.
      const carrierAction = carrierActionForGeminiKind(r.kind);
      if (carrierAction) carrierSend(carrierAction); // barge-in: flush Telnyx queued playback
      if (DEBUG_TRANSCRIPTS) {
        const t = parseGeminiTranscripts(msg);
        if (t.input) console.error(`[transcript] USER: ${t.input}`);
        if (t.output) console.error(`[transcript] CHARON: ${t.output}`);
      }
    });
    // ws fires `error` THEN `close` for a SINGLE failure — the factory's `ended` flag collapses the pair
    // (else the paired close would hang up the call right after the reconnect socket opened). One retry
    // only, for a pre-audio transient failure; otherwise end the call cleanly (never silence, never a clip).
    const onGeminiEnd = makeGeminiEndHandler({
      getGotAudio: () => gotAudio,
      getReconnects: () => geminiReconnects,
      incReconnects: () => { geminiReconnects++; },
      carrierOpen: () => carrierWs.readyState === WebSocket.OPEN,
      onReconnect: () => { gemini = null; openGeminiLive(); },
      onClose: () => { try { carrierWs.close(); } catch {} },
      log: (reason) => console.log(`[bridge] gemini ${reason} gotAudio=${gotAudio} reconnects=${geminiReconnects}`),
    });
    gemini.on("error", (e) => onGeminiEnd(`err ${e.message}`));
    gemini.on("close", () => {
      if (!geminiCostRecorded) {
        geminiCostRecorded = true;
        const quantity = Math.max(0, (Date.now() - geminiStartedAtMs) / 1000);
        // Duration proxy from spec §13's measured ~$0.023/min. Google bills Live API by actual
        // token usage, not wall time (https://ai.google.dev/gemini-api/docs/live-api/best-practices#pricing-billing),
        // but this bridge does not receive billable token totals, so the ledger stores this explicit estimate.
        // Keep the legacy kind for existing panels, but put estimated cost on the normalized event
        // only so aggregate cost is not counted twice.
        recordCost({ uid: wakeUid || null, kind: "gemini_live", quantity, unit: "seconds",
          estUsd: 0, meta: { reconnect: geminiReconnects, cost_source: "provider_usage" } });
        recordUsageEvent({ tenantId: wakeUid || "unknown", provider: "gemini",
          feature: "live_api", outcome: gotAudio ? "success" : "failure",
          failureClass: gotAudio ? null : "no_audio", providerUnits: quantity,
          providerUnit: "seconds_proxy", estimatedCostUsd: quantity / 60 * 0.023,
          meta: { reconnects: geminiReconnects, estimate_basis: "audio_duration_proxy" } });
      }
      onGeminiEnd("closed");
    });
  }

  carrierWs.on("message", (data) => {
    let msg;
    try { msg = JSON.parse(data.toString()); } catch { return; }
    const kind = routeTelnyxMessage(msg, state, geminiSend);
    if (kind === "start") {
      if (callStartedAtMs == null) callStartedAtMs = Date.now();
      // Recording still begins on media start. answered_at does not: with AMD enabled, only the
      // signed call.machine.detection.ended human webhook may mark it. LM_AMD=off preserves the old
      // media-start approximation as an explicit operational fallback.
      if (wakeUid && wakeEventKey && shouldMarkAnswered({
        amdEnabled: amdEnabled(process.env), signal: "media-start",
      })) markAnswered(wakeUid, wakeEventKey, {
        supaUrl: SUPA_URL, supaKey: SUPA_KEY,
      }).then((r) => {
        // Same PATCH as always; only the reporting is new. This return value used to be discarded
        // entirely, so an LM_AMD=off deployment writing to nothing left no trace (spec §1.3).
        if (!r.ok) console.error(`[bridge] answered_at PATCH FAILED (${r.error}) wake=${String(wakeUid).slice(0, 12)}`);
        else if (r.matched === 0) console.error(`[bridge] answered_at matched NO ROW wake=${String(wakeUid).slice(0, 12)}`);
      }).catch((e) => console.error(`[bridge] answered_at update failed: ${e && e.message}`));
      if (state.callControlId && !state.recordStarted) {
        state.recordStarted = true;
        startRecording(state.callControlId).then((r) => {
          if (r.ok) console.log(`[bridge] recording started ccid=${state.callControlId}`);
          else console.error(`[bridge] record_start FAILED: ${r.error}`);
        });
      }
      if (!gemini) openGeminiLive(); // DEFAULT: two-way Gemini Live from second 1
    }
    if (kind === "dtmf") console.log("[bridge] DTMF ignored (Gemini Live already open)");
    if (kind === "stop" && gemini) { try { gemini.close(); } catch {} }
  });
  let released = false;
  const release = () => { if (!released) { released = true; liveCalls = Math.max(0, liveCalls - 1); } };
  carrierWs.on("close", () => {
    release();
    console.log(`[bridge] carrier closed in=${state.inFrames} out=${state.outFrames} live_ws_opened=${liveWsOpened} live=${liveCalls}`);
    if (callStartedAtMs != null) {
      const quantity = Math.max(0, (Date.now() - callStartedAtMs) / 1000);
      recordCost({ uid: wakeUid || null, kind: "telnyx_call", quantity, unit: "seconds",
        estUsd: quantity / 60 * 0.002, meta: { stream_id: state.streamSid || null } });
    }
    if (gemini) { try { gemini.close(); } catch {} }
  });
  carrierWs.on("error", release);
});

// Only bind to the port when this file is run directly (not when required by tests).
// This allows test files to import inngestServeAllowed without starting the HTTP server.
if (require.main === module) {
  server.listen(PORT, () => {
    console.log(`[life-call] listening ${PORT} ws=/ws build=${BUILD_TAG}`);
    // A comp window silently changes who gets past the paywall and who the scheduler picks up, so it
    // announces itself once at boot — an operator must never have to guess whether it is on.
    const compBanner = compBootLog(process.env);
    if (compBanner) console.log(compBanner);
    // SINGLE-WRITER (B3): run the scheduler loops in-process ONLY when LIFE_RUN_LOOPS!=="false".
    // The /ws Telnyx⇄Gemini-Live voice bridge + /test-call + /telegram endpoints are ALWAYS on regardless.
    // As an OpenClaw voice daemon, set LIFE_RUN_LOOPS=false so the cron-COMMAND jobs (B2) own the loops.
    const loops = maybeStartLoops(process.env, {
      startScheduler, startWakeLoop, startReminderLoop, startTravelLoop, startAskLoop, startOnboardLoop, startDiscoveryLoop,
      startInvestmentDryRunLoop,
    });
    console.log(`[life-call] ${loops.started ? "loops ON (standalone)" : "VOICE DAEMON (loops OFF)"} — ${loops.reason}`);
    const browserJobs = startBrowserJobLoop({
      enabled: process.env.LM_BROWSER_TASKS_ENABLED === "1",
      opportunityStore: process.env.LM_BROWSER_TASKS_ENABLED === "1" ? getMoneyPrinterRuntimeStore() : null,
    });
    console.log(`[life-call] browser jobs ${browserJobs.enabled ? "ON (Railway private Steel)" : "OFF"}`);
    // INC-3: register our own webhook from our own env — registration and comparison are one value.
    selfHealWebhook(process.env).then((r) => {
      console.log(`[life-call] webhook self-heal: healed=${r.healed} ${r.reason}`);
    }).catch((e) => console.error(`[life-call] webhook self-heal error ${e && e.message}`));
  });
}

// redeploy trigger 010026

// Export pure helpers for unit tests (FIND-005).
module.exports = { browserCastPublicUrl, buildTag, createBrowserCastTicket, inngestServeAllowed, panelApiOptions, panelOriginForPath, server, steelCastUrl, testCallAllowed, verifyBrowserCastTicket, TEST_CALL_COOLDOWN_MS, TEST_CALL_DAILY_MAX };
