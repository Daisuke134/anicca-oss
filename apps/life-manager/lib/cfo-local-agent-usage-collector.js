"use strict";

const { createCfoSupabaseRpc } = require("./cfo-supabase-rpc.js");
const { fail, freeze } = createCfoSupabaseRpc("cfo_local_agent_collector_invalid:");
const { scanLocalAgentUsageAppend } = require("./cfo-local-agent-usage-cursor.js");
const { resolveLocalAgentUsageAttribution } = require("./cfo-local-agent-usage-attribution.js");
const { reduceLocalAgentUsageEvents } = require("./ledger.js");

function collectLocalAgentUsageBatch(sourceId, bytes, previousState) {
  const scanned = scanLocalAgentUsageAppend(sourceId, bytes, previousState);
  try {
    const pairs = scanned.pairs.map(({ input, context }) => ({ input, context: { ...context, financial_unit_id: resolveLocalAgentUsageAttribution(input.loop, input.task_label).financial_unit_id } }));
    const reduced = reduceLocalAgentUsageEvents(pairs);
    const unattributed_rows = reduced.events.filter(({ financial_unit_id }) => financial_unit_id === null).length;
    const attributed_rows = reduced.events.length - unattributed_rows;
    const counts = { ...reduced.counts, attributed_rows, unattributed_rows };
    const coverage_exceptions = [...new Set([...scanned.coverage_exceptions, ...reduced.coverage_exceptions, ...(unattributed_rows ? ["unattributed_usage"] : [])])].sort();
    return freeze({ events: reduced.events, source_state: scanned.state, mapping_id: "local_agent_usage_v1", counts, coverage_exceptions });
  } catch { fail("invalid_batch"); }
}

module.exports = { collectLocalAgentUsageBatch };
