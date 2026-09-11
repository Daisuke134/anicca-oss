"use strict";

const fs = require("node:fs");

function readEnvFile(file) {
  if (!file || !fs.existsSync(file)) return {};
  const values = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/u)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/u);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith('"') && value.endsWith('"'))
      || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    values[match[1]] = value;
  }
  return values;
}

function isConfigured(value) {
  const normalized = String(value || "").trim();
  return Boolean(normalized)
    && !/(?:YOUR[-_ ]|CHANGE[-_ ]?ME|EXAMPLE|PLACEHOLDER)/iu.test(normalized);
}

function hasTelegramCredentials(file, environment = process.env) {
  const values = readEnvFile(file);
  for (const [key, value] of Object.entries(environment)) {
    if (String(value || "").trim()) values[key] = value;
  }
  const token = values.TELEGRAM_BOT_TOKEN || values.LM_TELEGRAM_BOT_TOKEN;
  const chat = values.TELEGRAM_CHAT_ID
    || values.TELEGRAM_ALERT_CHAT_ID
    || values.LM_ADMIN_TELEGRAM_CHAT_ID;
  return isConfigured(token) && isConfigured(chat);
}

module.exports = { hasTelegramCredentials, isConfigured, readEnvFile };
