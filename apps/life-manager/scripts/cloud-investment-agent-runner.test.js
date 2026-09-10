"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { TASK_LABELS, runCloudAgent } = require("./cloud-investment-agent-runner.js");

const schema = {
  type: "object", additionalProperties: false,
  properties: {
    candidate_ref: { type: "string" }, probability_profit: { type: "number" },
    expected_gain_usd: { type: "number" }, reason: { type: "string" },
  },
  required: ["candidate_ref", "probability_profit", "expected_gain_usd", "reason"],
};

test("cloud adapter accepts only allocation and live-position decision labels", () => {
  assert.deepEqual([...TASK_LABELS].sort(), ["alpaca-allocation", "alpaca-position"]);
});

test("cloud adapter sends the core prompt and Gemini-compatible schema, then persists a private result", async () => {
  const evidenceDir = fs.mkdtempSync(path.join(os.tmpdir(), "investment-cloud-agent-"));
  const calls = [];
  const value = { candidate_ref: "NO_TRADE", probability_profit: 0, expected_gain_usd: 0, reason: "根拠不足" };
  const result = await runCloudAgent({ prompt: "unchanged core allocation prompt", schema, evidenceDir,
    apiKey: "fixture-key", fetchImpl: async (url, request) => {
      calls.push({ url, request });
      return { ok: true, json: async () => ({ candidates: [{ content: { parts: [{ text: JSON.stringify(value) }] } }] }) };
    } });
  assert.deepEqual(JSON.parse(fs.readFileSync(result.result_path, "utf8")), value);
  assert.equal(fs.statSync(result.result_path).mode & 0o777, 0o600);
  const body = JSON.parse(calls[0].request.body);
  assert.equal(body.contents[0].parts[0].text, "unchanged core allocation prompt");
  const { additionalProperties, ...supportedSchema } = schema;
  assert.equal(additionalProperties, false);
  assert.deepEqual(body.generationConfig.responseSchema, supportedSchema);
  assert.equal(calls[0].request.headers["x-goog-api-key"], "fixture-key");
  assert.equal(JSON.stringify(result).includes("fixture-key"), false);
});

test("cloud adapter rejects provider/schema drift", async () => {
  const evidenceDir = fs.mkdtempSync(path.join(os.tmpdir(), "investment-cloud-agent-bad-"));
  await assert.rejects(runCloudAgent({ prompt: "allocation prompt", schema, evidenceDir,
    apiKey: "key", fetchImpl: async () => ({ ok: true, json: async () => ({ candidates: [{ content: { parts: [{ text: '{"candidate_ref":"NO_TRADE"}' }] } }] }) }) }), /invalid/);
});

test("cloud adapter keeps strict local additional-property validation", async () => {
  const evidenceDir = fs.mkdtempSync(path.join(os.tmpdir(), "investment-cloud-agent-extra-"));
  const value = { candidate_ref: "NO_TRADE", probability_profit: 0, expected_gain_usd: 0,
    reason: "根拠不足", unexpected: true };
  await assert.rejects(runCloudAgent({ prompt: "allocation prompt", schema, evidenceDir,
    apiKey: "key", fetchImpl: async () => ({ ok: true, json: async () => ({
      candidates: [{ content: { parts: [{ text: JSON.stringify(value) }] } }],
    }) }) }), /invalid/);
});
