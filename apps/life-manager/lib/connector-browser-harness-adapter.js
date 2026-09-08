"use strict";

const { connectorPageWebsocketTargetId } = require("./connector-browser-target-controller.js");
const PROVIDER = /^[a-z][a-z0-9_-]{1,31}$/;
const CONTROL = /^[a-z][a-z0-9_-]{1,63}$/;
const EXPECTED_STATE = /^[a-z][a-z0-9_]{1,63}$/;
const DEFAULT_DURATION_MS = 120_000;
const PHASE_FAILURE_REASONS = Object.freeze({
  heartbeat: "agent_heartbeat_failed",
  observe: "agent_observe_failed",
  propose: "agent_propose_failed",
  perform: "agent_perform_failed",
  readback: "agent_readback_failed",
});
const ALLOWED = new Map([
  ["observe", new Set(["ax_inspect", "dom_inspect", "parent_readback"])],
  ["fill", new Set(["ax_fill", "dom_fill", "ax_check", "ax_select", "ax_uncheck"])],
  ["submit", new Set(["ax_click", "coordinate_click", "keyboard_submit"])],
  ["readback", new Set(["parent_readback", "ax_inspect", "dom_inspect"])],
]);

function invalid() {
  throw new Error("Browser Harness adapter invalid");
}

function dependencies(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  for (const name of ["observePage", "proposeAction", "performAction", "readExpectedState"]) {
    if (typeof input[name] !== "function") invalid();
  }
  if (input.heartbeat != null && typeof input.heartbeat !== "function") invalid();
  if (input.isCompletedState != null && typeof input.isCompletedState !== "function") invalid();
  return {
    ...input,
    heartbeat: input.heartbeat || (async () => {}),
    isCompletedState: input.isCompletedState || ((value) => completedState(value)),
  };
}

function scope(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  const websocket = String(input.pageWebsocket || "");
  let targetId;
  try { targetId = connectorPageWebsocketTargetId(websocket); } catch { invalid(); }
  if (!input.page || typeof input.page !== "object") invalid();
  const provider = String(input.provider || "");
  const expectedState = String(input.expectedState || "");
  if (!PROVIDER.test(provider) || !EXPECTED_STATE.test(expectedState)) invalid();
  const maxSteps = Number(input.maxSteps);
  if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 10) invalid();
  const maxDurationMs = Number(input.maxDurationMs == null ? DEFAULT_DURATION_MS : input.maxDurationMs);
  if (!Number.isInteger(maxDurationMs) || maxDurationMs < 1 || maxDurationMs > 900_000) invalid();
  const signal = input.signal;
  if (signal != null && (
    typeof signal !== "object" || typeof signal.aborted !== "boolean"
    || typeof signal.addEventListener !== "function" || typeof signal.removeEventListener !== "function"
  )) invalid();
  return Object.freeze({
    provider,
    page: input.page,
    page_websocket: websocket,
    target_id: targetId,
    expected_state: expectedState,
    max_steps: maxSteps,
    max_duration_ms: maxDurationMs,
    signal: signal || null,
  });
}

function safeAction(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const purpose = String(input.purpose || "");
  const method = String(input.method || "");
  const control = String(input.control || "");
  if (!ALLOWED.has(purpose) || !ALLOWED.get(purpose).has(method) || !CONTROL.test(control)) return null;
  return Object.freeze({ purpose, method, control });
}

function completedState(value) {
  return Boolean(value && ["registered", "pending"].includes(value.status));
}

function createBrowserHarnessAdapter(options = {}) {
  const deps = dependencies(options);

  async function execute(bounded) {
    const repaired = [];
    const controller = new AbortController();
    let dispatchAttempted = false;
    let phase = null;
    let stopReason = "time_limit";
    const stop = () => Object.freeze({
      status: "failed",
      safe_reason: dispatchAttempted ? "effect_unknown" : stopReason,
      repaired_actions: Object.freeze([...repaired]),
    });
    const parentAbort = () => {
      stopReason = "cancelled";
      controller.abort();
    };
    if (bounded.signal) bounded.signal.addEventListener("abort", parentAbort, { once: true });
    if (bounded.signal && bounded.signal.aborted) parentAbort();
    const timer = setTimeout(() => controller.abort(), bounded.max_duration_ms);
    const call = (fn, input) => new Promise((resolve, reject) => {
      if (controller.signal.aborted) return reject(new Error("bounded specialist stopped"));
      const onAbort = () => reject(new Error("bounded specialist stopped"));
      controller.signal.addEventListener("abort", onAbort, { once: true });
      Promise.resolve().then(() => fn(Object.freeze({ ...input, signal: controller.signal })))
        .then(resolve, reject).finally(() => controller.signal.removeEventListener("abort", onAbort));
    });
    try {
      for (let step = 1; step <= bounded.max_steps; step += 1) {
        phase = "heartbeat";
        await call(deps.heartbeat, {
          provider: bounded.provider, target_id: bounded.target_id,
          expected_state: bounded.expected_state, step,
        });
        phase = "observe";
        const observation = await call(deps.observePage, {
          page: bounded.page, target_id: bounded.target_id,
        });
        phase = "propose";
        const proposed = await call(deps.proposeAction, {
          provider: bounded.provider, page_websocket: bounded.page_websocket,
          target_id: bounded.target_id, expected_state: bounded.expected_state,
          step, observation,
        });
        const action = safeAction(proposed);
        if (!action) return Object.freeze({ status: "failed", safe_reason: "unsafe_agent_action", repaired_actions: Object.freeze([...repaired]) });
        phase = "perform";
        const effect = await call(deps.performAction, {
          page: bounded.page, target_id: bounded.target_id, action,
          ...(action.purpose === "submit" ? {
            beforeDispatch: () => {
              if (!controller.signal.aborted) dispatchAttempted = true;
            },
          } : {}),
        });
        if (!effect || effect.status !== "success") return Object.freeze({
          status: "failed",
          safe_reason: dispatchAttempted ? "effect_unknown" : "agent_action_failed",
          repaired_actions: Object.freeze([...repaired]),
        });
        repaired.push(action);
        phase = "readback";
        const providerState = await call(deps.readExpectedState, {
          page: bounded.page, target_id: bounded.target_id, provider: bounded.provider,
          expected_state: bounded.expected_state,
        });
        if (deps.isCompletedState(providerState, bounded.expected_state)) {
          return Object.freeze({ status: "completed", provider_state: Object.freeze({ ...providerState }), repaired_actions: Object.freeze([...repaired]) });
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return stop();
      if (PHASE_FAILURE_REASONS[phase]) return Object.freeze({
        status: "failed",
        safe_reason: dispatchAttempted ? "effect_unknown" : PHASE_FAILURE_REASONS[phase],
        repaired_actions: Object.freeze([...repaired]),
      });
      throw error;
    } finally {
      clearTimeout(timer);
      if (bounded.signal) bounded.signal.removeEventListener("abort", parentAbort);
    }
    return Object.freeze({
      status: "failed",
      safe_reason: "agent_step_limit",
      repaired_actions: Object.freeze([...repaired]),
    });
  }

  return Object.freeze({
    runFallback(input) {
      return execute(scope(input));
    },
  });
}

module.exports = { createBrowserHarnessAdapter };
