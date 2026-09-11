"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { createX402CostObserver } = require("./x402-cost-observer.js");

function headers(values) { return { get: (name) => values[name.toLowerCase()] || null }; }
function required(overrides = {}) {
  return Buffer.from(JSON.stringify({ resource: { url: "https://blockrun.ai/api/v1/chat/completions" },
    accepts: [{ amount: "1234", network: "eip155:8453",
      asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", ...overrides }] })).toString("base64");
}

test("joins one 402 requirement to its successful provider receipt as exact compute cost", async () => {
  const records = [];
  const observer = createX402CostObserver({ subjectId: "tenant-a", now: () => "2026-09-11T04:00:00Z",
    store: { append: async (record) => { records.push(record); return { created: true }; } } });
  const url = "https://blockrun.ai/api/v1/chat/completions";
  const body = JSON.stringify({ model: "test" });
  await observer.observe(url, { body, headers: {} }, { status: 402, ok: false,
    headers: headers({ "payment-required": required() }) });
  const result = await observer.observe(url, { body, headers: { "PAYMENT-SIGNATURE": "signed" } },
    { status: 200, ok: true, headers: headers({ "payment-response": "settled-receipt" }) });
  assert.equal(result.recorded, true);
  assert.deepEqual([records[0].kind, records[0].amount_minor, records[0].currency],
    ["business_cost", 1234, "USDC"]);
  assert.match(records[0].verification.evidence_refs[0], /^x402:\/\/receipt\/[a-f0-9]{64}$/);
});

test("never records estimates, failed responses, unsupported chains, or missing receipts", async () => {
  let writes = 0;
  const observer = createX402CostObserver({ subjectId: "tenant-a",
    store: { append: async () => { writes += 1; } } });
  const url = "https://blockrun.ai/api/v1/chat/completions";
  await observer.observe(url, { body: "a" }, { status: 402, ok: false,
    headers: headers({ "payment-required": required({ network: "eip155:1" }) }) });
  await observer.observe(url, { body: "a", headers: { "payment-signature": "x" } },
    { status: 200, ok: true, headers: headers({ "payment-response": "receipt" }) });
  await observer.observe(url, { body: "b" }, { status: 402, ok: false,
    headers: headers({ "payment-required": required() }) });
  await observer.observe(url, { body: "b", headers: { "payment-signature": "x" } },
    { status: 200, ok: true, headers: headers({}) });
  assert.equal(writes, 0);
});

test("retains the provider requirement for later pre-authorized calls of the same model", async () => {
  const records = [];
  const observer = createX402CostObserver({ subjectId: "tenant-a", now: () => "2026-09-11T04:00:00Z",
    store: { append: async (record) => { records.push(record); return { created: true }; } } });
  const url = "https://blockrun.ai/api/v1/chat/completions";
  await observer.observe(url, { body: JSON.stringify({ model: "m", messages: ["first"] }) },
    { status: 402, ok: false, headers: headers({ "payment-required": required() }) });
  for (const message of ["first", "second"]) {
    await observer.observe(url, { body: JSON.stringify({ model: "m", messages: [message] }),
      headers: { "payment-signature": "signed" } },
    { status: 200, ok: true, headers: headers({ "payment-response": `receipt-${message}` }) });
  }
  assert.equal(records.length, 2);
});

test("captures the SDK's body-form 402 without consuming its response", async () => {
  const records = [];
  const observer = createX402CostObserver({ subjectId: "tenant-a",
    store: { append: async (record) => { records.push(record); return { created: true }; } } });
  const url = "https://blockrun.ai/api/v1/chat/completions";
  const body = JSON.stringify({ model: "m" });
  const requirement = JSON.parse(Buffer.from(required(), "base64").toString("utf8"));
  let clones = 0;
  await observer.observe(url, { body }, { status: 402, ok: false, headers: headers({}),
    clone() { clones += 1; return { json: async () => requirement }; } });
  await observer.observe(url, { body, headers: { "payment-signature": "signed" } },
    { status: 200, ok: true, headers: headers({ "payment-response": "receipt" }) });
  assert.equal(clones, 1);
  assert.equal(records.length, 1);
});
