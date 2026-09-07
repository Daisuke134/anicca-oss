"use strict";

const crypto = require("node:crypto");

const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const MAX_WEBHOOK_AGE_SECONDS = 5 * 60;
const MAX_WAKE_CLAIM_TOKEN_LENGTH = 512;

function isValidOptionalText(value, maxLength) {
  return typeof value === "string" && value.trim() !== "" && value.length <= maxLength;
}

function encodeWakeClientState({ wakeUid, wakeEventKey, wakeClaimToken, managedActionKey,
  managedPeriodStart, managedReservationToken, voicePeriodStart, voiceReservationToken,
  voiceAllowedSeconds } = {}) {
  if (!wakeUid || !wakeEventKey) return "";
  const state = { wakeUid, wakeEventKey };
  if (isValidOptionalText(wakeClaimToken, MAX_WAKE_CLAIM_TOKEN_LENGTH)) state.wakeClaimToken = wakeClaimToken;
  if (isValidOptionalText(managedActionKey, MAX_WAKE_CLAIM_TOKEN_LENGTH)) state.managedActionKey = managedActionKey;
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(managedPeriodStart || ""))
      && isValidOptionalText(managedReservationToken, MAX_WAKE_CLAIM_TOKEN_LENGTH)) {
    state.managedPeriodStart = managedPeriodStart;
    state.managedReservationToken = managedReservationToken;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(voicePeriodStart || ""))
      && isValidOptionalText(voiceReservationToken, MAX_WAKE_CLAIM_TOKEN_LENGTH)
      && Number.isInteger(voiceAllowedSeconds) && voiceAllowedSeconds >= 1 && voiceAllowedSeconds <= 120) {
    state.voicePeriodStart = voicePeriodStart;
    state.voiceReservationToken = voiceReservationToken;
    state.voiceAllowedSeconds = voiceAllowedSeconds;
  }
  return Buffer.from(JSON.stringify(state), "utf8").toString("base64");
}

function decodeWakeClientState(value) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(Buffer.from(String(value), "base64").toString("utf8"));
    if (!parsed || typeof parsed.wakeUid !== "string" || typeof parsed.wakeEventKey !== "string") return null;
    if (!parsed.wakeUid || !parsed.wakeEventKey) return null;
    const state = {
      wakeUid: parsed.wakeUid.slice(0, 100),
      wakeEventKey: parsed.wakeEventKey.slice(0, 300),
    };
    if (isValidOptionalText(parsed.wakeClaimToken, MAX_WAKE_CLAIM_TOKEN_LENGTH)) {
      state.wakeClaimToken = parsed.wakeClaimToken;
    }
    if (isValidOptionalText(parsed.managedActionKey, MAX_WAKE_CLAIM_TOKEN_LENGTH)) {
      state.managedActionKey = parsed.managedActionKey;
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(parsed.managedPeriodStart || ""))
        && isValidOptionalText(parsed.managedReservationToken, MAX_WAKE_CLAIM_TOKEN_LENGTH)) {
      state.managedPeriodStart = parsed.managedPeriodStart;
      state.managedReservationToken = parsed.managedReservationToken;
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(parsed.voicePeriodStart || ""))
        && isValidOptionalText(parsed.voiceReservationToken, MAX_WAKE_CLAIM_TOKEN_LENGTH)
        && Number.isInteger(parsed.voiceAllowedSeconds) && parsed.voiceAllowedSeconds >= 1
        && parsed.voiceAllowedSeconds <= 120) {
      state.voicePeriodStart = parsed.voicePeriodStart;
      state.voiceReservationToken = parsed.voiceReservationToken;
      state.voiceAllowedSeconds = parsed.voiceAllowedSeconds;
    }
    return state;
  } catch {
    return null;
  }
}

// spec §3 row 2d: /test-call went out with NO client_state at all, so the webhook decoded null and
// returned "no wake context" before it could reach the hangup — every test call that hit a voicemail
// ran to the carrier's 120-second recording limit. The fix is to give a call state that says WHICH
// KIND of call it belongs to, because the two kinds have different records: a wake call has an
// lm_wake_log row to write amd_result onto, a test call has none. Writing the same shape for both
// would aim a PATCH at a row that does not exist and turn matched=0 (the "a wake row went missing"
// alarm of §1.3) into routine noise.
function encodeTestCallClientState({ testUid } = {}) {
  // "" and not a decodable blob: dial.js omits client_state entirely on a falsy value, and a call we
  // cannot name in a log is worse than a call that carries no state at all.
  if (!testUid) return "";
  return Buffer.from(JSON.stringify({ testUid }), "utf8").toString("base64");
}

// One decoder for both kinds so the webhook branches on `kind` instead of trying each decoder in turn
// and inferring the kind from which one answered. Wake is tried first and unchanged: its existing
// callers and its stricter shape (both fields required) keep deciding what a wake call is.
function decodeCallClientState(value) {
  const wake = decodeWakeClientState(value);
  if (wake) return { kind: "wake", ...wake };
  if (!value) return null;
  try {
    const parsed = JSON.parse(Buffer.from(String(value), "base64").toString("utf8"));
    if (!parsed || typeof parsed.testUid !== "string" || !parsed.testUid) return null;
    // Same slice as the wake fields: this ends up in a log line, and Telnyx echoes client_state back
    // verbatim, so its length is attacker-controlled input to our own logs.
    return { kind: "test", testUid: parsed.testUid.slice(0, 100) };
  } catch {
    return null;
  }
}

function createTelnyxPublicKey(value) {
  const text = String(value || "").trim().replace(/\\n/g, "\n");
  if (!text) return null;
  if (text.includes("BEGIN PUBLIC KEY")) return crypto.createPublicKey(text);
  const raw = Buffer.from(text, "base64");
  if (raw.length !== 32) return null;
  return crypto.createPublicKey({
    key: Buffer.concat([ED25519_SPKI_PREFIX, raw]),
    format: "der",
    type: "spki",
  });
}

function verifyTelnyxSignature({ rawBody, signature, timestamp, publicKey, nowMs = Date.now() } = {}) {
  try {
    if (!Buffer.isBuffer(rawBody) || !signature || !timestamp || !publicKey) return false;
    const signedAt = Number(timestamp);
    if (!Number.isFinite(signedAt) || Math.abs(Math.floor(nowMs / 1000) - signedAt) > MAX_WEBHOOK_AGE_SECONDS) return false;
    const key = createTelnyxPublicKey(publicKey);
    if (!key) return false;
    const signedPayload = Buffer.concat([Buffer.from(`${timestamp}|`, "utf8"), rawBody]);
    return crypto.verify(null, signedPayload, key, Buffer.from(String(signature), "base64"));
  } catch {
    return false;
  }
}

module.exports = {
  encodeWakeClientState,
  decodeWakeClientState,
  encodeTestCallClientState,
  decodeCallClientState,
  verifyTelnyxSignature,
};
