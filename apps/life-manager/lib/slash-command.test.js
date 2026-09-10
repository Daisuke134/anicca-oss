// lib/slash-command.test.js — spec §12.1 row 4: the generic slash-command router.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  parseSlashCommand,
  slashAliasText,
  helpMessage,
  handleSlashCommand,
} = require("./slash-command.js");
const { buildInvestmentReply } = require("./investment-chat.js");
const { buildGigReply, COCONALA_SIGNUP_URL, CROWDWORKS_SIGNUP_URL, CROWDWORKS_VERIFICATION_URL } = require("./gig-chat.js");

const NOW = Date.parse("2026-07-30T12:00:00.000Z");
const ADDRESS = `0x${"a".repeat(40)}`;

const ROW = Object.freeze({
  uid: "u1", telegram_chat_id: "100", tg_onboard_stage: "done",
  calendar_provider: "composio_gcal", phone: "+819012345678", paid: true,
  gmail_skipped: true, payout_destination: null, name: "Fixture",
});

test("/invest sends the shared host-neutral reply including its signup button", async () => {
  const snapshot = { lifecycle: "setup_required" };
  const expected = buildInvestmentReply(snapshot);
  const { sent, deps } = harness({ getInvestmentState: async () => snapshot });
  const outcome = await handleSlashCommand(parseSlashCommand("/invest"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "invest", ok: true, providerMessageId: 1 });
  assert.equal(sent[0].text, expected.text);
  assert.equal(sent[0].extra.reply_markup.inline_keyboard[0][0].url, "https://app.alpaca.markets/signup");
});

test("/economy projects the authenticated tenant state and exposes emergency pause", async () => {
  const snapshot = {
    citizen: { walletAddress: ADDRESS },
    pausedAt: null,
    latestJob: { status: "queued", cycle: 2, availableAt: "2026-09-11T00:10:00.000Z" },
    latestReceipt: { outcome: "completed", cycle: 1, profitable: false },
  };
  const { sent, deps } = harness({ getEconomyState: async (uid) => {
    assert.equal(uid, "u1");
    return snapshot;
  } });
  const outcome = await handleSlashCommand(parseSlashCommand("/economy"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "economy", ok: true, providerMessageId: 1 });
  assert.match(sent[0].text, /Agent Economy/);
  assert.match(sent[0].text, /queued/);
  assert.equal(sent[0].extra.reply_markup.inline_keyboard[0][0].callback_data, "economy:pause");
});

test("/economy exposes setup button when no citizen exists", async () => {
  const { sent, deps } = harness({ getEconomyState: async () => ({ citizen: null }) });
  const outcome = await handleSlashCommand(parseSlashCommand("/economy"), ROW, deps);
  assert.equal(outcome.ok, true);
  assert.equal(sent[0].extra.reply_markup.inline_keyboard[0][0].callback_data, "economy:setup");
});

test("/invest sends the shared balance and reason without Cloud-specific copy", async () => {
  const snapshot = {
    lifecycle: "in_review",
    account: { equity: "99996.76", cash: "99996.76" },
    decision: { approved: false, reason: "No fresh edge." },
  };
  const { sent, deps } = harness({ getInvestmentState: async () => snapshot });
  await handleSlashCommand(parseSlashCommand("/invest"), ROW, deps);
  assert.equal(sent[0].text, buildInvestmentReply(snapshot).text);
  assert.match(sent[0].text, /資産 \$99996\.76/);
  assert.match(sent[0].text, /No fresh edge\./);
});

test("/invest does not claim delivery without a Telegram provider message id", async () => {
  for (const delivery of [
    undefined,
    null,
    { ok: false },
    { ok: true, result: {} },
    { ok: true, result: { message_id: 0 } },
    { ok: true, result: { message_id: -1 } },
  ]) {
    const { deps } = harness({
      getInvestmentState: async () => ({ lifecycle: "setup_required" }),
      send: async () => delivery,
    });
    const outcome = await handleSlashCommand(parseSlashCommand("/invest"), ROW, deps);
    assert.equal(outcome.ok, false);
    assert.equal(outcome.reason, delivery?.ok === false ? "delivery_failed" : "delivery_unconfirmed");
    assert.equal(Object.hasOwn(outcome, "providerMessageId"), false);
  }
});

test("/invest fails closed when Cloud investment state is unavailable", async () => {
  for (const read of [
    async () => { throw new Error("unavailable"); },
    async () => null,
    async () => ({ lifecycle: "unexpected-provider-state" }),
    async () => ({ lifecycle: ["active"] }),
    async () => ({ lifecycle: "in_review", account: null }),
    async () => ({ lifecycle: "in_review", account: { equity: "0x10", cash: "1" } }),
    async () => ({ lifecycle: "in_review", decision: { approved: true, reason: {} } }),
    async () => ({ lifecycle: "in_review", decision: { approved: true } }),
  ]) {
    const { sent, deps } = harness({ getInvestmentState: read });
    const outcome = await handleSlashCommand(parseSlashCommand("/invest"), ROW, deps);
    assert.equal(outcome.ok, false);
    assert.equal(outcome.reason, "state_unavailable");
    assert.match(sent[0].text, /ライブ注文は出しません/);
  }
});

test("gig and crowd are known commands routed here, not passed through like start/panel", () => {
  const { KNOWN_COMMANDS, parseSlashCommand: parse } = require("./slash-command.js");
  assert.ok(KNOWN_COMMANDS.includes("gig"));
  assert.ok(KNOWN_COMMANDS.includes("crowd"));
  assert.ok(parse("/gig"));
  assert.ok(parse("/crowd"));
});

test("/gig and /crowd are not passed through — an empty deps object still gets a real reply, not handled:false", async () => {
  for (const raw of ["/gig", "/crowd"]) {
    const { sent, deps } = harness();
    const outcome = await handleSlashCommand(parseSlashCommand(raw), ROW, deps);
    assert.notDeepEqual(outcome, { handled: false }, raw);
    assert.equal(outcome.handled, true, raw);
    assert.equal(sent.length, 1, raw);
  }
});

test("helpMessage lists /gig and /crowd", () => {
  const text = helpMessage();
  assert.ok(text.includes("/gig"), "help must list /gig");
  assert.ok(text.includes("/crowd"), "help must list /crowd");
});

test("/gig sends the Coconala setup-required reply with its signup button", async () => {
  const snapshot = { lifecycle: "setup_required" };
  const expected = buildGigReply("coconala", snapshot);
  const { sent, deps } = harness({ getGigState: async (platform) => { assert.equal(platform, "coconala"); return snapshot; } });
  const outcome = await handleSlashCommand(parseSlashCommand("/gig"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "gig", ok: true, providerMessageId: 1 });
  assert.equal(sent[0].text, expected.text);
  assert.equal(sent[0].extra.reply_markup.inline_keyboard[0][0].url, COCONALA_SIGNUP_URL);
});

test("/crowd on verification_required links to the CrowdWorks verification page, not the signup page", async () => {
  const snapshot = { lifecycle: "verification_required" };
  const { sent, deps } = harness({ getGigState: async (platform) => { assert.equal(platform, "crowdworks"); return snapshot; } });
  const outcome = await handleSlashCommand(parseSlashCommand("/crowd"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "crowd", ok: true, providerMessageId: 1 });
  const url = sent[0].extra.reply_markup.inline_keyboard[0][0].url;
  assert.equal(url, CROWDWORKS_VERIFICATION_URL);
  assert.notEqual(url, CROWDWORKS_SIGNUP_URL);
});

test("/gig and /crowd read state through the injected getGigState, scoped to this row's uid", async () => {
  const reads = [];
  const { deps } = harness({
    getGigState: async (platform, uid) => { reads.push({ platform, uid }); return { lifecycle: "active", stats: { listingCount: 2 } }; },
  });
  await handleSlashCommand(parseSlashCommand("/gig"), ROW, deps);
  await handleSlashCommand(parseSlashCommand("/crowd"), ROW, deps);
  assert.deepEqual(reads, [
    { platform: "coconala", uid: "u1" },
    { platform: "crowdworks", uid: "u1" },
  ]);
});

test("a throwing getGigState yields the honest unknown reply, not an exception", async () => {
  for (const raw of ["/gig", "/crowd"]) {
    const { sent, deps } = harness({ getGigState: async () => { throw new Error("boom"); } });
    const outcome = await handleSlashCommand(parseSlashCommand(raw), ROW, deps);
    assert.equal(outcome.ok, false, raw);
    assert.equal(outcome.reason, "state_unavailable", raw);
    assert.match(sent[0].text, /確認できません/, raw);
    assert.equal(/\d/.test(sent[0].text), false, `${raw} unknown reply must not invent a number`);
  }
});

test("/gig with no getGigState dep at all also falls back to the honest unknown reply (reader not yet wired)", async () => {
  const { sent, deps } = harness();
  delete deps.getGigState;
  const outcome = await handleSlashCommand(parseSlashCommand("/gig"), ROW, deps);
  assert.equal(outcome.ok, false);
  assert.equal(outcome.reason, "state_unavailable");
  assert.match(sent[0].text, /確認できません/);
});

test("/gig and /crowd do not claim delivery without a Telegram provider message id", async () => {
  for (const raw of ["/gig", "/crowd"]) {
    for (const delivery of [undefined, null, { ok: false }, { ok: true, result: {} }, { ok: true, result: { message_id: 0 } }]) {
      const { deps } = harness({
        getGigState: async () => ({ lifecycle: "setup_required" }),
        send: async () => delivery,
      });
      const outcome = await handleSlashCommand(parseSlashCommand(raw), ROW, deps);
      assert.equal(outcome.ok, false, raw);
      assert.equal(outcome.reason, delivery?.ok === false ? "delivery_failed" : "delivery_unconfirmed", raw);
      assert.equal(Object.hasOwn(outcome, "providerMessageId"), false, raw);
    }
  }
});

test("/gig active reply contains only numbers present in the snapshot", async () => {
  const { sent, deps } = harness({ getGigState: async () => ({ lifecycle: "active", stats: { listingCount: 5, lastWakeCompleted: true } }) });
  await handleSlashCommand(parseSlashCommand("/gig"), ROW, deps);
  const numbers = sent[0].text.match(/\d+/g) || [];
  assert.deepEqual(numbers, ["5"], "no fabricated number besides the injected listingCount");
});

test("account-scoped /gig and /crowd on an unlinked chat get the setup-first reply and touch no store", async () => {
  for (const raw of ["/gig", "/crowd"]) {
    let reads = 0;
    const { sent, deps } = harness({ getGigState: async () => { reads++; return { lifecycle: "active" }; } });
    const outcome = await handleSlashCommand(parseSlashCommand(raw), null, deps);
    assert.equal(outcome.handled, true, raw);
    assert.equal(outcome.ok, false, raw);
    assert.equal(outcome.reason, "unlinked", raw);
    assert.equal(reads, 0, `${raw} must not read gig state for an unlinked chat`);
    assert.equal(sent.length, 1, raw);
    assert.ok(sent[0].text.includes("/start"), `${raw} setup-first reply names /start`);
  }
});

function harness(overrides = {}) {
  const sent = [];
  const logs = [];
  const deps = {
    token: "t", chatId: "100", base: "https://lm.test",
    // env is injected (never read ambiently) so the LM_COMP_UNTIL projection is hermetic.
    supaUrl: "https://db.test", supaKey: "k", nowMs: NOW, env: {},
    send: async (token, chatId, text, extra) => { sent.push({ token, chatId, text, extra }); return { ok: true, result: { message_id: 1 } }; },
    log: (line) => logs.push(line),
    getLiveLocation: async () => null,
    deleteLiveLocation: async () => ({ deleted: 0 }),
    setStage: async () => {},
    askPayoutQuestion: async () => ({ asked: true }),
    ...overrides,
  };
  return { sent, logs, deps };
}

test("parseSlashCommand recognises /commands, bot-name suffixes, and args; plain text is null", () => {
  assert.deepEqual(parseSlashCommand("/help"), { name: "help", args: "" });
  assert.deepEqual(parseSlashCommand("  /WHERE  "), { name: "where", args: "" });
  assert.deepEqual(parseSlashCommand("/help@LifeManagerBot"), { name: "help", args: "" });
  assert.deepEqual(parseSlashCommand("/panel@LifeManagerBot AB2cdef3"), { name: "panel", args: "AB2cdef3" });
  assert.deepEqual(parseSlashCommand("/frobnicate now please"), { name: "frobnicate", args: "now please" });
  assert.equal(parseSlashCommand("hello"), null);
  assert.equal(parseSlashCommand("feedback: /where broke"), null);
  assert.equal(parseSlashCommand(""), null);
  assert.equal(parseSlashCommand(undefined), null);
  assert.equal(parseSlashCommand("/ nope"), null);
});

// REGRESSION (intentional behaviour change, W2): before the slash router existed, ANY text starting
// with "/start" reached the onboarding branch (telegram.js isStart = startsWith("/start")), so
// "/startfoo" opened onboarding. Now only an EXACT /start command (optionally @BotName, optionally
// followed by a deep-link payload) is a start; "/startfoo" is a distinct unknown command.
// Evidence this is safe: core.telegram.org/bots/features → Deep Linking. Private chats:
// "https://t.me/your_bot?start=airplane" → "When someone opens a chat with your bot via this link,
// you will receive: /start airplane". Groups: "?startgroup=spaceship" → "/start@your_bot spaceship".
// The payload is ALWAYS space-separated (and limited to A-Z a-z 0-9 _ -), so no legitimate Telegram
// deep link can ever deliver "/startfoo".
test("only an exact /start (plus optional deep-link payload) is a start; /startfoo is a distinct command", async () => {
  assert.deepEqual(parseSlashCommand("/start"), { name: "start", args: "" });
  assert.deepEqual(parseSlashCommand("/start airplane"), { name: "start", args: "airplane" });
  assert.deepEqual(parseSlashCommand("/start@LifeManagerBot spaceship"), { name: "start", args: "spaceship" });
  assert.deepEqual(parseSlashCommand("/startfoo"), { name: "startfoo", args: "" });
  assert.deepEqual(parseSlashCommand("/startgroup"), { name: "startgroup", args: "" });

  // The documented deep-link spellings still pass through to the onboarding branch...
  for (const raw of ["/start", "/start airplane", "/start@LifeManagerBot spaceship"]) {
    const { sent, deps } = harness();
    assert.deepEqual(await handleSlashCommand(parseSlashCommand(raw), ROW, deps), { handled: false }, raw);
    assert.equal(sent.length, 0, raw);
  }
  // ...while a /start-prefixed non-command is answered honestly instead of silently onboarding.
  const { sent, deps } = harness();
  const outcome = await handleSlashCommand(parseSlashCommand("/startfoo"), ROW, deps);
  assert.equal(outcome.handled, true);
  assert.equal(outcome.action, "unknown");
  assert.ok(sent[0].text.includes("/startfoo"));
});

test("slashAliasText maps /connect to the same NL control flow and nothing else", () => {
  assert.equal(slashAliasText(parseSlashCommand("/connect")), "connect calendar");
  assert.equal(slashAliasText(parseSlashCommand("/connect@LifeManagerBot")), "connect calendar");
  assert.equal(slashAliasText(parseSlashCommand("/help")), null);
  assert.equal(slashAliasText(parseSlashCommand("/frobnicate")), null);
  assert.equal(slashAliasText(null), null);
});

test("/connect only aliases calendar-ish arguments; anything else declines the alias", () => {
  for (const raw of ["/connect", "/connect calendar", "/connect my calendar", "/connect google calendar",
    "/connect GCal", "/connect@LifeManagerBot calendar", "/connect カレンダー"]) {
    assert.equal(slashAliasText(parseSlashCommand(raw)), "connect calendar", raw);
  }
  for (const raw of ["/connect gmail", "/connect email", "/connect slack", "/connect calendar and gmail"]) {
    assert.equal(slashAliasText(parseSlashCommand(raw)), null, raw);
  }
});

test("/connect with a non-calendar argument says what is connectable instead of silently connecting calendar", async () => {
  const { sent, deps } = harness();
  const outcome = await handleSlashCommand(parseSlashCommand("/connect gmail"), ROW, deps);
  assert.equal(outcome.handled, true);
  assert.equal(outcome.action, "connect");
  assert.equal(outcome.ok, false);
  assert.equal(outcome.reason, "unsupported_provider");
  assert.equal(sent.length, 1);
  assert.ok(/only .*Google Calendar/i.test(sent[0].text), "names the one provider that is connectable");
  assert.ok(/gmail/i.test(sent[0].text), "says Gmail is off rather than pretending it was connected");
  assert.ok(sent[0].text.includes("/connect"), "tells the user the working spelling");
  // The argument is never echoed back: sendMessage posts parse_mode HTML, and echoing arbitrary user
  // text there is both an injection surface and a 400 risk.
  assert.ok(!/slack/i.test(helpMessage()), "sanity");

  // The unlinked chat gets the same honest answer — this reply reads no store at all.
  const unlinked = harness();
  const anon = await handleSlashCommand(parseSlashCommand("/connect slack"), null, unlinked.deps);
  assert.equal(anon.reason, "unsupported_provider");
  assert.ok(!/slack/i.test(unlinked.sent[0].text), "the raw argument is not echoed into an HTML message");
});

test("helpMessage lists every legacy-parity command and the NL actions from kind:help", () => {
  const text = helpMessage();
  for (const name of ["/start", "/panel", "/help", "/status", "/where", "/stop", "/subscribe", "/connect", "/payout", "/reset"]) {
    assert.ok(text.includes(name), `help must list ${name}`);
  }
  // kind:"help" wiring: the availableActions parseUserCommand computes are no longer dropped.
  assert.ok(text.includes("connect calendar"), "help must include the NL availableActions");
});

test("/start and /panel pass through untouched — their existing branches stay the owner", async () => {
  for (const raw of ["/start", "/panel", "/panel AB2cdef3", "/start panel"]) {
    const { sent, deps } = harness();
    const outcome = await handleSlashCommand(parseSlashCommand(raw), ROW, deps);
    assert.deepEqual(outcome, { handled: false }, raw);
    assert.equal(sent.length, 0, raw);
  }
});

test("unknown /command gets an honest unknown reply pointing at /help", async () => {
  const { sent, deps } = harness();
  const outcome = await handleSlashCommand(parseSlashCommand("/frobnicate now"), ROW, deps);
  assert.equal(outcome.handled, true);
  assert.equal(outcome.action, "unknown");
  assert.equal(sent.length, 1);
  assert.ok(sent[0].text.includes("/frobnicate"), "names the command it did not understand");
  assert.ok(sent[0].text.includes("/help"), "points at /help");
});

test("/help replies with the command list", async () => {
  const { sent, deps } = harness();
  const outcome = await handleSlashCommand(parseSlashCommand("/help"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "help" });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].text, helpMessage());
});

test("account-scoped commands on an unlinked chat get the setup-first reply and touch no store", async () => {
  for (const raw of ["/status", "/where", "/stop", "/payout", "/reset"]) {
    let stores = 0;
    const { sent, deps } = harness({
      getLiveLocation: async () => { stores++; return null; },
      deleteLiveLocation: async () => { stores++; return { deleted: 0 }; },
      setStage: async () => { stores++; },
      askPayoutQuestion: async () => { stores++; return { asked: true }; },
    });
    const outcome = await handleSlashCommand(parseSlashCommand(raw), null, deps);
    assert.equal(outcome.handled, true, raw);
    assert.equal(outcome.ok, false, raw);
    assert.equal(outcome.reason, "unlinked", raw);
    assert.equal(stores, 0, `${raw} must not touch the store for an unlinked chat`);
    assert.equal(sent.length, 1, raw);
    assert.ok(sent[0].text.includes("/start"), `${raw} setup-first reply names /start`);
  }
});

test("/where formats a stored fix privacy-safe (rounded coords, age, expiry — no fabricated accuracy)", async () => {
  const { sent, deps } = harness({
    getLiveLocation: async (uid) => {
      assert.equal(uid, "u1");
      return {
        uid: "u1", latitude: 35.681236, longitude: 139.767125,
        observed_at: new Date(NOW - 42_000).toISOString(),
        expires_at: new Date(NOW + 14 * 60_000).toISOString(),
      };
    },
  });
  const outcome = await handleSlashCommand(parseSlashCommand("/where"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "where", ok: true });
  assert.equal(sent.length, 1);
  const text = sent[0].text;
  assert.ok(text.includes("35.68"), "rounded latitude");
  assert.ok(text.includes("139.77"), "rounded longitude");
  assert.ok(!text.includes("35.681236"), "full-precision latitude never echoed");
  assert.ok(!text.includes("139.767125"), "full-precision longitude never echoed");
  assert.ok(text.includes("42s"), "age in seconds");
  assert.ok(text.includes("14"), "expiry minutes");
  assert.ok(!/accuracy/i.test(text), "accuracy is not stored, so it is never fabricated");
});

test("/where with no stored fix is an honest none reply", async () => {
  const { sent, deps } = harness({ getLiveLocation: async () => null });
  const outcome = await handleSlashCommand(parseSlashCommand("/where"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "where", ok: true, stored: false });
  assert.equal(sent.length, 1);
  assert.ok(/don't have|no fresh/i.test(sent[0].text));
  assert.ok(/Live Location/i.test(sent[0].text), "tells the user how to share");
});

test("/stop deletes the tenant's stored location and writes an audit log line", async () => {
  const deleted = [];
  const { sent, logs, deps } = harness({
    deleteLiveLocation: async (uid) => { deleted.push(uid); return { deleted: 1 }; },
  });
  const outcome = await handleSlashCommand(parseSlashCommand("/stop"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "stop", ok: true, deleted: 1 });
  assert.deepEqual(deleted, ["u1"], "delete is scoped to THIS row's uid");
  assert.equal(sent.length, 1);
  assert.ok(/deleted/i.test(sent[0].text));
  assert.equal(logs.length, 1, "audit log entry");
  assert.ok(logs[0].includes("[location]"), "audit names the surface");
  assert.ok(logs[0].includes("deleted=1"), "audit records the outcome");
  assert.ok(!logs[0].includes("u1-secret"), "sanity");
});

test("/stop with nothing stored and with a failed delete stay honest", async () => {
  const none = harness({ deleteLiveLocation: async () => ({ deleted: 0 }) });
  const zero = await handleSlashCommand(parseSlashCommand("/stop"), ROW, none.deps);
  assert.deepEqual(zero, { handled: true, action: "stop", ok: true, deleted: 0 });
  assert.ok(/no stored|nothing/i.test(none.sent[0].text), "zero rows is said, not dressed as a delete");

  const broken = harness({ deleteLiveLocation: async () => null });
  const fail = await handleSlashCommand(parseSlashCommand("/stop"), ROW, broken.deps);
  assert.deepEqual(fail, { handled: true, action: "stop", ok: false, reason: "delete_failed" });
  assert.ok(/couldn't|could not/i.test(broken.sent[0].text), "a failed delete is a visible failure");
  assert.ok(broken.logs[0].includes("deleted=error"), "audit records the failure");
});

test("/subscribe returns the tenant-scoped Stripe link only after explicit request", async () => {
  const { sent, deps } = harness({ stripePaymentLink: "https://buy.stripe.com/test_life_manager" });
  const outcome = await handleSlashCommand(parseSlashCommand("/subscribe"), { ...ROW, paid: false }, deps);
  assert.deepEqual(outcome, { handled: true, action: "subscribe", ok: true });
  assert.equal(sent.length, 1);
  const keyboard = sent[0].extra.reply_markup.inline_keyboard;
  assert.equal(keyboard[0][0].url, "https://buy.stripe.com/test_life_manager?client_reference_id=u1");
  assert.match(sent[0].text, /月額\$29/);
});

test("/subscribe on an already-paid row says so; unlinked chats never get checkout", async () => {
  const paid = harness();
  const active = await handleSlashCommand(parseSlashCommand("/subscribe"), ROW, paid.deps);
  assert.deepEqual(active, { handled: true, action: "subscribe", ok: true, alreadyActive: true });
  assert.ok(/already active/i.test(paid.sent[0].text));
  assert.equal(paid.sent[0].extra, undefined, "no checkout button for an active subscription");

  const fresh = harness({ stripePaymentLink: "https://buy.stripe.com/test_life_manager" });
  const linked = await handleSlashCommand(parseSlashCommand("/subscribe"), null, fresh.deps);
  assert.deepEqual(linked, { handled: true, action: "subscribe", ok: false, reason: "unlinked" });
  assert.equal(fresh.sent[0].extra, undefined);
});

test("/subscribe without a chat id is an honest unavailable reply, not an invented URL", async () => {
  const { sent, deps } = harness({ chatId: "" });
  const outcome = await handleSlashCommand(parseSlashCommand("/subscribe"), null, deps);
  assert.deepEqual(outcome, { handled: true, action: "subscribe", ok: false, reason: "unlinked" });
  assert.equal(sent[0].extra, undefined);
});

test("/reset reuses setStage to restart onboarding announcements and confirms", async () => {
  const staged = [];
  const { sent, deps } = harness({
    setStage: async (uid, stage, supaUrl, supaKey) => { staged.push({ uid, stage, supaUrl, supaKey }); },
  });
  const outcome = await handleSlashCommand(parseSlashCommand("/reset"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "reset", ok: true });
  assert.deepEqual(staged, [{ uid: "u1", stage: "calendar", supaUrl: "https://db.test", supaKey: "k" }]);
  assert.equal(sent.length, 1);
  assert.ok(sent[0].text.includes("/start"), "confirm message tells the user how to continue");
  // W1: rewinding tg_onboard_stage ALSO closes the browser-task gate
  // (lib/browser-task-intake.js requires tg_onboard_stage === "done"). That side effect is disclosed
  // in the confirmation instead of being silent.
  assert.ok(/browser task/i.test(sent[0].text), "/reset discloses that browser tasks pause");
  assert.ok(/pause/i.test(sent[0].text), "/reset says the browser-task intake is paused, not broken");
});

test("/payout reopens the picker through askPayoutQuestion (its read makes the reopen idempotent)", async () => {
  const asks = [];
  const { sent, deps } = harness({
    askPayoutQuestion: async (args) => { asks.push(args); return { asked: true }; },
  });
  const outcome = await handleSlashCommand(parseSlashCommand("/payout"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "payout", ok: true, asked: true });
  assert.equal(asks.length, 1);
  assert.equal(asks[0].uid, "u1");
  assert.equal(asks[0].chatId, "100");
  assert.equal(sent.length, 0, "the question itself is sent by askPayoutQuestion, never duplicated here");
});

test("/payout surfaces already-answered silently (askPayoutQuestion replied) and failures honestly", async () => {
  const answered = harness({ askPayoutQuestion: async () => ({ asked: false, reason: "already_answered" }) });
  const already = await handleSlashCommand(parseSlashCommand("/payout"), ROW, answered.deps);
  assert.deepEqual(already, { handled: true, action: "payout", ok: true, asked: false, reason: "already_answered" });
  assert.equal(answered.sent.length, 0, "askPayoutQuestion already sent the already-registered reply");

  const broken = harness({ askPayoutQuestion: async () => ({ asked: false, reason: "lookup_failed" }) });
  const fail = await handleSlashCommand(parseSlashCommand("/payout"), ROW, broken.deps);
  assert.deepEqual(fail, { handled: true, action: "payout", ok: false, asked: false, reason: "lookup_failed" });
  assert.equal(broken.sent.length, 1);
  assert.ok(/couldn't|could not/i.test(broken.sent[0].text), "a failed lookup is a visible failure");
});

test("/status projects only real local data: stage, connections, payout, location — no fabricated fields", async () => {
  const { sent, deps } = harness({
    getLiveLocation: async () => ({
      uid: "u1", latitude: 35.68, longitude: 139.76,
      observed_at: new Date(NOW - 9_000).toISOString(),
      expires_at: new Date(NOW + 60_000).toISOString(),
    }),
  });
  const outcome = await handleSlashCommand(parseSlashCommand("/status"), ROW, deps);
  assert.deepEqual(outcome, { handled: true, action: "status", ok: true });
  const text = sent[0].text;
  assert.ok(/Onboarding: done/.test(text));
  assert.ok(/Calendar: connected/.test(text));
  assert.ok(/Phone: on file/.test(text));
  assert.ok(/Monthly allowance: Plus active/.test(text));
  assert.ok(/Payout: not set/.test(text));
  assert.ok(/observed 9s ago/.test(text));
  assert.ok(/no missed call recorded/.test(text), "a healthy loop says so rather than staying silent");
  assert.ok(!/cron|daemon/i.test(text), "nothing the webhook cannot read is claimed");
});

// spec 2026-08-01-lm-daily-organ-design.md §3 row 1b + §5.5: /status must show 直近の失敗. This
// REVERSES the older "call health is unreachable from here" note above statusMessage — lm_wake_miss
// is a plain Supabase read, exactly like the live-location read this handler already performs.
test("/status names the last missed call, its clock time, and why it did not ring", async () => {
  const { sent, deps } = harness({
    getLastWakeMiss: async () => ({
      reason: "dial_failed", detail: "telnyx balance too low",
      due_at: "2026-07-30T08:15:00+09:00", occurred_at: "2026-07-30T08:15:04+09:00",
    }),
  });
  await handleSlashCommand(parseSlashCommand("/status"), { ...ROW, call_time_zone: "Asia/Tokyo" }, deps);
  const text = sent[0].text;
  assert.ok(/08:15/.test(text), "the user reads the clock time they were owed a call at");
  assert.ok(/could not be dialled/i.test(text));
  assert.ok(/telnyx balance too low/.test(text), "the reason is shown, not hidden behind a log");
});

test("/status reports a missed call in UTC rather than guessing a zone the row does not carry", async () => {
  const { sent, deps } = harness({
    getLastWakeMiss: async () => ({
      reason: "no_call_before_departure", due_at: "2026-07-30T08:15:00+09:00",
    }),
  });
  await handleSlashCommand(parseSlashCommand("/status"), ROW, deps);
  assert.ok(/23:15 UTC/.test(sent[0].text));
  assert.ok(/never rang/i.test(sent[0].text));
});

test("/status says 'no missed call' rather than inventing health when the ledger is unreachable", async () => {
  const { sent, deps } = harness({ getLastWakeMiss: async () => null });
  await handleSlashCommand(parseSlashCommand("/status"), ROW, deps);
  assert.ok(/no missed call recorded/.test(sent[0].text));
});

test("/status stays honest for the sparse row and the missing location", async () => {
  const row = {
    uid: "u2", telegram_chat_id: "200", tg_onboard_stage: "phone",
    calendar_provider: null, phone: null, paid: false,
    payout_destination: { type: "wallet", status: "awaiting_address" },
  };
  const { sent, deps } = harness({ getLiveLocation: async () => null });
  await handleSlashCommand(parseSlashCommand("/status"), row, deps);
  const text = sent[0].text;
  assert.ok(/Onboarding: at the "calendar" step/.test(text), "stage is computed from the row, not the stale column");
  assert.ok(/Calendar: not connected/.test(text));
  assert.ok(/Phone: not set/.test(text));
  assert.ok(/Monthly allowance: free monthly allowance/.test(text));
  assert.ok(/Payout: awaiting typed address/.test(text));
  assert.ok(/Location: not available/.test(text));
});

// S2: computeStage lets an unpaid row through the "pay" stage while LM_COMP_UNTIL is in the future
// (lib/comp-window.js, read-time only). /status used to report "Subscription: not active" for exactly
// those users while every gate treated them as entitled — a contradiction. The projection now names
// the comp window using the only real field there is: the configured expiry.
test("/status shows the monthly free allowance without reviving the old paywall", async () => {
  const comped = { ...ROW, paid: false };
  const active = harness({ env: { LM_COMP_UNTIL: "2026-08-01T00:00:00Z" }, getLiveLocation: async () => null });
  await handleSlashCommand(parseSlashCommand("/status"), comped, active.deps);
  const text = active.sent[0].text;
  assert.ok(/Monthly allowance: free monthly allowance/.test(text), text);
  assert.ok(!/not active|complimentary/.test(text));
  // The same window is what carried this row past the paywall, so the stage must agree with it.
  assert.ok(/Onboarding: done/.test(text), "the stage projection uses the same clock and env");

  const expired = harness({ env: { LM_COMP_UNTIL: "2026-07-01T00:00:00Z" }, getLiveLocation: async () => null });
  await handleSlashCommand(parseSlashCommand("/status"), comped, expired.deps);
  assert.ok(/Monthly allowance: free monthly allowance/.test(expired.sent[0].text));
  assert.ok(/Onboarding: done/.test(expired.sent[0].text), "the old paywall never returns");

  const paid = harness({ env: { LM_COMP_UNTIL: "2026-08-01T00:00:00Z" }, getLiveLocation: async () => null });
  await handleSlashCommand(parseSlashCommand("/status"), ROW, paid.deps);
  assert.ok(/Monthly allowance: Plus active$/m.test(paid.sent[0].text));

  const noComp = harness({ env: {}, getLiveLocation: async () => null });
  await handleSlashCommand(parseSlashCommand("/status"), comped, noComp.deps);
  assert.ok(/Monthly allowance: free monthly allowance/.test(noComp.sent[0].text));
});
