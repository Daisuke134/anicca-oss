"use strict";

const { createHash } = require("node:crypto");
const { zonedSlotInstant } = require("./honne-ja-shadow-schedule.js");
const { submitPeatixOnPage, readPeatixRegistrationStateOnPage } = require("./peatix-browser-provider.js");

const SEARCH_URL_BASE = "https://peatix.com/search?q=%E7%84%A1%E6%96%99&country=JP&l.text=Tokyo";
const SEARCH_RESPONSE_TIMEOUT_MS = 10_000;
const SEARCH_PAGE_LIMIT = 5;
const SEARCH_PAGE_SIZE = 20;
const TIME_ZONE = "Asia/Tokyo";
const EVENT_ID = /^[1-9][0-9]*$/;
const EVENT_PATH = /^\/event\/([1-9][0-9]*)\/?$/;
const EVENT_URL = /^https:\/\/peatix\.com\/event\/([1-9][0-9]*)\/?$/i;
// A wake must stay bounded: reconcile (readback -> evidence chain, never
// re-submit) at most this many already-registered-but-unbundled events per
// wake, even if a backlog of missed applied_bundle writes exists. Mirrors
// LUMA_RECONCILE_LIMIT / CONNPASS_RECONCILE_LIMIT (Dais 2026-08-18
// connector-core-recovery).
const PEATIX_RECONCILE_LIMIT = 3;
const SAFE_CODES = new Set([
  "PEATIX_SEARCH_NAVIGATION_FAILED",
  "PEATIX_SEARCH_READ_FAILED",
  "PEATIX_SEARCH_ROWS_CONTRACT_FAILED",
  "PEATIX_DETAIL_NAVIGATION_FAILED",
  "PEATIX_DETAIL_READ_FAILED",
  "PEATIX_DETAIL_IDENTITY_MISMATCH_FAILED",
  "PEATIX_CANDIDATE_VALIDATION_FAILED",
  "PEATIX_CALENDAR_CONFLICT_CHECK_FAILED",
]);
const DIRECT_REASONS = new Set("invalid_input readback_unavailable tickets_navigation_failed ticket_control_unavailable next_control_unavailable form_navigation_failed unknown_required_field required_field_unavailable privacy_control_unavailable form_submit_unavailable confirm_event_mismatch confirm_navigation_failed confirm_control_unavailable kana_control_unavailable confirm_validation_failed browser_action_failed session_expired".split(" "));

function invalid() {
  throw new Error("Peatix discovery workflow invalid");
}

function stageError(code) {
  const error = new Error("Peatix discovery stage failed");
  error.code = code;
  return error;
}

function preserveSafe(error, fallback) {
  const code = String(error && error.code || "");
  return stageError(SAFE_CODES.has(code) ? code : fallback);
}

function directSafeReason(outcome) {
  const reason = outcome && typeof outcome.reason === "string" ? outcome.reason : "";
  return DIRECT_REASONS.has(reason) ? `peatix_${reason}` : "direct_action_unverified";
}

function eventId(value) {
  const id = String(value == null ? "" : value);
  return EVENT_ID.test(id) ? id : null;
}

function eventUrlId(value) {
  let url;
  try { url = new URL(String(value == null ? "" : value).trim()); } catch { return null; }
  if (url.protocol !== "https:" || url.hostname.toLowerCase() !== "peatix.com"
    || url.username || url.password) return null;
  const match = EVENT_PATH.exec(url.pathname);
  return match ? match[1] : null;
}

function canonicalBinding(value) {
  const row = typeof value === "string" ? { canonical_url: value } : value;
  if (!row || typeof row !== "object" || Array.isArray(row)) return null;
  const rawUrl = String(row.canonical_url || row.href || row.url || "");
  const id = eventUrlId(rawUrl);
  if (!id) return null;
  const expectedRef = `peatix-event://event/${id}`;
  if (row.event_ref != null && String(row.event_ref) !== expectedRef) return null;
  return Object.freeze({
    event_ref: expectedRef,
    canonical_url: `https://peatix.com/event/${id}`,
  });
}

function localParts(value) {
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) invalid();
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value).filter((part) => part.type !== "literal")
    .map((part) => [part.type, Number(part.value)]));
  if (![parts.year, parts.month, parts.day].every(Number.isInteger)) invalid();
  return parts;
}

function dateString(parts) {
  return [parts.year, parts.month, parts.day]
    .map((part) => String(part).padStart(2, "0")).join("-");
}

function discoveryDateRange(observed) {
  const parts = localParts(observed);
  const end = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + 13));
  return Object.freeze({
    from: dateString(parts),
    to: dateString({
      year: end.getUTCFullYear(),
      month: end.getUTCMonth() + 1,
      day: end.getUTCDate(),
    }),
  });
}

function searchUrl(observed, pageNumber) {
  const range = discoveryDateRange(observed);
  return `${SEARCH_URL_BASE}&dr=${range.from}:${range.to}&p=${pageNumber}`;
}

function matchingSearchResponse(response, pageNumber) {
  try {
    const url = new URL(String(response && typeof response.url === "function" ? response.url() : ""));
    const params = url.searchParams;
    return url.protocol === "https:"
      && url.hostname.toLowerCase() === "peatix.com"
      && url.pathname === "/search/events"
      && params.getAll("p").length === 1
      && params.get("p") === String(pageNumber)
      && params.getAll("size").length === 1
      && params.get("size") === String(SEARCH_PAGE_SIZE);
  } catch {
    return false;
  }
}

async function readSearchResponseRows(response, pageNumber) {
  if (!response || typeof response.ok !== "function" || !response.ok()
    || typeof response.json !== "function") {
    throw stageError("PEATIX_SEARCH_READ_FAILED");
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw stageError("PEATIX_SEARCH_READ_FAILED");
  }
  const jsonData = payload && typeof payload === "object" && !Array.isArray(payload)
    ? payload.json_data : null;
  const events = jsonData && typeof jsonData === "object" && !Array.isArray(jsonData)
    ? jsonData.events : null;
  if (!Number.isInteger(jsonData && jsonData.page) || jsonData.page !== pageNumber
    || !Array.isArray(events) || events.length > SEARCH_PAGE_SIZE) {
    throw stageError("PEATIX_SEARCH_ROWS_CONTRACT_FAILED");
  }
  return events.map((event) => {
    if (!event || typeof event !== "object" || Array.isArray(event)
      || !Number.isSafeInteger(event.id) || event.id <= 0) {
      throw stageError("PEATIX_SEARCH_ROWS_CONTRACT_FAILED");
    }
    return Object.freeze({
      event_ref: `peatix-event://event/${event.id}`,
      canonical_url: `https://peatix.com/event/${event.id}`,
    });
  });
}

function candidateWindow(observed) {
  const parts = localParts(observed);
  const end = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + 14));
  return Object.freeze({
    start: Date.parse(zonedSlotInstant(parts, "00:00", TIME_ZONE)),
    end: Date.parse(zonedSlotInstant({
      year: end.getUTCFullYear(),
      month: end.getUTCMonth() + 1,
      day: end.getUTCDate(),
    }, "00:00", TIME_ZONE)),
  });
}

function parseTokyoDate(value) {
  const raw = String(value == null ? "" : value).trim();
  const wall = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/
    .exec(raw);
  if (wall) {
    const [, year, month, day, hour, minute, seconds = "0", fraction = "0"] = wall;
    if (Number(seconds) > 59) return null;
    try {
      const base = Date.parse(zonedSlotInstant({
        year: Number(year), month: Number(month), day: Number(day),
      }, `${hour}:${minute}`, TIME_ZONE));
      const milliseconds = Number((fraction + "000").slice(0, 3));
      return new Date(base + Number(seconds) * 1000 + milliseconds).toISOString();
    } catch {
      return null;
    }
  }
  if (!/(?:Z|[+-][0-9]{2}:[0-9]{2})$/i.test(raw)) return null;
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : null;
}

function exactCandidate(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value)
    || value.provider !== "peatix"
    || !/^peatix-event:\/\/event\/[1-9][0-9]*$/.test(String(value.event_ref || ""))
    || !EVENT_URL.test(String(value.canonical_url || ""))
    || !String(value.title || "").trim()
    || !["available", "closed"].includes(value.registration_status)
    || !["free", "paid"].includes(value.ticket_price_status)
    || !Number.isInteger(value.ticket_price_minor) || value.ticket_price_minor < 0
    || !Number.isFinite(Date.parse(String(value.starts_at || "")))
    || !Number.isFinite(Date.parse(String(value.ends_at || "")))
    || Date.parse(value.starts_at) >= Date.parse(value.ends_at)
    || (value.registration_status === "available" && !EVENT_ID.test(String(value.ticket_id || "")))
  ) invalid();
  return value;
}

function actionCandidate(value) {
  exactCandidate(value);
  const ref = /^peatix-event:\/\/event\/([1-9][0-9]*)$/.exec(String(value.event_ref || ""));
  let url; try { url = new URL(String(value.canonical_url || "")); } catch { invalid(); }
  const path = /^\/event\/([1-9][0-9]*)\/?$/.exec(url.pathname);
  if (value.provider !== "peatix" || value.registration_status !== "available"
    || value.ticket_price_status !== "free" || value.ticket_price_minor !== 0
    || typeof value.ticket_id !== "string" || !EVENT_ID.test(value.ticket_id)
    || !ref || !path || ref[1] !== path[1] || url.protocol !== "https:"
    || url.hostname !== "peatix.com" || url.port || url.username || url.password || url.search || url.hash) invalid();
  return value;
}

function numericMinor(tickets) {
  const prices = tickets
    .map((ticket) => ticket && ticket.price)
    .filter((price) => Number.isInteger(price) && price >= 0);
  return prices.length ? Math.min(...prices) : 0;
}

function usableFreeTicket(ticket, nowMs) {
  if (!ticket || typeof ticket !== "object" || Array.isArray(ticket)) return false;
  if (ticket.price !== 0 || ticket.status !== 10 || !EVENT_ID.test(String(ticket.id || ""))) return false;
  if (!Number.isInteger(ticket.seatsAvailable) || ticket.seatsAvailable <= 0) return false;
  const deadline = ticket.salesEnds && ticket.salesEnds.datetime;
  if (deadline == null || String(deadline).trim() === "") return true;
  const deadlineIso = parseTokyoDate(deadline);
  return deadlineIso != null && Date.parse(deadlineIso) > nowMs;
}

function normalizeDetail(binding, raw, nowMs) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) invalid();
  const jsonData = raw.json_data;
  const event = jsonData && jsonData.event;
  if (!event || typeof event !== "object" || Array.isArray(event)) invalid();
  const detailId = eventId(event.id);
  if (!detailId) invalid();
  const bindingId = binding.event_ref.split("/").pop();
  if (detailId !== bindingId) throw stageError("PEATIX_DETAIL_IDENTITY_MISMATCH_FAILED");
  const startsAt = parseTokyoDate(event.datetime);
  const endsAt = parseTokyoDate(event.datetimeEnd);
  if (!startsAt || !endsAt || Date.parse(startsAt) >= Date.parse(endsAt)) invalid();
  const tickets = event.tickets;
  if (!Array.isArray(tickets)) invalid();
  const title = String(event.name || "").trim();
  if (!title) invalid();
  const freeObserved = tickets.some((ticket) => ticket && ticket.price === 0);
  const freeTicket = tickets.find((ticket) => usableFreeTicket(ticket, nowMs));
  const freeOpen = event.status === "OPEN"
    && event.isOpen === true
    && event.isFinished !== true
    && Boolean(freeTicket);
  const candidate = Object.freeze({
    provider: "peatix",
    event_ref: binding.event_ref,
    canonical_url: binding.canonical_url,
    title,
    starts_at: startsAt,
    ends_at: endsAt,
    registration_status: freeOpen ? "available" : "closed",
    ticket_price_status: freeObserved ? "free" : "paid",
    ticket_price_minor: freeObserved ? 0 : numericMinor(tickets),
    ...(freeOpen ? { ticket_id: String(freeTicket.id) } : {}),
  });
  exactCandidate(candidate);
  return Object.freeze({ candidate, free_open: freeOpen });
}

function calendarIntervals(calendar) { return Array.isArray(calendar) ? calendar : (calendar && Array.isArray(calendar.busy_intervals) ? calendar.busy_intervals : []); }

function overlapsTimedCalendarInterval(candidate, busy) {
  if (!busy || busy.kind !== "timed") return false;
  const start = Date.parse(candidate.starts_at); const end = Date.parse(candidate.ends_at);
  const busyStart = Date.parse(busy.start_at); const busyEnd = Date.parse(busy.end_at);
  return [start, end, busyStart, busyEnd].every(Number.isFinite)
    && start < busyEnd && end > busyStart;
}

function exactCalendarCoverage(candidate, busy) {
  if (!overlapsTimedCalendarInterval(candidate, busy)) return false;
  const connectorIdempotency = createHash("sha256").update(String(candidate.canonical_url), "utf8").digest("hex");
  return busy.connector_idempotency === connectorIdempotency;
}

function defaultCalendarFree(candidate, calendar) {
  return !calendarIntervals(calendar).some((busy) => (
    overlapsTimedCalendarInterval(candidate, busy) && !exactCalendarCoverage(candidate, busy)
  ));
}

async function defaultReadSearchBindings(page, observed, bindingLimit = SEARCH_PAGE_LIMIT * SEARCH_PAGE_SIZE) {
  if (!page || typeof page.goto !== "function" || typeof page.waitForResponse !== "function") {
    invalid();
  }
  if (!Number.isInteger(bindingLimit) || bindingLimit < 1
    || bindingLimit > SEARCH_PAGE_LIMIT * SEARCH_PAGE_SIZE) invalid();
  async function readPage(pageNumber) {
    let responsePromise;
    try {
      responsePromise = page.waitForResponse(
        (response) => matchingSearchResponse(response, pageNumber),
        { timeout: SEARCH_RESPONSE_TIMEOUT_MS },
      );
    } catch {
      throw stageError("PEATIX_SEARCH_READ_FAILED");
    }
    if (responsePromise && typeof responsePromise.catch === "function") responsePromise.catch(() => {});
    try {
      await page.goto(searchUrl(observed, pageNumber), {
        waitUntil: "domcontentloaded", timeout: 30_000,
      });
    } catch {
      throw stageError("PEATIX_SEARCH_NAVIGATION_FAILED");
    }
    let response;
    try {
      response = await responsePromise;
    } catch {
      throw stageError("PEATIX_SEARCH_READ_FAILED");
    }
    try {
      return await readSearchResponseRows(response, pageNumber);
    } catch (error) {
      if (String(error && error.code || "") === "PEATIX_SEARCH_ROWS_CONTRACT_FAILED") throw error;
      throw stageError("PEATIX_SEARCH_READ_FAILED");
    }
  }
  const bindings = [];
  const seen = new Set();
  for (let pageNumber = 1; pageNumber <= SEARCH_PAGE_LIMIT; pageNumber += 1) {
    let rows;
    for (let attempt = 0; attempt < (pageNumber === 1 ? 2 : 1); attempt += 1) {
      try {
        rows = await readPage(pageNumber);
        break;
      } catch (error) {
        const code = String(error && error.code || "");
        const retryable = pageNumber === 1 && attempt === 0
          && ["PEATIX_SEARCH_NAVIGATION_FAILED", "PEATIX_SEARCH_READ_FAILED"].includes(code);
        if (!retryable) throw error;
      }
    }
    for (const row of rows) {
      if (seen.has(row.event_ref)) continue;
      seen.add(row.event_ref);
      bindings.push(row);
      if (bindings.length >= bindingLimit) break;
    }
    if (bindings.length >= bindingLimit) break;
    if (rows.length < SEARCH_PAGE_SIZE) break;
  }
  return Object.freeze(bindings);
}

async function defaultReadEventViewData(page, canonicalUrl) {
  if (!page || typeof page.goto !== "function" || typeof page.evaluate !== "function") invalid();
  try {
    await page.goto(canonicalUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  } catch {
    throw stageError("PEATIX_DETAIL_NAVIGATION_FAILED");
  }
  try {
    return await page.evaluate(async (url) => {
      const response = await fetch(`${url}/get_view_data`, { credentials: "same-origin" });
      if (!response.ok) throw new Error("Peatix event JSON response failed");
      return response.json();
    }, canonicalUrl);
  } catch {
    throw stageError("PEATIX_DETAIL_READ_FAILED");
  }
}

function normalizedProviderState(value) {
  const status = String(value && value.status || "");
  return Object.freeze({ status: ["registered", "absent"].includes(status) ? status : "unavailable" });
}

function createPeatixDiscoveryWorkflow(options = {}) {
  const now = options.now || (() => new Date());
  const readSearchBindings = options.readSearchBindings || defaultReadSearchBindings;
  const readEventViewData = options.readEventViewData || defaultReadEventViewData;
  const isCalendarFree = options.isCalendarFree || defaultCalendarFree;
  const onDiscoveryAudit = options.onDiscoveryAudit || (() => {});
  const submitOnPage = options.submitOnPage || submitPeatixOnPage;
  const readStateOnPage = options.readStateOnPage || readPeatixRegistrationStateOnPage;
  const readAttendeeProfile = options.readAttendeeProfile;
  // Fail closed: until production wiring proves an event has no bundle,
  // treat it as bundled so a mis-wired caller can never re-surface it.
  // Mirrors createLumaScriptFirstWorkflow's / createConnpassScriptFirst
  // Workflow's hasAppliedBundle default.
  const hasAppliedBundle = options.hasAppliedBundle || (() => true);
  const searchBindingLimit = options.searchBindingLimit == null
    ? SEARCH_PAGE_LIMIT * SEARCH_PAGE_SIZE : Number(options.searchBindingLimit);
  if ([now, readSearchBindings, readEventViewData, isCalendarFree, onDiscoveryAudit]
    .some((value) => typeof value !== "function") || typeof submitOnPage !== "function"
    || typeof readStateOnPage !== "function" || typeof hasAppliedBundle !== "function"
    || (readAttendeeProfile != null && typeof readAttendeeProfile !== "function")
    || !Number.isInteger(searchBindingLimit) || searchBindingLimit < 1
    || searchBindingLimit > SEARCH_PAGE_LIMIT * SEARCH_PAGE_SIZE) invalid();

  return Object.freeze({
    async runDirectAction({ page, candidate }) {
      const selected = actionCandidate(candidate);
      try {
        const profile = typeof readAttendeeProfile === "function" ? await readAttendeeProfile() : null;
        const outcome = await submitOnPage(page, selected, profile);
        return outcome && outcome.status === "registered"
          ? Object.freeze({ status: "completed", method: "peatix_direct_submit" })
          : Object.freeze({ status: "failed", safe_reason: directSafeReason(outcome) });
      } catch {
        return Object.freeze({ status: "failed", safe_reason: "direct_action_failed" });
      }
    },
    async readProviderState({ page, candidate }) {
      const selected = actionCandidate(candidate);
      try { return normalizedProviderState(await readStateOnPage(page, selected)); }
      catch { return Object.freeze({ status: "unavailable" }); }
    },
    async discoverCandidates({ page, calendar }) {
      if (!page || typeof page !== "object" || Array.isArray(page)) {
        throw stageError("PEATIX_PAGE_VALIDATION_FAILED");
      }
      const observed = now();
      let rows;
      try {
        rows = await readSearchBindings(page, observed, searchBindingLimit);
      } catch (error) {
        throw preserveSafe(error, "PEATIX_SEARCH_READ_FAILED");
      }
      if (!Array.isArray(rows)) {
        throw stageError("PEATIX_SEARCH_ROWS_CONTRACT_FAILED");
      }
      const bindings = [];
      const seen = new Set();
      for (const row of rows) {
        const binding = canonicalBinding(row);
        if (!binding || seen.has(binding.event_ref)) continue;
        seen.add(binding.event_ref);
        bindings.push(binding);
      }
      if (bindings.length > 100) {
        throw stageError("PEATIX_SEARCH_ROWS_CONTRACT_FAILED");
      }

      const window = candidateWindow(observed);
      const nowMs = observed.getTime();
      const exactCovered = [];
      const unprocessed = [];
      // Registrations Peatix already reports (a ticket link on the event
      // page, the same signal readProviderState/readStateOnPage uses) but
      // that never produced an applied_bundle (evidence chain failed
      // mid-way on a prior wake). Unlike Luma/Connpass, Peatix's own
      // free/open ticket availability never encodes the viewer's personal
      // registration state -- a still-open event that Dais already holds a
      // ticket for stays "available" and reaches the runner's own
      // pre-submit readback safety net unaided. The one place that safety
      // net is unreachable is a calendar conflict: an unrelated busy
      // interval would otherwise drop an already-registered candidate
      // before it ever reaches the runner. So this only checks the
      // registration signal right there, on the same page readEventViewData
      // already navigated to, before honoring that conflict. Bounded so a
      // backlog can't make a wake unbounded; see PEATIX_RECONCILE_LIMIT.
      const reconcile = [];
      let normalizedCount = 0;
      let windowCount = 0;
      let freeOpenCount = 0;
      for (const binding of bindings) {
        let raw;
        try {
          raw = await readEventViewData(page, binding.canonical_url);
        } catch (error) {
          throw preserveSafe(error, "PEATIX_DETAIL_READ_FAILED");
        }
        let normalized;
        try {
          normalized = normalizeDetail(binding, raw, nowMs);
        } catch (error) {
          if (String(error && error.code || "") === "PEATIX_DETAIL_IDENTITY_MISMATCH_FAILED") {
            throw error;
          }
          throw stageError("PEATIX_CANDIDATE_VALIDATION_FAILED");
        }
        normalizedCount += 1;
        const startsAt = Date.parse(normalized.candidate.starts_at);
        if (startsAt < window.start || startsAt >= window.end) continue;
        windowCount += 1;
        if (!normalized.free_open) continue;
        freeOpenCount += 1;
        let calendarFree;
        try {
          calendarFree = await isCalendarFree(normalized.candidate, calendar);
        } catch {
          throw stageError("PEATIX_CALENDAR_CONFLICT_CHECK_FAILED");
        }
        if (!calendarFree) {
          let registeredState;
          try {
            registeredState = await readStateOnPage(page, normalized.candidate);
          } catch {
            registeredState = null;
          }
          if (registeredState && registeredState.status === "registered"
            && reconcile.length < PEATIX_RECONCILE_LIMIT
            && !await hasAppliedBundle(normalized.candidate)) {
            reconcile.push(Object.freeze({ ...normalized.candidate }));
          }
          continue;
        }
        const candidate = Object.freeze({ ...normalized.candidate });
        const isExactCovered = calendarIntervals(calendar)
          .some((busy) => exactCalendarCoverage(candidate, busy));
        (isExactCovered ? exactCovered : unprocessed).push(candidate);
      }
      const eligible = [...exactCovered, ...unprocessed];
      const audit = Object.freeze({
        observed_count: bindings.length,
        normalized_count: normalizedCount,
        window_count: windowCount,
        free_open_count: freeOpenCount,
        calendar_free_count: reconcile.length + eligible.length,
      });
      await onDiscoveryAudit(audit);
      return Object.freeze([...reconcile, ...eligible]);
    },
  });
}

module.exports = { createPeatixDiscoveryWorkflow };
