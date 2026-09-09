"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { createLumaScriptFirstWorkflow } = require("./connector-luma-workflow.js");

function event(slug, overrides = {}) {
  return Object.freeze({
    provider: "luma",
    event_ref: `luma-event://event/${slug}`,
    canonical_url: `https://luma.com/${slug}`,
    title: `Event ${slug}`,
    starts_at: "2026-08-10T10:00:00.000Z",
    ends_at: "2026-08-10T11:00:00.000Z",
    attendance_mode: "in_person",
    event_status: "scheduled",
    rsvp_status: "available",
    ticket_price_status: "free",
    ticket_price_minor: 0,
    ...overrides,
  });
}

function lumaDetail(slug) {
  return {
    canonicalUrl: `https://luma.com/${slug}`,
    jsonLd: [{
      "@type": "Event",
      name: `Event ${slug}`,
      startDate: "2026-08-10T10:00:00.000Z",
      endDate: "2026-08-10T11:00:00.000Z",
      eventAttendanceMode: "https://schema.org/OfflineEventAttendanceMode",
      eventStatus: "https://schema.org/EventScheduled",
      location: { name: "Tokyo", address: "Tokyo" },
      offers: [{
        name: "Free",
        price: "0",
        priceCurrency: "JPY",
        availability: "https://schema.org/InStock",
      }],
    }],
    controls: ["参加登録"],
  };
}

function defaultDiscoveryPage(slugs, detailFor = () => ({})) {
  const discoveryUrl = "https://luma.com/tokyo?k=p";
  const snapshot = slugs.map((slug) => ({
    href: `https://luma.com/${slug}`,
    title: `Event ${slug}`,
    cardText: "10:00",
    timelineText: "8月10日 月曜日",
  }));
  let currentUrl = "";
  const gotoCalls = [];
  const page = {
    async goto(url) {
      currentUrl = url;
      gotoCalls.push(url);
      const behavior = detailFor(url);
      if (behavior.type === "goto-throw") throw behavior.error;
    },
    async waitForTimeout() {},
    async evaluate(callback) {
      if (currentUrl === discoveryUrl) {
        const source = String(callback);
        if (source.includes("querySelectorAll")) return snapshot;
        if (source.includes("root.scrollTo(")) return undefined;
        return { atEnd: true, scrollHeight: 100 };
      }
      const behavior = detailFor(currentUrl);
      if (behavior.type === "read-throw") throw behavior.error;
      return behavior.raw || lumaDetail(currentUrl.split("/").at(-1));
    },
  };
  return { page, gotoCalls };
}

test("Luma default detail walk is bounded to six candidates while observed_count stays full", async () => {
  const slugs = Array.from({ length: 13 }, (_, index) => `bounded-${index + 1}`);
  const { page, gotoCalls } = defaultDiscoveryPage(slugs);
  const audits = [];
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async onDiscoveryAudit(value) { audits.push(value); },
  });

  const result = await workflow.discoverCandidates({ page, calendar: [] });

  assert.equal(result.length, 6);
  assert.equal(audits[0].observed_count, 13);
  assert.equal(gotoCalls.length, 7);
  assert.equal(new Set(gotoCalls.slice(1)).size, 6);
});

test("Luma default detail walk advances by one full bounded slice every minute", async () => {
  const slugs = Array.from({ length: 8 }, (_, index) => `rotated-${index + 1}`);
  const { page, gotoCalls } = defaultDiscoveryPage(slugs);
  let now = new Date(0);
  const workflow = createLumaScriptFirstWorkflow({ now: () => now });

  await workflow.discoverCandidates({ page, calendar: [] });
  const first = gotoCalls.filter((url) => url !== "https://luma.com/tokyo?k=p");
  gotoCalls.length = 0;
  now = new Date(60_000);
  await workflow.discoverCandidates({ page, calendar: [] });
  const second = gotoCalls.filter((url) => url !== "https://luma.com/tokyo?k=p");

  assert.deepEqual(first, slugs.slice(0, 6).map((slug) => `https://luma.com/${slug}`));
  assert.deepEqual(second, [...slugs.slice(6), ...slugs.slice(0, 4)]
    .map((slug) => `https://luma.com/${slug}`));
});

test("Luma default detail navigation, read, and normalize failures skip one candidate and continue", async () => {
  const slugs = ["detail-ok-1", "detail-nav-fail", "detail-read-fail", "detail-normalize-fail", "detail-ok-2"];
  const privateNavigationMessage = "private detail navigation";
  const privateReadMessage = "private detail read";
  const privateNormalizeMessage = "private detail normalize";
  const detailFor = (url) => {
    if (url.endsWith("detail-nav-fail")) {
      return { type: "goto-throw", error: new Error(privateNavigationMessage) };
    }
    if (url.endsWith("detail-read-fail")) {
      return { type: "read-throw", error: new Error(privateReadMessage) };
    }
    if (url.endsWith("detail-normalize-fail")) {
      const throwingEvent = new Proxy({}, { get() { throw new Error(privateNormalizeMessage); } });
      return { raw: { canonicalUrl: url, jsonLd: [throwingEvent], controls: [] } };
    }
    return {};
  };
  const { page, gotoCalls } = defaultDiscoveryPage(slugs, detailFor);
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
  });

  const result = await workflow.discoverCandidates({ page, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref).sort(), [
    "luma-event://event/detail-ok-1",
    "luma-event://event/detail-ok-2",
  ]);
  assert.deepEqual(gotoCalls.slice(1).sort(), slugs.map((slug) => `https://luma.com/${slug}`).sort());
  const serialized = JSON.stringify(result);
  for (const privateText of [privateNavigationMessage, privateReadMessage, privateNormalizeMessage]) {
    assert.equal(serialized.includes(privateText), false);
  }
});

test("Luma discovery returns the first free open non-conflicting candidates in provider order", async () => {
  const page = Object.freeze({ page_id: "owned-page" });
  const inspected = [
    event("paid", { ticket_price_status: "paid", ticket_price_minor: 1000 }),
    event("conflict"),
    event("closed", { rsvp_status: "closed" }),
    event("free-first"),
    event("free-second", { rsvp_status: "approval_required" }),
  ];
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage(input) {
      assert.equal(input.page, page);
      return inspected;
    },
    isCalendarFree(candidate) { return candidate.event_ref !== "luma-event://event/conflict"; },
    async submitOnPage() { return { status: "registered" }; },
    async readProviderStateOnPage() { return { status: "registered" }; },
  });

  const result = await workflow.discoverCandidates({ page, calendar: { busy_intervals: [] } });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [
    "luma-event://event/free-first",
    "luma-event://event/free-second",
  ]);
});

test("Luma discovery excludes online and hybrid events from automatic application", async () => {
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() {
      return [
        event("online", { attendance_mode: "online" }),
        event("hybrid", { attendance_mode: "hybrid" }),
        event("onsite"),
      ];
    },
    async submitOnPage() { return { status: "registered" }; },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), ["luma-event://event/onsite"]);
});

test("Luma default conflict filter consumes the minimal runner busy interval array", async () => {
  const conflicting = event("calendar-conflict");
  const free = event("calendar-free", {
    starts_at: "2026-08-11T10:00:00.000Z",
    ends_at: "2026-08-11T11:00:00.000Z",
  });
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [conflicting, free]; },
    async submitOnPage() { return { status: "registered" }; },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  const result = await workflow.discoverCandidates({
    page: {},
    calendar: [{
      kind: "timed",
      start_at: "2026-08-10T09:30:00.000Z",
      end_at: "2026-08-10T10:30:00.000Z",
    }],
  });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [free.event_ref]);
});

test("Luma candidates include Tokyo day zero through day twenty-seven and exclude day twenty-eight", async () => {
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() {
      return [
        event("before-window", { starts_at: "2026-08-06T14:59:59.000Z", ends_at: "2026-08-06T15:30:00.000Z" }),
        event("today", { starts_at: "2026-08-06T15:00:00.000Z", ends_at: "2026-08-06T16:00:00.000Z" }),
        event("last-day", { starts_at: "2026-09-03T14:59:59.000Z", ends_at: "2026-09-03T15:30:00.000Z" }),
        event("after-window", { starts_at: "2026-09-03T15:00:00.000Z", ends_at: "2026-09-03T16:00:00.000Z" }),
      ];
    },
    async submitOnPage() { return { status: "registered" }; },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });
  assert.deepEqual(result.map((candidate) => candidate.event_ref), [
    "luma-event://event/today",
    "luma-event://event/last-day",
  ]);
});

test("Luma discovery reports only safe aggregate eligibility counts", async () => {
  const audits = [];
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() {
      return [
        event("outside", { starts_at: "2026-09-04T10:00:00.000Z", ends_at: "2026-09-04T11:00:00.000Z" }),
        event("paid", { ticket_price_status: "paid", ticket_price_minor: 1000 }),
        event("conflict"),
        event("eligible"),
      ];
    },
    isCalendarFree(candidate) { return candidate.event_ref !== "luma-event://event/conflict"; },
    async onDiscoveryAudit(value) { audits.push(value); },
    async submitOnPage() { return { status: "registered" }; },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(audits, [{
    observed_count: 4,
    normalized_count: 4,
    window_count: 3,
    free_open_count: 2,
    calendar_free_count: 1,
  }]);
  assert.deepEqual(Object.keys(audits[0]).sort(), [
    "calendar_free_count", "free_open_count", "normalized_count", "observed_count", "window_count",
  ]);
});

test("Luma discovery surfaces an already-registered event with no bundle for reconciliation, bypassing the free-open and calendar-free filters", async () => {
  const registered = event("already-registered", { rsvp_status: "registered" });
  const bundleChecks = [];
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered]; },
    isCalendarFree() { throw new Error("must not be called for a reconciliation candidate"); },
    async submitOnPage() { throw new Error("must not be called for a reconciliation candidate"); },
    async readProviderStateOnPage() { throw new Error("must not be called during discovery"); },
    async hasAppliedBundle(candidate) { bundleChecks.push(candidate.event_ref); return false; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: { busy_intervals: [] } });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [registered.event_ref]);
  assert.deepEqual(bundleChecks, [registered.event_ref]);
});

test("Luma discovery drops an already-registered event once it already has an applied bundle", async () => {
  const registered = event("bundled-already", { rsvp_status: "registered" });
  const openCandidate = event("still-open");
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered, openCandidate]; },
    isCalendarFree() { return true; },
    async hasAppliedBundle() { return true; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: { busy_intervals: [] } });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [openCandidate.event_ref]);
});

test("Luma discovery defaults to treating a candidate as already bundled when no bundle check is wired", async () => {
  const registered = event("unwired-registered", { rsvp_status: "registered" });
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered]; },
    isCalendarFree() { return true; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: { busy_intervals: [] } });

  assert.deepEqual(result, []);
});

test("Luma discovery reconciles at most three already-registered unbundled events per wake, even with a larger backlog", async () => {
  const backlog = [1, 2, 3, 4, 5].map((n) => event(`backlog-${n}`, { rsvp_status: "registered" }));
  const openCandidate = event("open-alongside-backlog");
  const workflow = createLumaScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [...backlog, openCandidate]; },
    isCalendarFree() { return true; },
    async hasAppliedBundle() { return false; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: { busy_intervals: [] } });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [
    "luma-event://event/backlog-1",
    "luma-event://event/backlog-2",
    "luma-event://event/backlog-3",
    "luma-event://event/open-alongside-backlog",
  ]);
});

test("Luma direct action forwards bounded agentic form assistance to the retained submit function", async () => {
  const calls = [];
  const page = Object.freeze({ page_id: "owned-page" });
  const selected = event("free-first");
  const agenticRegister = async () => Object.freeze({ status: "ready", answers: [] });
  const workflow = createLumaScriptFirstWorkflow({
    async discoverOnPage() { return [selected]; },
    isCalendarFree() { return true; },
    async submitOnPage(suppliedPage, contract, dependencies) {
      calls.push({ suppliedPage, contract, dependencies });
      return Object.freeze({ status: "registered", effect_started: true });
    },
    async readProviderStateOnPage() { return { status: "registered" }; },
    async readLumaFormProfile() { return Object.freeze({ profile_version: 1 }); },
    agenticRegister,
  });

  const result = await workflow.runDirectAction({ page, candidate: selected });

  assert.deepEqual(result, { status: "completed", method: "luma_direct_submit" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].suppliedPage, page);
  assert.equal(calls[0].contract.event_ref, selected.event_ref);
  assert.equal(calls[0].dependencies.agenticRegister, agenticRegister);
  assert.equal(typeof calls[0].dependencies.readLumaFormProfile, "function");
});

test("Luma direct action reports a login-wall candidate as a session problem without attempting the submit", async () => {
  const selected = event("login-walled", { auth_status: "login_required" });
  let submitCalls = 0;
  const workflow = createLumaScriptFirstWorkflow({
    async discoverOnPage() { return [selected]; },
    isCalendarFree() { return true; },
    async submitOnPage() { submitCalls += 1; return { status: "registered" }; },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  const result = await workflow.runDirectAction({ page: {}, candidate: selected });

  assert.deepEqual(result, { status: "failed", safe_reason: "luma_session_expired" });
  assert.equal(submitCalls, 0);
});

test("a closed/full candidate without a login-wall control reports its specific control reason", async () => {
  const selected = event("closed-not-logged-out", { auth_status: "unknown" });
  const workflow = createLumaScriptFirstWorkflow({
    async discoverOnPage() { return [selected]; },
    isCalendarFree() { return true; },
    async submitOnPage() {
      const error = new Error("Luma RSVP control unavailable");
      error.code = "LUMA_CONTROL_UNAVAILABLE";
      throw error;
    },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  const result = await workflow.runDirectAction({ page: {}, candidate: selected });

  assert.deepEqual(result, { status: "failed", safe_reason: "luma_control_unavailable" });
});

test("known Luma submit guards report their specific bounded literal reason", async () => {
  for (const [code, safeReason] of [
    ["LUMA_REQUIRED_PROFILE_FIELD_UNAVAILABLE", "luma_required_profile_field_unavailable"],
    ["LUMA_FORM_PROFILE_UNAVAILABLE", "luma_form_profile_unavailable"],
    ["LUMA_FORM_SCHEMA_UNAVAILABLE", "luma_form_schema_unavailable"],
    ["LUMA_FORM_PLAN_UNAVAILABLE", "luma_form_plan_unavailable"],
    ["LUMA_FORM_FILL_UNAVAILABLE", "luma_form_fill_unavailable"],
    ["LUMA_PAGE_UNAVAILABLE", "luma_page_unavailable"],
    ["LUMA_CONTROL_UNAVAILABLE", "luma_control_unavailable"],
    ["LUMA_CONFIRM_UNAVAILABLE", "luma_confirm_unavailable"],
    ["LUMA_BROWSER_ACTION_FAILED", "luma_browser_action_failed"],
  ]) {
    const workflow = createLumaScriptFirstWorkflow({
      async discoverOnPage() { return [event("fallback")]; },
      isCalendarFree() { return true; },
      async submitOnPage() {
        const error = new Error("private provider text");
        error.code = code;
        throw error;
      },
      async readProviderStateOnPage() { return { status: "absent" }; },
    });

    assert.deepEqual(await workflow.runDirectAction({ page: {}, candidate: event("fallback") }), {
      status: "failed",
      safe_reason: safeReason,
    });
  }
});

test("unknown Luma errors expose only the generic safe reason", async () => {
  const privateMessage = "private Luma page text";
  const privateCode = "LUMA_UNKNOWN_PRIVATE_CODE";
  const workflow = createLumaScriptFirstWorkflow({
    async discoverOnPage() { return [event("unknown-error")]; },
    isCalendarFree() { return true; },
    async submitOnPage() {
      const error = new Error(privateMessage);
      error.code = privateCode;
      throw error;
    },
    async readProviderStateOnPage() { return { status: "absent" }; },
  });

  const result = await workflow.runDirectAction({ page: {}, candidate: event("unknown-error") });

  assert.deepEqual(result, { status: "failed", safe_reason: "direct_action_failed" });
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes(privateMessage), false);
  assert.equal(serialized.includes(privateCode), false);
});

test("parent readback normalizes only registered, pending, absent, and unavailable states", async () => {
  for (const [observed, expected] of [
    [{ status: "registered", provider_receipt_id: "receipt-1" }, { status: "registered", provider_receipt_id: "receipt-1" }],
    [{ status: "pending", provider_receipt_id: "receipt-2" }, { status: "pending", provider_receipt_id: "receipt-2" }],
    [{ status: "available" }, { status: "absent" }],
    [{ status: "closed" }, { status: "unavailable" }],
  ]) {
    const workflow = createLumaScriptFirstWorkflow({
      async discoverOnPage() { return []; },
      isCalendarFree() { return true; },
      async submitOnPage() { return { status: "registered" }; },
      async readProviderStateOnPage() { return observed; },
    });
    assert.deepEqual(
      await workflow.readProviderState({ page: {}, candidate: event("readback") }),
      expected,
    );
  }
});
