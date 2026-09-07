"use strict";

const { sendMessage } = require("./telegram.js");

function noticeCopy(notice) {
  const used = Number(notice && notice.used), limit = Number(notice && notice.limit);
  const resetAt = String(notice && notice.resetAt || "");
  if (!Number.isInteger(used) || !Number.isInteger(limit) || limit <= 0 || !/^\d{4}-\d{2}-\d{2}$/.test(resetAt)) return null;
  const plan = notice.paid === true ? "Plus利用枠" : "無料利用枠";
  if (notice.kind === "eighty") {
    return `今月の${plan}は ${used}/${limit} 回です。残り${Math.max(0, limit - used)}回。${resetAt}に戻ります。`;
  }
  if (notice.kind === "exhausted" && notice.paid === true) {
    return `今月の${plan} ${used}/${limit} 回を使い切りました。設定・カレンダー・保存済みの経路はそのまま使えます。${resetAt}に戻ります。`;
  }
  if (notice.kind === "exhausted") {
    return `今月の${plan} ${used}/${limit} 回を使い切りました。設定・カレンダー・保存済みの経路はそのまま使えます。${resetAt}に戻ります。続ける場合は /subscribe を送ってください。`;
  }
  return null;
}

async function rpc(name, body, deps) {
  const fetchImpl = deps.fetchImpl === undefined ? global.fetch : deps.fetchImpl;
  if (!deps.supaUrl || !deps.supaKey || typeof fetchImpl !== "function") return null;
  try {
    const response = await fetchImpl(`${String(deps.supaUrl).replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
      method: "POST",
      headers: { apikey: deps.supaKey, Authorization: `Bearer ${deps.supaKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response || response.ok !== true) return null;
    return await response.json();
  } catch { return null; }
}

async function deliverAllowanceNotice(user, deps = {}) {
  if (!user || !user.uid || !user.telegram_chat_id || user.notifications_enabled === false) return { status: "skipped" };
  const token = deps.telegramToken === undefined ? process.env.LM_TELEGRAM_BOT_TOKEN : deps.telegramToken;
  if (!token) return { status: "skipped" };
  const notice = await rpc("claim_lm_managed_allowance_notice", { p_uid: String(user.uid) }, deps);
  if (!notice) return { status: "none" };
  const kind = String(notice.kind || ""), claimToken = String(notice.claimToken || "");
  const periodStart = String(notice.periodStart || "");
  const copy = noticeCopy({ ...notice, kind });
  if (!copy || !claimToken || !/^\d{4}-\d{2}-\d{2}$/.test(periodStart)) return { status: "reconciliation_required" };
  let sent;
  try { sent = await (deps.sendMessage || sendMessage)(token, user.telegram_chat_id, copy); }
  catch {
    await rpc("mark_lm_managed_allowance_notice_unknown", {
      p_uid: String(user.uid), p_notice_kind: kind, p_period_start: periodStart, p_claim_token: claimToken,
    }, deps);
    return { status: "delivery_unknown" };
  }
  const messageId = sent && sent.result && sent.result.message_id;
  if (!sent || sent.delivery_unknown === true || typeof sent.ok !== "boolean"
      || (sent.ok === true && (!Number.isInteger(messageId) || messageId <= 0))) {
    await rpc("mark_lm_managed_allowance_notice_unknown", {
      p_uid: String(user.uid), p_notice_kind: kind, p_period_start: periodStart, p_claim_token: claimToken,
    }, deps);
    return { status: "delivery_unknown" };
  }
  if (sent.ok !== true) {
    await rpc("release_lm_managed_allowance_notice", {
      p_uid: String(user.uid), p_notice_kind: kind, p_period_start: periodStart, p_claim_token: claimToken,
    }, deps);
    return { status: "send_failed" };
  }
  const recorded = await rpc("record_lm_managed_allowance_notice", {
    p_uid: String(user.uid), p_notice_kind: kind, p_period_start: periodStart, p_claim_token: claimToken,
    p_telegram_message_id: messageId,
  }, deps);
  return recorded === true ? { status: "sent", kind, telegramMessageId: messageId }
    : { status: "reconciliation_required" };
}

module.exports = { noticeCopy, deliverAllowanceNotice };
