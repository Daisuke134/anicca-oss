"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createMinimalEvidenceChain } = require("./connector-minimal-evidence.js");

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

test("evidence requires explicit Telegram senders and has no OpenClaw dependency", () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-evidence-sender-required-"));
  try {
    const source = fs.readFileSync(path.join(__dirname, "connector-minimal-evidence.js"), "utf8");
    assert.doesNotMatch(source, /outbound-guardian|notifyOpenClawGateway|notifyOpenClawPhoto|parseOpenClawMessageId/);
    assert.throws(() => createMinimalEvidenceChain({
      stateDir, tenantId: "dais-local", calendarId: "primary", telegramTarget: "private-target",
      calendar: { async findConnectorEvents() { return []; }, async createConnectorEvent() { return {}; } },
    }), /invalid/i);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("evidence rejects state and delivery-claim symlinks or wrong permissions before Telegram", async () => {
  const stateTarget = fs.mkdtempSync(path.join(os.tmpdir(), "connector-evidence-state-target-"));
  const stateLink = path.join(os.tmpdir(), `connector-evidence-state-link-${process.pid}-${Date.now()}`);
  try {
    fs.symlinkSync(stateTarget, stateLink, "dir");
    assert.throws(() => createMinimalEvidenceChain({
      stateDir: stateLink, tenantId: "dais-local", calendarId: "primary", telegramTarget: "private-target",
      calendar: { async findConnectorEvents() { return []; }, async createConnectorEvent() { return {}; } },
      sendMessage: async () => ({ messageId: 1 }), sendPhoto: async () => ({ messageId: 2 }),
    }), /invalid/i);
  } finally {
    fs.unlinkSync(stateLink);
    fs.rmSync(stateTarget, { recursive: true, force: true });
  }

  const wrongState = fs.mkdtempSync(path.join(os.tmpdir(), "connector-evidence-state-permission-"));
  try {
    fs.chmodSync(wrongState, 0o755);
    assert.throws(() => createMinimalEvidenceChain({
      stateDir: wrongState, tenantId: "dais-local", calendarId: "primary", telegramTarget: "private-target",
      calendar: { async findConnectorEvents() { return []; }, async createConnectorEvent() { return {}; } },
      sendMessage: async () => ({ messageId: 1 }), sendPhoto: async () => ({ messageId: 2 }),
    }), /invalid/i);
  } finally {
    fs.chmodSync(wrongState, 0o700);
    fs.rmSync(wrongState, { recursive: true, force: true });
  }

  for (const kind of ["symlink", "permissions"]) {
    const fixture = peatixFixture();
    const callsBefore = fixture.calls.length;
    const claimDir = path.join(fixture.stateDir, "evidence", "delivery-claims");
    let target;
    try {
      fs.mkdirSync(path.dirname(claimDir), { recursive: true, mode: 0o700 });
      if (kind === "symlink") {
        target = fs.mkdtempSync(path.join(os.tmpdir(), "connector-evidence-claims-target-"));
        fs.symlinkSync(target, claimDir, "dir");
      } else {
        fs.mkdirSync(claimDir, { mode: 0o755 });
      }
      await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assert.equal(fixture.calls.slice(callsBefore).filter(([name]) => name.startsWith("telegram-")).length, 0, kind);
    } finally {
      const stat = fs.lstatSync(claimDir);
      if (stat.isSymbolicLink()) fs.unlinkSync(claimDir);
      else { fs.chmodSync(claimDir, 0o700); fs.rmSync(claimDir, { recursive: true, force: true }); }
      if (target) fs.rmSync(target, { recursive: true, force: true });
      fixture.cleanup();
    }
  }
});

test("evidence fails closed when the claim directory is swapped after fd inspection", async () => {
  const fixture = peatixFixture();
  const claimDir = path.join(fixture.stateDir, "evidence", "delivery-claims");
  const backupDir = `${claimDir}.real`;
  const outsideDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-evidence-claim-swap-"));
  const originalOpen = fs.openSync;
  const originalFstat = fs.fstatSync;
  let claimDirFd;
  let swapped = false;
  fs.openSync = (file, flags, ...rest) => {
    const descriptor = originalOpen(file, flags, ...rest);
    if (String(file) === claimDir && typeof flags === "number" && (flags & fs.constants.O_CREAT) === 0) claimDirFd = descriptor;
    return descriptor;
  };
  fs.fstatSync = (descriptor, ...rest) => {
    const stat = originalFstat(descriptor, ...rest);
    if (descriptor === claimDirFd && !swapped) {
      fs.renameSync(claimDir, backupDir);
      fs.symlinkSync(outsideDir, claimDir, "dir");
      swapped = true;
    }
    return stat;
  };
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.equal(swapped, true);
    assert.equal(fixture.calls.filter(([name]) => name.startsWith("telegram-")).length, 0);
  } finally {
    fs.openSync = originalOpen;
    fs.fstatSync = originalFstat;
    try {
      const stat = fs.lstatSync(claimDir);
      if (stat.isSymbolicLink()) fs.unlinkSync(claimDir);
      else fs.rmSync(claimDir, { recursive: true, force: true });
    } catch (error) {
      if (!error || error.code !== "ENOENT") throw error;
    }
    if (fs.existsSync(backupDir)) fs.renameSync(backupDir, claimDir);
    fs.rmSync(outsideDir, { recursive: true, force: true });
    fixture.cleanup();
  }
});

test("verified registration becomes one durable Calendar PNG Telegram applied bundle", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-evidence-"));
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 7)]);
  const pngSha = createHash("sha256").update(png).digest("hex");
  const calls = [];
  const calendarReceipt = Object.freeze({
    id: "google-event-1",
    htmlLink: "https://www.google.com/calendar/event?eid=verified-one",
  });
  let calendarReads = 0;
  const calendar = {
    async findConnectorEvents(input) {
      calls.push(["calendar-read", input]);
      calendarReads += 1;
      return calendarReads === 1 ? [] : [calendarReceipt];
    },
    async createConnectorEvent(input) {
      calls.push(["calendar-create", input]);
      return calendarReceipt;
    },
  };
  const evidenceStore = {
    async record(input) {
      calls.push(["evidence-record", input]);
      assert.equal(input.screenshot, png);
      const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex");
      return Object.freeze({
        external_receipt_ref: `provider-receipt://luma/${receiptId}`,
        artifact_ref: `object://sha256/${pngSha}`,
      });
    },
  };
  const chain = createMinimalEvidenceChain({
    stateDir,
    tenantId: "dais-local",
    calendar,
    calendarId: "primary",
    telegramTarget: "private-target",
    evidenceStore,
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async sendMessage(message, options) {
      calls.push(["telegram-message", message, options]);
      return { ok: true, result: { message_id: 9101, chat: { id: 1 }, date: 1_754_000_000, text: "fixture" } };
    },
    async sendPhoto(bytes, options) {
      calls.push(["telegram-photo", bytes, options]);
      return { ok: true, result: { message_id: 9102, chat: { id: 1 }, date: 1_754_000_000, caption: "fixture" } };
    },
  });
  const candidate = Object.freeze({
    provider: "luma",
    event_ref: "luma-event://event/verified-one",
    canonical_url: "https://luma.com/verified-one",
    title: "Verified Technology Event",
    starts_at: "2026-08-10T10:00:00.000Z",
    ends_at: "2026-08-10T11:00:00.000Z",
    venue_name: "Tokyo",
  });
  const page = lumaRenderPage(calls, png);

  const bundle = await chain.completeEvidence({
    provider: "luma",
    candidate,
    page,
    providerState: { status: "registered" },
    repairedActions: [],
  });

  assert.equal(bundle.status, "applied_bundle");
  assert.match(bundle.bundle_id, /^applied-bundle:[0-9a-f]{64}$/);
  assert.equal(calls.filter(([name]) => name === "set-content").length, 0);
  assert.deepEqual(calls.find(([name]) => name === "goto"), ["goto", "about:blank", { waitUntil: "domcontentloaded", timeout: 30_000 }]);
  assert.ok(calls.find(([name]) => name === "document-write"));
  assert.equal(calls.filter(([name]) => name === "screenshot").length, 1);
  assert.deepEqual(calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
  assert.equal(calls.filter(([name]) => name === "calendar-create").length, 1);
  assert.equal(calls.filter(([name]) => name === "calendar-read").length, 2);
  assert.equal(calls.filter(([name]) => name === "telegram-message").length, 1);
  assert.equal(calls.filter(([name]) => name === "telegram-photo").length, 1);
  const deliveredMessage = calls.find(([name]) => name === "telegram-message")[1];
  assert.equal(
    deliveredMessage,
    [
      "Connector::: イベント申込を確認しました",
      "event: Verified Technology Event",
      "starts at: 2026/8/10(月) 19:00 Asia/Tokyo",
      "venue: Tokyo",
      "provider: luma",
      "status: registered",
      "event url: https://luma.com/verified-one",
      "calendar: https://www.google.com/calendar/event?eid=verified-one",
    ].join("\n"),
  );
  assert.equal(deliveredMessage.includes("2026-08-10T10:00:00.000Z"), false);
  assert.equal(deliveredMessage.includes("https://luma.com/verified-one"), true);
  assert.equal(deliveredMessage.includes("https://www.google.com/calendar/event?eid=verified-one"), true);
  assert.equal(
    calls.find(([name]) => name === "telegram-message")[2].idempotencyKey,
    `connector-evidence:${calls.find(([name]) => name === "calendar-create")[1].idempotencyValue}`,
  );
  const messageIdempotencyKey = calls.find(([name]) => name === "telegram-message")[2].idempotencyKey;
  const photoOptions = calls.find(([name]) => name === "telegram-photo")[2];
  const photoIdempotencyKey = photoOptions.idempotencyKey;
  const canonicalUrlSha256 = calls.find(([name]) => name === "calendar-create")[1].idempotencyValue;
  assert.equal(photoIdempotencyKey, `connector-evidence-photo:${canonicalUrlSha256}`);
  assert.match(photoIdempotencyKey, /^connector-evidence-photo:[0-9a-f]{64}$/);
  assert.notEqual(messageIdempotencyKey, photoIdempotencyKey);
  assert.doesNotMatch(photoIdempotencyKey, /https?:\/\/|private-target|dais-local|Verified Technology Event/);
  assert.equal(photoOptions.caption, "Connector::: Verified Technology Event / registered");

  const bundleFile = path.join(stateDir, "applied-bundles", `${bundle.bundle_id.slice("applied-bundle:".length)}.json`);
  const persisted = JSON.parse(fs.readFileSync(bundleFile, "utf8"));
  assert.equal(persisted.bundle_id, bundle.bundle_id);
  assert.equal(persisted.provider_status, "registered");
  assert.equal(persisted.artifact_sha256, pngSha);
  assert.equal(persisted.calendar_event_id, "google-event-1");
  assert.equal(persisted.telegram_message_provider_id, "9101");
  assert.equal(persisted.telegram_photo_provider_id, "9102");
  assert.equal(JSON.stringify(persisted).includes("private-target"), false);
  assert.equal(fs.statSync(bundleFile).mode & 0o777, 0o600);
});

test("hasAppliedBundle reports false before completion and true after, without touching Calendar or Telegram", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-evidence-has-bundle-"));
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 7)]);
  const pngSha = createHash("sha256").update(png).digest("hex");
  const calls = [];
  const calendarReceipt = Object.freeze({
    id: "google-event-2",
    htmlLink: "https://www.google.com/calendar/event?eid=has-bundle-one",
  });
  let calendarReads = 0;
  const calendar = {
    async findConnectorEvents(input) {
      calls.push(["calendar-read", input]);
      calendarReads += 1;
      return calendarReads === 1 ? [] : [calendarReceipt];
    },
    async createConnectorEvent(input) {
      calls.push(["calendar-create", input]);
      return calendarReceipt;
    },
  };
  const evidenceStore = {
    async record(input) {
      const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex");
      return Object.freeze({
        external_receipt_ref: `provider-receipt://luma/${receiptId}`,
        artifact_ref: `object://sha256/${pngSha}`,
      });
    },
  };
  const chain = createMinimalEvidenceChain({
    stateDir,
    tenantId: "dais-local",
    calendar,
    calendarId: "primary",
    telegramTarget: "private-target",
    evidenceStore,
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    async sendMessage(message, options) { calls.push(["telegram-message", message, options]); return { messageId: 9201 }; },
    async sendPhoto(bytes, options) { calls.push(["telegram-photo", bytes, options]); return { messageId: 9202 }; },
  });
  const candidate = Object.freeze({
    provider: "luma",
    event_ref: "luma-event://event/has-bundle-one",
    canonical_url: "https://luma.com/has-bundle-one",
    title: "Has Bundle Event",
    starts_at: "2026-08-10T10:00:00.000Z",
    ends_at: "2026-08-10T11:00:00.000Z",
    venue_name: "Tokyo",
  });
  const page = lumaRenderPage(calls, png);

  assert.equal(
    await chain.hasAppliedBundle({ provider: "luma", event_ref: candidate.event_ref, provider_status: "registered" }),
    false,
  );

  const before = calls.length;
  await chain.completeEvidence({
    provider: "luma", candidate, page, providerState: { status: "registered" }, repairedActions: [],
  });
  assert.equal(calls.length > before, true);

  assert.equal(
    await chain.hasAppliedBundle({ provider: "luma", event_ref: candidate.event_ref, provider_status: "registered" }),
    true,
  );
  const afterReads = calls.length;
  assert.equal(
    await chain.hasAppliedBundle({ provider: "luma", event_ref: candidate.event_ref, provider_status: "registered" }),
    true,
  );
  assert.equal(calls.length, afterReads, "hasAppliedBundle must never call Calendar or Telegram");
  assert.equal(calls.filter(([name]) => name === "calendar-create").length, 1);
  assert.equal(calls.filter(([name]) => name === "telegram-message").length, 1);
  assert.equal(calls.filter(([name]) => name === "telegram-photo").length, 1);

  fs.rmSync(stateDir, { recursive: true, force: true });
});

test("hasAppliedBundle rejects an unknown provider or an out-of-contract provider_status", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-evidence-has-bundle-invalid-"));
  const chain = createMinimalEvidenceChain({
    stateDir,
    tenantId: "dais-local",
    calendar: { async findConnectorEvents() { return []; }, async createConnectorEvent() { return {}; } },
    calendarId: "primary",
    telegramTarget: "private-target",
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    sendMessage: async () => ({ messageId: 1 }),
    sendPhoto: async () => ({ messageId: 2 }),
  });
  await assert.rejects(chain.hasAppliedBundle({ provider: "not-a-real-provider", event_ref: "x", provider_status: "registered" }));
  await assert.rejects(chain.hasAppliedBundle({ provider: "luma", event_ref: "luma-event://event/x", provider_status: "not-a-real-status" }));
  await assert.rejects(chain.hasAppliedBundle({ provider: "luma", event_ref: "not-a-real-ref", provider_status: "registered" }));
  fs.rmSync(stateDir, { recursive: true, force: true });
});

// Same about:blank + document.write technique the Peatix page uses (page.setContent() hangs on the
// live CloakBrowser daily-driver over CDP, verified 2026-08-16). Shared by every Luma test below so
// each one proves the real production path renders through goto/url/evaluate, never setContent.
function lumaRenderPage(calls, png, options = {}) {
  let url = "about:blank";
  return {
    async goto(target, input) { calls.push(["goto", target, input]); if (options.gotoThrows) throw new Error("reset failed"); url = options.gotoReadback || target; },
    url() { calls.push(["url"]); return url; },
    async evaluate(fn, payload) {
      calls.push(["evaluate", payload]);
      if (options.evaluateThrows) throw new Error("receipt write failed");
      const previousDocument = global.document;
      global.document = {
        open() { calls.push(["document-open"]); },
        write(value) { calls.push(["document-write", value]); },
        close() { calls.push(["document-close"]); },
        querySelector() { return options.receiptValid === false ? null : {}; },
        querySelectorAll() { return options.receiptValid === false ? [] : [{}, {}, {}]; },
      };
      try {
        const result = await fn(payload);
        return options.evaluateResult === undefined ? result : options.evaluateResult;
      } finally { global.document = previousDocument; }
    },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
}

test("Luma evidence renders the receipt via about:blank + document.write, never setContent, and refuses when it does not render", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-luma-render-"));
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 11)]);
  const pngSha = createHash("sha256").update(png).digest("hex");
  const calendarReceipt = { id: "google-luma-render", htmlLink: "https://www.google.com/calendar/event?eid=luma-render" };
  const candidate = Object.freeze({
    provider: "luma", event_ref: "luma-event://event/render-check", canonical_url: "https://luma.com/render-check",
    title: "Render Check Event", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z", venue_name: "Tokyo",
  });

  // Happy path: goto(about:blank) -> url() readback -> evaluate(document.open/write/close) -> screenshot.
  {
    const calls = [];
    let reads = 0;
    const chain = createMinimalEvidenceChain({
      stateDir, tenantId: "dais-local", calendarId: "primary", telegramTarget: "private-target",
      calendar: {
        async findConnectorEvents() { return reads++ === 0 ? [] : [calendarReceipt]; },
        async createConnectorEvent() { return calendarReceipt; },
      },
      evidenceStore: { async record(input) {
        const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex");
        return { external_receipt_ref: `provider-receipt://luma/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` };
      } },
      now: () => new Date("2026-08-07T08:30:00.000Z"),
      sendMessage: async () => ({ messageId: 9301 }),
      sendPhoto: async () => ({ messageId: 9302 }),
    });
    const page = lumaRenderPage(calls, png);
    const bundle = await chain.completeEvidence({ provider: "luma", candidate, page, providerState: { status: "registered" } });
    assert.equal(bundle.status, "applied_bundle");
    assert.equal(calls.filter(([name]) => name === "set-content").length, 0);
    assert.deepEqual(calls.find(([name]) => name === "goto"), ["goto", "about:blank", { waitUntil: "domcontentloaded", timeout: 30_000 }]);
    const write = calls.find(([name]) => name === "document-write");
    assert.ok(write);
    assert.match(write[1], /<dt>provider<\/dt><dd>luma<\/dd>/i);
    assert.match(write[1], /<dt>status<\/dt><dd>registered<\/dd>/i);
    assert.deepEqual(calls.filter(([name]) => ["document-open", "document-close"].includes(name)).map(([name]) => name), ["document-open", "document-close"]);
    assert.ok(calls.findIndex(([name]) => name === "document-close") < calls.findIndex(([name]) => name === "screenshot"));
  }

  // Refuses when the receipt does not render (structural dt/dd check fails) — no downstream effects.
  {
    fs.rmSync(stateDir, { recursive: true, force: true });
    fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
    const calls = [];
    const chain = createMinimalEvidenceChain({
      stateDir, tenantId: "dais-local", calendarId: "primary", telegramTarget: "private-target",
      calendar: { async findConnectorEvents() { calls.push(["calendar-read"]); return []; }, async createConnectorEvent() { calls.push(["calendar-create"]); return calendarReceipt; } },
      evidenceStore: { async record(input) { calls.push(["evidence-record", input]); return { external_receipt_ref: "provider-receipt://luma/deadbeef", artifact_ref: "object://sha256/deadbeef" }; } },
      now: () => new Date("2026-08-07T08:30:00.000Z"),
      sendMessage: async () => { calls.push(["telegram-message"]); return { messageId: 1 }; },
      sendPhoto: async () => { calls.push(["telegram-photo"]); return { messageId: 2 }; },
    });
    const page = lumaRenderPage(calls, png, { receiptValid: false });
    await assert.rejects(chain.completeEvidence({ provider: "luma", candidate, page, providerState: { status: "registered" } }));
    for (const name of ["evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
      assert.equal(calls.filter(([callName]) => callName === name).length, 0, name);
    }
  }

  fs.rmSync(stateDir, { recursive: true, force: true });
});

function peatixCandidate(extra = {}) {
  return {
    provider: "peatix", event_ref: "peatix-event://event/5075819", canonical_url: "https://peatix.com/event/5075819",
    title: "Peatix Public Event", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z",
    venue_name: "Tokyo", ticket_id: "6536845", ...extra,
  };
}
function peatixFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-peatix-evidence-"));
  const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 7)]); const pngSha = createHash("sha256").update(png).digest("hex");
  const calls = []; const receipt = { id: "google-peatix-1", htmlLink: "https://www.google.com/calendar/event?eid=peatix-one" }; let reads = 0;
  const calendar = options.calendar || {
    async findConnectorEvents(input) { calls.push(["calendar-read", input]); return reads++ === 0 ? [] : [receipt]; },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return receipt; },
  };
  const evidenceStore = options.evidenceStore || { async record(input) { calls.push(["evidence-record", input]); const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex"); return { external_receipt_ref: `provider-receipt://peatix/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` }; } };
  const chain = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar, calendarId: "primary", telegramTarget: "private-target", peatixEvidenceStore: evidenceStore, now: () => new Date("2026-08-07T08:30:00.000Z"), sendMessage: options.sendMessage || (async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9101 }; }), sendPhoto: options.sendPhoto || (async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9102 }; }) });
  let pageUrl = options.pageUrl || "https://peatix.com/event/5075819/ticket";
  const page = {
    async setContent(content) { calls.push(["set-content", content]); if (options.setContentNeverSettles) return new Promise(() => {}); if (options.setContentThrows) throw new Error("setContent failed"); },
    async goto(url, input) { calls.push(["goto", url, input]); if (options.gotoThrows) throw new Error("reset failed"); pageUrl = options.gotoReadback || url; },
    url() { calls.push(["url"]); return pageUrl; },
    async evaluate(fn, payload) {
      calls.push(["evaluate", payload]); if (options.evaluateThrows) throw new Error("receipt write failed");
      const previousDocument = global.document;
      global.document = {
        open() { calls.push(["document-open"]); },
        write(value) { calls.push(["document-write", value]); },
        close() { calls.push(["document-close"]); },
        querySelector() { return options.receiptValid === false ? null : {}; },
        querySelectorAll() { return options.receiptValid === false ? [] : [{}, {}, {}]; },
      };
      try { const result = await fn(payload); return options.evaluateResult === undefined ? result : options.evaluateResult; } finally { global.document = previousDocument; }
    },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
  return { chain, stateDir, calls, png, pngSha, candidate: peatixCandidate(), page, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}
function bundleFiles(stateDir) {
  try { return fs.readdirSync(path.join(stateDir, "applied-bundles")); } catch { return []; }
}

function testStableJson(value) { return value && typeof value === "object" ? (Array.isArray(value) ? `[${value.map(testStableJson).join(",")}]` : `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${testStableJson(value[key])}`).join(",")}}`) : JSON.stringify(value); }
function legacyBundleFixture(options = {}) {
  const stateDir = options.stateDir || fs.mkdtempSync(path.join(os.tmpdir(), "connector-existing-bundle-")); const provider = options.provider || "peatix"; const candidate = options.candidate || peatixCandidate(); const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 42)]); const artifactSha = createHash("sha256").update(png).digest("hex"); const observedAt = options.observedAt || "2026-08-07T08:30:00.000Z"; const receiptId = createHash("sha256").update(`dais-local\n${candidate.event_ref}\n${observedAt}\n${artifactSha}`).digest("hex"); const calendar = options.calendar || { id: "google-existing", htmlLink: "https://www.google.com/calendar/event?eid=existing" };
  const core = { schema_version: 1, status: "applied_bundle", provider, event_ref: candidate.event_ref, provider_status: options.providerStatus || "registered", provider_receipt_ref: `provider-receipt://${provider}/${receiptId}`, artifact_ref: `object://sha256/${artifactSha}`, artifact_sha256: artifactSha, calendar_event_id: calendar.id, calendar_event_url: calendar.htmlLink, calendar_readback_at: options.calendarReadbackAt || observedAt, telegram_message_provider_id: options.telegramMessageId || "9101", telegram_photo_provider_id: options.telegramPhotoId || "9102", created_at: observedAt };
  const digest = createHash("sha256").update(testStableJson(core)).digest("hex"); const bundle = { bundle_id: `applied-bundle:${digest}`, ...core }; const bundleDir = path.join(stateDir, "applied-bundles"); fs.mkdirSync(bundleDir, { recursive: true, mode: 0o700 }); const bundleFile = path.join(bundleDir, `${digest}.json`); fs.writeFileSync(bundleFile, `${JSON.stringify(bundle, null, 2)}\n`, { mode: 0o600 }); const calls = [];
  const store = { async record(input) { calls.push(["evidence-record", input]); return { external_receipt_ref: `provider-receipt://${provider}/${receiptId}`, artifact_ref: `object://sha256/${artifactSha}` }; }, async readExternalReceipt(tenant, ref) { calls.push(["evidence-read-receipt", tenant, ref]); return { kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: observedAt, event_ref: candidate.event_ref, artifact_sha256: artifactSha }; }, async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return options.artifact || png; } };
  let calendarReads = 0; const calendarAdapter = { async findConnectorEvents(input) { calls.push(["calendar-read", input]); calendarReads += 1; return typeof options.calendarEvents === "function" ? options.calendarEvents(calendarReads) : [calendar]; }, async createConnectorEvent(input) { calls.push(["calendar-create", input]); return calendar; } }; const page = { async setContent(value) { calls.push(["set-content", value]); }, async goto(url) { calls.push(["goto", url]); }, url() { calls.push(["url"]); return "about:blank"; }, async evaluate() { calls.push(["evaluate"]); return true; }, async screenshot(input) { calls.push(["screenshot", input]); return png; } };
  const chain = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar: calendarAdapter, calendarId: "primary", telegramTarget: "private-target", evidenceStore: provider === "luma" ? store : undefined, peatixEvidenceStore: provider === "peatix" ? store : undefined, now: () => new Date(options.now || "2026-08-07T08:30:00.000Z"), sendMessage: async () => { calls.push(["telegram-message"]); return { messageId: 9201 }; }, sendPhoto: async () => { calls.push(["telegram-photo"]); return { messageId: 9202 }; } });
  return { stateDir, candidate, png, bundle, bundleFile, store, calendar, page, chain, calls, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}
function rewriteBundle(fixture, mutate) { const oldFile = fixture.bundleFile; const value = JSON.parse(fs.readFileSync(oldFile, "utf8")); const { bundle_id, ...core } = value; mutate(core, fixture); const digest = createHash("sha256").update(testStableJson(core)).digest("hex"); const bundle = { bundle_id: `applied-bundle:${digest}`, ...core }; const file = path.join(path.dirname(oldFile), `${digest}.json`); if (file !== oldFile) fs.unlinkSync(oldFile); fs.writeFileSync(file, `${JSON.stringify(bundle, null, 2)}\n`, { mode: 0o600 }); fixture.bundle = bundle; fixture.bundleFile = file; return bundle; }
function writeValidBundle(directory, core) { const digest = createHash("sha256").update(testStableJson(core)).digest("hex"); const bundle = { bundle_id: `applied-bundle:${digest}`, ...core }; fs.writeFileSync(path.join(directory, `${digest}.json`), `${JSON.stringify(bundle, null, 2)}\n`, { mode: 0o600 }); return bundle; }

test("legacy exact applied bundle reuses without checkpoint, render, record, delivery, or schema mutation", async () => { const f = legacyBundleFixture(); const before = fs.readFileSync(f.bundleFile); try { const r = await f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }); assert.equal(r.completion_disposition, "reused"); assert.equal(r.bundle_id, f.bundle.bundle_id); assert.deepEqual(fs.readFileSync(f.bundleFile), before); assert.equal(Object.hasOwn(JSON.parse(before), "completion_disposition"), false); for (const n of ["set-content", "goto", "url", "evaluate", "screenshot", "evidence-record", "telegram-message", "telegram-photo", "calendar-create"]) assert.equal(f.calls.filter(([name]) => name === n).length, 0, n); assert.ok(f.calls.some(([n]) => n === "evidence-read-receipt") && f.calls.some(([n]) => n === "evidence-read-artifact") && f.calls.some(([n]) => n === "calendar-read")); } finally { f.cleanup(); } });
test("created bundle reports created, then exact next invocation reports reused with no duplicate effects", async () => { const f = legacyBundleFixture({ calendarEvents: (read) => (read === 1 ? [] : [f.calendar]) }); fs.unlinkSync(f.bundleFile); try { const first = await f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }); assert.equal(first.completion_disposition, "created"); const file = path.join(f.stateDir, "applied-bundles", bundleFiles(f.stateDir)[0]); const before = fs.readFileSync(file); const counts = new Map(["set-content", "goto", "url", "evaluate", "screenshot", "evidence-record", "telegram-message", "telegram-photo", "calendar-create"].map((n) => [n, f.calls.filter(([name]) => name === n).length])); const second = await f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }); assert.equal(second.completion_disposition, "reused"); assert.equal(second.bundle_id, first.bundle_id); assert.deepEqual(fs.readFileSync(file), before); for (const [n, count] of counts) assert.equal(f.calls.filter(([name]) => name === n).length, count, n); } finally { f.cleanup(); } });

test("existing bundle corruption and symlink matrix fails before downstream effects", async () => { const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 43)]); const cases = [["filename", (f) => { const n = path.join(path.dirname(f.bundleFile), `${"e".repeat(64)}.json`); fs.renameSync(f.bundleFile, n); f.bundleFile = n; }], ["digest", (f, v) => { v.bundle_id = `applied-bundle:${"a".repeat(64)}`; }], ["schema", (f, v) => { v.extra = true; }], ["provider", (f, v) => { v.provider = "luma"; }], ["event", (f, v) => { v.event_ref = "peatix-event://event/999"; }], ["status", (f, v) => { v.provider_status = "pending"; }], ["receipt", (f, v) => { v.provider_receipt_ref = `provider-receipt://peatix/${"f".repeat(64)}`; }], ["artifact", (f, v) => { v.artifact_ref = `object://sha256/${"b".repeat(64)}`; }], ["id", (f, v) => { v.telegram_message_provider_id = "0"; }], ["time", (f, v) => { v.created_at = "bad"; }], ["png", (f) => { f.store.readArtifact = async () => Buffer.alloc(6_008, 4); }], ["file-symlink", (f) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), "connector-bundle-outside-")); const t = path.join(d, "bundle.json"); fs.copyFileSync(f.bundleFile, t); fs.unlinkSync(f.bundleFile); fs.symlinkSync(t, f.bundleFile); f.outside = d; }], ["parent-symlink", (f) => { const p = path.dirname(f.bundleFile); const d = fs.mkdtempSync(path.join(os.tmpdir(), "connector-bundle-parent-")); fs.renameSync(p, path.join(d, "applied-bundles")); const out = fs.mkdtempSync(path.join(os.tmpdir(), "connector-bundle-outside-")); fs.symlinkSync(out, p, "dir"); f.saved = d; f.outside = out; }]];
  for (const [name, mutate] of cases) { const f = legacyBundleFixture({ png }); try { const value = JSON.parse(fs.readFileSync(f.bundleFile, "utf8")); mutate(f, value); if (!name.includes("symlink") && name !== "filename" && name !== "png") fs.writeFileSync(f.bundleFile, `${JSON.stringify(value)}\n`, { mode: 0o600 }); await assert.rejects(f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }), undefined, name); for (const n of ["set-content", "goto", "url", "evaluate", "screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) assert.equal(f.calls.filter(([callName]) => callName === n).length, 0, `${name}:${n}`); } finally { f.cleanup(); if (f.outside) fs.rmSync(f.outside, { recursive: true, force: true }); if (f.saved) fs.rmSync(f.saved, { recursive: true, force: true }); } }
});

test("bundle scan rejects multiple/unexpected entries, ignores unrelated exact bundles, and bounds files", async () => { const f = legacyBundleFixture(); try { const { bundle_id, ...base } = f.bundle; writeValidBundle(path.dirname(f.bundleFile), { ...base, calendar_event_id: "google-second", calendar_event_url: "https://www.google.com/calendar/event?eid=second", calendar_readback_at: "2026-08-07T08:31:00.000Z" }); await assert.rejects(f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } })); } finally { f.cleanup(); } const u = legacyBundleFixture(); try { legacyBundleFixture({ stateDir: u.stateDir, provider: "luma", candidate: { provider: "luma", event_ref: "luma-event://event/unrelated", canonical_url: "https://luma.com/unrelated", title: "Unrelated", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z", venue_name: "Tokyo" } }); const r = await u.chain.completeEvidence({ provider: "peatix", candidate: u.candidate, page: u.page, providerState: { status: "registered" } }); assert.equal(r.completion_disposition, "reused"); } finally { u.cleanup(); } for (const setup of [(f) => fs.writeFileSync(path.join(path.dirname(f.bundleFile), "unexpected.json"), "{}\n", { mode: 0o600 }), (f) => fs.writeFileSync(path.join(path.dirname(f.bundleFile), `${"c".repeat(64)}.json`), Buffer.alloc(20_000), { mode: 0o600 }), (f) => fs.mkdirSync(path.join(path.dirname(f.bundleFile), `${"b".repeat(64)}.json`))]) { const x = legacyBundleFixture(); try { setup(x); await assert.rejects(x.chain.completeEvidence({ provider: "peatix", candidate: x.candidate, page: x.page, providerState: { status: "registered" } })); } finally { x.cleanup(); } } });

test("semantic corruption recomputes digest and reaches provider/event/status/receipt/artifact/ID/time guards", async () => { const cases = [["provider", (core) => { core.provider = "luma"; }], ["event", (core) => { core.event_ref = "peatix-event://event/0"; }], ["status", (core) => { core.provider_status = "pending"; }], ["receipt", (core) => { core.provider_receipt_ref = `provider-receipt://peatix/${"f".repeat(64)}`; }], ["artifact", (core, f) => { const sha = "b".repeat(64); core.artifact_sha256 = sha; core.artifact_ref = `object://sha256/${sha}`; const id = createHash("sha256").update(`dais-local\n${core.event_ref}\n${core.created_at}\n${sha}`).digest("hex"); f.store.readExternalReceipt = async () => ({ kind: "provider_response", provider_id: id, observed_at: core.created_at, event_ref: core.event_ref, artifact_sha256: sha }); }], ["id", (core) => { core.telegram_message_provider_id = "0"; }], ["time", (core) => { core.created_at = "not-an-instant"; }]]; for (const [name, mutate] of cases) { const f = legacyBundleFixture(); try { rewriteBundle(f, mutate); await assert.rejects(f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }), undefined, name); assert.equal(f.calls.filter(([n]) => ["set-content", "screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(n)).length, 0, name); } finally { f.cleanup(); } } });

test("129 valid applied bundles hit the scan bound before any effect", async () => { const f = legacyBundleFixture(); try { const dir = path.dirname(f.bundleFile); fs.unlinkSync(f.bundleFile); const { bundle_id, ...base } = f.bundle; for (let i = 0; i < 129; i += 1) writeValidBundle(dir, { ...base, event_ref: `peatix-event://event/${7000000 + i}`, calendar_event_id: `other-${i}`, calendar_event_url: `https://www.google.com/calendar/event?eid=other-${i}`, telegram_message_provider_id: String(30000 + i), telegram_photo_provider_id: String(40000 + i), provider_receipt_ref: `provider-receipt://peatix/${String(i).padStart(64, "a")}` }); await assert.rejects(f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } })); assert.equal(f.calls.filter(([n]) => ["set-content", "screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(n)).length, 0); } finally { f.cleanup(); } });

test("Luma legacy exact bundle reuses deterministic evidence and current Calendar without side effects", async () => { const f = legacyBundleFixture({ provider: "luma", candidate: { provider: "luma", event_ref: "luma-event://event/legacy-luma", canonical_url: "https://luma.com/legacy-luma", title: "Legacy Luma", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z", venue_name: "Tokyo" } }); const before = fs.readFileSync(f.bundleFile); try { const r = await f.chain.completeEvidence({ provider: "luma", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }); assert.equal(r.completion_disposition, "reused"); assert.equal(r.bundle_id, f.bundle.bundle_id); assert.deepEqual(fs.readFileSync(f.bundleFile), before); assert.equal(f.calls.filter(([n]) => ["set-content", "screenshot", "evidence-record", "calendar-create", "telegram-message", "telegram-photo"].includes(n)).length, 0); assert.ok(f.calls.some(([n]) => n === "evidence-read-receipt") && f.calls.some(([n]) => n === "evidence-read-artifact") && f.calls.some(([n]) => n === "calendar-read")); } finally { f.cleanup(); } });

test("existing bundle requires one exact current Calendar event before reuse", async () => { const base = { id: "google-existing", htmlLink: "https://www.google.com/calendar/event?eid=existing" }; for (const events of [() => [], () => [base, { id: "other", htmlLink: "https://www.google.com/calendar/event?eid=other" }], () => [{ id: "other", htmlLink: base.htmlLink }], () => [{ id: base.id, htmlLink: "https://www.google.com/calendar/event?eid=other" }]]) { const f = legacyBundleFixture({ calendarEvents: () => events() }); try { await assert.rejects(f.chain.completeEvidence({ provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } })); assert.equal(f.calls.filter(([n]) => n === "calendar-create" || n.startsWith("telegram-") || n === "screenshot").length, 0); } finally { f.cleanup(); } } });

function checkpointFiles(stateDir) {
  try { return fs.readdirSync(path.join(stateDir, "evidence", "checkpoints")); } catch { return []; }
}

function deliveryCheckpointEntries(stateDir) {
  return checkpointFiles(stateDir).flatMap((file) => {
    const target = path.join(stateDir, "evidence", "checkpoints", file);
    try {
      const value = JSON.parse(fs.readFileSync(target, "utf8"));
      return value && (value.stage === "telegram_message" || value.stage === "telegram_photo")
        ? [{ file: target, value }]
        : [];
    } catch { return []; }
  });
}

function deliveryCheckpointFor(stateDir, stage) {
  const entry = deliveryCheckpointEntries(stateDir).find(({ value }) => value.stage === stage);
  assert.ok(entry, `missing ${stage} checkpoint`);
  return entry;
}

function recoveryPage(png, calls) {
  return {
    async goto(url, input) { calls.push(["goto", url, input]); },
    url() { calls.push(["url"]); return "about:blank"; },
    async evaluate() { calls.push(["evaluate"]); return true; },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
}

function makeRecoveryChain({ stateDir, png, calls, calendar, sendMessage, sendPhoto, now = "2026-08-07T08:30:00.000Z" }) {
  return createMinimalEvidenceChain({
    stateDir,
    tenantId: "dais-local",
    calendar,
    calendarId: "primary",
    telegramTarget: "private-target",
    peatixEvidenceStore: recoveryStore(png, calls),
    now: () => new Date(now),
    sendMessage,
    sendPhoto,
  });
}

function durableCalendar(calls, receipt = { id: "google-peatix-1", htmlLink: "https://www.google.com/calendar/event?eid=peatix-one" }) {
  return {
    async findConnectorEvents(input) { calls.push(["calendar-read", input]); return [receipt]; },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return receipt; },
  };
}

function recoveryStore(png, calls, options = {}) {
  const artifactSha = createHash("sha256").update(png).digest("hex");
  return { async record(input) { calls.push(["evidence-record", input]); const receiptHash = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${artifactSha}`).digest("hex"); return { external_receipt_ref: `provider-receipt://peatix/${receiptHash}`, artifact_ref: `object://sha256/${artifactSha}` }; },
    async readExternalReceipt(tenant, ref) { calls.push(["evidence-read-receipt", tenant, ref]); if (options.missingReceipt) throw new Error("missing receipt"); return { kind: "provider_response", provider_id: String(ref).split("/").at(-1) }; },
    async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return options.artifact || png; } };
}

function recoveryCandidate() {
  return peatixCandidate({ title: "Private Title", venue_name: "Private Venue", ticket_id: "private-ticket" });
}

function checkpointValue(fixture, overrides = {}) {
  const observedAt = "2026-08-07T08:30:00.000Z"; const eventRef = fixture.candidate.event_ref; const artifactSha = fixture.pngSha;
  const receiptId = createHash("sha256").update(`dais-local\n${eventRef}\n${observedAt}\n${artifactSha}`).digest("hex");
  const urlSha = createHash("sha256").update(fixture.candidate.canonical_url).digest("hex");
  return { schema_version: 1, stage: "evidence", provider: "peatix", event_ref: eventRef, canonical_url_sha256: urlSha, provider_status: "registered", provider_receipt_ref: `provider-receipt://peatix/${receiptId}`, artifact_ref: `object://sha256/${artifactSha}`, artifact_sha256: artifactSha, observed_at: observedAt, ...overrides };
}

function checkpointPathFor(fixture) {
  const urlSha = createHash("sha256").update(fixture.candidate.canonical_url).digest("hex");
  const identity = createHash("sha256").update(`peatix\n${fixture.candidate.event_ref}\n${urlSha}`).digest("hex");
  return path.join(fixture.stateDir, "evidence", "checkpoints", `${identity}.json`);
}
function rawPointer(fixture, raw) {
  const file = checkpointPathFor(fixture); fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 }); fs.writeFileSync(file, `${raw}\n`, { mode: 0o600 });
}
function symlinkPointer(fixture, raw, parent) {
  const file = checkpointPathFor(fixture); const outside = fs.mkdtempSync(path.join(os.tmpdir(), "connector-checkpoint-outside-")); const target = path.join(outside, path.basename(file)); fs.writeFileSync(target, `${raw}\n`, { mode: 0o600 }); fixture.outside = outside;
  if (parent) { fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 }); fs.rmSync(path.dirname(file), { recursive: true, force: true }); fs.symlinkSync(outside, path.dirname(file), "dir"); } else { fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 }); fs.symlinkSync(target, file); }
}

test("initial provider record rejects a forged receipt identity before pointer and downstream effects", async () => {
  const fixture = peatixFixture({ evidenceStore: { async record(input) { fixture.calls.push(["evidence-record", input]); return { external_receipt_ref: `provider-receipt://peatix/${"f".repeat(64)}`, artifact_ref: `object://sha256/${fixture.pngSha}` }; } } });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    for (const name of ["calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, name);
    assert.equal(checkpointFiles(fixture.stateDir).length, 0);
  } finally { fixture.cleanup(); }
});

test("checkpoint corruption and symlink matrix fails before every downstream effect", async () => {
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 6)]);
  const cases = [
    ["invalid-json", (f, p) => rawPointer(f, "{")],
    ["extra-key", (f, p) => rawPointer(f, JSON.stringify({ ...p, extra: "reject" }))],
    ["wrong-provider", (f, p) => rawPointer(f, JSON.stringify({ ...p, provider: "luma" }))],
    ["wrong-event", (f, p) => rawPointer(f, JSON.stringify({ ...p, event_ref: "peatix-event://event/999" }))],
    ["wrong-url-hash", (f, p) => rawPointer(f, JSON.stringify({ ...p, canonical_url_sha256: "e".repeat(64) }))],
    ["forged-receipt", (f, p) => rawPointer(f, JSON.stringify({ ...p, provider_receipt_ref: `provider-receipt://peatix/${"f".repeat(64)}` }))],
    ["malformed-receipt-ref", (f, p) => rawPointer(f, JSON.stringify({ ...p, provider_receipt_ref: "provider-receipt://peatix/not-a-hash" }))],
    ["malformed-artifact-ref", (f, p) => rawPointer(f, JSON.stringify({ ...p, artifact_ref: "object://sha256/not-a-hash" }))],
    ["artifact-ref-sha", (f, p) => rawPointer(f, JSON.stringify({ ...p, artifact_ref: `object://sha256/${"a".repeat(64)}` }))],
    ["missing-receipt", (f, p) => rawPointer(f, JSON.stringify(p)), { missingReceipt: true }],
    ["internally-consistent-non-png", (f, p) => { const bytes = Buffer.alloc(6_008, 5); const sha = createHash("sha256").update(bytes).digest("hex"); const receipt = createHash("sha256").update(`dais-local\n${f.candidate.event_ref}\n${p.observed_at}\n${sha}`).digest("hex"); rawPointer(f, JSON.stringify({ ...p, provider_receipt_ref: `provider-receipt://peatix/${receipt}`, artifact_ref: `object://sha256/${sha}`, artifact_sha256: sha })); }, { artifact: Buffer.alloc(6_008, 5) }],
    ["artifact-bytes", (f, p) => rawPointer(f, JSON.stringify(p)), { artifact: Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 5)]) }],
    ["file-symlink", (f, p) => symlinkPointer(f, JSON.stringify(p), false)],
    ["parent-symlink", (f, p) => symlinkPointer(f, JSON.stringify(p), true)],
  ];
  for (const [name, setup, storeOptions = {}] of cases) {
    const audit = []; const store = recoveryStore(png, audit, storeOptions); const fixture = peatixFixture({ png, evidenceStore: store });
    try {
      setup(fixture, checkpointValue(fixture));
      await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }), undefined, name);
      for (const effect of ["set-content", "goto", "url", "evaluate", "screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) assert.equal(fixture.calls.filter(([callName]) => callName === effect).length, 0, `${name}:${effect}`);
      assert.equal(bundleFiles(fixture.stateDir).length, 0, name);
    } finally { fixture.cleanup(); if (fixture.outside) fs.rmSync(fixture.outside, { recursive: true, force: true }); }
  }
});

test("evidence checkpoint recovers PNG/provider receipt without render, screenshot, or record", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-evidence-recovery-")); const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 9)]); const firstCalls = [];
  const evidenceStore = recoveryStore(png, firstCalls); const failingCalendar = { async findConnectorEvents() { firstCalls.push(["calendar-read"]); throw new Error("Calendar unavailable"); }, async createConnectorEvent() { firstCalls.push(["calendar-create"]); throw new Error("must not create"); } };
  const first = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar: failingCalendar, calendarId: "primary", telegramTarget: "private-target", peatixEvidenceStore: evidenceStore, now: () => new Date("2026-08-07T08:30:00.000Z"), sendMessage: async () => { throw new Error("must not send"); }, sendPhoto: async () => { throw new Error("must not send"); } });
  const firstPage = { async goto() {}, url() { return "about:blank"; }, async evaluate() { return true; }, async screenshot() { firstCalls.push(["screenshot"]); return png; } };
  await assert.rejects(first.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page: firstPage, providerState: { status: "registered" } }));
  assert.equal(firstCalls.filter(([name]) => name === "evidence-record").length, 1);
  assert.equal(firstCalls.filter(([name]) => name === "screenshot").length, 1);
  assert.equal(checkpointFiles(stateDir).length, 1);
  const checkpointFile = path.join(stateDir, "evidence", "checkpoints", checkpointFiles(stateDir)[0]);
  const checkpoint = JSON.parse(fs.readFileSync(checkpointFile, "utf8"));
  assert.deepEqual(Object.keys(checkpoint).sort(), ["artifact_ref", "artifact_sha256", "canonical_url_sha256", "event_ref", "observed_at", "provider", "provider_receipt_ref", "provider_status", "schema_version", "stage"]);
  assert.equal(checkpoint.stage, "evidence");
  assert.equal(fs.statSync(checkpointFile).mode & 0o777, 0o600);
  assert.doesNotMatch(JSON.stringify(checkpoint), /Private Title|Private Venue|private-ticket|private-target|peatix\.com|6536845|PNG/i);

  const secondCalls = [];
  const calendarReceipt = { id: "google-recovery", htmlLink: "https://www.google.com/calendar/event?eid=recovery" };
  let reads = 0;
  const second = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar: { async findConnectorEvents() { secondCalls.push(["calendar-read"]); return reads++ === 0 ? [calendarReceipt] : [calendarReceipt]; }, async createConnectorEvent() { secondCalls.push(["calendar-create"]); throw new Error("duplicate Calendar"); } }, calendarId: "primary", telegramTarget: "private-target", peatixEvidenceStore: recoveryStore(png, secondCalls), now: () => new Date("2026-08-07T08:31:00.000Z"), sendMessage: async () => ({ messageId: 1 }), sendPhoto: async () => ({ messageId: 2 }) });
  const noEffectsPage = { async goto() { throw new Error("render"); }, url() { throw new Error("render"); }, async evaluate() { throw new Error("render"); }, async screenshot() { throw new Error("screenshot"); } };
  const bundle = await second.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page: noEffectsPage, providerState: { status: "registered" } });
  assert.equal(bundle.status, "applied_bundle");
  assert.equal(secondCalls.filter(([name]) => name === "evidence-read-receipt").length, 1);
  assert.equal(secondCalls.filter(([name]) => name === "evidence-read-artifact").length, 1);
  for (const name of ["screenshot", "evidence-record", "calendar-create"]) assert.equal(secondCalls.filter(([callName]) => callName === name).length, 0, name);
  fs.rmSync(stateDir, { recursive: true, force: true });
});

test("Calendar create/readback crash recovery reuses the one idempotent event", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-calendar-recovery-"));
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 8)]); const calls = []; const store = recoveryStore(png, calls);
  const receipt = { id: "google-crash-recovery", htmlLink: "https://www.google.com/calendar/event?eid=crash-recovery" };
  let phase = "crash"; let created = false;
  const calendar = {
    async findConnectorEvents() { calls.push(["calendar-read", phase]); if (phase === "crash") return created ? (() => { throw new Error("readback crashed"); })() : []; if (phase === "deleted") return []; return [receipt]; },
    async createConnectorEvent() { calls.push(["calendar-create"]); created = true; return receipt; },
  };
  const chain = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar, calendarId: "primary", telegramTarget: "private-target", peatixEvidenceStore: store, now: () => new Date("2026-08-07T08:30:00.000Z"), sendMessage: async () => ({ messageId: 10 }), sendPhoto: async () => ({ messageId: 11 }) });
  const page = { async goto() {}, url() { return "about:blank"; }, async evaluate() { return true; }, async screenshot() { return png; } };
  await assert.rejects(chain.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page, providerState: { status: "registered" } }));
  assert.equal(calls.filter(([name]) => name === "calendar-create").length, 1);
  phase = "recovery";
  const recovered = await chain.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page, providerState: { status: "registered" } });
  assert.equal(recovered.status, "applied_bundle");
  assert.equal(calls.filter(([name]) => name === "calendar-create").length, 1);
  phase = "deleted";
  await assert.rejects(chain.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page, providerState: { status: "registered" } }));
  assert.equal(calls.filter(([name]) => name === "calendar-create").length, 1);
  fs.rmSync(stateDir, { recursive: true, force: true });
});

test("delivery block stage failures tag the thrown error with a stable per-stage code, not the generic fallback", async () => {
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 24)]);
  const cases = [
    ["EVIDENCE_SCREENSHOT_CAPTURE_FAILED", (calls) => ({
      calendar: durableCalendar(calls),
      sendMessage: async () => ({ messageId: 9401 }),
      sendPhoto: async () => ({ messageId: 9402 }),
      page: { ...recoveryPage(png, calls), async screenshot() { throw new Error("screenshot device gone"); } },
    })],
    ["EVIDENCE_CALENDAR_CREATE_FAILED", (calls) => ({
      calendar: { async findConnectorEvents() { calls.push(["calendar-read"]); return []; }, async createConnectorEvent() { calls.push(["calendar-create"]); throw new Error("calendar create gone"); } },
      sendMessage: async () => ({ messageId: 9401 }),
      sendPhoto: async () => ({ messageId: 9402 }),
      page: recoveryPage(png, calls),
    })],
    ["EVIDENCE_CALENDAR_READBACK_FAILED", (calls) => ({
      calendar: { async findConnectorEvents() { calls.push(["calendar-read"]); throw new Error("calendar readback gone"); }, async createConnectorEvent() { calls.push(["calendar-create"]); throw new Error("must not create"); } },
      sendMessage: async () => ({ messageId: 9401 }),
      sendPhoto: async () => ({ messageId: 9402 }),
      page: recoveryPage(png, calls),
    })],
    ["EVIDENCE_TELEGRAM_MESSAGE_FAILED", (calls) => ({
      calendar: durableCalendar(calls),
      sendMessage: async () => { throw new Error("telegram message gone"); },
      sendPhoto: async () => ({ messageId: 9402 }),
      page: recoveryPage(png, calls),
    })],
    ["EVIDENCE_TELEGRAM_PHOTO_FAILED", (calls) => ({
      calendar: durableCalendar(calls),
      sendMessage: async () => ({ messageId: 9401 }),
      sendPhoto: async () => { throw new Error("telegram photo gone"); },
      page: recoveryPage(png, calls),
    })],
  ];
  for (const [code, build] of cases) {
    const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-stage-code-"));
    const calls = [];
    const { calendar, sendMessage, sendPhoto, page } = build(calls);
    const chain = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar, calendarId: "primary", telegramTarget: "private-target", peatixEvidenceStore: recoveryStore(png, calls), now: () => new Date("2026-08-07T08:30:00.000Z"), sendMessage, sendPhoto });
    try {
      await assert.rejects(
        chain.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page, providerState: { status: "registered" } }),
        (error) => { assert.equal(error.code, code, code); assert.doesNotMatch(String(error.message), /gone|crash/i, code); return true; },
      );
    } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
  }

  // Bundle write: everything succeeds through Telegram delivery (same fixture as "Telegram checkpoints
  // survive bundle failure" above), then the applied-bundles directory is flipped read-only inside the
  // sendPhoto callback so only the final immutableJson write fails.
  const bundleStateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-stage-code-bundle-"));
  fs.mkdirSync(path.join(bundleStateDir, "applied-bundles"), { mode: 0o700 });
  try {
    const calls = [];
    const chain = createMinimalEvidenceChain({
      stateDir: bundleStateDir, tenantId: "dais-local", calendar: durableCalendar(calls), calendarId: "primary",
      telegramTarget: "private-target", peatixEvidenceStore: recoveryStore(png, calls),
      now: () => new Date("2026-08-07T08:30:00.000Z"),
      sendMessage: async () => ({ messageId: 9401 }),
      sendPhoto: async () => { fs.chmodSync(path.join(bundleStateDir, "applied-bundles"), 0o500); return { messageId: 9402 }; },
    });
    await assert.rejects(
      chain.completeEvidence({ provider: "peatix", candidate: recoveryCandidate(), page: recoveryPage(png, calls), providerState: { status: "registered" } }),
      (error) => { assert.equal(error.code, "EVIDENCE_BUNDLE_WRITE_FAILED"); return true; },
    );
  } finally {
    fs.chmodSync(path.join(bundleStateDir, "applied-bundles"), 0o700);
    fs.rmSync(bundleStateDir, { recursive: true, force: true });
  }
});

test("Peatix evidence resets the owned page and parent-writes the receipt without setContent", async () => {
  const fixture = peatixFixture({ setContentNeverSettles: true });
  try {
    const result = await Promise.race([
      fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }),
      new Promise((resolve) => setTimeout(() => resolve({ status: "timeout" }), 250)),
    ]);
    assert.equal(result.status, "applied_bundle");
    assert.equal(fixture.calls.filter(([name]) => name === "set-content").length, 0);
    assert.deepEqual(fixture.calls.find(([name]) => name === "goto"), ["goto", "about:blank", { waitUntil: "domcontentloaded", timeout: 30_000 }]);
    const write = fixture.calls.find(([name]) => name === "document-write");
    assert.ok(write);
    assert.match(write[1], /<dt>provider<\/dt><dd>peatix<\/dd>/i);
    assert.match(write[1], /<dt>status<\/dt><dd>registered<\/dd>/i);
    assert.match(write[1], /peatix-event:\/\/event\/5075819/);
    assert.doesNotMatch(write[1], /Peatix Public Event|https:\/\/peatix\.com|6536845|<script|<img|<iframe|<link|<style/i);
    assert.deepEqual(fixture.calls.filter(([name]) => ["document-open", "document-close"].includes(name)).map(([name]) => name), ["document-open", "document-close"]);
    assert.ok(fixture.calls.findIndex(([name]) => name === "document-close") < fixture.calls.findIndex(([name]) => name === "screenshot"));
    assert.equal(bundleFiles(fixture.stateDir).length, 1);
  } finally { fixture.cleanup(); }
});

test("Peatix receipt reset and validation fail before every downstream side effect", async () => {
  const cases = [
    ["missing-goto", (page) => { delete page.goto; }],
    ["missing-url", (page) => { delete page.url; }],
    ["missing-evaluate", (page) => { delete page.evaluate; }],
    ["reset-throws", (page) => { page.goto = async () => { throw new Error("reset failed"); }; }],
    ["wrong-reset-readback", (page) => { page.goto = async () => {}; }],
    ["receipt-write-throws", (page) => { page.evaluate = async () => { throw new Error("write failed"); }; }],
    ["receipt-validation-false", (page) => { page.evaluate = async () => false; }],
    ["invalid-png", (page) => { page.screenshot = async () => Buffer.from("not-a-png"); }],
    ["provider-mismatch", (page, fixture) => { fixture.candidate = peatixCandidate({ event_ref: "luma-event://event/mismatch" }); }],
  ];
  for (const [name, mutate] of cases) {
    const fixture = peatixFixture();
    try {
      mutate(fixture.page, fixture);
      await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }), undefined, name);
      for (const effect of ["screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) assert.equal(fixture.calls.filter(([callName]) => callName === effect).length, 0, `${name}:${effect}`);
      assert.equal(bundleFiles(fixture.stateDir).length, 0, name);
    } finally { fixture.cleanup(); }
  }
});

test("Peatix registered readback creates a provider-specific complete applied bundle", async () => {
  const fixture = peatixFixture();
  try {
    const bundle = await fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(bundle.provider, "peatix");
    assert.match(bundle.provider_receipt_ref, /^provider-receipt:\/\/peatix\/[0-9a-f]{64}$/);
    assert.equal(bundle.artifact_sha256, fixture.pngSha);
    assert.equal(fixture.calls.filter(([name]) => name === "calendar-read").length, 2);
    assert.equal(fixture.calls.filter(([name]) => name === "calendar-create").length, 1);
    assert.match(fixture.calls.find(([name]) => name === "telegram-message")[1], /provider: peatix/);
    assert.match(fixture.calls.find(([name]) => name === "telegram-photo")[2].caption, /peatix/i);
    assert.equal(fixture.calls.find(([name]) => name === "calendar-create")[1].canonicalUrl, fixture.candidate.canonical_url);
    assert.doesNotMatch(JSON.stringify(bundle), /Private Name|private@example\.test|6536845|private-target/);
    assert.equal(bundleFiles(fixture.stateDir).length, 1);
    const persisted = JSON.parse(fs.readFileSync(path.join(fixture.stateDir, "applied-bundles", `${bundle.bundle_id.slice("applied-bundle:".length)}.json`), "utf8"));
    assert.equal(persisted.provider, "peatix");
    assert.doesNotMatch(JSON.stringify(persisted), /Private Name|private@example\.test|6536845|private-target/);
  } finally { fixture.cleanup(); }
});

test("evidence parent-writes a fixed privacy-safe receipt before screenshot", async () => {
  const fixture = peatixFixture();
  try {
    await fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    const replacement = fixture.calls.find(([name]) => name === "document-write");
    const screenshotIndex = fixture.calls.findIndex(([name]) => name === "screenshot");
    assert.ok(replacement);
    assert.ok(fixture.calls.indexOf(replacement) < screenshotIndex);
    assert.match(replacement[1], /peatix-event:\/\/event\/5075819/);
    assert.match(replacement[1], /<dt>provider<\/dt><dd>peatix<\/dd>/i);
    assert.match(replacement[1], /<dt>status<\/dt><dd>registered<\/dd>/i);
    assert.doesNotMatch(replacement[1], /Peatix Public Event|https:\/\/peatix\.com|6536845|<script|<img|<iframe|<link|<style/i);
  } finally { fixture.cleanup(); }
});

test("receipt replacement failure prevents screenshot, evidence, Calendar, Telegram, and bundle effects", async () => {
  const fixture = peatixFixture(); fixture.page.evaluate = async () => { throw new Error("replacement failed"); };
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    for (const name of ["screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, name);
    assert.equal(bundleFiles(fixture.stateDir).length, 0);
  } finally { fixture.cleanup(); }
});

test("Peatix identity, state, receipt, Calendar, and Telegram partial failures never write a bundle", async () => {
  const wrongReceipt = { async record() { return { external_receipt_ref: `provider-receipt://luma/${"a".repeat(64)}`, artifact_ref: `object://sha256/${"c".repeat(64)}` }; } };
  let badCalendarReads = 0;
  const badCalendar = { async findConnectorEvents() { return badCalendarReads++ === 0 ? [] : [{ id: "wrong", htmlLink: "https://www.google.com/calendar/event?eid=wrong" }]; }, async createConnectorEvent() { return { id: "created", htmlLink: "https://www.google.com/calendar/event?eid=created" }; } };
  const cases = [
    () => { const f = peatixFixture(); return [f, { provider: "peatix", candidate: peatixCandidate({ canonical_url: "https://peatix.com/event/999" }), page: f.page, providerState: { status: "registered" } }]; },
    () => { const f = peatixFixture(); return [f, { provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "pending" } }]; },
    () => { const f = peatixFixture({ evidenceStore: wrongReceipt }); return [f, { provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }]; },
    () => { const f = peatixFixture({ calendar: badCalendar }); return [f, { provider: "peatix", candidate: f.candidate, page: f.page, providerState: { status: "registered" } }]; },
  ];
  for (const makeCase of cases) {
    const [fixture, input] = makeCase();
    try { await assert.rejects(fixture.chain.completeEvidence(input)); assert.equal(bundleFiles(fixture.stateDir).length, 0); } finally { fixture.cleanup(); }
  }
  for (const channel of ["message", "photo"]) {
    const fixture = peatixFixture(channel === "message" ? { sendMessage: async () => ({ messageId: 0 }) } : { sendPhoto: async () => ({ messageId: 0 }) });
    try { await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } })); assert.equal(deliveryCheckpointEntries(fixture.stateDir).length, channel === "message" ? 0 : 1); assert.equal(bundleFiles(fixture.stateDir).length, 0); } finally { fixture.cleanup(); }
  }
});

test("Peatix canonical URL rejects parser-normalized variants before every side effect", async () => {
  const invalidUrls = [
    "https://peatix.com/event/5075819/../5075819",
    "https://peatix.com/event/./5075819",
    "https://peatix.com:443/event/5075819",
    "https://PEATIX.COM/event/5075819",
    "https://peatix.com/event/5075819/",
    "https://peatix.com/event/5075819?utm_source=test",
    "https://peatix.com/event/5075819#details",
    " https://peatix.com/event/5075819",
    "https://peatix.com/event/5075819 ",
    "https://user:pass@peatix.com/event/5075819",
  ];
  for (const canonical_url of invalidUrls) {
    const fixture = peatixFixture();
    try {
      await assert.rejects(fixture.chain.completeEvidence({
        provider: "peatix",
        candidate: peatixCandidate({ canonical_url }),
        page: fixture.page,
        providerState: { status: "registered" },
      }));
      for (const name of ["screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
        assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, `${canonical_url}: ${name}`);
      }
      assert.equal(bundleFiles(fixture.stateDir).length, 0, canonical_url);
    } finally { fixture.cleanup(); }
  }
});

function connpassCandidate(extra = {}) {
  return { provider: "connpass", event_ref: "connpass-event://event/400028", canonical_url: "https://tokyo-builders.connpass.com/event/400028/", title: "Connpass Parent Verified Event", starts_at: "2026-08-12T10:00:00.000Z", ends_at: "2026-08-12T11:00:00.000Z", venue_name: "Tokyo", ...extra };
}

function connpassFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-connpass-evidence-")); const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 17)]); const pngSha = createHash("sha256").update(png).digest("hex"); const calls = [];
  const calendarReceipt = { id: "google-connpass-1", htmlLink: "https://www.google.com/calendar/event?eid=connpass-one" };
  let reads = 0;
  const calendar = { async findConnectorEvents(input) { calls.push(["calendar-read", input]); return reads++ === 0 ? [] : [calendarReceipt]; }, async createConnectorEvent(input) { calls.push(["calendar-create", input]); return calendarReceipt; } };
  const evidenceStore = {
    async record(input) { calls.push(["evidence-record", input]); const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex"); return { external_receipt_ref: `provider-receipt://connpass/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` }; },
    async readExternalReceipt(tenant, ref) { calls.push(["evidence-read-receipt", tenant, ref]); return { kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: "2026-08-11T08:30:00.000Z", event_ref: "connpass-event://event/400028", artifact_sha256: pngSha }; },
    async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return png; },
  };
  const chain = createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar, calendarId: "primary", telegramTarget: "private-target", connpassEvidenceStore: evidenceStore, now: () => new Date("2026-08-11T08:30:00.000Z"), sendMessage: async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9401 }; }, sendPhoto: async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9402 }; } });
  let screenshotCalls = 0; let currentPageUrl = options.pageUrl || "https://tokyo-builders.connpass.com/event/400028/";
  const page = { async screenshot(input) { calls.push(["screenshot", input]); screenshotCalls += 1; if (options.fullPageScreenshotThrows && screenshotCalls === 1) { if (options.changeUrlOnScreenshotFailure) currentPageUrl = "about:blank"; throw new Error("full page timeout"); } return png; }, async setContent() { throw new Error("Connpass must not replace the verified page"); }, async goto() { throw new Error("Connpass must not navigate away from the verified page"); }, url() { calls.push(["url"]); return currentPageUrl; } };
  return { stateDir, png, pngSha, calls, evidenceStore, chain, candidate: connpassCandidate(), page, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}
function assertConnpassNoDownstream(fixture, label) { assert.equal(fixture.calls.filter(([name]) => ["screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(name)).length, 0, label); }

test("Connpass captures the current parent-verified page and reuses the exact applied bundle", async () => {
  const fixture = connpassFixture();
  try {
    const first = await fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(first.provider, "connpass"); assert.equal(first.completion_disposition, "created");
    assert.match(first.provider_receipt_ref, /^provider-receipt:\/\/connpass\/[0-9a-f]{64}$/);
    assert.equal(first.artifact_sha256, fixture.pngSha);
    const count = (name) => fixture.calls.filter(([entry]) => entry === name).length;
    assert.equal(count("screenshot"), 1);
    assert.deepEqual(fixture.calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
    assert.deepEqual([count("calendar-create"), count("telegram-message"), count("telegram-photo")], [1, 1, 1]);
    const counts = new Map(["screenshot", "evidence-record", "calendar-create", "telegram-message", "telegram-photo"].map((name) => [name, count(name)]));
    const second = await fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(second.completion_disposition, "reused"); assert.equal(second.bundle_id, first.bundle_id);
    for (const [name, expected] of counts) assert.equal(count(name), expected, name);
    for (const pageUrl of ["https://tokyo-builders.connpass.com/event/400029/", "https://tokyo-builders.connpass.com/event/400028/?next=other", "https://tokyo-builders.connpass.com/event/400028/#other", "about:blank"]) {
      const before = fixture.calls.length; fixture.page.url = () => pageUrl;
      await assert.rejects(fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assertConnpassNoDownstream({ calls: fixture.calls.slice(before) }, pageUrl);
    }
  } finally { fixture.cleanup(); }
});

test("Connpass falls back to a viewport screenshot when full-page capture times out", async () => {
  const fixture = connpassFixture({ fullPageScreenshotThrows: true });
  try {
    const result = await fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(result.status, "applied_bundle");
    assert.deepEqual(fixture.calls.filter(([name]) => name === "screenshot").map(([, options]) => options), [
      { type: "png", fullPage: true },
      { type: "png", fullPage: false },
    ]);
  } finally { fixture.cleanup(); }
});

test("Connpass screenshot fallback rejects a page identity change before downstream effects", async () => {
  const fixture = connpassFixture({ fullPageScreenshotThrows: true, changeUrlOnScreenshotFailure: true });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.equal(fixture.calls.filter(([name]) => name === "screenshot").length, 1);
    assert.equal(fixture.calls.some(([name]) => ["evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(name)), false);
  } finally { fixture.cleanup(); }
});

test("Connpass identity, status, and current page URL reject before downstream effects", async () => {
  const cases = [{ candidate: { canonical_url: "https://tokyo-builders.connpass.com:443/event/400028/" } }, { candidate: { canonical_url: "https://tokyo-builders.connpass.com/event/400028/?utm_source=test" } }, { candidate: { canonical_url: "https://tokyo-builders.connpass.com/event/400029/" } }, { candidate: { event_ref: "connpass-event://event/0" } }, { status: "absent" }, ...["https://tokyo-builders.connpass.com/event/400029/", "https://tokyo-builders.connpass.com/event/400028/?next=other", "about:blank"].map((pageUrl) => ({ pageUrl }))];
  for (const input of cases) {
    const fixture = connpassFixture({ pageUrl: input.pageUrl });
    try { await assert.rejects(fixture.chain.completeEvidence({ provider: "connpass", candidate: connpassCandidate(input.candidate), page: fixture.page, providerState: { status: input.status || "registered" } })); assertConnpassNoDownstream(fixture, input.pageUrl || input.candidate?.canonical_url); }
    finally { fixture.cleanup(); }
  }
});

test("Connpass reused evidence fails closed on receipt or artifact corruption before downstream effects", async () => {
  for (const corruption of ["event", "artifact", "bytes", "missing"]) {
    const fixture = connpassFixture();
    try {
      const first = await fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
      const before = fixture.calls.length;
      if (corruption === "event") fixture.evidenceStore.readExternalReceipt = async () => ({ kind: "provider_response", provider_id: first.provider_receipt_ref.split("/").at(-1), observed_at: "2026-08-11T08:30:00.000Z", event_ref: "connpass-event://event/400029", artifact_sha256: fixture.pngSha });
      if (corruption === "artifact") fixture.evidenceStore.readExternalReceipt = async (tenant, ref) => ({ kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: "2026-08-11T08:30:00.000Z", event_ref: fixture.candidate.event_ref, artifact_sha256: "a".repeat(64) });
      if (corruption === "bytes") fixture.evidenceStore.readArtifact = async () => Buffer.concat([fixture.png, Buffer.from("tamper")]);
      if (corruption === "missing") fixture.evidenceStore.readExternalReceipt = async (tenant, ref) => ({ kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: "2026-08-11T08:30:00.000Z" });
      await assert.rejects(fixture.chain.completeEvidence({ provider: "connpass", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assertConnpassNoDownstream({ calls: fixture.calls.slice(before) }, corruption);
    } finally { fixture.cleanup(); }
  }
});

test("Telegram provider rejection or missing ID never writes a delivery checkpoint", async () => {
  for (const response of [{ ok: false }, { messageId: 0 }]) {
    const fixture = peatixFixture({
      sendMessage: async () => response,
      sendPhoto: async () => { throw new Error("photo must not run"); },
    });
    try {
      await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assert.equal(fixture.calls.filter(([name]) => name === "telegram-message").length, 0);
      assert.equal(deliveryCheckpointEntries(fixture.stateDir).length, 0);
    } finally { fixture.cleanup(); }
  }
});

test("known Telegram provider rejection releases only that stage claim for one later retry", async () => {
  let messageCalls = 0;
  let attempt = 0;
  const fixture = peatixFixture({
    sendMessage: async () => { messageCalls += 1; return attempt++ === 0 ? { ok: false } : { messageId: 9301 }; },
    sendPhoto: async () => ({ messageId: 9302 }),
  });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    const retryCalls = [];
    const retry = makeRecoveryChain({
      stateDir: fixture.stateDir, png: fixture.png, calls: retryCalls, calendar: durableCalendar(retryCalls),
      sendMessage: async () => { messageCalls += 1; return { messageId: 9301 }; },
      sendPhoto: async () => ({ messageId: 9302 }),
    });
    const bundle = await retry.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(fixture.png, retryCalls), providerState: { status: "registered" } });
    assert.equal(bundle.telegram_message_provider_id, "9301");
    assert.equal(messageCalls, 2);
  } finally { fixture.cleanup(); }
});

test("unknown Telegram result keeps the stage claim and never retries after cleanup failure", async () => {
  const originalUnlink = fs.unlinkSync;
  let messageCalls = 0;
  const fixture = peatixFixture({
    sendMessage: async () => { messageCalls += 1; return { ok: false }; }, sendPhoto: async () => ({ messageId: 9302 }),
  });
  try {
    fs.unlinkSync = (file) => {
      if (String(file).endsWith(".claim")) throw new Error("claim cleanup unavailable");
      return originalUnlink(file);
    };
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    fs.unlinkSync = originalUnlink;
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.equal(messageCalls, 1);
  } finally {
    fs.unlinkSync = originalUnlink;
    fixture.cleanup();
  }
});

test("evidence message validates visible length before claim and HTML-escapes provider fields", async () => {
  let delivered = "";
  const fixture = peatixFixture({
    sendMessage: async (message) => { delivered = message; return { messageId: 9303 }; },
    sendPhoto: async () => ({ messageId: 9304 }),
  });
  fixture.candidate.title = "<&>\"'".repeat(100);
  fixture.candidate.venue_name = "<&>\"'".repeat(400);
  try {
    await fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    const visible = delivered.replace(/&lt;/g, "<").replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
    assert.equal(visible.length <= 4_096, true);
    assert.match(delivered, /&lt;|&amp;|&gt;|&quot;|&#39;/);
    assert.doesNotMatch(delivered, /[<>"']/);
  } finally { fixture.cleanup(); }
});

test("local message validation fails before creating a delivery claim", async () => {
  let sends = 0;
  const fixture = peatixFixture({ sendMessage: async () => { sends += 1; return { messageId: 9305 }; }, sendPhoto: async () => ({ messageId: 9306 }) });
  fixture.candidate.title = "invalid\u0000provider title";
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.equal(sends, 0);
    assert.equal(fs.existsSync(path.join(fixture.stateDir, "evidence", "delivery-claims")), false);
  } finally { fixture.cleanup(); }
});

test("message claim without a checkpoint blocks an uncertain replay", async () => {
  let messageSends = 0;
  const fixture = peatixFixture({
    sendMessage: async () => { messageSends += 1; throw new Error("message transport uncertain"); },
    sendPhoto: async () => { throw new Error("photo must not run"); },
  });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    const claims = fs.readdirSync(path.join(fixture.stateDir, "evidence", "delivery-claims")).filter((name) => name.endsWith(".claim"));
    assert.equal(claims.length, 1);
    const secondCalls = [];
    const second = makeRecoveryChain({
      stateDir: fixture.stateDir, png: fixture.png, calls: secondCalls, calendar: durableCalendar(secondCalls),
      sendMessage: async () => { messageSends += 1; return { messageId: 9201 }; },
      sendPhoto: async () => { throw new Error("photo must not run"); },
    });
    await assert.rejects(second.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(fixture.png, secondCalls), providerState: { status: "registered" } }));
    assert.equal(messageSends, 1);
    assert.equal(secondCalls.filter(([name]) => name.startsWith("telegram-")).length, 0);
  } finally { fixture.cleanup(); }
});

test("Telegram message checkpoint does not resend the message after an uncertain photo failure", async () => {
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 12)]); let photoAttempts = 0;
  const fixture = peatixFixture({ png, sendMessage: async (m, o) => { fixture.calls.push(["telegram-message", m, o]); return { messageId: 9201 }; }, sendPhoto: async (b, o) => { fixture.calls.push(["telegram-photo", b, o]); if (++photoAttempts === 1) throw new Error("photo interrupted"); return { messageId: 9202 }; } });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.deepEqual([fixture.calls.filter(([n]) => n === "telegram-message").length, fixture.calls.filter(([n]) => n === "telegram-photo").length, deliveryCheckpointEntries(fixture.stateDir).length, bundleFiles(fixture.stateDir).length], [1, 1, 1, 0]);
    const calls = []; const chain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls, calendar: durableCalendar(calls), sendMessage: async () => { throw new Error("message resend"); }, sendPhoto: async (b, o) => { calls.push(["telegram-photo", b, o]); return { messageId: 9202 }; }, now: "2026-08-07T08:31:00.000Z" });
    await assert.rejects(chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, calls), providerState: { status: "registered" } }));
    assert.deepEqual([calls.filter(([n]) => n === "telegram-message").length, calls.filter(([n]) => n === "telegram-photo").length, bundleFiles(fixture.stateDir).length], [0, 0, 0]);
  } finally { fixture.cleanup(); }
});

test("two independent evidence processes claim one message stage and only one sends", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-evidence-cross-process-"));
  const startFile = path.join(stateDir, "start-gate");
  const releaseFile = path.join(stateDir, "release-gate");
  const sendsFile = path.join(stateDir, "actual-sends.jsonl");
  const modulePath = path.join(__dirname, "connector-minimal-evidence.js");
  const childScript = `
    const fs = require("node:fs");
    const { createHash } = require("node:crypto");
    const { createMinimalEvidenceChain } = require(process.env.EVIDENCE_MODULE);
    const png = Buffer.concat([Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a]), Buffer.alloc(6000, 4)]);
    const artifactSha = createHash("sha256").update(png).digest("hex");
    const candidate = { provider: "meetup", event_ref: "meetup-event://event/202", canonical_url: "https://www.meetup.com/tokyo-builders/events/202/", title: "Cross Process", starts_at: "2026-08-12T10:00:00.000Z", ends_at: "2026-08-12T11:00:00.000Z", venue_name: "Tokyo" };
    const calendar = { reads: 0, async findConnectorEvents() { return this.reads++ === 0 ? [] : [{ id: "google-cross", htmlLink: "https://www.google.com/calendar/event?eid=cross" }]; }, async createConnectorEvent() { return { id: "google-cross", htmlLink: "https://www.google.com/calendar/event?eid=cross" }; } };
    const store = { async record(input) { const id = createHash("sha256").update(input.tenantId + "\\n" + input.eventRef + "\\n" + input.observedAt + "\\n" + artifactSha).digest("hex"); return { external_receipt_ref: "provider-receipt://meetup/" + id, artifact_ref: "object://sha256/" + artifactSha }; }, async readExternalReceipt(_tenant, ref) { return { kind: "provider_response", provider_id: String(ref).split("/").at(-1), observed_at: "2026-08-12T08:30:00.000Z", event_ref: candidate.event_ref, artifact_sha256: artifactSha }; }, async readArtifact() { return png; } };
    const wait = (file) => fs.existsSync(file) ? Promise.resolve() : new Promise((resolve) => setTimeout(() => resolve(wait(file)), 5));
    (async () => {
      fs.writeFileSync(process.env.READY_FILE, String(process.pid), { mode: 0o600 });
      await wait(process.env.START_FILE);
      const chain = createMinimalEvidenceChain({ stateDir: process.env.STATE_DIR, tenantId: "dais-local", calendar, calendarId: "primary", telegramTarget: "private-target", meetupEvidenceStore: store, now: () => new Date("2026-08-12T08:30:00.000Z"), sendMessage: async () => { const fd = fs.openSync(process.env.SENDS_FILE, "a", 0o600); try { fs.writeSync(fd, JSON.stringify({ pid: process.pid }) + "\\n"); fs.fsyncSync(fd); } finally { fs.closeSync(fd); } await wait(process.env.RELEASE_FILE); return { messageId: 9900 + Number(process.env.ROLE) }; }, sendPhoto: async () => ({ messageId: 9910 + Number(process.env.ROLE) }) });
      try { const result = await chain.completeEvidence({ provider: "meetup", candidate, page: { url() { return candidate.canonical_url; }, async screenshot() { return png; } }, providerState: { status: "registered" } }); const output = { ok: true, result }; fs.writeFileSync(process.env.RESULT_FILE, JSON.stringify(output), { mode: 0o600 }); console.log(JSON.stringify(output)); }
      catch (error) { const output = { ok: false, code: error && error.code }; fs.writeFileSync(process.env.RESULT_FILE, JSON.stringify(output), { mode: 0o600 }); console.log(JSON.stringify(output)); }
    })();
  `;
  const children = [];
  const launch = (role) => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["-e", childScript], {
      cwd: __dirname,
      env: { ...process.env, EVIDENCE_MODULE: modulePath, STATE_DIR: stateDir, START_FILE: startFile, RELEASE_FILE: releaseFile, SENDS_FILE: sendsFile, READY_FILE: path.join(stateDir, `ready-${role}`), RESULT_FILE: path.join(stateDir, `result-${role}`), ROLE: String(role) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    children.push(child);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
  try {
    const first = launch(1);
    const second = launch(2);
    const readyAt = Date.now() + 5_000;
    while ((!fs.existsSync(path.join(stateDir, "ready-1")) || !fs.existsSync(path.join(stateDir, "ready-2"))) && Date.now() < readyAt) await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(fs.existsSync(path.join(stateDir, "ready-1")) && fs.existsSync(path.join(stateDir, "ready-2")), true);
    fs.writeFileSync(startFile, "go\n", { mode: 0o600 });
    const overlapAt = Date.now() + 5_000;
    let loserObserved = false;
    while (Date.now() < overlapAt) {
      for (const role of [1, 2]) {
        const resultFile = path.join(stateDir, `result-${role}`);
        if (!fs.existsSync(resultFile)) continue;
        try { loserObserved ||= JSON.parse(fs.readFileSync(resultFile, "utf8")).code === "EVIDENCE_TELEGRAM_MESSAGE_FAILED"; } catch { /* child is publishing */ }
      }
      if (loserObserved) break;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    assert.equal(loserObserved, true);
    fs.writeFileSync(releaseFile, "release\n", { mode: 0o600 });
    const results = await Promise.all([first, second]);
    const outputs = results.map((result) => JSON.parse(result.stdout.trim()));
    assert.equal(outputs.filter((result) => result.ok).length, 1);
    assert.deepEqual(outputs.filter((result) => !result.ok).map((result) => result.code), ["EVIDENCE_TELEGRAM_MESSAGE_FAILED"]);
    assert.equal(fs.readFileSync(sendsFile, "utf8").trim().split("\n").length, 1);
  } finally {
    for (const child of children) if (child.exitCode == null) child.kill("SIGTERM");
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("Telegram checkpoints survive bundle failure and completed rerun is idempotent", async () => {
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 13)]); let blockBundleWrite = true; const fixture = peatixFixture({ png, sendPhoto: async (bytes, options) => { fixture.calls.push(["telegram-photo", bytes, options]); if (blockBundleWrite) fs.chmodSync(path.join(fixture.stateDir, "applied-bundles"), 0o500); return { messageId: 9102 }; } }); fs.mkdirSync(path.join(fixture.stateDir, "applied-bundles"), { mode: 0o700 });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.deepEqual([deliveryCheckpointEntries(fixture.stateDir).length, bundleFiles(fixture.stateDir).length], [2, 0]); fs.chmodSync(path.join(fixture.stateDir, "applied-bundles"), 0o700); blockBundleWrite = false;
    const calls = []; const chain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls, calendar: durableCalendar(calls), sendMessage: async () => { throw new Error("message resend"); }, sendPhoto: async () => { throw new Error("photo resend"); }, now: "2026-08-07T08:32:00.000Z" });
    const bundle = await chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, calls), providerState: { status: "registered" } });
    assert.deepEqual([bundle.telegram_message_provider_id, bundle.telegram_photo_provider_id, calls.filter(([n]) => n.startsWith("telegram-")).length, bundleFiles(fixture.stateDir).length], ["9101", "9102", 0, 1]);
    const rerunCalls = []; const rerun = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls: rerunCalls, calendar: durableCalendar(rerunCalls), sendMessage: async () => { throw new Error("completed message"); }, sendPhoto: async () => { throw new Error("completed photo"); }, now: "2026-08-07T08:33:00.000Z" });
    const second = await rerun.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, rerunCalls), providerState: { status: "registered" } }); assert.equal(second.bundle_id, bundle.bundle_id); assert.equal(rerunCalls.filter(([n]) => n.startsWith("telegram-")).length, 0); assert.equal(bundleFiles(fixture.stateDir).length, 1);
  } finally { if (fs.existsSync(path.join(fixture.stateDir, "applied-bundles"))) fs.chmodSync(path.join(fixture.stateDir, "applied-bundles"), 0o700); fixture.cleanup(); }
});

test("Telegram checkpoints are exact/private and corrupt or symlinked receipts fail before delivery", async () => {
  const messageKeys = "artifact_ref,artifact_sha256,calendar_event_id,calendar_event_url,calendar_readback_at,canonical_url_sha256,event_ref,provider,provider_receipt_ref,schema_version,stage,telegram_message_provider_id";
  const photoKeys = "artifact_ref,artifact_sha256,calendar_event_id,calendar_event_url,calendar_readback_at,canonical_url_sha256,event_ref,message_checkpoint_sha256,provider,provider_receipt_ref,schema_version,stage,telegram_message_provider_id,telegram_photo_provider_id";
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 14)]); const fixture = peatixFixture({ png });
  try {
    await fixture.chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }); const entries = deliveryCheckpointEntries(fixture.stateDir); assert.equal(entries.length, 2);
    for (const { file, value } of entries) { assert.equal(fs.statSync(file).mode & 0o777, 0o600); assert.equal(Object.keys(value).sort().join(","), value.stage === "telegram_message" ? messageKeys : photoKeys); assert.doesNotMatch(JSON.stringify(value), /private-target|private-ticket|peatix\.com|6536845|data:image|PNG/i); }
    const original = entries.find(({ value }) => value.stage === "telegram_message").value;
    const corruptions = [["invalid", () => fs.writeFileSync(deliveryCheckpointFor(fixture.stateDir, "telegram_message").file, "{\n")], ["extra", (f) => { f.extra = 1; }], ["provider", (f) => { f.provider = "luma"; }], ["event", (f) => { f.event_ref = "peatix-event://event/999"; }], ["url", (f) => { f.canonical_url_sha256 = "e".repeat(64); }], ["id", (f) => { f.telegram_message_provider_id = "0"; }], ["calendar", (f) => { f.calendar_event_id = "wrong"; }], ["stage", (f) => { f.stage = "telegram_photo"; }]];
    for (const [name, mutate] of corruptions) { const entry = deliveryCheckpointFor(fixture.stateDir, "telegram_message"); let value = original; if (name === "invalid") mutate(); else { value = { ...original }; mutate(value); fs.writeFileSync(entry.file, `${JSON.stringify(value)}\n`, { mode: 0o600 }); } const calls = []; const chain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls, calendar: durableCalendar(calls), sendMessage: async () => { throw new Error(`${name} message`); }, sendPhoto: async () => { throw new Error(`${name} photo`); } }); await assert.rejects(chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, calls), providerState: { status: "registered" } })); assert.equal(calls.filter(([n]) => n === "calendar-read").length, 0, name); fs.writeFileSync(entry.file, `${JSON.stringify(original)}\n`, { mode: 0o600 }); }
    const photoOriginal = deliveryCheckpointFor(fixture.stateDir, "telegram_photo").value;
    for (const [name, mutate] of [["photo-id", (v) => { v.telegram_message_provider_id = "9999"; }], ["photo-time", (v) => { v.calendar_readback_at = "2026-08-07T08:31:00.000Z"; }], ["photo-sha", (v) => { v.message_checkpoint_sha256 = "f".repeat(64); }], ["photo-receipt", (v) => { v.provider_receipt_ref = `provider-receipt://peatix/${"a".repeat(64)}`; }], ["photo-artifact", (v) => { v.artifact_ref = `object://sha256/${"a".repeat(64)}`; }], ["photo-unsafe", (v) => { v.telegram_photo_provider_id = "9007199254740992"; }]]) { const photo = deliveryCheckpointFor(fixture.stateDir, "telegram_photo"); const value = { ...photoOriginal }; mutate(value); fs.writeFileSync(photo.file, `${JSON.stringify(value)}\n`, { mode: 0o600 }); const calls = []; const chain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls, calendar: durableCalendar(calls), sendMessage: async () => { throw new Error(`${name}:message`); }, sendPhoto: async () => { throw new Error(`${name}:photo`); } }); await assert.rejects(chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, calls), providerState: { status: "registered" } })); assert.equal(calls.filter(([n]) => n === "calendar-read").length, 0, name); fs.writeFileSync(photo.file, `${JSON.stringify(photoOriginal)}\n`, { mode: 0o600 }); }
    const removedMessage = deliveryCheckpointFor(fixture.stateDir, "telegram_message"); fs.unlinkSync(removedMessage.file); const orphanCalls = []; const orphanChain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls: orphanCalls, calendar: durableCalendar(orphanCalls), sendMessage: async () => { throw new Error("orphan message"); }, sendPhoto: async () => { throw new Error("orphan photo"); } }); await assert.rejects(orphanChain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, orphanCalls), providerState: { status: "registered" } })); assert.equal(orphanCalls.filter(([n]) => n === "calendar-read").length, 0); fs.writeFileSync(removedMessage.file, `${JSON.stringify(original)}\n`, { mode: 0o600 });
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), "connector-delivery-outside-")); const entry = deliveryCheckpointFor(fixture.stateDir, "telegram_message"); const target = path.join(outside, "message.json"); fs.copyFileSync(entry.file, target); fs.unlinkSync(entry.file); fs.symlinkSync(target, entry.file); const calls = []; const chain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls, calendar: durableCalendar(calls), sendMessage: async () => { throw new Error("symlink message"); }, sendPhoto: async () => { throw new Error("symlink photo"); } }); await assert.rejects(chain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, calls), providerState: { status: "registered" } })); assert.equal(calls.filter(([n]) => n === "calendar-read").length, 0); fs.unlinkSync(entry.file); fs.rmSync(outside, { recursive: true, force: true });
    const parentOutside = fs.mkdtempSync(path.join(os.tmpdir(), "connector-delivery-parent-outside-")); const checkpointDir = path.join(fixture.stateDir, "evidence", "checkpoints"); const saved = fs.mkdtempSync(path.join(os.tmpdir(), "connector-delivery-saved-")); for (const file of fs.readdirSync(checkpointDir)) fs.renameSync(path.join(checkpointDir, file), path.join(saved, file)); fs.rmSync(checkpointDir, { recursive: true, force: true }); fs.symlinkSync(parentOutside, checkpointDir, "dir"); const parentCalls = []; const parentChain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls: parentCalls, calendar: durableCalendar(parentCalls), sendMessage: async () => { throw new Error("parent message"); }, sendPhoto: async () => { throw new Error("parent photo"); } }); await assert.rejects(parentChain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, parentCalls), providerState: { status: "registered" } })); assert.equal(parentCalls.filter(([n]) => n === "calendar-read").length, 0); fs.unlinkSync(checkpointDir); fs.renameSync(saved, checkpointDir); fs.rmSync(parentOutside, { recursive: true, force: true });
    const bundleDir = path.join(fixture.stateDir, "applied-bundles"); const bundleFile = path.join(bundleDir, fs.readdirSync(bundleDir)[0]); const bundleOutside = fs.mkdtempSync(path.join(os.tmpdir(), "connector-bundle-outside-")); fs.rmSync(bundleDir, { recursive: true, force: true }); fs.symlinkSync(bundleOutside, bundleDir, "dir"); const bundleCalls = []; const bundleChain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls: bundleCalls, calendar: durableCalendar(bundleCalls), sendMessage: async () => { throw new Error("bundle message"); }, sendPhoto: async () => { throw new Error("bundle photo"); } }); await assert.rejects(bundleChain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, bundleCalls), providerState: { status: "registered" } })); assert.equal(bundleCalls.filter(([n]) => n.startsWith("telegram-")).length, 0); fs.unlinkSync(bundleDir); fs.mkdirSync(bundleDir, { mode: 0o700 }); fs.writeFileSync(path.join(bundleDir, path.basename(bundleFile)), "{\"forged\":true}\n", { mode: 0o600 }); const collisionCalls = []; const collisionChain = makeRecoveryChain({ stateDir: fixture.stateDir, png, calls: collisionCalls, calendar: durableCalendar(collisionCalls), sendMessage: async () => { throw new Error("collision message"); }, sendPhoto: async () => { throw new Error("collision photo"); } }); await assert.rejects(collisionChain.completeEvidence({ provider: "peatix", candidate: fixture.candidate, page: recoveryPage(png, collisionCalls), providerState: { status: "registered" } })); assert.equal(collisionCalls.filter(([n]) => n.startsWith("telegram-")).length, 0); fs.rmSync(bundleOutside, { recursive: true, force: true });
  } finally { fixture.cleanup(); }
});

test("composed provider, Calendar, Telegram, and bundle interruptions keep totals bounded", async () => {
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 15)]); const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-composed-recovery-")); fs.mkdirSync(path.join(stateDir, "applied-bundles"), { mode: 0o700 }); const candidate = peatixCandidate(); const receipt = { id: "google-composed", htmlLink: "https://www.google.com/calendar/event?eid=composed" }; const calls = []; let created = false; let phase = "crash"; let records = 0; const store = { async record(input) { records += 1; const sha = createHash("sha256").update(png).digest("hex"); const id = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${sha}`).digest("hex"); return { external_receipt_ref: `provider-receipt://peatix/${id}`, artifact_ref: `object://sha256/${sha}` }; }, async readExternalReceipt(t, r) { calls.push(["evidence-read", t, r]); return { kind: "provider_response", provider_id: String(r).split("/").at(-1) }; }, async readArtifact() { return png; } };
  const calendar = { async findConnectorEvents(input) { calls.push(["calendar-read", input]); if (!created) return []; if (phase === "crash") throw new Error("readback crash"); return [receipt]; }, async createConnectorEvent(input) { calls.push(["calendar-create", input]); created = true; return receipt; } };
  const chain = (now, sendMessage, sendPhoto) => createMinimalEvidenceChain({ stateDir, tenantId: "dais-local", calendar, calendarId: "primary", telegramTarget: "private-target", peatixEvidenceStore: store, now: () => new Date(now), sendMessage, sendPhoto }); const input = { provider: "peatix", candidate, providerState: { status: "registered" } };
  try {
    await assert.rejects(chain("2026-08-07T08:30:00.000Z", async () => ({ messageId: 9301 }), async () => ({ messageId: 9302 })).completeEvidence({ ...input, page: recoveryPage(png, calls) })); phase = "telegram";
    await assert.rejects(chain("2026-08-07T08:31:00.000Z", async () => { calls.push(["telegram-message"]); return { messageId: 9301 }; }, async () => { calls.push(["telegram-photo"]); throw new Error("photo crash"); }).completeEvidence({ ...input, page: recoveryPage(png, calls) }));
    const beforeRetry = calls.length;
    await assert.rejects(chain("2026-08-07T08:32:00.000Z", async () => { calls.push(["telegram-message"]); return { messageId: 9301 }; }, async () => { calls.push(["telegram-photo"]); return { messageId: 9302 }; }).completeEvidence({ ...input, page: recoveryPage(png, calls) }));
    assert.deepEqual([records, calls.filter(([n]) => n === "calendar-create").length, calls.filter(([n]) => n === "telegram-message").length, calls.filter(([n]) => n === "telegram-photo").length, bundleFiles(stateDir).length, calls.slice(beforeRetry).filter(([n]) => n.startsWith("telegram-")).length], [1, 1, 1, 1, 0, 0]);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

function meetupCandidate(extra = {}) {
  return {
    provider: "meetup", event_ref: "meetup-event://event/101",
    canonical_url: "https://www.meetup.com/tokyo-builders/events/101/",
    title: "Community Event", starts_at: "2026-08-12T10:00:00.000Z", ends_at: "2026-08-12T11:00:00.000Z",
    venue_name: "Tokyo", ...extra,
  };
}

function meetupFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-meetup-evidence-"));
  const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 18)]);
  const pngSha = createHash("sha256").update(png).digest("hex");
  const calls = [];
  const calendarReceipt = { id: "google-meetup-1", htmlLink: "https://www.google.com/calendar/event?eid=meetup-one" };
  let reads = 0;
  const calendar = options.calendar || {
    async findConnectorEvents(input) { calls.push(["calendar-read", input]); return reads++ === 0 ? [] : [calendarReceipt]; },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return calendarReceipt; },
  };
  const evidenceStore = options.evidenceStore || {
    async record(input) {
      calls.push(["evidence-record", input]);
      const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex");
      return { external_receipt_ref: `provider-receipt://meetup/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` };
    },
    async readExternalReceipt(tenant, ref) {
      calls.push(["evidence-read-receipt", tenant, ref]);
      return { kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: "2026-08-11T08:30:00.000Z", event_ref: "meetup-event://event/101", artifact_sha256: pngSha };
    },
    async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return png; },
  };
  const chain = createMinimalEvidenceChain({
    stateDir, tenantId: "meetup-test", calendar, calendarId: "primary", telegramTarget: "test-target",
    meetupEvidenceStore: evidenceStore, now: () => new Date("2026-08-11T08:30:00.000Z"),
    sendMessage: options.sendMessage || (async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9501 }; }),
    sendPhoto: options.sendPhoto || (async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9502 }; }),
  });
  const pageUrl = { value: options.pageUrl || "https://www.meetup.com/tokyo-builders/events/101/" };
  const page = {
    async setContent(value) { calls.push(["set-content", value]); throw new Error("Meetup must not replace the registered page"); },
    async goto(url, input) { calls.push(["goto", url, input]); throw new Error("Meetup must not navigate away from the registered page"); },
    async evaluate() { calls.push(["evaluate"]); throw new Error("Meetup must not render a receipt"); },
    url() { calls.push(["url"]); return pageUrl.value; },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
  return { stateDir, png, pngSha, calls, evidenceStore, chain, candidate: meetupCandidate(options.candidate), page, pageUrl, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

function assertMeetupNoDownstream(fixture, label) {
  for (const name of ["screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
    assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, `${label}:${name}`);
  }
}

test("Meetup captures the registered page and reuses immutable evidence without navigation or replacement", async () => {
  const fixture = meetupFixture();
  try {
    const first = await fixture.chain.completeEvidence({ provider: "meetup", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(first.provider, "meetup");
    assert.equal(first.completion_disposition, "created");
    assert.match(first.provider_receipt_ref, /^provider-receipt:\/\/meetup\/[0-9a-f]{64}$/);
    assert.equal(first.telegram_message_provider_id, "9501");
    assert.equal(first.telegram_photo_provider_id, "9502");
    assert.deepEqual(fixture.calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
    assert.equal(fixture.calls.filter(([name]) => ["set-content", "goto", "evaluate"].includes(name)).length, 0);
    assert.deepEqual(["calendar-create", "telegram-message", "telegram-photo"].map((name) => fixture.calls.filter(([entry]) => entry === name).length), [1, 1, 1]);
    const counts = new Map(["screenshot", "evidence-record", "calendar-create", "telegram-message", "telegram-photo"].map((name) => [name, fixture.calls.filter(([entry]) => entry === name).length]));
    const second = await fixture.chain.completeEvidence({ provider: "meetup", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(second.completion_disposition, "reused");
    assert.equal(second.bundle_id, first.bundle_id);
    for (const [name, count] of counts) assert.equal(fixture.calls.filter(([entry]) => entry === name).length, count, name);
  } finally { fixture.cleanup(); }
});

test("Meetup canonical identity, exact registered state, and current page URL fail closed before downstream effects", async () => {
  const invalidUrls = [
    "https://meetup.com/tokyo-builders/events/101/", "http://www.meetup.com/tokyo-builders/events/101/",
    "https://www.meetup.com/Tokyo-builders/events/101/", "https://www.meetup.com/tokyo_builders/events/101/",
    "https://www.meetup.com/tokyo-builders/events/101", "https://www.meetup.com/tokyo-builders/events/101/?source=test",
    "https://www.meetup.com/tokyo-builders/events/101/#details", "https://user:pass@www.meetup.com/tokyo-builders/events/101/",
    "https://www.meetup.com:443/tokyo-builders/events/101/", "https://www.meetup.com/ja-JP/tokyo-builders/events/101/",
    "https://www.meetup.com/tokyo-builders/events/102/", "https://www.meetup.example/tokyo-builders/events/101/",
  ];
  const cases = invalidUrls.map((canonical_url) => ({ candidate: { canonical_url } }));
  cases.push({ candidate: { event_ref: "meetup-event://event/0" } }, { candidate: { event_ref: "meetup-event://event/102" } }, { status: "pending" }, { status: "absent" });
  for (const pageUrl of ["https://www.meetup.com/tokyo-builders/events/102/", "https://www.meetup.com/tokyo-builders/events/101/?next=other", "about:blank"]) cases.push({ pageUrl });
  for (const input of cases) {
    const fixture = meetupFixture(input);
    try {
      await assert.rejects(fixture.chain.completeEvidence({ provider: "meetup", candidate: fixture.candidate, page: fixture.page, providerState: { status: input.status || "registered" } }));
      assertMeetupNoDownstream(fixture, input.pageUrl || input.candidate?.canonical_url || input.status || input.candidate?.event_ref);
    } finally { fixture.cleanup(); }
  }
});

test("Meetup valid checkpoints require receipt and artifact readback before reuse", async () => {
  for (const corruption of ["receipt", "artifact"]) {
    const fixture = meetupFixture();
    try {
      const first = await fixture.chain.completeEvidence({ provider: "meetup", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
      const before = fixture.calls.length;
      if (corruption === "receipt") fixture.evidenceStore.readExternalReceipt = async () => ({ kind: "provider_response", provider_id: first.provider_receipt_ref.split("/").at(-1), observed_at: "2026-08-11T08:30:00.000Z", event_ref: "meetup-event://event/102", artifact_sha256: fixture.pngSha });
      if (corruption === "artifact") fixture.evidenceStore.readArtifact = async () => Buffer.concat([fixture.png, Buffer.from("tamper")]);
      await assert.rejects(fixture.chain.completeEvidence({ provider: "meetup", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assertMeetupNoDownstream({ calls: fixture.calls.slice(before) }, corruption);
    } finally { fixture.cleanup(); }
  }
});

test("Meetup initial record requires receipt and artifact readback before downstream effects", async () => {
  for (const corruption of ["receipt", "receipt-missing", "artifact"]) {
    const fixture = meetupFixture();
    try {
      if (corruption === "receipt") {
        fixture.evidenceStore.readExternalReceipt = async () => ({
          kind: "provider_response", provider_id: "f".repeat(64), observed_at: "2026-08-11T08:30:00.000Z",
          event_ref: "meetup-event://event/102", artifact_sha256: fixture.pngSha,
        });
      } else if (corruption === "receipt-missing") {
        fixture.evidenceStore.readExternalReceipt = async () => ({ kind: "provider_response", provider_id: "f".repeat(64) });
      } else {
        fixture.evidenceStore.readArtifact = async () => Buffer.concat([fixture.png, Buffer.from("tamper")]);
      }
      await assert.rejects(fixture.chain.completeEvidence({ provider: "meetup", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assert.equal(fixture.calls.filter(([name]) => ["calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(name)).length, 0, corruption);
      assert.equal(bundleFiles(fixture.stateDir).length, 0, corruption);
    } finally { fixture.cleanup(); }
  }
});

function doorkeeperCandidate(extra = {}) {
  return {
    provider: "doorkeeper", event_ref: "doorkeeper-event://event/101",
    canonical_url: "https://tokyo-builders.doorkeeper.jp/events/101",
    title: "Community Event", starts_at: "2026-08-13T10:00:00.000Z", ends_at: "2026-08-13T11:00:00.000Z",
    venue_name: "Tokyo", ...extra,
  };
}

function doorkeeperFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-doorkeeper-evidence-"));
  const png = Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 19)]);
  const calls = [];
  const receipt = { id: "google-doorkeeper-1", htmlLink: "https://www.google.com/calendar/event?eid=doorkeeper-one" };
  let reads = 0;
  const calendar = {
    async findConnectorEvents(input) { calls.push(["calendar-read", input]); return reads++ === 0 ? [] : [receipt]; },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return receipt; },
  };
  const chain = createMinimalEvidenceChain({
    stateDir, tenantId: "doorkeeper-test", calendar, calendarId: "primary", telegramTarget: "test-target",
    now: () => new Date("2026-08-12T08:30:00.000Z"),
    sendMessage: async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9601 }; },
    sendPhoto: async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9602 }; },
  });
  const pageUrl = { value: options.pageUrl || "https://tokyo-builders.doorkeeper.jp/events/101" };
  const page = {
    async setContent() { calls.push(["set-content"]); throw new Error("Doorkeeper page replacement forbidden"); },
    async goto() { calls.push(["goto"]); throw new Error("Doorkeeper evidence navigation forbidden"); },
    async evaluate() { calls.push(["evaluate"]); throw new Error("Doorkeeper receipt render forbidden"); },
    url() { calls.push(["url"]); return pageUrl.value; },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
  return { stateDir, calls, chain, page, candidate: doorkeeperCandidate(options.candidate), cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

test("Doorkeeper captures the registered page and reuses immutable evidence without navigation or replacement", async () => {
  const fixture = doorkeeperFixture();
  try {
    const input = { provider: "doorkeeper", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } };
    const first = await fixture.chain.completeEvidence(input);
    assert.equal(first.provider, "doorkeeper");
    assert.equal(first.completion_disposition, "created");
    assert.match(first.provider_receipt_ref, /^provider-receipt:\/\/doorkeeper\/[0-9a-f]{64}$/);
    assert.deepEqual(fixture.calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
    assert.equal(fixture.calls.filter(([name]) => ["set-content", "goto", "evaluate"].includes(name)).length, 0);
    const effects = new Map(["screenshot", "calendar-create", "telegram-message", "telegram-photo"].map((name) => [name, fixture.calls.filter(([entry]) => entry === name).length]));
    const second = await fixture.chain.completeEvidence(input);
    assert.equal(second.completion_disposition, "reused");
    assert.equal(second.bundle_id, first.bundle_id);
    for (const [name, count] of effects) assert.equal(fixture.calls.filter(([entry]) => entry === name).length, count, name);
  } finally { fixture.cleanup(); }
});

test("Doorkeeper identity, registered state, and current page URL fail closed before downstream effects", async () => {
  const cases = [
    { candidate: { event_ref: "doorkeeper-event://event/0" } },
    { candidate: { event_ref: "doorkeeper-event://event/102" } },
    { candidate: { canonical_url: "https://www.doorkeeper.jp/events/101" } },
    { candidate: { canonical_url: "https://Tokyo-builders.doorkeeper.jp/events/101" } },
    { candidate: { canonical_url: "https://tokyo-builders.doorkeeper.jp/events/101/" } },
    { candidate: { canonical_url: "https://tokyo-builders.doorkeeper.jp/events/101?x=1" } },
    { candidate: { canonical_url: "https://tokyo-builders.doorkeeper.jp/events/102" } },
    { status: "pending" }, { status: "absent" },
    { pageUrl: "https://tokyo-builders.doorkeeper.jp/events/102" }, { pageUrl: "about:blank" },
  ];
  for (const value of cases) {
    const fixture = doorkeeperFixture(value);
    try {
      await assert.rejects(fixture.chain.completeEvidence({ provider: "doorkeeper", candidate: fixture.candidate, page: fixture.page, providerState: { status: value.status || "registered" } }));
      assert.equal(fixture.calls.filter(([name]) => ["screenshot", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(name)).length, 0);
    } finally { fixture.cleanup(); }
  }
});

function eventbriteCandidate(extra = {}) {
  return { provider: "eventbrite", event_ref: "eventbrite-event://event/1997468673573", canonical_url: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573", title: "Eventbrite Community Event", starts_at: "2026-08-13T10:00:00.000Z", ends_at: "2026-08-13T11:00:00.000Z", venue_name: "Tokyo", ...extra };
}

function eventbriteFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-eventbrite-evidence-")); const candidate = eventbriteCandidate(options.candidate);
  const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 21)]); const pngSha = createHash("sha256").update(png).digest("hex");
  const calls = []; const calendarReceipt = { id: "google-eventbrite-1", htmlLink: "https://www.google.com/calendar/event?eid=eventbrite-one" }; let calendarReads = 0; let recordedAt;
  const calendar = options.calendar || {
    async findConnectorEvents(input) { calls.push(["calendar-read", input]); return calendarReads++ === 0 ? [] : [calendarReceipt]; },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return calendarReceipt; },
  };
  const evidenceStore = options.evidenceStore || {
    async record(input) {
      calls.push(["evidence-record", input]);
      recordedAt = input.observedAt;
      const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex");
      return { external_receipt_ref: `provider-receipt://eventbrite/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` };
    },
    async readExternalReceipt(tenant, ref) {
      calls.push(["evidence-read-receipt", tenant, ref]);
      return { kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: recordedAt, event_ref: candidate.event_ref, artifact_sha256: pngSha };
    },
    async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return png; },
  };
  const chain = createMinimalEvidenceChain({
    stateDir, tenantId: "eventbrite-test", calendar, calendarId: "primary", telegramTarget: "test-target",
    eventbriteEvidenceStore: evidenceStore, now: () => new Date("2026-08-12T08:30:00.000Z"),
    sendMessage: options.sendMessage || (async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9701 }; }),
    sendPhoto: options.sendPhoto || (async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9702 }; }),
  });
  const pageUrl = { value: options.pageUrl || candidate.canonical_url };
  const page = {
    async setContent() { calls.push(["set-content"]); throw new Error("Eventbrite page replacement forbidden"); },
    async goto() { calls.push(["goto"]); throw new Error("Eventbrite evidence navigation forbidden"); },
    async evaluate() { calls.push(["evaluate"]); throw new Error("Eventbrite receipt render forbidden"); },
    url() { calls.push(["url"]); return pageUrl.value; },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
  return { stateDir, candidate, png, pngSha, calls, chain, page, pageUrl, evidenceStore, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

function assertEventbriteNoDownstream(fixture, label) {
  for (const name of ["screenshot", "evidence-record", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
    assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, `${label}:${name}`);
  }
}

test("Eventbrite slug path captures the registered parent page, reads evidence, and reuses the exact applied bundle", async () => {
  const fixture = eventbriteFixture();
  try {
    const input = { provider: "eventbrite", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } };
    const first = await fixture.chain.completeEvidence(input);
    assert.equal(first.provider, "eventbrite");
    assert.equal(first.completion_disposition, "created");
    assert.match(first.provider_receipt_ref, /^provider-receipt:\/\/eventbrite\/[0-9a-f]{64}$/);
    assert.deepEqual(fixture.calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
    assert.equal(fixture.calls.filter(([name]) => ["set-content", "goto", "evaluate"].includes(name)).length, 0);
    const receiptRead = fixture.calls.findIndex(([name]) => name === "evidence-read-receipt"); const artifactRead = fixture.calls.findIndex(([name]) => name === "evidence-read-artifact");
    const firstCalendarRead = fixture.calls.findIndex(([name]) => name === "calendar-read");
    assert.ok(receiptRead >= 0 && artifactRead > receiptRead && firstCalendarRead > artifactRead);
    const effects = new Map(["screenshot", "evidence-record", "calendar-create", "telegram-message", "telegram-photo"].map((name) => [name, fixture.calls.filter(([entry]) => entry === name).length]));
    const second = await fixture.chain.completeEvidence(input);
    assert.equal(second.completion_disposition, "reused");
    assert.equal(second.bundle_id, first.bundle_id);
    for (const [name, count] of effects) assert.equal(fixture.calls.filter(([entry]) => entry === name).length, count, name);
  } finally { fixture.cleanup(); }
});

test("Eventbrite direct-ID path is accepted with the same registered-page evidence contract", async () => {
  const fixture = eventbriteFixture({ candidate: { event_ref: "eventbrite-event://event/1997468673574", canonical_url: "https://www.eventbrite.com/e/1997468673574" } });
  try {
    const bundle = await fixture.chain.completeEvidence({ provider: "eventbrite", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } });
    assert.equal(bundle.completion_disposition, "created");
    assert.equal(fixture.calls.find(([name]) => name === "calendar-create")[1].canonicalUrl, fixture.candidate.canonical_url);
    assert.equal(fixture.calls.filter(([name]) => ["set-content", "goto", "evaluate"].includes(name)).length, 0);
  } finally { fixture.cleanup(); }
});

test("Eventbrite identity, exact registered state, and current page URL fail closed before downstream effects", async () => {
  const invalidUrls = [
    "http://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573",
    "https://eventbrite.com/e/tokyo-free-event-tickets-1997468673573",
    "https://user:pass@www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573",
    "https://www.eventbrite.com:443/e/tokyo-free-event-tickets-1997468673573",
    "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573/",
    "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573?source=test",
    "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573#details",
    "https://www.eventbrite.com/e/tokyo-free-event-tickets",
    "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673573/extra",
    "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673574",
  ];
  const cases = invalidUrls.map((canonical_url) => ({ candidate: { canonical_url } }));
  cases.push(
    { candidate: { event_ref: "eventbrite-event://event/0" } },
    { candidate: { event_ref: "eventbrite-event://event/1997468673574" } }, { status: "pending" }, { status: "absent" },
    { candidate: { canonical_url: "https://WWW.eventbrite.com/e/tokyo-free-event-tickets-1997468673573" }, pageUrl: "https://WWW.eventbrite.com/e/tokyo-free-event-tickets-1997468673573" },
    { pageUrl: "https://www.eventbrite.com/e/tokyo-free-event-tickets-1997468673574" },
    { pageUrl: "about:blank" },
  );
  for (const value of cases) {
    const fixture = eventbriteFixture(value);
    try {
      await assert.rejects(fixture.chain.completeEvidence({ provider: "eventbrite", candidate: fixture.candidate, page: fixture.page, providerState: { status: value.status || "registered" } }));
      assertEventbriteNoDownstream(fixture, value.pageUrl || value.candidate?.canonical_url || value.status || value.candidate?.event_ref);
    } finally { fixture.cleanup(); }
  }
});

function techplayCandidate(extra = {}) {
  return {
    provider: "techplay", event_ref: "techplay-event://event/999190",
    canonical_url: "https://techplay.jp/event/999190", title: "TECH PLAY Community Event",
    starts_at: "2026-08-13T10:00:00.000Z", ends_at: "2026-08-13T11:00:00.000Z", venue_name: "Tokyo", ...extra,
  };
}

function techplayFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-techplay-evidence-"));
  const candidate = techplayCandidate(options.candidate); const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 22)]);
  const pngSha = createHash("sha256").update(png).digest("hex"); const calls = []; const calendarReceipt = { id: "google-techplay-1", htmlLink: "https://www.google.com/calendar/event?eid=techplay-one" }; let calendarReads = 0; let recordedAt;
  const calendar = options.calendar || {
    async findConnectorEvents(input) { calls.push(["calendar-read", input]); return calendarReads++ === 0 ? [] : [calendarReceipt]; },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return calendarReceipt; },
  };
  const evidenceStore = options.evidenceStore || {
    async record(input) { calls.push(["evidence-record", input]); assert.equal(input.screenshot, png); recordedAt = input.observedAt; const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex"); return { external_receipt_ref: `provider-receipt://techplay/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` }; },
    async readExternalReceipt(tenant, ref) { calls.push(["evidence-read-receipt", tenant, ref]); return { kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: recordedAt, event_ref: candidate.event_ref, artifact_sha256: pngSha }; },
    async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return png; },
  };
  const chain = createMinimalEvidenceChain({
    stateDir, tenantId: "techplay-test", calendar, calendarId: "primary", telegramTarget: "test-target", techplayEvidenceStore: evidenceStore,
    now: () => new Date("2026-08-12T08:30:00.000Z"),
    sendMessage: options.sendMessage || (async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9801 }; }),
    sendPhoto: options.sendPhoto || (async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9802 }; }),
  });
  const pageUrl = { value: options.pageUrl || candidate.canonical_url };
  const page = {
    async setContent() { calls.push(["set-content"]); throw new Error("TECH PLAY page replacement forbidden"); },
    async goto() { calls.push(["goto"]); throw new Error("TECH PLAY evidence navigation forbidden"); },
    async evaluate() { calls.push(["evaluate"]); throw new Error("TECH PLAY receipt render forbidden"); },
    url() { calls.push(["url"]); return pageUrl.value; },
    async screenshot(input) { calls.push(["screenshot", input]); return png; },
  };
  return { stateDir, candidate, png, pngSha, calls, chain, page, pageUrl, evidenceStore, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

function assertTechPlayNoDownstream(fixture, label) {
  for (const name of ["screenshot", "evidence-record", "evidence-read-receipt", "evidence-read-artifact", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
    assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, `${label}:${name}`);
  }
}

test("TECH PLAY registered parent captures one evidence bundle, reads it before Calendar, and reuses without duplicate effects", async () => {
  const fixture = techplayFixture();
  try {
    const input = { provider: "techplay", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } };
    const first = await fixture.chain.completeEvidence(input);
    assert.equal(first.provider, "techplay"); assert.equal(first.completion_disposition, "created");
    assert.match(first.event_ref, /^techplay-event:\/\/event\/[1-9][0-9]*$/); assert.match(first.provider_receipt_ref, /^provider-receipt:\/\/techplay\/[0-9a-f]{64}$/);
    assert.equal(fixture.calls.filter(([name]) => name === "screenshot").length, 1); assert.deepEqual(fixture.calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
    assert.equal(fixture.calls.filter(([name]) => ["set-content", "goto", "evaluate"].includes(name)).length, 0);
    const receiptRead = fixture.calls.findIndex(([name]) => name === "evidence-read-receipt"); const artifactRead = fixture.calls.findIndex(([name]) => name === "evidence-read-artifact"); const calendarRead = fixture.calls.findIndex(([name]) => name === "calendar-read");
    assert.ok(receiptRead >= 0 && artifactRead > receiptRead && calendarRead > artifactRead);
    assert.equal(fixture.calls.find(([name]) => name === "calendar-create")[1].canonicalUrl, fixture.candidate.canonical_url);
    assert.equal(fixture.calls.filter(([name]) => name === "telegram-message").length, 1); assert.equal(fixture.calls.filter(([name]) => name === "telegram-photo").length, 1);
    assert.match(fixture.calls.find(([name]) => name === "telegram-message")[1], /provider: techplay/);
    assert.equal(fixture.calls.find(([name]) => name === "telegram-photo")[2].caption, "Connector::: techplay / TECH PLAY Community Event / registered");
    assert.equal(fixture.calls.filter(([name]) => name === "evidence-read-receipt").length, 1);
    assert.equal(fixture.calls.filter(([name]) => name === "evidence-read-artifact").length, 1);
    const counts = new Map(["screenshot", "evidence-record", "calendar-create", "telegram-message", "telegram-photo"].map((name) => [name, fixture.calls.filter(([entry]) => entry === name).length]));
    const second = await fixture.chain.completeEvidence(input);
    assert.equal(second.completion_disposition, "reused"); assert.equal(second.bundle_id, first.bundle_id);
    for (const [name, count] of counts) assert.equal(fixture.calls.filter(([entry]) => entry === name).length, count, name);
  } finally { fixture.cleanup(); }
});

test("TECH PLAY event, canonical/page identity, and registered-only state reject before every downstream effect", async () => {
  const cases = [
    { candidate: { event_ref: "techplay-event://event/0" } }, { candidate: { event_ref: "techplay-event://event/999191" } },
    { candidate: { canonical_url: "https://techplay.jp/event/999191" } }, { candidate: { canonical_url: "https://techplay.jp/event/999190/" } },
    { candidate: { canonical_url: "http://techplay.jp/event/999190" } }, { candidate: { canonical_url: "https://www.techplay.jp/event/999190" } },
    { candidate: { canonical_url: "https://techplay.jp/event/999190?source=test" } }, { status: "pending" }, { status: "absent" },
    { pageUrl: "https://techplay.jp/event/999191" }, { pageUrl: "https://techplay.jp/event/999190/" }, { pageUrl: "about:blank" },
  ];
  for (const value of cases) {
    const fixture = techplayFixture(value);
    try { await assert.rejects(fixture.chain.completeEvidence({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, providerState: { status: value.status || "registered" } })); assertTechPlayNoDownstream(fixture, value.pageUrl || value.status || value.candidate?.event_ref || value.candidate?.canonical_url); }
    finally { fixture.cleanup(); }
  }
});

test("TECH PLAY wrong receipt or tampered artifact fails before Calendar and delivery", async () => {
  for (const corruption of ["receipt", "artifact"]) {
    const fixture = techplayFixture();
    try {
      if (corruption === "receipt") fixture.evidenceStore.readExternalReceipt = async () => ({ kind: "provider_response", provider_id: "f".repeat(64), observed_at: "2026-08-12T08:30:00.000Z", event_ref: fixture.candidate.event_ref, artifact_sha256: fixture.pngSha });
      else fixture.evidenceStore.readArtifact = async () => Buffer.concat([fixture.png, Buffer.from("tamper")]);
      await assert.rejects(fixture.chain.completeEvidence({ provider: "techplay", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
      assert.equal(fixture.calls.filter(([name]) => ["calendar-read", "calendar-create", "telegram-message", "telegram-photo"].includes(name)).length, 0, corruption);
    } finally { fixture.cleanup(); }
  }
});

function kokuchproCandidate(extra = {}) {
  return {
    provider: "kokuchpro",
    event_ref: "kokuchpro-event://event/89a92aac6c9a221ec337481b51c1bbef/3847918",
    canonical_url: "https://www.kokuchpro.com/event/89a92aac6c9a221ec337481b51c1bbef/3847918/",
    title: "KokuchPro Community Event",
    starts_at: "2026-08-20T10:00:00.000Z",
    ends_at: "2026-08-20T11:00:00.000Z",
    venue_name: "Tokyo",
    venue: "KokuchPro Hall",
    address: "東京都豊島区池袋1-2-3",
    registration_status: "available",
    ticket_id: "ticket-3847918",
    ticket_price_status: "free",
    ticket_price_minor: 0,
    ...extra,
  };
}

function kokuchproFixture(options = {}) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-kokuchpro-evidence-"));
  const candidate = kokuchproCandidate(options.candidate);
  const png = options.png || Buffer.concat([PNG_SIGNATURE, Buffer.alloc(6_000, 23)]);
  const pngSha = createHash("sha256").update(png).digest("hex");
  const calls = [];
  const calendarReceipt = { id: "google-kokuchpro-1", htmlLink: "https://www.google.com/calendar/event?eid=kokuchpro-one" };
  let calendarReads = 0;
  let recordedAt;
  const calendar = options.calendar || {
    async findConnectorEvents(input) {
      calls.push(["calendar-read", input]);
      return calendarReads++ === 0 ? [] : [calendarReceipt];
    },
    async createConnectorEvent(input) { calls.push(["calendar-create", input]); return calendarReceipt; },
  };
  const evidenceStore = options.evidenceStore || {
    async record(input) {
      calls.push(["evidence-record", input]);
      recordedAt = input.observedAt;
      const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${pngSha}`).digest("hex");
      return { external_receipt_ref: `provider-receipt://kokuchpro/${receiptId}`, artifact_ref: `object://sha256/${pngSha}` };
    },
    async readExternalReceipt(tenant, ref) {
      calls.push(["evidence-read-receipt", tenant, ref]);
      return { kind: "provider_response", provider_id: ref.split("/").at(-1), observed_at: recordedAt, event_ref: candidate.event_ref, artifact_sha256: pngSha };
    },
    async readArtifact(tenant, ref) { calls.push(["evidence-read-artifact", tenant, ref]); return png; },
  };
  const chain = createMinimalEvidenceChain({
    stateDir,
    tenantId: "kokuchpro-test",
    calendar,
    calendarId: "primary",
    telegramTarget: "test-target",
    kokuchproEvidenceStore: evidenceStore,
    now: () => new Date("2026-08-13T08:30:00.000Z"),
    sendMessage: options.sendMessage || (async (message, telegram) => { calls.push(["telegram-message", message, telegram]); return { messageId: 9901 }; }),
    sendPhoto: options.sendPhoto || (async (bytes, telegram) => { calls.push(["telegram-photo", bytes, telegram]); return { messageId: 9902 }; }),
  });
  const pageUrl = { value: options.pageUrl || `${candidate.canonical_url}entry/` };
  const readbackFacts = options.readbackFacts || {
    entry_forms: [],
    password_count: 0,
    login_forms: [],
    detail_headings: [{ text: "申込詳細", visible: true }],
    canonical_links: [{ href: candidate.canonical_url, text: "イベント", visible: true }],
  };
  const page = {
    async setContent() { calls.push(["set-content"]); throw new Error("KokuchPro page replacement forbidden"); },
    async goto() { calls.push(["goto"]); throw new Error("KokuchPro evidence navigation forbidden"); },
    async evaluate(_script, input) { calls.push(["evaluate", input]); if (options.evaluateError) throw new Error("KokuchPro readback failed"); return readbackFacts; },
    url() { calls.push(["url"]); return pageUrl.value; },
    async screenshot(input) { calls.push(["screenshot", input]); if (options.afterScreenshotPageUrl) pageUrl.value = options.afterScreenshotPageUrl; return png; },
  };
  return { stateDir, candidate, png, pngSha, calls, chain, page, pageUrl, evidenceStore, cleanup: () => fs.rmSync(stateDir, { recursive: true, force: true }) };
}

function assertKokuchProNoDownstream(fixture, label) {
  for (const name of ["screenshot", "evidence-record", "evidence-read-receipt", "evidence-read-artifact", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
    assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, `${label}:${name}`);
  }
}

test("KokuchPro constructor rejects an incomplete injected evidence store", () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-minimal-kokuchpro-store-validation-"));
  try {
    assert.throws(() => createMinimalEvidenceChain({
      stateDir,
      tenantId: "kokuchpro-test",
      calendar: { async findConnectorEvents() { return []; }, async createConnectorEvent() { return { id: "id", htmlLink: "https://www.google.com/calendar/event?eid=id" }; } },
      calendarId: "primary",
      telegramTarget: "test-target",
      kokuchproEvidenceStore: {},
    }));
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("KokuchPro registered descendant captures one evidence bundle and reuses it without navigation", async () => {
  const fixture = kokuchproFixture();
  try {
    const input = { provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } };
    const first = await fixture.chain.completeEvidence(input);
    assert.equal(first.provider, "kokuchpro");
    assert.equal(first.completion_disposition, "created");
    assert.match(first.event_ref, /^kokuchpro-event:\/\/event\/[0-9a-f]{32}\/[1-9][0-9]*$/);
    assert.match(first.provider_receipt_ref, /^provider-receipt:\/\/kokuchpro\/[0-9a-f]{64}$/);
    assert.equal(fixture.calls.filter(([name]) => name === "screenshot").length, 1);
    assert.deepEqual(fixture.calls.find(([name]) => name === "screenshot")[1], { type: "png", fullPage: true });
    assert.equal(fixture.calls.filter(([name]) => ["set-content", "goto"].includes(name)).length, 0);
    assert.ok(fixture.calls.some(([name, input]) => name === "evaluate" && input && input.mode === "detail"));
    const receiptRead = fixture.calls.findIndex(([name]) => name === "evidence-read-receipt");
    const artifactRead = fixture.calls.findIndex(([name]) => name === "evidence-read-artifact");
    const calendarRead = fixture.calls.findIndex(([name]) => name === "calendar-read");
    assert.ok(receiptRead >= 0 && artifactRead > receiptRead && calendarRead > artifactRead);
    assert.equal(fixture.calls.find(([name]) => name === "calendar-create")[1].canonicalUrl, fixture.candidate.canonical_url);
    assert.equal(fixture.calls.find(([name]) => name === "calendar-create")[1].location, "東京都豊島区池袋1-2-3");
    assert.equal(fixture.calls.filter(([name]) => name === "telegram-message").length, 1);
    assert.equal(fixture.calls.filter(([name]) => name === "telegram-photo").length, 1);
    const effects = new Map(["screenshot", "evidence-record", "calendar-create", "telegram-message", "telegram-photo"].map((name) => [name, fixture.calls.filter(([entry]) => entry === name).length]));
    const second = await fixture.chain.completeEvidence(input);
    assert.equal(second.completion_disposition, "reused");
    assert.equal(second.bundle_id, first.bundle_id);
    for (const [name, count] of effects) assert.equal(fixture.calls.filter(([entry]) => entry === name).length, count, name);
  } finally { fixture.cleanup(); }
});

test("KokuchPro refuses an unverified registration form before taking evidence", async () => {
  const fixture = kokuchproFixture({ readbackFacts: {
    entry_forms: [{ action: "https://www.kokuchpro.com/event/89a92aac6c9a221ec337481b51c1bbef/3847918/entry/", method: "POST" }],
    password_count: 0,
    login_forms: [],
    detail_headings: [],
    canonical_links: [],
  } });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assertKokuchProNoDownstream(fixture, "unverified-registration-form");
  } finally { fixture.cleanup(); }
});

test("KokuchPro refuses a screenshot when the official page URL changes during capture", async () => {
  const fixture = kokuchproFixture({ afterScreenshotPageUrl: `${kokuchproCandidate().canonical_url}entry/detail/` });
  try {
    await assert.rejects(fixture.chain.completeEvidence({ provider: "kokuchpro", candidate: fixture.candidate, page: fixture.page, providerState: { status: "registered" } }));
    assert.equal(fixture.calls.filter(([name]) => name === "screenshot").length, 1);
    for (const name of ["evidence-record", "evidence-read-receipt", "evidence-read-artifact", "calendar-read", "calendar-create", "telegram-message", "telegram-photo"]) {
      assert.equal(fixture.calls.filter(([callName]) => callName === name).length, 0, `${name}: URL changed during capture`);
    }
    assert.equal(bundleFiles(fixture.stateDir).length, 0);
  } finally { fixture.cleanup(); }
});

test("KokuchPro identity, canonical/page URL, and registered-only state fail closed before downstream effects", async () => {
  const eventKeyMatch = /^kokuchpro-event:\/\/event\/([0-9a-f]{32})(?:\/[1-9][0-9]*)?$/.exec(kokuchproCandidate().event_ref);
  assert.ok(eventKeyMatch);
  const eventKey = eventKeyMatch[1];
  const canonical = `https://www.kokuchpro.com/event/${eventKey}/3847918/`;
  const validFixture = kokuchproFixture();
  try {
    await assert.doesNotReject(validFixture.chain.completeEvidence({ provider: "kokuchpro", candidate: validFixture.candidate, page: validFixture.page, providerState: { status: "registered" } }));
  } finally { validFixture.cleanup(); }
  const invalidUrls = [
    `http://www.kokuchpro.com/event/${eventKey}/3847918/`,
    `https://WWW.kokuchpro.com/event/${eventKey}/3847918/`,
    `https://www.kokuchpro.com:443/event/${eventKey}/3847918/`,
    `https://user:pass@www.kokuchpro.com/event/${eventKey}/3847918/`,
    `https://www.kokuchpro.com/event/${eventKey}/3847918`,
    `${canonical}?source=test`, `${canonical}#details`,
    `https://www.kokuchpro.com/event/${eventKey}/3847919/`,
    `https://www.kokuchpro.com/event/${eventKey}/`,
    "https://www.kokuchpro.com/",
  ];
  const cases = invalidUrls.map((canonical_url) => ({ candidate: { canonical_url } }));
  cases.push(
    { candidate: { event_ref: `kokuchpro-event://event/${eventKey.toUpperCase()}/3847918` } },
    { candidate: { event_ref: `kokuchpro-event://event/${eventKey.slice(0, 31)}/3847918` } },
    { candidate: { event_ref: `kokuchpro-event://event/${eventKey}/0` } },
    { candidate: { event_ref: `kokuchpro-event://event/${eventKey}/03847918` } },
    { status: "pending" }, { status: "absent" },
  );
  for (const pageUrl of [
    `https://www.kokuchpro.com/event/${eventKey.slice(0, 31)}0/3847918/entry/`,
    `https://www.kokuchpro.com/event/${eventKey}/3847918/?next=other`,
    `https://www.kokuchpro.com/event/${eventKey}/3847918/#other`,
    `${canonical}entry/ `,
    `https://www.kokuchpro.com/event/${eventKey}/`, "https://www.kokuchpro.com/", "about:blank",
  ]) cases.push({ pageUrl });
  for (const value of cases) {
    const fixture = kokuchproFixture(value);
    try {
      await assert.rejects(fixture.chain.completeEvidence({ provider: "kokuchpro", candidate: kokuchproCandidate(value.candidate), page: fixture.page, providerState: { status: value.status || "registered" } }));
      assertKokuchProNoDownstream(fixture, value.pageUrl || value.status || value.candidate?.event_ref || value.candidate?.canonical_url);
    } finally { fixture.cleanup(); }
  }
});
