"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { test } = require("node:test");
process.env.LM_CALL_SECRET = "unit_secret";

test("scheduler helper-skip and signed streamUrl contract", () => {
  const { isHelperBlock, buildStreamUrl } = require("../scheduler.js");
  assert.strictEqual(isHelperBlock("[Travel] [APPLIED] x"), true);
  assert.strictEqual(isHelperBlock("🎤 [PENDING] y"), true);
  assert.strictEqual(isHelperBlock("Dentist"), false);
  process.env.PUBLIC_WSS = "wss://life-call.up.railway.app";
  const u = buildStreamUrl({ summary: "Dentist & Co", startIso: "2026-06-18T20:40:00+09:00", location: "Tokyo" }, "firm");
  const p = new URL(u);
  assert.strictEqual(p.pathname, "/ws");
  assert.ok(p.searchParams.get("sig"), "must carry an HMAC sig");
  assert.strictEqual(p.searchParams.get("urgency"), "firm");
});

test("voice allowance context is signed and tampering is rejected by the bridge", () => {
  const { buildStreamUrl } = require("../scheduler.js");
  const { ctxFromReq } = require("../server.js");
  process.env.PUBLIC_WSS = "wss://life-call.up.railway.app";
  const url = new URL(buildStreamUrl({ summary: "Dentist", startIso: "2026-06-18T20:40:00+09:00",
    location: "Tokyo", wakeUid: "tenant-a", wakeEventKey: "call-a", voicePeriodStart: "2026-09-01",
    voiceReservationToken: "11111111-1111-4111-8111-111111111111", voiceAllowedSeconds: 37 }, "firm"));
  assert.deepEqual(ctxFromReq({ url: `${url.pathname}${url.search}` }).voiceReservation, {
    periodStart: "2026-09-01", reservationToken: "11111111-1111-4111-8111-111111111111", allowedSeconds: 37,
  });
  url.searchParams.set("voiceAllowedSeconds", "38");
  assert.equal(ctxFromReq({ url: `${url.pathname}${url.search}` }), null);
});

test("late tick scheduler surface has no mail sender dependency", () => {
  const source = fs.readFileSync(path.join(__dirname, "../scheduler.js"), "utf8");
  assert.doesNotMatch(source, /require\(["']\.\/lib\/notify\.js["']\)/);
  const start = source.indexOf("async function lateNoticeUserOnce");
  const end = source.indexOf("\n// ── Per-user single-invocation functions", start);
  assert.ok(start >= 0 && end > start, "lateNoticeUserOnce source boundary must remain discoverable");
  const lateTickSource = source.slice(start, end);
  assert.doesNotMatch(lateTickSource, /sendLateNotice|noticeOpts|RESEND_API_KEY/);
});
