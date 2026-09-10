#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const GEMINI_MODEL = "gemini-2.5-flash";
const GEMINI_URL = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`;
const TASK_LABELS = new Set(["alpaca-allocation", "alpaca-position"]);

function invalid() { throw new Error("cloud investment agent response invalid"); }

function geminiSchema(value) {
  if (Array.isArray(value)) return value.map(geminiSchema);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value)
    .filter(([key]) => key !== "additionalProperties")
    .map(([key, item]) => [key, geminiSchema(item)]));
}

function validate(value, schema) {
  if (!schema || schema.type !== "object" || !value || typeof value !== "object" || Array.isArray(value)) invalid();
  const properties = schema.properties || {};
  if (schema.additionalProperties === false
    && Object.keys(value).some((key) => !Object.hasOwn(properties, key))) invalid();
  for (const key of schema.required || []) if (!Object.hasOwn(value, key)) invalid();
  for (const [key, item] of Object.entries(value)) {
    const expected = properties[key] && properties[key].type;
    if ((expected === "string" && typeof item !== "string")
      || (expected === "number" && (typeof item !== "number" || !Number.isFinite(item)))
      || (expected === "boolean" && typeof item !== "boolean")) invalid();
  }
  return value;
}

async function runCloudAgent(input, deps = {}) {
  const prompt = String(input && input.prompt || "");
  const apiKey = String(input && input.apiKey || process.env.GEMINI_API_KEY || "").trim();
  const evidenceDir = path.resolve(String(input && input.evidenceDir || ""));
  const fetchImpl = input.fetchImpl || deps.fetchImpl || globalThis.fetch;
  if (!apiKey || prompt.length < 16 || prompt.length > 100_000
    || !path.isAbsolute(evidenceDir) || typeof fetchImpl !== "function") invalid();
  const response = await fetchImpl(GEMINI_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
    body: JSON.stringify({
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: {
        responseMimeType: "application/json", responseSchema: geminiSchema(input.schema),
        temperature: 0, maxOutputTokens: 512, thinkingConfig: { thinkingBudget: 0 },
      },
    }),
    signal: AbortSignal.timeout(120_000),
  });
  if (!response || response.ok !== true) throw new Error("cloud investment agent unavailable");
  const body = await response.json();
  const text = body && body.candidates && body.candidates[0]
    && body.candidates[0].content && body.candidates[0].content.parts
    && body.candidates[0].content.parts.map((part) => part.text || "").join("");
  let value;
  try { value = validate(JSON.parse(text || ""), input.schema); } catch { invalid(); }
  fs.mkdirSync(evidenceDir, { recursive: true, mode: 0o700 });
  fs.chmodSync(evidenceDir, 0o700);
  const resultPath = path.join(evidenceDir, "result.json");
  fs.writeFileSync(resultPath, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  fs.chmodSync(resultPath, 0o600);
  return Object.freeze({ status: "success", selected_provider: "gemini",
    selected_model: GEMINI_MODEL, result_path: resultPath });
}

function args(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--read-only" || value === "--prompt-stdin") result[value.slice(2)] = true;
    else if (value.startsWith("--")) result[value.slice(2)] = argv[++index];
    else invalid();
  }
  return result;
}

async function main() {
  const parsed = args(process.argv.slice(2));
  if (parsed["task-class"] !== "diagnostic-agent" || parsed.loop !== "alpaca-investment"
    || !TASK_LABELS.has(parsed["task-label"]) || parsed["prompt-stdin"] !== true
    || parsed["read-only"] !== true) invalid();
  const schema = JSON.parse(fs.readFileSync(path.resolve(parsed.schema), "utf8"));
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const result = await runCloudAgent({ prompt: Buffer.concat(chunks).toString("utf8"),
    schema, evidenceDir: parsed["evidence-dir"] });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (require.main === module) main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});

module.exports = { TASK_LABELS, runCloudAgent };
