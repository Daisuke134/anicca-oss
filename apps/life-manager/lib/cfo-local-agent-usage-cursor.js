"use strict";
const crypto = require("node:crypto");
const { types: { isProxy } } = require("node:util");
const { normalizeLocalAgentUsageEvent } = require("./ledger.js");
const { createCfoSupabaseRpc } = require("./cfo-supabase-rpc.js");
const ERROR_PREFIX = "cfo_local_agent_usage_cursor_invalid:", SOURCES = new Set(["life_manager_agent_usage", "anicca_agent_usage"]), STATE_KEYS = new Set(["schema_version", "source_id", "byte_offset", "prefix_sha256", "observed_file_size"]);
const { fail, internal, exact, freeze } = createCfoSupabaseRpc(ERROR_PREFIX);
function hash(bytes) { return crypto.createHash("sha256").update(bytes).digest("hex"); }
function exactState(value, sourceId) {
  if (isProxy(value)) fail("proxy");
  exact(value, STATE_KEYS, "invalid_state");
  if (value.schema_version !== 1) fail("invalid_schema");
  if (value.source_id !== sourceId) fail("source_mismatch");
  if (!Number.isSafeInteger(value.byte_offset) || value.byte_offset < 0) fail("invalid_offset");
  if (!Number.isSafeInteger(value.observed_file_size) || value.observed_file_size < 0 || value.byte_offset > value.observed_file_size) fail("invalid_size");
  if (typeof value.prefix_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(value.prefix_sha256)) fail("invalid_prefix");
  return value;
}
function result(pairs, state, coverage_exceptions) { return freeze({ pairs, state: { ...state }, coverage_exceptions }); }
function initial(sourceId) { return { schema_version: 1, source_id: sourceId, byte_offset: 0, prefix_sha256: hash(Buffer.alloc(0)), observed_file_size: 0 }; }
function scanLocalAgentUsageAppend(sourceId, bytes, previousState) {
  try {
    if (isProxy(bytes)) fail("proxy");
    if (typeof sourceId !== "string" || !SOURCES.has(sourceId)) fail("invalid_source");
    if (!Buffer.isBuffer(bytes)) fail("invalid_bytes");
    const prior = previousState === null ? initial(sourceId) : exactState(previousState, sourceId);
    if (bytes.length < prior.observed_file_size) return result([], prior, ["source_truncated"]);
    if (hash(bytes.subarray(0, prior.byte_offset)) !== prior.prefix_sha256) return result([], prior, ["source_rewritten"]);
    const pairs = []; let offset = prior.byte_offset;
    for (let end = offset; end < bytes.length; end += 1) if (bytes[end] === 10) {
      let input; try { input = JSON.parse(bytes.subarray(offset, end).toString("utf8")); normalizeLocalAgentUsageEvent(input, { source_row_ref: hash(Buffer.from(`cfo-local-agent-row-v1\0${sourceId}\0${offset}`)), financial_unit_id: null }); } catch { return result([], prior, ["invalid_source_row"]); }
      pairs.push({ input, context: { source_row_ref: hash(Buffer.from(`cfo-local-agent-row-v1\0${sourceId}\0${offset}`)), financial_unit_id: null } }); offset = end + 1;
    }
    const state = { schema_version: 1, source_id: sourceId, byte_offset: offset, prefix_sha256: hash(bytes.subarray(0, offset)), observed_file_size: bytes.length };
    return result(pairs, state, offset < bytes.length ? ["partial_tail"] : []);
  } catch (error) { if (internal(error)) throw error; fail("invalid_input"); }
}
module.exports = { scanLocalAgentUsageAppend };
