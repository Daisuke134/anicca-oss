"use strict";

const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const {
  createFinancialTransitionStore,
  deliverFinancialTransitionWithOutbox,
} = require("./agent-economy-telegram.js");

function localTransitionOptions(env = process.env) {
  const stateDir = env.CFO_STATE_DIR || env.LIFE_MANAGER_STATE_ROOT
    || path.join(os.homedir(), ".local/state/life-manager/life-manager-cfo-hourly");
  return {
    pythonBin: env.CFO_PYTHON_BIN || "python3",
    database: env.CFO_TELEGRAM_OUTBOX || path.join(stateDir, "telegram-outbox.sqlite3"),
    chatId: env.TELEGRAM_ALERT_CHAT_ID || env.LM_CFO_TELEGRAM_CHAT_ID || env.LM_ADMIN_TELEGRAM_CHAT_ID || "",
    envFile: env.LIFE_MANAGER_ENV_FILE || path.join(os.homedir(), ".local/state/life-manager/.env"),
  };
}

function notifyViaLocalOutbox(input, options = {}) {
  const script = path.resolve(__dirname, "../../../skills/cfo/notify.py");
  const result = spawnSync(options.pythonBin, [
    script, "--database", options.database, "--event-key", input.eventKey,
    "--observed-at", input.observedAt, "--chat-id", options.chatId,
    "--env-file", options.envFile,
  ], { encoding: "utf8", input: JSON.stringify({ message: input.message }),
    env: process.env, timeout: 30_000 });
  if (result.status !== 0) throw new Error("telegram_outbox_failed");
  return JSON.parse(String(result.stdout || "").trim());
}

function createLocalFinancialTransitionStore({ store, env = process.env, notify, now } = {}) {
  const options = localTransitionOptions(env);
  const send = notify || ((input) => notifyViaLocalOutbox(input, options));
  return createFinancialTransitionStore({ store, deliver: (record) => (
    deliverFinancialTransitionWithOutbox({ record, notify: send, now: now ? now() : new Date() })
  ) });
}

module.exports = { createLocalFinancialTransitionStore, localTransitionOptions, notifyViaLocalOutbox };
