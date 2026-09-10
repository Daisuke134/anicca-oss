"use strict";

function database(opts) {
  if (typeof opts.query === "function") return opts.query;
  throw new Error("Agent Economy control store unavailable");
}

function cycleFromRefs(refs) {
  const value = String(refs && refs.cycle_ref || "").split("/").at(-1);
  return /^\d+$/.test(value) ? Number(value) : null;
}

function project(row) {
  if (!row) return { citizen: null, pausedAt: null, latestJob: null, latestReceipt: null };
  const receipt = row.receipt && typeof row.receipt === "object" ? row.receipt : null;
  return {
    citizen: { walletAddress: row.wallet_address },
    pausedAt: row.agent_economy_paused_at ? new Date(row.agent_economy_paused_at).toISOString() : null,
    latestJob: row.job_id ? {
      status: row.job_status,
      cycle: cycleFromRefs(row.input_refs),
      availableAt: new Date(row.available_at).toISOString(),
    } : null,
    latestReceipt: row.receipt_outcome ? {
      outcome: row.receipt_outcome,
      cycle: receipt && Number.isSafeInteger(receipt.cycle) ? receipt.cycle : null,
      profitable: receipt && receipt.wake && typeof receipt.wake.profitable === "boolean"
        ? receipt.wake.profitable : null,
    } : null,
  };
}

function createAgentEconomyControlStore(opts = {}) {
  const query = database(opts);
  return Object.freeze({
    async read(tenantId) {
      const rows = (await query(`
        SELECT c.wallet_address, c.agent_economy_paused_at,
               jobs.job_id, jobs.status AS job_status, jobs.input_refs, jobs.available_at,
               receipts.outcome AS receipt_outcome, receipts.receipt
        FROM public.lm_cloud_citizens AS c
        LEFT JOIN LATERAL (
          SELECT job_id, status, input_refs, available_at
          FROM public.lm_runtime_jobs
          WHERE tenant_id = c.tenant_id AND loop_id = 'agent-economy'
          ORDER BY created_at DESC LIMIT 1
        ) AS jobs ON true
        LEFT JOIN LATERAL (
          SELECT outcome, receipt
          FROM public.lm_runtime_job_receipts
          WHERE tenant_id = c.tenant_id
            AND receipt->>'kind' IN ('agent_economy_wake', 'agent_economy_paused')
          ORDER BY created_at DESC LIMIT 1
        ) AS receipts ON true
        WHERE c.tenant_id = $1 LIMIT 1
      `, [String(tenantId)])).rows;
      return project(rows[0] || null);
    },
    async pause(tenantId) {
      const rows = (await query(`
        UPDATE public.lm_cloud_citizens
        SET agent_economy_paused_at = COALESCE(agent_economy_paused_at, clock_timestamp())
        WHERE tenant_id = $1
        RETURNING agent_economy_paused_at
      `, [String(tenantId)])).rows;
      if (rows.length !== 1) throw new Error("Agent Economy citizen unavailable");
      return { pausedAt: new Date(rows[0].agent_economy_paused_at).toISOString() };
    },
    async isPaused(tenantId) {
      const rows = (await query(`
        SELECT agent_economy_paused_at FROM public.lm_cloud_citizens
        WHERE tenant_id = $1 LIMIT 1
      `, [String(tenantId)])).rows;
      if (rows.length !== 1) throw new Error("Agent Economy citizen unavailable");
      return rows[0].agent_economy_paused_at != null;
    },
  });
}

function economyReply(snapshot) {
  if (!snapshot) return { text: "💸 Agent Economy\nStatus is temporarily unavailable." };
  if (!snapshot.citizen) {
    return {
      text: "💸 Agent Economy\nSetup is not complete yet.",
      extra: { reply_markup: { inline_keyboard: [[{ text: "Start setup", callback_data: "economy:setup" }]] } },
    };
  }
  const job = snapshot.latestJob;
  const receipt = snapshot.latestReceipt;
  const text = [
    "💸 Agent Economy",
    `Status: ${snapshot.pausedAt ? "paused" : "running"}`,
    `Wallet: ${snapshot.citizen.walletAddress}`,
    `Latest job: ${job ? `${job.status}${job.cycle == null ? "" : ` (cycle ${job.cycle})`}` : "none"}`,
    `Latest receipt: ${receipt ? `${receipt.outcome}${receipt.cycle == null ? "" : ` (cycle ${receipt.cycle})`}` : "none"}`,
    `Profitable: ${receipt && receipt.profitable != null ? (receipt.profitable ? "yes" : "no") : "not verified"}`,
  ].join("\n");
  return snapshot.pausedAt ? { text } : {
    text,
    extra: { reply_markup: { inline_keyboard: [[{ text: "Emergency pause", callback_data: "economy:pause" }]] } },
  };
}

module.exports = { createAgentEconomyControlStore, economyReply };
