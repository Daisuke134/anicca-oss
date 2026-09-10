"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildConnectorTicketCaption,
  deliverConnectorTicket,
  sendTelegramMedia,
} = require("./connector-ticket-telegram.js");

const ARTIFACT_REF = `object://sha256/${"5".repeat(64)}`;

function input(overrides = {}) {
  return {
    tenantId: "dais-local",
    artifactRef: ARTIFACT_REF,
    telegramTarget: "123456789",
    eventTitle: "Engineer BAR",
    startsAt: "2026-08-15T18:00:00+09:00",
    endsAt: "2026-08-15T23:00:00+09:00",
    venue: "Bar Layover（東京・新宿）",
    registrationIdentity: "Dais",
    selectionReason: "東京でエンジニアと直接会え、既存予定と移動時間が重ならないためです。",
    priorityClass: "open_talk",
    talkState: "provider_verified",
    applicationDeadlineAt: "2026-08-14T14:59:00.000Z",
    eventUrl: "https://luma.com/a879ax7k",
    calendarUrl: "https://www.google.com/calendar/event?eid=fixture",
    confirmationReceiptRef: `gmail-message://dais-local/${"b".repeat(64)}`,
    ...overrides,
  };
}

function png() {
  const bytes = Buffer.alloc(5000, 0x61);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(bytes);
  return bytes;
}

test("非技術者が理解でき、eventとCalendarへ直接tapできる日本語captionを作る", () => {
  const caption = buildConnectorTicketCaption(input());
  assert.match(caption, /^🎟️ イベント参加の申込みが完了しました。/);
  assert.match(caption, /イベント: Engineer BAR/);
  assert.match(caption, /日時: 2026年8月15日（土）18:00〜23:00/);
  assert.match(caption, /場所: Bar Layover（東京・新宿）/);
  assert.match(caption, /優先度: open_talk/);
  assert.match(caption, /LT: provider_verified/);
  assert.match(caption, /LT申請締切: 2026年8月14日 23:59/);
  assert.match(caption, /Lumaの確認メールを受信済み/);
  assert.match(caption, /当日の公式QRを添付しました/);
  assert.match(caption, /https:\/\/luma\.com\/a879ax7k/);
  assert.match(caption, /https:\/\/www\.google\.com\/calendar\/event\?eid=fixture/);
  assert.doesNotMatch(caption, /runner|job[_ ]?id|sha256|guest.?key|\{\{|\[.+\]\(.+\)/i);
  assert.ok(caption.length <= 1024);
});

test("placeholder、非Luma URL、非Google Calendar URL、壊れた時刻を拒否する", () => {
  assert.throws(() => buildConnectorTicketCaption(input({ eventTitle: "{{event_name}}" })), /placeholder/i);
  assert.throws(() => buildConnectorTicketCaption(input({ eventUrl: "https://evil.example/a879ax7k" })), /event URL/i);
  assert.throws(() => buildConnectorTicketCaption(input({ calendarUrl: "https://evil.example/calendar" })), /Calendar URL/i);
  assert.throws(() => buildConnectorTicketCaption(input({ endsAt: "2026-08-15T17:00:00+09:00" })), /time/i);
});

test("確認メールの証跡refがない申込みは確認メール受信済みと主張しない", () => {
  assert.throws(() => buildConnectorTicketCaption(input({ confirmationReceiptRef: "" })), /confirmation receipt/i);
  assert.throws(() => buildConnectorTicketCaption(input({ confirmationReceiptRef: "not-a-receipt" })), /confirmation receipt/i);
  assert.throws(
    () => buildConnectorTicketCaption(input({ confirmationReceiptRef: "provider-receipt://luma/fixture" })),
    /confirmation receipt/i,
  );
});

test("captionの確認メール文言は検証済みeventUrlのproviderから導出する", () => {
  const caption = buildConnectorTicketCaption(input({ eventUrl: "https://lu.ma/a879ax7k" }));
  assert.match(caption, /✅ Lumaの確認メールを受信済み/);
});

test("verified artifactを一度だけ送りpositive message IDだけをreceiptにする", async () => {
  const sends = [];
  const receipt = await deliverConnectorTicket(input(), {
    readArtifact: async (tenant, ref) => {
      assert.equal(tenant, "dais-local");
      assert.equal(ref, ARTIFACT_REF);
      return png();
    },
    sendMedia: async (target, bytes, caption) => {
      sends.push({ target, bytes, caption });
      return { messageId: "7007" };
    },
    observedAt: () => "2026-08-01T15:30:00.000Z",
  });

  assert.equal(sends.length, 1);
  assert.equal(sends[0].target, "123456789");
  assert.deepEqual(sends[0].bytes, png());
  assert.deepEqual(receipt, {
    kind: "telegram_delivery",
    provider_id: "7007",
    observed_at: "2026-08-01T15:30:00.000Z",
    tenant_id: "dais-local",
    chat_id_sha256: "15e2b0d3c33891ebb0f1ef609ec419420c20e320ce94c65fbc8c3312448eb225",
    artifact_ref: ARTIFACT_REF,
    event_url: "https://luma.com/a879ax7k",
  });
  assert.doesNotMatch(JSON.stringify(receipt), /123456789|Engineer BAR|Bar Layover/);
});

test("message ID欠落と壊れたPNGは成功にしない", async () => {
  await assert.rejects(deliverConnectorTicket(input(), {
    readArtifact: async () => png(),
    sendMedia: async () => ({ ok: true }),
  }), /positive message ID/i);
  await assert.rejects(deliverConnectorTicket(input(), {
    readArtifact: async () => Buffer.alloc(5000),
    sendMedia: async () => { throw new Error("must not send"); },
  }), /PNG/i);
});

test("ticket media transportはTelegram sendPhotoを使いpositive message IDだけを返す", async () => {
  const result = await sendTelegramMedia("123456789", png(), "caption", {
    telegramToken: "fixture-token",
    async sendPhoto(token, target, bytes, caption) {
      assert.equal(token, "fixture-token");
      assert.equal(target, "123456789");
      assert.deepEqual(bytes, png());
      assert.equal(caption, "caption");
      return { ok: true, result: { message_id: 8008 } };
    },
  });
  assert.deepEqual(result, { messageId: "8008" });
  await assert.rejects(() => sendTelegramMedia("123456789", png(), "caption", {
    telegramToken: "fixture-token",
    async sendPhoto() { return { ok: true, result: { message_id: 0 } }; },
  }), /Telegram media delivery failed/);
});
