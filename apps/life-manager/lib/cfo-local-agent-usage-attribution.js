"use strict";

const { createCfoSupabaseRpc } = require("./cfo-supabase-rpc.js");
const { fail, freeze } = createCfoSupabaseRpc("cfo_local_agent_attribution_invalid:");
const RULES = freeze([
  ["gig", "gig-*", "gig_work"], ["gig-loop", "self-fix-gig-loop", "gig_work"], ["gig-oauth-parity", "gig-oauth-*", "gig_work"], ["freelancer-work-sync", "freelancer-*", "gig_work"], ["bounty", "bounty-*", "gig_work"],
  ["job-search", "job-search-*", "job_income"], ["capafy", "capafy-*", "capafy_marketplace"], ["capafy-loop", "self-fix-capafy-loop", "capafy_marketplace"], ["capafy-verify", "budget-guard-probe", "capafy_marketplace"],
  ["life-manager", "life-manager-*", "life_manager_saas"], ["life-manager-dev", "life-manager-dev-*", "life_manager_saas"], ["life-manager-dev-promote", "life-manager-promote-*", "life_manager_saas"], ["reddit", "reddit-loop-*", "life_manager_saas"], ["reddit-loop", "self-fix-reddit-loop", "life_manager_saas"],
  ["larry-marketing", "larry-*", "anicca_ios"], ["honne-marketing", "reelclaw-honne-*", "anicca_ios"],
]);
const matches = (actual, pattern) => pattern.endsWith("*") ? actual.startsWith(pattern.slice(0, -1)) : actual === pattern;

function resolveLocalAgentUsageAttribution(loop, taskLabel) {
  if (typeof loop !== "string" || loop.length === 0 || loop.trim() !== loop) fail("invalid_loop");
  if (typeof taskLabel !== "string" || taskLabel.length === 0 || taskLabel.trim() !== taskLabel) fail("invalid_task_label");
  const match = RULES.find(([ruleLoop, pattern]) => ruleLoop === loop && matches(taskLabel, pattern));
  const financial_unit_id = match ? match[2] : null;
  return freeze({ mapping_id: "local_agent_usage_v1", financial_unit_id, attribution_status: financial_unit_id === null ? "unattributed" : "attributed" });
}

module.exports = { resolveLocalAgentUsageAttribution };
