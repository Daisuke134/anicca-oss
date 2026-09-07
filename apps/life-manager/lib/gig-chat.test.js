// lib/gig-chat.test.js — sibling of investment-chat.js's coverage, for the Coconala/CrowdWorks
// marketplace lane replies.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  COCONALA_SIGNUP_URL,
  CROWDWORKS_SIGNUP_URL,
  CROWDWORKS_VERIFICATION_URL,
  normalizeGigLifecycle,
  validGigSnapshot,
  buildGigReply,
  telegramExtra,
} = require("./gig-chat.js");

test("normalizeGigLifecycle recognises the known lifecycles and defaults anything else to unknown", () => {
  assert.equal(normalizeGigLifecycle("setup_required"), "setup_required");
  assert.equal(normalizeGigLifecycle("VERIFICATION_REQUIRED"), "verification_required");
  assert.equal(normalizeGigLifecycle("active"), "active");
  assert.equal(normalizeGigLifecycle("unknown"), "unknown");
  assert.equal(normalizeGigLifecycle("in_review"), "unknown", "investment-only lifecycles are not gig lifecycles");
  assert.equal(normalizeGigLifecycle(undefined), "unknown");
  assert.equal(normalizeGigLifecycle(null), "unknown");
  assert.equal(normalizeGigLifecycle(42), "unknown");
});

test("validGigSnapshot accepts a bare lifecycle and an optional well-formed stats block, rejects the rest", () => {
  assert.equal(validGigSnapshot({ lifecycle: "setup_required" }), true);
  assert.equal(validGigSnapshot({ lifecycle: "active", stats: { listingCount: 3, lastWakeCompleted: true } }), true);
  assert.equal(validGigSnapshot({ lifecycle: "active", stats: { listingCount: 0 } }), true, "lastWakeCompleted is optional");
  assert.equal(validGigSnapshot(null), false);
  assert.equal(validGigSnapshot([]), false);
  assert.equal(validGigSnapshot({ lifecycle: "not-a-real-lifecycle" }), false);
  assert.equal(validGigSnapshot({ lifecycle: "active", stats: null }), false);
  assert.equal(validGigSnapshot({ lifecycle: "active", stats: { listingCount: -1 } }), false);
  assert.equal(validGigSnapshot({ lifecycle: "active", stats: { listingCount: "3" } }), false);
  assert.equal(validGigSnapshot({ lifecycle: "active", stats: { listingCount: 3, lastWakeCompleted: "yes" } }), false);
});

test("buildGigReply rejects an unknown platform rather than defaulting to one", () => {
  assert.throws(() => buildGigReply("fiverr", { lifecycle: "setup_required" }), /Unknown gig platform/);
  assert.throws(() => buildGigReply("", { lifecycle: "setup_required" }), /Unknown gig platform/);
  assert.throws(() => buildGigReply(undefined, { lifecycle: "setup_required" }), /Unknown gig platform/);
});

test("setup_required carries the right platform's signup URL and a distinct reply per platform", () => {
  const coconala = buildGigReply("coconala", { lifecycle: "setup_required" });
  const crowdworks = buildGigReply("crowdworks", { lifecycle: "setup_required" });
  assert.match(coconala.text, /ココナラ/);
  assert.match(crowdworks.text, /CrowdWorks/);
  assert.notEqual(coconala.text, crowdworks.text);
  const coconalaUrl = coconala.presentation.blocks[0].buttons[0].url;
  const crowdworksUrl = crowdworks.presentation.blocks[0].buttons[0].url;
  assert.equal(coconalaUrl, COCONALA_SIGNUP_URL);
  assert.equal(crowdworksUrl, CROWDWORKS_SIGNUP_URL);
  assert.notEqual(coconalaUrl, crowdworksUrl);
});

// This is the state CrowdWorks is reported to be in right now (per the task brief, our worker
// profile at https://crowdworks.jp/public/employees/7145638 shows 本人確認 未提出), so the
// verification URL must be the actual identity-verification page, never the signup page reused for
// an account that already exists.
test("verification_required for crowdworks links to the identity-verification page, not signup", () => {
  const reply = buildGigReply("crowdworks", { lifecycle: "verification_required" });
  assert.match(reply.text, /本人確認/);
  const url = reply.presentation.blocks[0].buttons[0].url;
  assert.equal(url, CROWDWORKS_VERIFICATION_URL);
  assert.notEqual(url, CROWDWORKS_SIGNUP_URL);
});

test("verification_required for coconala names the situation honestly and does not claim a dedicated verification link", () => {
  const reply = buildGigReply("coconala", { lifecycle: "verification_required" });
  assert.match(reply.text, /本人確認/);
  const button = reply.presentation.blocks[0].buttons[0];
  // No dedicated Coconala identity-verification URL exists in this slice; the fallback is the one
  // known Coconala URL, and the button label must not overclaim it is a direct verification link.
  assert.equal(button.url, COCONALA_SIGNUP_URL);
  assert.doesNotMatch(button.label, /本人確認/);
});

test("active reports only numbers actually present in the snapshot, never an invented count", () => {
  const reply = buildGigReply("coconala", { lifecycle: "active", stats: { listingCount: 7, lastWakeCompleted: true } });
  assert.match(reply.text, /7/);
  assert.match(reply.text, /完了/);
  const numbers = reply.text.match(/\d+/g) || [];
  assert.deepEqual(numbers, ["7"], "no number besides the injected listingCount may appear");
});

test("active with missing stats says the count could not be read yet, and invents nothing", () => {
  const reply = buildGigReply("crowdworks", { lifecycle: "active" });
  assert.match(reply.text, /読み取れていません/);
  assert.equal(/\d/.test(reply.text), false, "no digits when there is no stats block to read them from");
});

test("unknown lifecycle says the state could not be read and contains no invented count", () => {
  for (const snapshot of [{ lifecycle: "unknown" }, {}, { lifecycle: "bogus" }, { lifecycle: 123 }]) {
    const reply = buildGigReply("coconala", snapshot);
    assert.match(reply.text, /確認できません/);
    assert.equal(/\d/.test(reply.text), false, "unknown state must not contain a fabricated number");
    assert.equal(reply.presentation, undefined, "unknown state gets no signup/verification button");
  }
});

test("telegramExtra is the shared, generic investment-chat helper — buttons render for gig replies too", () => {
  const reply = buildGigReply("coconala", { lifecycle: "setup_required" });
  const extra = telegramExtra(reply);
  assert.equal(extra.reply_markup.inline_keyboard[0][0].url, COCONALA_SIGNUP_URL);
  assert.equal(extra.reply_markup.inline_keyboard[0][1].callback_data, "coconala:later");

  const noButtons = telegramExtra(buildGigReply("coconala", { lifecycle: "active", stats: { listingCount: 1 } }));
  assert.equal(noButtons.reply_markup, undefined);
});
