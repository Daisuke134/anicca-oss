"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { createBrowserHarnessAdapter } = require("./connector-browser-harness-adapter.js");

const PAGE_WS = "ws://127.0.0.1:9222/devtools/page/TARGETOWNED123";

test("bounded fallback discovers and executes only focused actions on the exact claimed page", async () => {
  const calls = [];
  const page = Object.freeze({ page_id: "owned-page-1" });
  const proposed = [
    { purpose: "fill", method: "ax_fill", control: "required_text" },
    { purpose: "submit", method: "ax_click", control: "registration_submit" },
  ];
  const adapter = createBrowserHarnessAdapter({
    async observePage(input) {
      assert.equal(input.page, page);
      calls.push(["observe", input.target_id]);
      return Object.freeze({ state: "registration_form", controls: ["required_text", "registration_submit"] });
    },
    async proposeAction(input) {
      assert.equal(input.page_websocket, PAGE_WS);
      assert.equal(Object.hasOwn(input, "browser"), false);
      assert.equal(Object.hasOwn(input, "browser_endpoint"), false);
      calls.push(["propose", input.step]);
      return proposed.shift() || Object.freeze({ purpose: "readback", method: "parent_readback", control: "registration_state" });
    },
    async performAction(input) {
      assert.equal(input.page, page);
      calls.push(["perform", input.action.purpose, input.action.method]);
      return Object.freeze({ status: "success" });
    },
    async readExpectedState(input) {
      calls.push(["readback", input.expected_state]);
      return calls.filter(([name]) => name === "perform").length === 2
        ? Object.freeze({ status: "registered" })
        : Object.freeze({ status: "absent" });
    },
  });

  const result = await adapter.runFallback({
    provider: "luma",
    page,
    pageWebsocket: PAGE_WS,
    expectedState: "registered_or_pending",
    maxSteps: 10,
  });

  assert.equal(result.status, "completed");
  assert.equal(result.provider_state.status, "registered");
  assert.deepEqual(result.repaired_actions, [
    { purpose: "fill", method: "ax_fill", control: "required_text" },
    { purpose: "submit", method: "ax_click", control: "registration_submit" },
  ]);
  assert.deepEqual(calls.filter(([name]) => name === "perform").length, 2);
});

test("bounded specialist contains observe failures with a stage-specific safe reason", async () => {
  const adapter = createBrowserHarnessAdapter({
    async observePage() { throw new Error("observe dependency failed"); },
    async proposeAction() { throw new Error("proposal must not run"); },
    async performAction() { throw new Error("action must not run"); },
    async readExpectedState() { throw new Error("readback must not run"); },
  });

  const result = await adapter.runFallback({
    provider: "luma", page: {}, pageWebsocket: PAGE_WS,
    expectedState: "registered_or_pending", maxSteps: 1,
  });

  assert.deepEqual(result, {
    status: "failed",
    safe_reason: "agent_observe_failed",
    repaired_actions: [],
  });
});

test("bounded specialist contains a post-dispatch exception as effect_unknown", async () => {
  let dispatches = 0;
  const adapter = createBrowserHarnessAdapter({
    async observePage() { return Object.freeze({ state: "form", controls: ["submit"] }); },
    async proposeAction() { return Object.freeze({ purpose: "submit", method: "ax_click", control: "submit" }); },
    async performAction(input) {
      input.beforeDispatch();
      dispatches += 1;
      throw new Error("post-dispatch dependency failed");
    },
    async readExpectedState() { throw new Error("readback must not run after failed dispatch"); },
  });

  const result = await adapter.runFallback({
    provider: "luma", page: {}, pageWebsocket: PAGE_WS,
    expectedState: "registered_or_pending", maxSteps: 2,
  });

  assert.deepEqual(result, {
    status: "failed",
    safe_reason: "effect_unknown",
    repaired_actions: [],
  });
  assert.equal(dispatches, 1);
});

test("adapter rejects browser-wide, Gig, credential-bearing, and non-page websocket endpoints", () => {
  const adapter = createBrowserHarnessAdapter({
    observePage() {}, proposeAction() {}, performAction() {}, readExpectedState() {},
  });
  const base = {
    provider: "luma",
    page: {},
    expectedState: "registered_or_pending",
    maxSteps: 10,
  };

  for (const pageWebsocket of [
    "ws://127.0.0.1:9222/devtools/browser/abcdef",
    "ws://127.0.0.1:9223/devtools/page/TARGETOWNED123",
    "ws://user:secret@127.0.0.1:9222/devtools/page/TARGETOWNED123",
    "ws://127.0.0.1:9222/devtools/page/",
  ]) {
    assert.throws(() => adapter.runFallback({ ...base, pageWebsocket }), /Browser Harness adapter invalid/);
  }
});

test("adapter blocks browser lifecycle actions proposed by an agent", async () => {
  for (const method of ["browser_close", "target_create", "target_close", "new_tab"]) {
    const adapter = createBrowserHarnessAdapter({
      async observePage() { return Object.freeze({ state: "form", controls: [] }); },
      async proposeAction() {
        return Object.freeze({ purpose: "submit", method, control: "registration_submit" });
      },
      async performAction() { throw new Error("blocked action must not execute"); },
      async readExpectedState() { return Object.freeze({ status: "absent" }); },
    });
    const result = await adapter.runFallback({
      provider: "luma", page: {}, pageWebsocket: PAGE_WS,
      expectedState: "registered_or_pending", maxSteps: 10,
    });
    assert.equal(result.status, "failed");
    assert.equal(result.safe_reason, "unsafe_agent_action");
    assert.deepEqual(result.repaired_actions, []);
  }
});

test("adapter stops after ten unsuccessful steps without changing the page owner", async () => {
  let performed = 0;
  const adapter = createBrowserHarnessAdapter({
    async observePage() { return Object.freeze({ state: "form", controls: ["next"] }); },
    async proposeAction() {
      return Object.freeze({ purpose: "observe", method: "ax_inspect", control: "next" });
    },
    async performAction() {
      performed += 1;
      return Object.freeze({ status: "success" });
    },
    async readExpectedState() { return Object.freeze({ status: "absent" }); },
  });

  const result = await adapter.runFallback({
    provider: "connpass", page: {}, pageWebsocket: PAGE_WS,
    expectedState: "registered_or_pending", maxSteps: 10,
  });

  assert.equal(result.status, "failed");
  assert.equal(result.safe_reason, "agent_step_limit");
  assert.equal(performed, 10);
  assert.equal(result.repaired_actions.length, 10);
});

test("bounded specialist supports application readback and heartbeats every model step", async () => {
  const calls = [];
  const adapter = createBrowserHarnessAdapter({
    async heartbeat(input) {
      calls.push(["heartbeat", input.step, input.expected_state]);
    },
    async observePage(input) {
      assert.equal(input.signal.aborted, false);
      return Object.freeze({ state: "application_form", controls: ["proposal"] });
    },
    async proposeAction(input) {
      assert.equal(input.expected_state, "application_present");
      assert.equal(input.signal.aborted, false);
      return Object.freeze({ purpose: "submit", method: "ax_click", control: "proposal_submit" });
    },
    async performAction(input) {
      assert.equal(input.signal.aborted, false);
      return Object.freeze({ status: "success" });
    },
    async readExpectedState(input) {
      assert.equal(input.signal.aborted, false);
      return Object.freeze({ status: "present" });
    },
    isCompletedState(state, expectedState) {
      calls.push(["complete", state.status, expectedState]);
      return expectedState === "application_present" && state.status === "present";
    },
  });

  const result = await adapter.runFallback({
    provider: "lancers",
    page: {},
    pageWebsocket: PAGE_WS,
    expectedState: "application_present",
    maxSteps: 3,
    maxDurationMs: 1_000,
  });

  assert.equal(result.status, "completed");
  assert.deepEqual(calls, [
    ["heartbeat", 1, "application_present"],
    ["complete", "present", "application_present"],
  ]);
});

test("bounded specialist stops before work when its parent is cancelled", async () => {
  const controller = new AbortController();
  controller.abort();
  const adapter = createBrowserHarnessAdapter({
    async heartbeat() { throw new Error("cancelled specialist must not heartbeat"); },
    async observePage() { throw new Error("cancelled specialist must not observe"); },
    async proposeAction() { throw new Error("cancelled specialist must not decide"); },
    async performAction() { throw new Error("cancelled specialist must not act"); },
    async readExpectedState() { throw new Error("cancelled specialist must not read back"); },
  });

  const result = await adapter.runFallback({
    provider: "lancers", page: {}, pageWebsocket: PAGE_WS,
    expectedState: "application_present", maxSteps: 3, maxDurationMs: 1_000,
    signal: controller.signal,
  });

  assert.deepEqual(result, {
    status: "failed",
    safe_reason: "cancelled",
    repaired_actions: [],
  });
});

test("bounded specialist deadline aborts an in-flight dependency", async () => {
  let observedSignal;
  const adapter = createBrowserHarnessAdapter({
    async observePage(input) {
      observedSignal = input.signal;
      return new Promise(() => {});
    },
    async proposeAction() { throw new Error("deadline must stop before decision"); },
    async performAction() { throw new Error("deadline must stop before action"); },
    async readExpectedState() { throw new Error("deadline must stop before readback"); },
  });

  const result = await adapter.runFallback({
    provider: "lancers", page: {}, pageWebsocket: PAGE_WS,
    expectedState: "application_present", maxSteps: 3, maxDurationMs: 20,
  });

  assert.equal(observedSignal.aborted, true);
  assert.deepEqual(result, {
    status: "failed",
    safe_reason: "time_limit",
    repaired_actions: [],
  });
});

test("bounded specialist marks a submit timeout after dispatch begins as effect_unknown", async () => {
  let observedSignal;
  let dispatches = 0;
  const adapter = createBrowserHarnessAdapter({
    async observePage() { return Object.freeze({ state: "form", controls: ["submit"] }); },
    async proposeAction() { return Object.freeze({ purpose: "submit", method: "ax_click", control: "submit" }); },
    async performAction(input) {
      observedSignal = input.signal;
      input.beforeDispatch();
      dispatches += 1;
      return new Promise((resolve) => setTimeout(() => resolve({ status: "failed" }), 30));
    },
    async readExpectedState() { throw new Error("readback must not run"); },
  });

  const result = await adapter.runFallback({
    provider: "luma", page: {}, pageWebsocket: PAGE_WS,
    expectedState: "registered_or_pending", maxSteps: 2, maxDurationMs: 10,
  });

  assert.deepEqual(result, { status: "failed", safe_reason: "effect_unknown", repaired_actions: [] });
  assert.equal(dispatches, 1);
  assert.equal(observedSignal.aborted, true);
});
