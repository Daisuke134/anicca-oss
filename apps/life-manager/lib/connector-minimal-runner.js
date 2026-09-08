"use strict";

const { connectorPageWebsocketTargetId } = require("./connector-browser-target-controller.js");

const PURPOSE = /^(?:navigate|observe|fill|submit|readback)$/;
const METHOD = /^[a-z][a-z0-9_]{1,63}$/;
const SAFE_REASON = /^[a-z0-9][a-z0-9_:-]{1,99}$/;
// Bounded, non-sensitive: a JS class/constructor name only, never a message,
// stack, URL, or env value.
const ERROR_CLASS = /^[A-Za-z][A-Za-z0-9]{0,63}$/;
const FALLBACK_COMPLETION_RESERVE_MS = 160_000;

function invalid() {
  throw new Error("Connector minimal runner invalid");
}

function exactInstant(value) {
  const instant = String(value || "");
  if (!Number.isFinite(Date.parse(instant)) || new Date(Date.parse(instant)).toISOString() !== instant) invalid();
  return instant;
}

function positiveInteger(value, fallback) {
  const candidate = value == null ? fallback : Number(value);
  if (!Number.isInteger(candidate) || candidate < 1) invalid();
  return candidate;
}

function dependencies(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  const required = [
    "now", "readCalendarGaps", "discoverCandidates", "runDirectAction",
    "runCachedAction", "runAgentFallback", "readProviderState", "completeEvidence",
    "saveRepairedActions", "reportWake", "recordAction",
  ];
  for (const name of required) if (typeof input[name] !== "function") invalid();
  if ((input.runTalkApplication == null) !== (input.completeTalkEvidence == null)
    || (input.runTalkApplication != null && typeof input.runTalkApplication !== "function")
    || (input.completeTalkEvidence != null && typeof input.completeTalkEvidence !== "function")) invalid();
  if (input.reportConnpassActionBoundary != null && typeof input.reportConnpassActionBoundary !== "function") invalid();
  if (
    !input.browserRail || typeof input.browserRail !== "object"
    || typeof input.browserRail.open !== "function"
    || typeof input.browserRail.navigate !== "function"
    || typeof input.browserRail.close !== "function"
  ) invalid();
  return input;
}

function config(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  const ownerToken = String(input.ownerToken || "").trim();
  if (!/^[A-Za-z0-9._-]{16,200}$/.test(ownerToken)) invalid();
  if (
    !Array.isArray(input.providers) || input.providers.length < 1
    || input.providers.length > 20
    || input.providers.some((provider) => !/^[a-z][a-z0-9_-]{1,31}$/.test(String(provider)))
  ) invalid();
  return Object.freeze({
    ownerToken,
    providers: Object.freeze(input.providers.map(String)),
    maxConsecutiveFailures: positiveInteger(input.maxConsecutiveFailures, 3),
    maxWakeMs: positiveInteger(input.maxWakeMs, 600_000),
    maxAgentSteps: positiveInteger(input.maxAgentSteps, 10),
  });
}

function verifiedOwned(value) {
  const targetId = String(value && value.target_id || "");
  const pageWebsocket = String(value && value.page_websocket || "");
  let websocketTargetId;
  try { websocketTargetId = connectorPageWebsocketTargetId(pageWebsocket); } catch { invalid(); }
  if (
    !value || typeof value !== "object" || Array.isArray(value)
    || !/^[A-Za-z0-9._-]{3,128}$/.test(String(value.session_id || ""))
    || !/^[A-Za-z0-9._-]{3,128}$/.test(targetId)
    || websocketTargetId !== targetId
    || !value.page || typeof value.page !== "object"
  ) invalid();
  return value;
}

function verifiedCandidates(value, provider) {
  if (!Array.isArray(value) || value.length > 500) invalid();
  for (const candidate of value) {
    if (
      !candidate || typeof candidate !== "object" || Array.isArray(candidate)
      || candidate.provider !== provider
      || typeof candidate.canonical_url !== "string"
      || !candidate.canonical_url.startsWith("https://")
      || typeof candidate.event_ref !== "string"
    ) invalid();
  }
  return value;
}

function registered(value) {
  return Boolean(value && ["registered", "pending"].includes(value.status));
}

function operationSafeReason(value, fallback) {
  const reason = value && typeof value === "object" && typeof value.safe_reason === "string"
    ? value.safe_reason : "";
  return SAFE_REASON.test(reason) ? reason : fallback;
}

function resolvedSubmitFailureContext(provider, value, fallback) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.status !== "failed") return null;
  return Object.freeze({
    provider,
    safe_reason: operationSafeReason(value, fallback),
  });
}

function safeDiscoveryReason(error) {
  const code = String(error && error.code || "");
  if (code === "PROVIDER_CANDIDATE_CONTRACT_FAILED") return "provider_candidate_contract_failed";
  return /^CONNPASS_(?:CALENDAR_NAVIGATION|CALENDAR_BINDINGS|CALENDAR_BINDING_VALIDATION|CALENDAR_ROWS_CONTRACT|DETAIL_NAVIGATION|DETAIL_READ|DETAIL_TITLE_INVALID|DETAIL_START_INVALID|DETAIL_END_INVALID|DETAIL_RANGE_INVALID|DETAIL_IDENTITY_MISMATCH|CANDIDATE_VALIDATION|DISCOVERY_RESULT_CONTRACT|CALENDAR_CONFLICT_CHECK|DISCOVERY_PAGE_CONTRACT|DISCOVERY_DATES|DISCOVERY_MONTHS_CONTRACT|CANDIDATE_WINDOW|DISCOVERY_AUDIT)_FAILED$/.test(code)
    ? code.toLowerCase() : "provider_discovery_failed";
}

const CONNPASS_ACTION_BOUNDARY_CODES = new Set([
  "CONNPASS_ACTION_BOUNDARY_INPUT_FAILED",
  "CONNPASS_ACTION_BOUNDARY_CANDIDATE_FAILED",
  "CONNPASS_ACTION_BOUNDARY_SEND_FAILED",
  "CONNPASS_ACTION_BOUNDARY_PROVIDER_ID_FAILED",
  "CONNPASS_ACTION_BOUNDARY_CLAIM_FAILED",
  "CONNPASS_ACTION_BOUNDARY_LEDGER_FAILED",
  "CONNPASS_ACTION_BOUNDARY_DELIVERY_UNCERTAIN",
]);

function connpassActionBoundaryFailureContext(error) {
  const code = String(error && error.code || "");
  const errorClass = safeErrorClass(error);
  return Object.freeze({
    provider: "connpass",
    safe_reason: CONNPASS_ACTION_BOUNDARY_CODES.has(code)
      ? code.toLowerCase() : "connpass_action_boundary_failed",
    ...(errorClass ? { error_class: errorClass } : {}),
  });
}

// Sibling of safeDiscoveryReason for the submit stage: connpass-browser-provider.js's
// submitConnpassOnPage / readConnpassRegistrationStateOnPage throw these precise codes
// when a specific submit-stage guard fires (tier selection, questionnaire, confirm
// control, etc). Recording the mapped reason next to the failed action row (instead of
// only the caller's generic per-method fallback) names which guard tripped without ever
// touching the private message/stack. Every code not in this set keeps the caller's
// fallback, unchanged.
const CONNPASS_SUBMIT_CODES = /^CONNPASS_(?:REGISTRATION_UNAVAILABLE|CONTROL_UNAVAILABLE|CONFIRM_UNAVAILABLE|TIER_UNAVAILABLE|QUESTIONNAIRE_REQUIRED|PAGE_UNAVAILABLE|READBACK_UNAVAILABLE|BROWSER_ACTION_FAILED|SESSION_EXPIRED)$/;

function safeSubmitReason(error, fallback) {
  if (error && error.unknownEffect === true) return "effect_unknown";
  const code = String(error && error.code || "");
  return CONNPASS_SUBMIT_CODES.test(code) ? code.toLowerCase() : fallback;
}

function submitFailureContext(provider, error, fallback) {
  const errorClass = safeErrorClass(error);
  return Object.freeze({
    provider,
    safe_reason: safeSubmitReason(error, fallback),
    ...(errorClass ? { error_class: errorClass } : {}),
  });
}

// Every other error stays private (message/stack/URL/env are never touched)
// but the class name alone is bounded and safe to keep — it is what turns an
// unrecognised stage into "some TimeoutError" instead of a silent fallback
// with no lead at all. Prefer constructor.name (works for both Error
// subclasses and, if the caller ever threw a class instance, Playwright's
// own error classes); fall back to .name only when constructor.name is
// unavailable. A value that fails the pattern is dropped, not truncated.
function safeErrorClass(error) {
  const constructorName = error && error.constructor && typeof error.constructor.name === "string"
    ? error.constructor.name : "";
  const candidate = constructorName || String((error && error.name) || "");
  return ERROR_CLASS.test(candidate) ? candidate : "";
}

// Mirrors connector-minimal-evidence.js's EVIDENCE_SAFE_CODES: reads the .code a stageError()
// attached in the delivery block (screenshot capture / calendar create / calendar readback /
// telegram message / telegram photo / bundle write) so the wake report and action-history row name
// the failing stage instead of one generic "evidence_completion_failed" for every cause.
const EVIDENCE_STAGE_CODES = new Set([
  "EVIDENCE_SCREENSHOT_CAPTURE_FAILED",
  "EVIDENCE_CALENDAR_CREATE_FAILED",
  "EVIDENCE_CALENDAR_READBACK_FAILED",
  "EVIDENCE_TELEGRAM_MESSAGE_FAILED",
  "EVIDENCE_TELEGRAM_PHOTO_FAILED",
  "EVIDENCE_BUNDLE_WRITE_FAILED",
]);

function safeEvidenceReason(error) {
  const code = String(error && error.code || "");
  return EVIDENCE_STAGE_CODES.has(code) ? code.toLowerCase() : "evidence_completion_failed";
}

async function runMinimalConnectorWake(input = {}, injected = {}) {
  const settings = config(input);
  const deps = dependencies(injected);
  const startedAt = Date.parse(exactInstant(deps.now()));
  let owned = null;
  let consecutiveFailures = 0;
  let providerDiscoveryFailed = false;
  let connpassBoundaryFailed = false;
  let sessionExpiredReason = null;
  let discoveryFailureReason = "provider_discovery_failed";
  let lastSafeReason = "provider_discovery_failed";
  let reusedBundleObserved = false;

  const elapsed = () => Date.parse(exactInstant(deps.now())) - startedAt;
  const deadlineReached = () => elapsed() >= settings.maxWakeMs;

  async function action(purpose, method, task, onFailure, onResolvedFailure) {
    if (!PURPOSE.test(purpose) || !METHOD.test(method)) invalid();
    const timestamp = exactInstant(deps.now());
    const actionStartedAt = Date.parse(timestamp);
    let resolvedEffectUnknown = false;
    try {
      const value = await task();
      const resolvedFailure = typeof onResolvedFailure === "function" ? onResolvedFailure(value) : null;
      resolvedEffectUnknown = Boolean(resolvedFailure && resolvedFailure.safe_reason === "effect_unknown");
      const hasFailureContext = Boolean(
        resolvedFailure && typeof resolvedFailure === "object" && !Array.isArray(resolvedFailure),
      );
      await deps.recordAction(Object.freeze({
        purpose,
        method,
        timestamp,
        result: hasFailureContext ? "failed" : "success",
        duration_ms: Math.max(0, Date.parse(exactInstant(deps.now())) - actionStartedAt),
        ...(hasFailureContext ? resolvedFailure : {}),
      }));
      return value;
    } catch (error) {
      if (resolvedEffectUnknown) {
        const unknownEffect = new Error("Connector action effect unknown");
        unknownEffect.unknownEffect = true;
        throw unknownEffect;
      }
      const context = typeof onFailure === "function" ? onFailure(error) : null;
      try {
        await deps.recordAction(Object.freeze({
          purpose,
          method,
          timestamp,
          result: "failed",
          duration_ms: Math.max(0, Date.parse(exactInstant(deps.now())) - actionStartedAt),
          ...(context && typeof context === "object" ? context : {}),
        }));
      } catch (auditError) {
        if (error && error.unknownEffect === true) throw error;
        throw auditError;
      }
      throw error;
    }
  }

  async function finish(status, safeReason, extra = {}) {
    const delivery = await deps.reportWake(Object.freeze({
      status,
      safe_reason: safeReason,
      consecutive_failure_count: consecutiveFailures,
    }));
    const telegramProviderId = String(delivery && delivery.telegram_provider_id || "").trim();
    if (!telegramProviderId) invalid();
    if (status === "applied_bundle") {
      return Object.freeze({
        status,
        bundle_id: String(extra.bundle_id || ""),
        telegram_provider_id: telegramProviderId,
      });
    }
    return Object.freeze({ status, safe_reason: safeReason, telegram_provider_id: telegramProviderId });
  }

  try {
    const gaps = await action("observe", "calendar_busy", () => deps.readCalendarGaps());
    if (!Array.isArray(gaps)) invalid();
    if (deadlineReached()) return finish("circuit_open", "wake_deadline");
    owned = verifiedOwned(await deps.browserRail.open(Object.freeze({ ownerToken: settings.ownerToken })));
    if (deadlineReached()) return finish("circuit_open", "wake_deadline");

    for (let providerIndex = 0; providerIndex < settings.providers.length; providerIndex += 1) {
      const provider = settings.providers[providerIndex];
      if (!["luma", "connpass"].includes(provider)
        && elapsed() > settings.maxWakeMs - FALLBACK_COMPLETION_RESERVE_MS) {
        return finish("completed_no_effect", "fallback_deferred_for_wake_budget");
      }
      let candidates;
      try {
        if (providerIndex > 0) {
          await action("navigate", "browser_rail", () => deps.browserRail.navigate(owned, "about:blank"));
          if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        }
        const discovered = await action(
          "observe", "provider_discovery", () => deps.discoverCandidates(provider, gaps, owned.page),
          (error) => {
            const errorClass = safeErrorClass(error);
            return Object.freeze({
              provider,
              safe_reason: safeDiscoveryReason(error),
              ...(errorClass ? { error_class: errorClass } : {}),
            });
          },
        );
        try { candidates = verifiedCandidates(discovered, provider); }
        catch {
          const error = new Error("Provider candidate contract failed");
          error.code = "PROVIDER_CANDIDATE_CONTRACT_FAILED";
          throw error;
        }
        if (provider === "connpass" && candidates.length > 0 && typeof deps.reportConnpassActionBoundary === "function") {
          try {
            await action(
              "submit",
              "connpass_action_boundary",
              () => deps.reportConnpassActionBoundary({ candidates }),
              connpassActionBoundaryFailureContext,
            );
            continue;
          } catch {
            connpassBoundaryFailed = true;
            lastSafeReason = "connpass_action_boundary_failed";
            continue;
          }
        }
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");
      } catch (error) {
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        providerDiscoveryFailed = true;
        discoveryFailureReason = safeDiscoveryReason(error);
        lastSafeReason = discoveryFailureReason;
        consecutiveFailures += 1;
        if (consecutiveFailures >= settings.maxConsecutiveFailures) {
          return finish("circuit_open", lastSafeReason);
        }
        consecutiveFailures = 0;
        continue;
      }
      for (const selected of candidates) {
        if (selected.auto_apply_eligible === false) continue;
        const hasTalk = typeof deps.runTalkApplication === "function"
          && selected.talk_opportunity && selected.talk_opportunity.should_create_talk_application === true
          && selected.talk_pack && typeof selected.talk_pack === "object";
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        let navigationTaskThrew = false;
        let navigationTaskError;
        try {
          await action("navigate", "browser_rail", async () => {
            try {
              return await deps.browserRail.navigate(owned, hasTalk
                ? selected.talk_opportunity.application_url : selected.canonical_url);
            } catch (error) {
              navigationTaskThrew = true;
              navigationTaskError = error;
              throw error;
            }
          });
        } catch (error) {
          if (!navigationTaskThrew || !Object.is(error, navigationTaskError)) throw error;
          if (deadlineReached()) return finish("circuit_open", "wake_deadline");
          consecutiveFailures += 1;
          lastSafeReason = "candidate_navigation_failed";
          if (consecutiveFailures >= settings.maxConsecutiveFailures) return finish("circuit_open", lastSafeReason);
          continue;
        }
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");

        if (hasTalk) {
          let talkResult;
          try {
            talkResult = await action("submit", "talk_application", () => deps.runTalkApplication({
              provider, candidate: selected, page: owned.page,
            }));
          } catch {
            talkResult = Object.freeze({ status: "failed", safe_reason: "talk_application_failed" });
          }
          if (talkResult && talkResult.status === "submitted") return finish("circuit_open", "effect_unknown");
          if (talkResult && talkResult.status === "provider_verified") {
            let talkBundle;
            try {
              talkBundle = await action("submit", "talk_evidence", () => deps.completeTalkEvidence({
                provider, candidate: selected, page: owned.page, providerState: talkResult,
              }));
            } catch { return finish("circuit_open", "evidence_completion_failed"); }
            if (!talkBundle || talkBundle.status !== "applied_bundle" || !String(talkBundle.bundle_id || "")
              || !["created", "reused"].includes(talkBundle.completion_disposition)) {
              return finish("circuit_open", "evidence_result_invalid");
            }
            if (talkBundle.completion_disposition === "created") {
              return finish("applied_bundle", "applied_bundle", talkBundle);
            }
          }
          await action("navigate", "browser_rail", () => deps.browserRail.navigate(owned, selected.canonical_url));
          if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        }

        let operation;
        let providerState = await action("readback", "provider_state", () => deps.readProviderState({
          provider,
          candidate: selected,
          page: owned.page,
          phase: "pre_submit",
        }));
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        let usedFallback = false;
        let ambiguousAgentEffect = false;
        let directFailureReason = null;
        if (!registered(providerState)) providerState = null;
        if (!providerState) {
        try {
          operation = await action(
            "submit", "provider_cache",
            () => deps.runCachedAction({ provider, candidate: selected, page: owned.page }),
            (error) => submitFailureContext(provider, error, "cached_action_failed"),
            (value) => resolvedSubmitFailureContext(provider, value, "cached_action_unverified"),
          );
        } catch (error) {
          operation = Object.freeze({
            status: "failed",
            safe_reason: error && error.unknownEffect === true ? "effect_unknown" : "cached_action_failed",
          });
        }
        if (operation && operation.status === "completed" && registered(operation.provider_state)) {
          providerState = operation.provider_state;
        }
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        if (operation && operation.status === "failed"
          && operationSafeReason(operation, "cached_action_failed") === "effect_unknown") {
          consecutiveFailures += 1;
          return finish("circuit_open", "effect_unknown");
        }

        if (!providerState) {
        try {
          operation = await action(
            "submit", "provider_direct",
            () => deps.runDirectAction({ provider, candidate: selected, page: owned.page }),
            (error) => submitFailureContext(provider, error, "direct_action_failed"),
            (value) => resolvedSubmitFailureContext(provider, value, "direct_action_unverified"),
          );
        } catch (error) {
          operation = Object.freeze({
            status: "failed",
            safe_reason: safeSubmitReason(error, "direct_action_failed"),
          });
        }
        if (deadlineReached()) return finish("circuit_open", "wake_deadline");

        if (!operation || operation.status !== "completed") {
          directFailureReason = operationSafeReason(operation, "direct_action_unverified");
          if (directFailureReason === "effect_unknown") {
            consecutiveFailures += 1;
            return finish("circuit_open", "effect_unknown");
          }
          if (provider === "peatix" && directFailureReason === "peatix_readback_unavailable") {
            consecutiveFailures += 1;
            return finish("circuit_open", "effect_unknown");
          }
          const terminalDirectNoEffect = directFailureReason === `${provider}_session_expired`
            || (provider === "connpass" && directFailureReason === "connpass_registration_unavailable")
            || (provider === "luma" && directFailureReason === "luma_required_profile_field_unavailable");
          if (!terminalDirectNoEffect) try {
            operation = await action(
              "submit", "browser_harness",
              () => deps.runAgentFallback({
                provider,
                candidate: selected,
                page: owned.page,
                pageWebsocket: owned.page_websocket,
                maxSteps: settings.maxAgentSteps,
                expectedState: "registered_or_pending",
              }),
              (error) => submitFailureContext(provider, error, "agent_action_failed"),
              (value) => resolvedSubmitFailureContext(provider, value, "agent_action_failed"),
            );
            usedFallback = operation && operation.status === "completed";
            ambiguousAgentEffect = Boolean(operation && operation.status === "failed" && operation.safe_reason === "effect_unknown");
          } catch (error) {
            operation = Object.freeze({
              status: "failed",
              safe_reason: error && error.unknownEffect === true ? "effect_unknown" : "agent_action_failed",
            });
          }
          ambiguousAgentEffect = Boolean(operation && operation.status === "failed"
            && operationSafeReason(operation, "agent_action_failed") === "effect_unknown");
          if (deadlineReached()) return finish("circuit_open", "wake_deadline");
          if (operation && operation.status === "failed" && operation.safe_reason === "auth_required") break;
        }

        if (operation && operation.status === "completed") {
          providerState = await action("readback", "provider_state", () => deps.readProviderState({
            provider,
            candidate: selected,
            page: owned.page,
            phase: "post_submit",
          }));
          if (deadlineReached()) return finish("circuit_open", "wake_deadline");
        }
        }
        }

        if (registered(providerState)) {
          if (provider === "connpass" && operation && operation.status === "completed") {
            try {
              await action("navigate", "browser_rail", () => deps.browserRail.navigate(owned, selected.canonical_url));
              if (deadlineReached()) return finish("circuit_open", "wake_deadline");
              const canonicalState = await action("readback", "provider_state", () => deps.readProviderState({
                provider, candidate: selected, page: owned.page, phase: "canonical_recovery",
              }));
              if (deadlineReached()) return finish("circuit_open", "wake_deadline");
              if (!registered(canonicalState)) throw new Error("Connpass canonical recovery unverified");
              providerState = canonicalState;
            } catch {
              if (deadlineReached()) return finish("circuit_open", "wake_deadline");
              consecutiveFailures += 1;
              return finish("circuit_open", "evidence_completion_failed");
            }
          }
          const repairedActions = usedFallback && operation && Array.isArray(operation.repaired_actions)
            ? operation.repaired_actions : [];
          if (repairedActions.length > 0) {
            if (deadlineReached()) return finish("circuit_open", "wake_deadline");
            const saved = await deps.saveRepairedActions({
              provider,
              candidate: selected,
              page: owned.page,
              providerState,
              repairedActions,
            });
            if (!saved || saved.status !== "saved") invalid();
            if (deadlineReached()) return finish("circuit_open", "wake_deadline");
          }
          if (deadlineReached()) return finish("circuit_open", "wake_deadline");
          let bundle;
          try {
            bundle = await action(
              "submit", "evidence_completion",
              () => deps.completeEvidence({
                provider,
                candidate: selected,
                page: owned.page,
                providerState,
                repairedActions,
              }),
              (error) => ({ provider, safe_reason: safeEvidenceReason(error) }),
            );
          } catch (error) {
            if (deadlineReached()) return finish("circuit_open", "wake_deadline");
            consecutiveFailures += 1;
            return finish("circuit_open", safeEvidenceReason(error));
          }
          if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)) {
            return finish("circuit_open", "evidence_result_invalid");
          }
          if (
            bundle.status !== "applied_bundle" || !String(bundle.bundle_id || "")
            || typeof bundle.completion_disposition !== "string"
            || !["created", "reused"].includes(bundle.completion_disposition)
          ) {
            return finish("circuit_open", "evidence_disposition_invalid");
          }
          if (bundle.completion_disposition === "reused") {
            reusedBundleObserved = true;
            consecutiveFailures = 0;
            continue;
          }
          return finish("applied_bundle", "applied_bundle", bundle);
        }

        lastSafeReason = directFailureReason || operationSafeReason(operation, "direct_action_unverified");
        if (lastSafeReason === `${provider}_session_expired`) {
          sessionExpiredReason = sessionExpiredReason || lastSafeReason;
          break;
        }
        const knownNoEffect = (provider === "connpass" && lastSafeReason === "connpass_registration_unavailable")
          || (provider === "luma" && ["luma_required_profile_field_unavailable", "private_value_unavailable"].includes(operationSafeReason(operation, lastSafeReason)));
        if (knownNoEffect) continue;
        consecutiveFailures += 1;
        if (ambiguousAgentEffect) return finish("circuit_open", "effect_unknown");
        if (consecutiveFailures >= settings.maxConsecutiveFailures) {
          return finish("circuit_open", lastSafeReason);
        }
      }
      consecutiveFailures = 0;
    }
    return finish("completed_no_effect", providerDiscoveryFailed
      ? discoveryFailureReason : connpassBoundaryFailed ? "connpass_action_boundary_failed"
        : reusedBundleObserved ? "existing_bundles_reused"
          : sessionExpiredReason || "providers_exhausted");
  } catch (error) {
    if (deadlineReached()) return finish("circuit_open", "wake_deadline");
    throw error;
  } finally {
    if (owned) await deps.browserRail.close(owned);
  }
}

module.exports = { runMinimalConnectorWake };
