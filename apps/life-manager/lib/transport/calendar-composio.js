// lib/transport/calendar-composio.js — CLOUD calendar transport (#74 convergence). Wraps the Composio
// managed-OAuth GOOGLECALENDAR_* tools behind the adapter interface every life-logic module will use,
// so the same JS runs cloud (this) or local (calendar-gog.js, slice 5). Behaviour-identical to the
// inline Composio calls it replaces — the live caller is unchanged.
"use strict";
const { recordCost } = require("../ledger.js");

const COMPOSIO_EXEC = "https://backend.composio.dev/api/v3/tools/execute";

async function selectedAccountId(uid, opts = {}) {
  if (typeof opts.resolveConnectedAccountId === "function") return opts.resolveConnectedAccountId(uid);
  const base = opts.supaUrl || process.env.SUPABASE_URL;
  const key = opts.supaKey || process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!base || !key) return null;
  const r = await (opts.fetchImpl || fetch)(`${base}/rest/v1/lm_users?uid=eq.${encodeURIComponent(uid)}&select=calendar_connected_account_id&limit=1`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
  if (!r.ok) throw new Error("calendar account lookup failed");
  const rows = await r.json();
  return Array.isArray(rows) && rows[0] ? rows[0].calendar_connected_account_id || null : null;
}

async function exec(tool, uid, args, apiKey, opts) {
  const connectedAccountId = await selectedAccountId(uid, opts);
  const r = await (opts.fetchImpl || fetch)(`${COMPOSIO_EXEC}/${tool}`, {
    method: "POST",
    headers: { "x-api-key": apiKey, "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: uid, ...(connectedAccountId ? { connected_account_id: connectedAccountId } : {}), arguments: args }),
  });
  return r.json();
}

function makeComposioCalendar(opts = {}) {
  const { apiKey, recordCall } = opts;
  const key = apiKey || process.env.COMPOSIO_API_KEY;
  const ledger = recordCall || ((uid, tool) => {
    if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) return false;
    return recordCost({ uid, kind: "composio_call", quantity: 1, unit: "call", estUsd: 0, meta: { tool } });
  });
  const execute = async (tool, uid, args) => {
    const result = await exec(tool, uid, args, key, opts);
    await Promise.resolve(ledger(uid, tool)).catch(() => false);
    return result;
  };
  // ONE page of Google Calendar items PLUS the cursor that unlocks the next. events.list returns at
  // most `maxResults` items per page (250 by default, 2500 max) and sets data.nextPageToken whenever
  // more remain — measured against this exact endpoint on 2026-07-26: a 548-day window came back as
  // 250 + 250 + 203 with a live token on the first two pages, and the identical 703 events arrive in
  // one call at maxResults=2500 with NO token. listEventsRaw used to drop that token on the floor,
  // which left "the calendar holds 703 events" and "it holds 7000 and you were handed page one"
  // indistinguishable to every caller. A caller that persists an append-only record
  // (fetchCalendarHistory → lm_care_scan_log) cannot live with that ambiguity, so the cursor is now
  // part of the transport contract.
  // Error contract unchanged and shared with listEventsRaw: default (wake path) swallows every
  // failure to an empty page — load-bearing, a transport blip must not crash the 60s tick — while
  // strict (history path) THROWS, because "empty calendar" and "the read failed" must never merge.
  const listEventsPage = async (uid, { timeMin, timeMax, maxResults, pageToken, strict } = {}) => {
    const empty = { items: [], nextPageToken: null };
    if (!key || !uid) {
      if (strict) throw new Error(`calendar transport not ready (missing ${key ? "uid" : "API key"})`);
      return empty;
    }
    const args = { calendarId: "primary", singleEvents: true, orderBy: "startTime", timeMin, timeMax };
    if (maxResults) args.maxResults = maxResults;
    if (pageToken) args.pageToken = pageToken;
    let j;
    try {
      j = await execute("GOOGLECALENDAR_EVENTS_LIST", uid, args);
    } catch (e) {
      if (strict) throw e;
      return empty;
    }
    if (!j || !j.successful) {
      if (strict) throw new Error(`calendar list failed: ${String((j && (j.error || j.message)) || "unsuccessful response")}`);
      return empty;
    }
    const d = j.data || {};
    return {
      items: d.items || d.events || [],
      nextPageToken: typeof d.nextPageToken === "string" && d.nextPageToken ? d.nextPageToken : null,
    };
  };
  return {
    kind: "composio",
    ready: () => !!key,
    listEventsPage,
    // Raw Google Calendar items (each consumer maps to its own shape) for [timeMin, timeMax] (ISO Z),
    // FIRST page only — the wake path reads an 18-hour horizon and has never approached a page
    // boundary. Callers that need provable completeness follow listEventsPage's cursor instead.
    async listEventsRaw(uid, opts = {}) {
      return (await listEventsPage(uid, opts)).items;
    },
    async createEvent(uid, args) {
      if (!key) return { successful: false };
      try { return await execute("GOOGLECALENDAR_CREATE_EVENT", uid, args); } catch { return { successful: false }; }
    },
    async patchEvent(uid, args) {
      if (!key) return { successful: false };
      try { return await execute("GOOGLECALENDAR_PATCH_EVENT", uid, args); } catch { return { successful: false }; }
    },
  };
}

module.exports = { makeComposioCalendar };
