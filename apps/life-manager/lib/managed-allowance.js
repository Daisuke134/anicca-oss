"use strict";

const FREE_LIMIT = 30;
const PAID_LIMIT = 500;

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

module.exports = {
  FREE_LIMIT, PAID_LIMIT, validResult,
  reserveManagedAction, completeManagedAction, releaseManagedAction,
};
