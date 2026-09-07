"use strict";

const crypto = require("node:crypto");
const { DEFAULTS: RUNTIME_DEFAULTS } = require("./runtime-preferences.js");
const { normalizePhone } = require("./telegram-onboard.js");

const BOOLEAN_SETTINGS = new Set(["call_enabled", "notifications_enabled", "daily_automation_enabled"]);
const USER_SETTINGS = new Set(["call_language", "wake_policy"]);
const TIME_ZONES = new Set(["Asia/Tokyo", "UTC", "Europe/London", "America/New_York", "America/Los_Angeles"]);
const AVAILABLE_ACTIONS = Object.freeze([
  "connect calendar / カレンダーをつないで",
  "disconnect calendar / カレンダーを切断",
  "set home <address> / 自宅を<住所>にして",
  "set phone <number> / 電話番号を<番号>にして",
  "remove phone / 電話番号を削除",
  "turn calls on|off / 電話をオン|オフ",
  "turn notifications on|off / 通知をオン|オフ",
  "turn daily automation on|off / デイリー自動化をオン|オフ",
]);
const DELEGATION_UNAVAILABLE = Object.freeze({
  en: "Delegation is unavailable because no safe delegated-action runtime is active.",
  ja: "安全な委任アクション実行基盤が稼働していないため、委任は利用できません。",
});

function invalid() { const error = new Error("invalid_action"); error.status = 400; throw error; }
function exactKeys(value, expected) {
  const keys = Object.keys(value || {}).sort();
  return keys.length === expected.length && keys.every((key, index) => key === [...expected].sort()[index]);
}

function validateCommand(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  if (input.type === "connection.start") {
    if (!exactKeys(input, ["type", "provider"]) || input.provider !== "calendar") invalid();
    return Object.freeze({ type: input.type, provider: input.provider });
  }
  if (input.type === "connection.disconnect") {
    if (!exactKeys(input, ["type", "provider"]) || input.provider !== "calendar") invalid();
    return Object.freeze({ type: input.type, provider: input.provider });
  }
  if (input.type === "profile.set") {
    if (!exactKeys(input, ["type", "field", "value"])) invalid();
    if (input.field === "home_address") {
      const value = typeof input.value === "string" ? input.value.trim() : "";
      if (!value || value.length > 240) invalid();
      return Object.freeze({ type: "profile.set", field: "home_address", value });
    }
    if (input.field === "phone") {
      if (input.value === null) return Object.freeze({ type: "profile.set", field: "phone", value: null });
      const value = normalizePhone(input.value);
      if (!value) invalid();
      return Object.freeze({ type: "profile.set", field: "phone", value });
    }
    invalid();
  }
  if (input.type !== "setting.set" || !exactKeys(input, ["type", "setting", "value"])) invalid();
  if (BOOLEAN_SETTINGS.has(input.setting) && typeof input.value === "boolean") return Object.freeze({ ...input });
  if (input.setting === "call_language" && ["en", "ja"].includes(input.value)) return Object.freeze({ ...input });
  if (input.setting === "wake_policy" && ["travel-only", "all-events"].includes(input.value)) return Object.freeze({ ...input });
  if (input.setting === "call_time_zone" && TIME_ZONES.has(input.value)) return Object.freeze({ ...input });
  invalid();
}

function normalizeText(value) {
  return String(value || "").trim().toLowerCase().replace(/[。.!！?？]+$/g, "").replace(/\s+/g, " ");
}

function compactRawText(value) {
  return String(value || "").trim().replace(/[。.!！?？]+$/g, "").replace(/\s+/g, " ");
}

function parseUserCommand(text) {
  const raw = compactRawText(text);
  const value = raw.toLowerCase();
  const setting = (name, enabled) => ({ kind: "command", command: { type: "setting.set", setting: name, value: enabled } });
  const profile = (field, next) => ({ kind: "command", command: validateCommand({ type: "profile.set", field, value: next }) });
  let match;

  if (/^(open|get) (the )?dashboard( link)?$/.test(value) || /^(ダッシュボード|パネル)を?開いて$/.test(value)) return { kind: "panel" };
  if ((match = /^(?:set|change) (?:my )?(?:home|base)(?: address)? (.+)$/i.exec(raw))) return profile("home_address", match[1]);
  if ((match = /^(?:自宅|家|基準地点)を?(.+?)に(?:して|変更して)$/.exec(raw))) return profile("home_address", match[1]);
  if ((match = /^(?:set|change) (?:my )?phone(?: number)? (.+)$/i.exec(raw))) return profile("phone", match[1]);
  if ((match = /^電話番号を?(.+?)に(?:して|変更して)$/.exec(raw))) return profile("phone", match[1]);
  if (/^(?:remove|delete|clear) (?:my )?phone(?: number)?$/i.test(raw) || /^電話番号を?(?:削除|消して|解除)$/.test(raw)) return profile("phone", null);
  if (/^(connect|reconnect) (my )?(google )?calendar$/.test(value) || /^(カレンダーを?(接続|つないで|繋いで|再接続))$/.test(value)) return { kind: "command", command: { type: "connection.start", provider: "calendar" } };
  if (/^disconnect (my )?(google )?calendar$/.test(value) || /^カレンダーを?(切断|解除して)$/.test(value)) return { kind: "command", command: { type: "connection.disconnect", provider: "calendar" } };
  if (/^(turn |disable |enable )?(calls?|call)( (on|off))?$/.test(value)) return setting("call_enabled", !/(off|disable)/.test(value));
  if (/^(電話|コール)を?(止めて|オフ)$/.test(value)) return setting("call_enabled", false);
  if (/^(電話|コール)を?(再開して|オン)$/.test(value)) return setting("call_enabled", true);
  if (/^(turn )?notifications? (on|off)$/.test(value)) return setting("notifications_enabled", value.endsWith("on"));
  if (/^通知を?(オン|オフ)$/.test(value)) return setting("notifications_enabled", value.endsWith("オン"));
  if (/^(turn )?daily automation (on|off)$/.test(value)) return setting("daily_automation_enabled", value.endsWith("on"));
  if (/^デイリー自動化を?(オン|オフ)$/.test(value)) return setting("daily_automation_enabled", value.endsWith("オン"));
  if (/^(turn )?delegation (on|off)$/.test(value)) return { kind: "unavailable", message: DELEGATION_UNAVAILABLE.en };
  if (/^委任を?(オン|オフ)$/.test(value)) return { kind: "unavailable", message: DELEGATION_UNAVAILABLE.ja };
  if (/^calls? in (english|japanese)$/.test(value)) return { kind: "command", command: { type: "setting.set", setting: "call_language", value: value.endsWith("japanese") ? "ja" : "en" } };
  if (/^電話を?(英語|日本語)にして$/.test(value)) return { kind: "command", command: { type: "setting.set", setting: "call_language", value: value.includes("日本語") ? "ja" : "en" } };
  return { kind: "help", availableActions: [...AVAILABLE_ACTIONS] };
}

async function dispatchParsedControl(parsedControl, opts = {}) {
  if (parsedControl && parsedControl.kind === "unavailable") {
    return { handled: true, message: parsedControl.message };
  }
  if (!parsedControl || parsedControl.kind !== "command") return { handled: false };
  const result = await (opts.executeCommand || executeUserCommand)(opts.scope, parsedControl.command, opts.commandDeps);
  return { handled: true, result, message: result.message };
}

function hash(value) { return crypto.createHash("sha256").update(String(value)).digest("hex"); }
function requestHash(command) { return hash(JSON.stringify(command)); }

function connection(state, reason, actions = []) { return Object.freeze({ state, reason, actions: Object.freeze(actions) }); }

async function buildControlCenter(scope, deps = {}) {
  const store = deps.store;
  if (!store) throw new Error("store_required");
  const user = await store.readUser(scope);
  if (!user || String(user.telegram_chat_id) !== String(scope.chatId)) throw new Error("scope_mismatch");
  const prefs = { ...RUNTIME_DEFAULTS, call_time_zone: "Asia/Tokyo", ...(await store.readPreferences(scope)) };
  delete prefs.delegation_enabled;
  const location = await store.readLocation(scope);
  const locationLive = Boolean(location && (!location.expires_at || Date.parse(location.expires_at) > (deps.nowMs == null ? Date.now() : deps.nowMs)));
  let calendarState = user.calendar_provider === "composio_gcal" ? "ACTIVE" : "INACTIVE";
  try { if (deps.calendarStatus) calendarState = await deps.calendarStatus(scope); }
  catch { calendarState = "ERROR"; }
  return {
    identity: { name: "Life Manager user", uidRef: `user:${hash(user.uid).slice(0, 12)}` },
    context: { timeZone: prefs.call_time_zone, locationAvailable: locationLive },
    connections: {
      calendar: calendarState === "ACTIVE" ? { ...connection("connected", "Google Calendar is connected", ["connection.disconnect:calendar"]), actionLabel: "Disconnect calendar" } : calendarState === "ERROR" ? connection("error", "Calendar status is temporarily unavailable") : calendarState === "DISABLED" ? { ...connection("action_required", "Reconnect Google Calendar", ["connection.start:calendar"]), actionLabel: "Reconnect calendar" } : { ...connection("action_required", "Connect Google Calendar to manage your schedule", ["connection.start:calendar"]), actionLabel: "Connect Calendar" },
      telegram: connection("connected", "This dashboard is linked to your Telegram chat"),
      location: locationLive ? connection("connected", "Live location permission is available") : connection("action_required", "Share live location in Telegram to unlock automatic late notices", ["instructions:location"]),
      call: !user.phone ? connection("action_required", "Add a phone number in Telegram", ["instructions:call"]) : prefs.call_enabled ? connection("connected", "Calls are enabled", ["setting.set:call_enabled:false"]) : connection("action_required", "Calls are turned off", ["setting.set:call_enabled:true"]),
      email: connection("unavailable", "Gmail reading is unavailable while a verified free connection path is absent"),
      wallet: user.payout_destination ? connection("connected", "Payout destination is configured") : connection("action_required", "Set up a payout destination in Telegram", ["instructions:wallet"]),
    },
    settings: {
      ...prefs,
      call_language: user.call_language != null ? user.call_language : null,
      wake_policy: user.wake_policy != null ? user.wake_policy : "travel-only",
    },
    controls: { delegation: { state: "unavailable", reason: "No safe delegated-action runtime is available" }, physical_automation: { state: "unavailable" }, mental_automation: { state: "unavailable" }, financial_automation: { state: "unavailable" } },
  };
}

async function executeUserCommand(scope, rawCommand, deps = {}) {
  const command = validateCommand(rawCommand), store = deps.store;
  if (!store) throw new Error("store_required");
  const key = String(deps.idempotencyKey || "");
  if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) { const error = new Error("idempotency_required"); error.status = 400; throw error; }
  const user = await store.readUser(scope);
  if (!user || String(user.telegram_chat_id) !== String(scope.chatId)) throw new Error("scope_mismatch");
  const digest = requestHash(command), existing = await store.readReceipt(scope, key);
  if (existing) {
    if (existing.requestHash !== digest) { const error = new Error("idempotency_conflict"); error.status = 409; throw error; }
    if (existing.status === "succeeded" && existing.result) return existing.result;
    const error = new Error(existing.status === "pending" ? "idempotency_in_progress" : "idempotency_failed"); error.status = 409; throw error;
  } else {
    const claimed = await store.claimReceipt(scope, key, { requestHash: digest, commandType: command.type, status: "pending" });
    if (!claimed) { const error = new Error("idempotency_in_progress"); error.status = 409; throw error; }
  }
  try {
    let state;
    const rebound = store.assertCurrentScope ? null : await store.readUser(scope);
    const current = store.assertCurrentScope ? await store.assertCurrentScope(scope) : Boolean(rebound && String(rebound.telegram_chat_id) === String(scope.chatId));
    if (!current) throw new Error("scope_mismatch");
    if (command.type === "setting.set") {
      if (BOOLEAN_SETTINGS.has(command.setting) || command.setting === "call_time_zone") state = await (store.mutatePreferences || store.patchPreferences).call(store, scope, { [command.setting]: command.value });
      else state = await (store.mutateUser || store.patchUser).call(store, scope, { [command.setting]: command.value });
    } else if (command.type === "profile.set") {
      state = await (store.mutateUser || store.patchUser).call(store, scope, { [command.field]: command.value });
    } else if (command.type === "connection.start") {
      const resumed = deps.startCalendarConnection ? await deps.startCalendarConnection(scope) : null;
      if (resumed) {
        state = resumed;
      } else {
        const bytes = (deps.randomBytes || crypto.randomBytes)(32), stateToken = bytes.toString("base64url");
        await store.createOAuthState(scope, { stateHash: hash(stateToken), provider: "calendar", expiresAt: new Date(Date.now() + 5 * 60 * 1000).toISOString() });
        const oauth = await (deps.startCalendarOAuth || startCalendarOAuth)(scope, stateToken, deps);
        state = { provider: "calendar", state: "action_required", redirectUrl: oauth.redirectUrl };
      }
    } else {
      state = await (deps.disconnectCalendar || disconnectCalendar)(scope, deps);
    }
    const message = command.type === "setting.set" ? "Setting updated"
      : command.type === "profile.set" ? "Profile updated"
        : command.type === "connection.disconnect" ? "Calendar disconnected" : "Calendar connection is ready";
    const result = { ok: true, command, state, message };
    await store.finishReceipt(scope, key, { requestHash: digest, commandType: command.type, status: "succeeded", result });
    return result;
  } catch (error) {
    await store.finishReceipt(scope, key, { requestHash: digest, commandType: command.type, status: "failed", result: null });
    throw error;
  }
}

async function disconnectCalendar(scope, deps = {}) {
  if (!deps.composioKey || !deps.calendarAccount) throw new Error("provider_unavailable");
  return deps.calendarAccount.disable(scope);
}

async function startCalendarOAuth(scope, stateToken, deps = {}) {
  if (!deps.composioKey || !deps.composioAuthConfig) throw new Error("provider_unavailable");
  const callbackPath = deps.calendarCallbackPath || "/panel/oauth/calendar";
  if (!/^\/[A-Za-z0-9/_-]+$/.test(callbackPath)) throw new Error("provider_unavailable");
  let callbackUrl;
  try {
    const panelBase = new URL(String(deps.panelBaseUrl || ""));
    if (panelBase.protocol !== "https:" || panelBase.username || panelBase.password) throw new Error("invalid_panel_base");
    callbackUrl = new URL(callbackPath, panelBase.origin);
  } catch { throw new Error("provider_unavailable"); }
  callbackUrl.searchParams.set("state", stateToken);
  for (const [key, value] of Object.entries(deps.calendarCallbackParams || {})) {
    if (/^[a-z][a-z0-9_-]{0,31}$/i.test(key) && value != null) callbackUrl.searchParams.set(key, String(value).slice(0, 32));
  }
  const callback = callbackUrl.toString();
  const response = await (deps.fetchImpl || fetch)("https://backend.composio.dev/api/v3/connected_accounts/link", {
    method: "POST", headers: { "x-api-key": deps.composioKey, "content-type": "application/json" },
    body: JSON.stringify({ auth_config_id: deps.composioAuthConfig, user_id: scope.uid, callback_url: callback }),
  });
  if (!response.ok) throw new Error("provider_failed");
  const body = await response.json().catch(() => ({}));
  const redirect = body.redirect_url;
  if (typeof redirect !== "string" || !redirect) throw new Error("provider_failed");
  try {
    const url = new URL(redirect);
    if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error("invalid_protocol");
  } catch { throw new Error("provider_failed"); }
  return { redirectUrl: redirect };
}

async function claimCalendarOAuthState(scope, stateToken, deps = {}) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(String(stateToken || ""))) return false;
  return Boolean(await deps.store.claimOAuthState(scope, hash(stateToken)));
}

module.exports = { BOOLEAN_SETTINGS, parseUserCommand, dispatchParsedControl, validateCommand, buildControlCenter, executeUserCommand, disconnectCalendar, startCalendarOAuth, claimCalendarOAuthState, requestHash };
