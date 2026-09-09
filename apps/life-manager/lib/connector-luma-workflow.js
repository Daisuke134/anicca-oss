"use strict";

const {
  advanceLumaTimeline,
  collectLumaInventory,
  readLumaTimelineSnapshot,
} = require("./luma-discovery.js");
const {
  normalizeLumaEventDetail,
  readRawLumaEventDetail,
} = require("./luma-event-detail.js");
const { submitLumaOnPage } = require("./luma-browser-provider.js");
const { zonedSlotInstant } = require("./honne-ja-shadow-schedule.js");

const TOKYO_DISCOVER_URL = "https://luma.com/tokyo?k=p";
const PRODUCTION_TIME_ZONE = "Asia/Tokyo";
const EVENT_REF = /^luma-event:\/\/event\/[A-Za-z0-9_-]+$/;
const CANONICAL_URL = /^https:\/\/luma\.com\/[A-Za-z0-9_-]+$/;
const LUMA_DETAIL_WALK_LIMIT = 6;
const LUMA_DETAIL_ROTATION_MS = 60_000;
const LUMA_DETAIL_NAVIGATION_TIMEOUT_MS = 15_000;
const DIRECT_ACTION_FAILURE_REASONS = new Map([
  ["LUMA_REQUIRED_PROFILE_FIELD_UNAVAILABLE", "luma_required_profile_field_unavailable"],
  ["LUMA_FORM_PROFILE_UNAVAILABLE", "luma_form_profile_unavailable"],
  ["LUMA_FORM_SCHEMA_UNAVAILABLE", "luma_form_schema_unavailable"],
  ["LUMA_FORM_PLAN_UNAVAILABLE", "luma_form_plan_unavailable"],
  ["LUMA_FORM_FILL_UNAVAILABLE", "luma_form_fill_unavailable"],
  ["LUMA_PAGE_UNAVAILABLE", "luma_page_unavailable"],
  ["LUMA_CONTROL_UNAVAILABLE", "luma_control_unavailable"],
  ["LUMA_CONFIRM_UNAVAILABLE", "luma_confirm_unavailable"],
  ["LUMA_BROWSER_ACTION_FAILED", "luma_browser_action_failed"],
]);
// A wake must stay bounded: reconcile (readback -> evidence chain, never
// re-submit) at most this many already-registered-but-unbundled events per
// wake, even if a backlog of missed applied_bundle writes exists. One
// constant, one place (Dais 2026-08-17 connector-core-recovery).
const LUMA_RECONCILE_LIMIT = 3;

function invalid() {
  throw new Error("Luma script-first workflow invalid");
}

function candidateWindow(now) {
  const observed = now();
  if (!(observed instanceof Date) || !Number.isFinite(observed.getTime())) invalid();
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: PRODUCTION_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(observed).filter((part) => part.type !== "literal")
    .map((part) => [part.type, Number(part.value)]));
  if (![parts.year, parts.month, parts.day].every(Number.isInteger)) invalid();
  const end = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + 28));
  return Object.freeze({
    start: Date.parse(zonedSlotInstant(parts, "00:00", PRODUCTION_TIME_ZONE)),
    end: Date.parse(zonedSlotInstant({
      year: end.getUTCFullYear(),
      month: end.getUTCMonth() + 1,
      day: end.getUTCDate(),
    }, "00:00", PRODUCTION_TIME_ZONE)),
  });
}

function exactEvent(input) {
  if (
    !input || typeof input !== "object" || Array.isArray(input)
    || input.provider !== "luma"
    || !EVENT_REF.test(String(input.event_ref || ""))
    || !CANONICAL_URL.test(String(input.canonical_url || ""))
    || !Number.isFinite(Date.parse(String(input.starts_at || "")))
    || !Number.isFinite(Date.parse(String(input.ends_at || "")))
    || Date.parse(input.starts_at) >= Date.parse(input.ends_at)
  ) invalid();
  return input;
}

function isFreeOpen(candidate) {
  return candidate.attendance_mode === "in_person"
    && candidate.event_status === "scheduled"
    && ["available", "approval_required"].includes(candidate.rsvp_status)
    && candidate.ticket_price_status === "free"
    && candidate.ticket_price_minor === 0;
}

function defaultCalendarFree(candidate, calendar) {
  const intervals = Array.isArray(calendar)
    ? calendar
    : (calendar && Array.isArray(calendar.busy_intervals) ? calendar.busy_intervals : []);
  const start = Date.parse(candidate.starts_at);
  const end = Date.parse(candidate.ends_at);
  return !intervals.some((busy) => (
    busy && busy.kind === "timed"
    && start < Date.parse(busy.end_at)
    && end > Date.parse(busy.start_at)
  ));
}

async function defaultDiscoverOnPage({ page, detailOffset = 0 }) {
  if (
    !page || typeof page.goto !== "function" || typeof page.evaluate !== "function"
    || typeof page.waitForTimeout !== "function"
  ) invalid();
  await page.goto(TOKYO_DISCOVER_URL, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const inventory = await collectLumaInventory({
    readSnapshot: () => readLumaTimelineSnapshot(page),
    advance: () => advanceLumaTimeline(page),
  });
  if (!Number.isSafeInteger(detailOffset) || detailOffset < 0) invalid();
  const details = [];
  const detailCandidates = Array.from(
    { length: Math.min(LUMA_DETAIL_WALK_LIMIT, inventory.candidates.length) },
    (_, index) => inventory.candidates[(detailOffset + index) % inventory.candidates.length],
  );
  for (const candidate of detailCandidates) {
    try {
      await page.goto(candidate.canonical_url, { waitUntil: "domcontentloaded", timeout: LUMA_DETAIL_NAVIGATION_TIMEOUT_MS });
      const detail = normalizeLumaEventDetail(
        await readRawLumaEventDetail(page, candidate.canonical_url),
      );
      if (detail) details.push(Object.freeze({ provider: "luma", ...detail }));
    } catch {
      // One malformed or unavailable detail must not discard the rest of the listing.
    }
  }
  return Object.freeze({
    observed_count: inventory.candidates.length,
    candidates: Object.freeze(details),
  });
}

async function defaultReadProviderStateOnPage({ page, candidate }) {
  const detail = normalizeLumaEventDetail(
    await readRawLumaEventDetail(page, candidate.canonical_url),
  );
  if (!detail) return Object.freeze({ status: "unavailable" });
  if (detail.rsvp_status === "registered") return Object.freeze({ status: "registered" });
  if (detail.rsvp_status === "available") return Object.freeze({ status: "absent" });
  return Object.freeze({ status: "unavailable" });
}

function normalizedProviderState(value) {
  const status = String(value && value.status || "");
  if (["registered", "pending"].includes(status)) {
    const receipt = String(value.provider_receipt_id || "");
    return Object.freeze({ status, ...(receipt ? { provider_receipt_id: receipt } : {}) });
  }
  if (["available", "absent"].includes(status)) return Object.freeze({ status: "absent" });
  return Object.freeze({ status: "unavailable" });
}

function createLumaScriptFirstWorkflow(options = {}) {
  const now = options.now || (() => new Date());
  const discoverOnPage = options.discoverOnPage || ((input) => defaultDiscoverOnPage({
    ...input,
    detailOffset: Math.floor(now().getTime() / LUMA_DETAIL_ROTATION_MS) * LUMA_DETAIL_WALK_LIMIT,
  }));
  const isCalendarFree = options.isCalendarFree || defaultCalendarFree;
  const submitOnPage = options.submitOnPage || submitLumaOnPage;
  const readProviderStateOnPage = options.readProviderStateOnPage || defaultReadProviderStateOnPage;
  const readLumaFormProfile = options.readLumaFormProfile;
  const agenticRegister = options.agenticRegister;
  const onDiscoveryAudit = options.onDiscoveryAudit || (() => {});
  // Fail closed: until production wiring proves an event has no bundle,
  // treat it as bundled so a mis-wired caller can never re-surface it.
  const hasAppliedBundle = options.hasAppliedBundle || (() => true);
  if (
    typeof now !== "function" || typeof discoverOnPage !== "function"
    || typeof isCalendarFree !== "function"
    || typeof submitOnPage !== "function" || typeof readProviderStateOnPage !== "function"
    || typeof onDiscoveryAudit !== "function" || typeof hasAppliedBundle !== "function"
    || (readLumaFormProfile != null && typeof readLumaFormProfile !== "function")
    || (agenticRegister != null && typeof agenticRegister !== "function")
  ) invalid();

  return Object.freeze({
    async discoverCandidates({ page, calendar }) {
      const discovery = await discoverOnPage({ page });
      const observed = Array.isArray(discovery) ? discovery : discovery && discovery.candidates;
      if (!Array.isArray(observed) || observed.length > 500) invalid();
      const observedCount = Array.isArray(discovery)
        ? observed.length : Number(discovery.observed_count);
      if (!Number.isInteger(observedCount) || observedCount < observed.length || observedCount > 500) invalid();
      const window = candidateWindow(now);
      const result = [];
      // Registrations the provider already reports but that never produced
      // an applied_bundle (evidence chain failed mid-way on a prior wake).
      // These skip the direct-registration path entirely downstream — the
      // runner's existing pre-submit readback sees "registered" and jumps
      // straight to completeEvidence(). Bounded so a backlog can't make a
      // wake unbounded; see LUMA_RECONCILE_LIMIT.
      const reconcile = [];
      let windowCount = 0;
      let freeOpenCount = 0;
      for (const raw of observed) {
        const candidate = exactEvent(raw);
        const startsAt = Date.parse(candidate.starts_at);
        if (startsAt < window.start || startsAt >= window.end) continue;
        windowCount += 1;
        if (candidate.rsvp_status === "registered") {
          if (reconcile.length >= LUMA_RECONCILE_LIMIT) continue;
          if (await hasAppliedBundle(candidate)) continue;
          reconcile.push(Object.freeze({ ...candidate }));
          continue;
        }
        if (!isFreeOpen(candidate)) continue;
        freeOpenCount += 1;
        if (!await isCalendarFree(candidate, calendar)) continue;
        result.push(Object.freeze({ ...candidate }));
      }
      await onDiscoveryAudit(Object.freeze({
        observed_count: observedCount,
        normalized_count: observed.length,
        window_count: windowCount,
        free_open_count: freeOpenCount,
        calendar_free_count: reconcile.length + result.length,
      }));
      return Object.freeze([...reconcile, ...result]);
    },

    async runDirectAction({ page, candidate }) {
      const selected = exactEvent(candidate);
      // normalizeLumaEventDetail (luma-event-detail.js) already computed
      // auth_status for this exact candidate during discovery, by checking
      // for a "ログイン"/"sign in"/"log in" control on the event page — the
      // same signal a daily-driver logout leaves behind. Reusing it here
      // costs no extra page read and reports the session problem before an
      // attempt that would only fail with a generic control-unavailable
      // reason.
      if (selected.auth_status === "login_required") {
        return Object.freeze({ status: "failed", safe_reason: "luma_session_expired" });
      }
      try {
        const outcome = await submitOnPage(page, selected, {
          readLumaFormProfile,
          agenticRegister,
        });
        return outcome && outcome.status === "registered"
          ? Object.freeze({ status: "completed", method: "luma_direct_submit" })
          : Object.freeze({ status: "failed", safe_reason: "direct_action_unverified" });
      } catch (error) {
        const code = String(error && error.code || "");
        return Object.freeze({
          status: "failed",
          safe_reason: DIRECT_ACTION_FAILURE_REASONS.get(code) || "direct_action_failed",
        });
      }
    },

    async readProviderState({ page, candidate }) {
      const selected = exactEvent(candidate);
      return normalizedProviderState(await readProviderStateOnPage({ page, candidate: selected }));
    },
  });
}

module.exports = { createLumaScriptFirstWorkflow };
