"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createConnpassBrowserProvider,
  readConnpassRegistrationStateOnPage,
  submitConnpassOnPage,
} = require("./connpass-browser-provider.js");

function contract() {
  return {
    tenant_id: "dais-local",
    event_ref: "connpass-event://event/101",
    canonical_url: "https://tokyo-builders.connpass.com/event/101/",
  };
}

function fixture(states) {
  const calls = [];
  const queue = [...states];
  const page = {
    async evaluate() { return queue.shift(); },
    async screenshot(options) { calls.push(["screenshot", options]); return Buffer.from("png"); },
  };
  return {
    calls, page,
    dailyDriver: {
      async withEventPage(provider, url, task) {
        calls.push(["withEventPage", provider, url]);
        return task(page, { tab_owner_receipt: { target_id: "OWNED" } });
      },
    },
    evidenceStore: {
      async record(input) {
        calls.push(["record", input]);
        return {
          external_receipt_ref: "provider-receipt://connpass/registered-101",
          artifact_ref: `object://sha256/${"a".repeat(64)}`,
        };
      },
    },
  };
}

function domFixture({ pathname, bodyText, controls = [], statusMessages = [] }) {
  return {
    async evaluate(callback) {
      const previousLocation = globalThis.location;
      const previousDocument = globalThis.document;
      globalThis.location = { pathname };
      globalThis.document = {
        body: { innerText: bodyText },
        querySelectorAll(selector) {
          return selector === ".status_message" ? statusMessages : controls;
        },
      };
      try {
        return await callback();
      } finally {
        if (previousLocation === undefined) delete globalThis.location;
        else globalThis.location = previousLocation;
        if (previousDocument === undefined) delete globalThis.document;
        else globalThis.document = previousDocument;
      }
    },
  };
}

test("join-page attendee-section text does not impersonate pending registration", async () => {
  const page = domFixture({
    pathname: "/event/400028/join/",
    bodyText: "参加枠\n補欠者\n補欠者はいません\n申し込みを確定する",
    controls: [{ innerText: "申し込みを確定する", value: "", getAttribute() { return null; } }],
  });
  assert.deepEqual(await readConnpassRegistrationStateOnPage(page), { state: "unknown" });
});

test("exact Connpass join completion path is registered without relying on page copy", async () => {
  assert.deepEqual(await readConnpassRegistrationStateOnPage(domFixture({
    pathname: "/event/403786/join/complete/",
    bodyText: "イベントへの申し込みが完了しました",
  })), { state: "registered" });
  for (const pathname of [
    "/event/403786/join/complete",
    "/event/0/join/complete/",
    "/events/403786/join/complete/",
    "/event/403786/join/complete/extra",
  ]) assert.deepEqual(await readConnpassRegistrationStateOnPage(domFixture({ pathname, bodyText: "イベントへの申し込みが完了しました" })), { state: "unknown" }, pathname);
});

test("pending requires an exact visible line on the canonical event page", async () => {
  const page = domFixture({ pathname: "/event/400028/", bodyText: "参加状況\n補欠\n" });
  assert.deepEqual(await readConnpassRegistrationStateOnPage(page), { state: "pending" });
});

test("current Connpass lottery controls identify an authenticated pending registration", async () => {
  const control = (innerText) => ({ innerText, value: "", getAttribute() { return null; } });
  const page = domFixture({
    pathname: "/event/404826/",
    bodyText: "募集内容\n参加は 2026/09/10 に確定されます\n申し込みキャンセル",
    controls: [control("申し込みキャンセル")],
    statusMessages: [control("参加は 2026/09/10 に確定されます")],
  });
  assert.deepEqual(await readConnpassRegistrationStateOnPage(page), { state: "pending" });
});

test("pending markers are rejected for substring-only and non-canonical paths", async () => {
  for (const pathname of ["/event/400028/", "/event/0/", "/event/400028", "/event/400028/join/", "/events/400028/"]) {
    const bodyText = pathname === "/event/400028/" ? "補欠者" : "補欠";
    const page = domFixture({ pathname, bodyText });
    assert.deepEqual(await readConnpassRegistrationStateOnPage(page), { state: "unknown" }, pathname);
  }
});

test("parent readback separates login, absence, unavailable, pending, and registered", async () => {
  for (const [observed, expected] of [
    [{ state: "login_required" }, "login_required"],
    [{ state: "absent" }, "absent"],
    [{ state: "unavailable", reason: "closed" }, "unavailable"],
  ]) {
    const fx = fixture([observed]);
    assert.equal((await createConnpassBrowserProvider(fx).inspectRegistration(contract())).state, expected);
    assert.equal(fx.calls.some(([name]) => name === "record"), false);
  }
  for (const state of ["pending", "registered"]) {
    const fx = fixture([{ state }]);
    const proof = await createConnpassBrowserProvider({
      ...fx, now: () => "2026-08-06T01:02:03.000Z",
    }).inspectRegistration(contract());
    assert.equal(proof.state, "registered");
    assert.equal(proof.registration_status, state);
    assert.match(proof.artifact_ref, /^object:\/\/sha256\//);
  }
});

// Fake join-page built from the real DOM structure captured live from
// https://hfs.connpass.com/event/398207/join/:
//   input[name="participation_type"] (radio, id="ptype1")
//   button#FreeButton ("申し込みを確定する")
const EVENT_PAGE_URL = "https://hfs.connpass.com/event/398207/";

// The default single qualifying tier — keeps every pre-existing flow test
// (single-tier events) selecting index 0, exactly as before this change.
const SOLE_TIER = [{ label: "参加 無料 先着順 5/10人", disabled: false }];

// The five real tiers read live from
// https://mobilus.connpass.com/event/395464/join/ (see task brief). Correct
// pick is ptype2 (index 1): だれでも枠, free, room (17/20), unrestricted, in-person.
const MULTI_TIER_EVENT = [
  { label: "学生ゆうせん枠 無料 先着順 9/15人", disabled: false },
  { label: "だれでも枠 無料 先着順 17/20人", disabled: false },
  { label: "26新卒LT枠 無料 先着順 2/2人", disabled: false },
  { label: "【招待した方のみ】LT枠 無料 先着順 3/3人", disabled: false },
  { label: "オンライン視聴枠 (Google meet) 無料 参加者数 3人", disabled: false },
];

// Fake organizer-questionnaire `.question_list` group, shaped after the real
// DOM measured on Connpass join pages (see
// docs/superpowers/plans/2026-08-11-connector-connpass-real-dom-observation-14e.md
// and connector-production-browser-harness.js's connpassJoin handling):
// `.question_list > .question` holds the `必須`/`任意` marker, and the
// answer field(s) — radio/checkbox/input/textarea/select — live in the same
// group. `hasParticipationType` simulates the tier group sharing the same
// container structure, which must be excluded from questionnaire detection.
function questionnaireGroupFixture({
  hasParticipationType = false,
  questionText = "必須 このイベントは何を見て知りましたか？",
  fields = [],
} = {}) {
  return {
    querySelector(selector) {
      if (selector === 'input[name="participation_type"]') return hasParticipationType ? {} : null;
      if (selector === ":scope > .question") return { textContent: questionText };
      return null;
    },
    querySelectorAll(selector) {
      return selector === "input,textarea,select" ? fields : [];
    },
  };
}

function joinFlowFixture({
  tiers = SOLE_TIER, confirmCount = 1, confirmVisible = true,
  confirmLabel = "申し込みを確定する", confirmClickError = null, states,
  eventUrl = EVENT_PAGE_URL, gotoError = null, questionnaireGroups = [],
} = {}) {
  const calls = [];
  const joinLink = {
    first() { return this; },
    async count() { return 1; },
    async isVisible() { return true; },
    async click() { calls.push("click-join"); },
  };
  const radioLocator = {
    async count() { return tiers.length; },
    async evaluateAll() { return tiers.map((tier) => ({ disabled: !!tier.disabled, label: tier.label })); },
    nth(index) {
      return { async check() { calls.push(`check-radio:${index}`); } };
    },
  };
  const confirmLocator = {
    async count() { return confirmCount; },
    async isVisible() { return confirmVisible; },
    async innerText() { return confirmLabel; },
    async click() {
      calls.push("click-confirm");
      if (confirmClickError) throw confirmClickError;
    },
  };
  const questionnaireLocator = {
    async evaluateAll(callback, arg) { return callback(questionnaireGroups, arg); },
  };
  const stateQueue = [...(states || [])];
  const page = {
    calls,
    url() { calls.push("url"); return eventUrl; },
    async goto(url) {
      calls.push(`goto:${url}`);
      if (gotoError) throw gotoError;
    },
    getByRole(role, options) {
      assert.match(role, /link|button/); assert.equal(options.exact, true);
      assert.equal(options.name.test("このイベントに申し込む"), true);
      return joinLink;
    },
    locator(selector) {
      if (selector === 'input[name="participation_type"]') return radioLocator;
      if (selector === "button#FreeButton") return confirmLocator;
      if (selector === ".question_list") return questionnaireLocator;
      throw new Error(`unexpected selector ${selector}`);
    },
    async waitForTimeout() { calls.push("wait"); },
    async evaluate() { return stateQueue.shift(); },
  };
  return page;
}

test("submit clicks join link, selects the sole participation radio on a single-tier event, then confirms, then navigates back to the event page before reading the result — in order", async () => {
  const page = joinFlowFixture({ states: [{ state: "absent" }, { state: "registered" }] });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("on a multi-tier event, selects the first free open unrestricted in-person tier (ptype2, index 1), skipping student/full/invite/LT/online tiers", async () => {
  const page = joinFlowFixture({
    tiers: MULTI_TIER_EVENT, states: [{ state: "absent" }, { state: "registered" }],
  });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:1", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("an online-only event fails closed before the confirmation click", async () => {
  const page = joinFlowFixture({
    tiers: [
      { label: "学生ゆうせん枠 無料 先着順 9/15人", disabled: false },
      MULTI_TIER_EVENT[4], // オンライン視聴枠 (Google meet) 無料 参加者数 3人
    ],
    states: [{ state: "absent" }, { state: "registered" }],
  });
  await assert.rejects(
    submitConnpassOnPage(page),
    (error) => error.code === "CONNPASS_TIER_UNAVAILABLE" && error.unknownEffect === false,
  );
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("a non-general or unlabeled free tier fails closed before the confirmation click", async () => {
  const page = joinFlowFixture({
    tiers: [
      { label: "女性参加枠 無料 先着順 1/10人", disabled: false },
      { label: "一般参加・会員限定 無料 先着順 1/10人", disabled: false },
      { label: "一般参加 limited 無料 1/10人", disabled: false },
      { label: "Small 無料 先着順 1/10人", disabled: false },
      { label: "無料 先着順 1/10人", disabled: false },
    ],
    states: [{ state: "absent" }],
  });
  await assert.rejects(
    submitConnpassOnPage(page),
    (error) => error.code === "CONNPASS_TIER_UNAVAILABLE" && error.unknownEffect === false,
  );
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("a tier showing a yen amount is never selected, even when it is otherwise open and unrestricted", async () => {
  const page = joinFlowFixture({
    tiers: [
      { label: "一般参加 ¥3,000 先着順 1/10人", disabled: false },
      { label: "懇親会付き 5000円 先着順 1/5人", disabled: false },
      { label: "だれでも枠 無料 先着順 1/10人", disabled: false },
    ],
    states: [{ state: "absent" }, { state: "registered" }],
  });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:2", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("when every tier is full or restricted, submit clicks nothing and fails closed with CONNPASS_TIER_UNAVAILABLE", async () => {
  const page = joinFlowFixture({
    tiers: [
      MULTI_TIER_EVENT[0], // 学生ゆうせん枠 (restricted)
      MULTI_TIER_EVENT[2], // 26新卒LT枠 (restricted + full 2/2)
      MULTI_TIER_EVENT[3], // 招待した方のみ (restricted + full 3/3)
    ],
    states: [{ state: "absent" }],
  });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_TIER_UNAVAILABLE");
    assert.equal(error.unknownEffect, false);
    return true;
  });
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("a required unanswered organizer questionnaire clicks nothing and fails closed with CONNPASS_QUESTIONNAIRE_REQUIRED", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    questionnaireGroups: [questionnaireGroupFixture({
      fields: [{ tagName: "INPUT", type: "radio", name: "q1", checked: false, disabled: false }],
    })],
  });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_QUESTIONNAIRE_REQUIRED");
    assert.equal(error.unknownEffect, false);
    return true;
  });
  // Nothing on the join page itself (radio check, confirm click) may have happened.
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("an optional-only organizer questionnaire does not block registration", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }, { state: "registered" }],
    questionnaireGroups: [questionnaireGroupFixture({
      questionText: "任意 ご質問があればどうぞ",
      fields: [{ tagName: "TEXTAREA", type: "textarea", name: "q2", value: "", disabled: false }],
    })],
  });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("a required organizer questionnaire question that already has an answer does not block registration", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }, { state: "registered" }],
    questionnaireGroups: [questionnaireGroupFixture({
      fields: [{ tagName: "SELECT", type: "select-one", name: "q3", value: "Connpass", disabled: false }],
    })],
  });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

// Placeholder identity values only — never real personal data in a test
// fixture (Dais's actual name/affiliation come from
// ~/.config/anicca/job-search/profile.json / DAIS_LEGAL_NAME_ROMAJI at
// runtime, injected here via the same `dependencies` DI point `readState`
// already uses).
const PLACEHOLDER_IDENTITY = Object.freeze({ name: "Placeholder Taro", affiliation: "Placeholder University" });

test("required 氏名 and 所属 questionnaire fields are filled from identity and confirm proceeds", async () => {
  const nameField = { tagName: "INPUT", type: "text", name: "q_name", value: "", disabled: false };
  const affiliationField = { tagName: "INPUT", type: "text", name: "q_affiliation", value: "", disabled: false };
  const page = joinFlowFixture({
    states: [{ state: "absent" }, { state: "registered" }],
    questionnaireGroups: [
      questionnaireGroupFixture({ questionText: "必須 氏名", fields: [nameField] }),
      questionnaireGroupFixture({ questionText: "必須 所属学校名or会社名", fields: [affiliationField] }),
    ],
  });
  assert.deepEqual(
    await submitConnpassOnPage(page, undefined, { identity: PLACEHOLDER_IDENTITY }),
    { status: "registered", effect_started: true },
  );
  assert.equal(nameField.value, "Placeholder Taro");
  assert.equal(affiliationField.value, "Placeholder University");
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("a required radio question (e.g. the slide-sharing commitment) is skipped with nothing clicked, even with identity available", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    questionnaireGroups: [questionnaireGroupFixture({
      questionText: "必須 26新卒LT枠・招待LT枠について：当日スライドファイル（GoogleスライドやPDFなど）をご共有いただき、弊社の用意するPCにて投影いたします。",
      fields: [
        { tagName: "INPUT", type: "radio", name: "q_slide", value: "共有できる", checked: false, disabled: false },
        { tagName: "INPUT", type: "radio", name: "q_slide", value: "共有できない", checked: false, disabled: false },
      ],
    })],
  });
  await assert.rejects(submitConnpassOnPage(page, undefined, { identity: PLACEHOLDER_IDENTITY }), (error) => {
    assert.equal(error.code, "CONNPASS_QUESTIONNAIRE_REQUIRED");
    assert.equal(error.unknownEffect, false);
    return true;
  });
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

// Proof that a commitment-style control can never be answered: this radio's
// own group question is literally "必須 氏名" — the exact label the name
// policy matches — but the control itself is a radio (a choice/commitment
// control), so the control-type gate in planConnpassQuestionnaire must
// reject it regardless of the label. If this ever passed, it would mean a
// commitment question got silently pre-answered because its wording
// happened to resemble a factual field.
test("label match alone never answers a radio/checkbox/select — control type gates before label", async () => {
  const radioField = { tagName: "INPUT", type: "radio", name: "q_commit", checked: false, disabled: false };
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    questionnaireGroups: [questionnaireGroupFixture({
      questionText: "必須 氏名",
      fields: [radioField],
    })],
  });
  await assert.rejects(submitConnpassOnPage(page, undefined, { identity: PLACEHOLDER_IDENTITY }), (error) => {
    assert.equal(error.code, "CONNPASS_QUESTIONNAIRE_REQUIRED");
    return true;
  });
  assert.equal(radioField.checked, false);
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("one answerable and one unanswerable required question together are skipped with nothing filled and nothing clicked", async () => {
  const nameField = { tagName: "INPUT", type: "text", name: "q_name", value: "", disabled: false };
  const radioField = { tagName: "INPUT", type: "radio", name: "q_slide", checked: false, disabled: false };
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    questionnaireGroups: [
      questionnaireGroupFixture({ questionText: "必須 氏名", fields: [nameField] }),
      questionnaireGroupFixture({
        questionText: "必須 26新卒LT枠・招待LT枠について：当日スライドファイルをご共有いただき投影いたします。",
        fields: [radioField],
      }),
    ],
  });
  await assert.rejects(submitConnpassOnPage(page, undefined, { identity: PLACEHOLDER_IDENTITY }), (error) => {
    assert.equal(error.code, "CONNPASS_QUESTIONNAIRE_REQUIRED");
    return true;
  });
  assert.equal(nameField.value, "");
  assert.equal(radioField.checked, false);
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("an optional-only organizer questionnaire is unchanged even when identity data is available", async () => {
  const optionalField = { tagName: "TEXTAREA", type: "textarea", name: "q_opt", value: "", disabled: false };
  const page = joinFlowFixture({
    states: [{ state: "absent" }, { state: "registered" }],
    questionnaireGroups: [questionnaireGroupFixture({
      questionText: "任意 ご質問があればどうぞ",
      fields: [optionalField],
    })],
  });
  assert.deepEqual(
    await submitConnpassOnPage(page, undefined, { identity: PLACEHOLDER_IDENTITY }),
    { status: "registered", effect_started: true },
  );
  assert.equal(optionalField.value, "");
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("an unrecognized required free-text label is never guessed at, even with identity available", async () => {
  const unknownField = { tagName: "INPUT", type: "text", name: "q_unknown", value: "", disabled: false };
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    questionnaireGroups: [questionnaireGroupFixture({
      questionText: "必須 好きな技術スタックを教えてください",
      fields: [unknownField],
    })],
  });
  await assert.rejects(submitConnpassOnPage(page, undefined, { identity: PLACEHOLDER_IDENTITY }), (error) => {
    assert.equal(error.code, "CONNPASS_QUESTIONNAIRE_REQUIRED");
    return true;
  });
  assert.equal(unknownField.value, "");
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("a .question_list group wrapping the participation-type tier radios is not treated as an organizer questionnaire", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }, { state: "registered" }],
    questionnaireGroups: [questionnaireGroupFixture({
      hasParticipationType: true,
      questionText: "必須 参加枠",
      fields: [{ tagName: "INPUT", type: "radio", name: "participation_type", checked: false, disabled: false }],
    })],
  });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("no questionnaire on the page (the default fixture) behaves exactly as before this change", async () => {
  const page = joinFlowFixture({ states: [{ state: "absent" }, { state: "registered" }] });
  assert.deepEqual(await submitConnpassOnPage(page), { status: "registered", effect_started: true });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("already-registered/pending state returns immediately and clicks nothing", async () => {
  for (const state of ["registered", "pending"]) {
    const page = joinFlowFixture({ states: [] });
    const readState = async () => ({ state });
    assert.deepEqual(
      await submitConnpassOnPage(page, undefined, { readState }),
      { status: state, effect_started: false },
    );
    assert.deepEqual(page.calls, []);
  }
});

test("a login-wall join page fails closed with CONNPASS_SESSION_EXPIRED, clicking nothing", async () => {
  const page = joinFlowFixture({ states: [{ state: "login_required" }] });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_SESSION_EXPIRED");
    assert.equal(error.unknownEffect, false);
    return true;
  });
  // Thrown straight off the readState check — before the join link itself
  // is ever located, let alone clicked.
  assert.deepEqual(page.calls, []);
});

test("a genuinely unavailable (closed/full) event is never reported as a session problem", async () => {
  const page = joinFlowFixture({ states: [{ state: "unavailable", reason: "closed" }] });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_REGISTRATION_UNAVAILABLE");
    assert.equal(error.unknownEffect, false);
    return true;
  });
  assert.deepEqual(page.calls, []);
});

test("missing or duplicated confirm control fails closed before any click on the join page", async () => {
  for (const overrides of [{ confirmCount: 0 }, { confirmCount: 2 }, { confirmVisible: false }, { confirmLabel: "支払いに進む" }]) {
    const page = joinFlowFixture({ states: [{ state: "absent" }], ...overrides });
    await assert.rejects(submitConnpassOnPage(page), (error) => {
      assert.equal(error.code, "CONNPASS_CONFIRM_UNAVAILABLE");
      assert.equal(error.unknownEffect, false);
      return true;
    });
    // join-link click (navigation only) may have happened; nothing on the
    // join page itself (radio check, confirm click) may have.
    assert.equal(page.calls.some((call) => call.startsWith("check-radio")), false);
    assert.equal(page.calls.includes("click-confirm"), false);
  }
});

test("missing participation-type radio fails closed before the confirm control is touched", async () => {
  const page = joinFlowFixture({ states: [{ state: "absent" }], tiers: [] });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_CONTROL_UNAVAILABLE");
    assert.equal(error.unknownEffect, false);
    return true;
  });
  assert.deepEqual(page.calls, ["url", "click-join", "wait"]);
});

test("a failure after the confirm click is always reported as an unknown effect", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    confirmClickError: Object.assign(new Error("connpass 500"), { unknownEffect: false, code: "CONNPASS_READBACK_UNAVAILABLE" }),
  });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_BROWSER_ACTION_FAILED");
    assert.equal(error.unknownEffect, true);
    return true;
  });
  assert.deepEqual(page.calls, ["url", "click-join", "wait", "check-radio:0", "click-confirm"]);
});

test("a failure navigating back to the event page after confirming is still reported as an unknown effect", async () => {
  const page = joinFlowFixture({
    states: [{ state: "absent" }],
    gotoError: new Error("navigation timeout"),
  });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_BROWSER_ACTION_FAILED");
    assert.equal(error.unknownEffect, true);
    return true;
  });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("a failure reading state back on the event page after confirming is still reported as an unknown effect", async () => {
  // Only one state is queued (the pre-check "absent"); the post-navigation
  // readState call finds the queue empty, which readConnpassRegistrationStateOnPage
  // treats as CONNPASS_READBACK_UNAVAILABLE (unknownEffect: false) on its own —
  // proving the post-click boundary overrides that classification too.
  const page = joinFlowFixture({ states: [{ state: "absent" }] });
  await assert.rejects(submitConnpassOnPage(page), (error) => {
    assert.equal(error.code, "CONNPASS_BROWSER_ACTION_FAILED");
    assert.equal(error.unknownEffect, true);
    return true;
  });
  assert.deepEqual(page.calls, [
    "url", "click-join", "wait", "check-radio:0", "click-confirm", "wait", `goto:${EVENT_PAGE_URL}`,
  ]);
});

test("pre-click failures are known and post-click uncertainty is unknown", async () => {
  const known = fixture([{ state: "login_required" }]);
  await assert.rejects(createConnpassBrowserProvider(known).submitRegistration(contract()), (error) => {
    assert.equal(error.unknownEffect, false); return true;
  });

  const unknown = fixture([{ state: "absent" }]);
  unknown.page.getByRole = () => ({
    first() { return this; }, async count() { return 1; }, async isVisible() { return true; }, async click() {},
  });
  unknown.page.waitForTimeout = async () => {};
  await assert.rejects(createConnpassBrowserProvider({
    ...unknown, submitOnPage: async () => ({ status: "unknown", effect_started: true }),
  }).submitRegistration(contract()), (error) => {
    assert.equal(error.unknownEffect, true); return true;
  });
});
