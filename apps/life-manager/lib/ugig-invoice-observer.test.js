"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { observeUgigDeliveries } = require("./ugig-invoice-observer.js");

const DELIVERY = {
  application_id: "5e315cfd-33fc-433b-a5f0-3cfcdc27a9a4",
  gig_id: "2b410cad-7cc9-44fd-b2f1-843d9eae6c24",
  amount_usd: 1,
  payment_currency: "sol",
  merchant_wallet_address: "71FfqFniYoMsWZb1qFeQDb1fk2xqvajzivpsnMb44gTf",
  category: "code",
  pr_links: ["https://github.com/profullstack/aiornot.vote/pull/100"],
  description: "RSS enclosure MIME fix",
};

const MARKETING_DELIVERY = {
  application_id: "7e636f57-4d5a-4f54-b6d2-31ba1a86c5bd",
  gig_id: "1eea7af1-3089-443c-9975-74212c53683f",
  amount_usd: 0.25,
  payment_currency: "sol",
  merchant_wallet_address: "71FfqFniYoMsWZb1qFeQDb1fk2xqvajzivpsnMb44gTf",
  category: "marketing",
  proof_urls: ["https://pairux.com/@moshcoding"],
  description: "Subscribed to the requested PairUX channel",
};

test("pending applications are observed without reading invoices or creating one", async () => {
  const result = await observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "pending" }],
    listInvoices: async () => { throw new Error("must not list invoices before acceptance"); },
    isPullRequestMerged: async () => { throw new Error("must not query PR before acceptance"); },
    createInvoice: async () => { throw new Error("must not create before acceptance"); },
  });

  assert.deepEqual(result, {
    deliveries_seen: 1,
    pending: 1,
    waiting_for_merge: 0,
    setup_required: 0,
    invoiced: 0,
    invoice_created: 0,
    paid: 0,
    revenue_recorded: 0,
    revenue_duplicates: 0,
    rejected: 0,
    invoices: [],
  });
});

test("accepted code work waits for every pull request to be merged", async () => {
  let creates = 0;
  const result = await observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "accepted" }],
    listInvoices: async () => [],
    isPullRequestMerged: async () => false,
    createInvoice: async () => { creates += 1; },
  });

  assert.equal(result.waiting_for_merge, 1);
  assert.equal(result.invoice_created, 0);
  assert.equal(creates, 0);
});

test("accepted merged work creates one exact capped invoice", async () => {
  const creates = [];
  const result = await observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "accepted" }],
    listInvoices: async () => [],
    isPullRequestMerged: async () => true,
    createInvoice: async (gigId, payload) => {
      creates.push({ gigId, payload });
      return { id: "invoice-1", status: "sent" };
    },
  });

  assert.equal(result.invoice_created, 1);
  assert.deepEqual(result.invoices, ["invoice-1"]);
  assert.deepEqual(creates, [{
    gigId: DELIVERY.gig_id,
    payload: {
      application_id: DELIVERY.application_id,
      amount: 1,
      currency: "USD",
      payment_currency: "sol",
      merchant_wallet_address: DELIVERY.merchant_wallet_address,
      notes: DELIVERY.description,
      category: "code",
      pr_links: DELIVERY.pr_links,
      items: [{
        description: DELIVERY.description,
        quantity: 1,
        unit_price: 1,
        link: DELIVERY.pr_links[0],
      }],
    },
  }]);
});

test("an existing invoice is exactly-once and paid status is surfaced", async () => {
  let creates = 0;
  const settlements = [];
  const result = await observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "accepted" }],
    listInvoices: async () => [{
      id: "invoice-paid",
      application_id: DELIVERY.application_id,
      status: "paid",
    }],
    isPullRequestMerged: async () => { throw new Error("existing invoice needs no PR query"); },
    createInvoice: async () => { creates += 1; },
    processPaidInvoice: async (delivery, invoice, application) => {
      settlements.push({ delivery, invoice, application });
      return { ok: true, duplicate: false };
    },
  });

  assert.equal(result.invoiced, 1);
  assert.equal(result.paid, 1);
  assert.equal(result.revenue_recorded, 1);
  assert.equal(result.revenue_duplicates, 0);
  assert.equal(result.invoice_created, 0);
  assert.equal(creates, 0);
  assert.equal(settlements.length, 1);
  assert.equal(settlements[0].delivery.application_id, DELIVERY.application_id);
  assert.deepEqual(result.invoices, ["invoice-paid"]);
});

test("a completed application still processes its paid invoice and never creates another", async () => {
  let creates = 0;
  const result = await observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "completed" }],
    listInvoices: async () => [{
      id: "invoice-completed",
      application_id: DELIVERY.application_id,
      status: "paid",
    }],
    isPullRequestMerged: async () => { throw new Error("completed work needs no PR query"); },
    createInvoice: async () => { creates += 1; },
    processPaidInvoice: async () => ({ ok: true, duplicate: true }),
  });

  assert.equal(result.invoiced, 1);
  assert.equal(result.paid, 1);
  assert.equal(result.revenue_recorded, 0);
  assert.equal(result.revenue_duplicates, 1);
  assert.equal(creates, 0);
});

test("a paid invoice cannot be surfaced without an independent settlement processor", async () => {
  await assert.rejects(() => observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "completed" }],
    listInvoices: async () => [{
      id: "invoice-unverified",
      application_id: DELIVERY.application_id,
      status: "paid",
    }],
    isPullRequestMerged: async () => true,
    createInvoice: async () => ({}),
  }), /settlement processor/i);
});

test("a completed application without a paid invoice fails closed without creating one", async () => {
  let creates = 0;
  const result = await observeUgigDeliveries({
    deliveries: [DELIVERY],
    applications: [{ id: DELIVERY.application_id, gig_id: DELIVERY.gig_id, status: "completed" }],
    listInvoices: async () => [{
      id: "invoice-not-paid",
      application_id: DELIVERY.application_id,
      status: "sent",
    }],
    isPullRequestMerged: async () => true,
    createInvoice: async () => { creates += 1; },
    processPaidInvoice: async () => { throw new Error("must not process unpaid invoice"); },
  });

  assert.equal(result.rejected, 1);
  assert.equal(result.paid, 0);
  assert.equal(creates, 0);
});

test("accepted non-code delivery invoices without querying GitHub", async () => {
  const creates = [];
  const result = await observeUgigDeliveries({
    deliveries: [MARKETING_DELIVERY],
    applications: [{
      id: MARKETING_DELIVERY.application_id,
      gig_id: MARKETING_DELIVERY.gig_id,
      status: "accepted",
    }],
    listInvoices: async () => [],
    isPullRequestMerged: async () => {
      throw new Error("non-code work has no GitHub merge gate");
    },
    createInvoice: async (gigId, payload) => {
      creates.push({ gigId, payload });
      return { id: "invoice-marketing", status: "sent" };
    },
  });

  assert.equal(result.invoice_created, 1);
  assert.deepEqual(creates, [{
    gigId: MARKETING_DELIVERY.gig_id,
    payload: {
      application_id: MARKETING_DELIVERY.application_id,
      amount: 0.25,
      currency: "USD",
      payment_currency: "sol",
      merchant_wallet_address: MARKETING_DELIVERY.merchant_wallet_address,
      notes: "Subscribed to the requested PairUX channel\nProof: https://pairux.com/@moshcoding",
      category: "marketing",
      items: [{
        description: MARKETING_DELIVERY.description,
        quantity: 1,
        unit_price: 0.25,
      }],
    },
  }]);
});

test("malformed delivery configuration fails closed", async () => {
  await assert.rejects(() => observeUgigDeliveries({
    deliveries: [{ ...DELIVERY, amount_usd: 0 }],
    applications: [],
    listInvoices: async () => [],
    isPullRequestMerged: async () => true,
    createInvoice: async () => ({}),
  }), /amount_usd/);
});

test("non-code delivery without a public proof URL fails closed", async () => {
  await assert.rejects(() => observeUgigDeliveries({
    deliveries: [{ ...MARKETING_DELIVERY, proof_urls: [] }],
    applications: [],
    listInvoices: async () => [],
    isPullRequestMerged: async () => true,
    createInvoice: async () => ({}),
  }), /proof_urls/);
});
