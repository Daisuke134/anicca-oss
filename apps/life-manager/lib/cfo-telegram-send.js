"use strict";

const { renderCfoTelegram } = require("./cfo-telegram.js");
const { claimCfoTelegramDelivery, recordCfoTelegramDelivery } = require("./cfo-telegram-delivery.js");
const { sendMessage } = require("./telegram.js");

const ERROR_PREFIX = "cfo_telegram_send_failed:";

function failure(reason) { return new Error(`${ERROR_PREFIX}${reason}`); }
function outcome(status, messageId = null) { return Object.freeze({ status, messageId }); }
function positiveSafeInteger(value) { return Number.isSafeInteger(value) && value > 0; }

async function deliverCfoTelegram(input, options = {}) {
  const render = options.render || renderCfoTelegram;
  const claim = options.claim || claimCfoTelegramDelivery;
  const send = options.send || sendMessage;
  const record = options.record || recordCfoTelegramDelivery;
  const rpcOptions = { supaUrl: options.supaUrl, supaKey: options.supaKey };
  if (Object.prototype.hasOwnProperty.call(options, "fetchImpl")) rpcOptions.fetchImpl = options.fetchImpl;
  const renderInput = { locale: "ja", view: "summary", snapshot: input.snapshot };
  if (Object.prototype.hasOwnProperty.call(input, "transactions")) renderInput.transactions = input.transactions;
  const rendered = render(renderInput);
  const claimed = await claim({
    uid: input.uid,
    snapshotPublicRef: input.snapshotPublicRef,
    reportKind: "assets_liabilities",
    reportingDate: input.snapshot.reportingDate,
    revision: input.snapshot.revision,
  }, rpcOptions);
  if (claimed && claimed.decision === "sent") return outcome("already_sent");
  if (claimed && claimed.decision === "reconcile") return outcome("reconcile");
  if (!claimed || claimed.decision !== "send") throw failure("invalid_claim");

  let messageId;
  try {
    const response = await send(input.telegramToken, input.chatId, rendered.text, rendered.extra);
    const result = response && response.result;
    if (!response || response.ok !== true || !result || !positiveSafeInteger(result.message_id)) throw failure("provider_rejected");
    messageId = result.message_id;
  } catch { throw failure("provider_rejected"); }
  await record({ claimPublicRef: claimed.public_ref, messageId }, rpcOptions);
  return outcome("sent", messageId);
}

module.exports = { deliverCfoTelegram };
