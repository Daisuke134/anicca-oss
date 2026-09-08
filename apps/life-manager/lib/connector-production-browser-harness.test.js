"use strict";

const assert = require("node:assert/strict");
const { mock } = require("node:test");
const test = require("node:test");

const {
  createBoundedActionProposer,
  createPrivateValueResolver,
  createLumaPrivateValueResolver,
  createProductionBrowserHarness,
  inspectPageControls,
  operatePageControl,
} = require("./connector-production-browser-harness.js");
const { createBrowserHarnessAdapter } = require("./connector-browser-harness-adapter.js");

test("bounded proposer requests one structured action from Terra with sanitized controls only", async () => {
  let request;
  const controller = new AbortController();
  const proposer = createBoundedActionProposer({
    repoRoot: "/private/repo",
    evidenceDir: "/private/evidence",
    async runAgentRunner(input) {
      request = input;
      return {
        summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" },
        value: { control: "register_button" },
      };
    },
  });
  const action = await proposer({
    provider: "luma",
    page_websocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1",
    target_id: "OWNEDTARGET1",
    expected_state: "registered_or_pending",
    step: 2,
    signal: controller.signal,
    observation: {
      state: "registration_page",
      controls: [{ control: "register_button", kind: "button", label: "Register", required: false, submittable: true }],
    },
  });
  assert.deepEqual(action, { control: "register_button" });
  assert.equal(request.taskClass, "browser-lane-agent");
  assert.equal(request.timeoutMs, 30_000);
  assert.equal(request.signal, controller.signal);
  assert.equal(request.readOnly, true);
  assert.equal(request.tokenBudget, 24_576);
  assert.equal(request.budgetScopeId, "connector-step-luma-OWNEDTARGET1-1-2");
  assert.match(request.prompt, /one browser action/i);
  assert.match(request.prompt, /register_button/);
  assert.doesNotMatch(request.prompt, /purpose|method/i);
  assert.deepEqual(Object.keys(request.schema.properties), ["control"]);
  assert.deepEqual(request.schema.required, ["control"]);
  assert.doesNotMatch(request.prompt, /private-phone|cookie|password/i);
  assert.equal(JSON.stringify(request).includes("page_websocket"), false);
  assert.equal(JSON.stringify(request).includes("ws://"), false);
  for (const stepTokenBudget of [0, 1_000_001]) {
    assert.throws(() => createBoundedActionProposer({
      repoRoot: "/private/repo", evidenceDir: "/private/evidence", stepTokenBudget,
      async runAgentRunner() {},
    }), /Connector production Browser Harness invalid/);
  }
});

test("bounded proposer admits only one exact configured extension token", async () => {
  let request;
  const proposer = createBoundedActionProposer({
    repoRoot: "/private/repo",
    evidenceDir: "/private/evidence",
    extensionProvider: "extension-site",
    async runAgentRunner(input) {
      request = input;
      return {
        summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" },
        value: { control: "register_button" },
      };
    },
  });
  const baseInput = {
    target_id: "OWNEDTARGET1",
    expected_state: "registered_or_pending",
    step: 1,
    observation: {
      state: "registration_page",
      controls: [{ control: "register_button", kind: "button", label: "Register", required: false, submittable: true }],
    },
  };
  assert.deepEqual(await proposer({ ...baseInput, provider: "extension-site" }), { control: "register_button" });
  assert.match(request.prompt, /extension-site/i);
  assert.doesNotMatch(JSON.stringify(request), /event_ref|private-phone|private@example|cookie|password|ws:\/\//i);
  await assert.rejects(() => proposer({ ...baseInput, provider: "another-extension" }), /Connector production Browser Harness invalid/);

  const unconfigured = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner() {
    throw new Error("unconfigured provider must not reach the runner");
  } });
  await assert.rejects(() => unconfigured({ ...baseInput, provider: "extension-site" }), /Connector production Browser Harness invalid/);

  let missingProviderCalls = 0;
  const missingProvider = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner() {
    missingProviderCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: "register_button" } };
  } });
  await assert.rejects(() => missingProvider({ ...baseInput }), /Connector production Browser Harness invalid/);
  assert.equal(missingProviderCalls, 0);

  for (const extensionProvider of ["Extension-Site", "x", "extension/site", "luma"]) {
    assert.throws(() => createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", extensionProvider, async runAgentRunner() {} }), /Connector production Browser Harness invalid/);
  }
});

test("Connpass known form completes natively before the agent when the runner is unavailable", async () => {
  const radio = (control, label, question, group) => ({ control, kind: "radio", label, question, required: true, group });
  const groups = [radio("online_radio", "オンライン視聴枠（YouTube） 無料 参加者数 30人", "参加枠", "online"), radio("online_speaker", "オンライン登壇枠（Zoom） 無料 先着順 3/3人", "参加枠", "online"),
    radio("referral_radio", "Connpass", "このイベントは何を見て知りましたか？", "referral"), radio("referral_sns", "SNS", "このイベントは何を見て知りましたか？", "referral"),
    { control: "ack_one", kind: "radio", label: "はい、わかりました。", question: "speaker acknowledgement", required: false },
    { control: "ack_two", kind: "radio", label: "はい、わかりました。", question: "second speaker acknowledgement", required: false },
    { control: "confirm_button", kind: "button", label: "申し込みを確定する", required: false, submittable: true }];
  const groupOrder = { online: 0, referral: 1 };
  let step = 0;
  let agentCalls = 0;
  const operated = [];
  const page = Object.freeze({ page_id: "known-connpass-page", url() { return "https://osaka-driven-dev.connpass.com/event/400028/join/"; } });
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner() { agentCalls += 1; throw new Error("agent unavailable"); } });
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    connpassWorkflow: { async readProviderState() { return step === 3 ? { status: "registered" } : { status: "absent" }; } },
    async inspectControls() { return groups.map((control) => ({ ...control, completed: control.group ? step > groupOrder[control.group] : false })); },
    proposeAction: proposer,
    async operateControl(input) { operated.push(input.action.control); step += 1; return { status: "success" }; },
    resolveValue: createPrivateValueResolver({ async readPeatixProfile() { throw new Error("private profile must not be read"); }, async readFormProfile() { throw new Error("form profile must not be read"); } }),
  });

  const result = await harness.runFallback({ provider: "connpass", candidate: { event_ref: "connpass-event://event/400028" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/KNOWNCONNPASS1", maxSteps: 10, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed");
  assert.equal(agentCalls, 0);
  assert.deepEqual(operated, ["online_radio", "referral_radio", "confirm_button"]);
});

test("Connpass resolver approves only the exact safe radio predicates", async () => {
  let privateReads = 0;
  const resolver = createPrivateValueResolver({ async readPeatixProfile() { privateReads += 1; return {}; }, async readFormProfile() { privateReads += 1; return { form_answers: {} }; } });
  const cases = [["オンライン視聴枠（YouTube） 無料 参加者数 30人", "参加枠", true], ["Connpass", "このイベントは何を見て知りましたか？", true], ["オンライン視聴枠（YouTube） 無料", "参加枠", true], ["オンライン視聴枠（YouTube） 無料 参加者数 30人", "", null], ["Connpass", "", null], ["オンライン登壇枠（Zoom） 無料 先着順 3/3人", "参加枠", null], ["オンライン視聴枠（YouTube） 有料 参加者数 30人", "参加枠", null], ["いいえ", "negative", null], ["X", "other referral", null], ["Connpass / SNS", "ambiguous", null], ["unknown", "unknown", null]];
  for (const [label, question, expected] of cases) {
    assert.equal(await resolver({ provider: "connpass", state: "connpass_join", control: { control: "safe_radio", kind: "radio", label, question, required: true } }), expected, label);
  }
  assert.equal(privateReads, 0);
});

test("Connpass native rejects an unqualified online label", async () => {
  let agentCalls = 0;
  const control = { control: "online_unqualified", kind: "radio", label: "オンライン参加", question: "参加方法", required: true };
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
    agentCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
  } });
  assert.deepEqual(await proposer({ provider: "connpass", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls: [control] } }), { control: control.control });
  assert.equal(agentCalls, 1);
});

test("Connpass native ignores safe-looking controls outside the exact join page", async () => {
  let agentCalls = 0;
  const control = { control: "online_nonjoin", kind: "radio", label: "オンライン視聴枠（YouTube） 無料 参加者数 30人", question: "参加枠", required: true };
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
    agentCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
  } });
  assert.deepEqual(await proposer({ provider: "connpass", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { state: "registration_page", controls: [control] } }), { control: control.control });
  assert.equal(agentCalls, 1);
  const resolver = createPrivateValueResolver({ readPeatixProfile: async () => ({ accept_organizer_privacy: true }), readFormProfile: async () => ({}) });
  assert.equal(await resolver({ provider: "connpass", state: "registration_page", control }), null);
});

test("Connpass native fails closed for duplicate safe viewing options", async () => {
  let agentCalls = 0;
  const controls = [
    { control: "online_one", kind: "radio", label: "オンライン視聴枠（YouTube） 無料 参加者数 30人", question: "参加枠", required: true },
    { control: "online_two", kind: "radio", label: "オンライン視聴枠（YouTube） 無料 参加者数 30人", question: "参加枠", required: true },
  ];
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
    agentCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
  } });
  assert.deepEqual(await proposer({ provider: "connpass", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls } }), { control: "online_one" });
  assert.equal(agentCalls, 1);
});

test("Connpass native requires known question context", async () => {
  const cases = [
    { control: "online_empty_question", kind: "radio", label: "オンライン視聴枠（YouTube） 無料 参加者数 30人", question: "", required: true },
    { control: "referral_empty_question", kind: "radio", label: "Connpass", question: "", required: true },
    { control: "ack_unknown_question", kind: "radio", label: "はい、わかりました。", question: "未登録質問", required: true },
  ];
  for (const control of cases) {
    let agentCalls = 0;
    const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
      agentCalls += 1;
      return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
    } });
    assert.deepEqual(await proposer({ provider: "connpass", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls: [control] } }), { control: control.control });
    assert.equal(agentCalls, 1, control.control);
  }
});

test("Connpass fallback latches the first submit attempt across a path change", async () => {
  let href = "https://tokyo-builders.connpass.com/event/400028/";
  let proposals = 0;
  let operated = 0;
  const page = { url() { return href; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    connpassWorkflow: { async readProviderState() { return { status: "absent" }; } },
    async inspectControls() { return [{ control: proposals === 0 ? "submit_one" : "submit_two", kind: "button", label: "Register", required: false, submittable: true }]; },
    async proposeAction(input) { proposals += 1; return { purpose: "submit", method: "ax_click", control: input.observation.controls[0].control }; },
    async operateControl() { operated += 1; href = "https://tokyo-builders.connpass.com/event/400028/complete/"; return { status: "success" }; },
    async resolveValue() { return null; },
  });

  const result = await harness.runFallback({ provider: "connpass", candidate: { event_ref: "connpass-event://event/400028" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/CONNPASSLATCH1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.status, "failed");
  assert.equal(result.safe_reason, "effect_unknown");
  assert.equal(proposals, 2);
  assert.equal(operated, 1);
});

test("bounded proposer accepts Peatix while sending only provider and sanitized controls", async () => {
  let request;
  const proposer = createBoundedActionProposer({
    repoRoot: "/private/repo", evidenceDir: "/private/evidence",
    async runAgentRunner(input) {
      request = input;
      return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: "name_field" } };
    },
  });
  const action = await proposer({ provider: "peatix", target_id: "OWNEDTARGET1", expected_state: "registered_or_pending", step: 1, observation: {
    state: "registration_page", controls: [{ control: "name_field", kind: "input", label: "Name", required: true }],
  } });
  assert.deepEqual(action, { control: "name_field" });
  assert.match(request.prompt, /Peatix/i);
  assert.doesNotMatch(JSON.stringify(request), /event_ref|private-phone|private@example|cookie|password|ws:\/\//i);
});

test("default page adapter observes labels and parent resolves private values before DOM actions", async () => {
  const phoneElement = {
    tagName: "INPUT",
    type: "tel",
    required: true,
    dataset: {},
    labels: [{ innerText: "Phone number" }],
    innerText: "",
    options: [],
    getAttribute(name) { return name === "role" ? null : ""; },
  };
  const buttonElement = {
    tagName: "BUTTON",
    type: "submit",
    required: false,
    dataset: {},
    labels: [],
    innerText: "Register",
    options: [],
    getAttribute(name) { return name === "aria-label" ? "Register" : null; },
  };
  const calls = [];
  const locators = new Map();
  const page = {
    locator(selector) {
      if (selector === "input, textarea, select, button, a[role=button], a#confirm-button") {
        return { async evaluateAll(callback) { return callback([phoneElement, buttonElement]); } };
      }
      if (!locators.has(selector)) {
        locators.set(selector, {
          async count() { return 1; },
          async fill(value) { calls.push(["fill", selector, value]); },
          async click() { calls.push(["click", selector]); },
          async check() { calls.push(["check", selector]); },
          async selectOption(value) { calls.push(["select", selector, value]); },
        });
      }
      return locators.get(selector);
    },
    keyboard: { async press(key) { calls.push(["key", key]); } },
  };
  const controls = await inspectPageControls({ page });
  assert.deepEqual(controls.map(({ kind, label, required }) => ({ kind, label, required })), [
    { kind: "input", label: "Phone number", required: true },
    { kind: "button", label: "Register", required: false },
  ]);
  assert.match(controls[0].control, /^control_[0-9]+$/);
  assert.equal(phoneElement.dataset.lmConnectorControl, controls[0].control);

  const resolveValue = createLumaPrivateValueResolver({
    readProfile: async () => ({ phone: "private-phone", form_answers: {} }),
  });
  const value = await resolveValue({ control: controls[0] });
  assert.equal(value, "private-phone");
  await operatePageControl({
    page,
    control: controls[0],
    action: { purpose: "fill", method: "ax_fill", control: controls[0].control },
    value,
  });
  await operatePageControl({
    page,
    control: controls[1],
    action: { purpose: "submit", method: "ax_click", control: controls[1].control },
    value: null,
  });
  assert.deepEqual(calls.map(([name, , input]) => [name, input]), [
    ["fill", "private-phone"],
    ["click", undefined],
  ]);
});

test("page observation derives required from aria and supported required groups", async () => {
  const group = { className: "field required", getAttribute() { return null; }, querySelector() { return null; } };
  const make = (aria, required, closest) => ({ tagName: "INPUT", type: "text", required, dataset: {}, labels: [{ innerText: "Field" }], innerText: "", value: "", getAttribute(name) { return name === "aria-required" ? aria : ""; }, closest });
  const elements = [make("false", false, () => group), make("true", false, () => null), make("false", true, () => null)];
  const controls = await inspectPageControls({ page: { locator() { return { async evaluateAll(callback) { return callback(elements); } }; } } });
  assert.deepEqual(controls.map((control) => control.required), [true, true, true]);
});

test("parent derives the action method and purpose from the selected control kind", async () => {
  const cases = [["text_field", "input", "fill", "ax_fill"], ["notes_field", "textarea", "fill", "ax_fill"], ["ticket_field", "select", "fill", "ax_select"], ["agree_check", "checkbox", "fill", "ax_check"], ["choice_radio", "radio", "fill", "ax_check"], ["submit_button", "button", "submit", "ax_click"], ["submit_link", "link", "submit", "ax_click"]];
  const controls = cases.map(([control, kind]) => ({ control, kind, label: control, required: kind !== "button" && kind !== "link", submittable: kind === "button" })); const calls = [];
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async ({ page }) => [controls.find((item) => item.control === page.page_id)],
    async proposeAction() { return { purpose: "submit", method: "ax_check", control: "text_field" }; },
    async operateControl(input) { calls.push(input.action); return { status: "success" }; },
    async resolveValue() { return "parent-value"; },
  });
  for (const [control, kind] of cases) {
    const result = await harness.performAction({ page: { page_id: control }, action: { purpose: "submit", method: "ax_check", control } });
    assert.deepEqual(result, kind === "link" ? { status: "failed" } : { status: "success" });
  }
  assert.deepEqual(calls, cases.filter(([, kind]) => kind !== "link").map(([control, , purpose, method]) => ({ purpose, method, control })));
});

test("production harness lets the model choose controls but parent owns values actions and readback", async () => {
  const calls = [];
  const page = Object.freeze({ page_id: "owned-page" });
  const candidate = Object.freeze({ event_ref: "luma-event://event/one" });
  let performed = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: {
      async readProviderState(input) {
        calls.push(["readback", input]);
        return performed === 2 ? { status: "registered" } : { status: "absent" };
      },
    },
    async inspectControls(input) {
      assert.equal(input.page, page);
      calls.push(["inspect"]);
      return [
        { control: "phone_field", kind: "input", label: "Phone", required: true, completed: performed > 0 },
        { control: "register_button", kind: "button", label: "Register", required: false, submittable: true },
      ];
    },
    async proposeAction(input) {
      calls.push(["propose", input]);
      assert.equal(Object.hasOwn(input, "page"), false);
      assert.equal(Object.hasOwn(input, "browser"), false);
      return input.step === 1
        ? { purpose: "fill", method: "ax_fill", control: "phone_field" }
        : { purpose: "submit", method: "ax_click", control: "register_button" };
    },
    async operateControl(input) {
      calls.push(["operate", input]);
      performed += 1;
      if (input.action.control === "phone_field") assert.equal(input.value, "private-phone");
      return { status: "success" };
    },
    async resolveValue(input) {
      calls.push(["resolve", input.control.control]);
      return input.control.control === "phone_field" ? "private-phone" : null;
    },
  });

  const result = await harness.runFallback({
    provider: "luma",
    candidate,
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1",
    maxSteps: 10,
    expectedState: "registered_or_pending",
  });

  assert.equal(result.status, "completed");
  assert.equal(result.provider_state.status, "registered");
  assert.deepEqual(result.repaired_actions, [
    { purpose: "fill", method: "ax_fill", control: "phone_field" },
    { purpose: "submit", method: "ax_click", control: "register_button" },
  ]);
  assert.equal(calls.filter(([name]) => name === "inspect").length, 2);
  assert.equal(calls.filter(([name]) => name === "operate").length, 2);
  assert.equal(calls.filter(([name]) => name === "readback").length, 2);
  const promptInputs = calls.filter(([name]) => name === "propose").map(([, input]) => JSON.stringify(input));
  assert.equal(promptInputs.some((value) => value.includes("private-phone")), false);

  const replay = await harness.performAction({
    page,
    action: { purpose: "submit", method: "ax_click", control: "register_button" },
  });
  assert.deepEqual(replay, { status: "success" });
});

test("production harness uses Connpass parent readback for a Connpass fallback", async () => {
  const page = Object.freeze({ page_id: "owned-page" });
  let operated = false;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    connpassWorkflow: {
      async readProviderState(input) {
        assert.equal(input.page, page);
        return operated ? { status: "pending" } : { status: "absent" };
      },
    },
    async inspectControls() {
      return [{ control: "apply_button", kind: "button", label: "Apply", required: false, submittable: true }];
    },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "apply_button" }; },
    async operateControl() { operated = true; return { status: "success" }; },
    async resolveValue() { return null; },
  });

  const result = await harness.runFallback({
    provider: "connpass",
    candidate: { event_ref: "connpass-event://event/401001" },
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1",
    maxSteps: 10,
    expectedState: "registered_or_pending",
  });

  assert.equal(result.status, "completed");
});

test("production harness uses Peatix parent readback for a Peatix fallback", async () => {
  const page = Object.freeze({ page_id: "owned-peatix-page" }); let operated = false;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    connpassWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    peatixWorkflow: { async readProviderState(input) { assert.equal(input.page, page); return operated ? { status: "pending" } : { status: "absent" }; } },
    async inspectControls() { return [{ control: "name_field", kind: "input", label: "Name", required: true }]; },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "name_field" }; },
    async operateControl() { operated = true; return { status: "success" }; },
    async resolveValue() { return "parent-owned"; },
  });
  const result = await harness.runFallback({ provider: "peatix", candidate: { event_ref: "peatix-event://event/1" }, page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1", maxSteps: 10, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed");
});

test("production harness accepts Meetup and completes only after same-page parent registered readback", async () => {
  const page = Object.freeze({ page_id: "owned-meetup-page" });
  let operated = 0;
  let readbacks = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    connpassWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    peatixWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    meetupWorkflow: {
      async readProviderState(input) {
        assert.equal(input.page, page);
        readbacks += 1;
        return operated > 0 ? { status: "registered" } : { status: "absent" };
      },
    },
    async inspectControls() {
      return [{ control: "meetup_submit", kind: "button", label: "Attend", required: false, submittable: true }];
    },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "meetup_submit" }; },
    async operateControl(input) { assert.equal(input.page, page); operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });

  const result = await harness.runFallback({
    provider: "meetup",
    candidate: { event_ref: "meetup-event://event/315756352" },
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/MEETUPTARGET1",
    maxSteps: 2,
    expectedState: "registered_or_pending",
  });
  assert.equal(result.status, "completed");
  assert.equal(result.provider_state.status, "registered");
  assert.equal(operated, 1);
  assert.equal(readbacks, 1);
});

test("provider-neutral resolver returns only parent-owned Peatix/form values", async () => {
  const resolver = createPrivateValueResolver({
    readPeatixProfile: async () => ({ name: "Private Name", email: "private@example.test", family_name_kana: "サクラ", given_name_kana: "テスト", accept_organizer_privacy: true }),
    readFormProfile: async () => ({ phone: "+81 90 0000 0000", form_answers: { "Which role?": "Founder", "Approved option": ["Yes"] } }),
  });
  const control = (kind, label) => ({ control: "safe_control", kind, label, required: true });
  assert.equal(await resolver({ provider: "peatix", control: control("input", "Name") }), "Private Name");
  assert.equal(await resolver({ provider: "peatix", control: control("input", "Email") }), "private@example.test");
  assert.equal(await resolver({ provider: "peatix", control: control("input", "Family name kana") }), "サクラ");
  assert.equal(await resolver({ provider: "peatix", control: control("input", "Phone") }), "+81 90 0000 0000");
  assert.equal(await resolver({ provider: "peatix", control: control("select", "Which role?") }), "Founder");
  assert.equal(await resolver({ provider: "peatix", control: control("input", "Invented question") }), null);
});

test("Eventbrite attendee resolver maps exact incomplete fields to the private profile", async () => {
  let profile = { given_name: "GivenFixture", family_name: "FamilyFixture", email: "EmailFixture" };
  const resolver = createPrivateValueResolver({ readPeatixProfile: async () => profile, readFormProfile: async () => ({ form_answers: {} }) });
  const control = (label, extra = {}) => ({ control: "eventbrite_attendee_field", kind: "input", label, required: true, completed: false, submittable: false, ...extra });
  assert.equal(await resolver({ provider: "eventbrite", control: control("First name") }), "GivenFixture");
  assert.equal(await resolver({ provider: "eventbrite", control: control("Last name") }), "FamilyFixture");
  assert.equal(await resolver({ provider: "eventbrite", control: control("Email") }), "EmailFixture");
  assert.equal(await resolver({ provider: "eventbrite", control: control("Confirm email") }), "EmailFixture");
  for (const [label, extra] of [["first name"], ["First name please"], ["Unknown"], ["First name", { kind: "checkbox" }], ["First name", { completed: true }], ["First name", { required: false }], ["First name", { kind: "button", submittable: true }]]) {
    assert.equal(await resolver({ provider: "eventbrite", control: control(label, extra) }), null, label);
  }
  assert.equal(await resolver({ provider: "peatix", control: control("First name") }), null);
  profile = { given_name: " GivenFixture ", family_name: "FamilyFixture", email: "EmailFixture" };
  assert.equal(await resolver({ provider: "eventbrite", control: control("First name") }), null);
  for (const given_name of [undefined, "x".repeat(2_001)]) {
    profile = { given_name, family_name: "FamilyFixture", email: "EmailFixture" };
    assert.equal(await resolver({ provider: "eventbrite", control: control("First name") }), null);
  }
  profile = null;
  assert.equal(await resolver({ provider: "eventbrite", control: control("Email") }), null);
});

test("provider-neutral resolver rejects a radio option from a different form question", async () => { const resolver = createPrivateValueResolver({ readPeatixProfile: async () => ({ accept_organizer_privacy: false }), readFormProfile: async () => ({ form_answers: { "Role question": "Yes", "Other question": "No" } }) });
  const control = (question) => ({ control: "safe_radio", kind: "radio", label: "Yes", question, required: true });
  assert.equal(await resolver({ provider: "peatix", control: control("Other question") }), null); assert.equal(await resolver({ provider: "peatix", control: control("Role question") }), true);
});

test("provider-neutral resolver supports Peatix confirm Kana names and exact privacy consent label", async () => { const resolver = createPrivateValueResolver({ readPeatixProfile: async () => ({ family_name_kana: "サクラ", given_name_kana: "テスト", name_kanji: "桜 太郎", name_hiragana: "さくら てすと", accept_organizer_privacy: true }), readFormProfile: async () => ({ form_answers: {} }) });
  const control = (kind, label) => ({ control: "safe_control", kind, label, required: true });
  assert.equal(await resolver({ provider: "peatix", control: control("input", "お名前（漢字）") }), "桜 太郎");
  assert.equal(await resolver({ provider: "peatix", control: control("input", "お名前（ひらがな）") }), "さくら てすと");
  assert.equal(await resolver({ provider: "peatix", control: control("input", "お名前（漢字）追加") }), null);
  assert.equal(await resolver({ provider: "peatix", control: control("input", "お名前（ひらがな）追加") }), null);
  assert.equal(await resolver({ provider: "peatix", control: control("input", "lastname_edit") }), "サクラ"); assert.equal(await resolver({ provider: "peatix", control: control("input", "firstname_edit") }), "テスト");
  assert.equal(await resolver({ provider: "peatix", control: { ...control("radio", "確認し同意する。"), question: "主催者のプライバシーポリシーを読んだ・確認した" } }), true);
  assert.equal(await resolver({ provider: "peatix", control: control("radio", "確認し同意する。") }), null); assert.equal(await resolver({ provider: "peatix", control: { ...control("radio", "確認し同意する。"), question: "別の質問" } }), null);
  for (const label of ["Last name", "First name", "姓", "名"]) assert.equal(await resolver({ provider: "peatix", control: control("input", label) }), null);
});

test("production harness rejects unapproved Peatix radio before DOM action", async () => {
  let operated = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    peatixWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async () => [{ control: "unknown_radio", kind: "radio", label: "Invented answer", required: true }],
    proposeAction: async () => ({ purpose: "fill", method: "ax_check", control: "unknown_radio" }),
    operateControl: async () => { operated += 1; return { status: "success" }; },
    resolveValue: createPrivateValueResolver({ readPeatixProfile: async () => ({ accept_organizer_privacy: false }), readFormProfile: async () => ({ form_answers: {} }) }),
  });
  const result = await harness.runFallback({ provider: "peatix", candidate: { event_ref: "peatix-event://event/1" }, page: {},
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1", maxSteps: 1, expectedState: "registered_or_pending" });
  assert.equal(result.status, "failed"); assert.equal(result.safe_reason, "agent_action_failed"); assert.equal(operated, 0);
});

test("Luma fallback reports an unavailable parent-owned form answer", async () => {
  let operated = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async () => [{ control: "required_answer", kind: "input", label: "Unknown required answer", required: true }],
    proposeAction: async () => ({ control: "required_answer" }),
    operateControl: async () => { operated += 1; return { status: "success" }; },
    resolveValue: async () => null,
  });
  const result = await harness.runFallback({ provider: "luma", candidate: { event_ref: "luma-event://event/one" }, page: {}, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1", maxSteps: 1, expectedState: "registered_or_pending" });
  assert.equal(result.safe_reason, "private_value_unavailable");
  assert.equal(operated, 0);
});

test("page observation exposes boolean completion without values", async () => {
  const make = (tagName, type, name, value, checked, innerText = "") => ({ tagName, type, name, value, checked, required: false, dataset: {}, labels: [], innerText, getAttribute(key) { return key === "name" ? name : ""; } });
  const elements = [make("INPUT", "text", "name", "secret-value", false), make("TEXTAREA", "", "notes", "", false), make("SELECT", "", "ticket", "option-1", false), make("INPUT", "checkbox", "agree", "on", true), make("INPUT", "radio", "choice", "one", false), make("INPUT", "radio", "choice", "two", true), make("INPUT", "radio", " choice ", "spaced", false, "Spaced option"), make("INPUT", "radio", "other", "yes", false), make("INPUT", "radio", "", "unnamed-one", false, "Unnamed one"), make("INPUT", "radio", "", "unnamed-two", true, "Unnamed two"), make("BUTTON", "submit", "", "", false, "Submit")];
  const controls = await inspectPageControls({ page: { locator() { return { async evaluateAll(callback) { return callback(elements); } }; } } });
  assert.deepEqual(controls.map(({ kind, completed }) => ({ kind, completed })), [{ kind: "input", completed: true }, { kind: "textarea", completed: false }, { kind: "select", completed: true }, { kind: "checkbox", completed: true }, { kind: "radio", completed: true }, { kind: "radio", completed: true }, { kind: "radio", completed: false }, { kind: "radio", completed: false }, { kind: "radio", completed: false }, { kind: "radio", completed: true }, { kind: "button", completed: false }]);
  assert.equal(Object.hasOwn(controls[0], "value"), false); assert.doesNotMatch(JSON.stringify(controls), /secret-value/);
});

test("bounded proposer excludes completed and optional answer controls from the structured enum", async () => {
  let request; const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) { request = input; return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: "submit_button" } }; } });
  const action = await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls: [{ control: "name_field", kind: "input", label: "Name", required: true, completed: true }, { control: "optional_notes", kind: "input", label: "Optional notes", required: false, completed: false }, { control: "submit_button", kind: "button", label: "Submit", required: false, completed: false, submittable: true }] } });
  assert.deepEqual(action, { control: "submit_button" }); assert.deepEqual(request.schema.properties.control.enum, ["submit_button"]); assert.match(request.prompt, /incomplete|completed/i); assert.doesNotMatch(JSON.stringify(request), /secret-value/);
});

test("bounded proposer fails closed before the agent when no actionable control remains", async () => {
  let calls = 0; const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner() { calls += 1; throw new Error("agent must not run"); } });
  const result = await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls: [{ control: "name_field", kind: "input", label: "Name", required: true, completed: true }] } });
  assert.equal(result, null); assert.equal(calls, 0);
});

test("bounded proposer fails closed for a missing or unknown returned control", async () => {
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner() { return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { method: "ax_check", control: "invented_control" } }; } });
  const result = await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls: [{ control: "required_field", kind: "input", label: "Required", required: true, completed: false }] } });
  assert.equal(result, null);
});

test("production harness rejects a completed fill before resolving or operating DOM", async () => {
  let resolves = 0; let operates = 0; const harness = createProductionBrowserHarness({ lumaWorkflow: { async readProviderState() { return { status: "absent" }; } }, inspectControls: async () => [{ control: "name_field", kind: "input", label: "Name", required: true, completed: true }], proposeAction: async () => ({ purpose: "fill", method: "ax_fill", control: "name_field" }), async operateControl() { operates += 1; return { status: "success" }; }, async resolveValue() { resolves += 1; return "parent-owned"; } });
  const result = await harness.runFallback({ provider: "luma", candidate: { event_ref: "luma-event://event/one" }, page: {}, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TARGET1", maxSteps: 1, expectedState: "registered_or_pending" });
  assert.equal(result.status, "failed"); assert.equal(result.safe_reason, "agent_action_failed"); assert.equal(resolves, 0); assert.equal(operates, 0);
});

test("bounded proposer separates fallback evidence sequences on one target", async () => {
  const evidence = []; const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) { evidence.push(input.evidenceDir); return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: "submit_button" } }; } });
  const base = { provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", observation: { controls: [{ control: "submit_button", kind: "button", label: "Submit", required: false, submittable: true }] } };
  await proposer({ ...base, step: 1 }); await proposer({ ...base, step: 2 }); await proposer({ ...base, step: 1 });
  assert.deepEqual(evidence, ["/private/evidence/target-TARGET1/fallback-1/step-1", "/private/evidence/target-TARGET1/fallback-1/step-2", "/private/evidence/target-TARGET1/fallback-2/step-1"]); assert.equal(evidence.some((value) => /candidate/i.test(value)), false);
});

test("production harness rejects an identical mutating action only after its first success and resets per fallback", async () => {
  let operated = 0; const page = { url() { return "https://peatix.com/sales/event/1/form?token=one"; } }; const harness = createProductionBrowserHarness({ lumaWorkflow: { async readProviderState() { return { status: "absent" }; } }, inspectControls: async () => [{ control: "submit_button", kind: "button", label: "Submit", required: false, submittable: true }], proposeAction: async () => ({ purpose: "submit", method: "ax_click", control: "submit_button" }), async operateControl() { operated += 1; return { status: "success" }; }, async resolveValue() { return null; } });
  const input = { provider: "luma", candidate: { event_ref: "luma-event://event/one" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TARGET1", expectedState: "registered_or_pending" };
  const first = await harness.runFallback({ ...input, maxSteps: 2 }); assert.equal(first.safe_reason, "agent_action_failed"); assert.equal(operated, 1);
  const second = await harness.runFallback({ ...input, maxSteps: 1 }); assert.equal(second.safe_reason, "agent_step_limit"); assert.equal(operated, 2);
});

test("production harness allows the same mutating action after an exact page path change", async () => {
  let operated = 0; let href = "https://peatix.com/sales/event/1/form?token=one"; const page = { url() { return href; } }; const harness = createProductionBrowserHarness({ lumaWorkflow: { async readProviderState() { if (operated === 1) href = "https://peatix.com/sales/event/1/confirm#final"; return { status: "absent" }; } }, inspectControls: async () => [{ control: "submit_button", kind: "button", label: "Submit", required: false, submittable: true }], proposeAction: async () => ({ purpose: "submit", method: "ax_click", control: "submit_button" }), async operateControl() { operated += 1; return { status: "success" }; }, async resolveValue() { return null; } });
  const result = await harness.runFallback({ provider: "luma", candidate: { event_ref: "luma-event://event/one" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TARGET1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.safe_reason, "agent_step_limit"); assert.equal(operated, 2);
});

test("production harness treats equivalent activation methods as one repeated effect", async () => {
  let operated = 0; let method = "ax_click"; const page = { url() { return "https://peatix.com/sales/event/1/form"; } }; const harness = createProductionBrowserHarness({ lumaWorkflow: { async readProviderState() { return { status: "absent" }; } }, inspectControls: async () => [{ control: "submit_button", kind: "button", label: "Submit", required: false, submittable: true }], proposeAction: async () => ({ purpose: "submit", method, control: "submit_button" }), async operateControl() { operated += 1; method = "coordinate_click"; return { status: "success" }; }, async resolveValue() { return null; } });
  const result = await harness.runFallback({ provider: "luma", candidate: { event_ref: "luma-event://event/one" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TARGET1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.safe_reason, "agent_action_failed"); assert.equal(operated, 1);
});

test("inspector scopes submit to the registration form and fails closed for duplicate submits", async () => {
  const registrationForm = {}; const cookieForm = {};
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "", form: registrationForm, labels: [{ innerText: "Name" }] }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Register", form: registrationForm }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Register duplicate", form: registrationForm }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Accept cookies", form: cookieForm }),
    make({ tagName: "BUTTON", type: "button", required: false, value: "private-button-value", form: registrationForm }),
  ];
  const controls = await inspectPageControls({ page: { locator() { return { async evaluateAll(callback) { return callback(elements); } }; } } });
  assert.equal(controls.find((control) => control.label === "Register").submittable, false);
  assert.equal(controls.find((control) => control.label === "Register duplicate").submittable, false);
  assert.equal(controls.find((control) => control.label === "Accept cookies").submittable, false);
  assert.equal(controls.some((control) => control.label === "private-button-value"), false);
});

test("inspector marks the lone registration submit and rejects a separate cookie submit", async () => {
  const registrationForm = {}; const cookieForm = {};
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "", form: registrationForm, labels: [{ innerText: "Name" }] }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Register", form: registrationForm }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Accept cookies", form: cookieForm }),
  ];
  const controls = await inspectPageControls({ page: { locator() { return { async evaluateAll(callback) { return callback(elements); } }; } } });
  assert.equal(controls.find((control) => control.label === "Register").submittable, true);
  assert.equal(controls.find((control) => control.label === "Accept cookies").submittable, false);
});

test("inspector fails closed when registration and cookie forms both have required answers", async () => {
  const registrationForm = {}; const cookieForm = {};
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], checked: false, getAttribute() { return ""; }, closest() { return null; }, ...element });
  const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "", form: registrationForm, labels: [{ innerText: "Name" }] }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Register", form: registrationForm }),
    make({ tagName: "INPUT", type: "checkbox", required: true, value: "on", form: cookieForm, labels: [{ innerText: "Accept cookies" }] }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Save preferences", form: cookieForm }),
  ];
  const controls = await inspectPageControls({ page: { locator() { return { async evaluateAll(callback) { return callback(elements); } }; } } });
  assert.equal(controls.find((control) => control.label === "Register").submittable, false);
  assert.equal(controls.find((control) => control.label === "Save preferences").submittable, false);
});

test("parent rejects an injected submit while a required answer remains incomplete", async () => {
  let operated = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async () => [
      { control: "name_field", kind: "input", label: "Name", required: true, completed: false },
      { control: "register_button", kind: "button", label: "Register", required: false, submittable: true },
    ],
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "register_button" }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  assert.deepEqual(await harness.performAction({ page: {}, action: { purpose: "submit", method: "ax_click", control: "register_button" } }), { status: "failed" });
  assert.equal(operated, 0);
});

test("same-page duplicate guard rejects a reindexed submit token", async () => {
  let operated = 0; let observations = 0;
  const page = { url() { return "https://peatix.com/sales/event/1/form"; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async () => {
      observations += 1;
      return [
        { control: "name_field", kind: "input", label: "Name", required: true, completed: true },
        { control: observations === 1 ? "submit_one" : "submit_two", kind: "button", label: "Register", required: false, submittable: true },
      ];
    },
    async proposeAction(input) { return { purpose: "submit", method: "ax_click", control: input.observation.controls.find((control) => control.kind === "button").control }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const result = await harness.runFallback({ provider: "luma", candidate: { event_ref: "luma-event://event/one" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TARGET1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.safe_reason, "agent_action_failed");
  assert.equal(operated, 1);
});

test("inspector keeps input-submit value as a public label and marks only form submits", async () => {
  const form = {};
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "private-answer", form, labels: [{ innerText: "Name" }] }),
    make({ tagName: "INPUT", type: "submit", required: false, value: "Register now", form }),
    make({ tagName: "BUTTON", type: "button", required: false, innerText: "Apply", form: null }),
    make({ tagName: "A", type: "", required: false, innerText: "Help", form: null }),
  ];
  const controls = await inspectPageControls({ page: { locator() { return { async evaluateAll(callback) { return callback(elements); } }; } } });
  const submit = controls.find((control) => control.label === "Register now");
  assert.ok(submit);
  assert.equal(submit.kind, "button");
  assert.equal(submit.submittable, true);
  assert.equal(controls.some((control) => control.label === "private-answer"), false);
  assert.equal(controls.find((control) => control.label === "Apply").submittable, false);
  assert.equal(controls.find((control) => control.label === "Help").submittable, false);
});

test("Connpass join inspector normalizes the measured ticket and question DOM", async () => {
  const form = {};
  const referralTitle = { textContent: "必須 このイベントは何を見て知りましたか？" };
  const referralGroup = {
    querySelector(selector) { return selector === ":scope > .question" ? referralTitle : null; },
  };
  const acknowledgementGroup = (textContent) => ({ querySelector(selector) { return selector === ":scope > .question" ? { textContent } : null; } });
  const make = (element) => ({
    dataset: {}, labels: [], innerText: "", options: [], value: "", checked: false,
    required: false, disabled: false, form, getAttribute() { return ""; }, closest() { return null; },
    ...element,
  });
  const participation = make({
    tagName: "INPUT", type: "radio", name: "participation_type",
    labels: [{ innerText: "オンライン視聴枠（YouTube） 無料 参加者数 30人" }],
  });
  const speaker = make({
    tagName: "INPUT", type: "radio", name: "participation_type",
    labels: [{ innerText: "オンライン登壇枠（Zoom） 無料 先着順 3/3人" }],
  });
  const referral = make({
    tagName: "INPUT", type: "radio", name: "q_434349", required: true, labels: [{ innerText: "Connpass" }],
    closest(selector) { return selector === ".question_list" ? referralGroup : null; },
  });
  const acknowledgementOne = make({
    tagName: "INPUT", type: "radio", name: "q_434350", labels: [{ innerText: "はい、わかりました。" }],
    closest(selector) { return selector === ".question_list" ? acknowledgementGroup("任意 speaker acknowledgement") : null; },
  });
  const acknowledgementTwo = make({
    tagName: "INPUT", type: "radio", name: "q_434351", labels: [{ innerText: "はい、わかりました。" }],
    closest(selector) { return selector === ".question_list" ? acknowledgementGroup("任意 second speaker acknowledgement") : null; },
  });
  const submit = make({ tagName: "BUTTON", type: "submit", innerText: "申し込みを確定する" });
  const elements = [participation, speaker, referral, acknowledgementOne, acknowledgementTwo, submit];
  const page = {
    url() { return "https://osaka-driven-dev.connpass.com/event/400028/join/"; },
    locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; },
  };
  const pending = await inspectPageControls({ page, provider: "connpass" });
  assert.deepEqual(pending.map(({ label, question, required }) => ({ label, question, required })), [
    { label: "オンライン視聴枠（YouTube） 無料 参加者数 30人", question: "参加枠", required: true },
    { label: "オンライン登壇枠（Zoom） 無料 先着順 3/3人", question: "参加枠", required: true },
    { label: "Connpass", question: "このイベントは何を見て知りましたか？", required: true },
    { label: "はい、わかりました。", question: "speaker acknowledgement", required: false },
    { label: "はい、わかりました。", question: "second speaker acknowledgement", required: false },
    { label: "申し込みを確定する", question: undefined, required: false },
  ]);
  participation.checked = true;
  referral.checked = true;
  const complete = await inspectPageControls({ page, provider: "connpass" });
  assert.equal(complete.find((control) => control.label === "申し込みを確定する").submittable, true);

  for (const [provider, href] of [
    ["connpass", "https://connpass.com/event/400028/join/"],
    ["connpass", "https://osaka-driven-dev.connpass.com/event/400028/join"],
    ["connpass", "https://osaka-driven-dev.connpass.com/event/400028/join/?from=test"],
    ["connpass", "https://connpass.com/event/400028/JOIN/"],
    ["connpass", "https://connpass.com/EVENT/400028/JOIN/"],
    ["luma", "https://osaka-driven-dev.connpass.com/event/400028/join/"],
  ]) {
    const controls = await inspectPageControls({ provider, page: { url() { return href; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } } });
    const viewing = controls.find((control) => control.label.startsWith("オンライン視聴枠"));
    assert.equal(viewing.required, provider === "connpass" && href === "https://connpass.com/event/400028/join/", `${provider} ${href}`);
    assert.equal(viewing.question, provider === "connpass" && href === "https://connpass.com/event/400028/join/" ? "参加枠" : undefined, `${provider} ${href}`);
  }

  const wrongName = { ...participation, name: "other_radio" };
  const wrongNameControls = await inspectPageControls({ provider: "connpass", page: { url() { return "https://osaka-driven-dev.connpass.com/event/400028/join/"; }, locator() { return { async evaluateAll(callback, context) { return callback([wrongName, submit], context); } }; } } });
  assert.equal(wrongNameControls[0].required, false);
  assert.equal(wrongNameControls[0].question, undefined);
});

test("Connpass exact join does not adopt a generic question outside .question_list", async () => {
  const form = {};
  const genericTitle = { textContent: "必須 参加枠" };
  const genericGroup = { querySelector(selector) { return selector === ":scope > .question" ? genericTitle : null; } };
  const outsideQuestion = {
    dataset: {}, labels: [{ innerText: "オンライン視聴枠（YouTube） 無料 参加者数 30人" }], innerText: "", options: [], value: "", checked: false,
    required: true, disabled: false, form, tagName: "INPUT", type: "radio", name: "other_radio",
    getAttribute() { return ""; },
    closest(selector) { return selector === "fieldset, dl.field, [role='group'], .field" ? genericGroup : null; },
  };
  const page = {
    url() { return "https://osaka-driven-dev.connpass.com/event/400028/join/"; },
    locator() { return { async evaluateAll(callback, context) { return callback([outsideQuestion], context); } }; },
  };
  const controls = await inspectPageControls({ page, provider: "connpass" });
  assert.equal(controls[0].question, undefined);
  let agentCalls = 0;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
    agentCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
  } });
  assert.deepEqual(await proposer({ provider: "connpass", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { state: "connpass_join", controls } }), { control: controls[0].control });
  assert.equal(agentCalls, 1);
});

test("bounded proposer exposes pending answers first and only submittable buttons after completion", async () => {
  const enums = [];
  const proposer = createBoundedActionProposer({
    repoRoot: "/private/repo", evidenceDir: "/private/evidence",
    async runAgentRunner(input) {
      enums.push(input.schema.properties.control.enum);
      return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
    },
  });
  const controls = [
    { control: "name_field", kind: "input", label: "Name", required: true, completed: false },
    { control: "phone_field", kind: "input", label: "Phone", required: true, completed: true },
    { control: "cookie_button", kind: "button", label: "Accept all cookies", required: false, submittable: false },
    { control: "apply_button", kind: "button", label: "Apply", required: false, submittable: false },
    { control: "register_button", kind: "button", label: "Register", required: false, submittable: true },
    { control: "help_link", kind: "link", label: "Help", required: false, submittable: false },
  ];
  assert.deepEqual(await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls } }), { control: "name_field" });
  assert.deepEqual(await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 2, observation: { controls: controls.map((control) => control.control === "name_field" ? { ...control, completed: true } : control) } }), { control: "register_button" });
  assert.deepEqual(enums, [["name_field"], ["register_button"]]);
});

test("parent rejects arbitrary buttons and links even when an injected action selects them", async () => {
  let operated = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async () => [
      { control: "cookie_button", kind: "button", label: "Accept all cookies", required: false, submittable: false },
      { control: "help_link", kind: "link", label: "Help", required: false, submittable: false },
    ],
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "cookie_button" }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const page = {};
  assert.deepEqual(await harness.performAction({ page, action: { purpose: "submit", method: "ax_click", control: "cookie_button" } }), { status: "failed" });
  assert.deepEqual(await harness.performAction({ page, action: { purpose: "submit", method: "ax_click", control: "help_link" } }), { status: "failed" });
  assert.equal(operated, 0);
});

test("known Peatix input button is suppressed while required answers remain and exposed after completion", async () => {
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const form = {}; const required = make({ tagName: "INPUT", type: "text", required: true, value: "", form, labels: [{ innerText: "Name" }] });
  const known = make({ tagName: "INPUT", type: "button", id: "form-submit-button", value: "確認画面へ進む", disabled: false, form: null });
  const elements = [required, known, make({ tagName: "BUTTON", type: "button", innerText: "Accept all cookies" }), make({ tagName: "BUTTON", type: "button", innerText: "Filter" })];
  const page = { url() { return "https://peatix.com/sales/event/5075819/form"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  let request;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) { request = input; return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } }; } });
  const pending = await inspectPageControls({ page, provider: "peatix" });
  assert.deepEqual(await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 1, observation: { controls: pending } }), { control: pending[0].control });
  required.value = "filled";
  const complete = await inspectPageControls({ page, provider: "peatix" });
  const submit = complete.find((control) => control.label === "確認画面へ進む");
  assert.ok(submit);
  assert.equal(submit.submittable, true);
  assert.deepEqual(await proposer({ provider: "peatix", target_id: "TARGET1", expected_state: "registered_or_pending", step: 2, observation: { controls: complete } }), { control: submit.control });
  assert.deepEqual(request.schema.properties.control.enum, [submit.control]);
});

test("known Peatix submit fails closed for zero, duplicate, wrong-id, disabled, and competing-answer variants", async () => {
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const form = {}; const answer = make({ tagName: "INPUT", type: "text", required: true, value: "filled", form, labels: [{ innerText: "Name" }] });
  const submit = (overrides = {}) => make({ tagName: "INPUT", type: "button", id: "form-submit-button", value: "確認画面へ進む", disabled: false, form: null, ...overrides });
  const cases = [
    ["zero", [answer]],
    ["duplicate", [answer, submit(), submit()]],
    ["wrong-id", [answer, submit({ id: "other-submit" })]],
    ["wrong-form-label", [answer, submit({ value: "チケットを申し込む" })]],
    ["disabled", [answer, submit({ disabled: true })]],
    ["competing-answer-form", [answer, submit(), make({ tagName: "INPUT", type: "checkbox", required: true, value: "on", form: {}, labels: [{ innerText: "Cookie" }] })]],
  ];
  for (const [, elements] of cases) {
    const controls = await inspectPageControls({ provider: "peatix", page: { url() { return "https://peatix.com/sales/event/5075819/form"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } } });
    assert.equal(controls.some((control) => control.submittable === true), false);
  }
});

test("known submit requires the Peatix provider and canonical form URL", async () => {
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const form = {}; const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "filled", form, labels: [{ innerText: "Name" }] }),
    make({ tagName: "INPUT", type: "button", id: "form-submit-button", value: "確認画面へ進む", disabled: false, form: null }),
  ];
  const cases = [["luma", "https://peatix.com/sales/event/5075819/form"], ["peatix", "https://evil.example/sales/event/5075819/form"], ["peatix", "https://peatix.com/event/5075819"]];
  for (const [provider, href] of cases) {
    const controls = await inspectPageControls({ provider, page: { url() { return href; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } } });
    assert.equal(controls.some((control) => control.submittable === true), false);
  }
  const crossEvent = await inspectPageControls({ provider: "peatix", event_id: "5075819", page: { url() { return "https://peatix.com/sales/event/9999999/form"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } } });
  assert.equal(crossEvent.some((control) => control.submittable === true), false);
});

test("production harness does not reuse a Peatix observation for another provider", async () => {
  let operated = 0; const providers = []; const page = { url() { return "https://peatix.com/sales/event/5075819/form"; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async ({ provider }) => { providers.push(provider); return [{ control: "known_submit", kind: "button", label: "Register", required: false, submittable: provider == null || provider === "peatix" }]; },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "known_submit" }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  assert.deepEqual(await harness.performAction({ page, provider: "peatix", action: { purpose: "submit", method: "ax_click", control: "known_submit" } }), { status: "success" });
  assert.deepEqual(await harness.performAction({ page, provider: "luma", action: { purpose: "submit", method: "ax_click", control: "known_submit" } }), { status: "failed" });
  assert.equal(operated, 1);
  assert.deepEqual(providers, ["peatix", "luma"]);
});

test("production harness binds a cached observation to the same Peatix candidate event", async () => {
  let operated = 0; const eventIds = []; const page = { url() { return "https://peatix.com/sales/event/5104728/form"; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    inspectControls: async ({ event_id }) => { eventIds.push(event_id); return [{ control: "known_submit", kind: "button", label: "Register", required: false, submittable: event_id === "5104728" }]; },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "known_submit" }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const action = { purpose: "submit", method: "ax_click", control: "known_submit" };
  assert.deepEqual(await harness.performAction({ page, provider: "peatix", candidate: { event_ref: "peatix-event://event/5104728" }, action }), { status: "success" });
  assert.deepEqual(await harness.performAction({ page, provider: "peatix", candidate: { event_ref: "peatix-event://event/9999999" }, action }), { status: "failed" });
  assert.equal(operated, 1);
  assert.deepEqual(eventIds, ["5104728", "9999999"]);
});

test("known Peatix submit fails closed beside an unlabeled required answer", async () => {
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], getAttribute() { return ""; }, closest() { return null; }, ...element });
  const form = {}; const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "filled", form }),
    make({ tagName: "INPUT", type: "button", id: "form-submit-button", value: "確認画面へ進む", disabled: false, form: null }),
  ];
  const controls = await inspectPageControls({ provider: "peatix", page: { url() { return "https://peatix.com/sales/event/5075819/form"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } } });
  assert.equal(controls.some((control) => control.submittable === true), false);
});

test("known Peatix confirm anchor is exposed only on the exact same-event confirmation page", async () => {
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], disabled: false, hidden: false, isConnected: true, style: {}, getBoundingClientRect() { return { width: 120, height: 32 }; }, getAttribute() { return ""; }, closest() { return null; }, ...element });
  const form = {};
  const elements = [
    make({ tagName: "INPUT", type: "text", required: true, value: "filled", form, labels: [{ innerText: "Name" }] }),
    make({ tagName: "A", id: "confirm-button", innerText: "チケットを申し込む", form: null }),
    make({ tagName: "A", id: "help-link", innerText: "Help", form: null }),
    make({ tagName: "BUTTON", type: "button", innerText: "Accept cookies", form: null }),
  ];
  let selector = "";
  const page = {
    url() { return "https://peatix.com/sales/event/5104728/confirm"; },
    locator(value) {
      selector = value;
      return { async evaluateAll(callback, context) { return callback(elements, context); } };
    },
  };
  const controls = await inspectPageControls({ page, provider: "peatix", event_id: "5104728" });
  assert.match(selector, /a#confirm-button/);
  const confirm = controls.find((control) => control.label === "チケットを申し込む");
  assert.ok(confirm);
  assert.equal(confirm.kind, "button");
  assert.equal(confirm.submittable, true);
  assert.equal(controls.find((control) => control.label === "Help").kind, "link");
  assert.equal(controls.find((control) => control.label === "Help").submittable, false);
  assert.equal(controls.find((control) => control.label === "Accept cookies").submittable, false);
});

test("known Peatix confirm anchor fails closed when hidden, detached, CSS-hidden, or zero-sized", async () => {
  const make = (element = {}) => {
    const result = { dataset: {}, labels: [], innerText: "", options: [], disabled: false, hidden: false, isConnected: true, style: {}, ...element };
    result.getBoundingClientRect = () => result.rect || { width: 120, height: 32 };
    result.getAttribute = (name) => name === "hidden" ? (result.hidden ? "" : null) : name === "aria-hidden" ? (result.ariaHidden ? "true" : null) : "";
    result.closest = () => null;
    return result;
  };
  const variants = [
    ["hidden-attribute", { hidden: true }],
    ["aria-hidden", { ariaHidden: true }],
    ["css-display-none", { style: { display: "none" } }],
    ["css-visibility-hidden", { style: { visibility: "hidden" } }],
    ["ancestor-css-display-none", { parentElement: { style: { display: "none" } } }],
    ["zero-size", { rect: { width: 0, height: 0 } }],
    ["detached", { isConnected: false }],
  ];
  for (const [name, overrides] of variants) {
    const form = {};
    const elements = [
      make({ tagName: "INPUT", type: "text", required: true, value: "filled", form, labels: [{ innerText: "Name" }] }),
      make({ tagName: "A", id: "confirm-button", innerText: "チケットを申し込む", form: null, ...overrides }),
    ];
    const controls = await inspectPageControls({ provider: "peatix", event_id: "5104728", page: { url() { return "https://peatix.com/sales/event/5104728/confirm"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } } });
    assert.equal(controls.some((control) => control.submittable === true), false, name);
  }
});

test("known Peatix confirm fails closed for pending answers and identity, label, state, or uniqueness variants", async () => {
  const make = (element) => ({ dataset: {}, labels: [], innerText: "", options: [], disabled: false, getAttribute() { return ""; }, closest() { return null; }, ...element });
  const baseAnswer = { tagName: "INPUT", type: "text", required: true, value: "filled", form: {}, labels: [{ innerText: "Name" }] };
  const baseConfirm = { tagName: "A", id: "confirm-button", innerText: "チケットを申し込む", form: null };
  const cases = [
    ["pending", { answer: { value: "" } }],
    ["unlabeled", { answer: { labels: [] } }],
    ["zero-form-required-answer", { answer: { form: null } }],
    ["competing-answer-form", { competing: true }],
    ["wrong-provider", { provider: "luma" }],
    ["wrong-host", { href: "https://evil.example/sales/event/5104728/confirm" }],
    ["wrong-path", { href: "https://peatix.com/event/5104728" }],
    ["wrong-event", { href: "https://peatix.com/sales/event/9999999/confirm" }],
    ["wrong-tag", { confirm: { tagName: "BUTTON" } }],
    ["wrong-id", { confirm: { id: "other-confirm" } }],
    ["wrong-label", { confirm: { innerText: "申し込む" } }],
    ["form-associated", { confirm: { form: {} } }],
    ["disabled", { confirm: { disabled: true } }],
    ["duplicate", { duplicate: true }],
  ];
  for (const [name, variant] of cases) {
    const answer = make({ ...baseAnswer, ...(variant.answer || {}) });
    const confirm = make({ ...baseConfirm, ...(variant.confirm || {}) });
    const elements = [answer, ...(variant.competing ? [make({ tagName: "INPUT", type: "text", required: true, value: "filled", form: {}, labels: [{ innerText: "Email" }] })] : []), confirm, ...(variant.duplicate ? [make({ ...baseConfirm })] : [])];
    const href = variant.href || "https://peatix.com/sales/event/5104728/confirm";
    const controls = await inspectPageControls({
      page: { url() { return href; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } },
      provider: variant.provider || "peatix",
      event_id: "5104728",
    });
    assert.equal(controls.some((control) => control.submittable === true), false, name);
  }
});

test("Peatix form submit waits for the bounded same-event confirm navigation before succeeding", async () => {
  let href = "https://peatix.com/sales/event/5104728/form";
  let clicks = 0;
  const waitCalls = [];
  const page = {
    url() { return href; },
    waitForURL(predicate, options) {
      waitCalls.push(options);
      return new Promise((resolve, reject) => {
        const started = Date.now();
        const poll = () => {
          if (predicate(href)) return resolve();
          if (Date.now() - started > 100) return reject(new Error("confirm navigation timeout"));
          setTimeout(poll, 1);
        };
        poll();
      });
    },
  };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    peatixWorkflow: { async readProviderState() { return href.endsWith("/confirm") ? { status: "pending" } : { status: "absent" }; } },
    async inspectControls() {
      return href.endsWith("/form")
        ? [{ control: "form_submit", kind: "button", label: "確認画面へ進む", required: false, submittable: true }]
        : [{ control: "confirm_anchor", kind: "button", label: "Review", required: false, submittable: false }];
    },
    async proposeAction(input) {
      return input.observation.controls[0].control === "form_submit"
        ? { purpose: "submit", method: "ax_click", control: "form_submit" }
        : null;
    },
    async operateControl() {
      clicks += 1;
      setTimeout(() => { href = "https://peatix.com/sales/event/5104728/confirm"; }, 5);
      return { status: "success" };
    },
    async resolveValue() { return null; },
  });
  const result = await harness.runFallback({
    provider: "peatix",
    candidate: { event_ref: "peatix-event://event/5104728" },
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/PEATIXCONFIRM1",
    maxSteps: 2,
    expectedState: "registered_or_pending",
  });
  assert.equal(result.status, "completed");
  assert.equal(clicks, 1);
  assert.equal(waitCalls.length, 1);
  assert.deepEqual(waitCalls[0], { waitUntil: "domcontentloaded", timeout: 30_000 });
  assert.deepEqual(result.repaired_actions, [{ purpose: "submit", method: "ax_click", control: "form_submit" }]);
});

test("Peatix form submit stops on a cross-event confirm mismatch before readback or final observation", async () => {
  let href = "https://peatix.com/sales/event/5104728/form";
  let observed = 0; let operated = 0; let readbacks = 0; let finalObserved = 0;
  const page = {
    url() { return href; },
    waitForURL(predicate) {
      assert.equal(predicate("https://peatix.com/sales/event/9999999/confirm"), false);
      return Promise.reject(new Error("cross-event confirm"));
    },
  };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    peatixWorkflow: { async readProviderState() { readbacks += 1; return { status: "pending" }; } },
    async inspectControls() {
      observed += 1;
      if (!href.endsWith("/form")) finalObserved += 1;
      return [{ control: "form_submit", kind: "button", label: "確認画面へ進む", required: false, submittable: true }];
    },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "form_submit" }; },
    async operateControl() { operated += 1; href = "https://peatix.com/sales/event/9999999/confirm"; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const result = await harness.runFallback({ provider: "peatix", candidate: { event_ref: "peatix-event://event/5104728" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/PEATIXMISMATCH1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.status, "failed");
  assert.equal(result.safe_reason, "agent_action_failed");
  assert.equal(observed, 1);
  assert.equal(operated, 1);
  assert.equal(readbacks, 0);
  assert.equal(finalObserved, 0);
});

test("Peatix final click settles delayed registration before one completed outcome", async () => {
  let registered = false; let finalReadbackConsumed = false; let clicks = 0; let reads = 0;
  const page = { url() { return "https://peatix.com/sales/event/5104728/confirm"; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    peatixWorkflow: { async readProviderState() { reads += 1; if (registered && !finalReadbackConsumed) { finalReadbackConsumed = true; return { status: "pending" }; } return { status: "absent" }; } },
    async inspectControls() { return [{ control: "confirm_button", kind: "button", label: "チケットを申し込む", required: false, submittable: true }]; },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "confirm_button" }; },
    async operateControl() { clicks += 1; setTimeout(() => { registered = true; }, 10); return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const result = await harness.runFallback({ provider: "peatix", candidate: { event_ref: "peatix-event://event/5104728" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/PEATIXFINALDELAY1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed");
  assert.equal(clicks, 1);
  assert.equal(result.provider_state.status, "pending");
  assert.ok(reads >= 1);
});

test("Connpass final click settles delayed registration before one completed outcome", async () => {
  let registered = false; let clicks = 0; let observations = 0; let proposals = 0; let reads = 0;
  const page = { url() { return "https://tokyo-builders.connpass.com/event/400028/join/"; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    connpassWorkflow: { async readProviderState() { reads += 1; return registered ? { status: "registered" } : { status: "absent" }; } },
    async inspectControls() { observations += 1; return [{ control: "confirm_button", kind: "button", label: "申し込みを確定する", required: false, submittable: true }]; },
    async proposeAction() { proposals += 1; return { purpose: "submit", method: "ax_click", control: "confirm_button" }; },
    async operateControl() { clicks += 1; setTimeout(() => { registered = true; }, 10); return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const result = await harness.runFallback({ provider: "connpass", candidate: { event_ref: "connpass-event://event/400028" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/CONNPASSFINALDELAY1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed"); assert.equal(result.provider_state.status, "registered"); assert.equal(clicks, 1);
  assert.equal(observations, 1); assert.equal(proposals, 1);
  assert.ok(reads >= 1);
});

test("Connpass final settlement fails closed for identity, label, duplicate, and reader variants", async () => {
  const base = { href: "https://tokyo-builders.connpass.com/event/400028/join/", candidate: { event_ref: "connpass-event://event/400028" }, controls: [{ control: "confirm_button", kind: "button", label: "申し込みを確定する", required: false, submittable: true }], reader: true };
  const variants = [
    ["wrong-url", { href: "https://tokyo-builders.connpass.com/event/400028/" }],
    ["wrong-event", { candidate: { event_ref: "connpass-event://event/400029" } }],
    ["wrong-label", { controls: [{ ...base.controls[0], label: "申し込みを確定" }] }],
    ["non-submittable", { controls: [{ ...base.controls[0], submittable: false }] }],
    ["duplicate", { controls: [base.controls[0], { ...base.controls[0], control: "confirm_two" }] }],
    ["missing-reader", { reader: false }],
  ];
  for (const [name, variant] of variants) {
    let operated = 0;
    const config = { ...base, ...variant };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      ...(config.reader ? { connpassWorkflow: { async readProviderState() { return { status: "absent" }; } } } : {}),
      async inspectControls() { return config.controls; },
      async operateControl() { operated += 1; return { status: "success" }; }, async resolveValue() { return null; },
      async proposeAction() { return { purpose: "submit", method: "ax_click", control: config.controls[0].control }; },
    });
    assert.deepEqual(await harness.performAction({ provider: "connpass", candidate: config.candidate, page: { url() { return config.href; } }, action: { purpose: "submit", method: "ax_click", control: config.controls[0].control } }), { status: "failed" }, name);
    assert.equal(operated, 0, name);
  }
});

test("Peatix final click fails bounded when provider readback never settles", async () => {
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    let readStarted = false; let clicks = 0; let settled = false;
    const page = { url() { return "https://peatix.com/sales/event/5104728/confirm"; } };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      peatixWorkflow: { async readProviderState() { readStarted = true; return new Promise(() => {}); } },
      async inspectControls() { return [{ control: "confirm_button", kind: "button", label: "チケットを申し込む", required: false, submittable: true }]; },
      async proposeAction() { return { purpose: "submit", method: "ax_click", control: "confirm_button" }; },
      async operateControl() { clicks += 1; return { status: "success" }; },
      async resolveValue() { return null; },
    });
    const resultPromise = harness.runFallback({ provider: "peatix", candidate: { event_ref: "peatix-event://event/5104728" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/PEATIXNEVER1", maxSteps: 2, expectedState: "registered_or_pending" });
    for (let attempt = 0; attempt < 100 && !readStarted; attempt += 1) await Promise.resolve();
    assert.equal(readStarted, true);
    resultPromise.then(() => { settled = true; });
    mock.timers.tick(30_001);
    for (let attempt = 0; attempt < 20 && !settled; attempt += 1) await Promise.resolve();
    assert.equal(settled, true);
    const result = await resultPromise;
    assert.equal(result.status, "failed");
    assert.equal(result.safe_reason, "effect_unknown");
    assert.equal(clicks, 1);
  } finally {
    mock.timers.reset();
  }
});

test("Connpass final click fails bounded when provider readback never settles", async () => {
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    let readStarted = false; let clicks = 0; let settled = false; const page = { url() { return "https://tokyo-builders.connpass.com/event/400028/join/"; } };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      connpassWorkflow: { async readProviderState() { readStarted = true; return new Promise(() => {}); } },
      async inspectControls() { return [{ control: "confirm_button", kind: "button", label: "申し込みを確定する", required: false, submittable: true }]; },
      async proposeAction() { return { purpose: "submit", method: "ax_click", control: "confirm_button" }; },
      async operateControl() { clicks += 1; return { status: "success" }; },
      async resolveValue() { return null; },
    });
    const resultPromise = harness.runFallback({ provider: "connpass", candidate: { event_ref: "connpass-event://event/400028" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/CONNPASSFINALNEVER1", maxSteps: 2, expectedState: "registered_or_pending" });
    for (let attempt = 0; attempt < 100 && !readStarted; attempt += 1) await Promise.resolve();
    assert.equal(readStarted, true); resultPromise.then(() => { settled = true; });
    mock.timers.tick(30_001);
    for (let attempt = 0; attempt < 20 && !settled; attempt += 1) await Promise.resolve();
    assert.equal(settled, true);
    const result = await resultPromise;
    assert.equal(result.status, "failed"); assert.equal(result.safe_reason, "effect_unknown"); assert.equal(clicks, 1);
  } finally {
    mock.timers.reset();
  }
});

test("Doorkeeper final submit requires exact identity and registered readback", async () => {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  let registered = false; let clicks = 0; let reads = 0;
  const page = { url() { return candidate.canonical_url; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    doorkeeperWorkflow: { async readProviderState() { reads += 1; return registered ? (reads === 1 ? { status: "pending" } : { status: "registered" }) : { status: "absent" }; } },
    async inspectControls() { return [{ control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, submittable: true }]; },
    async proposeAction() { return { purpose: "submit", method: "ax_click", control: "doorkeeper_submit" }; },
    async operateControl() { clicks += 1; registered = true; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const result = await harness.runFallback({ provider: "doorkeeper", candidate, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/DOORKEEPER1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed");
  assert.equal(result.provider_state.status, "registered");
  assert.equal(clicks, 1);
  assert.ok(reads >= 1);
});

test("Doorkeeper final submit fails closed for identity, control, duplicate, and workflow variants", async () => {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  const button = { control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, submittable: true };
  const variants = [
    ["wrong-ref", { candidate: { ...candidate, event_ref: "doorkeeper-event://event/1002" } }],
    ["wrong-url", { href: "https://other.doorkeeper.jp/events/1001" }],
    ["duplicate", { controls: [button, { ...button, control: "doorkeeper_submit_two" }] }],
    ["non-button", { controls: [{ ...button, kind: "link", submittable: false }] }],
    ["non-submittable", { controls: [{ ...button, submittable: false }] }],
    ["wrong-label", { controls: [{ ...button, label: "申込む" }] }],
    ["missing-workflow", { workflow: false }],
  ];
  for (const [name, variant] of variants) {
    let operated = 0;
    const config = { ...variant, controls: variant.controls || [button] };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      ...(config.workflow === false ? {} : { doorkeeperWorkflow: { async readProviderState() { return { status: "registered" }; } } }),
      async inspectControls() { return config.controls; },
      async proposeAction() { return { purpose: "submit", method: "ax_click", control: config.controls[0].control }; },
      async operateControl() { operated += 1; return { status: "success" }; },
      async resolveValue() { return null; },
    });
    const result = await harness.performAction({ provider: "doorkeeper", candidate: config.candidate || candidate, page: { url() { return config.href || candidate.canonical_url; } }, action: { purpose: "submit", method: "ax_click", control: config.controls[0].control } });
    assert.deepEqual(result, { status: "failed" }, name);
    assert.equal(operated, 0, name);
  }
});

test("Doorkeeper does not complete a required fill from pending readback", async () => {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  let completed = false; let fills = 0; let submits = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    doorkeeperWorkflow: { async readProviderState() { return { status: "pending" }; } },
    async inspectControls() { return [
      { control: "name_field", kind: "input", label: "Name", required: true, completed },
      { control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, submittable: true },
    ]; },
    async proposeAction({ observation }) { return { control: observation.controls.find((control) => control.kind === "input" && !control.completed)?.control || "doorkeeper_submit" }; },
    async operateControl({ control }) { if (control.kind === "input") { fills += 1; completed = true; } else submits += 1; return { status: "success" }; },
    async resolveValue() { return "attendee"; },
  });
  const result = await harness.runFallback({ provider: "doorkeeper", candidate, page: { url() { return candidate.canonical_url; } }, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/DOORKEEPERPENDING1", maxSteps: 1, expectedState: "registered_or_pending" });
  assert.equal(result.status, "failed");
  assert.equal(result.safe_reason, "agent_step_limit");
  assert.equal(fills, 1);
  assert.equal(submits, 0);
});

test("Doorkeeper rejects URL identity variants before any action", async () => {
  const base = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  const invalidCanonical = [
    ["query", "https://tokyo-builders.doorkeeper.jp/events/1001?x=1"],
    ["fragment", "https://tokyo-builders.doorkeeper.jp/events/1001#final"],
    ["credentials", "https://user:pass@tokyo-builders.doorkeeper.jp/events/1001"],
    ["port", "https://tokyo-builders.doorkeeper.jp:443/events/1001"],
    ["www", "https://www.doorkeeper.jp/events/1001"],
    ["uppercase-group", "https://Tokyo-builders.doorkeeper.jp/events/1001"],
  ];
  const invalidCurrent = [
    ["query", "https://tokyo-builders.doorkeeper.jp/events/1001?x=1"],
    ["fragment", "https://tokyo-builders.doorkeeper.jp/events/1001#final"],
    ["credentials", "https://user:pass@tokyo-builders.doorkeeper.jp/events/1001"],
    ["port", "https://tokyo-builders.doorkeeper.jp:443/events/1001"],
    ["www", "https://www.doorkeeper.jp/events/1001"],
    ["uppercase-group", "https://Tokyo-builders.doorkeeper.jp/events/1001"],
  ];
  const assertRejected = async (name, candidate, href) => {
    let operated = 0;
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      doorkeeperWorkflow: { async readProviderState() { return { status: "registered" }; } },
      async inspectControls() { return [{ control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, submittable: true }]; },
      async proposeAction() { return { control: "doorkeeper_submit" }; },
      async operateControl() { operated += 1; return { status: "success" }; },
      async resolveValue() { return null; },
    });
    const result = await harness.performAction({ provider: "doorkeeper", candidate, page: { url() { return href; } }, action: { purpose: "submit", method: "ax_click", control: "doorkeeper_submit" } });
    assert.deepEqual(result, { status: "failed" }, name);
    assert.equal(operated, 0, name);
  };
  for (const [name, canonical_url] of invalidCanonical) await assertRejected(`candidate-${name}`, { ...base, canonical_url }, base.canonical_url);
  for (const [name, href] of invalidCurrent) await assertRejected(`current-${name}`, base, href);
});

test("Doorkeeper final readback is bounded when it never settles", async () => {
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    let readStarted = false; let clicks = 0; let settled = false;
    const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      doorkeeperWorkflow: { async readProviderState() { readStarted = true; return new Promise(() => {}); } },
      async inspectControls() { return [{ control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, submittable: true }]; },
      async proposeAction() { return { purpose: "submit", method: "ax_click", control: "doorkeeper_submit" }; },
      async operateControl() { clicks += 1; return { status: "success" }; },
      async resolveValue() { return null; },
    });
    const resultPromise = harness.runFallback({ provider: "doorkeeper", candidate, page: { url() { return candidate.canonical_url; } }, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/DOORKEEPERNEVER1", maxSteps: 2, expectedState: "registered_or_pending" });
    for (let attempt = 0; attempt < 100 && !readStarted; attempt += 1) await Promise.resolve();
    assert.equal(readStarted, true);
    resultPromise.then(() => { settled = true; });
    mock.timers.tick(30_001);
    for (let attempt = 0; attempt < 20 && !settled; attempt += 1) await Promise.resolve();
    const result = await resultPromise;
    assert.equal(result.status, "failed");
    assert.equal(result.safe_reason, "effect_unknown");
    assert.equal(clicks, 1);
  } finally {
    mock.timers.reset();
  }
});

test("Doorkeeper ambiguous click still uses readback and never clicks twice", async () => {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  for (const outcome of ["throw", "failed"]) {
    let registered = false; let clicks = 0;
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      doorkeeperWorkflow: { async readProviderState() { return registered ? { status: "registered" } : { status: "absent" }; } },
      async inspectControls() { return [{ control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, submittable: true }]; },
      async proposeAction() { return { purpose: "submit", method: "ax_click", control: "doorkeeper_submit" }; },
      async operateControl() { clicks += 1; registered = true; if (outcome === "throw") throw new Error("ambiguous click"); return { status: "failed" }; },
      async resolveValue() { return null; },
    });
    const result = await harness.runFallback({ provider: "doorkeeper", candidate, page: { url() { return candidate.canonical_url; } }, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/DOORKEEPER-${outcome}`, maxSteps: 2, expectedState: "registered_or_pending" });
    assert.equal(result.status, "completed", outcome);
    assert.equal(result.provider_state.status, "registered", outcome);
    assert.equal(clicks, 1, outcome);
  }
});

function makeDoorkeeperElement(overrides = {}) {
  const element = {
    tagName: "INPUT", type: "", id: "", name: "", value: "", innerText: "", textContent: "",
    labels: [], required: false, disabled: false, hidden: false, isConnected: true, style: {}, rect: { width: 120, height: 32 },
    dataset: {}, form: null, parentElement: null, ...overrides,
  };
  element.getAttribute = (name) => {
    if (name === "href") return element.href ?? null;
    if (name === "id") return element.id || null;
    if (name === "name") return element.name || null;
    if (name === "type") return element.type || null;
    if (name === "hidden") return element.hidden ? "" : null;
    if (name === "aria-hidden") return element.ariaHidden ? "true" : null;
    return element.attributes && Object.hasOwn(element.attributes, name) ? element.attributes[name] : null;
  };
  element.closest = () => null;
  element.getBoundingClientRect = () => element.rect;
  return element;
}

function makeDoorkeeperForm(id = "new_event_registration") {
  return { id, getAttribute(name) { return name === "id" ? id : null; } };
}

async function inspectDoorkeeperDom(elements, { provider = "doorkeeper", href = "https://techgym.doorkeeper.jp/events/198719", event_id = "198719", selectElements } = {}) {
  let selector = "";
  const page = {
    url() { return href; },
    locator(value) {
      selector = value;
      return { async evaluateAll(callback, context) {
        if (typeof selectElements === "function") return callback(selectElements(value, elements), context);
        const selected = value.includes('a[href="#new_registration_modal"]') ? elements : elements.filter((element) => !(String(element.tagName || "").toLowerCase() === "a" && element.href === "#new_registration_modal"));
        return callback(selected, context);
      } };
    },
  };
  const controls = await inspectPageControls({ page, provider, event_id });
  return { controls, selector };
}

test("non-Doorkeeper inspection keeps the old selector and ordinary controls", async () => {
  const ordinary = makeDoorkeeperElement({ tagName: "BUTTON", innerText: "Register" });
  const doorkeeperAnchor = makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" });
  const { controls, selector } = await inspectDoorkeeperDom([doorkeeperAnchor, ordinary], {
    provider: "luma", href: "https://example.test/register", event_id: "",
    selectElements: (value) => value.includes('a[href="#new_registration_modal"]') ? [doorkeeperAnchor, ordinary] : [ordinary],
  });
  assert.equal(selector, "input, textarea, select, button, a[role=button], a#confirm-button");
  assert.deepEqual(controls.map(({ kind, label, submittable }) => ({ kind, label, submittable })), [{ kind: "button", label: "Register", submittable: false }]);
  const doorkeeper = await inspectDoorkeeperDom([doorkeeperAnchor], { provider: "doorkeeper" });
  assert.equal(doorkeeper.selector, "input, textarea, select, button, a[role=button], a#confirm-button, a[href=\"#new_registration_modal\"]");
});

test("Doorkeeper DOM inspector exposes only the exact visible trigger while modal is closed", async () => {
  const form = makeDoorkeeperForm();
  const elements = [
    makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" }),
    makeDoorkeeperElement({ type: "email", id: "event_registration_email", name: "event_registration[email]", required: true, form, hidden: true, value: "private@example.com" }),
    makeDoorkeeperElement({ type: "submit", name: "commit", value: "申し込む", form, hidden: true }),
  ];
  const { controls, selector } = await inspectDoorkeeperDom(elements);
  assert.match(selector, /a\[href="#new_registration_modal"\]/);
  assert.deepEqual(controls.map(({ kind, label, submittable }) => ({ kind, label, submittable })), [{ kind: "link", label: "申し込む", submittable: false }]);
  assert.doesNotMatch(JSON.stringify(controls), /private@example\.com|event_registration\[email\]/);
});

test("Doorkeeper modal exposes public Email first and the exact single submit only after completion", async () => {
  const form = makeDoorkeeperForm();
  const trigger = makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" });
  const email = makeDoorkeeperElement({ type: "email", id: "event_registration_email", name: "event_registration[email]", required: true, form, value: "" });
  const submit = makeDoorkeeperElement({ type: "submit", name: "commit", value: "申し込む", form });
  let { controls } = await inspectDoorkeeperDom([trigger, email, submit]);
  assert.deepEqual(controls.map(({ kind, label, required, submittable }) => ({ kind, label, required, submittable })), [
    { kind: "link", label: "申し込む", required: false, submittable: false },
    { kind: "input", label: "Email", required: true, submittable: false },
    { kind: "button", label: "申し込む", required: false, submittable: false },
  ]);
  assert.equal(controls.some((control) => control.submittable), false);
  email.value = "private@example.com";
  ({ controls } = await inspectDoorkeeperDom([trigger, email, submit]));
  assert.deepEqual(controls.map(({ kind, label, completed, submittable }) => ({ kind, label, completed, submittable })), [
    { kind: "link", label: "申し込む", completed: false, submittable: false },
    { kind: "input", label: "Email", completed: true, submittable: false },
    { kind: "button", label: "申し込む", completed: false, submittable: true },
  ]);
  assert.doesNotMatch(JSON.stringify(controls), /private@example\.com|event_registration\[email\]|secret|Jane Doe/);
});

test("Doorkeeper trigger requires exact identity, href, label, uniqueness, and visibility", async () => {
  const base = makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" });
  const cases = [
    ["duplicate", [base, makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" })], {}],
    ["wrong-href", [makeDoorkeeperElement({ tagName: "A", href: "#other", innerText: "申し込む" })], {}],
    ["wrong-label", [makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "参加する" })], {}],
    ["wrong-provider", [base], { provider: "peatix" }],
    ["wrong-page", [base], { href: "https://techgym.doorkeeper.jp/groups/198719" }],
    ["wrong-event", [base], { event_id: "198720" }],
    ["www", [base], { href: "https://www.doorkeeper.jp/events/198719" }],
    ["uppercase", [base], { href: "https://Techgym.doorkeeper.jp/events/198719" }],
    ["query", [base], { href: "https://techgym.doorkeeper.jp/events/198719?x=1" }],
    ["fragment", [base], { href: "https://techgym.doorkeeper.jp/events/198719#final" }],
    ["port", [base], { href: "https://techgym.doorkeeper.jp:443/events/198719" }],
    ["credentials", [base], { href: "https://user:pass@techgym.doorkeeper.jp/events/198719" }],
    ["hidden", [makeDoorkeeperElement({ ...base, hidden: true })], {}],
    ["detached", [makeDoorkeeperElement({ ...base, isConnected: false })], {}],
    ["css-hidden", [makeDoorkeeperElement({ ...base, style: { display: "none" } })], {}],
    ["zero-size", [makeDoorkeeperElement({ ...base, rect: { width: 0, height: 0 } })], {}],
  ];
  for (const [name, elements, context] of cases) {
    const { controls } = await inspectDoorkeeperDom(elements, context);
    assert.equal(controls.some((control) => control.kind === "link" && control.label === "申し込む"), false, name);
  }
});

test("Doorkeeper submit fails closed for duplicate, wrong-form, hidden, and ambiguous required answers", async () => {
  const makeCase = (extra = [], submitOverrides = {}) => {
    const form = makeDoorkeeperForm();
    const email = makeDoorkeeperElement({ type: "email", id: "event_registration_email", name: "event_registration[email]", required: true, form, value: "private@example.com" });
    const submit = makeDoorkeeperElement({ type: "submit", name: "commit", value: "申し込む", form, ...submitOverrides });
    return [makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" }), email, submit, ...extra(form)];
  };
  const cases = [
    ["duplicate-submit", (form) => [makeDoorkeeperElement({ type: "submit", name: "commit", value: "申し込む", form })]],
    ["wrong-form", () => [], { form: makeDoorkeeperForm("cookie_form") }],
    ["hidden-submit", () => [], { hidden: true }],
    ["unlabeled-required-answer", (form) => [makeDoorkeeperElement({ type: "text", required: true, form, value: "secret" })]],
    ["ambiguous-required-answer", (form) => [makeDoorkeeperElement({ type: "text", required: true, form, value: "Jane Doe", labels: [{ innerText: "Email" }] })]],
  ];
  for (const [name, extra, submitOverrides] of cases) {
    const { controls } = await inspectDoorkeeperDom(makeCase(extra, submitOverrides));
    assert.equal(controls.some((control) => control.submittable === true), false, name);
    assert.doesNotMatch(JSON.stringify(controls), /private@example\.com|event_registration\[email\]|secret|Jane Doe/);
  }
});

function makeDoorkeeperAncestor(overrides = {}) {
  const ancestor = { hidden: false, ariaHidden: false, style: {}, computedStyle: null, rect: { width: 120, height: 32 }, parentElement: null, ...overrides };
  ancestor.getAttribute = (name) => name === "hidden" ? (ancestor.hidden ? "" : null) : name === "aria-hidden" ? (ancestor.ariaHidden ? "true" : null) : null;
  ancestor.hasAttribute = (name) => name === "hidden" && ancestor.hidden === true;
  ancestor.getBoundingClientRect = () => ancestor.rect;
  return ancestor;
}

test("Doorkeeper ancestor visibility is scoped to exactly one target control", async () => {
  const variants = [
    ["hidden-attribute", { hidden: true }],
    ["aria-hidden", { ariaHidden: true }],
    ["ancestor-style", { style: { display: "none" } }],
    ["computed-style", { computedStyle: { visibility: "hidden" } }],
    ["ancestor-zero-box", { rect: { width: 0, height: 0 } }],
  ];
  for (const [name, overrides] of variants) {
    for (const target of ["trigger", "email", "submit"]) {
      const form = makeDoorkeeperForm();
      const ancestor = makeDoorkeeperAncestor(overrides);
      const ownerDocument = { defaultView: { getComputedStyle(element) { return element.computedStyle || element.style || {}; } } };
      const controls = [
        ["trigger", makeDoorkeeperElement({ tagName: "A", href: "#new_registration_modal", innerText: "申し込む" })],
        ["email", makeDoorkeeperElement({ type: "email", id: "event_registration_email", name: "event_registration[email]", required: true, form, value: "private@example.com" })],
        ["submit", makeDoorkeeperElement({ type: "submit", name: "commit", value: "申し込む", form })],
      ].map(([kind, element]) => kind === target ? Object.assign(element, { parentElement: ancestor, ownerDocument }) : element);
      const { controls: observed } = await inspectDoorkeeperDom(controls);
      const trigger = observed.find((control) => control.kind === "link" && control.label === "申し込む");
      const email = observed.find((control) => control.label === "Email");
      const submittable = observed.filter((control) => control.submittable === true);
      if (target === "trigger") {
        assert.equal(trigger, undefined, `${name}/${target}`);
      } else if (target === "email") {
        assert.ok(trigger, `${name}/${target}`);
        assert.equal(email, undefined, `${name}/${target}`);
        assert.deepEqual(observed.map(({ kind, label, submittable }) => ({ kind, label, submittable })), [
          { kind: "link", label: "申し込む", submittable: false },
          { kind: "button", label: "申し込む", submittable: false },
        ], `${name}/${target}`);
        assert.equal(submittable.length, 0, `${name}/${target}`);
      } else {
        assert.ok(trigger, `${name}/${target}`);
        assert.deepEqual(email && { kind: email.kind, label: email.label, completed: email.completed, submittable: email.submittable }, { kind: "input", label: "Email", completed: true, submittable: false }, `${name}/${target}`);
        assert.equal(submittable.length, 0, `${name}/${target}`);
        assert.equal(observed.some((control) => control.kind === "button"), false, `${name}/${target}`);
      }
    }
  }
});

function makeDoorkeeperFallbackFixture(proposalForStep) {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  const page = { url() { return candidate.canonical_url; } };
  let modal = false; let email = ""; let registered = false; const operated = [];
  const controls = () => modal ? [
    { control: "doorkeeper_trigger", kind: "link", label: "申し込む", required: false, completed: false, submittable: false },
    { control: "event_email", kind: "input", label: "Email", required: true, completed: Boolean(email), submittable: false },
    { control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, completed: false, submittable: Boolean(email) },
  ] : [{ control: "doorkeeper_trigger", kind: "link", label: "申し込む", required: false, completed: false, submittable: false }];
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    doorkeeperWorkflow: { async readProviderState() { return registered ? { status: "registered" } : { status: "absent" }; } },
    async inspectControls() { return controls(); },
    async proposeAction(input) {
      const selected = proposalForStep ? proposalForStep(input.step) : input.observation.controls.find((control) => control.required && !control.completed)?.control || input.observation.controls.find((control) => control.submittable)?.control || input.observation.controls[0].control;
      return { control: selected };
    },
    async operateControl({ control, action, value }) {
      operated.push({ control: control.control, method: action.method });
      if (control.control === "doorkeeper_trigger") modal = true;
      if (control.control === "event_email") email = value;
      if (control.control === "doorkeeper_submit") registered = true;
      return { status: "success" };
    },
    async resolveValue({ control }) { return control.control === "event_email" ? "member@example.test" : null; },
  });
  return { candidate, page, harness, operated };
}

test("Doorkeeper fallback performs trigger, Email fill, and one final submit", async () => {
  const { candidate, page, harness, operated } = makeDoorkeeperFallbackFixture();
  const result = await harness.runFallback({ provider: "doorkeeper", candidate, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/DOORKEEPERFLOW1", maxSteps: 3, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed");
  assert.deepEqual(result.repaired_actions, [
    { purpose: "submit", method: "ax_click", control: "doorkeeper_trigger" },
    { purpose: "fill", method: "ax_fill", control: "event_email" },
    { purpose: "submit", method: "ax_click", control: "doorkeeper_submit" },
  ]);
  assert.deepEqual(operated.map(({ control }) => control), ["doorkeeper_trigger", "event_email", "doorkeeper_submit"]);
});

test("Doorkeeper fallback latches a repeated trigger without blocking fill or final submit", async () => {
  const sequence = ["doorkeeper_trigger", "doorkeeper_trigger", "event_email", "doorkeeper_submit"];
  const { candidate, page, harness, operated } = makeDoorkeeperFallbackFixture((step) => sequence[step - 1]);
  const result = await harness.runFallback({ provider: "doorkeeper", candidate, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/DOORKEEPERFLOW2", maxSteps: 4, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed");
  assert.deepEqual(operated.map(({ control }) => control), ["doorkeeper_trigger", "event_email", "doorkeeper_submit"]);
});

test("Doorkeeper modal trigger rejects wrong provider, identity, label, duplicate, and action", async () => {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  const trigger = { control: "doorkeeper_trigger", kind: "link", label: "申し込む", required: false, completed: false, submittable: false };
  const cases = [
    ["arbitrary", { controls: [{ ...trigger, control: "help_link", label: "Help" }], action: { purpose: "submit", method: "ax_click", control: "help_link" } }],
    ["wrong-provider", { provider: "luma" }],
    ["wrong-identity", { candidate: { ...candidate, event_ref: "doorkeeper-event://event/1002" } }],
    ["wrong-current-url", { href: "https://tokyo-builders.doorkeeper.jp/events/1002" }],
    ["wrong-label", { controls: [{ ...trigger, label: "参加する" }] }],
    ["duplicate", { controls: [trigger, { ...trigger, control: "doorkeeper_trigger_two" }] }],
    ["wrong-action-token", { action: { purpose: "submit", method: "ax_click", control: "missing_trigger" } }],
    ["wrong-action-method", { action: { purpose: "fill", method: "ax_fill", control: trigger.control } }],
  ];
  for (const [name, variant] of cases) {
    let operated = 0; const config = { provider: "doorkeeper", candidate, controls: [trigger], action: { purpose: "submit", method: "ax_click", control: trigger.control }, ...variant };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
      doorkeeperWorkflow: { async readProviderState() { return { status: "absent" }; } },
      async inspectControls() { return config.controls; }, async proposeAction() { return config.action; },
      async operateControl() { operated += 1; return { status: "success" }; }, async resolveValue() { return null; },
    });
    const result = await harness.performAction({ provider: config.provider, candidate: config.candidate, page: { url() { return config.href || candidate.canonical_url; } }, action: config.action });
    assert.deepEqual(result, { status: "failed" }, name); assert.equal(operated, 0, name);
  }
});

test("Doorkeeper default proposer chooses exact trigger, Email, and final submit without generic links", async () => {
  const trigger = { control: "doorkeeper_trigger", kind: "link", label: "申し込む", required: false, completed: false, submittable: false };
  const email = { control: "event_email", kind: "input", label: "Email", required: true, completed: false, submittable: false };
  const completedEmail = { ...email, completed: true };
  const submit = { control: "doorkeeper_submit", kind: "button", label: "申し込む", required: false, completed: false, submittable: true };
  let agentCalls = 0;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
    agentCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: input.schema.properties.control.enum[0] } };
  } });
  const base = { provider: "doorkeeper", target_id: "DOORKEEPERDEFAULT1", expected_state: "registered_or_pending" };
  assert.deepEqual(await proposer({ ...base, step: 1, observation: { state: "registration_page", controls: [trigger] } }), { control: "doorkeeper_trigger" });
  assert.deepEqual(await proposer({ ...base, step: 2, observation: { state: "registration_page", controls: [trigger, email] } }), { control: "event_email" });
  assert.deepEqual(await proposer({ ...base, step: 3, observation: { state: "registration_page", controls: [trigger, completedEmail, submit] } }), { control: "doorkeeper_submit" });
  assert.equal(agentCalls, 2);
  assert.equal(await proposer({ ...base, step: 1, observation: { state: "registration_page", controls: [{ control: "help_link", kind: "link", label: "Help", required: false, completed: false, submittable: false }] } }), null);
});

test("Doorkeeper semantic trigger duplicates fail closed even when the duplicate token is invalid", async () => {
  const candidate = { provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001" };
  const trigger = { control: "doorkeeper_trigger", kind: "link", label: "申し込む", required: false, completed: false, submittable: false };
  const duplicate = { ...trigger, control: "doorkeeper_invalid" };
  let agentCalls = 0;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner() {
    agentCalls += 1;
    return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: trigger.control } };
  } });
  const observation = { state: "registration_page", controls: [trigger, duplicate] };
  assert.equal(await proposer({ provider: "doorkeeper", target_id: "DOORKEEPERDUPLICATE1", expected_state: "registered_or_pending", step: 1, observation }), null);
  assert.equal(agentCalls, 0);
  let operated = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    doorkeeperWorkflow: { async readProviderState() { return { status: "absent" }; } },
    async inspectControls() { return observation.controls; },
    async proposeAction() { return { control: trigger.control }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { return null; },
  });
  const result = await harness.performAction({ provider: "doorkeeper", candidate, page: { url() { return candidate.canonical_url; } }, action: { purpose: "submit", method: "ax_click", control: trigger.control } });
  assert.deepEqual(result, { status: "failed" });
  assert.equal(operated, 0);
});

function makeEventbriteCta(overrides = {}) {
  const element = {
    tagName: "BUTTON",
    type: "button",
    innerText: "Get tickets",
    disabled: false,
    hidden: false,
    isConnected: true,
    style: {},
    dataset: {},
    ownerDocument: { defaultView: { getComputedStyle() { return {}; } } },
    parentElement: null,
    getAttribute(name) { return name === "data-testid" ? "conversion-bar-checkout-button" : null; },
    hasAttribute(name) { return name === "hidden" && this.hidden === true; },
    getBoundingClientRect() { return this.rect || { width: 120, height: 32 }; },
    ...overrides,
  };
  return element;
}

test("Eventbrite inspector binds the unique visible top CTA to the exact candidate page", async () => {
  const candidate = {
    provider: "eventbrite",
    event_ref: "eventbrite-event://event/1901",
    canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901",
  };
  const inspect = async (elements, href = candidate.canonical_url, eventId = "1901", canonicalUrl = candidate.canonical_url) => inspectPageControls({
    provider: "eventbrite",
    event_id: eventId,
    canonical_url: canonicalUrl,
    page: { url() { return href; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } },
  });
  const valid = await inspect([makeEventbriteCta(), makeEventbriteCta({ tagName: "BUTTON", innerText: "Reserve a spot" })]);
  assert.deepEqual(valid, []);
  for (const [name, duplicate] of [
    ["visible-fuzzy", makeEventbriteCta({ innerText: "Get tickets now" })],
    ["visible-disabled", makeEventbriteCta({ disabled: true })],
  ]) {
    assert.deepEqual(await inspect([makeEventbriteCta(), duplicate]), [], name);
  }
  const single = await inspect([makeEventbriteCta()]);
  assert.equal(single.length, 1);
  assert.deepEqual({ kind: single[0].kind, label: single[0].label, submittable: single[0].submittable }, { kind: "button", label: "Get tickets", submittable: true });
  const overflow = [makeEventbriteCta(), ...Array.from({ length: 99 }, () => makeEventbriteCta({ hidden: true })), makeEventbriteCta()];
  assert.equal(overflow.length, 101);
  assert.deepEqual(await inspect(overflow), [], "eventbrite-overflow-duplicate");
  for (const [name, element, href, id, url] of [
    ["fuzzy-label", makeEventbriteCta({ innerText: "Get tickets now" })],
    ["hidden", makeEventbriteCta({ hidden: true })],
    ["disabled", makeEventbriteCta({ disabled: true })],
    ["wrong-tag", makeEventbriteCta({ tagName: "A" })],
    ["wrong-type", makeEventbriteCta({ type: "submit" })],
    ["wrong-event", makeEventbriteCta(), candidate.canonical_url, "1902", candidate.canonical_url],
    ["wrong-page", makeEventbriteCta(), "https://www.eventbrite.com/e/other-tickets-1901"],
    ["query-page", makeEventbriteCta(), `${candidate.canonical_url}?aff=1`],
  ]) {
    assert.deepEqual(await inspect([element], href, id, url), [], name);
  }
});

test("Eventbrite CTA action clicks once and succeeds only on the exact checkout frame", async () => {
  const candidate = {
    provider: "eventbrite",
    event_ref: "eventbrite-event://event/1901",
    canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901",
  };
  const trigger = { control: "eventbrite_checkout_1901", kind: "button", label: "Get tickets", required: false, completed: false, submittable: true };
  const run = async ({ frameUrls, pageUrls, action = { purpose: "submit", method: "ax_click", control: trigger.control }, controls = [trigger], provider = "eventbrite", candidateValue = candidate } = {}) => {
    let operated = 0; let resolved = 0; let reads = 0; let pageReads = 0;
    const page = {
      url() {
        pageReads += 1;
        return typeof pageUrls === "function" ? pageUrls(pageReads) : pageUrls || candidate.canonical_url;
      },
      frames() {
        reads += 1;
        const urls = typeof frameUrls === "function" ? frameUrls(reads) : frameUrls || [];
        return urls.map((entry) => {
          const frame = typeof entry === "string" ? { href: entry } : entry;
          return { url() { return frame.href; }, parentFrame() { return frame.main === true ? null : {}; } };
        });
      },
    };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      async inspectControls() { return controls; },
      async proposeAction() { return action; },
      async operateControl() { operated += 1; return { status: "success" }; },
      async resolveValue() { resolved += 1; return "private-value"; },
    });
    mock.timers.enable({ apis: ["Date", "setTimeout"] });
    try {
      const resultPromise = harness.performAction({ provider, candidate: candidateValue, page, action });
      let settled = false;
      resultPromise.then(() => { settled = true; });
      for (let attempt = 0; attempt < 1_300 && !settled; attempt += 1) {
        await Promise.resolve();
        mock.timers.tick(25);
      }
      const result = await resultPromise;
      return { result, operated, resolved };
    } finally {
      mock.timers.reset();
    }
  };
  const success = await run({ frameUrls: ["https://www.eventbrite.com/checkout-external?eid=1901"] });
  assert.deepEqual(success.result, { status: "success" });
  assert.equal(success.operated, 1);
  assert.equal(success.resolved, 0);
  const mainOnly = await run({ frameUrls: [{ href: "https://www.eventbrite.com/checkout-external?eid=1901", main: true }] });
  assert.deepEqual(mainOnly.result, { status: "failed" }, "main-frame-only");
  assert.equal(mainOnly.operated, 1);
  const parentNavigated = await run({
    frameUrls: ["https://www.eventbrite.com/checkout-external?eid=1901"],
    pageUrls: (poll) => poll <= 3 ? candidate.canonical_url : "https://www.eventbrite.com/checkout-external?eid=1901",
  });
  assert.deepEqual(parentNavigated.result, { status: "failed" }, "parent-navigation");
  assert.equal(parentNavigated.operated, 1);
  for (const [name, frameUrls] of [
    ["missing", []],
    ["wrong-origin", ["https://evil.example/checkout-external?eid=1901"]],
    ["wrong-path", ["https://www.eventbrite.com/checkout?eid=1901"]],
    ["wrong-event", ["https://www.eventbrite.com/checkout-external?eid=1902"]],
    ["exact-and-wrong-event", ["https://www.eventbrite.com/checkout-external?eid=1901", "https://www.eventbrite.com/checkout-external?eid=1902"]],
    ["duplicate", ["https://www.eventbrite.com/checkout-external?eid=1901", "https://www.eventbrite.com/checkout-external?eid=1901"]],
  ]) {
    const { result, operated } = await run({ frameUrls });
    assert.deepEqual(result, { status: "failed" }, name);
    assert.equal(operated, 1, name);
  }
  const unstable = await run({ frameUrls: (poll) => poll === 1
    ? ["https://www.eventbrite.com/checkout-external?eid=1901"]
    : ["https://www.eventbrite.com/checkout-external?eid=1901", "https://www.eventbrite.com/checkout-external?eid=1901"] });
  assert.deepEqual(unstable.result, { status: "failed" }, "frame-count-grew-after-first-poll");
  assert.equal(unstable.operated, 1);
  const lateDuplicate = await run({ frameUrls: (poll) => poll <= 2
    ? ["https://www.eventbrite.com/checkout-external?eid=1901"]
    : ["https://www.eventbrite.com/checkout-external?eid=1901", "https://www.eventbrite.com/checkout-external?eid=1901"] });
  assert.deepEqual(lateDuplicate.result, { status: "failed" }, "frame-count-grew-after-second-poll");
  assert.equal(lateDuplicate.operated, 1);
  for (const [name, action, provider, candidateValue, controls] of [
    ["wrong-action", { purpose: "fill", method: "ax_fill", control: trigger.control }],
    ["wrong-token", { purpose: "submit", method: "ax_click", control: "other_button" }],
    ["mixed-fuzzy-semantic", { purpose: "submit", method: "ax_click", control: trigger.control }, "eventbrite", candidate, [trigger, { ...trigger, control: "eventbrite_checkout_1902", label: "Get tickets now" }]],
    ["duplicate-semantic", { purpose: "submit", method: "ax_click", control: trigger.control }, "eventbrite", candidate, [trigger, { ...trigger, control: "eventbrite_checkout_1902" }]],
    ["wrong-provider", { purpose: "submit", method: "ax_click", control: trigger.control }, "luma"],
  ]) {
    const { result, operated } = await run({ frameUrls: ["https://www.eventbrite.com/checkout-external?eid=1901"], action, provider, candidateValue, controls });
    assert.deepEqual(result, { status: "failed" }, name);
    assert.equal(operated, 0, name);
  }
});

function makeEventbriteTicketElement({ tagName = "DIV", type = "", testId = "", innerText = "", ariaLabel = "", disabled = false, hidden = false, children = [], ...overrides } = {}) {
  const element = {
    tagName, type, innerText, textContent: innerText, disabled, hidden, isConnected: true,
    style: {}, dataset: testId ? { testid: testId } : {}, parentElement: null, children,
    getAttribute(name) {
      if (name === "data-testid") return testId;
      if (name === "aria-label") return ariaLabel;
      return null;
    },
    hasAttribute(name) { return name === "hidden" && hidden === true; },
    getBoundingClientRect() { return hidden ? { width: 0, height: 0 } : { width: 120, height: 32 }; },
    querySelectorAll() { return children.flatMap((child) => [child, ...(child.querySelectorAll ? child.querySelectorAll("*") : [])]); },
    ownerDocument: { defaultView: { getComputedStyle() { return {}; } } },
    ...overrides,
  };
  for (const child of children) child.parentElement = element;
  return element;
}

function makeEventbriteMarketingHandle(element, press) {
  return {
    async evaluate(callback, context) { return callback(element, context); },
    async press(key) { return press(key); },
  };
}

function makeEventbriteFinalHandle(element, click) {
  return {
    async evaluate(callback, context) { return callback(element, context); },
    async click(...args) { return click(...args); },
  };
}

function eventbriteFallbackFixture(readProviderState, { elementDelayMs = 0, clickDelayMs = 0 } = {}) {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const fields = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email")];
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [...fields, primary]; const stats = { proposals: 0, operations: 0, clicks: 0, readbacks: 0, luma: 0 };
  const handle = makeEventbriteFinalHandle(primary, async () => {
    stats.clicks += 1;
    if (clickDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, clickDelayMs));
  });
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    if (String(selector).includes("data-lm-connector-control") && String(selector).includes("eds-modal__primary-button")) return {
      async count() { return 1; },
      async elementHandles() {
        if (elementDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, elementDelayMs));
        return [handle];
      },
    };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const action = { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" };
  const harness = createProductionBrowserHarness({
    eventbriteWorkflow: { async readProviderState(input) { stats.readbacks += 1; return readProviderState(input); } },
    lumaWorkflow: { async readProviderState() { stats.luma += 1; throw new Error("wrong workflow"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { stats.proposals += 1; return { control: action.control }; },
    async operateControl(input) { stats.operations += 1; return operatePageControl(input); },
    async resolveValue() { throw new Error("resolver must not run"); },
  });
  return { harness, candidate, page, action, stats };
}

test("Eventbrite runFallback dispatches the Eventbrite workflow and never Luma", async () => {
  const fixture = eventbriteFallbackFixture(async () => ({ status: "registered", receipt_id: "evt-1901" }));
  const result = await fixture.harness.runFallback({ provider: "eventbrite", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EVENTBRITEFALLBACK1", maxSteps: 2, expectedState: "registered_or_pending" });
  assert.deepEqual(result, { status: "completed", provider_state: { status: "registered", receipt_id: "evt-1901" }, repaired_actions: [fixture.action] });
  assert.deepEqual(fixture.stats, { proposals: 1, operations: 1, clicks: 1, readbacks: 1, luma: 0 });
});

test("Eventbrite runFallback stops after one final effect_unknown without retry", async () => {
  const fixture = eventbriteFallbackFixture(async () => ({ status: "pending" }));
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const resultPromise = fixture.harness.runFallback({ provider: "eventbrite", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EVENTBRITEEFFECTUNKNOWN1", maxSteps: 2, expectedState: "registered_or_pending" });
    let settled = false; resultPromise.then(() => { settled = true; }, () => { settled = true; });
    for (let attempt = 0; attempt < 1_300 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
    assert.deepEqual(await resultPromise, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [] });
  } finally { mock.timers.reset(); }
  assert.equal(fixture.stats.proposals, 1); assert.equal(fixture.stats.operations, 1); assert.equal(fixture.stats.clicks, 1); assert.equal(fixture.stats.luma, 0); assert.ok(fixture.stats.readbacks > 0);
});

test("Eventbrite final timeout before handle dispatch has zero clicks, while dispatch-start timeout is effect_unknown", async () => {
  const before = eventbriteFallbackFixture(async () => ({ status: "absent" }), { elementDelayMs: 30 });
  const beforeResult = await before.harness.runFallback({
    provider: "eventbrite", candidate: before.candidate, page: before.page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EVENTBRITE-PRE-DISPATCH",
    maxSteps: 2, maxDurationMs: 5, expectedState: "registered_or_pending",
  });
  assert.deepEqual(beforeResult, { status: "failed", safe_reason: "time_limit", repaired_actions: [] });
  assert.equal(before.stats.clicks, 0);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(before.stats.clicks, 0);

  const after = eventbriteFallbackFixture(async () => ({ status: "registered" }), { clickDelayMs: 30 });
  const afterResult = await after.harness.runFallback({
    provider: "eventbrite", candidate: after.candidate, page: after.page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EVENTBRITE-AFTER-DISPATCH",
    maxSteps: 2, maxDurationMs: 5, expectedState: "registered_or_pending",
  });
  assert.deepEqual(afterResult, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [] });
  assert.equal(after.stats.clicks, 1);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(after.stats.clicks, 1);
  assert.equal(after.stats.proposals, 1);
});

test("Eventbrite inspector exposes the exact free ticket Register control from the matching checkout child frame", async () => {
  const candidate = {
    provider: "eventbrite",
    event_ref: "eventbrite-event://event/1901",
    canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901",
  };
  const increase = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-stepper-increase-button", ariaLabel: "Increase quantity" });
  const decrease = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-stepper-decrease-button", ariaLabel: "Decrease quantity", disabled: true });
  const quantity = makeEventbriteTicketElement({ testId: "eds-stepper-quantity", innerText: "1" });
  const stepper = makeEventbriteTicketElement({ testId: "eds-stepper", children: [decrease, quantity, increase] });
  const price = makeEventbriteTicketElement({ testId: "ticket-price__price", innerText: "Free" });
  const card = makeEventbriteTicketElement({ testId: "ticket-display-card-content-full-size", innerText: "General Admission", children: [stepper, price] });
  let primaryTestId = "eds-modal__primary-button";
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register", getAttribute(name) {
    if (name === "data-testid") return primaryTestId;
    if (name === "aria-label") return "Register";
    return null;
  } });
  let ticketVisible = true; let operated = 0; let operatedFrame; let frameExtras = [];
  let frameInspectReads = 0; let mutationMode = "";
  let pageHref = candidate.canonical_url; let frameHref = "https://www.eventbrite.com/checkout-external?eid=1901"; let pageFrames;
  const frameElements = () => {
    frameInspectReads += 1;
    if (mutationMode && frameInspectReads === 2) {
      if (mutationMode === "paid") card.innerText = "General Admission Free 1,000円";
      if (mutationMode === "duplicate") frameExtras = [makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: primary.dataset.testid, innerText: "Register" })];
      if (mutationMode === "disabled") primary.disabled = true;
      if (mutationMode === "drift-page") pageHref = "https://www.eventbrite.com/e/tokyo-free-event-tickets-1902";
      if (mutationMode === "drift-frame") pageFrames = [frame, frame];
      if (mutationMode === "drift-eid") frameHref = "https://www.eventbrite.com/checkout-external?eid=1902";
    }
    return ticketVisible ? [card, stepper, quantity, increase, decrease, price, primary, ...frameExtras] : [primary];
  };
  const frame = {
    url() { return frameHref; },
    parentFrame() { return {}; },
    locator() { return { async evaluateAll(callback, context) { return callback(frameElements(), context); } }; },
  };
  pageFrames = [frame];
  const page = {
    url() { return pageHref; },
    frames() { return pageFrames; },
    locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; },
  };
  for (const marker of ["$10", "JPY 1,000", "USD 10", "1,000円", "1000 円", "1,000 JPY", "10 USD", "cash", "paid", "door fee", "minimum purchase", "会場払い", "当日払い", "有料"]) {
    card.innerText = `General Admission Free ${marker}`;
    assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], marker);
  }
  card.innerText = "General Admission Free";
  for (const [name, style] of [
    ["element-display-none", { display: "none" }],
    ["element-visibility-hidden", { visibility: "hidden" }],
    ["element-visibility-collapse", { visibility: "collapse" }],
    ["element-content-visibility-hidden", { contentVisibility: "hidden" }],
    ["element-opacity-zero", { opacity: "0" }],
  ]) {
    primary.style = style;
    assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], name);
    primary.style = {};
  }
  for (const [name, style] of [
    ["ancestor-display-none", { display: "none" }],
    ["ancestor-visibility-hidden", { visibility: "hidden" }],
    ["ancestor-visibility-collapse", { visibility: "collapse" }],
    ["ancestor-content-visibility-hidden", { contentVisibility: "hidden" }],
    ["ancestor-opacity-zero", { opacity: "0" }],
  ]) {
    primary.parentElement = { style, parentElement: null };
    assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], name);
    primary.parentElement = null;
  }
  const ownerDocument = primary.ownerDocument;
  primary.ownerDocument = { defaultView: { getComputedStyle() { return { opacity: "0" }; } } };
  assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], "computed-opacity-zero");
  primary.ownerDocument = ownerDocument;
  card.innerText = "General Admission Free";
  primaryTestId = "EDS-MODAL__PRIMARY-BUTTON";
  assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], "case-sensitive-primary-testid");
  primaryTestId = "eds-modal__primary-button";
  primary.disabled = true;
  assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], "disabled-primary");
  primary.disabled = false;
  for (const [index, duplicate] of [
    makeEventbriteTicketElement({ testId: card.dataset.testid, innerText: "Duplicate card" }),
    makeEventbriteTicketElement({ testId: stepper.dataset.testid }),
    makeEventbriteTicketElement({ testId: quantity.dataset.testid, innerText: "1" }),
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: increase.dataset.testid, ariaLabel: "Increase quantity" }),
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: decrease.dataset.testid, ariaLabel: "Decrease quantity", disabled: true }),
    makeEventbriteTicketElement({ testId: price.dataset.testid, innerText: "Free" }),
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: primary.dataset.testid, innerText: "Register" }),
  ].entries()) {
    frameExtras = [duplicate];
    if (index === 1 || index === 5) { card.children.push(duplicate); duplicate.parentElement = card; }
    if (index >= 2 && index <= 4) { stepper.children.push(duplicate); duplicate.parentElement = stepper; }
    assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], `same-testid-duplicate-${index}-${duplicate.dataset.testid}`);
    if (index === 1 || index === 5) card.children.pop();
    if (index >= 2 && index <= 4) stepper.children.pop();
  }
  frameExtras = [];
  for (const duplicate of [
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: primary.dataset.testid, innerText: "Register now" }),
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: primary.dataset.testid, innerText: "Register", hidden: true }),
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: primary.dataset.testid, innerText: "Register", disabled: true }),
  ]) {
    frameExtras = [duplicate];
    assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), [], "primary-candidate-duplicate");
  }
  frameExtras = [];
  const controls = await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url });
  assert.deepEqual(controls, [{ control: "eventbrite_ticket_register_1901", kind: "button", label: "Register", required: false, completed: false, submittable: true }]);
  card.innerText = "General Admission Free — USD tickets only";
  assert.deepEqual(await inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url }), controls, "currency-code-without-amount-is-not-paid");
  card.innerText = "General Admission Free";
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: controls[0].control }; },
    async operateControl(input) { operated += 1; operatedFrame = input.frame; ticketVisible = false; return { status: "success" }; },
    async resolveValue() { throw new Error("private value must not resolve"); },
  });
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const resultPromise = harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: controls[0].control } });
    let settled = false; resultPromise.then(() => { settled = true; });
    for (let attempt = 0; attempt < 1_300 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
    assert.deepEqual(await resultPromise, { status: "success" });
  } finally { mock.timers.reset(); }
  assert.equal(operated, 1);
  assert.equal(operatedFrame, frame);

  ticketVisible = true;
  frameInspectReads = 0;
  let timeoutOperated = 0;
  const timeoutHarness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: controls[0].control }; },
    async operateControl(input) { timeoutOperated += 1; assert.equal(input.frame, frame); return { status: "success" }; },
    async resolveValue() { throw new Error("private value must not resolve"); },
  });
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const timeoutPromise = timeoutHarness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: controls[0].control } });
    let settled = false; timeoutPromise.then(() => { settled = true; });
    for (let attempt = 0; attempt < 1_300 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
    assert.deepEqual(await timeoutPromise, { status: "failed", safe_reason: "effect_unknown" });
  } finally { mock.timers.reset(); }
  assert.equal(timeoutOperated, 1);

  for (const mutation of ["paid", "duplicate", "disabled", "drift-page", "drift-frame", "drift-eid"]) {
    ticketVisible = true;
    card.innerText = "General Admission Free";
    primary.disabled = false;
    frameExtras = [];
    frameInspectReads = 0;
    pageHref = candidate.canonical_url;
    frameHref = "https://www.eventbrite.com/checkout-external?eid=1901";
    pageFrames = [frame];
    mutationMode = mutation;
    let mutationOperated = 0;
    const mutationHarness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("readback must not run"); } },
      async inspectControls(input) { return inspectPageControls(input); },
      async proposeAction() { return { control: controls[0].control }; },
      async operateControl() { mutationOperated += 1; ticketVisible = false; return { status: "success" }; },
      async resolveValue() { throw new Error("private value must not resolve"); },
    });
    mock.timers.enable({ apis: ["Date", "setTimeout"] });
    try {
      const mutationPromise = mutationHarness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: controls[0].control } });
      let settled = false; mutationPromise.then(() => { settled = true; });
      for (let attempt = 0; attempt < 1_300 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
      assert.deepEqual(await mutationPromise, { status: "failed" }, `pre-operate-${mutation}`);
    } finally { mock.timers.reset(); }
    assert.equal(mutationOperated, 0, `pre-operate-${mutation}-must-not-click`);
    mutationMode = "";
  }
});

test("Eventbrite attendee inspector exposes only the exact required fields after the ticket card is gone", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text", value = "") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value });
  const first = field("buyer.N-first_name", "text", " Given ");
  const last = field("buyer.N-last_name");
  const email = field("buyer.N-email", "email", "person@example.test");
  const confirm = field("buyer.confirmEmailAddress", "email");
  const marketing = field("marketing_opt_in", "checkbox"); marketing.required = false;
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const base = [first, last, email, confirm, marketing, primary];
  let frameElements = base;
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator() { return { async evaluateAll(callback, context) { return callback(frameElements, context); } }; } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const inspect = () => inspectPageControls({ page, provider: "eventbrite", event_id: "1901", canonical_url: candidate.canonical_url });
  const expected = [{ control: "eventbrite_attendee_first_name_1901", kind: "input", label: "First name", required: true, completed: true, submittable: false }, { control: "eventbrite_attendee_last_name_1901", kind: "input", label: "Last name", required: true, completed: false, submittable: false }, { control: "eventbrite_attendee_email_1901", kind: "input", label: "Email", required: true, completed: true, submittable: false }, { control: "eventbrite_attendee_confirm_email_1901", kind: "input", label: "Confirm email", required: true, completed: false, submittable: false }];
  assert.deepEqual(await inspect(), expected);
  assert.doesNotMatch(JSON.stringify(await inspect()), /Given|person@example\.test|buyer\.(?:1|N)/);
  for (const [name, duplicate] of [["hidden-duplicate", { ...first, hidden: true }], ["disabled-duplicate", { ...first, disabled: true }], ["detached-duplicate", { ...first, isConnected: false }]]) {
    frameElements = [...base, duplicate];
    assert.deepEqual(await inspect(), [], name);
  }
  frameElements = base;
  for (const [name, extra] of [
    ["ticket-card-remains", makeEventbriteTicketElement({ testId: "ticket-display-card-content-full-size" })],
    ["unknown-required", makeEventbriteTicketElement({ tagName: "SELECT", name: "unknown", required: true })],
    ["duplicate", field("buyer.N-first_name")],
    ["wrong-type", { ...first, type: "email" }],
    ["wrong-tag", { ...first, tagName: "SELECT" }],
    ["wrong-name", { ...first, name: "buyer.N-middle_name" }],
    ["hidden", { ...first, hidden: true }],
    ["disabled", { ...first, disabled: true }],
  ]) {
    frameElements = ["duplicate", "ticket-card-remains", "unknown-required"].includes(name) ? [...base, extra] : [extra, last, email, confirm, marketing, primary];
    assert.deepEqual(await inspect(), [], name);
  }
  frameElements = [...base, ...Array.from({ length: 95 }, (_, index) => field(`unknown.${index}`))];
  assert.deepEqual(await inspect(), [], "over-100-controls");
  frameElements = base; const originalLocator = frame.locator;
  frame.locator = () => { throw new Error("fixture locator failure"); };
  assert.deepEqual(await inspect(), [], "locator-error");
  frame.locator = originalLocator; let operated = 0; let resolved = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: expected[0].control }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { resolved += 1; return "private"; },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_fill", control: expected[0].control } }), { status: "failed" });
  assert.equal(operated, 0);
  assert.equal(resolved, 0);
});

test("Eventbrite attendee inspector accepts the live literal N attendee field names", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "" });
  const makeFields = (names) => names.map((name, index) => field(name, index > 1 ? "email" : "text"));
  let elements = makeFields(["buyer.N-first_name", "buyer.N-last_name", "buyer.N-email", "buyer.confirmEmailAddress"]);
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const inspect = () => inspectPageControls({ provider: "eventbrite", page, event_id: "1901", canonical_url: candidate.canonical_url });
  assert.deepEqual(await inspect(), [
    { control: "eventbrite_attendee_first_name_1901", kind: "input", label: "First name", required: true, completed: false, submittable: false },
    { control: "eventbrite_attendee_last_name_1901", kind: "input", label: "Last name", required: true, completed: false, submittable: false },
    { control: "eventbrite_attendee_email_1901", kind: "input", label: "Email", required: true, completed: false, submittable: false },
    { control: "eventbrite_attendee_confirm_email_1901", kind: "input", label: "Confirm email", required: true, completed: false, submittable: false },
  ]);
  for (const names of [
    ["buyer.1-first_name", "buyer.1-last_name", "buyer.1-email", "buyer.confirmEmailAddress"],
    ["buyer.n-first_name", "buyer.n-last_name", "buyer.n-email", "buyer.confirmEmailAddress"],
    ["buyer.N-first_name-extra", "buyer.N-last_name", "buyer.N-email", "buyer.confirmEmailAddress"],
  ]) {
    elements = makeFields(names);
    assert.deepEqual(await inspect(), [], names[0]);
  }
});

test("Eventbrite attendee inspector exposes and binds the exact final Register control after exact4 completion", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const first = field("buyer.N-first_name"); const last = field("buyer.N-last_name"); const email = field("buyer.N-email", "email"); const confirm = field("buyer.confirmEmailAddress", "email");
  let primaryTestId = "eds-modal__primary-button";
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register", getAttribute(name) {
    if (name === "data-testid") return primaryTestId;
    if (name === "aria-label") return "Register";
    return null;
  } });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", required: false, checked: false });
  const base = [first, last, email, confirm, primary, marketing]; let elements = [...base];
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const inspect = () => inspectPageControls({ provider: "eventbrite", page, event_id: "1901", canonical_url: candidate.canonical_url });
  assert.deepEqual(await inspect(), [
    { control: "eventbrite_attendee_first_name_1901", kind: "input", label: "First name", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_last_name_1901", kind: "input", label: "Last name", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_email_1901", kind: "input", label: "Email", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_confirm_email_1901", kind: "input", label: "Confirm email", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_register_1901", kind: "button", label: "Register", required: false, completed: false, submittable: true },
  ]);
  assert.equal(primary.dataset.lmConnectorControl, "eventbrite_attendee_register_1901");
  const reset = () => { first.value = "complete"; last.value = "complete"; email.value = "complete"; confirm.value = "complete"; primaryTestId = "eds-modal__primary-button"; primary.tagName = "BUTTON"; primary.type = "button"; primary.innerText = "Register"; primary.textContent = "Register"; primary.hidden = false; primary.isConnected = true; primary.disabled = false; marketing.checked = false; marketing.required = false; marketing.hidden = false; marketing.isConnected = true; marketing.disabled = false; elements = [...base]; };
  for (const [name, mutate] of [
    ["incomplete", () => { first.value = ""; }],
    ["primary-duplicate", () => { elements = [...base, makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" })]; }],
    ["primary-wrong-testid", () => { primaryTestId = "other"; }], ["primary-wrong-tag", () => { primary.tagName = "DIV"; }], ["primary-wrong-type", () => { primary.type = "submit"; }],
    ["primary-wrong-label", () => { primary.innerText = "Continue"; primary.textContent = "Continue"; }], ["primary-hidden", () => { primary.hidden = true; }], ["primary-detached", () => { primary.isConnected = false; }], ["primary-disabled", () => { primary.disabled = true; }],
    ["marketing-checked", () => { marketing.checked = true; }], ["marketing-required", () => { marketing.required = true; }], ["marketing-hidden", () => { marketing.hidden = true; }], ["marketing-disabled", () => { marketing.disabled = true; }],
    ["marketing-duplicate", () => { elements = [...base, makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", required: false, checked: false })]; }],
  ]) {
    reset(); mutate(); assert.equal((await inspect()).some((control) => control.control === "eventbrite_attendee_register_1901"), false, name);
  }
  reset(); elements = [...base, ...Array.from({ length: 96 }, (_, index) => makeEventbriteTicketElement({ tagName: "INPUT", type: "text", name: `extra-${index}` }))];
  assert.deepEqual(await inspect(), [], "over-101");
  reset(); let resolved = 0; let operated = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("final readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: "eventbrite_attendee_register_1901" }; },
    async operateControl() { operated += 1; return { status: "success" }; },
    async resolveValue() { resolved += 1; return "unexpected"; },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" } }), { status: "failed" });
  assert.equal(resolved, 0); assert.equal(operated, 0); assert.equal(primary.dataset.lmConnectorControl, "eventbrite_attendee_register_1901");
});

test("Eventbrite final Register clicks the original primary handle once and accepts registered readback", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const fields = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email")];
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", required: false, checked: false });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [...fields, primary, marketing];
  let clicks = 0; let resolves = 0; let reads = 0; let operates = 0; let frameLive = true;
  const handle = makeEventbriteFinalHandle(primary, (...args) => { assert.equal(args.length, 0); clicks += 1; frameLive = false; });
  const finalSelector = (selector) => String(selector).includes("data-lm-connector-control") && String(selector).includes("eds-modal__primary-button");
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    if (finalSelector(selector)) return { async count() { return 1; }, async elementHandles() { return [handle]; } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return frameLive ? [frame] : []; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    eventbriteWorkflow: { async readProviderState({ page: readPage, candidate: readCandidate }) { assert.equal(readPage, page); assert.equal(readCandidate, candidate); reads += 1; return { status: "registered", receipt_id: "evt-1901" }; } },
    lumaWorkflow: { async readProviderState() { throw new Error("luma readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: "eventbrite_attendee_register_1901" }; },
    async operateControl(input) { operates += 1; return operatePageControl(input); },
    async resolveValue() { resolves += 1; return "unexpected"; },
  });
  const result = await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" } });
  assert.deepEqual(result, { status: "success", provider_state: { status: "registered", receipt_id: "evt-1901" } });
  assert.equal(clicks, 1); assert.equal(operates, 1); assert.equal(resolves, 0); assert.equal(reads, 1);
});

test("Eventbrite final Register never retries unknown effects and rejects a pre-click decoy", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const fields = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email")];
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", required: false, checked: false });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [...fields, primary, marketing]; let clicks = 0; let locatorMode = "normal";
  const handle = makeEventbriteFinalHandle(primary, () => { clicks += 1; });
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    if (String(selector).includes("data-lm-connector-control")) return { async count() { return locatorMode === "duplicate" ? 2 : 1; }, async elementHandles() { return [handle]; } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = (readProviderState, operateControl = (input) => operatePageControl(input)) => createProductionBrowserHarness({
    eventbriteWorkflow: { readProviderState }, lumaWorkflow: { async readProviderState() { throw new Error("wrong workflow"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: "eventbrite_attendee_register_1901" }; }, operateControl,
    async resolveValue() { throw new Error("resolver must not run"); },
  });

  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const unknownPromise = harness(async () => ({ status: "pending" })).performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" } });
    let settled = false; unknownPromise.then(() => { settled = true; });
    for (let attempt = 0; attempt < 1_300 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
    assert.deepEqual(await unknownPromise, { status: "failed", safe_reason: "effect_unknown" }); assert.equal(clicks, 1);
  } finally { mock.timers.reset(); }

  const registered = await harness(async () => ({ status: "registered", receipt_id: "evt-1901" }), async (input) => { await operatePageControl(input); return { status: "failed" }; }).performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" } });
  assert.deepEqual(registered, { status: "success", provider_state: { status: "registered", receipt_id: "evt-1901" } }); assert.equal(clicks, 2);

  locatorMode = "duplicate";
  const decoy = await harness(async () => ({ status: "registered" })).performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" } });
  assert.deepEqual(decoy, { status: "failed" }); assert.equal(clicks, 2);
});

test("Eventbrite final Register pins the original handle before a count-time token swap", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const fields = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email")];
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", required: false, checked: false });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const decoy = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [...fields, primary, marketing]; let countCalls = 0; let originalClicks = 0; let decoyClicks = 0;
  const originalHandle = makeEventbriteFinalHandle(primary, () => { originalClicks += 1; });
  const decoyHandle = makeEventbriteFinalHandle(decoy, () => { decoyClicks += 1; });
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    if (!String(selector).includes("data-lm-connector-control")) return { async evaluateAll(callback, context) { return callback(elements, context); } };
    return {
      async count() { countCalls += 1; if (countCalls === 1) { primary.isConnected = false; primary.dataset.lmConnectorControl = "stale"; decoy.dataset.lmConnectorControl = "eventbrite_attendee_register_1901"; } return 1; },
      async elementHandles() { return [countCalls === 0 ? originalHandle : decoyHandle]; },
    };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    eventbriteWorkflow: { async readProviderState() { return { status: "registered" }; } }, lumaWorkflow: { async readProviderState() { throw new Error("wrong workflow"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: "eventbrite_attendee_register_1901" }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  const result = await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1901" } });
  assert.deepEqual(result, { status: "failed" }); assert.equal(originalClicks, 0); assert.equal(decoyClicks, 0);
});

test("Eventbrite checked marketing input exposes a fixed opt-out control without a DOM label", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const eventbriteMarketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "ebMarketingOptIn", id: "eb-opt-in", required: false, checked: true });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, eventbriteMarketing, primary];
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  assert.deepEqual(await inspectPageControls({ provider: "eventbrite", page, event_id: "1901", canonical_url: candidate.canonical_url }), [
    { control: "eventbrite_attendee_first_name_1901", kind: "input", label: "First name", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_last_name_1901", kind: "input", label: "Last name", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_email_1901", kind: "input", label: "Email", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_confirm_email_1901", kind: "input", label: "Confirm email", required: true, completed: true, submittable: false },
    { control: "eventbrite_marketing_opt_out_organization_1901", kind: "checkbox", label: "Organizer marketing opt-out", required: true, completed: false, submittable: false },
    { control: "eventbrite_marketing_opt_out_eventbrite_1901", kind: "checkbox", label: "Eventbrite marketing opt-out", required: true, completed: false, submittable: false },
  ]);
  assert.equal(marketing.dataset.lmConnectorControl, "eventbrite_marketing_opt_out_organization_1901");
  assert.equal(eventbriteMarketing.dataset.lmConnectorControl, "eventbrite_marketing_opt_out_eventbrite_1901");
});

test("Eventbrite checked marketing input rejects global id collisions outside the actionable selector", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const actionableSelector = "input, textarea, select, button, [data-testid]";
  for (const duplicate of [
    makeEventbriteTicketElement({ tagName: "LABEL", id: "org-opt-in", innerText: "Organization updates" }),
    makeEventbriteTicketElement({ tagName: "DIV", id: "org-opt-in" }),
    makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", id: "org-opt-in", innerText: "Other button" }),
  ]) {
    const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
    const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, duplicate, primary];
    const selected = (selector) => selector === actionableSelector
      ? elements.filter((element) => ["input", "textarea", "select", "button"].includes(String(element.tagName || "").toLowerCase()) || element.dataset?.testid)
      : selector === "[id]" ? elements.filter((element) => Boolean(element.id)) : [];
    const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
      const matches = selected(selector);
      return { async count() { return matches.length; }, async evaluateAll(callback, context) { return callback(matches, context); } };
    } };
    const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
    assert.deepEqual(await inspectPageControls({ provider: "eventbrite", page, event_id: "1901", canonical_url: candidate.canonical_url }), [
      { control: "eventbrite_attendee_first_name_1901", kind: "input", label: "First name", required: true, completed: true, submittable: false },
      { control: "eventbrite_attendee_last_name_1901", kind: "input", label: "Last name", required: true, completed: true, submittable: false },
      { control: "eventbrite_attendee_email_1901", kind: "input", label: "Email", required: true, completed: true, submittable: false },
      { control: "eventbrite_attendee_confirm_email_1901", kind: "input", label: "Confirm email", required: true, completed: true, submittable: false },
    ]);
    assert.equal(marketing.dataset.lmConnectorControl, undefined);
  }
});

test("Eventbrite marketing actions require the exact fixed semantic label", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const attendeeControls = [
    { control: "eventbrite_attendee_first_name_1901", kind: "input", label: "First name", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_last_name_1901", kind: "input", label: "Last name", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_email_1901", kind: "input", label: "Email", required: true, completed: true, submittable: false },
    { control: "eventbrite_attendee_confirm_email_1901", kind: "input", label: "Confirm email", required: true, completed: true, submittable: false },
  ];
  const page = { url() { return candidate.canonical_url; }, frames() { return []; } };
  for (const [token, label] of [
    ["eventbrite_marketing_opt_out_organization_1901", "Eventbrite marketing opt-out"],
    ["eventbrite_marketing_opt_out_eventbrite_1901", "Organizer marketing opt-out"],
    ["eventbrite_marketing_opt_out_organization_1901", "Organization updates"],
  ]) {
    let resolved = 0; let operated = 0;
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
      async proposeAction() { return { control: token }; },
      async inspectControls() { return [...attendeeControls, { control: token, kind: "checkbox", label, required: true, completed: false, submittable: false }]; },
      async operateControl() { operated += 1; return { status: "success" }; },
      async resolveValue() { resolved += 1; return "unexpected"; },
    });
    assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: token } }), { status: "failed" }, label);
    assert.equal(resolved, 0, `${label}-resolve`); assert.equal(operated, 0, `${label}-operate`);

    const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: token.includes("_eventbrite_") ? "ebMarketingOptIn" : "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
    marketing.dataset.lmConnectorControl = token;
    let presses = 0;
    const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
      const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
      if (!exact) return { async count() { return 0; } };
      return { async elementHandles() { return [makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; })]; }, async count() { return exact[1] === token ? 1 : 0; } };
    } };
    const direct = await operatePageControl({ page: { url() { return candidate.canonical_url; } }, frame, control: { control: token, kind: "checkbox", label, required: true, completed: false, submittable: false }, action: { purpose: "fill", method: "ax_uncheck", control: token } });
    assert.equal(direct.status, "failed", `${label}-direct`); assert.equal(presses, 0, `${label}-press`);
  }
});

test("Eventbrite marketing opt-out presses Space on the verified input once without resolving a value", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "ebMarketingOptIn", id: "eb-opt-in", required: false, checked: true, hasAttribute(name) { return name === "checked"; } });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Event updates", getAttribute(name) { return name === "for" ? "eb-opt-in" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, primary];
  let presses = 0; let labelClicks = 0; let registerClicks = 0; let operateCalls = 0; let resolveCalls = 0;
  marketing.click = () => { presses += 1; marketing.checked = false; };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; });
      return { async elementHandles() { return [handle]; }, async count() { return exact[1] === "eventbrite_marketing_opt_out_eventbrite_1901" && exact[2] === "ebMarketingOptIn" ? 1 : 0; } };
    }
    const token = /^\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const labelToken = /^label\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const compoundToken = /^label\[data-lm-connector-control="([^"]+)"\]\[for="([^"]+)"\]$/.exec(selector)?.[1];
    if (token || labelToken || compoundToken) return { async count() { return (token || labelToken || compoundToken) === "eventbrite_marketing_opt_out_eventbrite_1901" ? 1 : 0; }, async click() { labelClicks += 1; if (labelToken || compoundToken) registerClicks += 1; marketing.checked = false; } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: "eventbrite_marketing_opt_out_eventbrite_1901" }; },
    async operateControl(input) { operateCalls += 1; assert.equal(input.frame, frame); return operatePageControl(input); },
    async resolveValue() { resolveCalls += 1; return "unexpected"; },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: "eventbrite_marketing_opt_out_eventbrite_1901" } }), { status: "success" });
  assert.equal(presses, 1); assert.equal(labelClicks, 0); assert.equal(registerClicks, 0); assert.equal(operateCalls, 1); assert.equal(resolveCalls, 0); assert.equal(marketing.checked, false); assert.equal(primary.dataset.lmConnectorControl, "eventbrite_attendee_register_1901");
});

test("Eventbrite marketing opt-out and final paths fail closed for DOM and action variants", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  let labelFor = "org-opt-in";
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? labelFor : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const base = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, primary];
  const optToken = "eventbrite_marketing_opt_out_organization_1901"; const finalToken = "eventbrite_attendee_register_1901"; let elements = [...base];
  let mode = ""; let frameReads = 0; let pageFrameReads = 0; let pageHref = candidate.canonical_url; let frameHref = "https://www.eventbrite.com/checkout-external?eid=1901"; let pageFrames; let locatorCount = 1; let clicks = 0; let presses = 0; let operates = 0; let resolves = 0;
  marketing.click = () => {
    presses += 1;
    if (mode === "click-error") throw new Error("click failed");
    if (mode === "hidden-postcondition") { marketing.hidden = true; return; }
    if (mode !== "postcondition") marketing.checked = false;
  };
  const replacementFrame = { url() { return frameHref; }, parentFrame() { return {}; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  const frame = { url() { return frameHref; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; if (mode === "press-error") throw new Error("press failed"); if (mode === "hidden-postcondition") { marketing.hidden = true; return; } if (mode !== "postcondition") marketing.checked = false; });
      if (mode === "missing-press") delete handle.press;
      return { async elementHandles() { return [handle]; }, async count() { return exact[1] === optToken && exact[2] === "organizationMarketingOptIn" ? locatorCount : 0; } };
    }
    const token = /^\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const labelToken = /^label\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const compoundToken = /^label\[data-lm-connector-control="([^"]+)"\]\[for="([^"]+)"\]$/.exec(selector)?.[1];
    if (token || labelToken || compoundToken) return { async count() { return locatorCount; }, async click() { clicks += 1; if (mode === "click-error") throw new Error("click failed"); if (mode === "hidden-postcondition") marketing.hidden = true; else if (mode !== "postcondition") marketing.checked = false; } };
    return { async evaluateAll(callback, context) { frameReads += 1; if (mode === "dom-drift" && frameReads === 3) elements = [...elements, field("buyer.N-first_name")]; return callback(elements, context); } };
  } };
  pageFrames = [frame];
  const page = { url() { return pageHref; }, frames() { pageFrameReads += 1; if (pageFrameReads === 3 && mode === "page-drift") pageHref = "https://www.eventbrite.com/e/tokyo-free-event-tickets-1902"; if (pageFrameReads === 3 && mode === "eid-drift") frameHref = "https://www.eventbrite.com/checkout-external?eid=1902"; if (pageFrameReads === 3 && mode === "frame-drift") return [replacementFrame]; return pageFrames; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const inspect = () => inspectPageControls({ provider: "eventbrite", page, event_id: "1901", canonical_url: candidate.canonical_url });
  const reset = () => { elements = [...base]; marketing.tagName = "INPUT"; marketing.type = "checkbox"; marketing.checked = true; marketing.required = false; marketing.hidden = false; marketing.isConnected = true; marketing.disabled = false; labelFor = "org-opt-in"; label.hidden = false; label.isConnected = true; primary.dataset.lmConnectorControl = undefined; pageHref = candidate.canonical_url; frameHref = "https://www.eventbrite.com/checkout-external?eid=1901"; pageFrames = [frame]; pageFrameReads = 0; frameReads = 0; locatorCount = 1; clicks = 0; presses = 0; operates = 0; resolves = 0; mode = ""; };
  for (const [name, mutate, expectedOpt, expectedFinal] of [
    ["unchecked", () => { marketing.checked = false; }, false, true], ["checked2", () => { const eb = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "ebMarketingOptIn", id: "eb-opt-in", required: false, checked: true }); const ebLabel = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Event updates", getAttribute(key) { return key === "for" ? "eb-opt-in" : null; } }); elements.push(eb, ebLabel); }, true, false],
    ["duplicate", () => { elements.push({ ...marketing }); }, false, false], ["raw-id-other-tag", () => { elements.push(makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", id: "org-opt-in" })); }, false, false], ["wrong-tag", () => { marketing.tagName = "DIV"; }, false, false], ["wrong-type", () => { marketing.type = "text"; }, false, false], ["required", () => { marketing.required = true; }, false, false], ["hidden", () => { marketing.hidden = true; }, false, false], ["detached", () => { marketing.isConnected = false; }, false, false], ["disabled", () => { marketing.disabled = true; }, false, false],
  ]) {
    reset(); mutate(); const controls = await inspect(); assert.equal(controls.some((control) => control.control === optToken), expectedOpt, name); assert.equal(controls.some((control) => control.control === finalToken), expectedFinal, name);
  }
  reset(); elements = [...base, ...Array.from({ length: 94 }, (_, index) => makeEventbriteTicketElement({ tagName: "INPUT", type: "text", name: `extra-${index}` }))]; assert.deepEqual(await inspect(), [], "over-101");
  const harness = createProductionBrowserHarness({ lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } }, async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: optToken }; }, async operateControl(input) { operates += 1; return operatePageControl(input); }, async resolveValue() { resolves += 1; return "unexpected"; } });
  for (const [name, action, mutate, expectedOperate, expectedClicks, expectedPresses] of [
    ["wrong-action", { purpose: "fill", method: "ax_click", control: optToken }, null, 0, 0, 0], ["page-drift", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "page-drift"; }, 0, 0, 0], ["frame-drift", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "frame-drift"; }, 0, 0, 0], ["eid-drift", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "eid-drift"; }, 0, 0, 0], ["dom-drift", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "dom-drift"; }, 0, 0, 0], ["locator0", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { locatorCount = 0; mode = "locator0"; }, 1, 0, 0], ["locator2", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { locatorCount = 2; mode = "locator2"; }, 1, 0, 0], ["missing-press", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "missing-press"; }, 1, 0, 0], ["press-error", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "press-error"; }, 1, 0, 1], ["postcondition", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "postcondition"; }, 1, 0, 1], ["hidden-postcondition", { purpose: "fill", method: "ax_uncheck", control: optToken }, () => { mode = "hidden-postcondition"; }, 1, 0, 1],
    ["final-action", { purpose: "submit", method: "ax_click", control: finalToken }, () => { marketing.checked = false; }, 0, 0, 0],
  ]) {
    reset(); mutate?.(); const result = await harness.performAction({ provider: "eventbrite", candidate, page, action }); assert.deepEqual(result, { status: "failed" }, name); assert.equal(operates, expectedOperate, `${name}-operate`); assert.equal(clicks, expectedClicks, `${name}-label`); assert.equal(presses, expectedPresses, `${name}-press`); assert.equal(resolves, 0, `${name}-resolve`);
  }
});

test("Eventbrite marketing token swap cannot activate the Register button", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? "org-opt-in" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, primary];
  const optToken = "eventbrite_marketing_opt_out_organization_1901"; let registerClicks = 0; let presses = 0; let labelClicks = 0; let operateCalls = 0;
  marketing.click = () => { presses += 1; marketing.checked = false; };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      marketing.dataset.lmConnectorControl = undefined;
      primary.dataset.lmConnectorControl = optToken;
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; });
      return { async elementHandles() { return [handle]; }, async count() { return 0; } };
    }
    const token = /^\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const labelToken = /^label\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const compoundToken = /^label\[data-lm-connector-control="([^"]+)"\]\[for="([^"]+)"\]$/.exec(selector)?.[1];
    if (token) {
      label.dataset.lmConnectorControl = undefined;
      primary.dataset.lmConnectorControl = token;
      return { async count() { return elements.filter((element) => element.dataset?.lmConnectorControl === token).length; }, async click() { labelClicks += 1; if (primary.dataset.lmConnectorControl === token) registerClicks += 1; else marketing.checked = false; } };
    }
    if (labelToken || compoundToken) return { async count() { return elements.filter((element) => element.dataset?.lmConnectorControl === (labelToken || compoundToken) && element.tagName === "LABEL").length; }, async click() { labelClicks += 1; marketing.checked = false; } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: optToken }; },
    async operateControl(input) { operateCalls += 1; return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: optToken } }), { status: "failed" });
  assert.equal(operateCalls, 1); assert.equal(presses, 0); assert.equal(labelClicks, 0); assert.equal(registerClicks, 0);
});

test("Eventbrite marketing final locator fails closed for label and input drift", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? "org-opt-in" : null; } });
  const registerLabel = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Register copy", getAttribute(name) { return name === "for" ? "register" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, registerLabel, primary];
  const optToken = "eventbrite_marketing_opt_out_organization_1901"; let mode = ""; let clicks = 0; let presses = 0; let registerClicks = 0;
  marketing.click = () => { presses += 1; marketing.checked = false; };
  const mutateDrift = () => {
    if (mode === "label-swap") { marketing.dataset.lmConnectorControl = undefined; label.dataset.lmConnectorControl = undefined; registerLabel.dataset.lmConnectorControl = optToken; }
    if (mode === "checked-drift") { marketing.checked = false; }
    if (mode === "hidden-drift") { marketing.hidden = true; }
    if (mode === "disabled-drift") { marketing.disabled = true; }
    if (mode === "required-drift") { marketing.required = true; }
    if (mode === "type-drift") { marketing.type = "text"; }
  };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; });
      return { async elementHandles() { return [handle]; }, async count() { if (mode) mutateDrift(); return exact[1] === optToken && exact[2] === "organizationMarketingOptIn" && !mode ? 1 : 0; } };
    }
    const token = /^\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const labelToken = /^label\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const compoundToken = /^label\[data-lm-connector-control="([^"]+)"\]\[for="([^"]+)"\]$/.exec(selector)?.[1]; const labelFor = /^label\[for="([^"]+)"\]$/.exec(selector)?.[1];
    if (token) return { async count() { return elements.filter((element) => element.dataset?.lmConnectorControl === token).length; } };
    if (labelToken || compoundToken || labelFor) {
      return { async count() { return 1; }, async click() { clicks += 1; mutateDrift(); if (mode === "checked-drift") marketing.checked = true; else if (registerLabel.dataset.lmConnectorControl !== optToken) marketing.checked = false; if (registerLabel.dataset.lmConnectorControl === optToken) registerClicks += 1; } };
    }
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: optToken }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  const reset = () => { marketing.type = "checkbox"; marketing.checked = true; marketing.hidden = false; marketing.disabled = false; marketing.required = false; label.dataset.lmConnectorControl = undefined; registerLabel.dataset.lmConnectorControl = undefined; mode = ""; clicks = 0; presses = 0; registerClicks = 0; };
  for (const name of ["label-swap", "checked-drift", "hidden-drift", "disabled-drift", "required-drift", "type-drift"]) {
    reset(); mode = name;
    const result = await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: optToken } });
    assert.deepEqual(result, { status: "failed" }, name); assert.equal(clicks, 0, `${name}-label`); assert.equal(presses, 0, `${name}-press`); assert.equal(registerClicks, 0, `${name}-register`); if (name === "checked-drift") assert.equal(marketing.checked, false, `${name}-state`);
  }
});

test("Eventbrite marketing click-time identity rewire cannot redirect the exact input uncheck", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  let labelFor = "org-opt-in"; let registerFor = "register";
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? labelFor : null; } });
  const registerLabel = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Register copy", getAttribute(name) { return name === "for" ? registerFor : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", id: "register", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, registerLabel, primary];
  const optToken = "eventbrite_marketing_opt_out_organization_1901";
  let mode = ""; let presses = 0; let labelClicks = 0; let registerClicks = 0;
  marketing.click = () => { presses += 1; marketing.checked = false; };
  const mutateClickTimeRewire = () => {
    label.dataset.lmConnectorControl = undefined;
    labelFor = "register";
    marketing.id = "verified-id";
    registerLabel.dataset.lmConnectorControl = optToken;
    registerFor = "verified-id";
    primary.id = "verified-id";
    registerClicks += 1;
  };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; });
      return { async elementHandles() { return [handle]; }, async count() { return exact[1] === optToken && exact[2] === "organizationMarketingOptIn" ? 1 : 0; } };
    }
    const labelToken = /^label\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1]; const compoundToken = /^label\[data-lm-connector-control="([^"]+)"\]\[for="([^"]+)"\]$/.exec(selector)?.[1];
    if (labelToken || compoundToken) return { async count() { return 1; }, async click() { labelClicks += 1; if (mode === "click-time-rewire") mutateClickTimeRewire(); } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: optToken }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  mode = "click-time-rewire";
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: optToken } }), { status: "success" });
  assert.equal(presses, 1); assert.equal(labelClicks, 0); assert.equal(registerClicks, 0); assert.equal(marketing.checked, false);
  assert.equal(marketing.id, "org-opt-in"); assert.equal(labelFor, "org-opt-in"); assert.equal(registerLabel.dataset.lmConnectorControl, undefined); assert.equal(primary.id, "register");
});

test("Eventbrite marketing opt-out never reads a page-owned input click accessor", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? "org-opt-in" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, primary];
  let getterReads = 0; let presses = 0; let registerClicks = 0;
  Object.defineProperty(marketing, "click", { configurable: true, get() { getterReads += 1; registerClicks += 1; return () => { marketing.checked = false; }; } });
  const token = "eventbrite_marketing_opt_out_organization_1901";
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; });
      return { async elementHandles() { return [handle]; }, async count() { return exact[1] === token && exact[2] === "organizationMarketingOptIn" ? 1 : 0; } };
    }
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: token }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: token } }), { status: "success" });
  assert.equal(getterReads, 0); assert.equal(presses, 1); assert.equal(registerClicks, 0); assert.equal(marketing.checked, false);
});

test("Eventbrite marketing opt-out fails when the input reverts during the stability window", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? "org-opt-in" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, primary];
  const token = "eventbrite_marketing_opt_out_organization_1901"; let presses = 0; let reverts = 0;
  const revert = () => { reverts += 1; marketing.checked = true; };
  marketing.click = () => { presses += 1; marketing.checked = false; setTimeout(revert, 10); };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) {
      const handle = makeEventbriteMarketingHandle(marketing, (key) => { assert.equal(key, "Space"); presses += 1; marketing.checked = false; setTimeout(revert, 10); });
      return { async elementHandles() { return [handle]; }, async count() { return exact[1] === token && exact[2] === "organizationMarketingOptIn" ? 1 : 0; } };
    }
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: token }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const resultPromise = harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: token } });
    let settled = false; resultPromise.then(() => { settled = true; });
    for (let attempt = 0; attempt < 20 && !settled; attempt += 1) await Promise.resolve();
    for (let attempt = 0; attempt < 100 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
    assert.deepEqual(await resultPromise, { status: "failed" });
  } finally { mock.timers.reset(); }
  assert.equal(presses, 1); assert.equal(reverts, 1); assert.equal(marketing.checked, true);
});

test("Eventbrite marketing opt-out pins the original input before a count-time decoy swap", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const decoy = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "decoyMarketingOptIn", id: "decoy-opt-in", required: false, checked: true, hidden: true });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? "org-opt-in" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, decoy, label, primary];
  const token = "eventbrite_marketing_opt_out_organization_1901";
  let swapped = false; let decoyPresses = 0; let registerClicks = 0;
  const originalHandle = {
    async evaluate(callback, context) { return callback(marketing, context); },
    async press(key) { assert.equal(key, "Space"); marketing.checked = false; },
  };
  const swapToDecoy = () => {
    if (swapped) return;
    swapped = true;
    marketing.dataset.lmConnectorControl = undefined;
    marketing.name = "staleMarketingOptIn";
    decoy.dataset.lmConnectorControl = token;
  };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) return {
      async elementHandles() { return [originalHandle]; },
      async count() { swapToDecoy(); return exact[1] === token && exact[2] === "organizationMarketingOptIn" ? 1 : 0; },
      async press(key) { assert.equal(key, "Space"); decoyPresses += 1; registerClicks += 1; decoy.checked = false; marketing.checked = false; },
    };
    const controlToken = /^\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1];
    if (controlToken) return { async count() { return elements.filter((element) => element.dataset?.lmConnectorControl === controlToken).length; } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: token }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: token } }), { status: "failed" });
  assert.equal(decoyPresses, 0); assert.equal(registerClicks, 0); assert.equal(marketing.checked, true); assert.equal(decoy.checked, true);
});

test("Eventbrite marketing opt-out rejects a hidden original input after the uncheck", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "complete" });
  const marketing = makeEventbriteTicketElement({ tagName: "INPUT", type: "checkbox", name: "organizationMarketingOptIn", id: "org-opt-in", required: false, checked: true });
  const label = makeEventbriteTicketElement({ tagName: "LABEL", innerText: "Organization updates", getAttribute(name) { return name === "for" ? "org-opt-in" : null; } });
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  const elements = [field("buyer.N-first_name"), field("buyer.N-last_name"), field("buyer.N-email", "email"), field("buyer.confirmEmailAddress", "email"), marketing, label, primary];
  const token = "eventbrite_marketing_opt_out_organization_1901"; let presses = 0;
  const originalHandle = {
    async evaluate(callback, context) { return callback(marketing, context); },
    async press(key) { assert.equal(key, "Space"); presses += 1; marketing.checked = false; marketing.hidden = true; },
  };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1901"; }, parentFrame() { return {}; }, locator(selector) {
    const exact = /^input\[data-lm-connector-control="([^"]+)"\]\[name="([^"]+)"\]$/.exec(selector);
    if (exact) return {
      async elementHandles() { return [originalHandle]; },
      async count() { return exact[1] === token && exact[2] === "organizationMarketingOptIn" ? 1 : 0; },
      async press(key) { assert.equal(key, "Space"); presses += 1; marketing.checked = false; marketing.hidden = true; },
    };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("marketing readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); }, async proposeAction() { return { control: token }; },
    async operateControl(input) { return operatePageControl(input); }, async resolveValue() { throw new Error("resolver must not run"); },
  });
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const resultPromise = harness.performAction({ provider: "eventbrite", candidate, page, action: { purpose: "fill", method: "ax_uncheck", control: token } });
    let settled = false; resultPromise.then(() => { settled = true; });
    for (let attempt = 0; attempt < 20 && !settled; attempt += 1) await Promise.resolve();
    for (let attempt = 0; attempt < 100 && !settled; attempt += 1) { await Promise.resolve(); mock.timers.tick(25); }
    assert.deepEqual(await resultPromise, { status: "failed" });
  } finally { mock.timers.reset(); }
  assert.equal(presses, 1); assert.equal(marketing.checked, false); assert.equal(marketing.hidden, true);
});

test("Eventbrite adapter forwards fill ax_uncheck exactly once", async () => {
  const action = { purpose: "fill", method: "ax_uncheck", control: "eventbrite_marketing_opt_out_eventbrite_1901" }; let proposals = 0; let performed = 0;
  const adapter = createBrowserHarnessAdapter({
    async observePage() { return { state: "registration_page", controls: [{ control: action.control, kind: "checkbox", label: "Event updates", required: true, completed: false, submittable: false }] }; },
    async proposeAction() { proposals += 1; return action; },
    async performAction(input) { performed += 1; assert.deepEqual(input.action, action); return { status: "success" }; },
    async readExpectedState() { return { status: "absent" }; },
  });
  assert.deepEqual(await adapter.runFallback({ provider: "eventbrite", page: {}, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EVENTBRITEOPT1", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed", safe_reason: "agent_step_limit", repaired_actions: [action] });
  assert.equal(proposals, 1); assert.equal(performed, 1);
});

test("Eventbrite attendee fill binds the selected field to the same checkout child frame", async () => {
  const candidate = { provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1901" };
  const field = (name, type = "text") => makeEventbriteTicketElement({ tagName: "INPUT", type, name, required: true, value: "" });
  const first = field("buyer.N-first_name"); const last = field("buyer.N-last_name"); const email = field("buyer.N-email", "email"); const confirm = field("buyer.confirmEmailAddress", "email");
  const marketing = field("marketing_opt_in", "checkbox"); marketing.required = false;
  const primary = makeEventbriteTicketElement({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", innerText: "Register" });
  let elements = [first, last, email, confirm, marketing, primary]; let fillCalls = 0; let resolved = 0; let operateCalls = 0; let locatorCount = 1; let applyFill = true;
  let pageHref = candidate.canonical_url; let frameHref = "https://www.eventbrite.com/checkout-external?eid=1901"; let pageFrames;
  const frame = { url() { return frameHref; }, parentFrame() { return {}; }, locator(selector) {
    const token = /^\[data-lm-connector-control="([^"]+)"\]$/.exec(selector)?.[1];
    if (token) return { async count() { return locatorCount; }, async fill(value) { fillCalls += 1; if (applyFill) elements.find((element) => element.dataset.lmConnectorControl === token).value = value; } };
    return { async evaluateAll(callback, context) { return callback(elements, context); } };
  } };
  const replacementFrame = { url() { return frameHref; }, parentFrame() { return {}; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  pageFrames = [frame];
  const page = { url() { return pageHref; }, frames() { return pageFrames; }, locator() { return { async evaluateAll(callback, context) { return callback([], context); } }; } };
  const action = { purpose: "fill", method: "ax_fill", control: "eventbrite_attendee_first_name_1901" };
  let mutateAfterResolve = null; let resolvedValue = "GivenFixture";
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("readback must not run"); } },
    async inspectControls(input) { return inspectPageControls(input); },
    async proposeAction() { return { control: action.control }; },
    async operateControl(input) { operateCalls += 1; assert.equal(input.page, page); assert.equal(input.frame, frame); return operatePageControl(input); },
    async resolveValue(input) { resolved += 1; assert.equal(input.control.control, action.control); mutateAfterResolve?.(); return resolvedValue; },
  });
  assert.deepEqual(await harness.performAction({ provider: "eventbrite", candidate, page, action }), { status: "success" });
  assert.equal(resolved, 1); assert.equal(operateCalls, 1); assert.equal(fillCalls, 1); assert.equal(first.value, "GivenFixture"); assert.equal(first.dataset.lmConnectorControl, action.control);

  const reset = () => { first.value = ""; elements = [first, last, email, confirm, marketing, primary]; pageHref = candidate.canonical_url; frameHref = "https://www.eventbrite.com/checkout-external?eid=1901"; pageFrames = [frame]; fillCalls = 0; operateCalls = 0; resolved = 0; locatorCount = 1; applyFill = true; mutateAfterResolve = null; resolvedValue = "GivenFixture"; };
  for (const [name, badAction, mutate, expectedResolve, expectedOperate] of [
    ["wrong-action", { purpose: "submit", method: "ax_click", control: action.control }, null, 0, 0],
    ["missing-value", action, () => { resolvedValue = ""; }, 1, 0],
    ["page-drift", action, () => { pageHref = "https://www.eventbrite.com/e/tokyo-free-event-tickets-1902"; }, 1, 0],
    ["frame-drift", action, () => { pageFrames = [replacementFrame]; }, 1, 0],
    ["eid-drift", action, () => { frameHref = "https://www.eventbrite.com/checkout-external?eid=1902"; }, 1, 0],
    ["dom-drift", action, () => { elements = [...elements, field("buyer.N-first_name")]; }, 1, 0],
    ["locator-zero", action, () => { locatorCount = 0; }, 1, 1],
    ["locator-duplicate", action, () => { locatorCount = 2; }, 1, 1],
    ["postcondition", action, () => { applyFill = false; }, 1, 1],
  ]) {
    reset(); mutateAfterResolve = mutate;
    const result = await harness.performAction({ provider: "eventbrite", candidate, page, action: badAction });
    assert.deepEqual(result, { status: "failed" }, name);
    assert.equal(resolved, expectedResolve, `${name}-resolve`); assert.equal(operateCalls, expectedOperate, `${name}-operate`);
    assert.equal(fillCalls, expectedOperate === 1 && name === "postcondition" ? 1 : 0, `${name}-fill`);
  }
});

function makeTechPlayElement(overrides = {}) {
  const element = {
    tagName: "INPUT", type: "text", name: "", id: "", value: "", checked: false, disabled: false,
    hidden: false, isConnected: true, style: {}, rect: { width: 120, height: 32 }, parentElement: null,
    labels: [], innerText: "", textContent: "", dataset: {}, ownerDocument: { defaultView: { getComputedStyle: (node) => node.style || {} } },
    getAttribute(name) { return name === "role" ? this.role || null : name === "aria-checked" ? (this.ariaChecked == null ? null : String(this.ariaChecked)) : name === "aria-hidden" ? (this.ariaHidden ? "true" : null) : this[name] || null; },
    hasAttribute(name) { return name === "hidden" && this.hidden === true; },
    getBoundingClientRect() { return this.rect; },
    closest() { return null; },
  };
  return Object.assign(element, overrides);
}

function makeTechPlayInputFixture() {
  const candidate = { provider: "techplay", event_ref: "techplay-event://event/999190", canonical_url: "https://techplay.jp/event/999190", ticket_id: "98036" };
  const questions = ["氏名", "メールアドレス", "年齢", "所属企業（学校）名"];
  const answers = questions.map((label, index) => makeTechPlayElement({ type: index === 2 ? "number" : "text", name: `enqueteAnswers[${index + 1}]`, labels: [{ innerText: `${label} *` }], value: "private@example.com" }));
  answers.forEach((answer) => { answer.value = ""; });
  const radioGroup = (question, id, labels) => labels.map((value, index) => makeTechPlayElement({ type: "radio", name: `enqueteAnswers[${id}]`, id: `enqueteAnswers_${id}_${index}`, value: `${id}-${index}`, labels: [{ innerText: value }], parentElement: makeTechPlayElement({ tagName: "DIV", innerText: `${question}* ${labels.join(" ")}`, parentElement: null }) }));
  const radioAnswers = [...radioGroup("キャリア状況", 5, ["社会人", "学生", "その他"]), ...radioGroup("職種", 6, Array.from({ length: 33 }, (_, index) => `職種${index + 1}`))];
  const ticket = makeTechPlayElement({ type: "radio", name: "ticket", value: candidate.ticket_id, checked: true, labels: [{ innerText: "無料チケット" }] });
  const optoutIds = ["area_1", "tag_2", "organizer_3", "area_4", "tag_5", "icon_published", "use_as_preset"];
  const optouts = optoutIds.map((id) => makeTechPlayElement({ tagName: "BUTTON", type: "button", id, role: "checkbox", ariaChecked: true, innerText: "通知を受け取る" }));
  const review = makeTechPlayElement({ tagName: "BUTTON", type: "submit", innerText: "同意して内容を確認する" });
  const elements = [ticket, ...answers, ...radioAnswers, ...optouts, review];
  const page = { url() { return "https://techplay.jp/event/join/999190"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  return { candidate, answers, radioAnswers, ticket, optouts, elements, page };
}

function inspectTechPlayFixture(fixture, candidate = fixture.candidate, elements = fixture.elements, pageOverride = null) {
  const page = pageOverride || { url: fixture.page.url, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  return inspectPageControls({ page, provider: "techplay", candidate, event_id: candidate.event_ref?.split("/").pop(), candidate_url: candidate.canonical_url, ticket_id: candidate.ticket_id });
}

test("TECH PLAY input inspector binds candidate ticket, required answers, and seven opt-outs without private values", async () => {
  const fixture = makeTechPlayInputFixture();
  const inspect = (candidate = fixture.candidate, elements = fixture.elements) => inspectTechPlayFixture(fixture, candidate, elements);
  let controls = await inspect();
  assert.equal(controls.length, 1 + fixture.answers.length + fixture.radioAnswers.length + fixture.optouts.length + 1);
  assert.equal(controls.filter((control) => control.required && control.completed === false).length, fixture.answers.length + fixture.radioAnswers.length + fixture.optouts.length);
  assert.equal(controls.find((control) => control.label === "同意して内容を確認する").submittable, false);
  assert.doesNotMatch(JSON.stringify(controls), /private@example\.com|98036|enqueteAnswers\[/);

  fixture.answers.slice(0, 3).forEach((answer) => { answer.value = "filled"; });
  fixture.optouts[0].ariaChecked = false;
  controls = await inspect();
  assert.equal(controls.find((control) => control.label === "同意して内容を確認する").submittable, false);
  fixture.answers.forEach((answer) => { answer.value = "filled"; });
  fixture.radioAnswers[0].checked = true;
  fixture.radioAnswers[3].checked = true;
  fixture.optouts.forEach((optout) => { optout.ariaChecked = false; });
  controls = await inspect();
  assert.equal(controls.find((control) => control.label === "同意して内容を確認する").submittable, true);
  for (const [name, candidate, elements] of [
    ["wrong-ticket", { ...fixture.candidate, ticket_id: "98037" }, fixture.elements],
    ["wrong-event", { ...fixture.candidate, event_ref: "techplay-event://event/999191" }, fixture.elements],
    ["duplicate-ticket", fixture.candidate, [...fixture.elements, makeTechPlayElement({ type: "radio", name: "ticket", value: "98036", checked: false, labels: [{ innerText: "無料チケット" }] })]],
    ["unknown-checked-optout", fixture.candidate, [...fixture.elements, makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "unknown_optout", role: "checkbox", ariaChecked: true, innerText: "Unknown" })]],
    ["ambiguous-career", fixture.candidate, fixture.elements.map((element) => element.id === "enqueteAnswers_5_1" ? { ...element, checked: true } : element)],
    ["hidden-required-answer", fixture.candidate, fixture.elements.map((element, index) => index === 1 ? { ...element, hidden: true } : element)],
  ]) {
    assert.deepEqual(await inspect(candidate, elements), [], name);
  }
});

test("TECH PLAY regression requires a checked candidate ticket radio", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.ticket.checked = false;
  assert.deepEqual(await inspectTechPlayFixture(fixture), []);
});

test("TECH PLAY regression requires exact career and職種 radio option cardinality and labels", async () => {
  for (const [name, mutate] of [
    ["missing-career-option", (fixture) => { fixture.elements = fixture.elements.filter((element) => element.id !== "enqueteAnswers_5_2"); }],
    ["extra-career-option", (fixture) => { const source = fixture.radioAnswers[0]; fixture.elements = [...fixture.elements, { ...source, id: "enqueteAnswers_5_extra", value: "5-extra", labels: [{ innerText: "インターン" }] }]; }],
    ["missing-job-option", (fixture) => { fixture.elements = fixture.elements.filter((element) => element.id !== "enqueteAnswers_6_32"); }],
    ["duplicate-career-label", (fixture) => { const source = fixture.radioAnswers[0]; fixture.elements = fixture.elements.map((element) => element.id === "enqueteAnswers_5_2" ? { ...element, labels: [{ innerText: "社会人" }] } : element); }],
  ]) {
    const fixture = makeTechPlayInputFixture(); mutate(fixture);
    assert.deepEqual(await inspectTechPlayFixture(fixture, fixture.candidate, fixture.elements), [], name);
  }
});

test("TECH PLAY regression accepts only visible button role-checkbox controls and ignores hidden companions", async () => {
  const hiddenCompanion = makeTechPlayElement({ type: "checkbox", hidden: true });
  const accepted = makeTechPlayInputFixture(); accepted.elements = [...accepted.elements, hiddenCompanion];
  assert.ok((await inspectTechPlayFixture(accepted, accepted.candidate, accepted.elements)).length > 0);
  for (const [name, extra] of [
    ["visible-div-role-checkbox", makeTechPlayElement({ tagName: "DIV", role: "checkbox", id: "area_99", ariaChecked: true, innerText: "Area" })],
    ["visible-native-safe-family-unchecked", makeTechPlayElement({ type: "checkbox", id: "area_99", checked: false })],
  ]) {
    const fixture = makeTechPlayInputFixture(); fixture.elements = [...fixture.elements, extra];
    assert.deepEqual(await inspectTechPlayFixture(fixture, fixture.candidate, fixture.elements), [], name);
  }
});

test("TECH PLAY regression requires input radio answer options", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.elements = fixture.elements.map((element) => element.id === "enqueteAnswers_5_1" ? { ...element, tagName: "BUTTON" } : element);
  assert.deepEqual(await inspectTechPlayFixture(fixture, fixture.candidate, fixture.elements), []);
});

test("TECH PLAY regression requires input radio ticket", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.ticket.tagName = "BUTTON";
  assert.deepEqual(await inspectTechPlayFixture(fixture), []);
});

test("TECH PLAY regression requires one exact visible submit CTA type", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.elements = fixture.elements.map((element) => element.innerText === "同意して内容を確認する" ? { ...element, type: "button" } : element);
  assert.deepEqual(await inspectTechPlayFixture(fixture, fixture.candidate, fixture.elements), []);
});

test("TECH PLAY regression rejects duplicate exact CTA with wrong button type", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.elements = [...fixture.elements, { ...fixture.elements.find((element) => element.innerText === "同意して内容を確認する"), id: "review-decoy", type: "button" }];
  assert.deepEqual(await inspectTechPlayFixture(fixture, fixture.candidate, fixture.elements), []);
});

test("TECH PLAY regression enforces scalar question tag and type contracts", async () => {
  for (const [name, index, overrides] of [
    ["name-number", 0, { type: "number" }], ["email-email", 1, { type: "email" }],
    ["age-button", 2, { tagName: "BUTTON", type: "button" }], ["company-textarea", 3, { tagName: "TEXTAREA", type: "" }],
  ]) {
    const fixture = makeTechPlayInputFixture(); fixture.answers[index] = Object.assign(fixture.answers[index], overrides);
    assert.deepEqual(await inspectTechPlayFixture(fixture), [], name);
  }
});

test("TECH PLAY regression rejects global DOM id collisions across answer and opt-out families", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.answers[0].id = "area_1";
  assert.deepEqual(await inspectTechPlayFixture(fixture), []);
});

test("TECH PLAY regression keeps actual scalar values out of the public projection", async () => {
  const fixture = makeTechPlayInputFixture(); fixture.answers.forEach((answer, index) => { answer.value = index === 1 ? "private@example.com" : "1987"; });
  const controls = await inspectTechPlayFixture(fixture);
  assert.ok(controls.length > 0); assert.doesNotMatch(JSON.stringify(controls), /private@example\.com|1987/);
});

test("TECH PLAY regression rejects duplicate review, answer name, answer id, hidden ancestor, opacity, zero-size, page drift, and oversized DOM", async () => {
  const base = makeTechPlayInputFixture();
  const duplicateReview = { ...base.elements.find((element) => element.innerText === "同意して内容を確認する"), id: "review-2" };
  const duplicateName = { ...base.answers[0], id: "answer-duplicate" };
  const duplicateId = { ...base.radioAnswers[0], name: "enqueteAnswers[7]", id: base.radioAnswers[1].id, labels: [{ innerText: "別職種" }], parentElement: base.radioAnswers[0].parentElement };
  const hiddenAncestor = makeTechPlayElement({ style: {}, parentElement: makeTechPlayElement({ style: { display: "none" } }) });
  const cases = [
    ["duplicate-review", [...base.elements, duplicateReview]], ["duplicate-answer-name", [...base.elements, duplicateName]], ["duplicate-answer-id", [...base.elements, duplicateId]],
    ["ancestor-hidden", base.elements.map((element, index) => index === 1 ? { ...element, parentElement: hiddenAncestor.parentElement } : element)],
    ["opacity-zero", base.elements.map((element, index) => index === 1 ? { ...element, style: { opacity: "0" } } : element)],
    ["zero-size", base.elements.map((element, index) => index === 1 ? { ...element, rect: { width: 0, height: 0 } } : element)],
    ["oversized", [...base.elements, ...Array.from({ length: 102 }, () => makeTechPlayElement({ hidden: true }))]],
  ];
  for (const [name, elements] of cases) {
    const fixture = makeTechPlayInputFixture(); assert.deepEqual(await inspectTechPlayFixture(fixture, fixture.candidate, elements), [], name);
  }
  const drift = makeTechPlayInputFixture(); let current = "https://techplay.jp/event/join/999190";
  const page = { url() { return current; }, locator() { return { async evaluateAll(callback, context) { current = "https://techplay.jp/event/join/999191"; return callback(drift.elements, context); } }; } };
  assert.deepEqual(await inspectTechPlayFixture(drift, drift.candidate, drift.elements, page), [], "page-drift");
});

function makeTechPlayConfirmFixture() {
  const candidate = { provider: "techplay", event_ref: "techplay-event://event/999190", canonical_url: "https://techplay.jp/event/999190", ticket_id: "98036" };
  const final = makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "confirm-final", innerText: "申し込みを確定する" });
  const elements = [final];
  const page = { url() { return "https://techplay.jp/event/join/999190/confirm"; }, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  return { candidate, final, elements, page };
}

function inspectTechPlayConfirmFixture(fixture, candidate = fixture.candidate, elements = fixture.elements, pageOverride = null) {
  const page = pageOverride || { url: fixture.page.url, locator() { return { async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  return inspectPageControls({ page, provider: "techplay", candidate, event_id: candidate.event_ref?.split("/").pop(), candidate_url: candidate.canonical_url, ticket_id: candidate.ticket_id });
}

test("TECH PLAY confirm inspector exposes only the exact same-event final control", async () => {
  const fixture = makeTechPlayConfirmFixture();
  assert.deepEqual(await inspectTechPlayConfirmFixture(fixture), [{ control: "techplay_final_999190", kind: "button", label: "申し込みを確定する", required: false, completed: false, submittable: true }]);
  assert.doesNotMatch(JSON.stringify(await inspectTechPlayConfirmFixture(fixture, fixture.candidate, [...fixture.elements, makeTechPlayElement({ tagName: "INPUT", type: "text", name: "email", id: "private", value: "private@example.com", hidden: true })])), /private@example\.com|98036|999190\/confirm/);
});

test("TECH PLAY confirm regression rejects wrong URL/event and every final-control drift", async () => {
  for (const [name, mutate] of [
    ["input-page", (fixture) => { fixture.page = { url() { return "https://techplay.jp/event/join/999190"; } }; }],
    ["duplicate-final", (fixture) => { fixture.elements = [...fixture.elements, { ...fixture.final, id: "confirm-decoy" }]; }],
    ["wrong-type", (fixture) => { fixture.final.type = "submit"; }],
    ["hidden-final", (fixture) => { fixture.final.hidden = true; }],
    ["disabled-final", (fixture) => { fixture.final.disabled = true; }],
    ["ancestor-hidden-final", (fixture) => { fixture.final.parentElement = makeTechPlayElement({ style: { display: "none" } }); }],
    ["opacity-zero-final", (fixture) => { fixture.final.style = { opacity: "0" }; }],
    ["zero-size-final", (fixture) => { fixture.final.rect = { width: 0, height: 0 }; }],
  ]) {
    const fixture = makeTechPlayConfirmFixture(); mutate(fixture);
    assert.deepEqual(await inspectTechPlayConfirmFixture(fixture), [], name);
  }
});

test("TECH PLAY confirm regression rejects a different event binding", async () => {
  const fixture = makeTechPlayConfirmFixture(); fixture.candidate = { ...fixture.candidate, event_ref: "techplay-event://event/999191", canonical_url: "https://techplay.jp/event/999191" };
  assert.deepEqual(await inspectTechPlayConfirmFixture(fixture), []);
});

test("TECH PLAY confirm regression rejects a visible input inside the registration main region", async () => {
  const fixture = makeTechPlayConfirmFixture(); const region = makeTechPlayElement({ tagName: "MAIN" }); fixture.final.parentElement = region;
  fixture.elements = [fixture.final, makeTechPlayElement({ type: "text", name: "summary", id: "summary", parentElement: region })];
  assert.deepEqual(await inspectTechPlayConfirmFixture(fixture, fixture.candidate, fixture.elements), []);
});

test("TECH PLAY confirm regression rejects residual registration controls, duplicate IDs, page drift, and oversized DOM", async () => {
  for (const [name, extra] of [
    ["answer", makeTechPlayElement({ type: "text", name: "enqueteAnswers[1]", id: "answer", hidden: true })],
    ["ticket", makeTechPlayElement({ type: "radio", name: "ticket", id: "ticket", hidden: true })],
    ["role-checkbox", makeTechPlayElement({ tagName: "BUTTON", type: "button", role: "checkbox", id: "area_1", innerText: "通知" })],
    ["native-checkbox", makeTechPlayElement({ type: "checkbox", id: "native", checked: false })],
    ["duplicate-id", makeTechPlayElement({ type: "text", id: "confirm-final", hidden: true })],
    ["extra-action", makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "back", innerText: "戻る" })],
  ]) {
    const fixture = makeTechPlayConfirmFixture(); fixture.elements = [...fixture.elements, extra];
    assert.deepEqual(await inspectTechPlayConfirmFixture(fixture, fixture.candidate, fixture.elements), [], name);
  }
  const oversized = makeTechPlayConfirmFixture(); oversized.elements = [oversized.final, ...Array.from({ length: 150 }, () => makeTechPlayElement({ hidden: true }))];
  assert.deepEqual(await inspectTechPlayConfirmFixture(oversized, oversized.candidate, oversized.elements), [], "oversized");
  const drift = makeTechPlayConfirmFixture(); let current = "https://techplay.jp/event/join/999190/confirm";
  const page = { url() { return current; }, locator() { return { async evaluateAll(callback, context) { current = "https://techplay.jp/event/join/999191/confirm"; return callback(drift.elements, context); } }; } };
  assert.deepEqual(await inspectTechPlayConfirmFixture(drift, drift.candidate, drift.elements, page), [], "page-drift");
});

test("TECH PLAY confirm inspector removes the ephemeral final token when dataset binding throws", async () => {
  const fixture = makeTechPlayConfirmFixture(); let setAttempts = 0;
  fixture.final.dataset = new Proxy({}, { set(target, key, value) { setAttempts += 1; Reflect.set(target, key, value); throw new Error("dataset setter"); } });
  assert.deepEqual(await inspectTechPlayConfirmFixture(fixture), []);
  assert.equal(setAttempts, 1);
  assert.equal(fixture.final.dataset.lmConnectorControl, undefined);
});

function makeTechPlayPrivateResolverFixture(options = {}) {
  const reads = { identity: 0, form: 0 };
  const identity = { name_kanji: "NameFixture", email: "email.fixture@example.test" };
  const form = { form_answers: {
    "生年月日": "2002-08-12", "Date of Birth": "2002-08-12",
    "所属企業（学校）名": "CompanyFixture", "キャリア状況": "社会人", "職種": "EngineerFixture",
  } };
  return {
    reads,
    form,
    resolver: createPrivateValueResolver({
      readPeatixProfile: async () => { reads.identity += 1; return identity; },
      readFormProfile: async () => { reads.form += 1; return form; },
      now: options.now || (() => new Date("2026-08-12T00:00:00.000Z")),
    }),
  };
}

function techPlayAnswerControl(overrides = {}) {
  return { kind: "input", label: "氏名", required: true, completed: false, submittable: false, control: "techplay_answer_1", ...overrides };
}

test("TECH PLAY private resolver maps scalar identity, age, and company answers", async () => {
  const fixture = makeTechPlayPrivateResolverFixture();
  assert.equal(await fixture.resolver({ provider: "techplay", control: techPlayAnswerControl() }), "NameFixture");
  assert.equal(await fixture.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "メールアドレス", control: "techplay_answer_2" }) }), "email.fixture@example.test");
  assert.equal(await fixture.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "年齢", control: "techplay_answer_3" }) }), "24");
  assert.equal(await fixture.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "所属企業（学校）名", control: "techplay_answer_4" }) }), "CompanyFixture");
  assert.deepEqual(fixture.reads, { identity: 2, form: 2 });
});

test("TECH PLAY private resolver maps only the exact radio question and option", async () => {
  const fixture = makeTechPlayPrivateResolverFixture();
  const control = (question, label, id = "5") => ({ kind: "radio", question, label, required: true, completed: false, submittable: false, control: `techplay_answer_${id}_1` });
  assert.equal(await fixture.resolver({ provider: "techplay", control: control("キャリア状況", "社会人") }), true);
  assert.equal(await fixture.resolver({ provider: "techplay", control: control("職種", "EngineerFixture", "6") }), true);
  assert.equal(await fixture.resolver({ provider: "techplay", control: control("キャリア状況", "学生") }), null);
  assert.equal(await fixture.resolver({ provider: "techplay", control: control("職種", "社会人", "7") }), null);
});

test("TECH PLAY private resolver requires exact keys, questions, and option labels", async () => {
  const paddedKey = makeTechPlayPrivateResolverFixture(); delete paddedKey.form.form_answers["職種"]; paddedKey.form.form_answers[" 職種 "] = "EngineerFixture";
  const radio = (question, label) => ({ kind: "radio", question, label, required: true, completed: false, submittable: false, control: "techplay_answer_6_1" });
  assert.equal(await paddedKey.resolver({ provider: "techplay", control: radio("職種", "EngineerFixture") }), null);
  const caseLabel = makeTechPlayPrivateResolverFixture(); assert.equal(await caseLabel.resolver({ provider: "techplay", control: radio("職種", "engineerfixture") }), null);
  const paddedQuestion = makeTechPlayPrivateResolverFixture(); assert.equal(await paddedQuestion.resolver({ provider: "techplay", control: radio(" 職種 ", "EngineerFixture") }), null);
  const paddedScalar = makeTechPlayPrivateResolverFixture(); assert.equal(await paddedScalar.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: " 所属企業（学校）名 " }) }), null);
});

test("TECH PLAY private resolver rejects malformed or non-answer controls without private reads", async () => {
  for (const control of [
    null, {}, techPlayAnswerControl({ kind: "button" }), techPlayAnswerControl({ required: false }), techPlayAnswerControl({ completed: undefined }),
    techPlayAnswerControl({ completed: true }), techPlayAnswerControl({ submittable: true }),
    techPlayAnswerControl({ control: "techplay_ticket_1" }), techPlayAnswerControl({ control: "techplay_optout_1" }),
    techPlayAnswerControl({ control: "techplay_final_999190", label: "申し込みを確定する" }),
    techPlayAnswerControl({ label: "未登録の質問" }),
  ]) {
    const fixture = makeTechPlayPrivateResolverFixture();
    assert.equal(await fixture.resolver({ provider: "techplay", control }), null);
    assert.deepEqual(fixture.reads, { identity: 0, form: 0 });
  }
});

test("TECH PLAY private resolver fails closed for throwing private profile getters", async () => {
  const identity = {}; Object.defineProperty(identity, "name_kanji", { get() { throw new Error("PRIVATE_IDENTITY_LEAK"); } });
  const form = {}; Object.defineProperty(form, "form_answers", { get() { throw new Error("PRIVATE_FORM_LEAK"); } });
  const resolver = createPrivateValueResolver({ readPeatixProfile: async () => identity, readFormProfile: async () => form, now: () => new Date("2026-08-12T00:00:00.000Z") });
  const scalar = (label) => techPlayAnswerControl({ label });
  const radio = { kind: "radio", question: "職種", label: "EngineerFixture", required: true, completed: false, submittable: false, control: "techplay_answer_6_1" };
  for (const control of [scalar("氏名"), scalar("年齢"), scalar("所属企業（学校）名"), radio]) assert.equal(await resolver({ provider: "techplay", control }), null);
});

test("TECH PLAY private resolver enforces exact DOBs, Tokyo age boundaries, and safe values", async () => {
  const birthday = makeTechPlayPrivateResolverFixture();
  assert.equal(await birthday.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "年齢" }) }), "24");
  for (const [now, expected] of [[() => new Date("2026-08-11T14:00:00.000Z"), "23"], [() => new Date("2026-08-11T15:00:00.000Z"), "24"], [() => new Date("2026-08-12T00:00:00.000Z"), "24"]]) {
    const fixture = makeTechPlayPrivateResolverFixture({ now });
    assert.equal(await fixture.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "年齢" }) }), expected);
  }
  for (const mutate of [
    (answers) => { answers["Date of Birth"] = "2002-08-13"; },
    (answers) => { answers["生年月日"] = "2002-02-30"; answers["Date of Birth"] = "2002-02-30"; },
    (answers) => { delete answers["生年月日"]; delete answers["Date of Birth"]; },
  ]) {
    const fixture = makeTechPlayPrivateResolverFixture(); mutate(fixture.form.form_answers);
    assert.equal(await fixture.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "年齢" }) }), null);
  }
  const throwing = makeTechPlayPrivateResolverFixture({ now: () => { throw new Error("clock"); } });
  assert.equal(await throwing.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "年齢" }) }), null);
  const invalidClock = makeTechPlayPrivateResolverFixture({ now: () => new Date("invalid") });
  assert.equal(await invalidClock.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "年齢" }) }), null);
  const unsafe = makeTechPlayPrivateResolverFixture(); unsafe.form.form_answers["所属企業（学校）名"] = " bad\nvalue ";
  assert.equal(await unsafe.resolver({ provider: "techplay", control: techPlayAnswerControl({ label: "所属企業（学校）名" }) }), null);
});

test("TECH PLAY private values stay out of the bounded runner prompt", async () => {
  let request;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", async runAgentRunner(input) {
    request = input; return { summary: { status: "success", selected_provider: "codex", selected_model: "gpt-5.6-terra" }, value: { control: "techplay_answer_1" } };
  } });
  await proposer({ provider: "luma", target_id: "TECHPLAY1", expected_state: "registered_or_pending", step: 1, observation: {
    state: "registration_page", controls: [{ ...techPlayAnswerControl(), value: "NameFixture", currentValue: "email.fixture@example.test" }],
  } });
  assert.doesNotMatch(JSON.stringify(request), /NameFixture|email\.fixture@example\.test|2002-08-12|CompanyFixture/);
});

function makeTechPlayOperationFixture(options = {}) {
  const fixture = makeTechPlayInputFixture();
  fixture.answers.forEach((answer) => { answer.value = ""; answer.checked = false; });
  fixture.radioAnswers.forEach((answer) => { answer.checked = false; });
  fixture.optouts.forEach((optout) => { optout.ariaChecked = true; });
  let href = fixture.candidate.canonical_url.replace("/event/", "/event/join/");
  const final = makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "confirm-final", innerText: "申し込みを確定する" });
  const confirmElements = [final];
  const tokenOf = (selector) => /^\[data-lm-connector-control="([^\"]+)"\]$/.exec(selector)?.[1] || "";
  const page = {
    url() { return href; },
    waitForURL(predicate) {
      options.waitCalls = (options.waitCalls || 0) + 1;
      options.events?.push("arm");
      if (options.waitReject) return Promise.reject(new Error("synthetic navigation rejection"));
      if (options.waitTimeout) return Promise.resolve(false);
      if (!options.navigateOnReview) return Promise.resolve(false);
      return new Promise((resolve) => { options.resolveWait = () => resolve(Boolean(predicate(href))); });
    },
    locator(selector) {
      const token = tokenOf(selector);
      const currentElements = () => href.endsWith("/confirm") ? confirmElements : fixture.elements;
      const matches = token ? () => currentElements().filter((element) => element.dataset?.lmConnectorControl === token) : () => [];
      const clickTarget = async (pinnedTarget = null) => {
        const target = pinnedTarget || matches()[0];
        if (!target || options.apply === false) return;
        if (target.role === "checkbox") target.ariaChecked = false;
        if (target.innerText === "同意して内容を確認する") {
          target.clicked = true; options.reviewClicks = (options.reviewClicks || 0) + 1; options.events?.push("click");
          if (options.navigateOnReview) { href = options.reviewHref || "https://techplay.jp/event/join/999190/confirm"; options.resolveWait?.(); if (options.throwOnReview) throw new Error("synthetic click rejection"); }
        } else if (target.innerText === "申し込みを確定する") {
          target.clicked = true; options.finalClicks = (options.finalClicks || 0) + 1; options.finalRegistered = true;
          if (options.throwOnFinal) throw new Error("synthetic final click rejection");
        }
      };
      return {
        async evaluateAll(callback, context) {
          options.inspectCalls = (options.inspectCalls || 0) + 1;
          if (href.endsWith("/confirm")) options.confirmInspectCalls = (options.confirmInspectCalls || 0) + 1;
          if (href.endsWith("/confirm") && options.throwOnConfirmInspect) throw new Error("synthetic confirm inspector failure");
          if (href.endsWith("/confirm") && options.driftConfirmHref) { href = options.driftConfirmHref; options.driftConfirmHref = null; }
          if (href.endsWith("/confirm") && options.driftConfirmTicket) { fixture.candidate.ticket_id = "98037"; options.driftConfirmTicket = false; }
          if (Array.isArray(options.inspectThrowCalls) && options.inspectThrowCalls.includes(options.inspectCalls)) throw new Error("synthetic inspector failure");
          const transient = Array.isArray(options.transientInspectCalls) && options.transientInspectCalls.includes(options.inspectCalls);
          const elements = currentElements();
          if (href.endsWith("/confirm") && options.confirmEmptyAttempts > 0) { options.confirmEmptyAttempts -= 1; return []; }
          return callback(transient ? [...elements, ...(options.transientNodes || [])] : elements, context);
        },
        async count() {
          if (options.driftOnReview && token === "techplay_review_999190") { options.driftOnReview = false; href = options.driftHref || "https://techplay.jp/event/join/999191"; }
          const override = token === "techplay_review_999190" && options.reviewLocatorCount != null ? options.reviewLocatorCount : options.locatorCount;
          return override == null ? matches().length : override;
        },
        async elementHandles() {
          const target = matches()[0];
          return target ? [{ async evaluate(callback, context) { return callback(target, context); }, async click() { return clickTarget(target); } }] : [];
        },
        async fill(value) { const target = matches()[0]; if (target && options.apply !== false) target.value = value; },
        async check() { const target = matches()[0]; if (target && options.apply !== false) target.checked = true; },
        async click() { return clickTarget(); },
        async press(key) { const target = matches()[0]; if (target && key === "Space" && options.apply !== false && target.role === "checkbox") target.ariaChecked = false; },
      };
    },
  };
  return { ...fixture, final, confirmElements, page, options, setHref(value) { href = value; } };
}

function makeTechPlayOperationHarness(fixture, options = {}) {
  const resolverFixture = makeTechPlayPrivateResolverFixture();
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("TECH PLAY must not use provider readback"); } },
    ...(options.techplayWorkflow ? { techplayWorkflow: options.techplayWorkflow } : {}),
    inspectControls: options.inspectControls || (async (input) => inspectPageControls(input)),
    proposeAction: async () => { options.proposeCalls = (options.proposeCalls || 0) + 1; throw new Error("TECH PLAY proposer must not run"); },
    operateControl: options.operateControl || (async (input) => { options.operateCalls = (options.operateCalls || 0) + 1; return operatePageControl(input); }),
    resolveValue: options.resolveValue || resolverFixture.resolver,
    sleep: options.sleep || (async () => {}),
  });
  return { harness, resolverFixture };
}

test("TECH PLAY input inspector binds ephemeral tokens only after full validation", async () => {
  const fixture = makeTechPlayOperationFixture();
  const controls = await inspectTechPlayFixture(fixture);
  assert.equal(controls.length, 49);
  for (const control of controls.filter(({ control }) => !control.startsWith("techplay_ticket_"))) assert.equal(fixture.elements.filter((element) => element.dataset?.lmConnectorControl === control.control).length, 1, control.control);
  assert.equal(fixture.ticket.dataset.lmConnectorControl, undefined);
  const invalidFixture = makeTechPlayOperationFixture(); invalidFixture.answers[0].hidden = true;
  assert.deepEqual(await inspectTechPlayFixture(invalidFixture), []);
  assert.equal(invalidFixture.elements.some((element) => element.dataset?.lmConnectorControl), false);
  const setterFailure = makeTechPlayOperationFixture(); let inputSetAttempts = 0;
  setterFailure.answers[1].dataset = new Proxy({}, { set(target, key, value) { inputSetAttempts += 1; Reflect.set(target, key, value); throw new Error("dataset setter"); } });
  assert.deepEqual(await inspectTechPlayFixture(setterFailure), []);
  assert.equal(inputSetAttempts, 1);
  assert.equal(setterFailure.elements.some((element) => element.dataset?.lmConnectorControl), false);
});

test("TECH PLAY parent operation fills scalar, checks the unique approved radio, and unchecks opt-out", async () => {
  const scalar = makeTechPlayOperationFixture(); const scalarHarness = makeTechPlayOperationHarness(scalar, {});
  assert.deepEqual(await scalarHarness.harness.performAction({ provider: "techplay", candidate: scalar.candidate, page: scalar.page, action: { purpose: "fill", method: "ax_fill", control: "techplay_answer_1" } }), { status: "success" });
  assert.equal(scalar.answers[0].value, "NameFixture");
  const radio = makeTechPlayOperationFixture(); const radioHarness = makeTechPlayOperationHarness(radio, {});
  assert.deepEqual(await radioHarness.harness.performAction({ provider: "techplay", candidate: radio.candidate, page: radio.page, action: { purpose: "fill", method: "ax_check", control: "techplay_answer_5_1" } }), { status: "success" });
  assert.equal(radio.radioAnswers[0].checked, true);
  const optout = makeTechPlayOperationFixture(); const optoutHarness = makeTechPlayOperationHarness(optout, {});
  assert.deepEqual(await optoutHarness.harness.performAction({ provider: "techplay", candidate: optout.candidate, page: optout.page, action: { purpose: "fill", method: "ax_uncheck", control: "techplay_optout_area_1" } }), { status: "success" });
  assert.equal(optout.optouts[0].ariaChecked, false);
});

test("TECH PLAY operation waits for transient oversized post-inspection to stabilize", async () => {
  const options = { transientInspectCalls: [3], transientNodes: Array.from({ length: 104 }, (_, index) => makeTechPlayElement({ type: "text", id: `aux_${index}`, hidden: true })) };
  const resolverFixture = makeTechPlayPrivateResolverFixture(); options.resolveValue = async (input) => { options.resolveCalls = (options.resolveCalls || 0) + 1; return resolverFixture.resolver(input); }; options.sleep = async () => { options.sleepCalls = (options.sleepCalls || 0) + 1; };
  const fixture = makeTechPlayOperationFixture(options); const { harness } = makeTechPlayOperationHarness(fixture, options);
  const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "fill", method: "ax_fill", control: "techplay_answer_1" } });
  assert.deepEqual(result, { status: "success" });
  assert.equal(fixture.answers[0].value, "NameFixture", "value"); assert.equal(options.inspectCalls, 4, "inspectCalls"); assert.equal(options.sleepCalls, 1, "sleepCalls"); assert.equal(options.operateCalls, 1, "operateCalls"); assert.equal(options.resolveCalls, 1, "resolveCalls"); assert.equal(options.proposeCalls || 0, 0, "proposeCalls");
});

test("TECH PLAY postcondition polling fails bounded on never-stable, drift, throw, and wrong completion", async () => {
  const auxiliary = Array.from({ length: 104 }, (_, index) => makeTechPlayElement({ type: "text", id: `aux_${index}`, hidden: true }));
  for (const [name, configure, expectedSleep] of [
    ["never-stable", (options) => { options.transientInspectCalls = Array.from({ length: 20 }, (_, index) => index + 3); }, 19],
    ["inspector-throw", (options) => { options.inspectThrowCalls = [3]; }, 0],
    ["wrong-completed", (options) => { options.apply = false; }, 19],
  ]) {
    const options = { transientNodes: auxiliary, sleep: async () => { options.sleepCalls = (options.sleepCalls || 0) + 1; } }; configure(options);
    const resolverFixture = makeTechPlayPrivateResolverFixture(); options.resolveValue = async (input) => { options.resolveCalls = (options.resolveCalls || 0) + 1; return resolverFixture.resolver(input); };
    const fixture = makeTechPlayOperationFixture(options); const { harness } = makeTechPlayOperationHarness(fixture, options);
    const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "fill", method: "ax_fill", control: "techplay_answer_1" } });
    assert.equal(result.status, "failed", name); assert.equal(options.sleepCalls || 0, expectedSleep, `${name}-sleep`); assert.equal(options.operateCalls, 1, `${name}-operate`); assert.equal(options.resolveCalls, 1, `${name}-resolve`); assert.equal(options.proposeCalls || 0, 0, `${name}-proposer`);
  }
  for (const [name, drift] of [["page-drift", (fixture) => fixture.setHref("https://techplay.jp/event/join/999191")], ["candidate-drift", (fixture) => { fixture.candidate.ticket_id = "98037"; }]]) {
    let fixture; const options = { transientInspectCalls: [3], transientNodes: auxiliary, sleep: async () => { options.sleepCalls = (options.sleepCalls || 0) + 1; drift(fixture); } };
    const resolverFixture = makeTechPlayPrivateResolverFixture(); options.resolveValue = async (input) => { options.resolveCalls = (options.resolveCalls || 0) + 1; return resolverFixture.resolver(input); };
    fixture = makeTechPlayOperationFixture(options); const { harness } = makeTechPlayOperationHarness(fixture, options);
    const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "fill", method: "ax_fill", control: "techplay_answer_1" } });
    assert.equal(result.status, "failed", name); assert.equal(options.sleepCalls, 1, `${name}-sleep`); assert.equal(options.operateCalls, 1, `${name}-operate`); assert.equal(options.resolveCalls, 1, `${name}-resolve`); assert.equal(options.proposeCalls || 0, 0, `${name}-proposer`);
  }
});

test("TECH PLAY harness rejects an invalid injected postcondition sleep", () => {
  assert.throws(() => createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() {} }, inspectControls() {}, proposeAction() {}, operateControl() {}, resolveValue() {}, sleep: "not-a-function",
  }), /Connector production Browser Harness invalid/);
  assert.throws(() => createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() {} }, techplayWorkflow: {}, inspectControls() {}, proposeAction() {}, operateControl() {}, resolveValue() {},
  }), /Connector production Browser Harness invalid/);
});

const KOKUCHPRO_EVENT_KEY = "33301feefecb914218e7c2318d9de99e", KOKUCHPRO_CANONICAL_URL = `https://www.kokuchpro.com/event/${KOKUCHPRO_EVENT_KEY}/`, KOKUCHPRO_ENTRY_URL = `${KOKUCHPRO_CANONICAL_URL}entry/`, KOKUCHPRO_TICKET_ID = "1", KOKUCHPRO_ENTRY_TOKEN = `kokuchpro_entry_${KOKUCHPRO_EVENT_KEY}`;
function kokuchProCandidate(overrides = {}) { return { provider: "kokuchpro", event_ref: `kokuchpro-event://event/${KOKUCHPRO_EVENT_KEY}`, canonical_url: KOKUCHPRO_CANONICAL_URL, ticket_id: KOKUCHPRO_TICKET_ID, ...overrides }; }

function makeKokuchProMultiStepPage({ candidate = kokuchProCandidate(), duplicateCanonical = false, duplicateFinal = false, malformedFinal = false, transientInspectCalls = [], onSleep, onInspect } = {}) {
  const entryUrl = `${candidate.canonical_url}entry/`; let href = candidate.canonical_url; const events = []; const clicks = []; let beforeDispatchCalls = 0; let inspectCalls = 0; let sleepCalls = 0;
  const document = { defaultView: { getComputedStyle() { return {}; } } };
  const makeElement = (tagName, properties = {}) => {
    const element = { tagName, ownerDocument: document, parentElement: null, isConnected: true, hidden: false, disabled: false, style: {}, dataset: {}, getBoundingClientRect() { return { width: 100, height: 20 }; }, ...properties };
    element.getAttribute = (name) => element[name] == null ? "" : element[name];
    return element;
  };
  const form = makeElement("FORM", { action: entryUrl, method: "POST" });
  const method = makeElement("INPUT", { type: "hidden", name: "_method", value: "POST" });
  const email = makeElement("INPUT", { type: "hidden", id: "EntryEmail", name: "data[Entry][email]", value: "hidden-email" });
  const name = makeElement("INPUT", { type: "hidden", id: "EntryName", name: "data[Entry][name]", value: "hidden-name" });
  const publish = makeElement("INPUT", { type: "hidden", id: "EntryNamePublish", name: "data[Entry][name_publish]", value: "1" });
  const message = makeElement("TEXTAREA", { id: "EntryMessage", name: "data[Entry][message]", value: "" });
  const seatLabel = makeElement("LABEL", { innerText: "8/31(月)18:00～（無料）", textContent: "8/31(月)18:00～（無料）" });
  const seat = makeElement("INPUT", { type: "radio", name: "data[Entry][seat]", value: "4631739", checked: false, labels: [seatLabel] });
  seatLabel.control = seat; seatLabel.contains = (element) => element === seat;
  const final = makeElement("BUTTON", { type: malformedFinal ? "button" : "submit", id: "entry_submit_button", innerText: "申込む", textContent: "申込む" });
  const decoyFinal = makeElement("BUTTON", { type: "submit", id: "entry_submit_button_2", innerText: "申込む", textContent: "申込む" });
  const canonicalMethod = makeElement("INPUT", { type: "hidden", name: "_method", value: "POST" });
  const canonicalAvailability = makeElement("INPUT", { type: "hidden", id: "FormEntryAvailability", name: "data[Form][entry_availability]", value: candidate.ticket_id });
  const canonicalButton = makeElement("BUTTON", { type: "submit", innerText: "申込む", textContent: "申込む" });
  const canonicalForm = makeElement("FORM", { action: entryUrl, method: "POST" });
  const canonicalForm2 = duplicateCanonical ? makeElement("FORM", { action: entryUrl, method: "POST" }) : null;
  const canonicalMethod2 = duplicateCanonical ? makeElement("INPUT", { type: "hidden", name: "_method", value: "POST" }) : null;
  const canonicalAvailability2 = duplicateCanonical ? makeElement("INPUT", { type: "hidden", id: "FormEntryAvailability", name: "data[Form][entry_availability]", value: candidate.ticket_id }) : null;
  const canonicalButton2 = duplicateCanonical ? makeElement("BUTTON", { type: "submit", innerText: "申込む", textContent: "申込む" }) : null;
  const searchForm = makeElement("FORM", { action: "https://www.kokuchpro.com/s/", method: "GET" });
  const search = makeElement("INPUT", { type: "search", name: "q", value: "" });
  const searchButton = makeElement("BUTTON", { type: "submit", innerText: "", textContent: "" });
  if (canonicalForm2) canonicalForm2.elements = [canonicalMethod2, canonicalAvailability2, canonicalButton2];
  const linkForms = [searchForm, canonicalForm, ...(canonicalForm2 ? [canonicalForm2] : [])];
  canonicalForm.elements = [canonicalMethod, canonicalAvailability, canonicalButton];
  form.elements = [method, email, name, publish, message, seat, final, ...(duplicateFinal ? [decoyFinal] : [])];
  searchForm.elements = [search, searchButton];
  for (const [owner, controls] of [[canonicalForm, canonicalForm.elements], ...(canonicalForm2 ? [[canonicalForm2, canonicalForm2.elements]] : []), [form, form.elements], [searchForm, searchForm.elements]]) {
    for (const element of controls) { element.form = owner; element.parentElement = owner; }
  }
  const currentForms = () => href === candidate.canonical_url ? linkForms : [searchForm, form];
  const tokenOf = (selector) => /^\[data-lm-connector-control="([^\"]+)"\]$/.exec(selector)?.[1] || "";
  const matching = (token) => currentForms().flatMap((item) => item.elements.flatMap((element) => [element, ...Array.from(element.labels || [])])).filter((element) => element.dataset?.lmConnectorControl === token);
  const page = {
    url() { return href; },
    async goto(url, options) {
      events.push(["goto", url, options]);
      if (url !== entryUrl) throw new Error("wrong navigation");
      href = url;
    },
    locator(selector) {
      const token = tokenOf(selector);
      if (!token) return { async evaluateAll(callback, context) { inspectCalls += 1; if (transientInspectCalls.includes(inspectCalls)) return []; const result = Function("return (" + callback.toString() + ")")()(currentForms(), context); onInspect?.(inspectCalls); return result; } };
      return {
        async count() { return matching(token).length; },
        async elementHandles() { const elements = matching(token); if (elements.length !== 1) return []; const [element] = elements; return [{ async evaluate(callback, context) { if (element === seat) { element.evaluateCalls = (element.evaluateCalls || 0) + 1; element.mutateOnEvaluate?.(element.evaluateCalls); } return callback(element, context); }, async fill(value) { if (elements.length !== 1) throw new Error("ambiguous control"); element.value = element.fillValue === undefined ? value : element.fillValue; events.push(["fill", token]); }, async click() { element.clicked = true; if (element === seatLabel) { seat.checked = true; events.push(["check", token]); return; } clicks.push(element); events.push(["click", token]); if (element === final) href = KOKUCHPRO_ENTRY_URL; } }]; },
        async click() { const elements = matching(token); if (elements.length !== 1) throw new Error("ambiguous control"); elements[0].clicked = true; clicks.push(elements[0]); events.push(["click", token]); },
        async check() { const elements = matching(token); if (elements.length !== 1) throw new Error("ambiguous control"); elements[0].checked = true; events.push(["check", token]); },
      };
    },
  };
  const sleep = async () => { sleepCalls += 1; await onSleep?.(); };
  return { page, candidate, form, canonicalForm, seat, final, clicks, events, sleep, entryUrl, setHref(value) { href = value; }, get inspectCalls() { return inspectCalls; }, get sleepCalls() { return sleepCalls; }, get beforeDispatchCalls() { return beforeDispatchCalls; }, beforeDispatch() { beforeDispatchCalls += 1; } };
}

function makeKokuchProSpinnerPage({ spinnerMax = "2", ...options } = {}) {
  const fixture = makeKokuchProMultiStepPage(options);
  Object.assign(fixture.seat, {
    type: "text",
    name: "data[Entry][seat4637229]",
    value: "0",
    class: "form-control spinner ui-spinner-input",
    className: "form-control spinner ui-spinner-input",
    labels: [],
  });
  fixture.seat["data-spinner-min"] = "0";
  fixture.seat["data-spinner-max"] = spinnerMax;
  return fixture;
}

test("KokuchPro canonical route navigates by GET, checks one seat locally, then clicks one pinned final", async () => {
  const fixture = makeKokuchProMultiStepPage(); let agentCalls = 0; let readbacks = 0;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", extensionProvider: "kokuchpro", async runAgentRunner() { agentCalls += 1; throw new Error("KokuchPro multi-step flow must stay native"); } });
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    extensionProvider: "kokuchpro",
    extensionWorkflow: { async readProviderState() { readbacks += 1; return fixture.clicks.includes(fixture.final) ? { status: "registered", receipt_id: "kokuchpro-multistep-1" } : { status: "absent" }; } },
    inspectControls: inspectPageControls, proposeAction: proposer,
    operateControl: (input) => operatePageControl({ ...input, beforeDispatch() { fixture.beforeDispatch(); input.beforeDispatch?.(); } }),
    resolveValue: createPrivateValueResolver({ async readPeatixProfile() { throw new Error("KokuchPro must not read private profiles"); }, async readFormProfile() { throw new Error("KokuchPro must not read private profiles"); } }),
  });
  const result = await harness.runFallback({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/KOKUCHPROMULTISTEP1", maxSteps: 3, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed"); assert.equal(result.provider_state.status, "registered"); assert.equal(agentCalls, 0); assert.equal(readbacks, 4);
  assert.deepEqual(fixture.events.map(([name]) => name), ["goto", "check", "click"]); assert.equal(fixture.beforeDispatchCalls, 1); assert.equal(fixture.clicks.length, 1); assert.equal(fixture.clicks[0], fixture.final);
  assert.doesNotMatch(JSON.stringify(result), /hidden-email|hidden-name|4631739/);
});

test("KokuchPro final control rejects duplicate or malformed final before dispatch", async () => {
  for (const options of [{ duplicateFinal: true }, { malformedFinal: true }]) {
    const fixture = makeKokuchProMultiStepPage(options); let beforeDispatchCalls = 0;
    await fixture.page.goto(KOKUCHPRO_ENTRY_URL);
    const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep });
    assert.deepEqual(controls, [], JSON.stringify(options));
    const finalToken = KOKUCHPRO_ENTRY_TOKEN.replace("kokuchpro_entry_", "kokuchpro_final_");
    const result = await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: { control: finalToken, kind: "button", label: "申込む", required: false, completed: false, submittable: true }, action: { purpose: "submit", method: "ax_click", control: finalToken }, sleep: fixture.sleep, beforeDispatch: () => { beforeDispatchCalls += 1; } });
    assert.equal(result.status, "failed", JSON.stringify(options)); assert.equal(beforeDispatchCalls, 0, JSON.stringify(options)); assert.equal(fixture.clicks.length, 0, JSON.stringify(options));
  }
});

test("KokuchPro canonical inspector accepts one or two exact forms and polls serialized DOM", async () => {
  const fixture = makeKokuchProMultiStepPage({ duplicateCanonical: true, transientInspectCalls: [1] });
  const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep });
  assert.deepEqual(controls, [{ control: KOKUCHPRO_ENTRY_TOKEN, kind: "link", label: "申込む", required: false, completed: false, submittable: false }]);
  assert.equal(fixture.inspectCalls, 2); assert.equal(fixture.sleepCalls, 1);
  const malformed = makeKokuchProMultiStepPage(); malformed.canonicalForm.method = "GET";
  assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: malformed.candidate, page: malformed.page, sleep: malformed.sleep }), []);
});

test("KokuchPro entry inspector polls the exact browser-realm form without exposing hidden values", async () => {
  const fixture = makeKokuchProMultiStepPage({ transientInspectCalls: [1] }); await fixture.page.goto(fixture.entryUrl);
  const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep });
  assert.equal(controls.length, 2); assert.equal(controls[0].kind, "radio"); assert.equal(controls[0].completed, false); assert.equal(controls[1].kind, "button");
  assert.equal(fixture.inspectCalls, 2); assert.equal(fixture.sleepCalls, 1); assert.doesNotMatch(JSON.stringify(controls), /hidden-email|hidden-name|4631739/);
});

test("KokuchPro live spinner form fills one seat with ax_fill before one final submit", async () => {
  const fixture = makeKokuchProSpinnerPage(); let agentCalls = 0; let readbacks = 0;
  const proposer = createBoundedActionProposer({ repoRoot: "/private/repo", evidenceDir: "/private/evidence", extensionProvider: "kokuchpro", async runAgentRunner() { agentCalls += 1; throw new Error("KokuchPro spinner flow must stay native"); } });
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    extensionProvider: "kokuchpro",
    extensionWorkflow: { async readProviderState() { readbacks += 1; return fixture.clicks.includes(fixture.final) ? { status: "registered", receipt_id: "kokuchpro-spinner-1" } : { status: "absent" }; } },
    inspectControls: inspectPageControls, proposeAction: proposer,
    operateControl: (input) => operatePageControl({ ...input, beforeDispatch() { fixture.beforeDispatch(); input.beforeDispatch?.(); } }),
    resolveValue: createPrivateValueResolver({ async readPeatixProfile() { throw new Error("KokuchPro must not read private profiles"); }, async readFormProfile() { throw new Error("KokuchPro must not read private profiles"); } }),
  });
  const result = await harness.runFallback({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/KOKUCHPROSPINNER1", maxSteps: 3, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed"); assert.equal(result.provider_state.status, "registered"); assert.equal(agentCalls, 0); assert.equal(readbacks, 4);
  assert.deepEqual(fixture.events.map(([name]) => name), ["goto", "fill", "click"]); assert.equal(fixture.seat.value, "1"); assert.equal(fixture.beforeDispatchCalls, 1); assert.equal(fixture.clicks.length, 1); assert.equal(fixture.clicks[0], fixture.final);
  assert.doesNotMatch(JSON.stringify(result), /seat4637229|hidden-email|hidden-name/);
});

test("KokuchPro spinner inspector rejects every measured-shape deviation", async () => {
  const variants = [
    ["wrong-name", (fixture) => { fixture.seat.name = "data[Entry][seat]"; }],
    ["wrong-type", (fixture) => { fixture.seat.type = "number"; }],
    ["wrong-class", (fixture) => { fixture.seat.class = "form-control spinner ui-spinner-input extra"; fixture.seat.className = fixture.seat.class; }],
    ["wrong-min", (fixture) => { fixture.seat["data-spinner-min"] = "1"; }],
    ["zero-max", (fixture) => { fixture.seat["data-spinner-max"] = "0"; }],
    ["noninteger-max", (fixture) => { fixture.seat["data-spinner-max"] = "2.5"; }],
    ["unsafe-max", (fixture) => { fixture.seat["data-spinner-max"] = "9007199254740992"; }],
    ["wrong-value", (fixture) => { fixture.seat.value = "2"; }],
    ["duplicate-spinner", (fixture) => { const duplicate = { ...fixture.seat, dataset: {}, form: fixture.form, parentElement: fixture.form }; fixture.form.elements.push(duplicate); }],
    ["mixed-radio-spinner", (fixture) => { const radio = { ...fixture.seat, type: "radio", name: "data[Entry][seat]", dataset: {}, form: fixture.form, parentElement: fixture.form }; fixture.form.elements.push(radio); }],
    ["extra-visible-input", (fixture) => { const extra = { ...fixture.seat, name: "unrelated", dataset: {}, form: fixture.form, parentElement: fixture.form }; fixture.form.elements.push(extra); }],
  ];
  for (const [name, mutate] of variants) {
    const fixture = makeKokuchProSpinnerPage(); await fixture.page.goto(fixture.entryUrl); mutate(fixture);
    assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep }), [], name);
  }
});

test("KokuchPro spinner action pins the input, rejects drift, and never dispatches before final submit", async () => {
  for (const [name, configure, expectedFills] of [
    ["wrong-value", (fixture) => {}, 0],
    ["value-not-one", (fixture) => { fixture.seat.fillValue = "0"; }, 1],
    ["rebound-drift", (fixture) => { fixture.seat.mutateOnEvaluate = (count) => { if (count === 2) fixture.seat.name = "data[Entry][seat999999]"; }; }, 0],
  ]) {
    const fixture = makeKokuchProSpinnerPage(); await fixture.page.goto(fixture.entryUrl); configure(fixture);
    const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep }); let beforeDispatchCalls = 0;
    const result = await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: controls[0], action: { purpose: "fill", method: "ax_fill", control: controls[0].control }, value: name === "wrong-value" ? "2" : "1", beforeDispatch: () => { beforeDispatchCalls += 1; }, sleep: fixture.sleep });
    assert.equal(result.status, "failed", name); assert.equal(fixture.events.filter(([event]) => event === "fill").length, expectedFills, `${name}-fills`); assert.equal(beforeDispatchCalls, 0, `${name}-before-dispatch`); assert.equal(fixture.clicks.length, 0, `${name}-clicks`);
  }
});

test("KokuchPro entry handle reads reject form mutations after the initial inspect", async () => {
  const mutations = [
    ["second-method", (fixture) => { const duplicate = { ...fixture.form.elements[0], dataset: {}, form: fixture.form, parentElement: fixture.form }; fixture.form.elements.push(duplicate); }],
    ["second-final", (fixture) => { const duplicate = { ...fixture.final, dataset: {}, form: fixture.form, parentElement: fixture.form }; fixture.form.elements.push(duplicate); }],
    ["extra-visible-input", (fixture) => { const extra = { ...fixture.seat, dataset: {}, name: "extra", form: fixture.form, parentElement: fixture.form }; extra.getAttribute = (name) => extra[name] == null ? "" : extra[name]; fixture.form.elements.push(extra); }],
  ];
  for (const [name, mutate] of mutations) {
    let fixture; fixture = makeKokuchProSpinnerPage({ onInspect: (calls) => { if (calls === 2) mutate(fixture); } }); await fixture.page.goto(fixture.entryUrl);
    const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep }); let beforeDispatchCalls = 0;
    const seatResult = await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: controls[0], action: { purpose: "fill", method: "ax_fill", control: controls[0].control }, value: "1", beforeDispatch: () => { beforeDispatchCalls += 1; }, sleep: fixture.sleep });
    assert.equal(seatResult.status, "failed", `${name}-seat`); assert.equal(beforeDispatchCalls, 0, `${name}-seat-before-dispatch`); assert.equal(fixture.events.filter(([event]) => event === "fill").length, 0, `${name}-seat-fill`); assert.equal(fixture.clicks.length, 0, `${name}-seat-click`);
  }
  for (const [name, mutate] of mutations) {
    let fixture; fixture = makeKokuchProSpinnerPage({ onInspect: (calls) => { if (calls === 2) mutate(fixture); } }); fixture.seat.value = "1"; await fixture.page.goto(fixture.entryUrl);
    const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep }); let beforeDispatchCalls = 0;
    const finalResult = await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: controls[1], action: { purpose: "submit", method: "ax_click", control: controls[1].control }, beforeDispatch: () => { beforeDispatchCalls += 1; }, sleep: fixture.sleep });
    assert.equal(finalResult.status, "failed", `${name}-final`); assert.equal(beforeDispatchCalls, 0, `${name}-final-before-dispatch`); assert.equal(fixture.clicks.length, 0, `${name}-final-click`);
  }
});

test("KokuchPro spinner accepts organizer maxima one and three while always filling one", async () => {
  for (const spinnerMax of ["1", "3"]) {
    const fixture = makeKokuchProSpinnerPage({ spinnerMax }); await fixture.page.goto(fixture.entryUrl);
    const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep });
    assert.equal(controls[0].kind, "input", `max-${spinnerMax}-kind`); assert.equal(controls[0].completed, false, `max-${spinnerMax}-incomplete`);
    const result = await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: controls[0], action: { purpose: "fill", method: "ax_fill", control: controls[0].control }, value: "1", sleep: fixture.sleep });
    assert.deepEqual(result, { status: "success" }, `max-${spinnerMax}-result`); assert.equal(fixture.seat.value, "1", `max-${spinnerMax}-value`);
  }
});

test("KokuchPro readiness ignores hidden submits, rejects unrelated submits, and bounds timeout and abort", async () => {
  const hidden = makeKokuchProMultiStepPage(); await hidden.page.goto(hidden.entryUrl);
  const hiddenSubmit = { ...hidden.final, id: "decoy", dataset: {}, getBoundingClientRect() { return { width: 0, height: 0 }; } }; hiddenSubmit.form = hidden.form; hiddenSubmit.parentElement = hidden.form; hidden.form.elements.push(hiddenSubmit);
  assert.equal((await inspectPageControls({ provider: "kokuchpro", candidate: hidden.candidate, page: hidden.page, sleep: hidden.sleep })).length, 2);
  const unrelated = makeKokuchProMultiStepPage(); await unrelated.page.goto(unrelated.entryUrl);
  const unrelatedSubmit = { ...unrelated.final, id: "decoy", dataset: {}, innerText: "別の送信", textContent: "別の送信" }; unrelatedSubmit.form = unrelated.form; unrelatedSubmit.parentElement = unrelated.form; unrelated.form.elements.push(unrelatedSubmit);
  assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: unrelated.candidate, page: unrelated.page, sleep: unrelated.sleep }), []);
  const timeout = makeKokuchProMultiStepPage(); await timeout.page.goto(timeout.entryUrl); timeout.form.elements = timeout.form.elements.filter((element) => element !== timeout.seat && element !== timeout.final);
  assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: timeout.candidate, page: timeout.page, sleep: timeout.sleep }), []); assert.equal(timeout.inspectCalls, 41); assert.equal(timeout.sleepCalls, 40);
  const controller = new AbortController(); const aborted = makeKokuchProMultiStepPage({ onSleep: () => controller.abort() }); await aborted.page.goto(aborted.entryUrl); aborted.form.elements = aborted.form.elements.filter((element) => element !== aborted.seat && element !== aborted.final);
  assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: aborted.candidate, page: aborted.page, signal: controller.signal, sleep: aborted.sleep }), []); assert.equal(aborted.sleepCalls, 1);
});

test("KokuchPro occurrence binding stamps distinct route, seat, and final tokens", async () => {
  const occurrence = "1901"; const candidate = kokuchProCandidate({ event_ref: `kokuchpro-event://event/${KOKUCHPRO_EVENT_KEY}/${occurrence}`, canonical_url: `${KOKUCHPRO_CANONICAL_URL}${occurrence}/` });
  const fixture = makeKokuchProMultiStepPage({ candidate }); const route = await inspectPageControls({ provider: "kokuchpro", candidate, page: fixture.page, sleep: fixture.sleep });
  assert.equal(route[0].control, `${KOKUCHPRO_ENTRY_TOKEN}_o_1gt`); await fixture.page.goto(fixture.entryUrl);
  const entry = await inspectPageControls({ provider: "kokuchpro", candidate, page: fixture.page, sleep: fixture.sleep });
  assert.deepEqual(entry.map((control) => control.control), [`kokuchpro_seat_${KOKUCHPRO_EVENT_KEY}_o_1gt`, `kokuchpro_final_${KOKUCHPRO_EVENT_KEY}_o_1gt`]); assert.notEqual(entry[0].control, entry[1].control);
});

test("KokuchPro semantic ambiguity and binding drift fail closed", async () => {
  const fixture = makeKokuchProMultiStepPage(); await fixture.page.goto(fixture.entryUrl); const duplicateIdentity = { ...fixture.form.elements[1], dataset: {}, value: "second" }; duplicateIdentity.form = fixture.form; duplicateIdentity.parentElement = fixture.form; fixture.form.elements.push(duplicateIdentity);
  assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep }), []);
  const emptyIdentity = makeKokuchProMultiStepPage(); await emptyIdentity.page.goto(emptyIdentity.entryUrl); emptyIdentity.form.elements[1].value = "   "; assert.deepEqual(await inspectPageControls({ provider: "kokuchpro", candidate: emptyIdentity.candidate, page: emptyIdentity.page, sleep: emptyIdentity.sleep }), []);
  const route = makeKokuchProMultiStepPage(); const controls = await inspectPageControls({ provider: "kokuchpro", candidate: route.candidate, page: route.page, sleep: route.sleep }); route.setHref("https://www.kokuchpro.com/event/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/");
  assert.equal((await operatePageControl({ provider: "kokuchpro", candidate: route.candidate, page: route.page, control: controls[0], action: { purpose: "submit", method: "ax_click", control: controls[0].control } })).status, "failed"); assert.equal(route.events.length, 0);
});

test("KokuchPro pinned final handle rejects pre-dispatch DOM drift without a second click", async () => {
  const fixture = makeKokuchProMultiStepPage(); await fixture.page.goto(fixture.entryUrl); const controls = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep });
  assert.equal((await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: controls[0], action: { purpose: "fill", method: "ax_check", control: controls[0].control }, value: true, sleep: fixture.sleep })).status, "success");
  const after = await inspectPageControls({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, sleep: fixture.sleep }); let beforeDispatchCalls = 0;
  const result = await operatePageControl({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, control: after[1], action: { purpose: "submit", method: "ax_click", control: after[1].control }, beforeDispatch: () => { beforeDispatchCalls += 1; fixture.final.type = "button"; }, sleep: fixture.sleep });
  assert.equal(result.status, "failed"); assert.equal(result.attempted, true); assert.equal(beforeDispatchCalls, 1); assert.equal(fixture.clicks.length, 0);
});


test("configured extension provider reuses the generic fallback for its exact token", async () => {
  const page = { url() { return "https://extension.example.test/event/1"; } };
  const candidate = { event_ref: "extension-event://event/1" };
  let operated = 0;
  let readbacks = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    extensionProvider: "extension-site",
    extensionWorkflow: {
      async readProviderState(input) {
        assert.equal(input.page, page);
        assert.equal(input.candidate, candidate);
        readbacks += 1;
        return operated === 1 ? { status: "registered", receipt_id: "extension-1" } : { status: "absent" };
      },
    },
    async inspectControls() {
      return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }];
    },
    async proposeAction(input) {
      assert.equal(input.provider, "extension-site");
      return { control: "register_button" };
    },
    async operateControl(input) {
      assert.equal(input.page, page);
      input.beforeDispatch?.();
      operated += 1;
      return { status: "success" };
    },
    async resolveValue() { throw new Error("extension submit must not resolve a private value"); },
  });

  const result = await harness.runFallback({
    provider: "extension-site", candidate, page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EXTENSION1",
    maxSteps: 2, expectedState: "registered_or_pending",
  });
  assert.equal(result.status, "completed");
  assert.equal(result.provider_state.status, "registered");
  assert.deepEqual(result.repaired_actions, [{ purpose: "submit", method: "ax_click", control: "register_button" }]);
  assert.equal(operated, 1);
  assert.equal(readbacks, 2);
  await assert.rejects(() => harness.runFallback({
    provider: "another-extension", candidate, page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EXTENSION2",
    maxSteps: 1, expectedState: "registered_or_pending",
  }), /Connector production Browser Harness invalid/);
});

test("extension provider configuration is an exact safe pair", () => {
  const workflow = { async readProviderState() { return { status: "absent" }; } };
  const base = {
    lumaWorkflow: { async readProviderState() {} },
    inspectControls() {}, proposeAction() {}, operateControl() {}, resolveValue() {},
  };
  for (const options of [
    { extensionProvider: "extension-site" },
    { extensionWorkflow: workflow },
    { extensionProvider: "Extension-Site", extensionWorkflow: workflow },
    { extensionProvider: "x", extensionWorkflow: workflow },
    { extensionProvider: "extension-site", extensionWorkflow: {} },
    { extensionProvider: "luma", extensionWorkflow: workflow },
  ]) {
    assert.throws(() => createProductionBrowserHarness({ ...base, ...options }), /Connector production Browser Harness invalid/);
  }
  assert.doesNotThrow(() => createProductionBrowserHarness({ ...base }));
});

test("extension fallback requires independent workflow readback despite action proof", async () => {
  const cases = [
    ["absent", async () => ({ status: "absent" })],
    ["unavailable", async () => ({ status: "unavailable" })],
    ["malformed", async () => ({ status: 42 })],
    ["throw", async () => { throw new Error("extension readback"); }],
  ];
  for (const claimedStatus of ["registered", "pending"]) {
    for (const [name, read] of cases) {
      let reads = 0;
      let operations = 0;
      const harness = createProductionBrowserHarness({
        lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
        extensionProvider: "extension-proof",
        extensionWorkflow: { async readProviderState(input) { reads += 1; return read(input); } },
        async inspectControls() { return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
        async proposeAction() { return { control: "register_button" }; },
        async operateControl(input) { operations += 1; input.beforeDispatch?.(); return { status: "success", provider_state: { status: claimedStatus } }; },
        async resolveValue() { return null; },
      });
      const result = await harness.runFallback({ provider: "extension-proof", candidate: { event_ref: "extension-proof://event/1" }, page: { url() { return "https://extension-proof.example/event/1"; } }, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/EXTENSION-PROOF-${claimedStatus}-${name}`, maxSteps: 1, expectedState: "registered_or_pending" });
      assert.equal(reads, 2, `${claimedStatus}-${name}-readback`);
      assert.equal(operations, 1, `${claimedStatus}-${name}-operation`);
      assert.equal(result.status, "failed", `${claimedStatus}-${name}-status`);
      assert.equal(result.provider_state, undefined, `${claimedStatus}-${name}-no-self-proof`);
    }
  }
});

test("extension auth preflight is terminal before any browser adapter step", async () => {
  const calls = { readback: 0, inspect: 0, propose: 0, operate: 0, resolve: 0 };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    extensionProvider: "extension-auth",
    extensionWorkflow: { async readProviderState() { calls.readback += 1; return { status: "auth_required" }; } },
    async inspectControls() { calls.inspect += 1; return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
    async proposeAction() { calls.propose += 1; return { control: "register_button" }; },
    async operateControl() { calls.operate += 1; return { status: "success" }; },
    async resolveValue() { calls.resolve += 1; return "private-value"; },
  });
  const result = await harness.runFallback({
    provider: "extension-auth", candidate: { event_ref: "extension-auth://event/1" },
    page: { url() { return "https://extension-auth.example/event/1"; } },
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EXTENSION-AUTH",
    maxSteps: 10, expectedState: "registered_or_pending",
  });
  assert.deepEqual(result, { status: "failed", safe_reason: "auth_required", repaired_actions: [] });
  assert.deepEqual(calls, { readback: 1, inspect: 0, propose: 0, operate: 0, resolve: 0 });
});

test("extension auth preflight cannot bypass adapter scope validation", async () => {
  const calls = { readback: 0, inspect: 0, propose: 0, operate: 0, resolve: 0 };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    extensionProvider: "extension-scope",
    extensionWorkflow: { async readProviderState() { calls.readback += 1; return { status: "auth_required" }; } },
    async inspectControls() { calls.inspect += 1; return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
    async proposeAction() { calls.propose += 1; return { control: "register_button" }; },
    async operateControl() { calls.operate += 1; return { status: "success" }; },
    async resolveValue() { calls.resolve += 1; return "private-value"; },
  });
  const candidate = { event_ref: "extension-scope://event/1" };
  const validWebsocket = "ws://127.0.0.1:9222/devtools/page/EXTENSION-SCOPE";
  for (const [name, input] of [
    ["malformed websocket", { page: {}, pageWebsocket: "http://bad", maxSteps: 1, expectedState: "registered_or_pending" }],
    ["zero maxSteps", { page: {}, pageWebsocket: validWebsocket, maxSteps: 0, expectedState: "registered_or_pending" }],
    ["wrong expectedState", { page: {}, pageWebsocket: validWebsocket, maxSteps: 1, expectedState: "registered" }],
    ["null page", { page: null, pageWebsocket: validWebsocket, maxSteps: 1, expectedState: "registered_or_pending" }],
  ]) {
    await assert.rejects(
      () => harness.runFallback({ provider: "extension-scope", candidate, ...input }),
      /Browser Harness adapter invalid/,
      name,
    );
  }
  assert.deepEqual(calls, { readback: 0, inspect: 0, propose: 0, operate: 0, resolve: 0 });
});

test("extension auth after one submit effect becomes effect_unknown without retry", async () => {
  const calls = { readback: 0, inspect: 0, propose: 0, operate: 0, resolve: 0 };
  const action = { purpose: "submit", method: "ax_click", control: "register_button" };
  const page = { url() { return "https://extension-auth-after.example/event/1"; } };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    extensionProvider: "extension-auth-after",
    extensionWorkflow: { async readProviderState() { calls.readback += 1; return { status: calls.readback === 1 ? "absent" : "auth_required" }; } },
    async inspectControls() { calls.inspect += 1; return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
    async proposeAction() { calls.propose += 1; return { control: "register_button" }; },
    async operateControl(input) { calls.operate += 1; input.beforeDispatch?.(); return { status: "success" }; },
    async resolveValue() { calls.resolve += 1; return "must-not-read"; },
  });
  const result = await harness.runFallback({
    provider: "extension-auth-after", candidate: { event_ref: "extension-auth-after://event/1" }, page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/EXTENSION-AUTH-AFTER",
    maxSteps: 2, expectedState: "registered_or_pending",
  });
  assert.deepEqual(result, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [action] });
  assert.deepEqual(calls, { readback: 2, inspect: 1, propose: 1, operate: 1, resolve: 0 });
});

test("extension submit success followed by step-limit or unavailable readback becomes effect_unknown", async () => {
  for (const [name, readback] of [
    ["step-limit", async () => ({ status: "unavailable" })],
    ["unavailable", async () => ({ status: "unavailable" })],
  ]) {
    let operated = 0;
    const action = { purpose: "submit", method: "ax_click", control: "register_button" };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      extensionProvider: `extension-effect-${name}`,
      extensionWorkflow: { async readProviderState() { return readback(); } },
      async inspectControls() { return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
      async proposeAction() { return { control: "register_button" }; },
      async operateControl(input) { operated += 1; assert.deepEqual(input.action, action); input.beforeDispatch?.(); return { status: "success" }; },
      async resolveValue() { return null; },
    });
    const result = await harness.runFallback({
      provider: `extension-effect-${name}`,
      candidate: { event_ref: `extension-effect-${name}://event/1` },
      page: { url() { return `https://extension-effect-${name}.example/event/1`; } },
      pageWebsocket: `ws://127.0.0.1:9222/devtools/page/EXTENSION-EFFECT-${name.toUpperCase()}`,
      maxSteps: 1,
      expectedState: "registered_or_pending",
    });
    assert.deepEqual(result, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [action] }, name);
    assert.equal(operated, 1, name);
  }
});

test("extension submit attempt latches effect_unknown on dispatch failure but not pre-dispatch validation", async () => {
  for (const [name, operate] of [
    ["click-throw", async () => { throw new Error("locator click timeout"); }],
    ["non-success", async () => ({ status: "failed" })],
  ]) {
    let operated = 0;
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      extensionProvider: `extension-attempt-${name}`,
      extensionWorkflow: { async readProviderState() { return { status: "absent" }; } },
      async inspectControls() { return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
      async proposeAction() { return { control: "register_button" }; },
      async operateControl(input) {
        operated += 1;
        assert.deepEqual(input.action, { purpose: "submit", method: "ax_click", control: "register_button" });
        input.beforeDispatch?.();
        return operate();
      },
      async resolveValue() { return null; },
    });
    const result = await harness.runFallback({
      provider: `extension-attempt-${name}`,
      candidate: { event_ref: `extension-attempt-${name}://event/1` },
      page: { url() { return `https://extension-attempt-${name}.example/event/1`; } },
      pageWebsocket: `ws://127.0.0.1:9222/devtools/page/EXTENSION-ATTEMPT-${name.toUpperCase()}`,
      maxSteps: 1,
      expectedState: "registered_or_pending",
    });
    assert.deepEqual(result, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [] }, name);
    assert.equal(operated, 1, name);
  }

  for (const locatorCount of [0, 2]) {
    let operated = 0;
    const page = {
      url() { return `https://extension-validation-${locatorCount}.example/event/1`; },
      locator() { return { async count() { return locatorCount; } }; },
    };
    const harness = createProductionBrowserHarness({
      lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
      extensionProvider: `extension-validation-${locatorCount}`,
      extensionWorkflow: { async readProviderState() { return { status: "absent" }; } },
      async inspectControls() { return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
      async proposeAction() { return { control: "register_button" }; },
      async operateControl(input) { operated += 1; return operatePageControl(input); },
      async resolveValue() { return null; },
    });
    const validationResult = await harness.runFallback({
      provider: `extension-validation-${locatorCount}`,
      candidate: { event_ref: `extension-validation-${locatorCount}://event/1` },
      page,
      pageWebsocket: `ws://127.0.0.1:9222/devtools/page/EXTENSION-VALIDATION-${locatorCount}`,
      maxSteps: 1,
      expectedState: "registered_or_pending",
    });
    assert.deepEqual(validationResult, { status: "failed", safe_reason: "agent_action_failed", repaired_actions: [] }, `locator-${locatorCount}`);
    assert.equal(operated, 1, `locator-${locatorCount}`);
  }
});

test("production harness timeout before locator dispatch prevents immediate and delayed clicks", async () => {
  let clicks = 0;
  let proposals = 0;
  const page = {
    url() { return "https://luma.com/timeout-before-dispatch"; },
    locator() {
      return {
        async count() {
          await new Promise((resolve) => setTimeout(resolve, 30));
          return 1;
        },
        async click() { clicks += 1; },
      };
    },
  };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    async inspectControls() { return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
    async proposeAction() { proposals += 1; return { control: "register_button" }; },
    operateControl: operatePageControl,
    async resolveValue() { return null; },
  });

  const result = await harness.runFallback({
    provider: "luma",
    candidate: { event_ref: "luma-event://event/timeout-before-dispatch" },
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TIMEOUT-BEFORE-DISPATCH",
    maxSteps: 2,
    maxDurationMs: 5,
    expectedState: "registered_or_pending",
  });

  assert.deepEqual(result, { status: "failed", safe_reason: "time_limit", repaired_actions: [] });
  assert.equal(clicks, 0);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(clicks, 0);
  assert.equal(proposals, 1);
});

test("production harness timeout after submit dispatch is effect_unknown and does not start another step", async () => {
  let clicks = 0;
  let proposals = 0;
  const page = {
    url() { return "https://luma.com/timeout-after-dispatch"; },
    locator() {
      return {
        async count() { return 1; },
        async click() {
          clicks += 1;
          await new Promise((resolve) => setTimeout(resolve, 30));
        },
      };
    },
  };
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { return { status: "absent" }; } },
    async inspectControls() { return [{ control: "register_button", kind: "button", label: "Register", required: false, completed: false, submittable: true }]; },
    async proposeAction() { proposals += 1; return { control: "register_button" }; },
    operateControl: operatePageControl,
    async resolveValue() { return null; },
  });

  const result = await harness.runFallback({
    provider: "luma",
    candidate: { event_ref: "luma-event://event/timeout-after-dispatch" },
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TIMEOUT-AFTER-DISPATCH",
    maxSteps: 2,
    maxDurationMs: 5,
    expectedState: "registered_or_pending",
  });

  assert.deepEqual(result, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [] });
  assert.equal(clicks, 1);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(clicks, 1);
  assert.equal(proposals, 1);
});

test("TECH PLAY parent operation rejects radio ambiguity, drift, failed postcondition, and review/final clicks", async () => {
  for (const [name, resolveValue] of [
    ["zero-approved", async () => null],
    ["multiple-approved", async (input) => input.control.kind === "radio" ? true : "NameFixture"],
  ]) {
    const fixture = makeTechPlayOperationFixture(); const options = { resolveValue }; const { harness } = makeTechPlayOperationHarness(fixture, options);
    const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "fill", method: "ax_check", control: "techplay_answer_5_1" } });
    assert.deepEqual(result, { status: "failed" }, name); assert.equal(options.operateCalls || 0, 0, `${name}-operate`);
  }
  for (const [name, mutate] of [
    ["page-drift", (fixture) => fixture.setHref("https://techplay.jp/event/join/999191")],
    ["postcondition", (fixture) => { fixture.options.apply = false; }],
    ["duplicate-locator", (fixture) => { fixture.options.locatorCount = 2; }],
    ["missing-locator", (fixture) => { fixture.options.locatorCount = 0; }],
  ]) {
    const fixture = makeTechPlayOperationFixture(); const options = {}; const { harness } = makeTechPlayOperationHarness(fixture, options); mutate(fixture);
    const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "fill", method: "ax_fill", control: "techplay_answer_1" } });
    assert.deepEqual(result, { status: "failed" }, name);
  }
  const wrongMethod = makeTechPlayOperationFixture(); const wrongOptions = {}; const { harness: wrongHarness } = makeTechPlayOperationHarness(wrongMethod, wrongOptions);
  assert.deepEqual(await wrongHarness.performAction({ provider: "techplay", candidate: wrongMethod.candidate, page: wrongMethod.page, action: { purpose: "submit", method: "ax_click", control: "techplay_answer_1" } }), { status: "failed" }, "wrong-method");
  assert.equal(wrongOptions.operateCalls || 0, 0);
  const driftCandidate = makeTechPlayOperationFixture(); const { harness: driftHarness } = makeTechPlayOperationHarness(driftCandidate, {});
  assert.deepEqual(await driftHarness.performAction({ provider: "techplay", candidate: { ...driftCandidate.candidate, event_ref: "techplay-event://event/999191" }, page: driftCandidate.page, action: { purpose: "fill", method: "ax_fill", control: "techplay_answer_1" } }), { status: "failed" }, "candidate-drift");
  const blocked = makeTechPlayOperationFixture(); blocked.answers.forEach((answer) => { answer.value = "filled"; }); blocked.radioAnswers[0].checked = true; blocked.radioAnswers[3].checked = true; blocked.optouts.forEach((optout) => { optout.ariaChecked = false; });
  const { harness: blockedHarness } = makeTechPlayOperationHarness(blocked, {});
  const reviewResult = await blockedHarness.performAction({ provider: "techplay", candidate: blocked.candidate, page: blocked.page, action: { purpose: "submit", method: "ax_click", control: "techplay_review_999190" } });
  assert.equal(reviewResult.status, "failed"); assert.equal(reviewResult.safe_reason, "effect_unknown");
  assert.deepEqual(await blockedHarness.performAction({ provider: "techplay", candidate: blocked.candidate, page: blocked.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } }), { status: "failed" }, "final");
  assert.deepEqual(await blockedHarness.performAction({ provider: "techplay", candidate: blocked.candidate, page: blocked.page, action: { purpose: "fill", method: "ax_check", control: "techplay_ticket_999190" } }), { status: "failed" }, "ticket");
});

test("TECH PLAY final action clicks the exact confirm CTA and accepts registered readback", async () => {
  const candidate = {
    provider: "techplay",
    event_ref: "techplay-event://event/999190",
    canonical_url: "https://techplay.jp/event/999190",
    ticket_id: "98036",
  };
  const page = { url() { return "https://techplay.jp/event/join/999190/confirm"; } };
  let clicks = 0;
  const harness = createProductionBrowserHarness({
    lumaWorkflow: { async readProviderState() { throw new Error("wrong provider"); } },
    techplayWorkflow: { async readProviderState() { return { status: "registered", receipt_id: "techplay-1" }; } },
    async inspectControls() {
      return [{ control: "techplay_final_999190", kind: "button", label: "申し込みを確定する", required: false, completed: false, submittable: true }];
    },
    async operateControl() { clicks += 1; return { status: "success" }; },
    async proposeAction() { throw new Error("TECH PLAY final must not use proposer"); },
    async resolveValue() { return null; },
  });
  const result = await harness.performAction({
    provider: "techplay",
    candidate,
    page,
    action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" },
  });
  assert.deepEqual(result, { status: "success", provider_state: { status: "registered", receipt_id: "techplay-1" } });
  assert.equal(clicks, 1);
});

test("TECH PLAY final action rechecks the complete binding after effect wait setup", async () => {
  const fixture = makeTechPlayOperationFixture({ events: [] });
  fixture.setHref("https://techplay.jp/event/join/999190/confirm");
  let ticketReads = 0;
  const originalCandidate = fixture.candidate;
  fixture.candidate = new Proxy(originalCandidate, {
    get(target, property, receiver) {
      if (property === "ticket_id") {
        ticketReads += 1;
        const value = Reflect.get(target, property, receiver);
        if (ticketReads === 18) target.ticket_id = "98037";
        return value;
      }
      return Reflect.get(target, property, receiver);
    },
  });
  let operateCalls = 0; let readbacks = 0;
  const options = {
    techplayWorkflow: { async readProviderState() { readbacks += 1; return { status: "registered" }; } },
    operateControl: async () => { operateCalls += 1; return { status: "success" }; },
  };
  const { harness } = makeTechPlayOperationHarness(fixture, options);
  const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
  assert.deepEqual(result, { status: "failed" });
  assert.equal(originalCandidate.ticket_id, "98037");
  assert.equal(ticketReads, 21);
  assert.equal(operateCalls, 0);
  assert.equal(readbacks, 0);
  assert.equal(fixture.final.clicked, undefined);
});

test("TECH PLAY final locator rejects count-time ticket drift and retargeted CTA swaps", async () => {
  for (const [name, mutate] of [
    ["ticket-drift", (fixture) => { fixture.candidate.ticket_id = "98037"; }],
    ["label-drift", (fixture) => { fixture.final.innerText = "申し込みを確定"; }],
    ["type-drift", (fixture) => { fixture.final.type = "submit"; }],
    ["hidden-drift", (fixture) => { fixture.final.hidden = true; }],
    ["disabled-drift", (fixture) => { fixture.final.disabled = true; }],
    ["token-drift", (fixture) => { fixture.final.dataset.lmConnectorControl = "techplay_final_stale"; }],
    ["id-drift", (fixture) => { fixture.final.id = "confirm-rebound"; }],
    ["element-swap", (fixture, decoy) => { fixture.final.dataset.lmConnectorControl = "techplay_final_stale"; decoy.dataset.lmConnectorControl = "techplay_final_999190"; }],
  ]) {
    const fixture = makeTechPlayOperationFixture({ events: [] }); fixture.setHref("https://techplay.jp/event/join/999190/confirm");
    const decoy = makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "confirm-decoy", innerText: "申し込みを確定する" });
    if (name === "element-swap") fixture.confirmElements.push(decoy);
    const originalLocator = fixture.page.locator.bind(fixture.page);
    fixture.page.locator = (selector) => {
      const locator = originalLocator(selector);
      if (!String(selector).includes('data-lm-connector-control="techplay_final_999190"')) return locator;
      return {
        async elementHandles() { return locator.elementHandles(); },
        async count() { mutate(fixture, decoy); return locator.count(); },
        async click() { return locator.click(); },
      };
    };
    fixture.options.techplayWorkflow = { async readProviderState() { return { status: "registered" }; } };
    const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
    const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
    assert.deepEqual(result, { status: "failed" }, name);
    assert.equal(fixture.options.finalClicks || 0, 0, `${name}-no-click`);
    assert.equal(fixture.final.clicked, undefined, `${name}-original-no-click`);
    assert.equal(decoy.clicked, undefined, `${name}-decoy-no-click`);
  }
});

test("TECH PLAY final ticket drift at effect-wait read start fails before operate and history", async () => {
  const makeDriftingWorkflow = (candidate) => {
    let reads = 0;
    let drifted = false;
    const workflow = {};
    Object.defineProperty(workflow, "readProviderState", {
      configurable: true,
      get() {
        reads += 1;
        if (reads === 3) { candidate.ticket_id = "98037"; drifted = true; }
        return async () => ({ status: "registered" });
      },
    });
    return { workflow, reads: () => reads, drifted: () => drifted };
  };

  const direct = makeTechPlayOperationFixture({ events: [] }); direct.setHref("https://techplay.jp/event/join/999190/confirm");
  const directWorkflow = makeDriftingWorkflow(direct.candidate); let directOperateCalls = 0;
  const directOptions = {
    techplayWorkflow: directWorkflow.workflow,
    operateControl: async () => { directOperateCalls += 1; return { status: "success" }; },
  };
  const { harness: directHarness } = makeTechPlayOperationHarness(direct, directOptions);
  const directResult = await directHarness.performAction({ provider: "techplay", candidate: direct.candidate, page: direct.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
  assert.equal(directWorkflow.reads(), 3); assert.equal(directWorkflow.drifted(), true);
  assert.deepEqual(directResult, { status: "failed" }); assert.equal(Object.hasOwn(directResult, "attempted"), false);
  assert.equal(directOperateCalls, 0); assert.equal(direct.options.finalClicks || 0, 0); assert.equal(direct.final.clicked, undefined);

  const fallback = makeTechPlayOperationFixture({ navigateOnReview: true, events: [] });
  const fallbackWorkflow = makeDriftingWorkflow(fallback.candidate); let fallbackFinalOperateCalls = 0;
  const fallbackBase = makeTechPlayPrivateResolverFixture();
  const fallbackOptions = {
    techplayWorkflow: fallbackWorkflow.workflow,
    resolveValue: async (input) => input.control.question === "職種" ? input.control.label === "職種1" : fallbackBase.resolver(input),
    operateControl: async (input) => {
      if (input.action.control === "techplay_final_999190") fallbackFinalOperateCalls += 1;
      return operatePageControl(input);
    },
  };
  const { harness: fallbackHarness } = makeTechPlayOperationHarness(fallback, fallbackOptions);
  const fallbackResult = await fallbackHarness.runFallback({ provider: "techplay", candidate: fallback.candidate, page: fallback.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY-FINAL-DRIFT", maxSteps: 15, expectedState: "registered_or_pending" });
  assert.equal(fallbackWorkflow.reads(), 3); assert.equal(fallbackWorkflow.drifted(), true);
  assert.equal(fallbackResult.status, "failed"); assert.equal(fallbackResult.safe_reason, "final_blocked");
  assert.equal(fallbackResult.repaired_actions.length, 14);
  assert.equal(fallbackResult.repaired_actions.some((action) => action.control === "techplay_final_999190"), false);
  assert.equal(fallbackFinalOperateCalls, 0); assert.equal(fallback.options.finalClicks || 0, 0); assert.equal(fallback.final.clicked, undefined);
});

test("TECH PLAY fallback performs 13 inputs, review, and one final click only at maxSteps 15", async () => {
  const fixture = makeTechPlayOperationFixture({ navigateOnReview: true, events: [] }); const options = fixture.options;
  const base = makeTechPlayPrivateResolverFixture();
  options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  options.techplayWorkflow = { async readProviderState() { options.readbacks = (options.readbacks || 0) + 1; return options.finalRegistered ? { status: "registered", receipt_id: "techplay-1" } : { status: "absent" }; } };
  const { harness } = makeTechPlayOperationHarness(fixture, options);
  const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAYFINAL15", maxSteps: 15, expectedState: "registered_or_pending" });
  assert.equal(result.status, "completed"); assert.equal(result.provider_state.status, "registered"); assert.equal(result.repaired_actions.length, 15);
  assert.equal(options.proposeCalls || 0, 0); assert.equal(options.operateCalls, 15); assert.equal(options.reviewClicks, 1); assert.equal(options.finalClicks, 1); assert.ok(options.readbacks >= 1);
  assert.equal(result.repaired_actions.at(-1).control, "techplay_final_999190");
});

test("TECH PLAY fallback keeps a confirm page final_blocked at maxSteps 14", async () => {
  const fixture = makeTechPlayOperationFixture({ events: [] }); fixture.setHref("https://techplay.jp/event/join/999190/confirm");
  fixture.options.techplayWorkflow = { async readProviderState() { return { status: "registered" }; } };
  const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
  const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAYFINAL14", maxSteps: 14, expectedState: "registered_or_pending" });
  assert.deepEqual(result, { status: "failed", safe_reason: "final_blocked", repaired_actions: [] }); assert.equal(fixture.final.clicked, undefined);
});

test("TECH PLAY attempted final effect records one action, accepts registered throw, and never retries unknown", async () => {
  const throwing = makeTechPlayOperationFixture({ events: [] }); throwing.setHref("https://techplay.jp/event/join/999190/confirm");
  const throwingOptions = throwing.options; throwingOptions.throwOnFinal = true;
  throwingOptions.techplayWorkflow = { async readProviderState() { return throwingOptions.finalRegistered ? { status: "registered" } : { status: "absent" }; } };
  const { harness: throwingHarness } = makeTechPlayOperationHarness(throwing, throwingOptions);
  const registered = await throwingHarness.performAction({ provider: "techplay", candidate: throwing.candidate, page: throwing.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
  assert.deepEqual(registered, { status: "success", provider_state: { status: "registered" } }); assert.equal(throwingOptions.finalClicks, 1);

  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    const unknown = makeTechPlayOperationFixture({ events: [] }); unknown.setHref("https://techplay.jp/event/join/999190/confirm"); const unknownOptions = unknown.options; let attempts = 0; let reads = 0;
    unknownOptions.techplayWorkflow = { async readProviderState() { reads += 1; return { status: "absent" }; } };
    unknownOptions.operateControl = async () => { attempts += 1; return { status: "failed", attempted: true }; };
    const { harness: unknownHarness } = makeTechPlayOperationHarness(unknown, unknownOptions);
    const resultPromise = unknownHarness.performAction({ provider: "techplay", candidate: unknown.candidate, page: unknown.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
    for (let attempt = 0; attempt < 100 && reads === 0; attempt += 1) await Promise.resolve();
    mock.timers.tick(30_001);
    const result = await resultPromise;
    assert.deepEqual(result, { status: "failed", safe_reason: "effect_unknown" }); assert.equal(attempts, 1); assert.ok(reads >= 1);
  } finally { mock.timers.reset(); }
});

test("TECH PLAY final readback accepts only registered across every non-success boundary", async () => {
  const cases = [
    ["pending", async () => ({ status: "pending" })],
    ["absent", async () => ({ status: "absent" })],
    ["unavailable", async () => ({ status: "unavailable" })],
    ["malformed", async () => ({ status: 42 })],
    ["rejected", async () => { throw new Error("provider readback rejected"); }],
    ["timeout", async () => new Promise(() => {})],
  ];
  mock.timers.enable({ apis: ["Date", "setTimeout"] });
  try {
    for (const [name, reader] of cases) {
      const fixture = makeTechPlayOperationFixture({ events: [] }); fixture.setHref("https://techplay.jp/event/join/999190/confirm");
      let attempts = 0; let reads = 0;
      const options = {
        techplayWorkflow: { async readProviderState() { reads += 1; return reader(); } },
        operateControl: async (input) => { attempts += 1; return operatePageControl(input); },
      };
      const { harness } = makeTechPlayOperationHarness(fixture, options);
      const resultPromise = harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
      for (let tick = 0; tick < 100 && reads === 0; tick += 1) await Promise.resolve();
      assert.ok(reads >= 1, `${name}-readback-started`);
      mock.timers.tick(30_001);
      for (let tick = 0; tick < 100; tick += 1) await Promise.resolve();
      assert.deepEqual(await resultPromise, { status: "failed", safe_reason: "effect_unknown" }, name);
      assert.equal(attempts, 1, `${name}-one-operation`);
      assert.equal(fixture.options.finalClicks, 1, `${name}-one-click`);
      assert.equal(fixture.final.clicked, true, `${name}-clicked`);
    }
  } finally { mock.timers.reset(); }
});

test("TECH PLAY final action rejects pre-click URL, token, state, duplicate, and locator drift", async () => {
  const cases = [
    ["wrong-url", (fixture) => fixture.setHref("https://techplay.jp/event/join/999191/confirm")],
    ["wrong-label", (fixture) => { fixture.final.innerText = "申し込みを確定"; }],
    ["duplicate", (fixture) => fixture.confirmElements.push(makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "decoy", innerText: "申し込みを確定する" }))],
    ["missing-locator", (fixture) => { fixture.options.locatorCount = 0; }],
    ["wrong-state", (fixture) => { fixture.options.inspectControls = async () => [{ control: "techplay_final_999190", kind: "button", label: "申し込みを確定する", required: true, completed: false, submittable: true }]; }],
  ];
  for (const [name, mutate] of cases) {
    const fixture = makeTechPlayOperationFixture({ events: [] }); mutate(fixture); fixture.options.techplayWorkflow = { async readProviderState() { return { status: "registered" }; } };
    const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
    const result = await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action: { purpose: "submit", method: "ax_click", control: "techplay_final_999190" } });
    assert.deepEqual(result, { status: "failed" }, name); assert.equal(fixture.final.clicked, undefined, `${name}-no-click`);
  }
  const fixture = makeTechPlayOperationFixture({ events: [] }); fixture.options.techplayWorkflow = { async readProviderState() { return { status: "registered" }; } }; const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
  for (const action of [{ purpose: "fill", method: "ax_click", control: "techplay_final_999190" }, { purpose: "submit", method: "ax_fill", control: "techplay_final_999190" }, { purpose: "submit", method: "ax_click", control: "techplay_final_999191" }]) {
    assert.deepEqual(await harness.performAction({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, action }), { status: "failed" });
  }
  assert.equal(fixture.final.clicked, undefined);
});

test("TECH PLAY fallback selects scalar/radio/opt-out inputs and blocks when review navigation is unavailable", async () => {
  const fixture = makeTechPlayOperationFixture(); const options = fixture.options;
  const base = makeTechPlayPrivateResolverFixture();
  options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  const { harness } = makeTechPlayOperationHarness(fixture, options);
  const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAYINPUT1", maxSteps: 20, expectedState: "registered_or_pending" });
  assert.equal(options.proposeCalls || 0, 0); assert.equal(options.operateCalls || 0, 14); assert.equal(options.reviewClicks, 1);
  assert.equal(result.status, "failed"); assert.equal(result.safe_reason, "effect_unknown"); assert.equal(result.repaired_actions.length, 14);
  assert.equal(fixture.answers.every((answer) => Boolean(String(answer.value).trim())), true); assert.equal(fixture.radioAnswers[0].checked, true); assert.equal(fixture.radioAnswers[3].checked, true); assert.equal(fixture.optouts.every((optout) => optout.ariaChecked === false), true);
  assert.equal(fixture.elements.filter((element) => element.innerText === "同意して内容を確認する").some((element) => element.clicked), true);
});

test("TECH PLAY fallback navigates once to same-event confirm and blocks the final CTA", async () => {
  const fixture = makeTechPlayOperationFixture({ navigateOnReview: true, confirmEmptyAttempts: 1, events: [] }); const options = fixture.options;
  const base = makeTechPlayPrivateResolverFixture();
  options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  const { harness } = makeTechPlayOperationHarness(fixture, options);
  const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAYREVIEW1", maxSteps: 14, expectedState: "registered_or_pending" });
  assert.equal(result.status, "failed"); assert.equal(result.safe_reason, "final_blocked"); assert.equal(result.repaired_actions.length, 14);
  assert.equal(options.proposeCalls || 0, 0); assert.equal(options.operateCalls, 14); assert.equal(options.reviewClicks, 1);
  assert.deepEqual(options.events.slice(0, 2), ["arm", "click"]);
  assert.equal(options.confirmInspectCalls, 2);
  assert.deepEqual(await inspectTechPlayConfirmFixture({ ...fixture, elements: [fixture.final] }), [{ control: "techplay_final_999190", kind: "button", label: "申し込みを確定する", required: false, completed: false, submittable: true }]);
  assert.equal(fixture.final.clicked, undefined);
});

test("TECH PLAY review rechecks the complete candidate binding before clicking", async () => {
  const fixture = makeTechPlayOperationFixture({ navigateOnReview: true, events: [] }); const base = makeTechPlayPrivateResolverFixture(); let completeObservations = 0;
  fixture.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  fixture.options.inspectControls = async (input) => {
    const controls = await inspectPageControls(input);
    if (input.provider === "techplay" && input.page.url().endsWith("/event/join/999190") && fixture.answers.every((answer) => Boolean(String(answer.value).trim())) && fixture.radioAnswers[0].checked && fixture.radioAnswers[3].checked && fixture.optouts.every((optout) => optout.ariaChecked === false) && ++completeObservations === 3) fixture.candidate.ticket_id = "98037";
    return controls;
  };
  const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
  const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY-TICKET-DRIFT", maxSteps: 14, expectedState: "registered_or_pending" });
  assert.equal(completeObservations, 3);
  assert.equal(result.safe_reason, "review_blocked"); assert.equal(result.repaired_actions.length, 13); assert.equal(fixture.options.reviewClicks || 0, 0); assert.equal(fixture.options.confirmInspectCalls || 0, 0);
});

test("TECH PLAY review navigation accepts only one exact same-event URL and records one mutation", async () => {
  const cases = [
    ["timeout", { navigateOnReview: true, waitTimeout: true }],
    ["reject", { navigateOnReview: true, waitReject: true }],
    ["wrong-event", { navigateOnReview: true, reviewHref: "https://techplay.jp/event/join/999191/confirm" }],
    ["query", { navigateOnReview: true, reviewHref: "https://techplay.jp/event/join/999190/confirm?x=1" }],
    ["fragment", { navigateOnReview: true, reviewHref: "https://techplay.jp/event/join/999190/confirm#x" }],
  ];
  for (const [name, options] of cases) {
    const fixture = makeTechPlayOperationFixture({ ...options, events: [] }); const base = makeTechPlayPrivateResolverFixture();
    fixture.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
    const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
    const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/TECHPLAY-${name}`, maxSteps: 20, expectedState: "registered_or_pending" });
    assert.equal(result.status, "failed", name); assert.equal(result.safe_reason, "effect_unknown", name); assert.equal(result.repaired_actions.length, 14, name);
    assert.equal(fixture.options.waitCalls, 1, `${name}-wait-once`); assert.equal(fixture.options.reviewClicks, 1, `${name}-click-once`); assert.equal(fixture.final.clicked, undefined, `${name}-final-blocked`);
  }
  const throwing = makeTechPlayOperationFixture({ navigateOnReview: true, throwOnReview: true, events: [] }); const base = makeTechPlayPrivateResolverFixture();
  throwing.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  const { harness } = makeTechPlayOperationHarness(throwing, throwing.options);
  const result = await harness.runFallback({ provider: "techplay", candidate: throwing.candidate, page: throwing.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY-THROW", maxSteps: 20, expectedState: "registered_or_pending" });
  assert.equal(result.safe_reason, "final_blocked"); assert.equal(result.repaired_actions.length, 14); assert.equal(throwing.options.reviewClicks, 1); assert.equal(throwing.final.clicked, undefined);
});

test("TECH PLAY confirm hydration retry is read-only and bounded", async () => {
  const transient = makeTechPlayOperationFixture({ navigateOnReview: true, confirmEmptyAttempts: 20, events: [] }); const base = makeTechPlayPrivateResolverFixture();
  transient.options.sleep = async () => { transient.options.sleepCalls = (transient.options.sleepCalls || 0) + 1; };
  transient.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  const { harness } = makeTechPlayOperationHarness(transient, transient.options);
  const never = await harness.runFallback({ provider: "techplay", candidate: transient.candidate, page: transient.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY-NEVER-STABLE", maxSteps: 14, expectedState: "registered_or_pending" });
  assert.equal(never.safe_reason, "effect_unknown"); assert.equal(never.repaired_actions.length, 14); assert.equal(transient.options.reviewClicks, 1); assert.equal(transient.options.sleepCalls, 19); assert.equal(transient.options.confirmInspectCalls, 20); assert.equal(transient.final.clicked, undefined);

  for (const [name, options] of [["throw", { navigateOnReview: true, throwOnConfirmInspect: true }], ["url-drift", { navigateOnReview: true, driftConfirmHref: "https://techplay.jp/event/join/999191/confirm" }], ["ticket-drift", { navigateOnReview: true, driftConfirmTicket: true }]]) {
    const fixture = makeTechPlayOperationFixture({ ...options, events: [] }); const values = makeTechPlayPrivateResolverFixture(); fixture.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : values.resolver(input);
    fixture.options.sleep = async () => { fixture.options.sleepCalls = (fixture.options.sleepCalls || 0) + 1; };
    const { harness: caseHarness } = makeTechPlayOperationHarness(fixture, fixture.options);
    const result = await caseHarness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/TECHPLAY-CONFIRM-${name}`, maxSteps: 14, expectedState: "registered_or_pending" });
    assert.equal(result.safe_reason, "effect_unknown", name); assert.equal(result.repaired_actions.length, 14, name); assert.equal(fixture.options.reviewClicks, 1, `${name}-review-once`); assert.equal(fixture.options.sleepCalls || 0, 0, `${name}-no-retry`); assert.equal(fixture.final.clicked, undefined, `${name}-final-zero`);
  }
});

test("TECH PLAY review blocks pre-click drift or locator ambiguity without recording a false action", async () => {
  for (const [name, options] of [["page-drift", { navigateOnReview: true, driftOnReview: true }], ["duplicate-locator", { navigateOnReview: true, reviewLocatorCount: 2 }], ["missing-locator", { navigateOnReview: true, reviewLocatorCount: 0 }]]) {
    const fixture = makeTechPlayOperationFixture({ ...options, events: [] }); const base = makeTechPlayPrivateResolverFixture();
    fixture.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
    const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
    const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/TECHPLAY-PRE-${name}`, maxSteps: 20, expectedState: "registered_or_pending" });
    assert.equal(result.safe_reason, "review_blocked", name); assert.equal(result.repaired_actions.length, 13, name); assert.equal(fixture.options.reviewClicks || 0, 0, `${name}-click-zero`);
  }
});

test("TECH PLAY final inspector drift blocks without a final click", async () => {
  for (const [name, mutate] of [["residual", (fixture) => fixture.confirmElements.push(makeTechPlayElement({ tagName: "BUTTON", type: "button", id: "back", innerText: "戻る" }))], ["wrong-type", (fixture) => { fixture.final.type = "submit"; }]]) {
    const fixture = makeTechPlayOperationFixture({ navigateOnReview: true, events: [] }); mutate(fixture); const base = makeTechPlayPrivateResolverFixture();
    fixture.options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
    const { harness } = makeTechPlayOperationHarness(fixture, fixture.options);
    const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/TECHPLAY-FINAL-${name}`, maxSteps: 20, expectedState: "registered_or_pending" });
    assert.equal(result.safe_reason, "effect_unknown", name); assert.equal(result.repaired_actions.length, 14, name); assert.equal(fixture.options.reviewClicks, 1, `${name}-review-once`); assert.equal(fixture.final.clicked, undefined, `${name}-final-zero`);
  }
});

test("TECH PLAY fallback stops before DOM action for zero or multiple approved radio options", async () => {
  for (const [name, resolveValue] of [["zero", async (input) => input.control.kind === "radio" ? null : "NameFixture"], ["multiple", async (input) => input.control.kind === "radio" ? true : "NameFixture"]]) {
    const fixture = makeTechPlayOperationFixture(); const options = { resolveValue }; const { harness } = makeTechPlayOperationHarness(fixture, options);
    const result = await harness.runFallback({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, pageWebsocket: `ws://127.0.0.1:9222/devtools/page/TECHPLAY-${name}`, maxSteps: 20, expectedState: "registered_or_pending" });
    assert.equal(options.proposeCalls || 0, 0, `${name}-proposer`); assert.equal(options.operateCalls || 0, 4, `${name}-operate`); assert.equal(result.status, "failed", name); assert.equal(result.safe_reason, "agent_action_failed", name);
  }
});

test("TECH PLAY fallback preserves exact page scope, candidate binding, and bounded private-free action history", async () => {
  const fixture = makeTechPlayOperationFixture(); const options = {};
  const base = makeTechPlayPrivateResolverFixture();
  options.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : base.resolver(input);
  const { harness } = makeTechPlayOperationHarness(fixture, options);
  const scoped = { provider: "techplay", candidate: fixture.candidate, page: fixture.page, maxSteps: 10, expectedState: "registered_or_pending" };
  for (const pageWebsocket of [
    "ws://127.0.0.1:9222/devtools/browser/TECHPLAY_SCOPE1",
    "ws://127.0.0.1:9223/devtools/page/TECHPLAY_SCOPE1",
    "ws://127.0.0.1:9222/devtools/page/",
    "ws://user:secret@127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE1",
    "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE1?query=1",
  ]) {
    await assert.rejects(() => harness.runFallback({ ...scoped, pageWebsocket }), /Connector production Browser Harness invalid/);
  }
  const { maxSteps, ...missingMaxSteps } = scoped;
  await assert.rejects(() => harness.runFallback({ ...missingMaxSteps, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE1" }), /Connector production Browser Harness invalid/);
  const { expectedState, ...missingExpectedState } = scoped;
  await assert.rejects(() => harness.runFallback({ ...missingExpectedState, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE1" }), /Connector production Browser Harness invalid/);
  for (const invalidMaxSteps of [0, 21]) {
    await assert.rejects(() => harness.runFallback({ ...scoped, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE1", maxSteps: invalidMaxSteps }), /Connector production Browser Harness invalid/);
  }
  await assert.rejects(() => harness.runFallback({ ...scoped, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE1", candidate: { ...fixture.candidate, event_ref: "techplay-event://event/999191" } }), /Connector production Browser Harness invalid/);

  const ticketDrift = makeTechPlayOperationFixture(); const ticketOptions = {};
  const ticketBase = makeTechPlayPrivateResolverFixture(); ticketOptions.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : ticketBase.resolver(input);
  const { harness: ticketHarness } = makeTechPlayOperationHarness(ticketDrift, ticketOptions);
  const ticketResult = await ticketHarness.runFallback({ ...scoped, candidate: { ...ticketDrift.candidate, ticket_id: "98037" }, page: ticketDrift.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE2" });
  assert.deepEqual(ticketResult, { status: "failed", safe_reason: "agent_action_failed", repaired_actions: [] });
  assert.equal(ticketOptions.operateCalls || 0, 0);

  const limited = makeTechPlayOperationFixture(); const limitedOptions = {};
  const limitedBase = makeTechPlayPrivateResolverFixture(); limitedOptions.resolveValue = async (input) => input.control.question === "職種" ? (input.control.label === "職種1" ? true : null) : limitedBase.resolver(input);
  const { harness: limitedHarness } = makeTechPlayOperationHarness(limited, limitedOptions);
  const limitedResult = await limitedHarness.runFallback({ provider: "techplay", candidate: limited.candidate, page: limited.page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAY_SCOPE3", maxSteps: 10, expectedState: "registered_or_pending" });
  assert.equal(limitedResult.safe_reason, "agent_step_limit"); assert.equal(limitedResult.repaired_actions.length, 10); assert.equal(limitedOptions.proposeCalls || 0, 0); assert.equal(limitedOptions.operateCalls || 0, 10);
  assert.ok(limitedResult.repaired_actions.every((action) => typeof action.control === "string" && typeof action.method === "string" && !Object.hasOwn(action, "value")));
  assert.doesNotMatch(JSON.stringify(limitedResult.repaired_actions), /NameFixture|email\.fixture@example\.test|CompanyFixture|2002-08-12/);
});
