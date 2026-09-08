"use strict";

const {
  readConnpassRegistrationStateOnPage,
  submitConnpassOnPage,
} = require("./connpass-browser-provider.js");
const {
  normalizeConnpassEventDetail,
  readCalendarBindings,
  readEventDetail,
} = require("./connpass-browser-discovery.js");
const { zonedSlotInstant } = require("./honne-ja-shadow-schedule.js");

const EVENT_REF = /^connpass-event:\/\/event\/[1-9][0-9]*$/;
const EVENT_URL = /^https:\/\/(?:[a-z0-9-]+\.)?connpass\.com\/event\/[1-9][0-9]*\/$/i;
const TIME_ZONE = "Asia/Tokyo";
const CONNPASS_ONLINE_LOCATION_PATTERN = /オンライン|online|リモート|remote|配信|視聴|zoom|google\s*meet/i;
const CONNPASS_LOCATION_PLACEHOLDER_PATTERN = /未定|未設定|調整中|後日|非公開|非表示|tbd|tba|n\/a/i;
const CONNPASS_GENERIC_VENUE_PATTERN = /^(?:会場|現地|オフライン|対面|venue|location)$/i;
// A wake must stay bounded: walk detail pages for at most this many
// earliest-dated bindings per wake, even when a busy window discovers
// hundreds of candidates. One constant, one place (Dais 2026-08-16).
const CONNPASS_DETAIL_WALK_BUDGET = 40;
// A wake must stay bounded: reconcile (readback -> evidence chain, never
// re-submit) at most this many already-registered-but-unbundled events per
// wake, even if a backlog of missed applied_bundle writes exists. Mirrors
// LUMA_RECONCILE_LIMIT in connector-luma-workflow.js (Dais 2026-08-17
// connector-core-recovery).
const CONNPASS_RECONCILE_LIMIT = 3;

function invalid() { throw new Error("Connpass script-first workflow invalid"); }
const DETAIL_FIELD_CODES = new Set([
  "CONNPASS_DETAIL_TITLE_INVALID_FAILED",
  "CONNPASS_DETAIL_START_INVALID_FAILED",
  "CONNPASS_DETAIL_END_INVALID_FAILED",
  "CONNPASS_DETAIL_RANGE_INVALID_FAILED",
]);

function stageError(code) {
  const error = new Error("Connpass discovery stage failed");
  error.code = code;
  return error;
}

function exactCandidate(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value)
    || value.provider !== "connpass"
    || !EVENT_REF.test(String(value.event_ref || ""))
    || !EVENT_URL.test(String(value.canonical_url || ""))
    || !String(value.title || "").trim()
    || !Number.isFinite(Date.parse(String(value.starts_at || "")))
    || !Number.isFinite(Date.parse(String(value.ends_at || "")))
    || Date.parse(value.starts_at) >= Date.parse(value.ends_at)
  ) invalid();
  return value;
}

function isInPersonCandidate(candidate) {
  const venueName = String(candidate.venue_name || "").trim();
  const venueAddress = String(candidate.venue_address || "").trim();
  const location = [venueName, venueAddress].filter(Boolean).join(" ");
  // Both fields must carry public, non-placeholder location data. A bare
  // venue label (or "未定") is not enough to establish an in-person event.
  return Boolean(venueName && venueAddress)
    && !CONNPASS_ONLINE_LOCATION_PATTERN.test(location)
    && !CONNPASS_LOCATION_PLACEHOLDER_PATTERN.test(location)
    && !CONNPASS_GENERIC_VENUE_PATTERN.test(venueName)
    && !CONNPASS_GENERIC_VENUE_PATTERN.test(venueAddress);
}

function candidateWindow(now) {
  const observed = now();
  if (!(observed instanceof Date) || !Number.isFinite(observed.getTime())) invalid();
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(observed).filter((part) => part.type !== "literal")
    .map((part) => [part.type, Number(part.value)]));
  const end = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + 28));
  return Object.freeze({
    start: Date.parse(zonedSlotInstant(parts, "00:00", TIME_ZONE)),
    end: Date.parse(zonedSlotInstant({
      year: end.getUTCFullYear(), month: end.getUTCMonth() + 1, day: end.getUTCDate(),
    }, "00:00", TIME_ZONE)),
  });
}

function discoveryDates(now) {
  const observed = now();
  if (!(observed instanceof Date) || !Number.isFinite(observed.getTime())) invalid();
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(observed).filter((part) => part.type !== "literal")
    .map((part) => [part.type, Number(part.value)]));
  const dates = [];
  for (let offset = 0; offset < 28; offset += 1) {
    const date = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + offset));
    dates.push(date.toISOString().slice(0, 10));
  }
  return Object.freeze(dates);
}

function createApiDiscovery({ now, apiClient }) {
  if (!apiClient || typeof apiClient.searchTokyoInventory !== "function") invalid();
  return async function discoverViaOfficialApi() {
    const ymd = discoveryDates(now).map((date) => date.replaceAll("-", ""));
    let events;
    try { events = await apiClient.searchTokyoInventory({ ymd }); }
    catch { throw stageError("CONNPASS_API_INVENTORY_FAILED"); }
    if (!Array.isArray(events) || events.length > 10_000) throw stageError("CONNPASS_API_RESULT_CONTRACT_FAILED");
    return Object.freeze(events.map((event) => {
      const id = Number(event && event.id);
      const accepted = Number(event && event.accepted);
      const limit = event && event.limit == null ? null : Number(event.limit);
      const capacityAvailable = limit == null || limit === 0 || (Number.isFinite(accepted) && accepted < limit);
      const registrationOpen = event && event.open_status === "preopen"
        && event.event_type === "participation";
      const participationSlotStatus = !registrationOpen ? "closed" : capacityAvailable ? "available" : "waitlist";
      return Object.freeze({
        provider: "connpass",
        event_ref: `connpass-event://event/${id}`,
        canonical_url: String(event && event.url || ""),
        title: String(event && event.title || "").trim(),
        description: String(event && event.description || "").replace(/<[^>]*>/g, " ")
          .replace(/\s+/g, " ").trim().slice(0, 8_000),
        starts_at: new Date(Date.parse(String(event && event.started_at || ""))).toISOString(),
        ends_at: new Date(Date.parse(String(event && event.ended_at || ""))).toISOString(),
        venue_name: String(event && event.place || "").trim(),
        venue_address: String(event && event.address || "").trim(),
        registration_status: registrationOpen && capacityAvailable ? "available" : "closed",
        participation_slot_status: participationSlotStatus,
        lightning_talk_status: "unknown",
        application_deadline_at: null,
        ticket_price_status: "free",
        ticket_price_minor: 0,
        participant_limit: Number.isSafeInteger(limit) ? limit : null,
        accepted_count: Number.isSafeInteger(accepted) ? accepted : null,
        waiting_count: Number.isSafeInteger(Number(event && event.waiting)) ? Number(event.waiting) : null,
        discovery_source: "official_api_v2",
      });
    }));
  };
}

function eventIdOf(eventRef) {
  return Number(String(eventRef).slice(String(eventRef).lastIndexOf("/") + 1));
}

function byCalendarDateThenEventId(a, b) {
  if (a.calendar_date !== b.calendar_date) return a.calendar_date < b.calendar_date ? -1 : 1;
  return eventIdOf(a.event_ref) - eventIdOf(b.event_ref);
}

function byStartsAtThenEventId(a, b) {
  const diff = Date.parse(a.starts_at) - Date.parse(b.starts_at);
  return diff !== 0 ? diff : eventIdOf(a.event_ref) - eventIdOf(b.event_ref);
}

// A busy window's earliest date alone can hold more than the budget (measured
// hundreds of Tokyo events in one bounded inventory), so taking the earliest N bindings only
// ever walks today/tomorrow and never learns whether later dates have any
// free+open seats. Round-robin across calendar dates (ascending) instead:
// one binding per date per round, so every date already-observed gets a
// bounded, deterministic share of the same total budget before a busier date
// is allowed a second pass.
function spreadAcrossDates(sortedBindings, budget) {
  const byDate = new Map();
  for (const binding of sortedBindings) {
    const bucket = byDate.get(binding.calendar_date);
    if (bucket) bucket.push(binding); else byDate.set(binding.calendar_date, [binding]);
  }
  const dates = [...byDate.keys()].sort();
  const cursors = new Array(dates.length).fill(0);
  const result = [];
  let progressed = true;
  while (result.length < budget && progressed) {
    progressed = false;
    for (let i = 0; i < dates.length && result.length < budget; i += 1) {
      const bucket = byDate.get(dates[i]);
      if (cursors[i] < bucket.length) {
        result.push(bucket[cursors[i]]);
        cursors[i] += 1;
        progressed = true;
      }
    }
  }
  return result;
}

function createDefaultDiscovery({ now, readBindings, readDetail }) {
  return async ({ page }) => {
    if (!page || typeof page.goto !== "function") throw stageError("CONNPASS_DISCOVERY_PAGE_CONTRACT_FAILED");
    let dates;
    try { dates = discoveryDates(now); }
    catch { throw stageError("CONNPASS_DISCOVERY_DATES_FAILED"); }
    const allowedDates = new Set(dates);
    const months = [...new Set(dates.map((date) => date.slice(0, 7).replace("-", "")))];
    if (months.length < 1 || months.length > 2) throw stageError("CONNPASS_DISCOVERY_MONTHS_CONTRACT_FAILED");
    const bindings = [];
    const seen = new Set();
    let discoveredCount = 0;
    for (const month of months) {
      try {
        await page.goto(`https://connpass.com/calendar/?ym=${month}&prefectures=13`, {
          waitUntil: "domcontentloaded", timeout: 30_000,
        });
      } catch { throw stageError("CONNPASS_CALENDAR_NAVIGATION_FAILED"); }
      let rows;
      try { rows = await readBindings(page); }
      catch { throw stageError("CONNPASS_CALENDAR_BINDINGS_FAILED"); }
      if (!Array.isArray(rows) || rows.length > 5_000) {
        throw stageError("CONNPASS_CALENDAR_ROWS_CONTRACT_FAILED");
      }
      const monthBindings = [];
      for (const row of rows) {
        const eventRef = String(row && row.event_ref || "");
        const url = String(row && row.canonical_url || "");
        const date = String(row && row.calendar_date || "");
        if (!allowedDates.has(date) || seen.has(eventRef)) continue;
        if (!EVENT_REF.test(eventRef) || !EVENT_URL.test(url)) {
          throw stageError("CONNPASS_CALENDAR_BINDING_VALIDATION_FAILED");
        }
        seen.add(eventRef);
        discoveredCount += 1;
        monthBindings.push(Object.freeze({ event_ref: eventRef, canonical_url: url, calendar_date: date }));
      }
      // Bound accumulation per month, spread across the month's calendar
      // dates, BEFORE it ever reaches the 500-binding hard guard below — a
      // legitimate busy month with hundreds of Tokyo events
      // must never trip it, and a busy single date must never crowd out
      // every other date in the month's own budget share.
      monthBindings.sort(byCalendarDateThenEventId);
      for (const binding of spreadAcrossDates(monthBindings, CONNPASS_DETAIL_WALK_BUDGET)) {
        bindings.push(binding);
        if (bindings.length > 500) {
          throw stageError("CONNPASS_CALENDAR_ROWS_CONTRACT_FAILED");
        }
      }
    }
    // Each month's contribution is already spread across its own dates, so
    // combine both months and spread once more across the full window
    // (dates ascending) to apply the wake-level budget without letting one
    // month's dates crowd out the other's.
    const walkList = spreadAcrossDates(bindings.slice().sort(byCalendarDateThenEventId), CONNPASS_DETAIL_WALK_BUDGET);
    const result = [];
    for (const binding of walkList) {
      try {
        await page.goto(binding.canonical_url, { waitUntil: "domcontentloaded", timeout: 30_000 });
      } catch { throw stageError("CONNPASS_DETAIL_NAVIGATION_FAILED"); }
      let detail;
      try { detail = normalizeConnpassEventDetail(await readDetail(page)); }
      catch (error) {
        const code = String(error && error.code || "");
        throw stageError(DETAIL_FIELD_CODES.has(code) ? code : "CONNPASS_DETAIL_READ_FAILED");
      }
      if (detail.event_ref !== binding.event_ref) {
        throw stageError("CONNPASS_DETAIL_IDENTITY_MISMATCH_FAILED");
      }
      result.push(detail);
    }
    // The walk itself is now spread across dates (see spreadAcrossDates
    // above) so later dates get visited too, but candidate SELECTION
    // priority is unchanged: sort back to earliest-first so a downstream
    // caller still tries the soonest eligible event first.
    result.sort(byStartsAtThenEventId);
    // observed_count in the audit must reflect what the window really
    // listed, not the walked/truncated count — carry the true total on the
    // array itself so discoverCandidates can report it honestly.
    result.discoveredCount = discoveredCount;
    return Object.freeze(result);
  };
}

function defaultCalendarFree(candidate, calendar) {
  const intervals = Array.isArray(calendar) ? calendar
    : (calendar && Array.isArray(calendar.busy_intervals) ? calendar.busy_intervals : []);
  const start = Date.parse(candidate.starts_at);
  const end = Date.parse(candidate.ends_at);
  return !intervals.some((busy) => busy && busy.kind === "timed"
    && start < Date.parse(busy.end_at) && end > Date.parse(busy.start_at));
}

function normalizedState(value) {
  const state = String(value && (value.status || value.state) || "");
  if (["registered", "pending"].includes(state)) return Object.freeze({ status: state });
  if (state === "absent") return Object.freeze({ status: "absent" });
  return Object.freeze({ status: "unavailable" });
}

function createConnpassScriptFirstWorkflow(options = {}) {
  const now = options.now || (() => new Date());
  const readBindings = options.readCalendarBindings || readCalendarBindings;
  const readDetail = options.readEventDetail || readEventDetail;
  const discoverOnPage = options.discoverOnPage || (options.connpassApiClient
    ? createApiDiscovery({ now, apiClient: options.connpassApiClient })
    : createDefaultDiscovery({ now, readBindings, readDetail }));
  const isCalendarFree = options.isCalendarFree || defaultCalendarFree;
  const submitOnPage = options.submitOnPage || submitConnpassOnPage;
  const readStateOnPage = options.readStateOnPage || readConnpassRegistrationStateOnPage;
  const onDiscoveryAudit = options.onDiscoveryAudit || (() => {});
  // Fail closed: until production wiring proves an event has no bundle,
  // treat it as bundled so a mis-wired caller can never re-surface it.
  // Mirrors createLumaScriptFirstWorkflow's hasAppliedBundle default.
  const hasAppliedBundle = options.hasAppliedBundle || (() => true);
  // Same shape as createPeatixDiscoveryWorkflow's readAttendeeProfile: an
  // optional injector for the attendee's own name, resolved upstream (see
  // connector-minimal-production.js) and threaded down to submitOnPage so
  // connpass-browser-provider.js never reads process.env itself. Absent
  // (undefined) is valid — the provider then fails closed on any required
  // name-shaped questionnaire field instead of guessing.
  const readAttendeeName = options.readAttendeeName;
  const allowAutomatedSubmit = options.allowAutomatedSubmit === true;
  if ([now, readBindings, readDetail, discoverOnPage, isCalendarFree, submitOnPage, readStateOnPage,
    onDiscoveryAudit, hasAppliedBundle]
    .some((value) => typeof value !== "function")
    || (readAttendeeName != null && typeof readAttendeeName !== "function")) {
    throw stageError("CONNPASS_WORKFLOW_DEPENDENCIES_INVALID_FAILED");
  }

  return Object.freeze({
    async discoverCandidates({ page, calendar }) {
      const observed = await discoverOnPage({ page });
      if (!Array.isArray(observed) || observed.length > 500) {
        throw stageError("CONNPASS_DISCOVERY_RESULT_CONTRACT_FAILED");
      }
      let window;
      try { window = candidateWindow(now); }
      catch { throw stageError("CONNPASS_CANDIDATE_WINDOW_FAILED"); }
      // Registrations the provider already reports but that never produced
      // an applied_bundle (evidence chain failed mid-way on a prior wake).
      // These skip the direct-registration path entirely downstream — the
      // runner's existing pre-submit readback sees "registered" and jumps
      // straight to completeEvidence(). Bounded so a backlog can't make a
      // wake unbounded; see CONNPASS_RECONCILE_LIMIT.
      const registeredExisting = [];
      const result = [];
      let normalizedCount = 0;
      let windowCount = 0;
      let freeOpenCount = 0;
      for (const raw of observed) {
        let candidate;
        try { candidate = exactCandidate(raw); }
        catch { throw stageError("CONNPASS_CANDIDATE_VALIDATION_FAILED"); }
        normalizedCount += 1;
        const startsAt = Date.parse(candidate.starts_at);
        if (startsAt < window.start || startsAt >= window.end) continue;
        windowCount += 1;
        if (candidate.registration_status === "registered") {
          if (registeredExisting.length >= CONNPASS_RECONCILE_LIMIT) continue;
          if (await hasAppliedBundle(candidate)) continue;
          registeredExisting.push(Object.freeze({ ...candidate }));
          continue;
        }
        if (candidate.registration_status !== "available") continue;
        if (candidate.ticket_price_status !== "free" || candidate.ticket_price_minor !== 0) continue;
        if (!isInPersonCandidate(candidate)) continue;
        freeOpenCount += 1;
        let calendarFree;
        try { calendarFree = await isCalendarFree(candidate, calendar); }
        catch { throw stageError("CONNPASS_CALENDAR_CONFLICT_CHECK_FAILED"); }
        if (!calendarFree) continue;
        result.push(Object.freeze({ ...candidate }));
      }
      try {
        await onDiscoveryAudit(Object.freeze({
          observed_count: typeof observed.discoveredCount === "number" ? observed.discoveredCount : observed.length,
          normalized_count: normalizedCount,
          window_count: windowCount,
          free_open_count: freeOpenCount,
          calendar_free_count: registeredExisting.length + result.length,
        }));
      } catch { throw stageError("CONNPASS_DISCOVERY_AUDIT_FAILED"); }
      return Object.freeze([...registeredExisting, ...result]);
    },
    async runDirectAction({ page, candidate }) {
      const selected = exactCandidate(candidate);
      if (!allowAutomatedSubmit) {
        return Object.freeze({ status: "failed", safe_reason: "connpass_action_permission_required" });
      }
      const attendeeName = typeof readAttendeeName === "function" ? await readAttendeeName() : undefined;
      let outcome;
      try {
        outcome = await submitOnPage(page, selected, { attendeeName });
      } catch (error) {
        // connpass-browser-provider.js's submitConnpassOnPage throws this one
        // code (rather than returning a status, unlike every other guard on
        // that path) when the join control is a login wall. Caught here —
        // instead of propagating like every other submit error still does —
        // so the runner's own safe_reason (not just the action-history row)
        // names the session problem instead of falling back to the generic
        // direct_action_failed every other thrown connpass error still gets.
        if (String(error && error.code || "") === "CONNPASS_SESSION_EXPIRED") {
          return Object.freeze({ status: "failed", safe_reason: "connpass_session_expired" });
        }
        throw error;
      }
      let currentUrl = "";
      if (outcome && ["registered", "pending"].includes(outcome.status)) {
        try {
          currentUrl = page && typeof page.url === "function" ? String(page.url()) : "";
        } catch { currentUrl = ""; }
      }
      return outcome && ["registered", "pending"].includes(outcome.status)
        && currentUrl === selected.canonical_url
        ? Object.freeze({ status: "completed", method: "connpass_direct_submit" })
        : Object.freeze({ status: "failed", safe_reason: "direct_action_unverified" });
    },
    async readProviderState({ page, candidate }) {
      exactCandidate(candidate);
      return normalizedState(await readStateOnPage(page));
    },
  });
}

module.exports = { createConnpassScriptFirstWorkflow };
