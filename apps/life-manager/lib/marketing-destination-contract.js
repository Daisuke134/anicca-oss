"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const HANDLE = /^@[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const OBJECT_REF = /^object:\/\/sha256\/[0-9a-f]{64}$/;
const TIME = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;
const PLATFORMS = new Set(["instagram", "tiktok", "youtube"]);

function invalid(field) {
  throw new Error(`marketing destination contract ${field} invalid`);
}

function text(value, field, pattern = ID) {
  if (typeof value !== "string" || !pattern.test(value)) invalid(field);
  return value;
}

function unique(set, value, field) {
  const key = String(value).toLowerCase();
  if (set.has(key)) invalid(`duplicate ${field}`);
  set.add(key);
}

function validateMarketingDestinationContract(input) {
  if (!input || input.schema_version !== 1 || input.timezone !== "Asia/Tokyo"
    || !Array.isArray(input.targets) || !Array.isArray(input.holds)) invalid("root");
  const lanes = new Set();
  const integrations = new Set();
  const nativeHandles = new Set();
  const targetProfiles = new Set();
  const targets = input.targets.map((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) invalid("target");
    for (const field of ["lane_id", "product_id", "job_product_id", "locale", "platform", "postiz_profile", "native_handle", "integration_id", "renderer_id", "job_format_id", "media_form", "approved_pack", "approved_pack_ref", "loop_name", "label", "entrypoint", "cadence_jst"]) {
      if (row[field] === undefined) invalid(field);
    }
    text(row.lane_id, "lane_id");
    text(row.product_id, "product_id");
    text(row.job_product_id, "job_product_id");
    text(row.locale, "locale", /^[a-z]{2}$/);
    if (!PLATFORMS.has(row.platform)) invalid("platform");
    text(row.postiz_profile, "postiz_profile", HANDLE);
    text(row.native_handle, "native_handle", HANDLE);
    text(row.integration_id, "integration_id");
    text(row.renderer_id, "renderer_id");
    text(row.job_format_id, "job_format_id");
    text(row.media_form, "media_form");
    text(row.approved_pack, "approved_pack", /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json$/);
    text(row.approved_pack_ref, "approved_pack_ref", OBJECT_REF);
    text(row.loop_name, "loop_name");
    text(row.label, "label", /^ai\.anicca\.life-manager-[A-Za-z0-9._-]+$/);
    text(row.entrypoint, "entrypoint", /^apps\/life-manager\/scripts\/(?:mobile-app|[A-Za-z0-9._-]+\.sh)$/);
    if (!Array.isArray(row.cadence_jst) || row.cadence_jst.length !== 3
      || row.cadence_jst.some((value) => typeof value !== "string" || !TIME.test(value))
      || new Set(row.cadence_jst).size !== 3) invalid("cadence_jst");
    unique(lanes, row.lane_id, "lane_id");
    unique(integrations, row.integration_id, "integration_id");
    unique(nativeHandles, row.native_handle, "native handle");
    unique(targetProfiles, row.postiz_profile, "target profile across platforms");
    return { ...row, cadence_jst: [...row.cadence_jst] };
  });
  const holds = input.holds.map((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) invalid("hold");
    if (!PLATFORMS.has(row.platform) && !["x", "youtube"].includes(row.platform)) invalid("hold platform");
    text(row.postiz_profile, "hold postiz_profile", HANDLE);
    if (row.integration_id !== null) {
      text(row.integration_id, "hold integration_id");
      unique(integrations, row.integration_id, "integration_id");
    }
    text(row.reason, "hold reason", /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/);
    if (row.target_daily_limit !== 0) invalid("hold target_daily_limit");
    return { ...row };
  });
  return Object.freeze({ schema_version: 1, timezone: input.timezone, targets: Object.freeze(targets), holds: Object.freeze(holds) });
}

function auditMarketingDestinationRegistry(contractInput, registry) {
  const contract = validateMarketingDestinationContract(contractInput);
  if (!registry || typeof registry !== "object" || !registry.loops || typeof registry.loops !== "object") {
    invalid("loop registry");
  }
  for (const target of contract.targets) {
    const loop = registry.loops[target.loop_name];
    if (!loop || loop.label !== target.label) invalid(`${target.lane_id} label`);
    if (loop.entrypoint !== target.entrypoint) invalid(`${target.lane_id} entrypoint`);
    const intervals = loop.cadence && loop.cadence.calendar_interval;
    if (!Array.isArray(intervals)) invalid(`${target.lane_id} cadence`);
    const cadence = intervals.map((value) => {
      if (!value || !Number.isSafeInteger(value.Hour) || !Number.isSafeInteger(value.Minute)) {
        invalid(`${target.lane_id} cadence`);
      }
      return `${String(value.Hour).padStart(2, "0")}:${String(value.Minute).padStart(2, "0")}`;
    });
    if (JSON.stringify(cadence) !== JSON.stringify(target.cadence_jst)) invalid(`${target.lane_id} cadence`);
  }
  return Object.freeze({ targets: contract.targets.length });
}

function findMarketingDestinationTarget(contractInput, input = {}) {
  const contract = validateMarketingDestinationContract(contractInput);
  const matches = contract.targets.filter((target) => (
    target.job_product_id === input.jobProductId
    && target.locale === input.locale
    && target.platform === input.platform
    && target.integration_id === input.integrationId
    && target.job_format_id === input.jobFormatId
    && target.media_form === input.mediaForm
  ));
  return matches.length === 1 ? matches[0] : null;
}

function loadMarketingDestinationContract(file = path.resolve(__dirname, "../../../config/marketing-destinations.json")) {
  let value;
  try { value = JSON.parse(fs.readFileSync(file, "utf8")); } catch { invalid("file"); }
  return validateMarketingDestinationContract(value);
}

module.exports = {
  auditMarketingDestinationRegistry,
  findMarketingDestinationTarget,
  loadMarketingDestinationContract,
  validateMarketingDestinationContract,
};
