#!/usr/bin/env node
"use strict";

const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createMinimalProductionDependencies } = require(
  "../../apps/life-manager/lib/connector-minimal-production.js",
);
const { runMinimalConnectorWake } = require(
  "../../apps/life-manager/lib/connector-minimal-runner.js",
);
const { loadConnectorEnv } = require("./lib/load-connector-env.js");
const { readConnectorProfile } = require("../../apps/life-manager/lib/connector-profile.js");

const DEFAULT_PROVIDERS = Object.freeze([
  "luma",
  "connpass",
  "peatix",
  "meetup",
  "doorkeeper",
  "eventbrite",
  "techplay",
  "kokuchpro",
]);

function unavailable() {
  throw new Error("Connector minimal pass unavailable");
}

function absoluteDirectory(value) {
  const directory = path.resolve(String(value == null ? "" : value));
  if (!path.isAbsolute(directory) || directory === path.parse(directory).root) unavailable();
  return directory;
}

function requiredToken(value) {
  const token = String(value == null ? "" : value).trim();
  if (!/^[A-Za-z0-9._-]{16,200}$/.test(token)) unavailable();
  return token;
}

function requiredText(value, fallback) {
  const text = String(value == null || value === "" ? fallback || "" : value).trim();
  if (!text || text.length > 2_000 || /[\x00-\x1f\x7f]/.test(text)) unavailable();
  return text;
}

function requiredEmail(value) {
  const raw = String(value == null ? "" : value);
  const email = requiredText(raw);
  if (raw !== email || email.length > 254 || !/^(?!\.)(?!.*\.\.)[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+(?<!\.)@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/.test(email)) unavailable();
  return email;
}

function toHiragana(value) {
  return [...value].map((char) => {
    const code = char.codePointAt(0);
    return code >= 0x30a1 && code <= 0x30f6 ? String.fromCodePoint(code - 0x60) : char;
  }).join("");
}

function readKanaIdentity(env) {
  const home = absoluteDirectory(env.HOME || os.homedir());
  const file = path.join(home, ".config", "anicca", "job-search", "profile.json");
  let profile;
  try {
    const stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.size < 2 || stat.size > 1024 * 1024 || (stat.mode & 0o777) !== 0o600) unavailable();
    profile = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch { unavailable(); }
  const nameKana = profile && typeof profile === "object" && !Array.isArray(profile)
    ? profile.candidate?.name_kana : null;
  const rawNameRomaji = profile?.candidate?.name;
  const rawPreferredName = profile?.candidate?.preferred_name;
  if (typeof rawNameRomaji !== "string" || typeof rawPreferredName !== "string") unavailable();
  if (/[\x00-\x1f\x7f-\x9f]/.test(rawNameRomaji) || /[\x00-\x1f\x7f-\x9f]/.test(rawPreferredName)) unavailable();
  const nameRomaji = requiredText(rawNameRomaji);
  const preferredName = requiredText(rawPreferredName);
  if (rawNameRomaji.length > 200 || rawPreferredName.length > 200 || nameRomaji.length > 200 || preferredName.length > 200) unavailable();
  const nameTokens = nameRomaji.split(/\s+/u);
  const preferredTokens = preferredName.split(/\s+/u);
  const legalName = requiredText(env.DAIS_LEGAL_NAME_ROMAJI);
  if (nameTokens.length !== 2 || preferredTokens.length !== 1 || nameRomaji !== legalName) unavailable();
  const preferredMatches = nameTokens.filter((token) => token.toLowerCase() === preferredName.toLowerCase());
  if (preferredMatches.length !== 1) unavailable();
  const givenIndex = nameTokens.findIndex((token) => token.toLowerCase() === preferredName.toLowerCase());
  const givenName = nameTokens[givenIndex];
  const familyName = nameTokens[1 - givenIndex];
  if (!nameKana || typeof nameKana !== "object" || Array.isArray(nameKana)
    || Object.keys(nameKana).length !== 2 || !Object.hasOwn(nameKana, "family") || !Object.hasOwn(nameKana, "given")) unavailable();
  const rawNameJa = profile?.candidate?.name_ja;
  if (typeof rawNameJa !== "string" || rawNameJa !== rawNameJa.trim() || !rawNameJa || rawNameJa.length > 200 || /[\x00-\x1f\x7f-\x9f]/.test(rawNameJa)) unavailable();
  const nameJa = rawNameJa;
  const part = (value) => {
    if (typeof value !== "string" || value.length < 1 || value.length > 100 || value !== value.trim()
      || !/^[\u30A1-\u30F6\u30FC]+$/u.test(value)) unavailable();
    return value;
  };
  const family = part(nameKana.family); const given = part(nameKana.given);
  return Object.freeze({ family, given, name_kanji: nameJa, name_hiragana: `${toHiragana(family)} ${toHiragana(given)}`, given_name: givenName, family_name: familyName });
}

function resolvedTelegramTarget(env) {
  const configured = String(env.LM_CONNECTOR_TELEGRAM_TARGET || "").trim();
  return requiredText(configured);
}

function resolveEnv(options) {
  const supplied = options.env && typeof options.env === "object" && !Array.isArray(options.env)
    ? options.env : process.env;
  const sharedFile = String(supplied.LM_CONNECTOR_SHARED_ENV_FILE || "").trim();
  const loaded = sharedFile && fs.existsSync(sharedFile) ? loadConnectorEnv(sharedFile) : {};
  const sharedToken = sharedFile ? loaded.TELEGRAM_BOT_TOKEN : undefined;
  // The official entrypoint always supplies the shared state .env; never let an inline token override it.
  return {
    ...loaded,
    ...supplied,
    ...(sharedFile ? (sharedToken ? { TELEGRAM_BOT_TOKEN: sharedToken } : { TELEGRAM_BOT_TOKEN: undefined }) : {}),
  };
}

function reportConfig(options, stateDir, ownerToken) {
  const env = resolveEnv(options);
  const repoRoot = absoluteDirectory(options.repoRoot);
  const validatedStateDir = absoluteDirectory(stateDir);
  const validatedOwnerToken = requiredToken(ownerToken);
  return Object.freeze({
    repoRoot,
    stateDir: validatedStateDir,
    wakeId: `wake-${createHash("sha256").update(validatedOwnerToken).digest("hex").slice(0, 24)}`,
    telegramTarget: resolvedTelegramTarget(env),
  });
}

function productionConfig(options, stateDir, ownerToken) {
  const report = reportConfig(options, stateDir, ownerToken);
  const env = resolveEnv(options);
  const calendarAccount = requiredEmail(env.GOG_ACCOUNT || env.LM_CONNECTOR_LUMA_EMAIL);
  const attendeeEmail = requiredEmail(env.GOG_ACCOUNT);
  const attendeeName = requiredText(env.DAIS_LEGAL_NAME_ROMAJI);
  const kanaIdentity = readKanaIdentity(env);
  if (attendeeName.length > 200) unavailable();
  const peatixAttendeeProfile = Object.freeze({ name: attendeeName, email: attendeeEmail, given_name: kanaIdentity.given_name, family_name: kanaIdentity.family_name, family_name_kana: kanaIdentity.family, given_name_kana: kanaIdentity.given, name_kanji: kanaIdentity.name_kanji, name_hiragana: kanaIdentity.name_hiragana, accept_organizer_privacy: true });
  const tenantId = requiredText(env.LM_CONNECTOR_TENANT_ID, "dais-local");
  const profile = readConnectorProfile({
    tenantId,
    path: path.join(report.repoRoot, "apps", "life-manager", "config", "connector", `${tenantId}.json`),
  });
  return Object.freeze({
    ...report,
    telegramToken: requiredText(env.TELEGRAM_BOT_TOKEN),
    calendarAccount,
    peatixAttendeeProfile,
    gogKeyring: requiredText(env.GOG_KEYRING_PASSWORD),
    gogBin: String(env.GOG_BIN || "").trim() || undefined,
    tenantId,
    eventPreferences: profile.preferences,
    geminiApiKey: requiredText(env.GEMINI_API_KEY),
    connpassApiKey: String(env.CONNPASS_API_KEY || "").trim() || undefined,
    connpassAutomatedSubmitAllowed: String(env.LM_CONNECTOR_CONNPASS_AUTOMATED_SUBMIT_ALLOWED || "").trim() === "true",
    calendarId: requiredText(env.LM_CONNECTOR_CALENDAR_ID, "primary"),
    lumaFormProfilePath: path.join(path.dirname(stateDir), "private", "connector-luma-form-profile.json"),
    lunaEvidenceDir: path.join(stateDir, "luna"),
  });
}

async function runNativePass(options = {}) {
  absoluteDirectory(options.repoRoot);
  const stateDir = absoluteDirectory(options.stateDir);
  const ownerToken = requiredToken(options.ownerToken);
  const runWake = typeof options.runWake === "function"
    ? options.runWake
    : runMinimalConnectorWake;
  const createDependencies = typeof options.createDependencies === "function"
    ? options.createDependencies : createMinimalProductionDependencies;
  const dependencies = options.dependencies || createDependencies(
    productionConfig(options, stateDir, ownerToken),
  );
  if (!dependencies || typeof dependencies !== "object" || Array.isArray(dependencies)) unavailable();

  return runWake(Object.freeze({
    ownerToken,
    stateDir,
    providers: DEFAULT_PROVIDERS,
    maxConsecutiveFailures: 8,
    maxWakeMs: 600_000,
    maxAgentSteps: 15,
  }), dependencies);
}

function cliArguments(argv = process.argv.slice(2)) {
  if (
    argv.length !== 6
    || argv[0] !== "--repo-root"
    || argv[2] !== "--state-dir"
    || argv[4] !== "--owner-token"
  ) unavailable();
  return Object.freeze({
    repoRoot: argv[1],
    stateDir: argv[3],
    ownerToken: argv[5],
  });
}

function nativeExitCode(result) {
  const status = result && typeof result === "object" && !Array.isArray(result) && Object.hasOwn(result, "status")
    ? result.status : undefined;
  return status === "applied_bundle" || status === "completed_no_effect" ? 0 : 1;
}

if (require.main === module) {
  runNativePass(cliArguments())
    .then((result) => {
      process.exit(nativeExitCode(result));
    })
    .catch(() => {
      process.stderr.write("Connector minimal pass unavailable\n", () => process.exit(2));
    });
}

module.exports = {
  createNativeProductionConfig: productionConfig,
  createNativeReportConfig: reportConfig,
  nativeExitCode,
  runNativePass,
};
