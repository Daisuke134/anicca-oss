// LM-33a: Telegram /panel one-time token and browser session helpers.
"use strict";

const crypto = require("crypto");
const { renderPanelPage, renderPanelOnboardingPage } = require("./panel-ui.js");

const PANEL_SESSION_IDLE_MS = 30 * 24 * 60 * 60 * 1000;
const PANEL_TELEGRAM_INIT_MAX_AGE_MS = 5 * 60 * 1000;
const PANEL_TELEGRAM_INIT_FUTURE_SKEW_MS = 30 * 1000;
const PANEL_DEVICE_TTL_MS = 10 * 60 * 1000;
const PANEL_COOKIE = "__Host-lm_panel_session";
const PANEL_CHALLENGE_COOKIE = "__Host-lm_panel_challenge";
const OPAQUE_TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;
const DEVICE_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
const DEVICE_CODE_RE = /^[2-9A-HJ-NP-Z]{8}$/;
const MONEY_PRINTER_GUEST_PREFIX = "webmcp-guest-";
const MONEY_PRINTER_GUEST_NAME = "WebMCP Judge Guest";
const MONEY_PRINTER_GUEST_CSP = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'";

function sha256(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

function supabaseHeaders(supaKey, prefer) {
  return {
    apikey: supaKey,
    Authorization: `Bearer ${supaKey}`,
    "Content-Type": "application/json",
    ...(prefer ? { Prefer: prefer } : {}),
  };
}

async function sendPanelLink({ uid, chatId }, opts = {}) {
  void uid;
  let base;
  try { base = new URL(String(opts.panelBaseUrl || "")); }
  catch { throw new Error("panel base URL is unavailable"); }
  if (base.protocol !== "https:" || base.username || base.password) throw new Error("panel base URL is unavailable");
  const result = { url: `${base.origin}/panel` };
  const sender = opts.sendMessage || require("./telegram.js").sendMessage;
  const sent = await sender(
    opts.token,
    chatId,
    "Open your personal Life Manager panel.",
    { reply_markup: { inline_keyboard: [[{ text: "Open dashboard", web_app: { url: result.url } }]] } },
  );
  if (!sent || sent.ok !== true) throw new Error("panel web app button send failed");
  return result;
}

async function createPanelSession({ uid, chatId }, opts = {}) {
  const randomBytes = opts.randomBytes || crypto.randomBytes;
  const now = opts.now ? opts.now() : new Date();
  const session = randomBytes(32).toString("base64url");
  const response = await (opts.fetchImpl || fetch)(`${opts.supaUrl}/rest/v1/lm_panel_sessions`, {
    method: "POST",
    headers: supabaseHeaders(opts.supaKey, "return=minimal"),
    body: JSON.stringify({
      session_hash: sha256(session),
      uid,
      chat_id: String(chatId),
      expires_at: new Date(now.getTime() + PANEL_SESSION_IDLE_MS).toISOString(),
      idle_expires_at: new Date(now.getTime() + PANEL_SESSION_IDLE_MS).toISOString(),
      absolute_expires_at: null,
    }),
  });
  if (!response.ok) throw new Error(`panel session insert failed (${response.status})`);
  return session;
}

function panelSessionCookie(value, maxAge = PANEL_SESSION_IDLE_MS / 1000) {
  return `${PANEL_COOKIE}=${value}; Max-Age=${maxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

function clearPanelCookies() {
  return [`${PANEL_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`, "lm_panel_session=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"];
}

async function panelRpc(name, body, opts) {
  const response = await (opts.fetchImpl || fetch)(`${String(opts.supaUrl).replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
    method: "POST", headers: supabaseHeaders(opts.supaKey), body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`panel session rpc failed (${response.status})`);
  return response.json().catch(() => null);
}

async function resolvePanelSession(session, opts = {}) {
  if (!OPAQUE_TOKEN_RE.test(String(session || ""))) return null;
  const randomBytes = opts.randomBytes || crypto.randomBytes;
  const candidateSeed = sha256(randomBytes(32).toString("base64url"));
  const rotationSecret = String(opts.sessionRotationSecret || process.env.LM_PANEL_SESSION_ROTATION_SECRET || opts.supaKey || "");
  if (!rotationSecret) return null;
  const deriveChild = (seed) => crypto.createHmac("sha256", rotationSecret).update(`lm-panel-session:v1:${session}:${seed}`).digest("base64url");
  const child = deriveChild(candidateSeed);
  const rows = await panelRpc("resolve_lm_panel_session", { p_session_hash: sha256(session), p_child_hash: sha256(child), p_child_seed: candidateSeed }, opts);
  const row = Array.isArray(rows) ? rows[0] : rows;
  if (!row || !row.uid || !row.chat_id) return null;
  let replacement = null;
  if (row.rotated) {
    if (row.accepted_child_seed) {
      replacement = deriveChild(String(row.accepted_child_seed));
      if (row.accepted_child_hash && sha256(replacement) !== row.accepted_child_hash) return null;
    } else {
      replacement = child;
    }
  }
  const csrf = row.family_id ? sha256(`${row.family_id}:panel-family-csrf`) : csrfToken(replacement || session);
  const scope = { uid: String(row.uid), chatId: String(row.chat_id), replacement, csrf };
  const cookieMaxAge = Number(row.cookie_max_age);
  if (Number.isInteger(cookieMaxAge) && cookieMaxAge > 0) {
    scope.cookieValue = replacement || session;
    scope.cookieMaxAge = cookieMaxAge;
  }
  return scope;
}

function panelScopeCookie(scope) {
  if (!scope) return "";
  if (scope.cookieValue && scope.cookieMaxAge) return panelSessionCookie(scope.cookieValue, scope.cookieMaxAge);
  return scope.replacement ? panelSessionCookie(scope.replacement) : "";
}

async function revokePanelSession(session, opts = {}) {
  if (!OPAQUE_TOKEN_RE.test(String(session || ""))) return false;
  return Boolean(await panelRpc("revoke_lm_panel_session", { p_session_hash: sha256(session) }, opts));
}

async function revokePanelSessionsForTenant(scope, opts = {}) {
  return Boolean(await panelRpc("revoke_lm_panel_sessions_for_tenant", { p_uid: String(scope.uid), p_chat_id: String(scope.chatId) }, opts));
}

function cookieValue(header, name) {
  for (const part of String(header || "").split(";")) {
    const index = part.indexOf("=");
    if (index === -1 || part.slice(0, index).trim() !== name) continue;
    return part.slice(index + 1).trim();
  }
  return "";
}

async function sessionScope(session, opts = {}) {
  return resolvePanelSession(session, opts);
}

async function sessionUid(session, opts = {}) {
  const scope = await sessionScope(session, opts);
  return scope && scope.uid;
}

function csrfToken(session) {
  return OPAQUE_TOKEN_RE.test(String(session || "")) ? sha256(`${session}:panel-csrf`) : "";
}

function moneyPrinterGuestHeaders(extra = {}) {
  return {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "origin-agent-cluster": "?1",
    "permissions-policy": "tools=(self)",
    "content-security-policy": MONEY_PRINTER_GUEST_CSP,
    ...extra,
  };
}

function moneyPrinterGuestIdentity(opts = {}) {
  const uid = opts.guestUid
    ? String(opts.guestUid)
    : `${MONEY_PRINTER_GUEST_PREFIX}${(opts.randomBytes || crypto.randomBytes)(12).toString("hex")}`;
  const chatId = String(opts.guestChatId || uid);
  if (!uid || !chatId) throw new Error("money printer guest identity unavailable");
  return { uid, chatId };
}

async function upsertMoneyPrinterGuest(identity, opts = {}) {
  const base = String(opts.supaUrl || "").replace(/\/$/, "");
  if (!base || !opts.supaKey) throw new Error("money printer guest storage unavailable");
  const response = await (opts.fetchImpl || fetch)(`${base}/rest/v1/lm_users?on_conflict=uid`, {
    method: "POST",
    headers: supabaseHeaders(opts.supaKey, "resolution=merge-duplicates,return=minimal"),
    body: JSON.stringify({
      uid: identity.uid,
      name: MONEY_PRINTER_GUEST_NAME,
      telegram_chat_id: identity.chatId,
      paid: false,
    }),
  });
  if (!response.ok) throw new Error(`money printer guest upsert failed (${response.status})`);
}

async function handleMoneyPrinterGuestRequest(req, res, opts = {}) {
  const requestUrl = new URL(req.url || "/money-printer", "http://panel.local");
  if (req.method !== "GET") {
    res.writeHead(405, moneyPrinterGuestHeaders({ Allow: "GET" }));
    res.end("method not allowed");
    return;
  }
  if (requestUrl.pathname !== "/money-printer") {
    res.writeHead(404, moneyPrinterGuestHeaders());
    res.end("not found");
    return;
  }
  if (requestUrl.search) {
    res.writeHead(400, moneyPrinterGuestHeaders());
    res.end("query not allowed");
    return;
  }

  const current = cookieValue(req.headers.cookie, PANEL_COOKIE) || cookieValue(req.headers.cookie, "lm_panel_session");
  const scope = current ? await sessionScope(current, opts) : null;
  const configuredUid = opts.guestUid ? String(opts.guestUid) : null;
  const reusable = scope && scope.chatId === scope.uid
    && (configuredUid ? scope.uid === configuredUid : scope.uid.startsWith(MONEY_PRINTER_GUEST_PREFIX));
  if (reusable) {
    res.writeHead(200, moneyPrinterGuestHeaders(panelScopeCookie(scope) ? { "Set-Cookie": panelScopeCookie(scope) } : {}));
    res.end(renderPanelPage({ csrf: scope.csrf || csrfToken(current), guest: true }));
    return;
  }

  const identity = moneyPrinterGuestIdentity(opts);
  await upsertMoneyPrinterGuest(identity, opts);
  const session = await createPanelSession({ uid: identity.uid, chatId: identity.chatId }, opts);
  const createdScope = await sessionScope(session, opts);
  if (!createdScope || createdScope.uid !== identity.uid || createdScope.chatId !== identity.chatId) {
    throw new Error("money printer guest session unavailable");
  }
  res.writeHead(200, moneyPrinterGuestHeaders({ "Set-Cookie": panelScopeCookie(createdScope) || panelSessionCookie(session) }));
  res.end(renderPanelPage({ csrf: createdScope.csrf || csrfToken(session), guest: true }));
}

function jsonResponse(res, status, body, headers = {}) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    ...headers,
  });
  res.end(JSON.stringify(body));
}

function readJsonBody(req, maxBytes = 16 * 1024) {
  return new Promise((resolve) => {
    const chunks = [];
    let length = 0;
    let rejected = false;
    req.on("data", (chunk) => {
      if (rejected) return;
      length += chunk.length;
      if (length > maxBytes) {
        rejected = true;
        resolve(null);
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (rejected) return;
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}")); }
      catch { resolve(null); }
    });
    req.on("error", () => resolve(null));
  });
}

function telegramProfileName(user) {
  const parts = [user && user.first_name, user && user.last_name]
    .filter((part) => typeof part === "string")
    .map((part) => part.trim())
    .filter(Boolean);
  return parts.join(" ").slice(0, 120);
}

function verifyTelegramInitData(rawInitData, opts = {}) {
  const raw = String(rawInitData || "");
  const botToken = String(opts.token || "");
  if (!raw || raw.length > 8192 || !botToken) return { ok: false, reason: "invalid" };

  let params;
  try { params = new URLSearchParams(raw); }
  catch { return { ok: false, reason: "invalid" }; }
  const counts = new Map();
  for (const [key] of params) counts.set(key, (counts.get(key) || 0) + 1);
  if ([...counts.values()].some((count) => count !== 1)) return { ok: false, reason: "invalid" };

  const receivedHex = params.get("hash") || "";
  if (!/^[a-f0-9]{64}$/i.test(receivedHex)) return { ok: false, reason: "invalid" };
  const dataCheckString = [...params.entries()]
    .filter(([key]) => key !== "hash")
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
  const secret = crypto.createHmac("sha256", "WebAppData").update(botToken).digest();
  const expected = crypto.createHmac("sha256", secret).update(dataCheckString).digest();
  const received = Buffer.from(receivedHex, "hex");
  if (received.length !== expected.length || !crypto.timingSafeEqual(received, expected)) {
    return { ok: false, reason: "invalid" };
  }

  const authDateText = params.get("auth_date") || "";
  if (!/^\d{10}$/.test(authDateText)) return { ok: false, reason: "invalid" };
  const authDateMs = Number(authDateText) * 1000;
  const nowMs = opts.now ? opts.now().getTime() : Date.now();
  const maxAgeMs = Number(opts.telegramInitDataMaxAgeMs) || PANEL_TELEGRAM_INIT_MAX_AGE_MS;
  if (authDateMs < nowMs - maxAgeMs || authDateMs > nowMs + PANEL_TELEGRAM_INIT_FUTURE_SKEW_MS) {
    return { ok: false, reason: "stale" };
  }

  let user;
  try { user = JSON.parse(params.get("user") || "null"); }
  catch { return { ok: false, reason: "invalid" }; }
  if (!user || typeof user.id !== "number" || !Number.isSafeInteger(user.id) || user.id <= 0) {
    return { ok: false, reason: "invalid" };
  }
  const actorId = String(user.id);
  const chatText = params.get("chat");
  if (chatText) {
    let chat;
    try { chat = JSON.parse(chatText); }
    catch { return { ok: false, reason: "invalid" }; }
    if (!chat || String(chat.id || "") !== actorId || (chat.type && chat.type !== "private")) {
      return { ok: false, reason: "invalid" };
    }
  }
  return { ok: true, actorId, profileName: telegramProfileName(user), initHash: sha256(`lm-panel-telegram-init:v1:${receivedHex.toLowerCase()}`) };
}

async function claimTelegramInit(verified, opts) {
  const rows = await panelRpc("claim_lm_panel_telegram_init_v2", {
    p_init_hash: verified.initHash,
    p_actor_id: verified.actorId,
    p_profile_name: verified.profileName || "",
  }, opts);
  const row = Array.isArray(rows) ? rows[0] : rows;
  return row || { status: "unknown_actor" };
}

async function claimTelegramWebhookActor(input = {}, opts = {}) {
  const actorId = String(input.actorId || "");
  const chatId = String(input.chatId || "");
  const updateId = String(input.updateId || "");
  if (!/^[1-9][0-9]{0,19}$/.test(actorId) || chatId !== actorId || !/^[1-9][0-9]{0,19}$/.test(updateId)) {
    throw new Error("telegram actor unavailable");
  }
  const profileName = String(input.profileName || "").trim().slice(0, 120);
  return claimTelegramInit({
    actorId,
    profileName,
    initHash: sha256(`lm-telegram-webhook-start:v1:${actorId}:${updateId}`),
  }, opts);
}

function challengeCookie(value, maxAge = PANEL_DEVICE_TTL_MS / 1000) {
  return `${PANEL_CHALLENGE_COOKIE}=${value}; Max-Age=${maxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

function clearChallengeCookie() {
  return `${PANEL_CHALLENGE_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

async function createPanelDeviceChallenge(opts = {}) {
  const randomBytes = opts.randomBytes || crypto.randomBytes;
  const now = opts.now ? opts.now() : new Date();
  const challenge = randomBytes(32).subarray(0, 32).toString("base64url");
  const codeBytes = randomBytes(5).subarray(0, 5);
  let codeValue = 0n;
  for (const byte of codeBytes) codeValue = (codeValue << 8n) | BigInt(byte);
  let code = "";
  for (let index = 0; index < 8; index++) {
    code = DEVICE_CODE_ALPHABET[Number(codeValue & 31n)] + code;
    codeValue >>= 5n;
  }
  const row = {
    challenge_hash: sha256(challenge),
    code_hash: sha256(code),
    expires_at: new Date(now.getTime() + PANEL_DEVICE_TTL_MS).toISOString(),
  };
  const result = await (opts.fetchImpl || fetch)(`${opts.supaUrl}/rest/v1/lm_panel_device_challenges`, {
    method: "POST",
    headers: supabaseHeaders(opts.supaKey, "return=minimal"),
    body: JSON.stringify(row),
  });
  if (!result.ok) throw new Error(`panel challenge insert failed (${result.status})`);
  return { challenge, code };
}

function panelDeviceCodeFromCommand(text) {
  const match = /^\/panel(?:@[A-Za-z0-9_]+)?\s+([2-9A-HJ-NP-Za-hj-np-z]{8})$/i.exec(String(text || "").trim());
  const code = match ? match[1].toUpperCase() : "";
  return DEVICE_CODE_RE.test(code) ? code : "";
}

async function confirmPanelDeviceCode({ uid, chatId, actorId, code }, opts = {}) {
  const normalized = String(code || "").trim().toUpperCase();
  if (!uid || !chatId || String(actorId || "") !== String(chatId) || !DEVICE_CODE_RE.test(normalized)) return false;
  const rows = await panelRpc("claim_lm_panel_device_code", {
    p_code_hash: sha256(normalized),
    p_uid: String(uid),
    p_chat_id: String(chatId),
  }, opts);
  const row = Array.isArray(rows) ? rows[0] : rows;
  return Boolean(row && row.status === "claimed" && String(row.uid) === String(uid) && String(row.chat_id) === String(chatId));
}

function panelReturnPath(value) {
  return value === "/panel/onboarding" ? "/panel/onboarding" : "/panel";
}

function renderPanelLogin({ code, returnTo = "/panel" }) {
  const safeCode = DEVICE_CODE_RE.test(String(code || "")) ? code : "--------";
  const destination = panelReturnPath(returnTo);
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><meta name="color-scheme" content="light"><title>Life Manager sign in</title><style>:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3efe5;color:#122238;font-family:Avenir,"Hiragino Sans",sans-serif}.card{width:min(38rem,calc(100% - 32px));border:1px solid #9d9484;background:#fbf8f0;padding:clamp(24px,6vw,44px);box-shadow:0 24px 70px rgba(38,35,30,.12)}h1{margin:0 0 18px;font-family:"Iowan Old Style",serif;font-size:clamp(2rem,8vw,3.5rem);font-weight:500}.method{padding:20px 0;border-top:1px solid #cfc7b8}.code{display:inline-block;margin:8px 0;padding:10px 14px;border:1px solid #122238;background:white;font:700 1.4rem/1.1 ui-monospace,monospace;letter-spacing:.12em}.status{min-height:1.4em;color:#536070}</style><script src="https://telegram.org/js/telegram-web-app.js?59"></script></head><body><main class="card" data-panel-login><p>Life Manager</p><h1>Your panel, at one permanent address.</h1><section class="method"><h2>Open this panel inside Telegram</h2><p>Use the <b>Open dashboard</b> button from your own Life Manager bot chat. Telegram verifies your identity and this page opens your personal panel.</p></section><section class="method"><h2>Or confirm this browser</h2><p>Send <b>/panel ${safeCode}</b> in your own bot chat. Keep this browser tab open.</p><output class="code" data-device-code="${safeCode}">${safeCode}</output><p class="status" data-login-status>Waiting for confirmation…</p></section></main><script>(()=>{const status=document.querySelector("[data-login-status]");const post=async(path,body)=>{const response=await fetch(path,{method:"POST",credentials:"same-origin",headers:body?{"content-type":"application/json"}:undefined,body:body?JSON.stringify(body):undefined});if(response.ok){const data=await response.json();if(data.redirect==="${destination}")location.replace(data.redirect);}return response;};const initData=window.Telegram&&window.Telegram.WebApp&&window.Telegram.WebApp.initData;if(initData){status.textContent="Verifying Telegram…";post("/api/panel/session/telegram",{initData,returnTo:"${destination}"}).catch(()=>{status.textContent="Telegram verification failed. Reopen the bot button.";});}else{const poll=setInterval(()=>{post("/api/panel/session/device",{returnTo:"${destination}"}).then((response)=>{if(response.status===409||response.status===410){clearInterval(poll);status.textContent="This code is no longer available. Reload for a new code.";}}).catch(()=>{});},1500);}})();</script></body></html>`;
}

async function handleTelegramSession(req, res, opts) {
  const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
  if (req.method !== "POST") return jsonResponse(res, 405, { error: "method_not_allowed" }, { Allow: "POST" });
  if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) return jsonResponse(res, 403, { error: "origin_rejected" });
  if (!/^application\/json(?:;|$)/i.test(String(req.headers["content-type"] || ""))) return jsonResponse(res, 415, { error: "json_required" });
  const body = await readJsonBody(req);
  if (!body || typeof body.initData !== "string" || !body.initData) return jsonResponse(res, 400, { error: "init_data_required" });
  const returnTo = panelReturnPath(body.returnTo);
  const verified = verifyTelegramInitData(body.initData, opts);
  if (!verified.ok) return jsonResponse(res, 401, { error: "telegram_auth_rejected" });
  const claim = await claimTelegramInit(verified, opts);
  if (claim.status === "replayed") return jsonResponse(res, 409, { error: "telegram_auth_replayed" });
  if (claim.status !== "claimed" || !claim.uid || String(claim.chat_id) !== verified.actorId) {
    return jsonResponse(res, 403, { error: "telegram_actor_unbound" });
  }

  const current = cookieValue(req.headers.cookie, PANEL_COOKIE) || cookieValue(req.headers.cookie, "lm_panel_session");
  if (current) {
    const scope = await sessionScope(current, opts);
    if (scope && scope.uid === String(claim.uid) && scope.chatId === String(claim.chat_id)) await revokePanelSession(current, opts);
  }
  const session = await createPanelSession({ uid: String(claim.uid), chatId: String(claim.chat_id) }, opts);
  return jsonResponse(res, 200, { redirect: returnTo }, { "Set-Cookie": [panelSessionCookie(session), clearChallengeCookie()] });
}

async function handleDeviceSession(req, res, opts) {
  const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
  if (req.method !== "POST") return jsonResponse(res, 405, { error: "method_not_allowed" }, { Allow: "POST" });
  if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) return jsonResponse(res, 403, { error: "origin_rejected" });
  const challenge = cookieValue(req.headers.cookie, PANEL_CHALLENGE_COOKIE);
  if (!OPAQUE_TOKEN_RE.test(challenge)) return jsonResponse(res, 401, { error: "challenge_required" });
  const body = await readJsonBody(req);
  const returnTo = panelReturnPath(body && body.returnTo);
  const rows = await panelRpc("exchange_lm_panel_device_challenge", { p_challenge_hash: sha256(challenge) }, opts);
  const claim = (Array.isArray(rows) ? rows[0] : rows) || { status: "not_found" };
  if (claim.status === "pending") return jsonResponse(res, 202, { status: "pending" });
  if (claim.status === "replayed") return jsonResponse(res, 409, { error: "challenge_replayed" }, { "Set-Cookie": clearChallengeCookie() });
  if (claim.status === "expired") return jsonResponse(res, 410, { error: "challenge_expired" }, { "Set-Cookie": clearChallengeCookie() });
  if (claim.status !== "claimed" || !claim.uid || !claim.chat_id) return jsonResponse(res, 401, { error: "challenge_rejected" });
  const session = await createPanelSession({ uid: String(claim.uid), chatId: String(claim.chat_id) }, opts);
  return jsonResponse(res, 200, { redirect: returnTo }, { "Set-Cookie": [panelSessionCookie(session), clearChallengeCookie()] });
}

async function handlePanelRequest(req, res, opts = {}) {
  const requestUrl = new URL(req.url || "/panel", "http://panel.local");
  const pathname = requestUrl.pathname;
  if (pathname === "/api/panel/session/telegram") return handleTelegramSession(req, res, opts);
  if (pathname === "/api/panel/session/device") return handleDeviceSession(req, res, opts);
  if (pathname === "/panel/logout") {
    const session = cookieValue(req.headers.cookie, PANEL_COOKIE) || cookieValue(req.headers.cookie, "lm_panel_session");
    const expectedOrigin = String(opts.panelOrigin || opts.panelBaseUrl || "").replace(/\/$/, "");
    if (req.method !== "POST") { res.writeHead(405, { Allow: "POST", "cache-control": "no-store" }); res.end(); return; }
    if (!expectedOrigin || String(req.headers.origin || "") !== expectedOrigin) { res.writeHead(403, { Allow: "POST", "cache-control": "no-store" }); res.end(); return; }
    const scope = await sessionScope(session, opts);
    if (!scope || !timingEqual(req.headers["x-lm-csrf"], scope.csrf || csrfToken(session))) { res.writeHead(403, { Allow: "POST", "cache-control": "no-store" }); res.end(); return; }
    await revokePanelSession(session, opts);
    res.writeHead(303, { Location: "/panel", "Set-Cookie": clearPanelCookies(), "cache-control": "no-store" }); res.end(); return;
  }
  if (req.method !== "GET") {
    res.writeHead(405, { Allow: "GET" });
    res.end("method not allowed");
    return;
  }
  if (requestUrl.search) {
    res.writeHead(303, { Location: pathname === "/panel/onboarding" ? "/panel/onboarding" : "/panel", "cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff" });
    res.end();
    return;
  }

  const session = cookieValue(req.headers.cookie, PANEL_COOKIE) || cookieValue(req.headers.cookie, "lm_panel_session");
  const scope = await sessionScope(session, opts);
  if (scope) {
    res.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...(panelScopeCookie(scope) ? { "Set-Cookie": panelScopeCookie(scope) } : {}),
    });
    res.end(pathname === "/panel/onboarding" ? renderPanelOnboardingPage({ csrf: scope.csrf }) : renderPanelPage({ csrf: scope.csrf }));
    return;
  }

  const device = await createPanelDeviceChallenge(opts);
  res.writeHead(200, {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; script-src 'self' https://telegram.org 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://web.telegram.org",
    "Set-Cookie": challengeCookie(device.challenge),
  });
  res.end(renderPanelLogin({ code: device.code, returnTo: pathname === "/panel/onboarding" ? "/panel/onboarding" : "/panel" }));
}

function timingEqual(left, right) {
  const a = Buffer.from(String(left || "")), b = Buffer.from(String(right || ""));
  return a.length > 0 && a.length === b.length && crypto.timingSafeEqual(a, b);
}

module.exports = {
  PANEL_SESSION_IDLE_MS,
  PANEL_TELEGRAM_INIT_MAX_AGE_MS,
  PANEL_DEVICE_TTL_MS,
  sha256,
  sendPanelLink,
  createPanelSession,
  panelSessionCookie,
  panelScopeCookie,
  clearPanelCookies,
  resolvePanelSession,
  revokePanelSession,
  revokePanelSessionsForTenant,
  cookieValue,
  csrfToken,
  sessionScope,
  sessionUid,
  handleMoneyPrinterGuestRequest,
  verifyTelegramInitData,
  claimTelegramWebhookActor,
  createPanelDeviceChallenge,
  panelDeviceCodeFromCommand,
  confirmPanelDeviceCode,
  handlePanelRequest,
};
