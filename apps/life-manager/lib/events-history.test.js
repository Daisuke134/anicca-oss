// events-history.test.js — 11a runtime: care detection needs the user's OWN visit HISTORY, and the
// fetchUpcomingEvents contract is future-only by design (same unreachable-rule disease as 12c: the
// one thing the detector needs — past visits — is the one thing that fetch can never return).
// fetchCalendarHistory is a dedicated history read with its own contract: [now - historyMs, now],
// all-day events kept (a barber visit logged all-day is still a visit), future events excluded.
// Run: node --test lib/events-history.test.js
"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const {
  fetchCalendarHistory, CARE_HISTORY_MS, HISTORY_PAGE_SIZE, HISTORY_MAX_EVENTS,
} = require("./events.js");

const NOW = Date.parse("2026-07-26T00:00:00Z");

function fakeCalendar(items, capture) {
  return {
    kind: "fake",
    ready: () => true,
    async listEventsRaw(_uid, opts) {
      if (capture) {
        capture.opts = opts; // last call
        (capture.all = capture.all || []).push(opts); // every call (the window may be paged)
      }
      return items;
    },
  };
}

// A cursor-aware transport (the calendar-composio.js shape): hands back one page plus the token
// that unlocks the next, exactly like Google's events.list nextPageToken.
function pagingCalendar(pages, capture) {
  return {
    kind: "fake-paging",
    ready: () => true,
    async listEventsPage(_uid, opts) {
      if (capture) {
        capture.opts = opts;
        (capture.all = capture.all || []).push(opts);
      }
      const index = opts.pageToken ? Number(String(opts.pageToken).replace("p", "")) : 0;
      return { items: pages[index] || [], nextPageToken: index + 1 < pages.length ? `p${index + 1}` : null };
    },
  };
}

function eventsSpanning(count, prefix) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const startMs = NOW - CARE_HISTORY_MS + Math.floor(i * ((CARE_HISTORY_MS - 86400000) / Math.max(count, 1)));
    out.push({ id: `${prefix}${i}`, summary: "予定", start: { dateTime: new Date(startMs).toISOString() } });
  }
  return out;
}

// Sorted [startMs, endMs] spans of every transport call — the union must cover the whole window.
function spansOf(capture) {
  return (capture.all || [])
    .map((o) => [Date.parse(o.timeMin), Date.parse(o.timeMax)])
    .sort((a, b) => a[0] - b[0]);
}

test("asks the transport for [now - historyMs, now] — a real look BACK, not forward", async () => {
  const capture = {};
  await fetchCalendarHistory("uid", { nowMs: NOW, historyMs: 100 * 86400000, calendar: fakeCalendar([], capture) });
  assert.equal(capture.opts.timeMin, new Date(NOW - 100 * 86400000).toISOString().replace(/\.\d{3}Z$/, "Z"));
  assert.equal(capture.opts.timeMax, new Date(NOW).toISOString().replace(/\.\d{3}Z$/, "Z"));
});

test("default window is at least 10 years so stable annual/biennial checkup cadence remains observable", async () => {
  const capture = {};
  await fetchCalendarHistory("uid", { nowMs: NOW, calendar: fakeCalendar([], capture) });
  const spans = spansOf(capture);
  assert.equal(spans[0][0], NOW - CARE_HISTORY_MS, "oldest call starts at now - CARE_HISTORY_MS");
  assert.equal(spans[spans.length - 1][1], NOW, "newest call ends at now");
  assert.ok(CARE_HISTORY_MS >= 3652 * 86400000, "at least ten years back");
});

test("complete-history hard cap admits 10,000 events before failing closed", () => {
  assert.equal(HISTORY_MAX_EVENTS, 10000);
});

// 🔴 Completeness must be PROVEN, not guessed. The previous shape split the window into ≤3 ~183-day
// chunks and hoped no chunk exceeded maxResults; it threw nextPageToken away, so a busier calendar
// would have written a silently-truncated history into the append-only lm_care_scan_log as if it
// were the whole truth. Measured against the live Composio GOOGLECALENDAR_EVENTS_LIST: the response
// carries data.nextPageToken whenever more remain (250-item default page → 3 pages → 703 events),
// and the same 703 come back either by following the cursor or in one maxResults=2500 call.
test("follows nextPageToken until the cursor is exhausted — every page's events survive", async () => {
  const capture = {};
  const pages = [
    [{ id: "a", summary: "予定", start: { dateTime: "2025-08-01T00:00:00Z" } }],
    [{ id: "b", summary: "散髪", start: { dateTime: "2025-09-01T00:00:00Z" } }],
    [{ id: "c", summary: "歯科", start: { dateTime: "2026-06-01T00:00:00Z" } }],
  ];
  const events = await fetchCalendarHistory("uid", { nowMs: NOW, calendar: pagingCalendar(pages, capture) });
  assert.deepEqual(events.map((e) => e.id), ["a", "b", "c"], "no page may be dropped");
  assert.equal(capture.all.length, 3, "one request per page");
  assert.deepEqual(capture.all.map((o) => o.pageToken || null), [null, "p1", "p2"], "the cursor must be fed back");
  for (const call of capture.all) {
    assert.equal(call.strict, true, "every history page is a strict read");
    assert.equal(call.maxResults, HISTORY_PAGE_SIZE, "ask for the largest page the API allows");
  }
});

test("one window, not blind chunks: the cursor walk covers [now - historyMs, now] in a single span", async () => {
  const capture = {};
  await fetchCalendarHistory("uid", { nowMs: NOW, calendar: pagingCalendar([[]], capture) });
  assert.equal(capture.all.length, 1, "a short calendar costs exactly one call");
  assert.equal(capture.all[0].timeMin, new Date(NOW - CARE_HISTORY_MS).toISOString().replace(/\.\d{3}Z$/, "Z"));
  assert.equal(capture.all[0].timeMax, new Date(NOW).toISOString().replace(/\.\d{3}Z$/, "Z"));
});

// 🔴 The two honest failures that replace the silent lie. careUserOnce turns a throw into
// status:"history_unavailable" — no row, no daily claim, the next 60s tick retries. A truncated
// history frozen into an append-only table would be a permanent fabrication.
test("cursor still live at the hard cap → THROWS rather than persisting a partial history as truth", async () => {
  const pages = [];
  const per = HISTORY_PAGE_SIZE;
  for (let p = 0; p < 10; p += 1) pages.push(eventsSpanning(per, `p${p}e`));
  await assert.rejects(
    () => fetchCalendarHistory("uid", { nowMs: NOW, calendar: pagingCalendar(pages) }),
    (e) => /truncat/i.test(e.message) && String(e.message).includes(String(HISTORY_MAX_EVENTS)),
  );
});

test("a cursor-less transport that returns a FULL page → THROWS (truncation with nothing to follow)", async () => {
  const cal = fakeCalendar(eventsSpanning(HISTORY_PAGE_SIZE, "full"));
  await assert.rejects(
    () => fetchCalendarHistory("uid", { nowMs: NOW, calendar: cal }),
    /truncat/i,
  );
  // a short page from the same transport is provably complete and must NOT throw
  const short = await fetchCalendarHistory("uid", { nowMs: NOW, calendar: fakeCalendar(eventsSpanning(3, "s")) });
  assert.equal(short.length, 3);
});

test("a transport that repeats its own cursor cannot spin forever — it THROWS", async () => {
  const cal = {
    kind: "fake-stuck",
    ready: () => true,
    async listEventsPage() {
      return { items: [{ id: "x", summary: "予定", start: { dateTime: "2026-01-01T00:00:00Z" } }], nextPageToken: "same" };
    },
  };
  await assert.rejects(() => fetchCalendarHistory("uid", { nowMs: NOW, calendar: cal }), /cursor/i);
});

test("merged pages never duplicate ids — a repeated event is one visit, not two", async () => {
  const dup = { id: "d1", summary: "散髪", start: { dateTime: "2026-01-05T00:00:00Z" } };
  const events = await fetchCalendarHistory("uid", {
    nowMs: NOW,
    calendar: pagingCalendar([[dup], [dup, { id: "d2", summary: "歯科", start: { dateTime: "2026-02-05T00:00:00Z" } }]]),
  });
  assert.deepEqual(events.map((e) => e.id), ["d1", "d2"]);
});

// The live composio transport must actually expose the cursor the walk depends on.
test("composio transport exposes listEventsPage: returns nextPageToken and forwards pageToken", async () => {
  const { makeComposioCalendar } = require("./transport/calendar-composio.js");
  const origFetch = globalThis.fetch;
  try {
    const sent = [];
    globalThis.fetch = async (_url, init) => {
      sent.push(JSON.parse(init.body).arguments);
      return { json: async () => ({ successful: true, data: { items: [{ id: "e1" }], nextPageToken: "tok2" } }) };
    };
    const cal = makeComposioCalendar({ apiKey: "k", recordCall: () => false });
    const page = await cal.listEventsPage("uid", { timeMin: "2025-01-01T00:00:00Z", timeMax: "2026-07-26T00:00:00Z", maxResults: 2500, pageToken: "tok1", strict: true });
    assert.deepEqual(page.items, [{ id: "e1" }]);
    assert.equal(page.nextPageToken, "tok2");
    assert.equal(sent[0].pageToken, "tok1", "the cursor must reach Composio");
    assert.equal(sent[0].maxResults, 2500);
    // an exhausted cursor reports null, never the empty string Google can send
    globalThis.fetch = async () => ({ json: async () => ({ successful: true, data: { items: [], nextPageToken: "" } }) });
    assert.equal((await cal.listEventsPage("uid", { strict: true })).nextPageToken, null);
    // listEventsRaw keeps its one-page array contract for the wake path
    globalThis.fetch = async () => ({ json: async () => ({ successful: true, data: { items: [{ id: "w" }], nextPageToken: "more" } }) });
    assert.deepEqual(await cal.listEventsRaw("uid", {}), [{ id: "w" }]);
    // strict failure still throws through the paged entry point
    globalThis.fetch = async () => ({ json: async () => ({ successful: false, error: "quota exceeded" }) });
    await assert.rejects(() => cal.listEventsPage("uid", { strict: true }), /quota exceeded/);
    assert.deepEqual(await cal.listEventsPage("uid", {}), { items: [], nextPageToken: null });
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("composio transport pins every Calendar execution to the tenant's selected account", async () => {
  const { makeComposioCalendar } = require("./transport/calendar-composio.js");
  const bodies = [];
  const calendar = makeComposioCalendar({
    apiKey: "k",
    recordCall: () => false,
    resolveConnectedAccountId: async (uid) => uid === "tenant-a" ? "ca-selected-a" : null,
    fetchImpl: async (_url, init) => { bodies.push(JSON.parse(init.body)); return { json: async () => ({ successful: true, data: { items: [] } }) }; },
  });
  await calendar.listEventsRaw("tenant-a");
  await calendar.createEvent("tenant-a", { summary: "fixture" });
  await calendar.patchEvent("tenant-a", { event_id: "fixture" });
  assert.equal(bodies.length, 3);
  assert.ok(bodies.every((body) => body.connected_account_id === "ca-selected-a"));
  assert.ok(bodies.every((body) => body.user_id === "tenant-a"));
});

// 🔴 Finding 1 (transport leg): the history path must distinguish failure from empty. The composio
// wake path keeps its swallow-to-[] (load-bearing there); the history read passes strict and lets a
// transport failure PROPAGATE instead of returning a fake empty calendar.
test("history read is STRICT: passes strict to the transport and propagates its failure", async () => {
  const capture = {};
  await fetchCalendarHistory("uid", { nowMs: NOW, calendar: fakeCalendar([], capture) });
  for (const call of capture.all) assert.equal(call.strict, true, "every history page must be a strict read");
  await assert.rejects(
    () => fetchCalendarHistory("uid", {
      nowMs: NOW,
      calendar: { kind: "fake", ready: () => true, async listEventsRaw() { throw new Error("api down"); } },
    }),
    /api down/,
  );
});

test("composio transport: strict mode throws on API failure; the non-strict wake path still swallows to []", async () => {
  const { makeComposioCalendar } = require("./transport/calendar-composio.js");
  const origFetch = globalThis.fetch;
  const origKey = process.env.COMPOSIO_API_KEY;
  delete process.env.COMPOSIO_API_KEY; // the keyless case below must really be keyless
  try {
    globalThis.fetch = async () => ({ json: async () => ({ successful: false, error: "quota exceeded" }) });
    const cal = makeComposioCalendar({ apiKey: "k", recordCall: () => false });
    assert.deepEqual(await cal.listEventsRaw("uid", { timeMin: "2026-01-01T00:00:00Z", timeMax: "2026-07-26T00:00:00Z" }), [],
      "wake path contract unchanged: failure swallows to []");
    await assert.rejects(
      () => cal.listEventsRaw("uid", { timeMin: "2026-01-01T00:00:00Z", timeMax: "2026-07-26T00:00:00Z", strict: true }),
      /quota exceeded/,
    );
    globalThis.fetch = async () => { throw new Error("network down"); };
    await assert.rejects(
      () => cal.listEventsRaw("uid", { timeMin: "2026-01-01T00:00:00Z", timeMax: "2026-07-26T00:00:00Z", strict: true }),
      /network down/,
    );
    const keyless = makeComposioCalendar({ apiKey: "", recordCall: () => false });
    await assert.rejects(
      () => keyless.listEventsRaw("uid", { strict: true }),
      /key|uid|ready/i,
    );
  } finally {
    globalThis.fetch = origFetch;
    if (origKey !== undefined) process.env.COMPOSIO_API_KEY = origKey;
  }
});

test("keeps all-day (date-only) events — a care visit logged all-day is still a visit", async () => {
  const cal = fakeCalendar([
    { id: "h1", summary: "散髪", start: { date: "2026-06-21" } },
    { id: "h2", summary: "歯科", location: "青山", start: { dateTime: "2026-05-01T09:00:00Z" }, end: { dateTime: "2026-05-01T10:00:00Z" } },
  ]);
  const events = await fetchCalendarHistory("uid", { nowMs: NOW, calendar: cal });
  assert.deepEqual(events.map((e) => e.id), ["h2", "h1"]); // ascending by start
  assert.equal(events[1].summary, "散髪");
  assert.equal(events[0].location, "青山");
  // detectCalendarCare reads event.start.dateTime || event.start.date — the raw start must survive
  assert.deepEqual(events[1].start, { date: "2026-06-21" });
});

test("the END of a timed event survives the projection — a consumer must not have to invent one", async () => {
  // H4's weekly mirror asks "did the block run into the evening this tap is about". Without endMs
  // here every consumer had to fabricate start+1h and then reason as if it were measured.
  const cal = fakeCalendar([
    { id: "t1", summary: "定例", start: { dateTime: "2026-05-01T09:00:00Z" }, end: { dateTime: "2026-05-01T17:30:00Z" } },
    { id: "t2", summary: "散髪", start: { date: "2026-06-21" } },                       // all-day: no end instant
    { id: "t3", summary: "壊れた予定", start: { dateTime: "2026-05-02T09:00:00Z" }, end: { dateTime: "not a time" } },
  ]);
  const events = await fetchCalendarHistory("uid", { nowMs: NOW, calendar: cal });
  const byId = Object.fromEntries(events.map((e) => [e.id, e]));
  assert.equal(byId.t1.endMs, Date.parse("2026-05-01T17:30:00Z"), "a real end must be reported as itself");
  assert.equal(byId.t2.endMs, null, "an all-day event has no end instant, and null says so");
  assert.equal(byId.t3.endMs, null, "an unparseable end is an absent end, never a guessed one");
  // Additive: every key the care callers already read is untouched.
  assert.equal(byId.t1.summary, "定例");
  assert.deepEqual(byId.t2.start, { date: "2026-06-21" });
});

test("drops id-less items and events starting after now — history holds only real past visits", async () => {
  const cal = fakeCalendar([
    { summary: "no-id", start: { dateTime: "2026-05-01T09:00:00Z" } },
    { id: "future", summary: "予約", start: { dateTime: "2026-08-01T09:00:00Z" } },
    { id: "ok", summary: "散髪", start: { dateTime: "2026-05-02T09:00:00Z" } },
  ]);
  const events = await fetchCalendarHistory("uid", { nowMs: NOW, calendar: cal });
  assert.deepEqual(events.map((e) => e.id), ["ok"]);
});

test("history projection preserves attendee identity metadata for the relations adapter", async () => {
  const attendees = [
    { email: "owner@example.com", self: true, responseStatus: "accepted" },
    { email: "mother@example.com", displayName: "母", responseStatus: "accepted" },
  ];
  const organizer = { email: "owner@example.com", self: true };
  const [event] = await fetchCalendarHistory("uid", {
    nowMs: NOW,
    calendar: fakeCalendar([{
      id: "one-to-one",
      summary: "private title",
      start: { dateTime: "2026-06-21T09:00:00Z" },
      end: { dateTime: "2026-06-21T10:00:00Z" },
      attendees,
      organizer,
    }]),
  });
  assert.deepEqual(event.attendees, attendees);
  assert.deepEqual(event.organizer, organizer);
});

test("no uid → [] (mirrors fetchUpcomingEvents' guard)", async () => {
  assert.deepEqual(await fetchCalendarHistory("", { nowMs: NOW, calendar: fakeCalendar([{ id: "x" }]) }), []);
});
