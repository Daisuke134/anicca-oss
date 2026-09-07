"use strict";

const FREE_LIMIT = 30;
const PAID_LIMIT = 500;
const VOICE_LIMIT_SECONDS = 60 * 60;

function validResult(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && typeof value.allowed === "boolean"
    && Number.isInteger(value.used) && value.used >= 0
    && Number.isInteger(value.limit) && value.limit > 0
    && typeof value.periodStart === "string" && typeof value.resetAt === "string";
}

async function allowanceRpc(name, uid, actionKey, supaUrl, supaKey, opts = {}) {
  const fetchImpl = opts.fetchImpl === undefined ? global.fetch : opts.fetchImpl;
  if (!uid || !actionKey || !supaUrl || !supaKey || typeof fetchImpl !== "function") return null;
  try {
    const response = await fetchImpl(`${String(supaUrl).replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
      method: "POST",
      headers: {
        apikey: String(supaKey), Authorization: `Bearer ${String(supaKey)}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ p_uid: String(uid), p_action_key: String(actionKey), ...(opts.body || {}) }),
    });
    if (!response || response.ok !== true) return null;
    const value = await response.json();
    return validResult(value) ? value : null;
  } catch { return null; }
}

const reserveManagedAction = (uid, actionKey, supaUrl, supaKey, opts) =>
  allowanceRpc("reserve_lm_managed_action", uid, actionKey, supaUrl, supaKey, opts);

function ownedReservationRpc(name, uid, actionKey, supaUrl, supaKey, opts = {}) {
  const reservation = opts && opts.reservation;
  if (!reservation || !reservation.periodStart || !reservation.reservationToken) return null;
  return allowanceRpc(name, uid, actionKey, supaUrl, supaKey, {
    ...opts,
    body: {
      p_period_start: String(reservation.periodStart),
      p_reservation_token: String(reservation.reservationToken),
    },
  });
}

const completeManagedAction = (uid, actionKey, supaUrl, supaKey, opts) =>
  ownedReservationRpc("complete_lm_managed_action", uid, actionKey, supaUrl, supaKey, opts);
const releaseManagedAction = (uid, actionKey, supaUrl, supaKey, opts) =>
  ownedReservationRpc("release_lm_managed_action", uid, actionKey, supaUrl, supaKey, opts);

function validVoiceResult(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && typeof value.allowed === "boolean"
    && Number.isInteger(value.usedSeconds) && value.usedSeconds >= 0
    && value.limitSeconds === VOICE_LIMIT_SECONDS
    && Number.isInteger(value.allowedSeconds) && value.allowedSeconds >= 0
    && typeof value.periodStart === "string" && typeof value.resetAt === "string";
}

async function voiceRpc(name, uid, callKey, supaUrl, supaKey, opts = {}) {
  const fetchImpl = opts.fetchImpl === undefined ? global.fetch : opts.fetchImpl;
  if (!uid || !callKey || !supaUrl || !supaKey || typeof fetchImpl !== "function") return null;
  try {
    const response = await fetchImpl(`${String(supaUrl).replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
      method: "POST",
      headers: { apikey: String(supaKey), Authorization: `Bearer ${String(supaKey)}`, "Content-Type": "application/json" },
      body: JSON.stringify({ p_uid: String(uid), p_call_key: String(callKey), ...(opts.body || {}) }),
    });
    if (!response || response.ok !== true) return null;
    const value = await response.json();
    return validVoiceResult(value) ? value : null;
  } catch { return null; }
}

const reserveVoiceAllowance = (uid, callKey, supaUrl, supaKey, opts) =>
  voiceRpc("reserve_lm_voice_allowance", uid, callKey, supaUrl, supaKey, opts);

function ownedVoiceRpc(name, uid, callKey, supaUrl, supaKey, opts = {}) {
  const reservation = opts.reservation;
  if (!reservation || !reservation.periodStart || !reservation.reservationToken) return null;
  return voiceRpc(name, uid, callKey, supaUrl, supaKey, {
    ...opts,
    body: {
      p_period_start: String(reservation.periodStart),
      p_reservation_token: String(reservation.reservationToken),
      ...(name === "complete_lm_voice_allowance"
        ? { p_connected_seconds: Math.max(0, Math.ceil(Number(opts.connectedSeconds) || 0)) }
        : {}),
    },
  });
}

const completeVoiceAllowance = (uid, callKey, supaUrl, supaKey, opts) =>
  ownedVoiceRpc("complete_lm_voice_allowance", uid, callKey, supaUrl, supaKey, opts);
const acceptVoiceAllowance = (uid, callKey, supaUrl, supaKey, opts) =>
  ownedVoiceRpc("accept_lm_voice_allowance", uid, callKey, supaUrl, supaKey, opts);
const releaseVoiceAllowance = (uid, callKey, supaUrl, supaKey, opts) =>
  ownedVoiceRpc("release_lm_voice_allowance", uid, callKey, supaUrl, supaKey, opts);

module.exports = {
  FREE_LIMIT, PAID_LIMIT, VOICE_LIMIT_SECONDS, validResult, validVoiceResult,
  reserveManagedAction, completeManagedAction, releaseManagedAction,
  reserveVoiceAllowance, acceptVoiceAllowance, completeVoiceAllowance, releaseVoiceAllowance,
};
