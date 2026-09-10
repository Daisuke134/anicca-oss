"use strict";

const { createHash } = require("node:crypto");
const { canonicalEventUrl } = require("./canonical-event-url.js");
const {
  buildEventApplicationJob,
} = require("./outbound-event-job.js");
const {
  executeLumaRsvpJob,
} = require("./luma-rsvp-adapter.js");
const {
  assertVerifiedOutboundReceipt,
} = require("./outbound-success.js");
const {
  syncVerifiedRegistrationToGoogleCalendar,
} = require("./connector-calendar-sync.js");
const {
  buildVerifiedRegistrationCoverageEvidence,
  rebuildRollingEventCoverage,
} = require("./connector-coverage-assembler.js");
const {
  buildConnectorCoverageTelegramMessage,
  deliverConnectorCoverageTelegram,
} = require("./connector-coverage-telegram.js");
const { isVerifiedLumaDateInventory } = require("./luma-date-inventory.js");
const { isVerifiedEventProviderDateInventory } = require("./event-provider-date-inventory.js");
const { isVerifiedRollingEventCoverage } = require("./rolling-event-coverage.js");
const { isVerifiedGoogleCalendarBusyInventory } = require("./google-calendar-busy-inventory.js");
const { isVerifiedEventGoalSerendipity } = require("./event-goal-serendipity.js");
const { hashChatId } = require("./telegram.js");
const { verifyLumaConfirmationMessage } = require("./luma-confirmation-mail.js");
const { createLumaGuestBinding } = require("./luma-ticket-qr.js");
const { deliverConnectorTicket } = require("./connector-ticket-telegram.js");

const EVENT_REF = /^(?:luma-event:\/\/event\/[A-Za-z0-9_-]+|connpass-event:\/\/event\/[1-9][0-9]*)$/;
const TENANT = /^[a-z0-9][a-z0-9._-]{0,199}$/;
const POSITIVE_REF = /^[^\x00-\x1f\x7f]{1,1024}$/;
const PRIORITY_CLASSES = Object.freeze(["yc_hackathon", "open_talk", "ai", "crypto", "startup", "other"]);
const TALK_STATES = Object.freeze(["not_open", "application_ready", "submitted", "provider_verified", "accepted", "rejected", "human_action_required"]);

function invalid(message = "Connector native write pipeline invalid") {
  throw new Error(message);
}

function object(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid(`${label} invalid`);
  return value;
}

function text(value, label, max = 512) {
  const result = String(value == null ? "" : value).trim();
  if (!result || result.length > max || /[\x00-\x1f\x7f]/.test(result)) invalid(`${label} invalid`);
  return result;
}

function exactInstant(value, label) {
  const raw = text(value, label);
  const milliseconds = Date.parse(raw);
  if (!Number.isFinite(milliseconds) || !/[zZ]|[+-]\d\d:\d\d$/.test(raw)) {
    invalid(`${label} invalid`);
  }
  return new Date(milliseconds).toISOString();
}

function factory(deps, name, fallback) {
  return typeof deps[name] === "function" ? deps[name] : fallback;
}

function safeCode(error, fallback) {
  const code = String(error && error.code || "").trim();
  return /^[A-Z][A-Z0-9_:-]{0,99}$/.test(code) ? code : fallback;
}

function baseEventRef(value) {
  const raw = text(value, "event reference").split("?", 1)[0];
  if (!EVENT_REF.test(raw)) invalid("event reference invalid");
  return raw;
}

function jobRef(job) {
  const tenantId = text(job && job.tenant_id, "job tenant");
  const jobId = text(job && job.job_id, "job id");
  return `runtime-job://${tenantId}/${jobId.replace(/^outbound-event:/, "")}`;
}

function attemptRef(job) {
  return `runtime-attempt://${text(job && job.tenant_id, "job tenant")}/${text(job && job.job_id, "job id")}/${job.attempt}`;
}

function safeReceiptProjection(receipt) {
  return Object.freeze({
    attempt_ref: receipt.attempt_ref,
    external_receipt_ref: receipt.external_receipt_ref,
    artifact_ref: receipt.artifact_ref,
    evidence_observed_at: receipt.evidence_observed_at,
    artifact_sha256: receipt.artifact_sha256,
    canonical_url: receipt.canonical_url,
    verified_at: receipt.verified_at,
  });
}

function coverageProjection(coverage) {
  return Object.freeze({
    coverage_snapshot_id: coverage.coverage_snapshot_id,
    window_start_date: coverage.window_start_date,
    window_end_date: coverage.window_end_date,
    counts: coverage.counts,
  });
}

function failureResult(stage, context, error) {
  return Object.freeze({
    status: "incomplete",
    outcome: stage,
    error_code: safeCode(error, `${stage.toUpperCase()}_FAILED`),
    job_ref: jobRef(context.job),
    attempt_ref: attemptRef(context.job),
    event_ref: context.eventRef,
    canonical_url: context.eventUrl,
  });
}

function reconciliationResult(context, error) {
  return Object.freeze({
    status: "reconciliation_required",
    outcome: "unknown_external_effect",
    error_code: safeCode(error, "CONNECTOR_EFFECT_UNKNOWN"),
    job_ref: jobRef(context.job),
    attempt_ref: attemptRef(context.job),
    event_ref: context.eventRef,
    canonical_url: context.eventUrl,
  });
}

function verifiedTelegramDelivery(delivery, context, coverage, artifactSha256) {
  if (!delivery || typeof delivery !== "object" || Array.isArray(delivery)) return null;
  if (delivery.kind !== "connector_coverage_telegram_delivery") return null;
  const providerId = String(delivery.provider_id == null ? "" : delivery.provider_id).trim();
  const photoProviderId = String(delivery.photo_provider_id == null ? "" : delivery.photo_provider_id).trim();
  if (
    !providerId || !POSITIVE_REF.test(providerId)
    || !photoProviderId || !POSITIVE_REF.test(photoProviderId)
    || delivery.artifact_sha256 !== artifactSha256
  ) return null;
  const observedAt = String(delivery.observed_at == null ? "" : delivery.observed_at).trim();
  const observedMilliseconds = Date.parse(observedAt);
  if (
    !observedAt
    || !Number.isFinite(observedMilliseconds)
    || !/[zZ]|[+-]\d\d:\d\d$/.test(observedAt)
    || new Date(observedMilliseconds).toISOString() !== observedAt
  ) return null;
  if (delivery.tenant_id !== context.tenantId) return null;
  if (delivery.coverage_snapshot_id !== coverage.coverage_snapshot_id) return null;
  if (
    !/^[0-9a-f]{64}$/.test(String(delivery.chat_id_sha256 || ""))
    || delivery.chat_id_sha256 !== hashChatId(context.telegramTarget)
  ) return null;
  return Object.freeze({ providerId, photoProviderId, artifactSha256, observedAt });
}

function selectedContext(input) {
  object(input, "write input");
  const application = input.application || input.chosenCandidate || input.candidate || input;
  object(application, "chosen application");
  const profile = input.profile || input.connectorProfile || {};
  const tenantId = text(
    application.tenantId || application.tenant_id || input.tenantId || input.tenant_id || profile.tenant_id,
    "tenant",
  );
  if (!TENANT.test(tenantId)) invalid("tenant invalid");
  const eventUrl = canonicalEventUrl(
    application.eventUrl || application.event_url || application.canonicalUrl || application.canonical_url,
  );
  if (!eventUrl) invalid("chosen event URL invalid");
  const eventStartIso = application.eventStartIso
    || application.event_start_iso
    || application.startsAt
    || application.starts_at;
  const identityRef = text(
    application.identityRef || application.identity_ref || input.identityRef || input.identity_ref || profile.identity_ref,
    "identity reference",
  );
  const browserProfileRef = text(
    application.browserProfileRef
      || application.browser_profile_ref
      || input.browserProfileRef
      || input.browser_profile_ref
      || profile.browser_profile_ref,
    "browser profile reference",
  );
  const calendarRef = text(
    application.calendarRef
      || application.calendar_ref
      || input.calendarRef
      || input.calendar_ref
      || profile.calendar_ref,
    "Calendar reference",
  );
  const dateInventory = input.dateInventory || input.verifiedDateInventory;
  const currentCoverage = input.currentCoverage || input.coverage;
  const busyInventory = input.busyInventory
    || input.verifiedBusyInventory
    || input.googleCalendarBusyInventory;
  const now = input.now || input.observedAt || currentCoverage && currentCoverage.calculated_at;
  if (!eventStartIso) invalid("chosen event start invalid");
  if (!dateInventory || !currentCoverage || !busyInventory || !now) invalid("verified context missing");
  const eventRef = baseEventRef(
    application.eventRef || application.event_ref || application.eventReference || application.event_reference
      || (() => {
        const event = dateInventory.days && dateInventory.days.flatMap((day) => day.events || [])
          .find((candidate) => candidate && candidate.canonical_url === eventUrl);
        return event && event.event_ref;
      })(),
  );
  const goalDecision = application.goalDecision || application.goal_decision || input.goalDecision || input.goal_decision;
  const calendar = input.calendar || application.calendar;
  const calendarId = input.calendarId || input.calendar_id || application.calendarId || application.calendar_id;
  const telegramTarget = input.telegramTarget || input.telegram_target || application.telegramTarget || application.telegram_target;
  const calendarCoverageUrl = input.calendarCoverageUrl
    || input.calendar_coverage_url
    || application.calendarCoverageUrl
    || application.calendar_coverage_url;
  const registrationIdentity = text(
    input.registrationIdentity || input.registration_identity || application.registrationIdentity
      || application.registration_identity || "Dais",
    "registration identity",
    100,
  );
  const ranked = goalDecision && Array.isArray(goalDecision.ranked_events)
    ? goalDecision.ranked_events.find((candidate) => candidate.event_ref === eventRef) : null;
  const priorityClass = String(application.priorityClass || application.priority_class || "other");
  const talkState = String(application.talkState || application.talk_state || "not_open");
  if (!PRIORITY_CLASSES.includes(priorityClass) || !TALK_STATES.includes(talkState)) invalid("selection metadata invalid");
  const deadlineValue = application.applicationDeadlineAt || application.application_deadline_at || null;
  const applicationDeadlineAt = deadlineValue == null ? null : exactInstant(deadlineValue, "application deadline");
  const preferenceReason = text(
    application.preferenceReason || application.preference_reason || ranked?.goal_reason || "Calendarの空き枠に適合した登録",
    "preference reason",
    500,
  );
  return {
    tenantId,
    eventUrl,
    eventStartIso: exactInstant(eventStartIso, "event start"),
    eventRef,
    identityRef,
    browserProfileRef,
    calendarRef,
    dateInventory,
    currentCoverage,
    busyInventory,
    now: exactInstant(now, "observed time"),
    goalDecision,
    calendar,
    calendarId: calendarId == null ? "" : String(calendarId).trim(),
    telegramTarget: telegramTarget == null ? "" : String(telegramTarget).trim(),
    calendarCoverageUrl: calendarCoverageUrl == null ? "" : String(calendarCoverageUrl).trim(),
    registrationIdentity,
    selection: Object.freeze({
      priority_class: priorityClass,
      preference_reason: preferenceReason,
      talk_state: talkState,
      application_deadline_at: applicationDeadlineAt,
    }),
    unavailableDays: Array.isArray(input.unavailableDays) ? input.unavailableDays : [],
    registrations: Array.isArray(input.registrations) ? input.registrations : [],
  };
}

function assertContext(context, deps) {
  const providerInventory = isVerifiedEventProviderDateInventory(context.dateInventory);
  const verifyDateInventory = typeof deps.isVerifiedLumaDateInventory === "function"
    ? deps.isVerifiedLumaDateInventory
    : (value) => isVerifiedLumaDateInventory(value) || isVerifiedEventProviderDateInventory(value);
  const verifyCoverage = factory(deps, "isVerifiedRollingEventCoverage", isVerifiedRollingEventCoverage);
  const verifyBusy = factory(deps, "isVerifiedGoogleCalendarBusyInventory", isVerifiedGoogleCalendarBusyInventory);
  if (!verifyDateInventory(context.dateInventory) || !verifyCoverage(context.currentCoverage) || !verifyBusy(context.busyInventory)) {
    invalid("verified context missing");
  }
  if (context.currentCoverage.tenant_id !== context.tenantId) invalid("coverage tenant mismatch");
  if (
    context.dateInventory.coverage_snapshot_id !== context.currentCoverage.coverage_snapshot_id
    || context.dateInventory.timezone !== context.currentCoverage.timezone
    || context.busyInventory.time_zone !== context.currentCoverage.timezone
  ) invalid("verified context lineage mismatch");
  const event = context.dateInventory.days.flatMap((day) => day.events || []).find((candidate) => (
    candidate && candidate.event_ref === context.eventRef
  ));
  if (!event || event.canonical_url !== context.eventUrl) invalid("chosen event is not in verified inventory");
  if (Date.parse(event.starts_at) !== Date.parse(context.eventStartIso)) invalid("chosen event start mismatch");
  if (!providerInventory) {
    const verifyGoal = factory(deps, "isVerifiedEventGoalSerendipity", isVerifiedEventGoalSerendipity);
    if (!verifyGoal(context.goalDecision)) invalid("chosen judgment is not verified");
    if (!context.goalDecision.ranked_events.some((candidate) => candidate.event_ref === context.eventRef)) {
      invalid("chosen judgment event mismatch");
    }
  }
  if (!context.calendar || !context.calendarId) invalid("Calendar write context missing");
  if (!context.telegramTarget || !context.calendarCoverageUrl) invalid("Telegram write context missing");
  return event;
}

async function runNativeConnectorWrite(input = {}, deps = {}) {
  const injected = deps && typeof deps === "object" && !Array.isArray(deps) ? deps : {};
  const context = selectedContext(input);
  const event = assertContext(context, injected);
  const buildJob = factory(injected, "buildEventApplicationJob", buildEventApplicationJob);
  const executeJob = factory(injected, "executeLumaRsvpJob", executeLumaRsvpJob);
  const assertReceipt = factory(injected, "assertVerifiedOutboundReceipt", assertVerifiedOutboundReceipt);
  const syncCalendar = factory(
    injected,
    "syncVerifiedRegistrationToGoogleCalendar",
    syncVerifiedRegistrationToGoogleCalendar,
  );
  const buildRegistrationEvidence = factory(
    injected,
    "buildVerifiedRegistrationCoverageEvidence",
    buildVerifiedRegistrationCoverageEvidence,
  );
  const rebuildCoverage = factory(injected, "rebuildRollingEventCoverage", rebuildRollingEventCoverage);
  const buildTelegramMessage = factory(
    injected,
    "buildConnectorCoverageTelegramMessage",
    buildConnectorCoverageTelegramMessage,
  );
  const deliverTelegram = factory(
    injected,
    "deliverConnectorCoverageTelegram",
    deliverConnectorCoverageTelegram,
  );
  const verifyConfirmation = factory(
    injected, "verifyLumaConfirmationMessage", verifyLumaConfirmationMessage,
  );
  const buildGuestBinding = factory(injected, "createLumaGuestBinding", createLumaGuestBinding);
  const deliverTicket = factory(injected, "deliverConnectorTicket", deliverConnectorTicket);

  let job;
  try {
    job = { ...buildJob({
      tenantId: context.tenantId,
      eventUrl: context.eventUrl,
      eventStartIso: context.eventStartIso,
      identityRef: context.identityRef,
      browserProfileRef: context.browserProfileRef,
      calendarRef: context.calendarRef,
    }), attempt: 1 };
    object(job, "application job");
    if (job.attempt !== 1) invalid("application attempt invalid");
  } catch (error) {
    invalid(`application job unavailable: ${safeCode(error, "BUILD_APPLICATION_JOB_FAILED")}`);
  }

  const provider = injected.provider;
  const executeDependencies = {
    provider,
    readExternalReceipt: injected.readExternalReceipt,
    readArtifact: injected.readArtifact,
    fetchImpl: injected.fetchImpl,
    now: () => context.now,
  };
  let execution;
  try {
    execution = await executeJob(Object.freeze(job), executeDependencies);
  } catch (error) {
    if (error && error.unknownEffect === true) {
      return reconciliationResult({ ...context, job }, error);
    }
    return failureResult("application_failed", { ...context, job }, error);
  }
  const receipt = execution && execution.receipt;
  if (!receipt || receipt.status !== "verified") {
    const error = new Error("verified RSVP receipt missing");
    error.unknownEffect = true;
    return reconciliationResult({ ...context, job }, error);
  }
  try {
    assertReceipt(receipt, job);
  } catch (error) {
    if (error && error.unknownEffect === true) return reconciliationResult({ ...context, job }, error);
    return failureResult("receipt_verification_failed", { ...context, job }, error);
  }

  let confirmation;
  let ticket;
  let ticketFailureReason = null;
  try {
    if (
      typeof injected.readLumaConfirmation !== "function"
      || typeof injected.recordLumaConfirmation !== "function"
      || typeof injected.captureLumaTicketQr !== "function"
      || typeof injected.recordLumaTicketQr !== "function"
    ) throw new Error("Luma mail and ticket services unavailable");
    const message = await injected.readLumaConfirmation({
      registrationStartedAt: context.now,
      registrationCompletedAt: receipt.verified_at,
      eventUrl: context.eventUrl,
      eventTitle: event.title,
    });
    const verifiedMail = verifyConfirmation({
      tenantId: context.tenantId,
      jobId: job.job_id,
      eventUrl: context.eventUrl,
      eventTitle: event.title,
      registrationStartedAt: context.now,
      registrationCompletedAt: receipt.verified_at,
      message,
    });
    confirmation = await injected.recordLumaConfirmation(verifiedMail);
    if (!/^gmail-message:\/\/[a-z0-9._-]+\/[0-9a-f]{64}$/i.test(String(confirmation.external_receipt_ref || ""))) {
      throw new Error("Luma confirmation receipt invalid");
    }
    const binding = buildGuestBinding({
      tenantId: context.tenantId,
      jobId: job.job_id,
      eventUrl: context.eventUrl,
      providerMessageId: message.id,
      body: message.body,
    });
    const verifiedQr = await injected.captureLumaTicketQr(binding);
    ticket = await injected.recordLumaTicketQr(verifiedQr);
    if (
      !/^ticket:\/\/[a-z0-9._-]+\/[0-9a-f]{64}$/i.test(String(ticket.ticket_receipt_ref || ""))
      || !/^object:\/\/sha256\/[0-9a-f]{64}$/.test(String(ticket.artifact_ref || ""))
    ) throw new Error("Luma ticket receipt invalid");
  } catch (error) {
    ticketFailureReason = "TICKET_EVIDENCE_FAILED";
  }

  const syncInput = {
    calendar: context.calendar,
    calendarId: context.calendarId,
    dateInventory: context.dateInventory,
    eventRef: context.eventRef,
    registrationReceipt: receipt,
    registrationJob: Object.freeze(job),
  };
  let calendarSync;
  try {
    calendarSync = await syncCalendar(syncInput);
  } catch (error) {
    if (error && error.unknownEffect === true) return reconciliationResult({ ...context, job }, error);
    return failureResult("calendar_sync_failed", { ...context, job }, error);
  }

  let registrationEvidence;
  try {
    registrationEvidence = buildRegistrationEvidence({
      dateInventory: context.dateInventory,
      calendarSync,
    });
  } catch (error) {
    return failureResult("registration_evidence_failed", { ...context, job }, error);
  }

  let ticketDelivery;
  if (ticket) {
    try {
      ticketDelivery = await deliverTicket({
        tenantId: context.tenantId,
        telegramTarget: context.telegramTarget,
        artifactRef: ticket.artifact_ref,
        eventTitle: event.title,
        venue: event.venue_name || event.venue_address,
        registrationIdentity: context.registrationIdentity,
        selectionReason: context.goalDecision?.ranked_events.find(
          (candidate) => candidate.event_ref === context.eventRef,
        )?.goal_reason || context.selection.preference_reason,
        priorityClass: context.selection.priority_class,
        talkState: context.selection.talk_state,
        applicationDeadlineAt: context.selection.application_deadline_at,
        startsAt: event.starts_at,
        endsAt: event.ends_at,
        eventUrl: event.canonical_url,
        calendarUrl: calendarSync.calendar_event_url,
        confirmationReceiptRef: confirmation.external_receipt_ref,
      }, {
        readArtifact: injected.readTicketArtifact,
        sendMedia: injected.sendTicketMedia,
        observedAt: injected.observedAt || (() => context.now),
      });
      if (!ticketDelivery || !POSITIVE_REF.test(String(ticketDelivery.provider_id || ""))) {
        const error = new Error("Luma ticket Telegram receipt invalid");
        error.unknownEffect = true;
        throw error;
      }
    } catch (error) {
      if (error && error.unknownEffect === true) {
        return reconciliationResult({ ...context, job }, error);
      }
      ticketFailureReason = "TICKET_TELEGRAM_FAILED";
      ticketDelivery = null;
    }
  }

  let coverage;
  try {
    coverage = rebuildCoverage({
      tenantId: context.tenantId,
      timeZone: context.currentCoverage.timezone,
      now: context.now,
      previousCoverage: context.currentCoverage,
      registrations: [...context.registrations, registrationEvidence],
      unavailableDays: context.unavailableDays,
    });
  } catch (error) {
    return failureResult("coverage_rebuild_failed", { ...context, job }, error);
  }
  const verifyCoverage = factory(injected, "isVerifiedRollingEventCoverage", isVerifiedRollingEventCoverage);
  if (!verifyCoverage(coverage) || coverage.tenant_id !== context.tenantId) {
    return failureResult("coverage_rebuild_failed", { ...context, job }, new Error("coverage result invalid"));
  }

  const newEvents = [{
    eventRef: context.eventRef,
    dateInventory: context.dateInventory,
    goalDecision: context.goalDecision,
    calendarSync,
    selection: context.selection,
  }];
  const telegramInput = {
    tenantId: context.tenantId,
    telegramTarget: context.telegramTarget,
    coverage,
    newEvents,
    calendarCoverageUrl: context.calendarCoverageUrl,
  };
  try {
    if (typeof injected.readArtifact !== "function") throw new Error("registration PNG reader unavailable");
    const bytes = await injected.readArtifact(context.tenantId, receipt.artifact_ref);
    const digest = Buffer.isBuffer(bytes) ? createHash("sha256").update(bytes).digest("hex") : "";
    if (digest !== receipt.artifact_sha256) throw new Error("registration PNG hash mismatch");
    telegramInput.registrationEvidence = Object.freeze({
      event_ref: context.eventRef,
      canonical_url: receipt.canonical_url,
      artifact_ref: receipt.artifact_ref,
      artifact_sha256: receipt.artifact_sha256,
      bytes,
    });
  } catch (error) {
    return failureResult("telegram_evidence_failed", { ...context, job }, error);
  }
  try {
    // Build first so a malformed report can never be hidden by a delivery call.
    buildTelegramMessage(telegramInput);
  } catch (error) {
    return failureResult("telegram_message_build_failed", { ...context, job }, error);
  }

  let delivery;
  try {
    delivery = await deliverTelegram(telegramInput, {
      send: injected.send,
      observedAt: injected.observedAt || (() => context.now),
    });
  } catch (error) {
    if (error && error.unknownEffect === true) return reconciliationResult({ ...context, job }, error);
    return failureResult("telegram_delivery_failed", { ...context, job }, error);
  }
  const verifiedDelivery = verifiedTelegramDelivery(delivery, context, coverage, receipt.artifact_sha256);
  if (!verifiedDelivery) {
    const error = new Error("Telegram delivery needs a positive receipt");
    error.unknownEffect = true;
    return reconciliationResult({ ...context, job }, error);
  }
  const complete = coverage.counts.open === 0;
  return Object.freeze({
    status: complete ? "complete" : "incomplete",
    outcome: complete ? "verified_delivery" : "open_coverage",
    job_ref: jobRef(job),
    attempt_ref: attemptRef(job),
    event_ref: context.eventRef,
    canonical_url: event.canonical_url,
    registration_receipt: safeReceiptProjection(receipt),
    confirmation: confirmation
      ? Object.freeze({ external_receipt_ref: confirmation.external_receipt_ref })
      : Object.freeze({ status: "unavailable", reason: "CONFIRMATION_EVIDENCE_FAILED" }),
    ticket: ticket && ticketDelivery
      ? Object.freeze({
        ticket_receipt_ref: ticket.ticket_receipt_ref,
        artifact_ref: ticket.artifact_ref,
        telegram_provider_id: String(ticketDelivery.provider_id),
      })
      : Object.freeze({ status: "unavailable", reason: ticketFailureReason || "TICKET_EVIDENCE_FAILED" }),
    calendar_sync: Object.freeze({
      status: calendarSync.status,
      calendar_sync_id: calendarSync.calendar_sync_id,
      registration_receipt_ref: calendarSync.registration_receipt_ref,
      calendar_event_ref: calendarSync.calendar_event_ref,
      calendar_event_url: calendarSync.calendar_event_url,
    }),
    coverage: coverageProjection(coverage),
    selection: context.selection,
    telegram: Object.freeze({
      provider_id: verifiedDelivery.providerId,
      photo_provider_id: verifiedDelivery.photoProviderId,
      artifact_sha256: verifiedDelivery.artifactSha256,
      observed_at: verifiedDelivery.observedAt,
      coverage_snapshot_id: coverage.coverage_snapshot_id,
    }),
  });
}

module.exports = { runNativeConnectorWrite };
