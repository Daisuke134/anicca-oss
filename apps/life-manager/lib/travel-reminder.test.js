"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { fetchUpcomingEvents } = require("./events.js");
const travel = require("./travel.js");

const recordTravelTelegramReceipt = (...args) => typeof travel.recordTravelTelegramReceipt === "function"
  ? travel.recordTravelTelegramReceipt(...args)
  : { ok: false, matched: 0, error: "module_missing" };

const {
  T5_MS,
  CATCH_UP_MS,
  isReminderDue,
  nextReminderEvent,
  resolveReminderOrigin,
  resolveReminderDestination,
  computeDepartureMs,
  computeReminderDueAt,
  formatTravelReminder,
  travelReminderOnce,
} = require("./travel-reminder.js");

const NOW = Date.parse("2026-08-28T04:00:00.000Z"); // 13:00 JST
const START = Date.parse("2026-08-28T05:00:00.000Z"); // 14:00 JST
const END = START + 60 * 60 * 1000;
const HOME = "東京都新宿区1-1-1";
const successfulReceipt = async () => ({ ok: true, matched: 1 });

function event(overrides = {}) {
  return {
    id: "event-1",
    summary: "打ち合わせ",
    location: "渋谷",
    startMs: START,
    startIso: "2026-08-28T14:00:00+09:00",
    endMs: END,
    ...overrides,
  };
}

function routeFixture(overrides = {}) {
  return {
    provider: "transit",
    departureAt: "2026-08-28T13:20:00+09:00",
    arrivalAt: "2026-08-28T14:00:00+09:00",
    durationSeconds: 2400,
    accessWalkSeconds: 300,
    egressWalkSeconds: 0,
    transferCount: 1,
    fare: { currency: "JPY", ticket: null, ic: 209 },
    steps: [
      {
        kind: "transit", mode: "subway", service: "丸ノ内線", trainType: null, headsign: "荻窪行",
        from: { name: "東京駅", platform: "2番線" }, to: { name: "新宿駅", platform: null },
        departAt: "2026-08-28T13:20:00+09:00", arriveAt: "2026-08-28T13:40:00+09:00",
      },
      {
        kind: "transit", mode: "rail", service: "JR線", trainType: null, headsign: null,
        from: { name: "新宿駅", platform: null }, to: { name: "渋谷駅", platform: null },
        departAt: "2026-08-28T13:46:00+09:00", arriveAt: "2026-08-28T13:55:00+09:00",
      },
    ],
    availability: { platform: true, fare: true, stationExit: false },
    ...overrides,
  };
}

test("Telegram receipt RPC posts the exact service body and accepts matched=1", async () => {
  const calls = [];
  const result = await recordTravelTelegramReceipt(
    "tenant-a", "event-a", "telegram-t5", 901, "https://supa.example/", "service-secret",
    { fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return { ok: true, status: 200, json: async () => 1 };
    } },
  );
  assert.deepEqual(result, { ok: true, matched: 1 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://supa.example/rest/v1/rpc/record_lm_travel_telegram_receipt");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(calls[0].init.headers, {
    "Content-Type": "application/json",
    apikey: "service-secret",
    Authorization: "Bearer service-secret",
  });
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    p_uid: "tenant-a",
    p_event_key: "event-a",
    p_leg: "telegram-t5",
    p_telegram_message_id: 901,
  });
});

test("Telegram receipt RPC rejects invalid input and never fetches or leaks raw errors", async () => {
  const base = ["tenant-a", "event-a", "telegram-t5", 901, "https://supa.example", "service-secret"];
  const invalid = [
    ["uid", "\t\n"],
    ["uid", "u".repeat(257)],
    ["eventKey", "\t\n"],
    ["eventKey", "e".repeat(513)],
    ["leg", "go"],
    ["messageId", 0],
    ["messageId", -1],
    ["messageId", 1.5],
    ["messageId", Number.MAX_SAFE_INTEGER + 1],
  ];
  for (const [key, value] of invalid) {
    let fetches = 0;
    const input = { ...Object.fromEntries(["uid", "eventKey", "leg", "messageId", "supaUrl", "supaKey"].map((name, i) => [name, base[i]])), [key]: value };
    const result = await recordTravelTelegramReceipt(
      input.uid, input.eventKey, input.leg, input.messageId, input.supaUrl, input.supaKey,
      { fetchImpl: async () => { fetches += 1; throw new Error("raw-secret"); } },
    );
    assert.equal(result.ok, false, key);
    assert.equal(result.matched, 0, key);
    assert.equal(fetches, 0, key);
    assert.doesNotMatch(JSON.stringify(result), /raw-secret|service-secret|supa\.example/);
  }
  for (const [supaUrl, supaKey] of [["", "key"], ["https://supa.example", ""], ["https://supa.example", "\t\n"]]) {
    let fetches = 0;
    const result = await recordTravelTelegramReceipt("tenant-a", "event-a", "telegram-t5", 901, supaUrl, supaKey, {
      fetchImpl: async () => { fetches += 1; throw new Error("raw-secret"); },
    });
    assert.equal(result.ok, false);
    assert.equal(result.matched, 0);
    assert.equal(fetches, 0);
  }
  const malformed = await recordTravelTelegramReceipt(...base, { fetchImpl: async () => ({ ok: true, status: 200, json: async () => "1" }) });
  assert.deepEqual(malformed, { ok: false, matched: 0, error: "invalid_result" });
  const failed = await recordTravelTelegramReceipt(...base, { fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({ secret: "raw-secret" }) }) });
  assert.deepEqual(failed, { ok: false, matched: 0, error: "http_error" });
  const contradictoryStatus = await recordTravelTelegramReceipt(...base, {
    fetchImpl: async () => ({ ok: true, status: 500, json: async () => 1 }),
  });
  assert.deepEqual(contradictoryStatus, { ok: false, matched: 0, error: "http_error" });
  const thrown = await recordTravelTelegramReceipt(...base, { fetchImpl: async () => { throw new Error("raw-secret"); } });
  assert.deepEqual(thrown, { ok: false, matched: 0, error: "network_error" });
  const noFetch = await recordTravelTelegramReceipt(...base, { fetchImpl: null });
  assert.deepEqual(noFetch, { ok: false, matched: 0, error: "network_error" });
  const unreadable = await recordTravelTelegramReceipt(...base, {
    fetchImpl: async () => ({ ok: true, status: 200, json: async () => { throw new Error("raw-secret"); } }),
  });
  assert.deepEqual(unreadable, { ok: false, matched: 0, error: "unreadable_response" });
});

test("T-5 physical event uses computed departure and non-travel uses event start", () => {
  const physical = event();
  const route = { durationSeconds: 30 * 60 };
  const departure = computeDepartureMs(physical, route, { bufferMin: 5 });
  assert.equal(departure, START - 35 * 60 * 1000);
  assert.equal(computeReminderDueAt(physical, { departureMs: departure }), START - 40 * 60 * 1000);

  const online = event({ id: "online", location: "", online: true });
  assert.equal(computeDepartureMs(online, null), START);
  assert.equal(computeReminderDueAt(online, { departureMs: START }), START - T5_MS);
});

test("T-5 due window includes threshold and 15-minute catch-up, but not early/late ticks", () => {
  const dueAt = START - T5_MS;
  assert.equal(isReminderDue(dueAt - 1, dueAt), false);
  assert.equal(isReminderDue(dueAt, dueAt), true);
  assert.equal(isReminderDue(dueAt + CATCH_UP_MS, dueAt), true);
  assert.equal(isReminderDue(dueAt + CATCH_UP_MS + 1, dueAt), false);
});

test("next event is the first timed non-helper event and eligibility is independent of call settings", () => {
  const helper = event({ id: "travel", summary: "[Travel] helper", startMs: START - 30 * 60 * 1000 });
  const first = event({ id: "first", startMs: START + 5 * 60 * 1000 });
  const later = event({ id: "later", startMs: START + 20 * 60 * 1000 });
  assert.equal(nextReminderEvent([later, helper, first], NOW).id, "first");
  assert.equal(nextReminderEvent([event({ startMs: NOW - 10 * 60 * 1000 - 1 })], NOW), null);
});

test("origin precedence is fresh live location, previous venue within 90m, then home", () => {
  const previous = event({ id: "previous", summary: "前の予定", location: "東京駅", startMs: START - 2 * 60 * 60 * 1000, endMs: START - 30 * 60 * 1000 });
  const current = event();
  const fresh = { latitude: 35.681, longitude: 139.767, observedAtMs: NOW - 1000, expiresAtMs: NOW + 10 * 60 * 1000 };
  assert.deepEqual(resolveReminderOrigin(current, { events: [previous, current], liveLocation: fresh, home: HOME, nowMs: NOW }), {
    kind: "live", value: "geo:35.681,139.767",
  });

  const expired = { ...fresh, expiresAtMs: NOW };
  assert.deepEqual(resolveReminderOrigin(current, { events: [previous, current], liveLocation: expired, home: HOME, nowMs: NOW }), {
    kind: "previous", value: "東京駅",
  });
  const far = { ...previous, endMs: START - 2 * 60 * 60 * 1000 };
  assert.deepEqual(resolveReminderOrigin(current, { events: [far, current], home: HOME, nowMs: NOW }), {
    kind: "home", value: HOME,
  });
  assert.equal(resolveReminderOrigin(current, { events: [far, current], home: "", nowMs: NOW }), null);
});

test("resolved destination uses a unique adjacent outbound Travel location only", () => {
  const current = event({ id: "target", location: "渋谷" });
  const older = { id: "travel-old", summary: "[Travel] 🚆 unrelated", location: "旧住所", startMs: START - 50 * 60000, endMs: START - 5 * 60000 };
  const latest = { id: "travel-latest", summary: "🚆 移動 home→complete", location: "東京都渋谷区神南1-1-1", startMs: START - 40 * 60000, endMs: START + 30000 };
  assert.equal(resolveReminderDestination(current, { events: [older, latest, current] }), latest.location);

  const returnBlock = { summary: "[Travel] 🚆 return", location: "帰宅住所", startMs: START + 30000, endMs: START + 30000 };
  const pending = { summary: "[PENDING] helper", location: "保留住所", startMs: START - 10 * 60000, endMs: START };
  const unrelated = { summary: "[Travel] 🚆 unrelated", location: "遠い住所", startMs: START - 30 * 600000, endMs: START - 30 * 600000 };
  const empty = { summary: "[Travel] 🚆 empty", location: "", startMs: START - 5 * 60000, endMs: START };
  assert.equal(resolveReminderDestination(current, { events: [returnBlock, pending, unrelated, empty, current] }), current.location);
});

test("resolved destination rejects a home-destination return block", () => {
  const current = event({ id: "target-home-return", location: "MUIT 出社 (着席)" });
  const homeReturn = { summary: "[Travel] 🚆 赤坂→南元町", location: HOME, startMs: START - 20 * 60000, endMs: START };
  assert.equal(resolveReminderDestination(current, { events: [homeReturn, current], home: HOME }), current.location);
});

test("resolved destination rejects an old-home return block by event geometry", () => {
  const previous = event({
    id: "previous-old-home",
    summary: "前の予定",
    location: "赤坂",
    startMs: START - 60 * 60000,
    endMs: START - 20 * 60000,
  });
  const returnBlock = {
    id: "return-old-home",
    summary: "[Travel] 🚆 赤坂→旧自宅",
    location: "東京都新宿区1丁目1番1号 旧建物",
    startMs: previous.endMs,
    endMs: START,
  };
  const current = event({ id: "target-after-return", location: "MUIT 出社 (着席)" });
  assert.equal(
    resolveReminderDestination(current, { events: [previous, returnBlock, current], home: HOME }),
    current.location,
  );
});

test("resolved destination fails closed when adjacent candidates are ambiguous", () => {
  const current = event({ id: "target-ambiguous", location: "渋谷" });
  const ambiguous = [
    { summary: "[Travel] 🚆 first", location: "候補A", startMs: START - 40 * 60000, endMs: START },
    { summary: "[Travel] 🚆 second", location: "候補B", startMs: START - 30 * 60000, endMs: START + 30000 },
  ];
  assert.equal(resolveReminderDestination(current, { events: [...ambiguous, current], home: HOME }), current.location);
});

test("resolved destination fails closed when the adjacent Travel block belongs to a different nearby event", () => {
  const eventB = event({ id: "event-b", summary: "歯医者", location: "銀座4丁目歯科", startMs: START });
  const eventC = event({ id: "event-c", summary: "打ち合わせ", location: "六本木ヒルズ森タワー", startMs: START + 90000 });
  const travelForC = {
    id: "travel-for-c", summary: "[Travel] 🚆 移動", location: "六本木ヒルズ森タワー",
    startMs: START - 20 * 60000, endMs: START + 30000,
  };
  assert.equal(resolveReminderDestination(eventB, { events: [eventB, eventC, travelForC] }), eventB.location);
});

test("resolved destination normalizes full-width digits and dash variants before comparing against home", () => {
  const home = "東京都新宿区南元町1-1-1";
  const current = event({ id: "target-fullwidth-home", location: "MUIT 出社 (着席)" });
  const fullwidthHomeReturn = {
    summary: "[Travel] 🚆 帰宅", location: "東京都新宿区南元町１－１－１",
    startMs: START - 20 * 60000, endMs: START,
  };
  assert.equal(resolveReminderDestination(current, { events: [fullwidthHomeReturn, current], home }), current.location);
});

test("travel reminder routes through resolved destination while displaying the original event location", async () => {
  const destination = "東京都渋谷区神南1-1-1";
  const current = event({ id: "target-route", location: "渋谷", startMs: NOW + 3 * T5_MS, endMs: NOW + 63 * 60000 });
  const outbound = { id: "travel-route", summary: "[Travel] 🚆 home→resolved", location: destination, startMs: current.startMs - 40 * 60000, endMs: current.startMs - 30000 };
  const seen = [], sent = [];
  const result = await travelReminderOnce({ uid: "u-destination", telegram_chat_id: "chat-destination", notifications_enabled: true }, NOW, {
    events: [outbound, current], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo", telegramToken: "token", supaUrl: "supa", supaKey: "key",
    directionsRoute: async (_origin, to) => { seen.push(to); return { durationSeconds: 5 * 60 }; },
    claimTravel: async () => true,
    sendMessage: async (_token, _chat, text) => { sent.push(text); return { ok: true, result: { message_id: 709 } }; },
    recordTravelTelegramReceipt: successfulReceipt,
  });
  assert.equal(result.status, "sent");
  assert.deepEqual(seen, [destination]);
  assert.match(sent[0], /目的地: 渋谷/);
  assert.doesNotMatch(sent[0], new RegExp(destination));
});

test("formatter emits the canonical ordered Japanese route shape and only provider facts", () => {
  const text = formatTravelReminder(event(), routeFixture(), {
    departureMs: Date.parse("2026-08-28T04:15:00.000Z"),
    timezone: "Asia/Tokyo",
  });
  assert.equal(text, [
    "🚆 次は 14:00「打ち合わせ」",
    "13:15 出発 → 14:00 到着予定",
    "目的地: 渋谷",
    "",
    "🚶 出発地 → 東京駅 2番線",
    "徒歩 5分",
    "13:20 東京駅 2番線",
    "丸ノ内線・荻窪行 → 13:40 新宿駅",
    "13:46 新宿駅からJR線 → 13:55 渋谷駅",
    "乗換 1回 / IC 209円",
    "",
    "※ 出口番号は経路元が返した場合だけ表示します。運行情報が変わることがあります。",
  ].join("\n"));
  assert.doesNotMatch(text, /出口\d|best|混雑|車両/);

  const withoutOptional = formatTravelReminder(event(), routeFixture({
    transferCount: null,
    fare: null,
    accessWalkSeconds: null,
    egressWalkSeconds: null,
    steps: routeFixture().steps.map((step) => ({ ...step, from: { ...step.from, platform: null } })),
  }), { departureMs: Date.parse("2026-08-28T04:15:00.000Z"), timezone: "Asia/Tokyo" });
  assert.doesNotMatch(withoutOptional, /番線|乗換|円/);
});

test("route failure sends event-only fallback with an explicit unavailable sentence", () => {
  const text = formatTravelReminder(event(), null, {
    departureMs: Date.parse("2026-08-28T04:15:00.000Z"),
    timezone: "Asia/Tokyo",
  });
  assert.match(text, /次は 14:00「打ち合わせ」/);
  assert.doesNotMatch(text, /出発|到着予定/);
  assert.match(text, /目的地: 渋谷/);
  assert.match(text, /経路を取得できませんでした/);
  assert.doesNotMatch(text, /丸ノ内線|209円/);

  const locationless = formatTravelReminder(event({ location: "", online: true }), null, {
    departureMs: START, timezone: "Asia/Tokyo",
  });
  assert.doesNotMatch(locationless, /目的地:/);
});

test("formatter escapes Calendar/provider text for Telegram HTML", () => {
  const text = formatTravelReminder(event({ summary: "<b>会議</b> &確認", location: "<渋谷> &出口" }), routeFixture({
    steps: [{
      ...routeFixture().steps[0],
      from: { name: "<東京駅>", platform: "<2番線>" },
      to: { name: "新宿 &駅", platform: null },
      service: "<丸ノ内線>", headsign: "荻窪 &行",
    }],
  }), { departureMs: Date.parse("2026-08-28T04:15:00.000Z"), timezone: "Asia/Tokyo" });
  assert.match(text, /&lt;b&gt;会議&lt;\/b&gt; &amp;確認/);
  assert.match(text, /目的地: &lt;渋谷&gt; &amp;出口/);
  assert.match(text, /&lt;東京駅&gt; &lt;2番線&gt;/);
  assert.doesNotMatch(text, /<b>|<渋谷>|<丸ノ内線>/);
});

test("travelReminderOnce claims telegram-t5 before send, suppresses duplicate, and releases failures", async () => {
  const calls = [];
  const dueEvent = event({ startMs: NOW + 3 * T5_MS, startIso: "2026-08-28T13:15:00+09:00" });
  const deps = {
    events: [dueEvent], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo",
    directionsRoute: async () => ({ ...routeFixture(), durationSeconds: 5 * 60 }),
    claimTravel: async (...args) => { calls.push(["claim", ...args]); return true; },
    unclaimTravel: async (...args) => { calls.push(["release", ...args]); },
    sendMessage: async (...args) => { calls.push(["send", ...args]); return { ok: true, result: { message_id: 701 } }; },
    recordTravelTelegramReceipt: successfulReceipt,
    telegramToken: "token", supaUrl: "supa", supaKey: "key", log: (line) => calls.push(["log", line]),
  };
  const first = await travelReminderOnce({ uid: "u-123456789012345", telegram_chat_id: "chat-1", notifications_enabled: true }, NOW, deps);
  assert.equal(first.status, "sent");
  assert.equal(calls.findIndex((x) => x[0] === "claim") < calls.findIndex((x) => x[0] === "send"), true);
  assert.equal(calls.find((x) => x[0] === "claim")[3], "telegram-t5");
  assert.equal(first.telegramMessageId, 701);
  assert.match(calls.find((x) => x[0] === "log")[1], /uid=u-123456789/);
  assert.doesNotMatch(calls.find((x) => x[0] === "log")[1], /打ち合わせ|渋谷|\+81|@/);

  const duplicate = await travelReminderOnce({ uid: "u-123456789012345", telegram_chat_id: "chat-1", notifications_enabled: true }, NOW, {
    ...deps,
    claimTravel: async () => false,
    sendMessage: async () => { throw new Error("duplicate must not send"); },
  });
  assert.equal(duplicate.status, "suppressed");

  const failedCalls = [];
  const failed = await travelReminderOnce({ uid: "u-123456789012345", telegram_chat_id: "chat-1", notifications_enabled: true }, NOW, {
    ...deps,
    claimTravel: async (...args) => { failedCalls.push(["claim", ...args]); return true; },
    unclaimTravel: async (...args) => { failedCalls.push(["release", ...args]); },
    sendMessage: async () => ({ ok: false, status: 500 }),
  });
  assert.equal(failed.status, "send_failed");
  assert.equal(failedCalls.some((x) => x[0] === "release" && x[3] === "telegram-t5"), true);

  const missingIdCalls = [];
  const missingId = await travelReminderOnce({ uid: "u-123456789012345", telegram_chat_id: "chat-1", notifications_enabled: true }, NOW, {
    ...deps,
    claimTravel: async (...args) => { missingIdCalls.push(["claim", ...args]); return true; },
    unclaimTravel: async (...args) => { missingIdCalls.push(["release", ...args]); },
    sendMessage: async () => ({ ok: true, result: {} }),
  });
  assert.equal(missingId.status, "delivery_unknown");
  assert.equal(missingIdCalls.some((x) => x[0] === "release"), false);
});

test("travelReminderOnce keeps the claim on throw or delivery_unknown and replay sends zero", async () => {
  const dueEvent = event({ id: "unknown-delivery", startMs: NOW + 3 * T5_MS, startIso: "2026-08-28T13:15:00+09:00" });
  for (const mode of ["throw", "unknown", "ambiguous"]) {
    const calls = [], logs = [];
    let claims = 0;
    const deps = {
      events: [dueEvent], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo",
      directionsRoute: async () => ({ ...routeFixture(), durationSeconds: 5 * 60 }),
      claimTravel: async (...args) => { calls.push(["claim", ...args]); claims++; return claims === 1; },
      unclaimTravel: async (...args) => { calls.push(["release", ...args]); return true; },
      sendMessage: async (...args) => {
        calls.push(["send", ...args]);
        if (mode === "throw") throw new Error("transport failure");
        if (mode === "ambiguous") return { error: "upstream reset" };
        return { ok: false, delivery_unknown: true };
      },
      telegramToken: "token", supaUrl: "supa", supaKey: "key", log: (line) => logs.push(line),
    };
    const first = await travelReminderOnce({ uid: `u-${mode}`, telegram_chat_id: "chat", notifications_enabled: true }, NOW, deps);
    const replay = await travelReminderOnce({ uid: `u-${mode}`, telegram_chat_id: "chat", notifications_enabled: true }, NOW, deps);
    assert.equal(first.status, "delivery_unknown", mode);
    assert.equal(replay.status, "suppressed", mode);
    assert.deepEqual(calls.map((entry) => entry[0]), ["claim", "send", "claim"], mode);
    assert.equal(logs.length, 1, mode);
    assert.match(logs[0], /reconciliation required/);
  }
});

test("Telegram receipt is recorded after claim and send in exact order", async () => {
  const calls = [];
  const dueEvent = event({ id: "receipt-order", startMs: NOW + 3 * T5_MS, startIso: "2026-08-28T13:15:00+09:00" });
  const user = { uid: "receipt-user", telegram_chat_id: "receipt-chat", notifications_enabled: true };
  const deps = {
    events: [dueEvent], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo",
    directionsRoute: async () => ({ durationSeconds: 5 * 60 }),
    claimTravel: async (...args) => { calls.push(["claim", ...args]); return true; },
    sendMessage: async (...args) => { calls.push(["send", ...args]); return { ok: true, result: { message_id: 905 } }; },
    recordTravelTelegramReceipt: async (...args) => {
      calls.push(["receipt", ...args]);
      assert.deepEqual(args.slice(0, 6), [user.uid, dueEvent.id, "telegram-t5", 905, "supa", "key"]);
      return { ok: true, matched: 1 };
    },
    telegramToken: "token", supaUrl: "supa", supaKey: "key", log: () => {},
  };
  const result = await travelReminderOnce(user, NOW, deps);
  assert.equal(result.status, "sent");
  assert.equal(result.telegramMessageId, 905);
  assert.deepEqual(calls.map((entry) => entry[0]), ["claim", "send", "receipt"]);
});

test("Telegram receipt mismatch, failure, and throw retain the claim and replay sends nothing", async () => {
  const scenarios = [
    { result: { ok: true, matched: 0 } },
    { result: { ok: false, matched: 0, error: "raw-provider-error" } },
    { throws: true },
  ];
  for (const scenario of scenarios) {
    const calls = [], logs = [];
    let claims = 0;
    const forbidden = ["receipt-user", "receipt-event", "route-secret", "901", "raw-provider-error", "token-secret"];
    const dueEvent = event({ id: "receipt-event", summary: "route-secret", startMs: NOW + 3 * T5_MS, startIso: "2026-08-28T13:15:00+09:00" });
    const deps = {
      events: [dueEvent], home: "home-secret", mapsKey: "maps-secret", timezone: "Asia/Tokyo",
      directionsRoute: async () => ({ durationSeconds: 5 * 60 }),
      claimTravel: async (...args) => { calls.push(["claim", ...args]); claims += 1; return claims === 1; },
      unclaimTravel: async (...args) => { calls.push(["release", ...args]); return true; },
      sendMessage: async (...args) => { calls.push(["send", ...args]); return { ok: true, result: { message_id: 901 } }; },
      recordTravelTelegramReceipt: async (...args) => {
        calls.push(["receipt", ...args]);
        if (scenario.throws) throw new Error("raw-provider-error");
        return scenario.result;
      },
      telegramToken: "token-secret", supaUrl: "supa-secret", supaKey: "key-secret",
      log: (line) => logs.push(String(line)),
    };
    const user = { uid: "receipt-user", telegram_chat_id: "chat-secret", notifications_enabled: true };
    const first = await travelReminderOnce(user, NOW, deps);
    const replay = await travelReminderOnce(user, NOW, deps);
    assert.equal(first.status, "delivery_unknown");
    assert.equal(replay.status, "suppressed");
    assert.deepEqual(calls.map((entry) => entry[0]), ["claim", "send", "receipt", "claim"]);
    assert.equal(calls.filter((entry) => entry[0] === "release").length, 0);
    assert.equal(calls.filter((entry) => entry[0] === "receipt").length, 1);
    assert.deepEqual(logs, ["[travel-reminder] delivery receipt reconciliation required"]);
    assert.ok(logs.every((line) => forbidden.every((sentinel) => !line.includes(sentinel))),
      `sensitive value leaked: ${JSON.stringify(logs)}`);
  }
});

test("provider rejection, transport uncertainty, and duplicate claim never write a receipt", async () => {
  const dueEvent = event({ id: "receipt-boundary", startMs: NOW + 3 * T5_MS, startIso: "2026-08-28T13:15:00+09:00" });
  let receiptWrites = 0;
  let releases = 0;
  const base = {
    events: [dueEvent], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo",
    directionsRoute: async () => ({ durationSeconds: 5 * 60 }),
    telegramToken: "token", supaUrl: "supa", supaKey: "key", log: () => {},
    recordTravelTelegramReceipt: async () => { receiptWrites += 1; return { ok: true, matched: 1 }; },
  };
  const rejected = await travelReminderOnce({ uid: "rejected", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    ...base,
    claimTravel: async () => true,
    unclaimTravel: async (...args) => { releases += 1; assert.equal(args[2], "telegram-t5"); return true; },
    sendMessage: async () => ({ ok: false, status: 500 }),
  });
  assert.equal(rejected.status, "send_failed");
  assert.equal(releases, 1);
  assert.equal(receiptWrites, 0);

  const uncertain = await travelReminderOnce({ uid: "uncertain", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    ...base,
    claimTravel: async () => true,
    unclaimTravel: async () => { releases += 1; return true; },
    sendMessage: async () => ({ ok: true, result: {} }),
  });
  assert.equal(uncertain.status, "delivery_unknown");
  assert.equal(releases, 1);
  assert.equal(receiptWrites, 0);

  const duplicate = await travelReminderOnce({ uid: "duplicate", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    ...base,
    claimTravel: async () => false,
    sendMessage: async () => { throw new Error("duplicate must not send"); },
  });
  assert.equal(duplicate.status, "suppressed");
  assert.equal(releases, 1);
  assert.equal(receiptWrites, 0);
});

test("travelReminderOnce keeps an accepted receipt claim when the transport status contradicts ok", async () => {
  const dueEvent = event({ id: "contradictory-status", startMs: NOW + 3 * T5_MS, startIso: "2026-08-28T13:15:00+09:00" });
  let claims = 0;
  let sends = 0;
  let releases = 0;
  const deps = {
    events: [dueEvent], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo",
    directionsRoute: async () => ({ durationSeconds: 5 * 60 }),
    claimTravel: async () => { claims += 1; return claims === 1; },
    unclaimTravel: async () => { releases += 1; return true; },
    sendMessage: async () => { sends += 1; return { ok: true, result: { message_id: 803 }, status: 500 }; },
    recordTravelTelegramReceipt: successfulReceipt,
    telegramToken: "token", supaUrl: "supa", supaKey: "key", log: () => {},
  };

  const first = await travelReminderOnce({ uid: "u-contradictory", telegram_chat_id: "chat", notifications_enabled: true }, NOW, deps);
  const replay = await travelReminderOnce({ uid: "u-contradictory", telegram_chat_id: "chat", notifications_enabled: true }, NOW, deps);

  assert.equal(first.status, "sent");
  assert.equal(first.telegramMessageId, 803);
  assert.equal(replay.status, "suppressed");
  assert.equal(sends, 1);
  assert.equal(releases, 0);
});

test("travelReminderOnce reads reserved fallback keys as exact eq values and uses the associated destination", async () => {
  const summary = '予定: A, B.(C) "引用"\\suffix';
  const targetStart = NOW + 3 * T5_MS;
  const dueEvent = event({ id: "", summary, location: "元の会場名", startMs: targetStart, endMs: targetStart + 60 * 60000, startIso: "2026-08-28T13:15:00+09:00" });
  const previous = event({ id: "previous-reserved", summary: "前の予定", location: "前の場所", startMs: targetStart - 90 * 60000, endMs: targetStart - 30 * 60000 });
  const outbound = { id: "target-go-reserved", summary: "[Travel] 🚆 前の場所→解決住所", location: "東京都渋谷区神南1-1-1", startMs: previous.endMs, endMs: targetStart };
  const uid = 'tenant:alpha,beta.(gamma)"\\suffix';
  const eventKey = `${dueEvent.startMs}:${summary}`;
  const queries = [], destinations = [];
  let exactTargetRows = 0;
  const result = await travelReminderOnce({ uid, telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    events: [previous, outbound, dueEvent], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo", telegramToken: "token", supaUrl: "https://supa.example", supaKey: "key",
    fetchImpl: async (url) => {
      const value = String(url);
      queries.push(value);
      const params = new URL(value).searchParams;
      const exactTarget = params.get("uid") === `eq.${uid}`
        && params.get("event_key") === `eq.${eventKey}`
        && params.get("leg") === "eq.go";
      if (exactTarget) {
        exactTargetRows += 1;
        return { ok: true, status: 200, json: async () => [{ event_key: eventKey }] };
      }
      return { ok: true, status: 200, json: async () => [] };
    },
    directionsRoute: async (_origin, destination) => { destinations.push(destination); return { durationSeconds: 5 * 60 }; },
    claimTravel: async () => true,
    sendMessage: async () => ({ ok: true, result: { message_id: 804 } }),
    recordTravelTelegramReceipt: successfulReceipt,
  });

  assert.equal(result.status, "sent");
  assert.deepEqual(destinations, [outbound.location]);
  assert.equal(exactTargetRows, 1);
  assert.equal(queries.length, 2);
  const params = new URL(queries[0]).searchParams;
  assert.equal(params.get("uid"), `eq.${uid}`);
  assert.equal(params.get("event_key"), `eq.${eventKey}`);
  assert.equal(params.get("leg"), "eq.go");
});

test("stale target go claim plus previous event return claim falls back to event location", async () => {
  const targetStart = NOW + 3 * T5_MS;
  const previous = event({ id: "previous-return", summary: "前の予定", location: "前の場所", startMs: targetStart - 90 * 60000, endMs: targetStart - 30 * 60000 });
  const outbound = { id: "target-go-stale", summary: "[Travel] 🚆 前の場所→解決住所", location: "東京都渋谷区神南1-1-1", startMs: previous.endMs, endMs: targetStart };
  const target = event({ id: "target-event-stale", startMs: targetStart, endMs: targetStart + 60 * 60000, location: "元の会場名" });
  const destinations = [], queries = [];
  const result = await travelReminderOnce({ uid: "tenant-stale", telegram_chat_id: "chat-stale", notifications_enabled: true }, NOW, {
    events: [previous, outbound, target], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo", telegramToken: "token", supaUrl: "supa", supaKey: "key",
    fetchImpl: async (url, init) => {
      const value = String(url);
      queries.push({ url: value, init });
      if (value.includes("leg=eq.go")) return { ok: true, status: 200, json: async () => [{ event_key: "target-event-stale" }] };
      return { ok: true, status: 200, json: async () => [{ event_key: "previous-return" }] };
    },
    directionsRoute: async (_origin, destination) => { destinations.push(destination); return { durationSeconds: 5 * 60 }; },
    claimTravel: async () => true, sendMessage: async () => ({ ok: true, result: { message_id: 803 } }),
    recordTravelTelegramReceipt: successfulReceipt,
  });
  assert.equal(result.status, "sent");
  assert.deepEqual(destinations, [target.location]);
  assert.equal(queries.length, 2);
  assert.match(queries[1].url, /event_key=eq\.previous-return/);
  assert.match(queries[1].url, /leg=eq\.return/);
});

test("adjacent outbound Travel starts at prior event end only when target go claim is present", async () => {
  const targetStart = NOW + 3 * T5_MS;
  const previous = event({ id: "previous-event", summary: "前の予定", location: "前の場所", startMs: targetStart - 90 * 60000, endMs: targetStart - 30 * 60000 });
  const outbound = { id: "target-go", summary: "[Travel] 🚆 前の場所→解決住所", location: "東京都渋谷区神南1-1-1", startMs: previous.endMs, endMs: targetStart };
  const target = event({ id: "target-event", startMs: targetStart, endMs: targetStart + 60 * 60000 });
  const destinations = [], queries = [];
  const result = await travelReminderOnce({ uid: "tenant-target", telegram_chat_id: "chat-target", notifications_enabled: true }, NOW, {
    events: [previous, outbound, target], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo", telegramToken: "token", supaUrl: "supa", supaKey: "key",
    fetchImpl: async (url, init) => { queries.push({ url: String(url), init }); return { ok: true, status: 200, json: async () => [{ event_key: "target-event" }] }; },
    directionsRoute: async (_origin, destination) => { destinations.push(destination); return { durationSeconds: 5 * 60 }; },
    claimTravel: async () => true, sendMessage: async () => ({ ok: true, result: { message_id: 801 } }),
    recordTravelTelegramReceipt: successfulReceipt,
  });
  assert.equal(result.status, "sent");
  assert.deepEqual(destinations, [outbound.location]);
  assert.equal(queries.length, 2);
  assert.match(queries[0].url, /uid=eq\.tenant-target/);
  assert.match(queries[0].url, /event_key=eq\.target-event/);
  assert.match(queries[0].url, /leg=eq\.go/);
  assert.equal(queries[0].init.method, undefined);
  assert.match(queries[1].url, /event_key=eq\.previous-event/);
  assert.match(queries[1].url, /leg=eq\.return/);
});

test("the same adjacent geometry without a target go claim falls back to event location", async () => {
  const targetStart = NOW + 3 * T5_MS;
  const previous = event({ id: "previous-no-claim", summary: "前の予定", location: "前の場所", startMs: targetStart - 90 * 60000, endMs: targetStart - 30 * 60000 });
  const outbound = { id: "target-go-no-claim", summary: "[Travel] 🚆 前の場所→解決住所", location: "東京都渋谷区神南1-1-1", startMs: previous.endMs, endMs: targetStart };
  const target = event({ id: "target-event-no-claim", startMs: targetStart, endMs: targetStart + 60 * 60000, location: "元の会場名" });
  const destinations = [];
  const result = await travelReminderOnce({ uid: "tenant-no-claim", telegram_chat_id: "chat-no-claim", notifications_enabled: true }, NOW, {
    events: [previous, outbound, target], home: HOME, mapsKey: "maps", timezone: "Asia/Tokyo", telegramToken: "token", supaUrl: "supa", supaKey: "key",
    fetchImpl: async () => ({ ok: true, status: 200, json: async () => [] }),
    directionsRoute: async (_origin, destination) => { destinations.push(destination); return { durationSeconds: 5 * 60 }; },
    claimTravel: async () => true, sendMessage: async () => ({ ok: true, result: { message_id: 802 } }),
    recordTravelTelegramReceipt: successfulReceipt,
  });
  assert.equal(result.status, "sent");
  assert.deepEqual(destinations, [target.location]);
});

test("travelReminderOnce sends an event-only reminder when origin is unavailable and does not invent a route", async () => {
  const sent = [];
  const online = event({ id: "online", location: "", online: true, startMs: NOW + T5_MS, startIso: "2026-08-28T13:05:00+09:00" });
  const result = await travelReminderOnce({ uid: "u-online", telegram_chat_id: "chat-online", notifications_enabled: true }, NOW, {
    events: [online],
    directionsRoute: async () => { throw new Error("online must not route"); },
    claimTravel: async () => true,
    sendMessage: async (_token, _chat, text) => { sent.push(text); return { ok: true, result: { message_id: 702 } }; },
    recordTravelTelegramReceipt: successfulReceipt,
    telegramToken: "token", supaUrl: "supa", supaKey: "key",
  });
  assert.equal(result.status, "sent");
  assert.match(sent[0], /次は/);
  assert.doesNotMatch(sent[0], /経路を取得できませんでした/);
});

test("monthly allowance blocks new route effects and emits its notice at most when authorized", async () => {
  for (const notify of [true, false]) {
    let routes = 0, sends = 0, releases = 0;
    const due = event({ id: `allowance-${notify}`, startMs: NOW + T5_MS });
    const result = await travelReminderOnce({ uid: "allowance-user", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
      events: [due], home: HOME, telegramToken: "token", supaUrl: "supa", supaKey: "key",
      reserveManagedAction: async () => ({ allowed: false, notify }),
      directionsRoute: async () => { routes += 1; return routeFixture(); },
      claimTravel: async () => true,
      unclaimTravel: async () => { releases += 1; return true; },
      sendMessage: async (_token, _chat, text) => {
        sends += 1;
        assert.match(text, /今月の無料利用分を使い切りました/);
        return { ok: true, result: { message_id: 799 } };
      },
      recordTravelTelegramReceipt: successfulReceipt,
    });
    assert.equal(routes, 0, "no paid route call after allowance exhaustion");
    assert.equal(sends, notify ? 1 : 0);
    assert.equal(result.status, notify ? "sent" : "suppressed");
    assert.equal(releases, notify ? 0 : 1);
  }
});

test("travelReminderOnce does not send before threshold", async () => {
  let sends = 0;
  const dueEvent = event({ id: "early", startMs: NOW + 15 * T5_MS, startIso: "2026-08-28T14:15:00+09:00" });
  const result = await travelReminderOnce({ uid: "u-early", telegram_chat_id: "chat-early", notifications_enabled: true }, NOW, {
    events: [dueEvent], home: HOME,
    directionsRoute: async () => ({ durationSeconds: 5 * 60 }),
    claimTravel: async () => true,
    sendMessage: async () => { sends += 1; return { ok: true, result: { message_id: 703 } }; },
    telegramToken: "token", supaUrl: "supa", supaKey: "key",
  });
  assert.equal(result.status, "suppressed");
  assert.equal(sends, 0);
});

test("travelReminderOnce fails closed without Supabase and performs no route, claim, or send", async () => {
  let routes = 0;
  let claims = 0;
  let sends = 0;
  const online = event({ id: "no-supa", location: "https://meet.example/room", online: true, startMs: NOW + T5_MS });
  const result = await travelReminderOnce({ uid: "u-no-supa", telegram_chat_id: "chat-no-supa", notifications_enabled: true }, NOW, {
    events: [online], home: HOME, telegramToken: "token",
    directionsRoute: async () => { routes += 1; return null; },
    claimTravel: async () => { claims += 1; return true; },
    sendMessage: async () => { sends += 1; return { ok: true, result: { message_id: 704 } }; },
  });
  assert.equal(result.status, "skipped");
  assert.equal(routes, 0);
  assert.equal(claims, 0);
  assert.equal(sends, 0);
});

test("online event reminder catches up from start+1m through start+10m, but not later", async () => {
  const sent = [];
  const eventAt = (offsetMs, id) => event({ id, location: "https://meet.example/room", online: true, startMs: NOW - offsetMs });
  const deps = {
    home: HOME, supaUrl: "supa", supaKey: "key", telegramToken: "token",
    claimTravel: async () => true,
    sendMessage: async (_token, _chat, text) => { sent.push(text); return { ok: true, result: { message_id: 705 + sent.length } }; },
    recordTravelTelegramReceipt: successfulReceipt,
  };
  const one = await travelReminderOnce({ uid: "u-online-1", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    ...deps, events: [eventAt(60 * 1000, "online-1")],
  });
  assert.equal(one.status, "sent");
  const boundary = await travelReminderOnce({ uid: "u-online-10", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    ...deps, events: [eventAt(10 * 60 * 1000, "online-10")],
  });
  assert.equal(boundary.status, "sent");
  const late = await travelReminderOnce({ uid: "u-online-late", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    ...deps, events: [eventAt(10 * 60 * 1000 + 1, "online-late")],
  });
  assert.equal(late.status, "suppressed");
  assert.equal(sent.length, 2);
  assert.doesNotMatch(sent[0], /目的地:|経路を取得できませんでした/);
});

test("Calendar URL online event reaches the reminder at event-start T-5 without routing", async () => {
  const onlineStart = new Date(NOW - 60 * 1000).toISOString();
  const calendar = { async listEventsRaw() {
    return [{ id: "calendar-online", summary: "配信", location: "https://meet.example/room",
      start: { dateTime: onlineStart }, end: { dateTime: new Date(NOW + 30 * 60 * 1000).toISOString() } }];
  } };
  const events = await fetchUpcomingEvents("u-calendar-online", {
    nowMs: NOW, horizonH: 1, lookbackMs: 10 * 60 * 1000, calendar,
  });
  let routes = 0;
  const sent = [];
  const result = await travelReminderOnce({ uid: "u-calendar-online", telegram_chat_id: "chat", notifications_enabled: true }, NOW, {
    events, home: HOME, supaUrl: "supa", supaKey: "key", telegramToken: "token",
    directionsRoute: async () => { routes += 1; return null; }, claimTravel: async () => true,
    sendMessage: async (_token, _chat, text) => { sent.push(text); return { ok: true, result: { message_id: 708 } }; },
    recordTravelTelegramReceipt: successfulReceipt,
  });
  assert.equal(events[0].online, true);
  assert.equal(result.status, "sent");
  assert.equal(routes, 0);
  assert.match(sent[0], /次は/);
});
