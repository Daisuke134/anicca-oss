"use strict";

const { createCfoSupabaseRpc } = require("./cfo-supabase-rpc.js");

const ERROR_PREFIX = "cfo_telegram_delivery_failed:";
const CLAIM_INPUT_KEYS = new Set(["uid", "snapshotPublicRef", "reportKind", "reportingDate", "revision"]);
const RECORD_INPUT_KEYS = new Set(["claimPublicRef", "messageId"]);
const CLAIM_RECEIPT_KEYS = new Set(["public_ref", "decision", "reporting_date", "revision", "created_at"]);
const RECORD_RECEIPT_KEYS = new Set(["public_ref", "claim_public_ref", "message_id", "created_at"]);
const DECISIONS = new Set(["send", "sent", "reconcile"]);

const { fail, internal, exact, validDate, uuid, timestamp, validateOptions, freeze, postRpc } = createCfoSupabaseRpc(ERROR_PREFIX);

function positiveSafeInteger(value) { return Number.isSafeInteger(value) && value > 0; }
function validUid(value) { return typeof value === "string" && value.length > 0 && value.length <= 255 && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value); }
function validateClaimInput(input) {
  exact(input, CLAIM_INPUT_KEYS);
  if (!validUid(input.uid)) fail("invalid_uid");
  const snapshotPublicRef = uuid(input.snapshotPublicRef, "invalid_snapshot_public_ref");
  if (input.reportKind !== "assets_liabilities") fail("invalid_report_kind");
  if (!validDate(input.reportingDate)) fail("invalid_reporting_date");
  if (!positiveSafeInteger(input.revision)) fail("invalid_revision");
  return { uid: input.uid, snapshotPublicRef, reportKind: input.reportKind, reportingDate: input.reportingDate, revision: input.revision };
}
function validateRecordInput(input) {
  exact(input, RECORD_INPUT_KEYS);
  const claimPublicRef = uuid(input.claimPublicRef, "invalid_claim_public_ref");
  if (!positiveSafeInteger(input.messageId)) fail("invalid_message_id");
  return { claimPublicRef, messageId: input.messageId };
}
function validateReceipt(value, expected, record) {
  exact(value, record ? RECORD_RECEIPT_KEYS : CLAIM_RECEIPT_KEYS, "invalid_receipt");
  uuid(value.public_ref, "invalid_receipt");
  if (record) {
    uuid(value.claim_public_ref, "invalid_receipt");
    if (!positiveSafeInteger(value.message_id)) fail("invalid_receipt");
    if (value.claim_public_ref !== expected.claimPublicRef || value.message_id !== expected.messageId) fail("receipt_mismatch");
  } else {
    if (!DECISIONS.has(value.decision)) fail("invalid_receipt");
    if (!validDate(value.reporting_date) || !positiveSafeInteger(value.revision)) fail("invalid_receipt");
    if (value.reporting_date !== expected.reportingDate || value.revision !== expected.revision) fail("receipt_mismatch");
  }
  if (!timestamp(value.created_at)) fail("invalid_receipt");
  try { return freeze(structuredClone(value)); } catch { fail("invalid_receipt"); }
}
async function callDeliveryRpc(input, opts, path, payload, record) {
  let identity, config;
  try { identity = record ? validateRecordInput(input) : validateClaimInput(input); config = validateOptions(opts); } catch (error) { if (internal(error)) throw error; fail("invalid_input"); }
  const parsed = await postRpc(config, path, payload(identity));
  try { return validateReceipt(parsed, identity, record); } catch (error) { if (internal(error)) throw error; fail("invalid_receipt"); }
}
function claimCfoTelegramDelivery(input, opts = {}) {
  return callDeliveryRpc(input, opts, "lm_claim_cfo_telegram_delivery", identity => ({ p_uid: identity.uid, p_snapshot_public_ref: identity.snapshotPublicRef, p_report_kind: identity.reportKind, p_reporting_date: identity.reportingDate, p_revision: identity.revision }), false);
}
function recordCfoTelegramDelivery(input, opts = {}) {
  return callDeliveryRpc(input, opts, "lm_record_cfo_telegram_delivery", identity => ({ p_claim_public_ref: identity.claimPublicRef, p_message_id: identity.messageId }), true);
}

module.exports = { claimCfoTelegramDelivery, recordCfoTelegramDelivery };
