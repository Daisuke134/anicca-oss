// lib/user-selector.test.js — C4 RED. The wake user-selection filter must include BOTH Composio and
// Pipedream-provisioned users, at BOTH selection sites (batch scan scheduler.js:42 AND getUserByUid
// refetch scheduler.js:280), else a Pipedream user is picked in the batch but re-excluded on refetch.
// Extract the filter into ONE pure function so both sites share it (SSOT).
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { calendarProviderFilter, schedulerCohortFilter, isCallablePhone, WAKE_CALENDAR_PROVIDERS } = require("./user-selector.js");

test("providers include composio_gcal AND pipedream_gcal", () => {
  assert.ok(WAKE_CALENDAR_PROVIDERS.includes("composio_gcal"));
  assert.ok(WAKE_CALENDAR_PROVIDERS.includes("pipedream_gcal"));
});

test("isCallablePhone accepts only stored E.164 strings", () => {
  for (const value of ["+819012345678", "+14155552671"]) assert.equal(isCallablePhone(value), true, value);
  for (const value of [null, undefined, "", "  +819012345678", "+81 (90) 1234-5678", "819012345678", 819012345678, "+123"]) {
    assert.equal(isCallablePhone(value), false, String(value));
  }
});

test("calendarProviderFilter: PostgREST in.() over both providers, not eq.composio_gcal", () => {
  const f = calendarProviderFilter();
  assert.equal(f, "calendar_provider=in.(composio_gcal,pipedream_gcal)");
  assert.equal(f.includes("eq.composio_gcal"), false); // the old exclusive filter is gone
});

test("scheduler cohort keeps every connected tenant; allowance is enforced at paid-provider boundary", () => {
  assert.equal(schedulerCohortFilter(), "calendar_provider=in.(composio_gcal,pipedream_gcal)");
  assert.equal(schedulerCohortFilter({ LM_COMP_UNTIL: "expired" }, Number.NaN), calendarProviderFilter());
});

test("scheduler.js uses the shared filter at BOTH sites (exactly 2), no lingering eq.composio_gcal — FIND-007", () => {
  const src = fs.readFileSync(path.join(__dirname, "../scheduler.js"), "utf8");
  // no hardcoded exclusive filter remains anywhere
  assert.equal((src.match(/calendar_provider=eq\.composio_gcal/g) || []).length, 0);
  // the shared helper is INTERPOLATED (inside a template literal) at BOTH selection sites → exactly 2
  const uses = (src.match(/\$\{schedulerCohortFilter\(\)\}/g) || []).length;
  assert.equal(uses, 2, `expected schedulerCohortFilter() at both selector sites, found ${uses}`);
  // and it is imported
  assert.ok(/require\(["']\.\/lib\/user-selector\.js["']\)/.test(src));
});
test("scheduler batch and uid selectors execute the same shared cohort contract", async () => {
  const oldUrl = process.env.SUPABASE_URL;
  const oldKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const oldFetch = global.fetch;
  process.env.SUPABASE_URL = "https://example.test";
  process.env.SUPABASE_SERVICE_ROLE_KEY = "synthetic-key";
  const urls = [];
  global.fetch = async (url) => {
    urls.push(String(url));
    return { ok: true, json: async () => [{ uid: "synthetic-user", phone: null, paid: true, calendar_provider: "composio_gcal" }] };
  };
  try {
    const { listPaidUsers, getUserByUid } = require("../scheduler.js");
    const listed = await listPaidUsers();
    const reloaded = await getUserByUid("synthetic-user");
    assert.equal(listed[0].phone, null, "cohort includes paid phone-less users for reminder/travel organs");
    assert.equal(reloaded.phone, null, "uid reload preserves phone-less cohort membership");
  } finally {
    global.fetch = oldFetch;
    if (oldUrl === undefined) delete process.env.SUPABASE_URL; else process.env.SUPABASE_URL = oldUrl;
    if (oldKey === undefined) delete process.env.SUPABASE_SERVICE_ROLE_KEY; else process.env.SUPABASE_SERVICE_ROLE_KEY = oldKey;
  }
  assert.equal(urls.length, 4);
  const cohortUrls = urls.filter(value => new URL(value).pathname.endsWith("/lm_users"));
  assert.equal(cohortUrls.length, 2);
  for (const value of cohortUrls) {
    const url = new URL(value);
    assert.equal(url.searchParams.get("phone"), null, "phone is optional for reminder/travel cohort");
    assert.equal(url.searchParams.get("paid"), null);
    assert.equal(url.searchParams.get("or"), null, "trial expiry must not remove Calendar/Telegram access");
    assert.equal(url.searchParams.get("calendar_provider"), "in.(composio_gcal,pipedream_gcal)");
  }
  assert.equal(urls.filter(value => new URL(value).pathname.endsWith("/lm_panel_preferences")).length, 2);
});
