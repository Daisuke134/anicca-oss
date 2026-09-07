// lib/wake-filter.js — #69 wake/travel importance filter (kills routine spam) + leave-time anchor
// (never-late). PURE/injectable, unit-tested in wake-filter.test.js. Used by scheduler.js tick().
"use strict";

const { computeDoorDepartureMs } = require("./travel-timing.js");

// Anicca's own inserted blocks are not real commitments — never wake someone for them.
function isHelperBlock(summary) {
  const s = summary || "";
  return s.startsWith("[Travel]") || s.includes("🚆 移動") || s.includes("[PENDING]") || s.includes("[APPLIED]");
}

const norm = (s) => (s || "").replace(/\s+/g, "").toLowerCase();

// Should we place a wake call for this event?
//   travel-only (default): only events you must TRAVEL to — a real location that isn't home. Routines
//     (no location, or at home) are skipped → no 27-calls-a-day spam. Home unknown → skip.
//   all-events: any timed non-helper event (opt-in to the original behavior).
function shouldWake(ev, home, policy) {
  if (!ev || isHelperBlock(ev.summary)) return false;
  const p = policy === "all-events" ? "all-events" : "travel-only"; // null/garbage → safe default
  if (p === "all-events") return true;
  const loc = norm(ev.location);
  if (!loc) return false;            // no location → routine / phone task → no travel
  if (!norm(home)) return false;     // home unknown → can't tell travel vs at-home → skip
  return loc !== norm(home);         // travel only when the venue differs from home
}

// Tolerance window for matching a [Travel] block to its event: travel.js caps block duration at 59 min
// (Composio event_duration_minutes ≤ 59), so a ~59.6-min travel can end up to ~1 min BEFORE event start;
// Composio echo / seconds drift can also shift it. Accept a block ending in [start-2min, start+1min].
const DEP_LO = 120000; // 2 min before event start
const DEP_HI = 60000;  // 1 min after

// Leave-time anchor (pure, block-based): the LATEST [Travel] helper block ending ~at this event's start
// marks the departure. No matching block → event start (the caller's inline fallback then refines it).
function departureMs(ev, allEvents) {
  if (!ev) return null;
  const start = ev.startMs;
  let best = null;
  for (const e of allEvents || []) {
    if (!e || !isHelperBlock(e.summary) || !Number.isFinite(e.endMs) || !Number.isFinite(e.startMs)) continue;
    if (e.endMs >= start - DEP_LO && e.endMs <= start + DEP_HI) {
      if (best === null || e.startMs > best.startMs) best = e; // latest such block = the immediate leg
    }
  }
  return best ? best.startMs : start;
}

// Origin for the inline leave-time computation: the PREVIOUS real venue if the user is going back-to-back
// from it (it ends within 90 min before this event), else home. Mirrors travel.js travelDecision so the
// inline fallback can't under-estimate a far-previous-venue leg (the residual the gate flagged). Returns
// null if neither origin is known (caller then can't compute → conservative event-start).
function originFor(ev, allEvents, home) {
  let prev = null;
  for (const e of allEvents || []) {
    if (!e || isHelperBlock(e.summary) || !(e.location || "").trim() || !Number.isFinite(e.endMs)) continue;
    if (e.endMs <= ev.startMs && (prev === null || e.endMs > prev.endMs)) prev = e; // latest real event before ev
  }
  const gap = prev ? ev.startMs - prev.endMs : Infinity;
  if (prev && gap >= 0 && gap <= 90 * 60000) return prev.location; // back-to-back → come from prev venue
  return home || null;
}

// Never-late departure resolver. If a [Travel] block exists → use it. If NOT (the travel loop runs only
// every 30 min, or the event was added late, or leaveMs was already past when travel ran), compute the
// leave time INLINE — from the back-to-back previous venue when there is one, else home — so a
// must-travel event still wakes before departure instead of (wrongly) anchoring to event start.
// Structured routing is authoritative when supplied. directionsFn is the legacy compatibility path
// only for callers that do not have routeFn; it must never issue a second provider call after routeFn.
async function resolveDeparture(ev, allEvents, {
  home, mapsKey, nowMs = Date.now(), bufferMin = 5, directionsFn, routeFn, uid, timezone, routeOptions,
} = {}) {
  const blockDep = departureMs(ev, allEvents);
  if (blockDep !== ev.startMs) return blockDep;           // a Travel block already pins door departure
  const origin = originFor(ev, allEvents, home);
  if (!origin || !ev.location) return ev.startMs;
  if (typeof routeFn === "function") {
    let route = null;
    try {
      const eventId = String(ev.id || `${ev.startMs}:${ev.summary || ""}`);
      route = await routeFn(origin, ev.location, mapsKey, ev.startMs, nowMs, false, {
        uid, timezone, eventId, purpose: "go", ...(routeOptions || {}),
      });
    } catch { route = null; }
    const exact = computeDoorDepartureMs(ev.startMs, route, { bufferMin });
    return exact === null ? ev.startMs : exact;
  }
  if (typeof directionsFn !== "function") return ev.startMs;
  let mins = null;
  try { mins = await directionsFn(origin, ev.location, mapsKey, ev.startMs, nowMs); } catch { mins = null; }
  if (mins == null) return ev.startMs;                    // provider unavailable → conservative event-start
  return ev.startMs - (mins + bufferMin) * 60000;         // compatibility fallback only
}

module.exports = { isHelperBlock, shouldWake, departureMs, resolveDeparture, originFor };
