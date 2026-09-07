"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { CFO_SECRET_REFS, readCfoSecret } = require("./cfo-secret-ref.js");

const ERROR = "cfo_moneytree_refresh_invalid:request";
const PRODUCTION = "https://jp-api.getmoneytree.com";
const STAGING = "https://jp-api-staging.getmoneytree.com";
const AUTH_PRODUCTION = "https://myaccount.getmoneytree.com";
const AUTH_STAGING = "https://myaccount-staging.getmoneytree.com";
const MAX_DAILY_REQUESTS = 4;
const SCOPES = Object.freeze(["guest_read", "accounts_read", "transactions_read", "request_refresh"]);
const DEFAULT_CREDENTIALS_PATH = path.join(os.homedir(), ".local", "share", "anicca", "credentials.json");

function fail() { throw new Error(ERROR); }
function plain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}
function iso(value) { return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value)); }
function baseUrl(value) {
  const candidate = value == null ? PRODUCTION : String(value).replace(/\/$/, "");
  if (candidate !== PRODUCTION && candidate !== STAGING) fail();
  return candidate;
}
function accessToken(value) {
  if (typeof value !== "string" || value.length < 20 || value.length > 4096 || /[\r\n]/.test(value)) return null;
  return value;
}
function credentialsPath(value) { return value == null ? DEFAULT_CREDENTIALS_PATH : String(value); }
function readMoneytreeCredential(file = DEFAULT_CREDENTIALS_PATH) {
  try {
    const root = JSON.parse(fs.readFileSync(credentialsPath(file), "utf8"));
    const entries = Array.isArray(root && root.credentials) ? root.credentials : [];
    return entries.find(entry => plain(entry) && entry.service === "moneytree_link") || null;
  } catch { return null; }
}
function hasMoneytreeLinkCredentials(file = DEFAULT_CREDENTIALS_PATH) {
  const entry = readMoneytreeCredential(file);
  return Boolean(entry && (accessToken(entry.access_token) || accessToken(entry.refresh_token)));
}
function authBase(apiBase) { return apiBase === STAGING ? AUTH_STAGING : AUTH_PRODUCTION; }
function expiryMillis(entry) {
  const value = entry && (entry.access_token_expires_at || entry.expires_at);
  if (typeof value === "number" && Number.isFinite(value)) return value > 2e12 ? value : value * 1000;
  if (typeof value === "string" && Number.isFinite(Date.parse(value))) return Date.parse(value);
  return null;
}
function writeMoneytreeTokens(file, patch) {
  const target = credentialsPath(file), root = JSON.parse(fs.readFileSync(target, "utf8"));
  if (!Array.isArray(root.credentials)) throw new Error("moneytree_credentials_invalid");
  const index = root.credentials.findIndex(entry => plain(entry) && entry.service === "moneytree_link");
  if (index < 0) throw new Error("moneytree_credentials_missing");
  root.credentials[index] = { ...root.credentials[index], ...patch, updated_at: new Date().toISOString() };
  const temporary = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(root, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  try { fs.chmodSync(temporary, 0o600); fs.renameSync(temporary, target); } catch (error) { try { fs.unlinkSync(temporary); } catch {} throw error; }
}
async function refreshAccessToken(entry, options) {
  const clientId = accessToken(entry && entry.client_id), clientSecret = accessToken(entry && entry.client_secret), refreshToken = accessToken(entry && entry.refresh_token);
  if (!clientId || !clientSecret || !refreshToken) return null;
  const response = await options.fetchImpl(`${authBase(baseUrl(options.baseUrl))}/oauth/token`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", client_id: clientId, client_secret: clientSecret, refresh_token: refreshToken }).toString(),
  });
  if (!response || Number(response.status) !== 200 || typeof response.json !== "function") return null;
  const payload = await response.json();
  const token = accessToken(payload && payload.access_token);
  if (!token) return null;
  const expiresIn = Number(payload.expires_in), observed = Date.parse(options.observedAt);
  const patch = { access_token: token };
  if (accessToken(payload.refresh_token)) patch.refresh_token = payload.refresh_token;
  if (Number.isFinite(expiresIn) && expiresIn > 0 && Number.isFinite(observed)) patch.access_token_expires_at = new Date(observed + expiresIn * 1000).toISOString();
  writeMoneytreeTokens(options.credentialsPath, patch);
  return token;
}
async function resolveAccessToken(options) {
  let token = accessToken(options.accessToken);
  const entry = readMoneytreeCredential(options.credentialsPath);
  if (!token) token = accessToken(entry && entry.access_token);
  const now = Date.parse(options.observedAt), expires = expiryMillis(entry);
  if (token && (!expires || !Number.isFinite(now) || expires - now > 60_000)) return token;
  if (!token && options.secretProvider && options.tenantId) {
    try { token = accessToken(await readCfoSecret(options.secretProvider, options.tenantId, options.accessTokenRef || CFO_SECRET_REFS.moneytree_link_refresh_token)); } catch { token = null; }
  }
  if (token && (!expires || !Number.isFinite(now) || expires - now > 60_000)) return token;
  if (!options.fetchImpl || !entry) return token;
  try { return await refreshAccessToken(entry, options); } catch { return null; }
}

/**
 * Request a Moneytree LINK provider refresh without ever fabricating a read.
 * The caller owns the durable quota counter and supplies a user-authorized
 * OAuth access token carrying request_refresh. A 202 only means the provider
 * queued an asynchronous job; the next read must still prove new data.
 */
async function requestMoneytreeRefresh(options = {}) {
  try {
    if (!plain(options)) fail();
    const observedAt = options.observedAt == null ? new Date().toISOString() : options.observedAt;
    if (!iso(observedAt)) fail();
    const fetchImpl = options.fetchImpl || globalThis.fetch;
    if (typeof fetchImpl !== "function") fail();
    const token = await resolveAccessToken({ ...options, fetchImpl });
    if (!token) return Object.freeze({ status: "unavailable", reason: "request_refresh_credentials_missing", observedAt });
    const requestCount = options.dailyRequestCount;
    if (requestCount === null) return Object.freeze({ status: "unavailable", reason: "refresh_quota_unknown", observedAt });
    if (!Number.isSafeInteger(requestCount) || requestCount < 0 || requestCount > MAX_DAILY_REQUESTS) fail();
    if (requestCount >= MAX_DAILY_REQUESTS) return Object.freeze({ status: "rate_limited", reason: "daily_quota_exhausted", observedAt, dailyLimit: MAX_DAILY_REQUESTS });
    const endpoint = `${baseUrl(options.baseUrl)}/link/profile/refresh.json`;
    const response = await fetchImpl(endpoint, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });
    const status = Number(response && response.status) || 0;
    if (status === 202) return Object.freeze({ status: "accepted", reason: "provider_refresh_queued", observedAt, httpStatus: 202, dailyLimit: MAX_DAILY_REQUESTS, nextReadAfterSeconds: 300 });
    if (status === 401) return Object.freeze({ status: "reauthorization_required", reason: "access_token_invalid", observedAt, httpStatus: 401 });
    if (status === 403) return Object.freeze({ status: "reauthorization_required", reason: "request_refresh_scope_missing", observedAt, httpStatus: 403 });
    if (status === 429) return Object.freeze({ status: "rate_limited", reason: "provider_or_daily_rate_limit", observedAt, httpStatus: 429, dailyLimit: MAX_DAILY_REQUESTS });
    return Object.freeze({ status: "failed", reason: "provider_refresh_rejected", observedAt, httpStatus: status || null });
  } catch (error) {
    if (error && error.message === ERROR) throw error;
    return Object.freeze({ status: "failed", reason: "provider_refresh_transport", observedAt: new Date().toISOString() });
  }
}

function buildMoneytreeAuthorizationUrl({ clientId, redirectUri, state, authorizationBaseUrl = "https://myaccount.getmoneytree.com/oauth/authorize" } = {}) {
  if (typeof clientId !== "string" || clientId.length < 1 || clientId.length > 512
    || typeof redirectUri !== "string" || redirectUri.length < 1 || redirectUri.length > 2048
    || typeof state !== "string" || state.length < 16 || state.length > 512) fail();
  const url = new URL(authorizationBaseUrl);
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", SCOPES.join(" "));
  url.searchParams.set("state", state);
  url.searchParams.set("locale", "ja-JP");
  return url.toString();
}

module.exports = { PRODUCTION, STAGING, MAX_DAILY_REQUESTS, SCOPES, requestMoneytreeRefresh, buildMoneytreeAuthorizationUrl, hasMoneytreeLinkCredentials };
