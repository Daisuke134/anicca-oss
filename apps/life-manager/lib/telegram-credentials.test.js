"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { hasTelegramCredentials } = require("./telegram-credentials.js");

function fixture(contents) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "lm-telegram-credentials-"));
  const file = path.join(directory, ".env");
  fs.writeFileSync(file, contents);
  return file;
}

test("placeholder or incomplete Telegram credentials fail closed", () => {
  assert.equal(hasTelegramCredentials(fixture(
    "TELEGRAM_BOT_TOKEN=fixture:YOUR-BOT-TOKEN\nTELEGRAM_CHAT_ID=YOUR-TELEGRAM-CHAT-ID\n",
  ), {}), false);
  assert.equal(hasTelegramCredentials(fixture("TELEGRAM_BOT_TOKEN=real-token\n"), {}), false);
});

test("private Telegram token and chat ID satisfy the local requirement", () => {
  assert.equal(hasTelegramCredentials(fixture(
    "TELEGRAM_BOT_TOKEN=real-token\nTELEGRAM_CHAT_ID=private-chat-fixture\n",
  ), {}), true);
});

test("hosted Telegram aliases do not satisfy the private Local sender contract", () => {
  assert.equal(hasTelegramCredentials(fixture(""), {
    LM_TELEGRAM_BOT_TOKEN: "hosted-token",
    LM_ADMIN_TELEGRAM_CHAT_ID: "hosted-chat",
  }), false);
});
