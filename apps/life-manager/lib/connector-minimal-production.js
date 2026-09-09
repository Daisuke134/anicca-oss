"use strict";

const crypto = require("node:crypto");
const path = require("node:path");

const {
  CONNECTOR_CDP_ENDPOINT,
  createConnectorBrowserTargetController,
} = require("./connector-browser-target-controller.js");
const { createConnectorTabOwner } = require("./connector-tab-owner.js");
const { createConnectorTargetLease } = require("./connector-target-lease.js");
const { createConnectorActionCache } = require("./connector-action-cache.js");
const { createConnectorReconciliationStore } = require("./connector-reconciliation-store.js");
const { eligibleRankedCandidates, inferProviderCandidateRanking } = require("./event-preference-ranking.js");
const { inferEventTalkOpportunity, isVerifiedEventTalkOpportunity } = require("./event-talk-opportunity.js");
const { generateGroundedTalkPack } = require("./grounded-talk-pack.js");
const { readConnectorTalkFacts } = require("./connector-talk-facts.js");
const { createTalkBrowserProvider } = require("./connector-talk-browser-provider.js");
const { createTalkApplicationWorkflow } = require("./connector-talk-application-workflow.js");
const { createTalkEvidenceChain } = require("./connector-talk-evidence.js");
const { createConnpassActionTelegram } = require("./connector-connpass-action-telegram.js");
const { createMinimalEvidenceChain } = require("./connector-minimal-evidence.js");
const { createMinimalProductionOperations } = require("./connector-minimal-operations.js");
const { createLumaScriptFirstWorkflow } = require("./connector-luma-workflow.js");
const { createConnpassScriptFirstWorkflow } = require("./connector-connpass-workflow.js");
const { createConnpassApiClient } = require("./connpass-api-client.js");
const { createPeatixDiscoveryWorkflow } = require("./connector-peatix-workflow.js");
const { createMeetupScriptFirstWorkflow } = require("./connector-meetup-workflow.js");
const { createDoorkeeperScriptFirstWorkflow } = require("./connector-doorkeeper-workflow.js");
const { createEventbriteScriptFirstWorkflow } = require("./connector-eventbrite-workflow.js");
const { createTechPlayDiscoveryWorkflow } = require("./connector-techplay-workflow.js");
const { createKokuchProDiscoveryWorkflow } = require("./connector-kokuchpro-workflow.js");
const { readLumaFormProfile } = require("./luma-form-profile.js");
const { runConnectorAgenticRegistration } = require("./connector-agentic-registration.js");
const {
  createBoundedActionProposer,
  createPrivateValueResolver,
  createProductionBrowserHarness,
  inspectPageControls,
  operatePageControl,
} = require("./connector-production-browser-harness.js");
const {
  inspectGoogleCalendarBusyInventory,
  isVerifiedGoogleCalendarBusyInventory,
} = require("./google-calendar-busy-inventory.js");
const { zonedSlotInstant } = require("./honne-ja-shadow-schedule.js");
const { makeGogCalendar } = require("./transport/calendar-gog.js");
const { sendMessage: sendTelegramMessage, sendPhoto: sendTelegramPhoto } = require("./telegram.js");

const LUMA_DISCOVERY_URL = "https://luma.com/tokyo?k=p";
const PRODUCTION_TIME_ZONE = "Asia/Tokyo";
const LUMA_WORKFLOW_VERSION = "luma_registration_v1";
const CONNPASS_WORKFLOW_VERSION = "connpass_registration_v1";
const PEATIX_WORKFLOW_VERSION = "peatix_registration_v1";
const MEETUP_WORKFLOW_VERSION = "meetup_registration_v1";
const DOORKEEPER_WORKFLOW_VERSION = "doorkeeper_registration_v1";
const EVENTBRITE_WORKFLOW_VERSION = "eventbrite_registration_v1";
const TECHPLAY_WORKFLOW_VERSION = "techplay_registration_v1";
const KOKUCHPRO_WORKFLOW_VERSION = "kokuchpro_registration_v1";
const LUMA_PAGE_STATE = "registration_page_v1";
const EXPECTED_REGISTRATION_EFFECT = "registered_or_pending";
const STALE_TARGET_MAX_IDLE_MS = 660_000;
const CONNECTOR_CDP_CONNECT_TIMEOUT_MS = 120_000;
const PROVIDER_RANK_MAX_DATES = 12;
const PROVIDER_RANK_MAX_CANDIDATES = 12;
const PROVIDER_RANK_ROTATION_MS = 60_000;
const CONNPASS_DURABLE_RECONCILE_LIMIT = 3;

function invalid() {
  throw new Error("Connector minimal production unavailable");
}

function absoluteDirectory(value) {
  const directory = path.resolve(String(value || ""));
  if (!path.isAbsolute(directory) || directory === path.parse(directory).root) invalid();
  return directory;
}

function ownerToken(value) {
  const token = String(value || "").trim();
  if (!/^[A-Za-z0-9._-]{16,200}$/.test(token)) invalid();
  return token;
}

function requiredText(value, max = 2_000) {
  const result = String(value == null ? "" : value).trim();
  if (!result || result.length > max || /[\x00-\x1f\x7f]/.test(result)) invalid();
  return result;
}

function createDirectReportSender(token) {
  const telegramToken = requiredText(token, 2_000);
  return (message, options = {}) => sendTelegramMessage(
    telegramToken,
    String(options.telegramTarget || "").trim(),
    message,
  );
}

function createDirectConnpassActionSender(token) {
  const telegramToken = requiredText(token, 2_000);
  return async (message, options = {}) => {
    let response;
    try {
      response = await sendTelegramMessage(
        telegramToken,
        requiredText(options.telegramTarget, 200),
        message,
      );
    } catch {
      const error = new Error("Connpass action Telegram transport failed");
      error.safeReason = "transport";
      throw error;
    }
    if (!response || typeof response !== "object" || Array.isArray(response) || response.ok !== true
      || !response.result || typeof response.result !== "object" || Array.isArray(response.result)
      || !Object.prototype.hasOwnProperty.call(response.result, "message_id")) {
      const error = new Error("Connpass action Telegram provider result unavailable");
      error.safeReason = response && response.delivery_unknown === true ? "delivery_unknown" : "provider_rejection";
      throw error;
    }
    const messageId = response.result && response.result.message_id;
    if (!Number.isSafeInteger(messageId) || messageId <= 0) {
      const error = new Error("Connpass action Telegram provider message ID unavailable");
      error.safeReason = "missing_message_id";
      throw error;
    }
    return { messageId: String(messageId) };
  };
}

function createUnavailableConnpassActionSender() {
  return async () => {
    const error = new Error("Connpass action Telegram transport unavailable");
    error.safeReason = "transport";
    throw error;
  };
}

function directEvidenceReceipt(response, telegramTarget) {
  const messageId = response && response.result && response.result.message_id;
  const messageDate = response && response.result && response.result.date;
  const chatId = response && response.result && response.result.chat && response.result.chat.id;
  const knownNoEffect = response && typeof response === "object" && !Array.isArray(response)
    && response.ok === false && response.delivery_unknown !== true;
  if (!response || typeof response !== "object" || Array.isArray(response) || response.ok !== true
    || !response.result || typeof response.result !== "object" || Array.isArray(response.result)
    || !Object.prototype.hasOwnProperty.call(response.result, "message_id")
    || !Number.isSafeInteger(messageId) || messageId <= 0
    || !Number.isSafeInteger(messageDate) || messageDate <= 0
    || !response.result.chat || typeof response.result.chat !== "object" || Array.isArray(response.result.chat)
    || !Object.prototype.hasOwnProperty.call(response.result.chat, "id")
    || String(chatId) !== String(telegramTarget)) {
      const error = new Error("Evidence Telegram provider receipt unavailable");
      if (knownNoEffect) error.knownNoEffect = true;
      throw error;
    }
  return { messageId: String(messageId) };
}

function createDirectEvidenceMessageSender(token) {
  const telegramToken = requiredText(token, 2_000);
  return async (message, options = {}) => {
    const telegramTarget = requiredText(options.telegramTarget, 200);
    return directEvidenceReceipt(await sendTelegramMessage(telegramToken, telegramTarget, message), telegramTarget);
  };
}

function createDirectEvidencePhotoSender(token) {
  const telegramToken = requiredText(token, 2_000);
  return async (bytes, options = {}) => {
    const telegramTarget = requiredText(options.telegramTarget, 200);
    return directEvidenceReceipt(await sendTelegramPhoto(telegramToken, telegramTarget, bytes, options.caption), telegramTarget);
  };
}

function exactNow(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) invalid();
  return date;
}

function localDay(date, timeZone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date).filter((part) => part.type !== "literal")
    .map((part) => [part.type, Number(part.value)]));
  if (![parts.year, parts.month, parts.day].every(Number.isInteger)) invalid();
  return Object.freeze({ year: parts.year, month: parts.month, day: parts.day });
}

function addCalendarDays(day, count) {
  const shifted = new Date(Date.UTC(day.year, day.month - 1, day.day + count));
  return Object.freeze({
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  });
}

function candidateTokyoDateKey(candidate) {
  const startsAt = candidate && typeof candidate.starts_at === "string" ? candidate.starts_at : "";
  const timestamp = Date.parse(startsAt);
  if (!Number.isFinite(timestamp)) return null;
  const day = localDay(new Date(timestamp), PRODUCTION_TIME_ZONE);
  return [day.year, day.month, day.day].map((part, index) => String(part).padStart(index === 0 ? 4 : 2, "0")).join("-");
}

function candidateCoverageWeek(candidate, observed) {
  const date = candidateTokyoDateKey(candidate);
  if (!date) return 4;
  const current = localDay(observed, PRODUCTION_TIME_ZONE);
  const [year, month, day] = date.split("-").map(Number);
  const offset = Math.floor((Date.UTC(year, month - 1, day)
    - Date.UTC(current.year, current.month - 1, current.day)) / 86_400_000);
  return Math.min(3, Math.max(0, Math.floor(offset / 7)));
}

function boundedPendingCandidates(candidates, rotation = 0) {
  if (!Number.isSafeInteger(rotation) || rotation < 0) invalid();
  if (candidates.length <= PROVIDER_RANK_MAX_CANDIDATES) return candidates;
  const byDate = new Map();
  const invalidDates = [];
  for (const candidate of candidates) {
    const date = candidateTokyoDateKey(candidate);
    if (!date) {
      invalidDates.push(candidate);
      continue;
    }
    const group = byDate.get(date) || [];
    group.push(candidate);
    byDate.set(date, group);
  }
  const allDates = [...byDate.keys()].sort();
  const dates = allDates.length <= PROVIDER_RANK_MAX_DATES
    ? allDates
    : Array.from({ length: PROVIDER_RANK_MAX_DATES }, (_, index) => (
      allDates[Math.floor(index * (allDates.length - 1) / (PROVIDER_RANK_MAX_DATES - 1))]
    ));
  const selected = [];
  for (let index = 0; selected.length < PROVIDER_RANK_MAX_CANDIDATES; index += 1) {
    let added = false;
    for (const date of dates) {
      const group = byDate.get(date);
      if (index >= group.length) continue;
      selected.push(group[(rotation + index) % group.length]);
      added = true;
      if (selected.length >= PROVIDER_RANK_MAX_CANDIDATES) break;
    }
    if (!added) break;
  }
  for (const candidate of invalidDates) {
    if (selected.length >= PROVIDER_RANK_MAX_CANDIDATES) break;
    selected.push(candidate);
  }
  return Object.freeze(selected);
}

function createProductionCalendarReader(options = {}) {
  const timeZone = String(options.timeZone || PRODUCTION_TIME_ZONE);
  if (timeZone !== PRODUCTION_TIME_ZONE) invalid();
  const now = options.now || (() => new Date());
  const makeCalendar = options.makeCalendar || makeGogCalendar;
  const inspectBusyInventory = options.inspectBusyInventory || inspectGoogleCalendarBusyInventory;
  const isVerifiedBusyInventory = options.isVerifiedBusyInventory || isVerifiedGoogleCalendarBusyInventory;
  if (
    typeof now !== "function" || typeof makeCalendar !== "function"
    || typeof inspectBusyInventory !== "function" || typeof isVerifiedBusyInventory !== "function"
  ) invalid();
  const calendar = makeCalendar({
    bin: options.gogBin,
    account: options.account,
    keyring: options.keyring,
  });
  if (!calendar || typeof calendar.ready !== "function" || calendar.ready() !== true) invalid();

  return Object.freeze({
    async readCalendarGaps() {
      const observed = exactNow(now());
      const firstDay = localDay(observed, timeZone);
      const request = {
        calendar,
        timeMin: zonedSlotInstant(firstDay, "00:00", timeZone),
        timeMax: zonedSlotInstant(addCalendarDays(firstDay, 28), "00:00", timeZone),
        now: observed.toISOString(),
        timeZone,
      };
      let inventory;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          inventory = await inspectBusyInventory(request);
          break;
        } catch (error) {
          if (attempt === 1) throw error;
        }
      }
      if (!isVerifiedBusyInventory(inventory) || !Array.isArray(inventory.busy_intervals)) invalid();
      return inventory.busy_intervals;
    },
  });
}

function createProductionProviderRouter(options = {}) {
  const lumaWorkflow = options.lumaWorkflow;
  const connpassWorkflow = options.connpassWorkflow;
  const peatixWorkflow = options.peatixWorkflow;
  const meetupWorkflow = options.meetupWorkflow;
  const doorkeeperWorkflow = options.doorkeeperWorkflow;
  const eventbriteWorkflow = options.eventbriteWorkflow;
  const techplayWorkflow = options.techplayWorkflow;
  const kokuchproWorkflow = options.kokuchproWorkflow;
  const actionCache = options.actionCache;
  const reconciliationStore = options.reconciliationStore || Object.freeze({ list: () => [], save: () => {}, remove: () => false });
  const browserHarness = options.browserHarness;
  const performAction = options.performAction;
  const rankCandidates = options.rankCandidates;
  const classifyTalkOpportunity = options.classifyTalkOpportunity;
  const buildTalkPack = options.buildTalkPack;
  const eventPreferences = rankCandidates == null ? null : requiredText(options.eventPreferences);
  const connpassAutomatedSubmitAllowed = options.connpassAutomatedSubmitAllowed === true;
  const now = options.now || (() => new Date());
  if (
    !lumaWorkflow || typeof lumaWorkflow.discoverCandidates !== "function"
    || typeof lumaWorkflow.runDirectAction !== "function"
    || typeof lumaWorkflow.readProviderState !== "function"
    || !connpassWorkflow || typeof connpassWorkflow.discoverCandidates !== "function"
    || typeof connpassWorkflow.runDirectAction !== "function"
    || typeof connpassWorkflow.readProviderState !== "function"
    || (peatixWorkflow != null && (typeof peatixWorkflow.discoverCandidates !== "function"
      || typeof peatixWorkflow.runDirectAction !== "function"
      || typeof peatixWorkflow.readProviderState !== "function"))
    || (meetupWorkflow != null && (typeof meetupWorkflow.discoverCandidates !== "function"
      || typeof meetupWorkflow.runDirectAction !== "function"
      || typeof meetupWorkflow.readProviderState !== "function"))
    || (doorkeeperWorkflow != null && (typeof doorkeeperWorkflow.discoverCandidates !== "function"
      || typeof doorkeeperWorkflow.runDirectAction !== "function"
      || typeof doorkeeperWorkflow.readProviderState !== "function"))
    || (eventbriteWorkflow != null && (typeof eventbriteWorkflow.discoverCandidates !== "function"
      || typeof eventbriteWorkflow.runDirectAction !== "function"
      || typeof eventbriteWorkflow.readProviderState !== "function"))
    || (techplayWorkflow != null && (typeof techplayWorkflow.discoverCandidates !== "function"
      || typeof techplayWorkflow.runDirectAction !== "function"
      || typeof techplayWorkflow.readProviderState !== "function"))
    || (kokuchproWorkflow != null && (typeof kokuchproWorkflow.discoverCandidates !== "function"
      || typeof kokuchproWorkflow.runDirectAction !== "function"
      || typeof kokuchproWorkflow.readProviderState !== "function"))
    || !actionCache || typeof actionCache.replay !== "function"
    || typeof actionCache.saveVerifiedRepair !== "function"
    || !reconciliationStore || typeof reconciliationStore.list !== "function"
    || typeof reconciliationStore.save !== "function" || typeof reconciliationStore.remove !== "function"
    || !browserHarness || typeof browserHarness.runFallback !== "function"
    || typeof performAction !== "function" || typeof now !== "function"
    || (rankCandidates != null && typeof rankCandidates !== "function")
    || (classifyTalkOpportunity != null && typeof classifyTalkOpportunity !== "function")
    || (buildTalkPack != null && typeof buildTalkPack !== "function")
  ) invalid();

  function selected(input) {
    if (!input || !["luma", "connpass", "peatix", "meetup", "doorkeeper", "eventbrite", "techplay", "kokuchpro"].includes(input.provider)) invalid();
    const workflow = input.provider === "luma" ? lumaWorkflow
      : input.provider === "connpass" ? connpassWorkflow
        : input.provider === "peatix" ? peatixWorkflow
          : input.provider === "meetup" ? meetupWorkflow
            : input.provider === "doorkeeper" ? doorkeeperWorkflow
              : input.provider === "eventbrite" ? eventbriteWorkflow
                : input.provider === "techplay" ? techplayWorkflow : kokuchproWorkflow;
    if (!workflow) invalid();
    return Object.freeze({
      input,
      workflow,
      workflowVersion: input.provider === "luma" ? LUMA_WORKFLOW_VERSION
        : input.provider === "connpass" ? CONNPASS_WORKFLOW_VERSION
          : input.provider === "peatix" ? PEATIX_WORKFLOW_VERSION
            : input.provider === "meetup" ? MEETUP_WORKFLOW_VERSION
              : input.provider === "doorkeeper" ? DOORKEEPER_WORKFLOW_VERSION
                : input.provider === "eventbrite" ? EVENTBRITE_WORKFLOW_VERSION
                  : input.provider === "techplay" ? TECHPLAY_WORKFLOW_VERSION : KOKUCHPRO_WORKFLOW_VERSION,
    });
  }

  function rememberConnpass(result, route) {
    const remember = (value) => {
      if (route.input.provider === "connpass" && value && value.status === "completed") {
        reconciliationStore.save(route.input.candidate, exactNow(now()).toISOString());
      }
      return value;
    };
    return result && typeof result.then === "function" ? result.then(remember) : remember(result);
  }

  return Object.freeze({
    discoverCandidates(provider, calendar, page) {
      const route = selected({ provider });
      const discovered = route.workflow.discoverCandidates({ page, calendar });
      return (async () => {
        const discoveredCandidates = await discovered;
        const pendingReconciliation = provider === "connpass" ? reconciliationStore.list(provider) : [];
        const queueOffset = pendingReconciliation.length === 0 ? 0
          : Math.floor(exactNow(now()).getTime() / 1_800_000) % pendingReconciliation.length;
        const rotatedReconciliation = Object.freeze([
          ...pendingReconciliation.slice(queueOffset), ...pendingReconciliation.slice(0, queueOffset),
        ].slice(0, CONNPASS_DURABLE_RECONCILE_LIMIT));
        const queued = rotatedReconciliation.map((candidate) => Object.freeze({
          ...candidate,
          registration_status: "registered",
          reconciliation_only: true,
        }));
        const queuedRefs = new Set(queued.map((candidate) => candidate.event_ref));
        const candidates = Object.freeze([...queued, ...discoveredCandidates.filter((candidate) => !queuedRefs.has(candidate.event_ref))]);
        if (rankCandidates == null) return candidates;
        if (candidates.length === 0) return candidates;
        const reconcile = candidates.filter((candidate) => (
          candidate.rsvp_status === "registered" || candidate.registration_status === "registered"
        ));
        const pending = candidates.filter((candidate) => !reconcile.includes(candidate));
        if (pending.length === 0) return candidates;
        const rotation = Math.floor(exactNow(now()).getTime() / PROVIDER_RANK_ROTATION_MS);
        const rankingCandidates = boundedPendingCandidates(pending, rotation);
        const ranking = await rankCandidates({ candidates: rankingCandidates, preferences: eventPreferences });
        const sourceByRef = new Map(rankingCandidates.map((candidate) => [candidate.event_ref, candidate]));
        const observed = exactNow(now());
        const eligible = eligibleRankedCandidates(ranking).map((ranked) => Object.freeze({
          ...sourceByRef.get(ranked.event_ref),
          priority_class: ranked.priority_class,
          preference_fit: ranked.preference_fit,
          preference_reason: ranked.preference_reason,
          auto_apply_eligible: ranked.auto_apply_eligible,
        })).sort((a, b) => candidateCoverageWeek(a, observed) - candidateCoverageWeek(b, observed));
        if (classifyTalkOpportunity == null) return Object.freeze([...reconcile, ...eligible]);
        const enriched = new Array(eligible.length);
        let next = 0;
        async function classifyWorker() {
          while (next < eligible.length) {
            const index = next;
            next += 1;
            const candidate = eligible[index];
            if (candidate.priority_class !== "open_talk") {
              enriched[index] = candidate;
              continue;
            }
            let opportunity = null;
            try { opportunity = await classifyTalkOpportunity(candidate); } catch { opportunity = null; }
            let talkPack = null;
            const classifiedOpen = isVerifiedEventTalkOpportunity(opportunity)
              && opportunity.should_create_talk_application === true;
            if (classifiedOpen && buildTalkPack != null) {
              try { talkPack = await buildTalkPack(candidate, opportunity); } catch { talkPack = null; }
            }
            const openTalk = classifiedOpen && (buildTalkPack == null || talkPack != null);
            enriched[index] = Object.freeze(openTalk ? {
              ...candidate,
              priority_class: "open_talk",
              talk_opportunity: opportunity,
              ...(talkPack == null ? {} : { talk_pack: talkPack }),
            } : { ...candidate });
          }
        }
        await Promise.all(Array.from({ length: Math.min(3, eligible.length) }, () => classifyWorker()));
        enriched.sort((a, b) => candidateCoverageWeek(a, observed) - candidateCoverageWeek(b, observed)
          || Number(b.priority_class === "open_talk") - Number(a.priority_class === "open_talk"));
        return Object.freeze([...reconcile, ...enriched]);
      })();
    },

    runCachedAction(input) {
      const route = selected(input);
      if (route.input.provider === "connpass" && !connpassAutomatedSubmitAllowed) {
        return Object.freeze({ status: "failed", safe_reason: "connpass_action_permission_required" });
      }
      return rememberConnpass(actionCache.replay({
        provider: route.input.provider,
        workflowVersion: route.workflowVersion,
        pageState: LUMA_PAGE_STATE,
        expectedEffect: EXPECTED_REGISTRATION_EFFECT,
        page: route.input.page,
        performAction: (action) => performAction({ ...action, provider: route.input.provider }),
        readExpectedState: ({ page }) => route.workflow.readProviderState({
          page,
          candidate: route.input.candidate,
        }),
      }), route);
    },

    runDirectAction(input) {
      const route = selected(input);
      if (route.input.provider === "connpass" && !connpassAutomatedSubmitAllowed) {
        return Object.freeze({ status: "failed", safe_reason: "connpass_action_permission_required" });
      }
      return rememberConnpass(route.workflow.runDirectAction({ page: route.input.page, candidate: route.input.candidate }), route);
    },

    runAgentFallback(input) {
      const route = selected(input);
      if (route.input.provider === "connpass" && !connpassAutomatedSubmitAllowed) {
        return Object.freeze({ status: "failed", safe_reason: "connpass_action_permission_required" });
      }
      if (!Number.isInteger(route.input.maxSteps) || route.input.maxSteps < 1) invalid();
      const maxSteps = route.input.provider === "techplay"
        ? route.input.maxSteps : Math.min(route.input.maxSteps, 10);
      return rememberConnpass(browserHarness.runFallback({
        provider: route.input.provider,
        candidate: route.input.candidate,
        page: route.input.page,
        pageWebsocket: route.input.pageWebsocket,
        maxSteps,
        expectedState: route.input.expectedState,
      }), route);
    },

    readProviderState(input) {
      const route = selected(input);
      const result = route.workflow.readProviderState({ page: route.input.page, candidate: route.input.candidate });
      const settle = (value) => {
        if (route.input.provider === "connpass" && route.input.candidate.reconciliation_only === true
          && value && value.status === "absent") {
          reconciliationStore.remove(route.input.provider, route.input.candidate.event_ref);
        }
        return value;
      };
      return result && typeof result.then === "function" ? result.then(settle) : settle(result);
    },

    saveRepairedActions(input) {
      const route = selected(input);
      return actionCache.saveVerifiedRepair({
        provider: route.input.provider,
        workflowVersion: route.workflowVersion,
        pageState: LUMA_PAGE_STATE,
        expectedEffect: EXPECTED_REGISTRATION_EFFECT,
        providerState: route.input.providerState,
        actions: route.input.repairedActions,
        observedAt: exactNow(now()).toISOString(),
      });
    },
  });
}

function createMinimalProductionDependencies(options = {}) {
  const repoRoot = absoluteDirectory(options.repoRoot);
  const stateDir = absoluteDirectory(options.stateDir);
  const wakeId = requiredText(options.wakeId, 160);
  const calendarAccount = requiredText(options.calendarAccount, 1_024);
  const gogKeyring = requiredText(options.gogKeyring, 2_000);
  const telegramTarget = requiredText(options.telegramTarget, 200);
  const telegramToken = options.operations ? String(options.telegramToken || "").trim() : requiredText(options.telegramToken, 2_000);
  const tenantId = requiredText(options.tenantId || "dais-local", 128);
  const calendarId = requiredText(options.calendarId || "primary", 1_024);
  const lumaFormProfilePath = path.resolve(requiredText(options.lumaFormProfilePath, 2_000));
  const lunaEvidenceDir = absoluteDirectory(options.lunaEvidenceDir);
  const now = options.now || (() => new Date());
  if (typeof now !== "function") invalid();
  const nowIso = () => exactNow(now()).toISOString();
  const eventPreferences = options.eventPreferences == null
    ? null : requiredText(options.eventPreferences, 2_000);
  const rankCandidates = options.rankCandidates || (eventPreferences == null ? null : (input) => (
    inferProviderCandidateRanking(input, {
      apiKey: requiredText(options.geminiApiKey, 2_000),
      onAudit: operations.recordRankingAudit,
    })
  ));
  const classifyTalkOpportunity = options.classifyTalkOpportunity || (eventPreferences == null ? null : (candidate) => (
    inferEventTalkOpportunity({
      canonicalUrl: candidate.canonical_url,
      title: candidate.title,
      body: candidate.body || candidate.description,
      now: nowIso(),
    }, { apiKey: requiredText(options.geminiApiKey, 2_000), tenantId,
      recordUsageEvent: options.recordUsageEvent })
  ));
  const talkFactsPath = path.resolve(options.talkFactsPath || path.join(
    repoRoot, "apps", "life-manager", "config", "connector", "life-manager-talk-facts.json",
  ));
  const buildTalkPack = options.buildTalkPack || ((candidate, opportunity) => generateGroundedTalkPack({
    event: {
      canonicalUrl: candidate.canonical_url,
      title: candidate.title,
      body: candidate.body || candidate.description,
      now: nowIso(),
    },
    facts: readConnectorTalkFacts(talkFactsPath),
    opportunity,
  }, { apiKey: requiredText(options.geminiApiKey, 2_000), tenantId,
    recordUsageEvent: options.recordUsageEvent }));
  const talkBrowserProvider = options.talkBrowserProvider || createTalkBrowserProvider();
  const talkApplicationWorkflow = options.talkApplicationWorkflow || createTalkApplicationWorkflow(talkBrowserProvider);
  const talkEvidenceChain = options.talkEvidenceChain || createTalkEvidenceChain({ stateDir, now });
  const connpassActionTelegram = options.connpassActionTelegram || createConnpassActionTelegram({
    stateDir,
    wakeId,
    telegramTarget,
    now,
    send: options.sendMessage || (telegramToken
      ? createDirectConnpassActionSender(telegramToken)
      : createUnavailableConnpassActionSender()),
  });

  const calendar = options.calendar || makeGogCalendar({
    bin: options.gogBin,
    account: calendarAccount,
    keyring: gogKeyring,
  });
  const calendarReader = options.calendarReader || createProductionCalendarReader({
    gogBin: options.gogBin,
    account: calendarAccount,
    keyring: gogKeyring,
    now,
    makeCalendar: () => calendar,
  });
  const browserRail = options.browserRail || createProductionBrowserRail({ stateDir });
  const operations = options.operations || createMinimalProductionOperations({
    stateDir,
    wakeId,
    telegramTarget,
    telegramToken,
    sendMessage: options.reportSendMessage || createDirectReportSender(telegramToken),
    now,
  });
  // Created before the workflows (moved up from its original spot below the
  // provider router) so the Luma workflow's discoverCandidates can ask it
  // "does this already-registered event have a bundle yet?" via the same
  // bundle store completeEvidence() itself reads — no parallel bundle-check
  // path, no second source of truth for "already applied".
  const evidenceChain = options.evidenceChain || createMinimalEvidenceChain({
    stateDir,
    tenantId,
    calendar,
    calendarId,
    telegramTarget,
    evidenceStore: options.evidenceStore,
    peatixEvidenceStore: options.peatixEvidenceStore,
    connpassEvidenceStore: options.connpassEvidenceStore,
    meetupEvidenceStore: options.meetupEvidenceStore,
    doorkeeperEvidenceStore: options.doorkeeperEvidenceStore,
    eventbriteEvidenceStore: options.eventbriteEvidenceStore,
    techplayEvidenceStore: options.techplayEvidenceStore,
    sendMessage: options.evidenceSendMessage || (telegramToken
      ? createDirectEvidenceMessageSender(telegramToken)
      : createUnavailableConnpassActionSender()),
    sendPhoto: options.evidenceSendPhoto || (telegramToken
      ? createDirectEvidencePhotoSender(telegramToken)
      : createUnavailableConnpassActionSender()),
    timeZone: PRODUCTION_TIME_ZONE,
    now,
  });
  const lumaWorkflow = options.lumaWorkflow || createLumaScriptFirstWorkflow({
    now,
    onDiscoveryAudit: operations.recordDiscoveryAudit || (() => {}),
    readLumaFormProfile: () => readLumaFormProfile({ path: lumaFormProfilePath }),
    agenticRegister: options.lumaAgenticRegister || ((input) => runConnectorAgenticRegistration({
      ...input,
      evidenceDir: lunaEvidenceDir,
      repoRoot,
    })),
    hasAppliedBundle: (candidate) => evidenceChain.hasAppliedBundle({
      provider: "luma",
      event_ref: candidate.event_ref,
      provider_status: "registered",
    }),
  });
  const connpassApiClient = options.connpassApiClient || (options.connpassWorkflow || options.providerRouter ? null : createConnpassApiClient({
    apiKey: options.connpassApiKey,
  }));
  const connpassWorkflow = options.connpassWorkflow || createConnpassScriptFirstWorkflow({
    now,
    connpassApiClient,
    allowAutomatedSubmit: options.connpassAutomatedSubmitAllowed === true,
    onDiscoveryAudit: operations.recordConnpassDiscoveryAudit || (() => {}),
    hasAppliedBundle: (candidate) => evidenceChain.hasAppliedBundle({
      provider: "connpass",
      event_ref: candidate.event_ref,
      provider_status: "registered",
    }),
    // Same attendee name Peatix already gets via peatixAttendeeProfile.name
    // (see native-pass.js's productionConfig -> attendeeName) — reused here
    // rather than adding a second config field for the same fact. Read
    // options.peatixAttendeeProfile exactly once per call (it may be a lazy
    // getter — see the "keeps attendee profile lazy" test) rather than
    // twice via a short-circuit expression.
    readAttendeeName: () => {
      const profile = options.peatixAttendeeProfile;
      return profile ? profile.name : undefined;
    },
  });
  const peatixWorkflow = options.peatixWorkflow || createPeatixDiscoveryWorkflow({
    now,
    searchBindingLimit: 20,
    onDiscoveryAudit: operations.recordPeatixDiscoveryAudit || (() => {}),
    readAttendeeProfile: () => options.peatixAttendeeProfile,
    hasAppliedBundle: (candidate) => evidenceChain.hasAppliedBundle({
      provider: "peatix",
      event_ref: candidate.event_ref,
      provider_status: "registered",
    }),
  });
  const meetupWorkflow = options.meetupWorkflow || createMeetupScriptFirstWorkflow({
    now,
    onDiscoveryAudit: operations.recordMeetupDiscoveryAudit || (() => {}),
  });
  const doorkeeperWorkflow = options.doorkeeperWorkflow || createDoorkeeperScriptFirstWorkflow({
    now,
    onDiscoveryAudit: operations.recordDoorkeeperDiscoveryAudit || (() => {}),
  });
  const eventbriteWorkflow = options.eventbriteWorkflow || createEventbriteScriptFirstWorkflow({
    now,
    onDiscoveryAudit: operations.recordEventbriteDiscoveryAudit || (() => {}),
  });
  const techplayWorkflow = options.techplayWorkflow || createTechPlayDiscoveryWorkflow({
    now,
    onDiscoveryAudit: operations.recordTechPlayDiscoveryAudit || (() => {}),
  });
  const kokuchproWorkflow = options.kokuchproWorkflow || createKokuchProDiscoveryWorkflow({
    now,
    onDiscoveryAudit: operations.recordKokuchProDiscoveryAudit || (() => {}),
    registeredReconciliationEnabled: true,
    hasAppliedBundle: (candidate) => evidenceChain.hasAppliedBundle({
      provider: "kokuchpro",
      event_ref: candidate.event_ref,
      provider_status: "registered",
    }),
  });
  const actionCache = options.actionCache || createConnectorActionCache({
    path: path.join(stateDir, "action-cache.json"),
  });
  const reconciliationStore = options.reconciliationStore || createConnectorReconciliationStore({
    path: path.join(stateDir, "reconciliation-candidates.json"),
  });
  const proposeAction = options.proposeAction || createBoundedActionProposer({
    repoRoot,
    evidenceDir: lunaEvidenceDir,
    extensionProvider: "kokuchpro",
  });
  const resolveValue = options.resolveValue || createPrivateValueResolver({
    readPeatixProfile: () => options.peatixAttendeeProfile,
    readFormProfile: () => readLumaFormProfile({ path: lumaFormProfilePath }),
  });
  const browserHarness = options.browserHarness || createProductionBrowserHarness({
    lumaWorkflow,
    connpassWorkflow,
    peatixWorkflow,
    meetupWorkflow,
    doorkeeperWorkflow,
    eventbriteWorkflow,
    techplayWorkflow,
    extensionProvider: "kokuchpro",
    extensionWorkflow: kokuchproWorkflow,
    inspectControls: inspectPageControls,
    proposeAction,
    operateControl: operatePageControl,
    resolveValue,
  });
  const providerRouter = options.providerRouter || createProductionProviderRouter({
    lumaWorkflow,
    connpassWorkflow,
    peatixWorkflow,
    meetupWorkflow,
    doorkeeperWorkflow,
    eventbriteWorkflow,
    techplayWorkflow,
    kokuchproWorkflow,
    actionCache,
    reconciliationStore,
    browserHarness,
    performAction: browserHarness.performAction,
    connpassAutomatedSubmitAllowed: options.connpassAutomatedSubmitAllowed === true,
    eventPreferences,
    rankCandidates,
    classifyTalkOpportunity,
    buildTalkPack,
    now,
  });
  return Object.freeze({
    now: nowIso,
    browserRail,
    readCalendarGaps: calendarReader.readCalendarGaps,
    discoverCandidates: providerRouter.discoverCandidates,
    runCachedAction: providerRouter.runCachedAction,
    runDirectAction: providerRouter.runDirectAction,
    runAgentFallback: providerRouter.runAgentFallback,
    readProviderState: providerRouter.readProviderState,
    saveRepairedActions: providerRouter.saveRepairedActions,
    completeEvidence: async (input) => {
      const bundle = await evidenceChain.completeEvidence(input);
      if (bundle && bundle.status === "applied_bundle" && String(bundle.bundle_id || "")
        && ["created", "reused"].includes(bundle.completion_disposition)) {
        reconciliationStore.remove(input.provider, input.candidate.event_ref);
      }
      return bundle;
    },
    runTalkApplication: talkApplicationWorkflow.run,
    completeTalkEvidence: talkEvidenceChain.completeTalkEvidence,
    reportConnpassActionBoundary: options.connpassAutomatedSubmitAllowed === true
      ? undefined : connpassActionTelegram.report,
    reportWake: operations.reportWake,
    recordAction: operations.recordAction,
  });
}

function createProductionBrowserRail(options = {}) {
  const stateDir = absoluteDirectory(options.stateDir);
  const connectOverCDP = options.connectOverCDP || ((endpoint) => {
    const { chromium } = require("playwright-core");
    return chromium.connectOverCDP(endpoint, { timeout: CONNECTOR_CDP_CONNECT_TIMEOUT_MS });
  });
  const createTargetController = options.createTargetController
    || ((input) => createConnectorBrowserTargetController(input));
  const createTargetOwnership = options.createTargetOwnership || ((input) => {
    const targetLease = createConnectorTargetLease({
      ledgerPath: path.join(stateDir, "evidence", "target-leases.json"),
      ownerToken: () => input.ownerToken,
      probeTarget: (pageWebsocket) => input.controller.probe(pageWebsocket),
      closeTarget: (targetId) => input.controller.close(targetId),
    });
    return createConnectorTabOwner({
      endpoint: CONNECTOR_CDP_ENDPOINT,
      targetLease,
    });
  });
  const makeSessionId = options.makeSessionId || (() => crypto.randomUUID());
  if (
    typeof connectOverCDP !== "function"
    || typeof createTargetController !== "function"
    || typeof createTargetOwnership !== "function"
    || typeof makeSessionId !== "function"
  ) invalid();

  return Object.freeze({
    async open(input = {}) {
      const exactOwnerToken = ownerToken(input.ownerToken);
      const browser = await connectOverCDP(
        CONNECTOR_CDP_ENDPOINT,
        { timeout: CONNECTOR_CDP_CONNECT_TIMEOUT_MS },
      );
      const controller = createTargetController({ browser, endpoint: CONNECTOR_CDP_ENDPOINT });
      if (!controller || typeof controller.create !== "function" || typeof controller.close !== "function") invalid();
      let ownership = null;
      let receipt = null;
      let target = null;
      try {
        ownership = createTargetOwnership({
          controller,
          ownerToken: exactOwnerToken,
          stateDir,
        });
        if (
          !ownership || typeof ownership.claimExact !== "function"
          || typeof ownership.probe !== "function" || typeof ownership.heartbeat !== "function"
          || typeof ownership.release !== "function" || typeof ownership.reapStale !== "function"
        ) invalid();
        await ownership.reapStale({ maxIdleMs: STALE_TARGET_MAX_IDLE_MS });
        target = await controller.create();
        receipt = await ownership.claimExact({
          canonicalUrl: LUMA_DISCOVERY_URL,
          targetId: target.target_id,
          pageWebsocket: target.page_websocket,
          receiptPath: path.join(stateDir, "evidence", "tab-owner.json"),
        });
        if (await ownership.probe(receipt) !== true) invalid();
        await ownership.heartbeat(receipt);
        const sessionId = String(makeSessionId());
        if (!/^[A-Za-z0-9._-]{3,128}$/.test(sessionId)) invalid();
        return Object.freeze({
          session_id: sessionId,
          target_id: target.target_id,
          page_websocket: target.page_websocket,
          page: target.page,
          ownership,
          receipt,
        });
      } catch (error) {
        if (receipt && ownership) {
          try { await ownership.release(receipt); } catch {}
        } else {
          try { await controller.close(target.target_id); } catch {}
        }
        throw error;
      }
    },

    async navigate(owned, url) {
      if (!owned || !owned.page || !owned.ownership || !owned.receipt) invalid();
      await owned.ownership.heartbeat(owned.receipt);
      await owned.page.goto(String(url), { waitUntil: "domcontentloaded", timeout: 30_000 });
      await owned.ownership.heartbeat(owned.receipt);
    },

    async close(owned) {
      if (!owned || !owned.ownership || !owned.receipt) invalid();
      return owned.ownership.release(owned.receipt);
    },
  });
}

module.exports = {
  createProductionBrowserRail,
  createProductionCalendarReader,
  createMinimalProductionDependencies,
  createProductionProviderRouter,
};
