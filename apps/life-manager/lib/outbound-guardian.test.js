"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  parseTelegramMessageId,
  notifyTelegram,
  notifyTelegramReport,
  notifyTelegramPhoto,
} = require("./outbound-guardian.js");

test("Telegram Bot API receiptはpositive message IDだけを配信成功にする", () => {
  assert.equal(parseTelegramMessageId({ ok: true, result: { message_id: 4312 } }), "4312");
  for (const value of [
    {}, { ok: false }, { ok: true, result: {} }, { ok: true, result: { message_id: 0 } },
    { ok: true, result: { message_id: "4312" } }, '{"ok":true,"result":{"message_id":0}}', "not-json",
  ]) assert.throws(() => parseTelegramMessageId(value), /message ID/);
});

test("legacy text delivery uses Telegram sendMessage and receives a provider ID", async () => {
  const receipt = await notifyTelegram("wake report", {
    telegramTarget: "fixture-target",
    telegramToken: "fixture-token",
    async sendMessage(token, target, message) {
      assert.equal(token, "fixture-token");
      assert.equal(target, "fixture-target");
      assert.equal(message, "wake report");
      return { ok: true, result: { message_id: 321 } };
    },
  });
  assert.deepEqual(receipt, { messageId: "321" });
});

test("report text delivery validates wake inputs, uses sendMessage, and fails closed", async () => {
  const receipt = await notifyTelegramReport("wake report", {
    telegramTarget: "123456789",
    idempotencyKey: "wake-20260810-001",
    telegramToken: "fixture-token",
    async sendMessage(token, target, message) {
      assert.equal(token, "fixture-token");
      assert.equal(target, "123456789");
      assert.equal(message, "wake report");
      return { ok: true, result: { message_id: 322 } };
    },
  });
  assert.deepEqual(receipt, { messageId: "322" });

  for (const [telegramTarget, idempotencyKey] of [["fixture-target", "wake-20260810-001"], ["1234", "wake-20260810-001"], ["123456789", "bad key"]]) {
    let sent = false;
    await assert.rejects(() => notifyTelegramReport("wake report", {
      telegramTarget, idempotencyKey, telegramToken: "fixture-token", async sendMessage() { sent = true; },
    }), /Telegram report delivery failed/);
    assert.equal(sent, false);
  }

  await assert.rejects(() => notifyTelegramReport("private report", {
    telegramTarget: "123456789", idempotencyKey: "wake-test-failure", telegramToken: "fixture-token",
    async sendMessage() { throw new Error("private provider failure"); },
  }), (error) => {
    assert.equal(error.message, "Telegram report delivery failed");
    assert.doesNotMatch(error.message, /private provider failure/);
    return true;
  });
});

test("photo delivery uses Telegram sendPhoto directly, validates before sending, and fails closed", async () => {
  const bytes = Buffer.alloc(5_000, 0x61);
  const receipt = await notifyTelegramPhoto(bytes, {
    telegramTarget: "123456789",
    caption: "registered evidence",
    idempotencyKey: "connector-evidence:abc123",
    telegramToken: "fixture-token",
    async sendPhoto(token, target, sentBytes, caption) {
      assert.equal(token, "fixture-token");
      assert.equal(target, "123456789");
      assert.deepEqual(sentBytes, bytes);
      assert.equal(caption, "registered evidence");
      return { ok: true, result: { message_id: 323 } };
    },
  });
  assert.deepEqual(receipt, { messageId: "323" });

  let sent = false;
  await assert.rejects(() => notifyTelegramPhoto(bytes, {
    telegramTarget: "1234", idempotencyKey: "connector-evidence:abc123", telegramToken: "fixture-token",
    async sendPhoto() { sent = true; },
  }), /Telegram photo delivery invalid/);
  assert.equal(sent, false);

  await assert.rejects(() => notifyTelegramPhoto(bytes, {
    telegramTarget: "123456789", idempotencyKey: "connector-evidence:abc123", telegramToken: "fixture-token",
    async sendPhoto() { return { ok: true, result: { message_id: 0 } }; },
  }), /Telegram photo delivery failed/);
});
