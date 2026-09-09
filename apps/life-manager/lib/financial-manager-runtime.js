"use strict";

const {
  buildFinancialManagerReport,
  renderFinancialManagerTelegram,
} = require("./financial-manager-report.js");

function required(value, label) {
  const text = String(value == null ? "" : value).trim();
  if (!text) throw new Error(`${label} is required`);
  return text;
}

function instant(value, label) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error(`${label} is invalid`);
  return date;
}

function unavailableSources(ingestion) {
  return Object.entries(ingestion && ingestion.sources || {})
    .filter(([, status]) => status === "unavailable")
    .map(([source]) => source);
}

function deliveryFailure(delivery) {
  if (!delivery || delivery.delivered !== true) return "telegram_delivery_uncertain";
  if (!String(delivery.providerMessageId || "").trim()) {
    return "telegram_provider_receipt_missing";
  }
  return null;
}

// Host-neutral Financial Manager transition. Hosts only supply a FinancialRecord
// store plus source ingestion and a receipt-backed notifier; the report shape,
// quiet rules, digest fence, and provider-receipt requirement are identical.
async function runFinancialManager(options = {}) {
  const subjectId = required(options.subjectId, "Financial Manager subject");
  const reportingDate = required(options.reportingDate, "Financial Manager reporting date");
  const now = instant(options.now || new Date(), "Financial Manager clock");
  const store = options.store;
  if (!store || typeof store.read !== "function") {
    throw new Error("Financial Manager store is required");
  }
  const ingest = options.ingest || (async () => ({ observed: 0, created: 0, sources: {} }));
  const ingestion = await ingest();
  const unavailable = unavailableSources(ingestion);
  if (unavailable.length) {
    return {
      status: "failed", reason: "financial_source_unavailable", unavailableSources: unavailable,
      reportingDate, recordCount: 0, delivered: false, ingestion,
    };
  }

  const records = await store.read({ subjectId });
  const { report, digest } = buildFinancialManagerReport(records, reportingDate, {
    timezone: options.timezone || "Asia/Tokyo",
  });
  if (report.verifiedRecordCount === 0) {
    return {
      status: "quiet", reason: "no_verified_financial_records", reportingDate,
      recordCount: records.length, delivered: false, ingestion, report, digest,
    };
  }

  const deliveryStore = options.deliveryStore || {};
  if (typeof deliveryStore.lookup === "function") {
    const existing = await deliveryStore.lookup({ subjectId, digest, report, observedAt: now.toISOString() });
    if (existing) {
      return {
        status: "quiet", reason: "unchanged", reportingDate, recordCount: records.length,
        delivered: false, ingestion, report, digest, duplicate: existing,
      };
    }
  }
  if (typeof deliveryStore.claim === "function") {
    const claimed = await deliveryStore.claim({ subjectId, digest, report, observedAt: now.toISOString() });
    if (!claimed || claimed.claimed !== true) {
      const proof = typeof deliveryStore.readAfterClaim === "function"
        ? await deliveryStore.readAfterClaim({ subjectId, digest, report, observedAt: now.toISOString() })
        : null;
      if (proof && typeof deliveryStore.verifyDelivered === "function"
        && deliveryStore.verifyDelivered(proof)) {
        return {
          status: "quiet", reason: "unchanged", reportingDate, recordCount: records.length,
          delivered: false, ingestion, report, digest, duplicate: proof,
        };
      }
      return {
        status: "failed", reason: "financial_delivery_claim_unresolved", reportingDate,
        recordCount: records.length, delivered: false, ingestion, report, digest,
        unknownEffect: true,
      };
    }
  }

  if (typeof options.notify !== "function") throw new Error("Financial Manager notify is required");
  const eventKey = typeof options.eventKey === "function"
    ? options.eventKey({ subjectId, digest })
    : `financial-manager:${subjectId}:${digest}`;
  required(eventKey, "Financial Manager event key");
  let delivery;
  try {
    delivery = await options.notify({
      eventKey, observedAt: now.toISOString(), message: renderFinancialManagerTelegram(report),
    });
  } catch (error) {
    if (typeof deliveryStore.markFailed === "function") {
      await deliveryStore.markFailed({ subjectId, digest, report, observedAt: now.toISOString() });
    }
    throw error;
  }
  const failure = deliveryFailure(delivery);
  if (failure) {
    if (typeof deliveryStore.markFailed === "function") {
      await deliveryStore.markFailed({ subjectId, digest, report, observedAt: now.toISOString() });
    }
    return {
      status: "failed", reason: failure, reportingDate, recordCount: records.length,
      delivered: false, ingestion, report, digest,
    };
  }
  if (typeof deliveryStore.markDelivered === "function") {
    await deliveryStore.markDelivered({
      subjectId, digest, report, observedAt: now.toISOString(), delivery,
    });
  }
  return {
    status: "sent", reason: null, reportingDate, recordCount: records.length,
    delivered: true, providerMessageId: String(delivery.providerMessageId),
    ingestion, report, digest,
  };
}

module.exports = { runFinancialManager };
