"use strict";

const { createCfoSupabaseRpc } = require("./cfo-supabase-rpc.js");

const ERROR_PREFIX = "cfo_daily_run_failed:";
const INPUT_KEYS = new Set(["uid"]);
const RECEIPT_KEYS = new Set(["public_ref", "reporting_date", "run_id", "time_zone", "created_at"]);

const { fail, internal, exact, validDate, uuid, timestamp, validateOptions, freeze, postRpc } = createCfoSupabaseRpc(ERROR_PREFIX);

function validTimeZone(value) {
  if (typeof value !== "string" || value.length === 0) return false;
  try { new Intl.DateTimeFormat("en", { timeZone: value }).format(0); return true; } catch { return false; }
}
function validateInput(input) {
  exact(input, INPUT_KEYS);
  if (typeof input.uid !== "string" || input.uid.length === 0 || input.uid.length > 255 || input.uid.trim() !== input.uid || /[\u0000-\u001f\u007f]/.test(input.uid)) fail("invalid_uid");
  return { uid: input.uid };
}
function validateReceipt(value) {
  exact(value, RECEIPT_KEYS, "invalid_receipt");
  uuid(value.public_ref, "invalid_receipt");
  if (!validDate(value.reporting_date)) fail("invalid_receipt");
  uuid(value.run_id, "invalid_receipt");
  if (!validTimeZone(value.time_zone)) fail("invalid_receipt");
  if (!timestamp(value.created_at)) fail("invalid_receipt");
  try { return freeze(structuredClone(value)); } catch { fail("invalid_receipt"); }
}

async function resolveCfoDailyRun(input, opts = {}) {
  let identity, config;
  try { identity = validateInput(input); config = validateOptions(opts); } catch (error) { if (internal(error)) throw error; fail("invalid_input"); }
  const parsed = await postRpc(config, "lm_claim_cfo_daily_run", { p_uid: identity.uid });
  try { return validateReceipt(parsed); } catch (error) { if (internal(error)) throw error; fail("invalid_receipt"); }
}

module.exports = { resolveCfoDailyRun };
