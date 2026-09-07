"use strict";

const { sendMessage } = require("./telegram.js");

const COPY = Object.freeze({
  eighty: "今月の無料利用分は残り20%です。設定やカレンダー接続はそのまま使えます。",
  exhausted: "今月の無料利用分を使い切りました。設定とこれまでの情報はそのまま残っています。翌月に無料利用分が戻ります。続ける場合は /subscribe を送ってください。",
});

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
  if (!COPY[kind] || !claimToken) return { status: "reconciliation_required" };
  let sent;
  try { sent = await (deps.sendMessage || sendMessage)(token, user.telegram_chat_id, COPY[kind]); }
  catch { return { status: "delivery_unknown" }; }
  const messageId = sent && sent.result && sent.result.message_id;
  if (!sent || sent.delivery_unknown === true || typeof sent.ok !== "boolean"
      || (sent.ok === true && (!Number.isInteger(messageId) || messageId <= 0))) {
    return { status: "delivery_unknown" };
  }
  if (sent.ok !== true) {
    await rpc("release_lm_managed_allowance_notice", {
      p_uid: String(user.uid), p_notice_kind: kind, p_claim_token: claimToken,
    }, deps);
    return { status: "send_failed" };
  }
  const recorded = await rpc("record_lm_managed_allowance_notice", {
    p_uid: String(user.uid), p_notice_kind: kind, p_claim_token: claimToken,
    p_telegram_message_id: messageId,
  }, deps);
  return recorded === true ? { status: "sent", kind, telegramMessageId: messageId }
    : { status: "reconciliation_required" };
}

module.exports = { COPY, deliverAllowanceNotice };
