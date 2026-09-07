// lib/dial.js — place a Telnyx Call Control call whose media streams to OUR bridge (/ws), so the
// answered call is bridged to Gemini Live (Charon). Reuses the proven body builders in call-logic.js
// (same ones runner-telnyx.mjs uses locally). No cloudflared: streamUrl is this service's stable
// Railway public wss. Returns { ok, ccid } | { ok:false, error }.
"use strict";

const { telnyxDialBody, telnyxStreamingStartBody } = require("./call-logic.js");
const { amdEnabled } = require("./answered.js");
const { encodeWakeClientState } = require("./telnyx-webhook.js");

const TELNYX = "https://api.telnyx.com/v2";
const MAX_PROVIDER_ID_LENGTH = 512;

function normalizeProviderId(value) {
  if (typeof value !== "string" || value.trim() === "" || value.length > MAX_PROVIDER_ID_LENGTH) return null;
  return value;
}

function authHeaders(apiKey) {
  return { Authorization: `Bearer ${apiKey || process.env.TELNYX_API_KEY}`, "Content-Type": "application/json" };
}

// `opts` lets a caller inject the transport (fetchImpl) and the credential (apiKey) instead of
// reaching for globals — the same style lib/late-notice.js uses, so a Telnyx action can be proven
// without a network or a mutated process.env. Omitted, both fall back to production exactly as before.
async function txPost(path, body, opts = {}) {
  const f = opts.fetchImpl || fetch;
  const r = await f(`${TELNYX}${path}`, { method: "POST", headers: authHeaders(opts.apiKey), body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`telnyx ${path} ${r.status}: ${JSON.stringify(j).slice(0, 200)}`);
  return j;
}

async function balanceUsd() {
  const r = await fetch(`${TELNYX}/balance`, { headers: authHeaders() });
  const j = await r.json().catch(() => ({}));
  return Number(j && j.data && j.data.balance);
}

// spec §3 row 2d: `opts.clientState` exists because reading the state out of the stream URL only ever
// worked for wake calls — /test-call builds its URL with empty wakeUid/wakeEventKey, so the dial body
// carried no client_state and the detection webhook had nothing to correlate. The kind is passed in
// rather than added to the URL: buildStreamUrl signs its query with signCtx([...]) and the bridge
// verifies the SAME ordered array, so a new query item changes what the signature means on both ends.
// An argument costs nothing and cannot desync from a signature.
function amdDialOptions(streamUrl, env = process.env, opts = {}) {
  if (!amdEnabled(env)) return {};
  const url = new URL(streamUrl);
  const wakeUid = url.searchParams.get("wakeUid") || "";
  const wakeEventKey = url.searchParams.get("wakeEventKey") || "";
  const webhookProtocol = url.protocol === "ws:" ? "http:" : "https:";
  const clientState = opts.clientState || encodeWakeClientState({ wakeUid, wakeEventKey });
  return {
    answering_machine_detection: "detect",
    webhook_url: `${webhookProtocol}//${url.host}/telnyx-events`,
    webhook_url_method: "POST",
    ...(clientState ? { client_state: clientState } : {}),
  };
}

// to: E.164 callee. streamUrl: wss://<this-svc>/ws?summary=...&dateTime=...&location=...&urgency=...
// clientState: OPTIONAL, for a caller whose identity is not in the stream URL (/test-call). Omitted,
// the wake path derives it from the URL exactly as before.
// Returns the call_control_id so the caller can issue record_start / streaming_start.
async function placeCall({ to, streamUrl, clientState, timeLimitSeconds = 120 }) {
  const API = process.env.TELNYX_API_KEY;
  const CONN = process.env.TELNYX_CONNECTION_ID;
  const FROM = process.env.TELNYX_PHONE_NUMBER;
  if (!API || !CONN || !FROM) return { ok: false, error: "telnyx env missing (API/CONN/FROM)" };
  if (!to || !streamUrl) return { ok: false, error: "to/streamUrl required" };

  // Preflight: never dial on an empty balance (a mid-call cutoff is a fake "connected").
  const usd = await balanceUsd().catch(() => NaN);
  if (!Number.isFinite(usd) || usd < 0.5) return { ok: false, error: `telnyx balance too low ($${usd})` };

  const dialBody = {
    ...telnyxDialBody({ connectionId: CONN, to, from: FROM, streamUrl, timeLimitSeconds }),
    ...amdDialOptions(streamUrl, process.env, { clientState }),
  };
  let call;
  try {
    call = await txPost("/calls", dialBody);
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
  const ccid = normalizeProviderId(call && call.data && call.data.call_control_id);
  if (!ccid) return { ok: false, error: "no call_control_id" };

  // NOTE: do NOT record_start here — the call is still RINGING (not answered), so Telnyx rejects
  // record_start ("call is not in a valid state"). Recording is started by the bridge the moment the
  // media `start` frame arrives (= call answered). See startRecording() + the server.js start handler.
  return {
    ok: true,
    ccid,
    callSessionId: normalizeProviderId(call && call.data && call.data.call_session_id),
    callLegId: normalizeProviderId(call && call.data && call.data.call_leg_id),
  };
}

// Start mp3 recording on an ANSWERED call. Telnyx record_start requires the call to be active
// (media streaming) — fire this from the bridge's Telnyx `start` frame, NOT right after dial.
// Returns { ok:true } or { ok:false, error } so the caller can LOG it (never silently swallowed).
async function startRecording(ccid) {
  if (!ccid) return { ok: false, error: "no ccid" };
  try {
    await txPost(`/calls/${encodeURIComponent(ccid)}/actions/record_start`, { format: "mp3", channels: "single" });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

// End a call in progress. spec §3 row 2b / §5.2.1: AMD says the far end is a recording, and without
// this NOTHING in this service ever hangs up — measured, `hangup_source` was `callee` on all 43
// correlated wake calls, i.e. every one of them ran until the carrier's 120-second recording limit
// cut it off, ~$0.05 of Gemini Live spoken into a voicemail nobody plays back.
//
// Same shape as startRecording: returns { ok } | { ok:false, error } and NEVER throws, so the caller
// can log it. A hangup is a cost saving; it must not be able to take out the amd_result write that
// tells a voicemail apart from a webhook that never arrived (§1.3).
// Path per Telnyx: POST /v2/calls/{call_control_id}/actions/hangup (team-telnyx/telnyx-node
// src/resources/calls/actions.ts → `path`/calls/${callControlID}/actions/hangup``).
async function hangupCall(ccid, opts = {}) {
  if (!ccid) return { ok: false, error: "no ccid" };
  try {
    // encodeURIComponent because a call_control_id is opaque base64url-ish text ("v2:T02llQ…") that
    // can carry `/` and `+`; unencoded, it would walk straight out of the actions path.
    await txPost(`/calls/${encodeURIComponent(ccid)}/actions/hangup`, {}, opts);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

module.exports = { placeCall, startRecording, hangupCall, telnyxStreamingStartBody, balanceUsd, amdDialOptions };
