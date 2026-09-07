"use strict";
const fs = require("node:fs"); const path = require("node:path"); const crypto = require("node:crypto");
const { collectLocalAgentUsageBatch } = require("./cfo-local-agent-usage-collector.js"); const { createCfoSupabaseRpc } = require("./cfo-supabase-rpc.js");
const { fail, plain, timestamp, freeze } = createCfoSupabaseRpc("cfo_local_agent_usage_batch_store_invalid:");

function validate(stateRoot, collectedAt, options) {
  if (typeof stateRoot !== "string" || stateRoot.length === 0 || stateRoot.trim() !== stateRoot || !path.isAbsolute(stateRoot) || path.parse(stateRoot).root === stateRoot || path.resolve(stateRoot) !== stateRoot) fail("invalid_state_root");
  if (!timestamp(collectedAt)) fail("invalid_collected_at");
  if (!plain(options)) fail("invalid_options"); const keys = Reflect.ownKeys(options);
  if (keys.length > 1 || keys.some((key) => key !== "fsyncImpl")) fail("invalid_options");
  if (keys.length === 1) { const descriptor = Object.getOwnPropertyDescriptor(options, "fsyncImpl"); if (!descriptor || !descriptor.enumerable || !Object.prototype.hasOwnProperty.call(descriptor, "value") || typeof descriptor.value !== "function") fail("invalid_options"); return descriptor.value; }
  return fs.fsyncSync;
}

function collectAndWriteLocalAgentUsageBatch(stateRoot, collectedAt, sourceId, bytes, priorSourceState, options = {}) {
  const fsyncImpl = validate(stateRoot, collectedAt, options), batch = collectLocalAgentUsageBatch(sourceId, bytes, priorSourceState);
  const record = { schema_version: 1, collected_at: collectedAt, mapping_id: batch.mapping_id, prior_source_state: priorSourceState, source_state: batch.source_state, events: batch.events, delta_counts: batch.counts, coverage_exceptions: batch.coverage_exceptions };
  const content = Buffer.from(JSON.stringify(record)), record_id = crypto.createHash("sha256").update(content).digest("hex"), dir = path.join(stateRoot, "cfo", "local-agent-usage", sourceId), finalPath = path.join(dir, `${record_id}.json`);
  let temporary = null, fileFd = null;
  try {
    for (const fixed of [path.join(stateRoot, "cfo"), path.join(stateRoot, "cfo", "local-agent-usage"), dir]) { fs.mkdirSync(fixed, { recursive: true, mode: 0o700 }); fs.chmodSync(fixed, 0o700); }
    if (fs.existsSync(finalPath)) { if (!fs.readFileSync(finalPath).equals(content)) throw new Error("immutable_record_conflict"); }
    else {
      temporary = path.join(dir, `.${record_id}.${process.pid}.${Date.now()}.${crypto.randomBytes(8).toString("hex")}.tmp`); fileFd = fs.openSync(temporary, "wx", 0o600); try { fs.writeFileSync(fileFd, content); fsyncImpl(fileFd); } finally { fs.closeSync(fileFd); fileFd = null; }
      fs.renameSync(temporary, finalPath); temporary = null;
    }
    const directoryFd = fs.openSync(dir, "r"); try { fsyncImpl(directoryFd); } finally { fs.closeSync(directoryFd); }
  } catch { if (fileFd !== null) try { fs.closeSync(fileFd); } catch {} if (temporary !== null) try { fs.unlinkSync(temporary); } catch {} fail("write_failed"); }
  return freeze({ record_id, source_id: sourceId, byte_offset: batch.source_state.byte_offset, event_count: batch.events.length, mapping_id: batch.mapping_id });
}

module.exports = { collectAndWriteLocalAgentUsageBatch };
