"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { createConnpassScriptFirstWorkflow } = require("./connector-connpass-workflow.js");

function event(id, overrides = {}) {
  return Object.freeze({
    provider: "connpass",
    event_ref: `connpass-event://event/${id}`,
    canonical_url: `https://tokyo-builders.connpass.com/event/${id}/`,
    title: `Event ${id}`,
    starts_at: "2026-08-10T10:00:00.000Z",
    ends_at: "2026-08-10T11:00:00.000Z",
    registration_status: "available",
    ticket_price_status: "free",
    ticket_price_minor: 0,
    venue_name: "Tokyo venue",
    venue_address: "Tokyo",
    ...overrides,
  });
}

test("Connpass discovery includes Tokyo day zero through day twenty-seven and excludes day twenty-eight", async () => {
  const page = Object.freeze({ page_id: "same-owned-page" });
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage(input) {
      assert.equal(input.page, page);
      return [
        event(101, { starts_at: "2026-09-04T10:00:00.000Z", ends_at: "2026-09-04T11:00:00.000Z" }),
        event(106, { starts_at: "2026-09-03T14:59:59.000Z", ends_at: "2026-09-03T15:30:00.000Z" }),
        event(102, { ticket_price_status: "paid", ticket_price_minor: 1000 }),
        event(103, { registration_status: "closed" }),
        event(104),
        event(105),
      ];
    },
    isCalendarFree(candidate) { return candidate.event_ref !== "connpass-event://event/104"; },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });

  const result = await workflow.discoverCandidates({ page, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), ["connpass-event://event/106", "connpass-event://event/105"]);
});

test("Connpass official API discovery reads 28 Tokyo dates without navigating provider pages", async () => {
  const apiCalls = [];
  const page = { async goto() { assert.fail("official API discovery must not navigate connpass pages"); } };
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    connpassApiClient: {
      async searchTokyoInventory(input) {
        apiCalls.push(input);
        return [{
          id: 901, title: "Tokyo AI Builders", url: "https://tokyo-ai.connpass.com/event/901/",
          started_at: "2026-08-10T19:00:00+09:00", ended_at: "2026-08-10T21:00:00+09:00",
          open_status: "preopen", event_type: "participation", limit: 100, accepted: 20, waiting: 0,
          description: `AI builders meetup ${"x".repeat(9_000)}`, place: "Shibuya", address: "Tokyo",
        }, {
          id: 902, title: "Already Started", url: "https://tokyo-ai.connpass.com/event/902/",
          started_at: "2026-08-10T19:00:00+09:00", ended_at: "2026-08-10T21:00:00+09:00",
          open_status: "open", event_type: "participation", limit: 100, accepted: 20, waiting: 0,
        }, {
          id: 903, title: "External Advertisement", url: "https://tokyo-ai.connpass.com/event/903/",
          started_at: "2026-08-10T19:00:00+09:00", ended_at: "2026-08-10T21:00:00+09:00",
          open_status: "preopen", event_type: "advertisement", limit: 100, accepted: 20, waiting: 0,
        }];
      },
    },
  });

  const result = await workflow.discoverCandidates({ page, calendar: [] });
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0].ymd.length, 28);
  assert.equal(new Set(apiCalls[0].ymd).size, 28);
  assert.deepEqual([apiCalls[0].ymd[0], apiCalls[0].ymd.at(-1)], ["20260807", "20260903"]);
  assert.deepEqual(result.map((candidate) => candidate.event_ref), ["connpass-event://event/901"]);
  assert.equal(result[0].discovery_source, "official_api_v2");
  assert.equal(result[0].description.length, 8_000);
  assert.match(result[0].description, /^AI builders meetup /);
  assert.equal(result[0].starts_at, "2026-08-10T10:00:00.000Z");
  assert.equal(result[0].ends_at, "2026-08-10T12:00:00.000Z");
  assert.equal(result[0].ticket_price_status, "provider_validation_required");
  assert.equal(result[0].ticket_price_minor, null);
  assert.deepEqual({
    participation_slot_status: result[0].participation_slot_status,
    lightning_talk_status: result[0].lightning_talk_status,
    waiting_count: result[0].waiting_count,
    application_deadline_at: result[0].application_deadline_at,
    canonical_url: result[0].canonical_url,
  }, {
    participation_slot_status: "available",
    lightning_talk_status: "unknown",
    waiting_count: 0,
    application_deadline_at: null,
    canonical_url: "https://tokyo-ai.connpass.com/event/901/",
  });
});

test("Connpass official API never invents free pricing and only that source may defer fee validation", async () => {
  const deferred = event(910, {
    ticket_price_status: "provider_validation_required",
    ticket_price_minor: null,
    discovery_source: "official_api_v2",
  });
  const untrusted = event(911, {
    ticket_price_status: "provider_validation_required",
    ticket_price_minor: null,
  });
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [deferred, untrusted]; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [deferred.event_ref]);
});

test("Connpass recovery stably returns registered before available candidates", async () => {
  const registered = event(106, { registration_status: "registered" });
  const availableA = event(107);
  const availableB = event(108);
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [availableA, registered, availableB]; },
    async hasAppliedBundle() { return false; },
  });
  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result, [registered, availableA, availableB]);
});

test("Connpass registered recovery bypasses Calendar conflict while available remains blocked", async () => {
  const registered = event(109, { registration_status: "registered" });
  const available = event(110);
  const checked = [];
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered, available]; },
    async isCalendarFree(candidate) { checked.push(candidate.event_ref); return false; },
    async hasAppliedBundle() { return false; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [{ kind: "timed", start_at: registered.starts_at, end_at: registered.ends_at }] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [registered.event_ref]);
  assert.deepEqual(checked, [available.event_ref]);
});

test("Connpass recovery audit counts registered separately from free-open candidates", async () => {
  const audits = [];
  const registered = event(111, { registration_status: "registered" });
  const available = event(112);
  const paid = event(113, { registration_status: "unknown", ticket_price_status: "paid", ticket_price_minor: 1000 });
  const outsideRegistered = event(115, { registration_status: "registered", starts_at: "2026-09-05T10:00:00.000Z", ends_at: "2026-09-05T11:00:00.000Z" });
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered, available, paid, outsideRegistered]; },
    onDiscoveryAudit(audit) { audits.push(audit); },
    async hasAppliedBundle() { return false; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [registered.event_ref, available.event_ref]);
  assert.deepEqual(audits, [{
    observed_count: 4, normalized_count: 4, window_count: 3,
    free_open_count: 1, calendar_free_count: 2,
  }]);
});

test("Connpass discovery surfaces an already-registered event with no bundle for reconciliation, bypassing the free-open and calendar-free filters", async () => {
  const registered = event(116, { registration_status: "registered" });
  const bundleChecks = [];
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered]; },
    isCalendarFree() { throw new Error("must not be called for a reconciliation candidate"); },
    async submitOnPage() { throw new Error("must not be called for a reconciliation candidate"); },
    async readStateOnPage() { throw new Error("must not be called during discovery"); },
    async hasAppliedBundle(candidate) { bundleChecks.push(candidate.event_ref); return false; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [registered.event_ref]);
  assert.deepEqual(bundleChecks, [registered.event_ref]);
});

test("Connpass discovery drops an already-registered event once it already has an applied bundle", async () => {
  const registered = event(117, { registration_status: "registered" });
  const openCandidate = event(118);
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered, openCandidate]; },
    isCalendarFree() { return true; },
    async hasAppliedBundle() { return true; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [openCandidate.event_ref]);
});

test("Connpass discovery defaults to treating a candidate as already bundled when no bundle check is wired", async () => {
  const registered = event(119, { registration_status: "registered" });
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [registered]; },
    isCalendarFree() { return true; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result, []);
});

test("Connpass discovery reconciles at most three already-registered unbundled events per wake, even with a larger backlog", async () => {
  const backlog = [201, 202, 203, 204, 205].map((id) => event(id, { registration_status: "registered" }));
  const openCandidate = event(206);
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() { return [...backlog, openCandidate]; },
    isCalendarFree() { return true; },
    async hasAppliedBundle() { return false; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [
    "connpass-event://event/201",
    "connpass-event://event/202",
    "connpass-event://event/203",
    "connpass-event://event/206",
  ]);
});

test("Connpass discovery reports the ordered eligibility gate counts", async () => {
  const audits = [];
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() {
      return [
        event(201, { starts_at: "2026-09-04T16:00:00.000Z", ends_at: "2026-09-04T17:00:00.000Z" }),
        event(202, { ticket_price_status: "paid", ticket_price_minor: 1000 }),
        event(203, { registration_status: "closed" }),
        event(204),
        event(205),
      ];
    },
    isCalendarFree(candidate) { return candidate.event_ref !== "connpass-event://event/204"; },
    onDiscoveryAudit(audit) { audits.push(audit); },
  });

  await workflow.discoverCandidates({ page: {}, calendar: [] });

  assert.deepEqual(audits, [{
    observed_count: 5,
    normalized_count: 5,
    window_count: 4,
    free_open_count: 2,
    calendar_free_count: 1,
  }]);
});

test("Connpass discovery requires a non-online public venue before checking Calendar", async () => {
  const checked = [];
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async discoverOnPage() {
      return [
        event(301, { venue_name: "", venue_address: "" }),
        event(302, { venue_name: "オンライン配信", venue_address: "Zoom" }),
        event(303, { venue_name: "未定", venue_address: "東京都港区1-2-3" }),
        event(304, { venue_name: "Shibuya Hall", venue_address: "未定" }),
        event(305, { venue_name: "Shibuya Hall", venue_address: "会場" }),
        event(306, { venue_name: "Shibuya Hall", venue_address: "Tokyo" }),
      ];
    },
    isCalendarFree(candidate) { checked.push(candidate.event_ref); return true; },
  });

  const result = await workflow.discoverCandidates({ page: {}, calendar: [] });
  assert.deepEqual(result.map((candidate) => candidate.event_ref), ["connpass-event://event/306"]);
  assert.deepEqual(checked, ["connpass-event://event/306"]);
});

test("Connpass direct action and parent readback use the supplied owned page", async () => {
  const candidate = event(105);
  const page = Object.freeze({ page_id: "same-owned-page", url() { return candidate.canonical_url; } });
  const calls = [];
  const workflow = createConnpassScriptFirstWorkflow({
    allowAutomatedSubmit: true,
    async discoverOnPage() { return []; },
    async submitOnPage(suppliedPage, suppliedCandidate) {
      calls.push(["submit", suppliedPage, suppliedCandidate]);
      return { status: "pending", effect_started: true };
    },
    async readStateOnPage(suppliedPage) {
      calls.push(["readback", suppliedPage]);
      return { state: "pending" };
    },
  });

  assert.deepEqual(await workflow.runDirectAction({ page, candidate }), {
    status: "completed", method: "connpass_direct_submit",
  });
  assert.deepEqual(await workflow.readProviderState({ page, candidate }), { status: "pending" });
  assert.equal(calls.every((entry) => entry[1] === page), true);
});

test("Connpass direct action is zero while provider automation permission is unverified", async () => {
  let submits = 0;
  const workflow = createConnpassScriptFirstWorkflow({
    discoverOnPage: async () => [],
    allowAutomatedSubmit: false,
    async submitOnPage() { submits += 1; return { status: "registered" }; },
  });
  const result = await workflow.runDirectAction({ page: {}, candidate: event(990) });
  assert.deepEqual(result, { status: "failed", safe_reason: "connpass_action_permission_required" });
  assert.equal(submits, 0);
});

test("Connpass workflow defaults to no automated submit permission", async () => {
  let submits = 0;
  const workflow = createConnpassScriptFirstWorkflow({
    discoverOnPage: async () => [],
    async submitOnPage() { submits += 1; return { status: "registered" }; },
  });
  const result = await workflow.runDirectAction({ page: {}, candidate: event(991) });
  assert.deepEqual(result, { status: "failed", safe_reason: "connpass_action_permission_required" });
  assert.equal(submits, 0);
});

// Placeholder name only — never Dais's real name in a test fixture (see
// connpass-browser-provider.test.js's PLACEHOLDER_IDENTITY comment). Proves
// the workflow layer threads whatever readAttendeeName resolves straight
// through to submitOnPage's third argument, the same shape
// connector-peatix-workflow.js already threads readAttendeeProfile through
// (see "Peatix direct action carries the exact ticket/profile..." in
// connector-peatix-workflow.test.js).
test("Connpass direct action threads the injected attendee name to submitOnPage, resolved after discovery", async () => {
  const candidate = event(109);
  const page = Object.freeze({ page_id: "same-owned-page", url() { return candidate.canonical_url; } });
  const calls = [];
  const workflow = createConnpassScriptFirstWorkflow({
    allowAutomatedSubmit: true,
    async discoverOnPage() { return []; },
    readAttendeeName: async () => { calls.push("name"); return "Placeholder Taro"; },
    async submitOnPage(suppliedPage, suppliedCandidate, suppliedDependencies) {
      calls.push(["submit", suppliedPage, suppliedCandidate, suppliedDependencies]);
      return { status: "registered", effect_started: true };
    },
  });
  assert.deepEqual(await workflow.runDirectAction({ page, candidate }), {
    status: "completed", method: "connpass_direct_submit",
  });
  assert.deepEqual(calls, [
    "name",
    ["submit", page, candidate, { attendeeName: "Placeholder Taro" }],
  ]);
});

test("Connpass direct action with no attendee-name injector still submits with an undefined name, never a guess", async () => {
  const candidate = event(110);
  const page = Object.freeze({ page_id: "same-owned-page", url() { return candidate.canonical_url; } });
  let receivedDependencies;
  const workflow = createConnpassScriptFirstWorkflow({
    allowAutomatedSubmit: true,
    async discoverOnPage() { return []; },
    async submitOnPage(suppliedPage, suppliedCandidate, suppliedDependencies) {
      receivedDependencies = suppliedDependencies;
      return { status: "registered", effect_started: true };
    },
  });
  assert.deepEqual(await workflow.runDirectAction({ page, candidate }), {
    status: "completed", method: "connpass_direct_submit",
  });
  assert.deepEqual(receivedDependencies, { attendeeName: undefined });
});

test("Connpass direct action reports a login-wall submit as a session problem, not a generic failure", async () => {
  const candidate = event(111);
  const page = Object.freeze({ page_id: "same-owned-page", url() { return candidate.canonical_url; } });
  const workflow = createConnpassScriptFirstWorkflow({
    allowAutomatedSubmit: true,
    async discoverOnPage() { return []; },
    async submitOnPage() {
      const error = new Error("Connpass session expired");
      error.code = "CONNPASS_SESSION_EXPIRED";
      error.unknownEffect = false;
      throw error;
    },
  });
  assert.deepEqual(await workflow.runDirectAction({ page, candidate }), {
    status: "failed", safe_reason: "connpass_session_expired",
  });
});

test("Connpass direct action still propagates every other submit error unchanged, never mistaking it for a session problem", async () => {
  const candidate = event(112);
  const page = Object.freeze({ page_id: "same-owned-page", url() { return candidate.canonical_url; } });
  const workflow = createConnpassScriptFirstWorkflow({
    allowAutomatedSubmit: true,
    async discoverOnPage() { return []; },
    async submitOnPage() {
      const error = new Error("Connpass participation tier unavailable");
      error.code = "CONNPASS_TIER_UNAVAILABLE";
      error.unknownEffect = false;
      throw error;
    },
  });
  await assert.rejects(workflow.runDirectAction({ page, candidate }), (error) => {
    assert.equal(error.code, "CONNPASS_TIER_UNAVAILABLE");
    return true;
  });
});

test("Connpass direct action requires exact canonical URL without browser side effects", async () => {
  const candidate = event(106);
  const cases = [
    ["join", `${candidate.canonical_url}join/`],
    ["complete", `${candidate.canonical_url}complete/`],
    ["query", `${candidate.canonical_url}?submitted=1`],
    ["hash", `${candidate.canonical_url}#submitted`],
    ["wrong-event", "https://tokyo-builders.connpass.com/event/108/"],
    ["about-blank", "about:blank"],
    ["missing-url", null],
    ["throwing-url", "throw"],
  ];
  for (const [label, resultingUrl] of cases) {
    let currentUrl = candidate.canonical_url;
    let submitCalls = 0;
    const forbiddenCalls = [];
    const page = label === "missing-url" ? {} : {
      url() {
        if (resultingUrl === "throw") throw new Error("url unavailable");
        return currentUrl;
      },
      goto() { forbiddenCalls.push("goto"); },
      getByRole() { forbiddenCalls.push("click"); },
      readCredentials() { forbiddenCalls.push("credentials"); },
    };
    const workflow = createConnpassScriptFirstWorkflow({
      allowAutomatedSubmit: true,
      async discoverOnPage() { return []; },
      async submitOnPage() {
        submitCalls += 1;
        currentUrl = resultingUrl;
        return { status: "registered" };
      },
    });
    assert.deepEqual(
      await workflow.runDirectAction({ page, candidate }),
      { status: "failed", safe_reason: "direct_action_unverified" },
      label,
    );
    assert.equal(submitCalls, 1, label);
    assert.deepEqual(forbiddenCalls, [], label);
  }
});

test("Connpass default discovery reuses one page across two calendar months and event details", async () => {
  const navigations = [];
  const page = {
    current: "",
    async goto(url) { this.current = url; navigations.push(url); },
  };
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-27T03:00:00.000Z"),
    async readCalendarBindings(suppliedPage) {
      assert.equal(suppliedPage, page);
      return suppliedPage.current.includes("ym=202608") ? [{
        event_ref: "connpass-event://event/201",
        canonical_url: "https://tokyo-builders.connpass.com/event/201/",
        calendar_date: "2026-08-30",
      }] : [{
        event_ref: "connpass-event://event/202",
        canonical_url: "https://tokyo-builders.connpass.com/event/202/",
        calendar_date: "2026-09-03",
      }];
    },
    async readEventDetail(suppliedPage) {
      assert.equal(suppliedPage, page);
      const id = suppliedPage.current.includes("/201/") ? 201 : 202;
      const date = id === 201 ? "2026-08-30" : "2026-09-03";
      return {
        ...event(id, { starts_at: `${date}T10:00:00.000Z`, ends_at: `${date}T11:00:00.000Z` }),
        controls: ["このイベントに申し込む"], offers: [{ price: "0", priceCurrency: "JPY" }],
        venue_name: "Public venue", address: "Tokyo",
      };
    },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });

  const result = await workflow.discoverCandidates({ page, calendar: [] });

  assert.deepEqual(result.map((candidate) => candidate.event_ref), [
    "connpass-event://event/201", "connpass-event://event/202",
  ]);
  assert.equal(navigations.filter((url) => url.includes("/calendar/")).length, 2);
  assert.equal(navigations.filter((url) => url.includes("/event/")).length, 2);
});

test("Connpass default discovery identifies the exact failed browser stage safely", async () => {
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async readCalendarBindings() { return []; },
    async readEventDetail() { return {}; },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });
  const page = { async goto() { throw new Error("private browser error"); } };

  await assert.rejects(
    workflow.discoverCandidates({ page, calendar: [] }),
    (error) => error.code === "CONNPASS_CALENDAR_NAVIGATION_FAILED"
      && error.message === "Connpass discovery stage failed",
  );
});

test("Connpass accepts a verified same-event redirect from group subdomain to root host", async () => {
  const page = { current: "", async goto(url) { this.current = url; } };
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async readCalendarBindings() {
      return [{ event_ref: "connpass-event://event/393711", canonical_url: "https://group.connpass.com/event/393711/", calendar_date: "2026-08-10" }];
    },
    async readEventDetail() {
      return {
        event_ref: "connpass-event://event/393711", canonical_url: "https://connpass.com/event/393711/",
        title: "Public event", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z",
        venue_name: "Public venue", address: "Tokyo", controls: ["このイベントに申し込む"],
        offers: [{ price: "0", priceCurrency: "JPY" }], price_labels: ["参加費 無料"],
      };
    },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });

  const result = await workflow.discoverCandidates({ page, calendar: [] });
  assert.equal(result[0].canonical_url, "https://connpass.com/event/393711/");
});

test("Connpass reports a safe code when detail redirects to a different event identity", async () => {
  const page = { async goto() {} };
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async readCalendarBindings() {
      return [{ event_ref: "connpass-event://event/393711", canonical_url: "https://connpass.com/event/393711/", calendar_date: "2026-08-10" }];
    },
    async readEventDetail() {
      return {
        event_ref: "connpass-event://event/999999", canonical_url: "https://connpass.com/event/999999/",
        title: "Public event", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z",
        venue_name: "Public venue", address: "Tokyo", controls: ["このイベントに申し込む"],
        offers: [{ price: "0", priceCurrency: "JPY" }], price_labels: ["参加費 無料"],
      };
    },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });

  await assert.rejects(
    workflow.discoverCandidates({ page, calendar: [] }),
    (error) => error.code === "CONNPASS_DETAIL_IDENTITY_MISMATCH_FAILED",
  );
});

test("Connpass distinguishes binding validation from candidate validation", async () => {
  const common = {
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  };
  const bindingWorkflow = createConnpassScriptFirstWorkflow({
    ...common,
    async readCalendarBindings() {
      return [{ event_ref: "connpass-event://event/393711", canonical_url: "https://example.com/event/393711/", calendar_date: "2026-08-10" }];
    },
    async readEventDetail() { return {}; },
  });
  await assert.rejects(
    bindingWorkflow.discoverCandidates({ page: { async goto() {} }, calendar: [] }),
    (error) => error.code === "CONNPASS_CALENDAR_BINDING_VALIDATION_FAILED",
  );

  const candidateWorkflow = createConnpassScriptFirstWorkflow({
    ...common,
    async discoverOnPage() { return [{ provider: "connpass" }]; },
  });
  await assert.rejects(
    candidateWorkflow.discoverCandidates({ page: {}, calendar: [] }),
    (error) => error.code === "CONNPASS_CANDIDATE_VALIDATION_FAILED",
  );
});

test("Connpass classifies remaining parent discovery contracts without leaking errors", async () => {
  const common = {
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  };
  const rowsWorkflow = createConnpassScriptFirstWorkflow({
    ...common,
    async readCalendarBindings() { return Array.from({ length: 5001 }, () => ({})); },
    async readEventDetail() { return {}; },
  });
  await assert.rejects(
    rowsWorkflow.discoverCandidates({ page: { async goto() {} }, calendar: [] }),
    (error) => error.code === "CONNPASS_CALENDAR_ROWS_CONTRACT_FAILED",
  );

  const resultWorkflow = createConnpassScriptFirstWorkflow({
    ...common,
    async discoverOnPage() { return {}; },
  });
  await assert.rejects(
    resultWorkflow.discoverCandidates({ page: {}, calendar: [] }),
    (error) => error.code === "CONNPASS_DISCOVERY_RESULT_CONTRACT_FAILED",
  );

  const calendarWorkflow = createConnpassScriptFirstWorkflow({
    ...common,
    async discoverOnPage() { return [event(301)]; },
    async isCalendarFree() { throw new Error("private calendar error"); },
  });
  await assert.rejects(
    calendarWorkflow.discoverCandidates({ page: {}, calendar: [] }),
    (error) => error.code === "CONNPASS_CALENDAR_CONFLICT_CHECK_FAILED",
  );
});

// Regression for the 2026-08-16 measured wake failure: every code below used
// to be an uncoded `invalid()` throw that escaped createDefaultDiscovery /
// discoverCandidates without ever being wrapped by a stageError() try/catch,
// so connector-minimal-runner.js's safeDiscoveryReason() fell back to the
// generic "provider_discovery_failed" no matter which of these actually
// fired. Each one must now surface its own stage code.
test("Connpass gives a stable stage code to every previously uncoded discovery throw", async () => {
  const noPageWorkflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T03:00:00.000Z"),
  });
  await assert.rejects(
    noPageWorkflow.discoverCandidates({ page: null, calendar: [] }),
    (error) => error.code === "CONNPASS_DISCOVERY_PAGE_CONTRACT_FAILED",
  );

  const badClockWorkflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("not-a-real-date"),
  });
  await assert.rejects(
    badClockWorkflow.discoverCandidates({ page: { async goto() {} }, calendar: [] }),
    (error) => error.code === "CONNPASS_DISCOVERY_DATES_FAILED",
  );

  const badWindowWorkflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("also-not-a-real-date"),
    async discoverOnPage() { return []; },
  });
  await assert.rejects(
    badWindowWorkflow.discoverCandidates({ page: {}, calendar: [] }),
    (error) => error.code === "CONNPASS_CANDIDATE_WINDOW_FAILED",
  );

  const auditSinkWorkflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async discoverOnPage() { return []; },
    onDiscoveryAudit() { throw new Error("private audit sink error"); },
  });
  await assert.rejects(
    auditSinkWorkflow.discoverCandidates({ page: {}, calendar: [] }),
    (error) => error.code === "CONNPASS_DISCOVERY_AUDIT_FAILED"
      && !String(error.message).includes("private audit sink error"),
  );
});

test("Connpass filters large calendar noise before enforcing the eligible binding cap", async () => {
  const noise = Array.from({ length: 501 }, (_, index) => ({
    event_ref: `connpass-event://event/${500000 + index}`,
    canonical_url: `https://connpass.com/event/${500000 + index}/`,
    calendar_date: "2026-07-01",
  }));
  const workflow = createConnpassScriptFirstWorkflow({
    now: () => new Date("2026-08-07T03:00:00.000Z"),
    async readCalendarBindings() {
      return [...noise, {
        event_ref: "connpass-event://event/393711",
        canonical_url: "https://connpass.com/event/393711/",
        calendar_date: "2026-08-10",
      }];
    },
    async readEventDetail() {
      return {
        event_ref: "connpass-event://event/393711", canonical_url: "https://connpass.com/event/393711/",
        title: "Public event", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z",
        venue_name: "Public venue", address: "Tokyo", controls: ["このイベントに申し込む"],
        offers: [{ price: "0", priceCurrency: "JPY" }], price_labels: ["参加費 無料"],
      };
    },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });

  const result = await workflow.discoverCandidates({ page: { async goto() {} }, calendar: [] });
  assert.deepEqual(result.map((candidate) => candidate.event_ref), ["connpass-event://event/393711"]);
});

function detailFor(id, date) {
  return {
    ...event(id, { starts_at: `${date}T10:00:00.000Z`, ends_at: `${date}T11:00:00.000Z` }),
    controls: ["このイベントに申し込む"], offers: [{ price: "0", priceCurrency: "JPY" }],
    venue_name: "Public venue", address: "Tokyo",
  };
}

function bindingRow(id, date) {
  return {
    event_ref: `connpass-event://event/${id}`,
    canonical_url: `https://connpass.com/event/${id}/`,
    calendar_date: date,
  };
}

test("Connpass default discovery spreads the walk budget across dates instead of draining the earliest date", async () => {
  // Regression for the measured 2026-08-16 bias: 767 Tokyo events observed,
  // 40 walked, 0 free+open — because the old pass sorted (date, id) then
  // took the first 40, which a busy earliest date fills entirely, so a wake
  // only ever inspected today/tomorrow. Here the earliest date alone has 40
  // events (>= the whole budget) and a later date has 5; a correct fix must
  // still visit some of the later date instead of exhausting the budget on
  // the earliest date alone.
  const now = () => new Date("2026-08-07T08:30:00.000Z");
  const earlyDate = "2026-08-07";
  const lateDate = "2026-08-08";
  const earlyIds = Array.from({ length: 40 }, (_, i) => 700000 + i);
  const lateIds = Array.from({ length: 5 }, (_, i) => 800000 + i);
  // Later-date rows appear first and the early group is reversed, so a pass
  // that trusted page order (instead of sorting) would walk the wrong set.
  const rows = [
    ...lateIds.map((id) => bindingRow(id, lateDate)),
    ...[...earlyIds].reverse().map((id) => bindingRow(id, earlyDate)),
  ];
  const navigatedIds = [];
  const detailDateOf = (id) => (id >= 800000 ? lateDate : earlyDate);
  const workflow = createConnpassScriptFirstWorkflow({
    now,
    async readCalendarBindings() { return rows; },
    async readEventDetail(page) {
      const id = Number(page.current.match(/event\/(\d+)\//)[1]);
      navigatedIds.push(id);
      return detailFor(id, detailDateOf(id));
    },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
  });
  const page = { current: "", async goto(url) { this.current = url; } };

  const result = await workflow.discoverCandidates({ page, calendar: [] });

  // Total visits is still exactly the budget — spreading must not raise it.
  assert.equal(navigatedIds.length, 40);
  // Every later-date event is represented: the whole point of spreading is
  // that a busy earliest date can no longer crowd every other date out.
  assert.deepEqual([...navigatedIds].filter((id) => id >= 800000).sort((a, b) => a - b), lateIds);
  // The earliest date is bounded (not all 40 of its own events survive) —
  // proves the budget is genuinely shared, not just "late date appended".
  const earlyVisited = navigatedIds.filter((id) => id < 800000);
  assert.equal(earlyVisited.length, 35);
  assert.ok(earlyVisited.every((id) => earlyIds.includes(id)));
  // Candidate selection priority is unchanged: earliest-first even though
  // the walk itself sampled out of chronological order.
  const resultIds = result.map((candidate) => Number(candidate.event_ref.split("/").pop()));
  assert.deepEqual(resultIds, [...resultIds].sort((a, b) => a - b));
  assert.deepEqual(resultIds.filter((id) => id >= 800000).sort((a, b) => a - b), lateIds);
});

test("Connpass default discovery survives a busy month without tripping the binding cap and reports the true observed count", async () => {
  const now = () => new Date("2026-08-07T08:30:00.000Z");
  const date = "2026-08-10";
  const totalEvents = 600; // exceeds both the old inline 500-binding cap and the 40 walk budget
  const rows = Array.from({ length: totalEvents }, (_, i) => bindingRow(900_000 + i, date));
  const navigatedIds = [];
  const audits = [];
  const workflow = createConnpassScriptFirstWorkflow({
    now,
    async readCalendarBindings() { return rows; },
    async readEventDetail(page) {
      const id = Number(page.current.match(/event\/(\d+)\//)[1]);
      navigatedIds.push(id);
      return detailFor(id, date);
    },
    async submitOnPage() { return { status: "registered" }; },
    async readStateOnPage() { return { state: "registered" }; },
    onDiscoveryAudit(audit) { audits.push(audit); },
  });
  const page = { current: "", async goto(url) { this.current = url; } };

  const result = await workflow.discoverCandidates({ page, calendar: [] });

  assert.equal(navigatedIds.length, 40);
  assert.equal(result.length, 40);
  assert.deepEqual(
    [...navigatedIds].sort((a, b) => a - b),
    Array.from({ length: 40 }, (_, i) => 900_000 + i),
  );
  assert.equal(audits.length, 1);
  assert.equal(audits[0].observed_count, totalEvents);
});
