// Session-scoped Calendar consent for panel onboarding.
"use strict";

const crypto = require("node:crypto");
const { cookieValue, csrfToken, panelScopeCookie, sessionScope, sha256 } = require("./panel-auth.js");
const { createSupabaseCommandStore, readJson, composioCalendarAccountStatus, composioCalendarStart } = require("./panel-api.js");
const { startCalendarOAuth } = require("./user-command.js");

const CALENDAR_STATE_TTL_MS = 5 * 60 * 1000;
const CALENDAR_STATE_RE = /^[A-Za-z0-9_-]{43}$/;
const START = "/api/panel/onboarding/calendar/start";
const STATUS = "/api/panel/onboarding/calendar/status";

function sendJson(res, status, body, extra = {}) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff", ...extra });
  res.end(JSON.stringify(body));
}

function deriveCalendarState(nonce, scope, secret) {
  if (!nonce || !scope || !scope.uid || !scope.chatId || !secret) throw new Error("calendar_state_unavailable");
  return crypto.createHmac("sha256", String(secret)).update(`lm-panel-calendar:v1\n${nonce}\n${scope.uid}\n${scope.chatId}`).digest("base64url");
}

async function resolveScope(req, opts, store) {
  const session = cookieValue(req.headers && req.headers.cookie, "__Host-lm_panel_session") || cookieValue(req.headers && req.headers.cookie, "lm_panel_session");
  if (!session) return null;
  let value;
  try { value = await (opts.sessionScopeImpl || sessionScope)(session, opts); } catch { return null; }
  if (!value || !value.uid || !value.chatId) return null;
  const scope = { uid: String(value.uid), chatId: String(value.chatId), csrf: value.csrf || csrfToken(session) };
  try {
    const current = typeof store.assertCurrentScope === "function"
      ? await store.assertCurrentScope(scope)
      : typeof store.readUser === "function" && await store.readUser(scope).then((user) => user && String(user.uid) === scope.uid && String(user.telegram_chat_id) === scope.chatId);
    return current ? { session, scope, renewed: panelScopeCookie({ ...value, ...scope }) } : null;
  } catch { return null; }
}

async function syncCalendarStatus(store, scope, status) {
  if (typeof store.syncCalendarStatus !== "function") throw new Error("calendar_sync_unavailable");
  const synced = await store.syncCalendarStatus(scope, status);
  if (synced === false) throw new Error("calendar_sync_failed");
}

async function selectedCalendar(store, scope, provider) {
  const user = await store.readUser(scope);
  const id = String(user && user.calendar_connected_account_id || "");
  if (!id) return { id: null, status: "MISSING" };
  return { id, status: await composioCalendarAccountStatus(scope, id, provider) };
}

async function handleCalendarOnboardRequest(req, res, opts = {}) {
  const path = new URL(req.url || "/", "http://panel.local").pathname;
  if (path !== START && path !== STATUS) return sendJson(res, 404, { error: "not_found" });
  try {
    const store = opts.commandStore || createSupabaseCommandStore(opts);
    const auth = await resolveScope(req, opts, store);
    if (!auth) return sendJson(res, 401, { error: "unauthorized" });
    if (auth.renewed && typeof res.setHeader === "function") res.setHeader("Set-Cookie", auth.renewed);
    if (path === STATUS) {
      if (req.method !== "GET") return sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "GET" });
      const selected = opts.composioCalendarStatusImpl
        ? { id: null, status: await opts.composioCalendarStatusImpl(auth.scope, opts) }
        : await selectedCalendar(store, auth.scope, { ...opts, composioKey: opts.composioKey || process.env.COMPOSIO_API_KEY });
      const status = selected.status;
      if (!["ACTIVE", "MISSING", "DISABLED", "INACTIVE"].includes(status)) throw new Error("calendar_status_unavailable");
      await syncCalendarStatus(store, auth.scope, status);
      if (status === "ACTIVE") return sendJson(res, 200, { connected: true, state: "connected" });
      if (["MISSING", "DISABLED", "INACTIVE"].includes(status)) return sendJson(res, 200, { connected: false, state: "action_required" });
      throw new Error("calendar_status_unavailable");
    }
    if (req.method !== "POST") return sendJson(res, 405, { error: "method_not_allowed" }, { Allow: "POST" });
    const configured = String(opts.panelOrigin || opts.panelBaseUrl || "");
    let origin = "";
    try { const parsed = new URL(configured.endsWith("/") ? configured.slice(0, -1) : configured); if (configured === configured.trim() && /^https:\/\//.test(configured) && parsed.protocol === "https:" && !parsed.username && !parsed.password && parsed.pathname === "/" && !parsed.search && !parsed.hash) origin = parsed.origin; } catch {}
    if (!origin || String(req.headers && req.headers.origin || "") !== origin) return sendJson(res, 403, { error: "origin_rejected" });
    if (!/^application\/json(?:;|$)/i.test(String(req.headers && req.headers["content-type"] || ""))) return sendJson(res, 415, { error: "json_required" });
    const csrf = Buffer.from(String(req.headers && req.headers["x-lm-csrf"] || "")), expected = Buffer.from(String(auth.scope.csrf || csrfToken(auth.session)));
    if (!csrf.length || csrf.length !== expected.length || !crypto.timingSafeEqual(csrf, expected)) return sendJson(res, 403, { error: "csrf_rejected" });
    let body;
    try { body = await readJson(req); } catch { return sendJson(res, 400, { error: "invalid_json" }); }
    if (!body || typeof body !== "object" || Array.isArray(body)) return sendJson(res, 400, { error: "invalid_json" });
    const secret = opts.sessionSecret || process.env.LM_PANEL_SESSION_ROTATION_SECRET || process.env.LM_UID_SECRET;
    if (!secret) return sendJson(res, 502, { error: "calendar_unavailable" });
    const provider = { ...opts, panelBaseUrl: opts.panelBaseUrl || origin, composioKey: opts.composioKey || process.env.COMPOSIO_API_KEY, composioAuthConfig: opts.composioAuthConfig || process.env.COMPOSIO_GCAL_AUTH_CONFIG };
    const selected = opts.composioCalendarStatusImpl
      ? { id: null, status: await opts.composioCalendarStatusImpl(auth.scope, provider) }
      : await selectedCalendar(store, auth.scope, provider);
    const status = selected.status;
    if (!["ACTIVE", "MISSING", "DISABLED", "INACTIVE"].includes(status)) throw new Error("calendar_status_unavailable");
    await syncCalendarStatus(store, auth.scope, status);
    if (status === "ACTIVE") return sendJson(res, 200, { connected: true, state: "connected" });
    if (!["MISSING", "DISABLED", "INACTIVE"].includes(status)) throw new Error("calendar_status_unavailable");
    const conflict = async () => {
      let current = true;
      try { if (typeof store.assertCurrentScope === "function") current = await store.assertCurrentScope(auth.scope); } catch { current = false; }
      return sendJson(res, current ? 409 : 401, { error: current ? "calendar_in_progress" : "unauthorized" });
    };
    const nonce = (opts.randomBytes || crypto.randomBytes)(32).toString("base64url");
    const state = deriveCalendarState(nonce, auth.scope, secret);
    if (!CALENDAR_STATE_RE.test(state)) throw new Error("invalid_state");
    let claimed;
    try {
      claimed = await store.createOAuthState(auth.scope, { stateHash: sha256(state), provider: "calendar", expiresAt: new Date((Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now()) + CALENDAR_STATE_TTL_MS).toISOString() });
    } catch (error) {
      if (error && (error.code === "oauth_state_in_progress" || error.message === "oauth_state_in_progress" || error.status === 409)) return conflict();
      throw error;
    }
    if (claimed === false) return conflict();
    const resumed = await (opts.composioCalendarStartImpl || composioCalendarStart)(auth.scope, { ...provider, connectedAccountId: selected.id });
    if (resumed && (resumed.state === "connected" || resumed.connected === true || resumed.state?.state === "connected")) {
      await syncCalendarStatus(store, auth.scope, "ACTIVE");
      return sendJson(res, 200, { connected: true, state: "connected" });
    }
    const oauth = await (opts.startCalendarOAuthImpl || startCalendarOAuth)(auth.scope, state, provider);
    if (!oauth || !oauth.connectedAccountId || typeof store.attachOAuthAccount !== "function"
      || !await store.attachOAuthAccount(auth.scope, sha256(state), oauth.connectedAccountId)) throw new Error("oauth_account_bind_failed");
    const redirect = oauth && oauth.redirectUrl;
    let redirectUrl;
    try { const parsed = new URL(redirect); if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.origin === "null" || /[\r\n]/.test(String(redirect))) throw new Error("redirect"); redirectUrl = redirect; } catch { throw new Error("redirect"); }
    return sendJson(res, 200, { connected: false, state: "action_required", redirectUrl });
  } catch {
    return sendJson(res, 502, { error: "calendar_unavailable" });
  }
}

module.exports = { CALENDAR_STATE_TTL_MS, CALENDAR_STATE_RE, START, STATUS, sha256, deriveCalendarState, handleCalendarOnboardRequest };
