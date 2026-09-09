"use strict";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SOLANA_ADDRESS_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const GITHUB_PR_RE = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/[1-9][0-9]*$/;
const INVOICE_CATEGORIES = new Set(["code", "art", "marketing", "other"]);

function isPublicHttpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      !["localhost", "127.0.0.1", "::1"].includes(url.hostname) &&
      !url.hostname.endsWith(".local");
  } catch {
    return false;
  }
}

function validateDelivery(delivery) {
  if (!delivery || typeof delivery !== "object") throw new Error("delivery must be an object");
  if (!UUID_RE.test(delivery.application_id || "")) throw new Error("invalid application_id");
  if (!UUID_RE.test(delivery.gig_id || "")) throw new Error("invalid gig_id");
  if (!Number.isFinite(delivery.amount_usd) || delivery.amount_usd <= 0) {
    throw new Error("amount_usd must be a positive finite number");
  }
  if (delivery.payment_currency !== "sol") throw new Error("payment_currency must be sol");
  if (!SOLANA_ADDRESS_RE.test(delivery.merchant_wallet_address || "")) {
    throw new Error("invalid merchant_wallet_address");
  }
  if (!INVOICE_CATEGORIES.has(delivery.category)) {
    throw new Error("category must be code, art, marketing, or other");
  }
  if (delivery.category === "code") {
    if (!Array.isArray(delivery.pr_links) || delivery.pr_links.length === 0 ||
        delivery.pr_links.some((link) => !GITHUB_PR_RE.test(link))) {
      throw new Error("pr_links must contain valid GitHub pull request URLs");
    }
  } else if (!Array.isArray(delivery.proof_urls) || delivery.proof_urls.length === 0 ||
      delivery.proof_urls.some((link) => !isPublicHttpsUrl(link))) {
    throw new Error("proof_urls must contain public HTTPS URLs");
  }
  if (typeof delivery.description !== "string" || delivery.description.trim() === "") {
    throw new Error("description must be non-empty");
  }
}

function invoiceId(invoice) {
  return invoice?.id || invoice?.invoice_id || invoice?.data?.id || invoice?.data?.invoice_id || null;
}

async function observeUgigDeliveries({
  deliveries,
  applications,
  listInvoices,
  isPullRequestMerged,
  createInvoice,
  processPaidInvoice,
}) {
  if (!Array.isArray(deliveries)) throw new Error("deliveries must be an array");
  if (!Array.isArray(applications)) throw new Error("applications must be an array");
  deliveries.forEach(validateDelivery);

  const result = {
    deliveries_seen: deliveries.length,
    pending: 0,
    waiting_for_merge: 0,
    setup_required: 0,
    invoiced: 0,
    invoice_created: 0,
    paid: 0,
    revenue_recorded: 0,
    revenue_duplicates: 0,
    rejected: 0,
    invoices: [],
  };

  for (const delivery of deliveries) {
    const application = applications.find((candidate) =>
      candidate?.id === delivery.application_id &&
      (!candidate.gig_id || candidate.gig_id === delivery.gig_id));

    if (!application) {
      result.rejected += 1;
      continue;
    }
    if (application.status === "pending") {
      result.pending += 1;
      continue;
    }
    if (!["accepted", "completed"].includes(application.status)) {
      result.rejected += 1;
      continue;
    }

    const invoices = await listInvoices(delivery.gig_id);
    const existing = invoices.find((invoice) => invoice?.application_id === delivery.application_id);
    if (existing) {
      result.invoiced += 1;
      if (String(existing.status).toLowerCase() === "paid") {
        if (typeof processPaidInvoice !== "function") {
          throw new Error("a settlement processor is required for a paid invoice");
        }
        const settlement = await processPaidInvoice(delivery, existing, application);
        if (!settlement || settlement.ok !== true) {
          throw new Error("paid invoice settlement processor did not verify the payout");
        }
        result.paid += 1;
        if (settlement.duplicate) result.revenue_duplicates += 1;
        else result.revenue_recorded += 1;
      } else if (application.status === "completed") {
        result.rejected += 1;
      }
      const id = invoiceId(existing);
      if (id) result.invoices.push(id);
      continue;
    }

    if (application.status === "completed") {
      result.rejected += 1;
      continue;
    }

    if (delivery.category === "code") {
      const merged = await Promise.all(delivery.pr_links.map(isPullRequestMerged));
      if (merged.some((value) => value !== true)) {
        result.waiting_for_merge += 1;
        continue;
      }
    }

    const isCode = delivery.category === "code";
    const payload = {
      application_id: delivery.application_id,
      amount: delivery.amount_usd,
      currency: "USD",
      payment_currency: delivery.payment_currency,
      merchant_wallet_address: delivery.merchant_wallet_address,
      notes: isCode
        ? delivery.description
        : `${delivery.description}\nProof: ${delivery.proof_urls.join(", ")}`,
      category: delivery.category,
      items: [{
        description: delivery.description,
        quantity: 1,
        unit_price: delivery.amount_usd,
      }],
    };
    if (isCode) {
      payload.pr_links = delivery.pr_links;
      payload.items[0].link = delivery.pr_links[0];
    }
    let created;
    try {
      created = await createInvoice(delivery.gig_id, payload);
    } catch (error) {
      if (error?.code === "UGIG_SETUP_REQUIRED") {
        result.setup_required += 1;
        continue;
      }
      throw error;
    }
    const id = invoiceId(created);
    if (!id) throw new Error("uGig invoice creation response did not include an invoice id");
    result.invoice_created += 1;
    result.invoices.push(id);
  }

  return result;
}

module.exports = { observeUgigDeliveries, validateDelivery };
