"use strict";

const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createBrowserHarnessAdapter } = require("./connector-browser-harness-adapter.js");
const { createConnectorActionCache } = require("./connector-action-cache.js");
const { validateProviderCandidateRanking } = require("./event-preference-ranking.js");
const { validateEventTalkOpportunity } = require("./event-talk-opportunity.js");
const { runMinimalConnectorWake } = require("./connector-minimal-runner.js");
const {
  createProductionBrowserRail,
  createProductionCalendarReader,
  createMinimalProductionDependencies,
  createProductionProviderRouter,
} = require("./connector-minimal-production.js");

function rankingCandidate(slug, startsAt, overrides = {}) {
  return Object.freeze({
    provider: "connpass",
    event_ref: `connpass-event://event/${slug}`,
    canonical_url: `https://tokyo-builders.connpass.com/event/${slug}/`,
    title: `Tokyo event ${slug}`,
    body: "Public AI event for builders.",
    ...(startsAt == null ? {} : { starts_at: startsAt }),
    ...overrides,
  });
}

test("production factory injects direct raw Telegram Bot API sending for wake reports", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-direct-telegram-"));
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  const timeoutCalls = [];
  const requests = [];
  AbortSignal.timeout = (milliseconds) => {
    timeoutCalls.push(milliseconds);
    return originalTimeout(milliseconds);
  };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { async json() { return { ok: true, result: { message_id: 7001 } }; } };
  };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-direct-telegram-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "123456789",
      telegramToken: "fixture-telegram-token", lumaFormProfilePath: "/private/form-profile.json",
      lunaEvidenceDir: "/private/luna-evidence",
      calendar: {}, calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: {}, providerRouter: { discoverCandidates() {}, runCachedAction() {}, runDirectAction() {}, runAgentFallback() {}, readProviderState() {}, saveRepairedActions() {} },
      evidenceChain: { async completeEvidence() {} },
    });

    assert.deepEqual(await dependencies.reportWake({
      status: "completed_no_effect", safe_reason: "providers_exhausted", consecutive_failure_count: 0,
    }), { telegram_provider_id: "7001" });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.signal instanceof AbortSignal, true);
    assert.deepEqual(timeoutCalls, [20_000]);
    assert.equal(requests[0].url, "https://api.telegram.org/botfixture-telegram-token/sendMessage");
    assert.deepEqual(JSON.parse(requests[0].options.body), {
      chat_id: "123456789",
      text: "Connector::: 今回の新規申込なし\nstatus: completed_no_effect\nsafe reason: providers_exhausted\nconsecutive failures: 0",
      parse_mode: "HTML",
      disable_web_page_preview: true,
    });
    assert.equal(JSON.stringify(dependencies).includes("fixture-telegram-token"), false);
  } finally {
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory injects the direct Telegram adapter for the manual Connpass boundary", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-connpass-direct-telegram-"));
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { async json() { return { ok: true, result: {
      message_id: 7002, chat: { id: 123456789, type: "private" }, date: 1_756_000_000, text: "fixture",
    } }; } };
  };
  const candidate = {
    provider: "connpass",
    event_ref: "connpass-event://event/401002",
    canonical_url: "https://tokyo.connpass.com/event/401002/",
    title: "Tokyo AI Builders LT",
    participation_slot_status: "available",
    lightning_talk_status: "unknown",
    participant_limit: 100,
    accepted_count: 20,
    waiting_count: 0,
    application_deadline_at: null,
    priority_class: "ai",
    preference_reason: "AI buildersとの接点に合います。",
  };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-connpass-direct-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "123456789",
      telegramToken: "fixture-telegram-token", lumaFormProfilePath: "/private/form-profile.json",
      lunaEvidenceDir: "/private/luna-evidence", calendar: {},
      calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: {}, providerRouter: { discoverCandidates() {}, runCachedAction() {}, runDirectAction() {}, runAgentFallback() {}, readProviderState() {}, saveRepairedActions() {} },
      evidenceChain: { async completeEvidence() {} },
    });
    assert.deepEqual(await dependencies.reportConnpassActionBoundary({ candidates: [candidate] }), {
      telegram_provider_id: "7002", completion_disposition: "created",
    });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "https://api.telegram.org/botfixture-telegram-token/sendMessage");
    assert.deepEqual(JSON.parse(requests[0].options.body), {
      chat_id: "123456789",
      text: "Connector::: connpass候補（手動action boundary）\n自動申込: 0件\n\n1. Tokyo AI Builders LT\n優先度: ai\n理由: AI buildersとの接点に合います。\n参加枠: available / 参加 20人 / 定員 100人\nLT: unknown / 補欠: 0人\n締切: provider未提供\nhttps://tokyo.connpass.com/event/401002/",
      parse_mode: "HTML",
      disable_web_page_preview: true,
    });
    const claimFile = path.join(stateDir, "connpass-action-boundary-send-claims.jsonl");
    const deliveryFile = path.join(stateDir, "connpass-action-boundary-deliveries.jsonl");
    assert.equal(fs.readFileSync(claimFile, "utf8").trim().split("\n").length, 1);
    assert.equal(fs.readFileSync(deliveryFile, "utf8").trim().split("\n").length, 1);
    assert.equal(fs.existsSync(path.join(stateDir, "connpass-action-boundary-uncertain.jsonl")), false);
  } finally {
    globalThis.fetch = originalFetch;
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory injects direct Telegram message and photo adapters into evidence", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-evidence-direct-telegram-"));
  const originalFetch = globalThis.fetch;
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(6_000, 3)]);
  const artifactSha = createHash("sha256").update(png).digest("hex");
  const requests = [];
  const calendarReceipt = { id: "google-evidence-direct", htmlLink: "https://www.google.com/calendar/event?eid=evidence-direct" };
  let calendarReads = 0;
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    const messageId = requests.length === 1 ? 9011 : 9012;
    return { async json() { return { ok: true, result: {
      message_id: messageId, chat: { id: 123456789, type: "private" }, date: 1_756_000_000, text: "fixture",
    } }; } };
  };
  const evidenceStore = {
    async record(input) {
      const receiptId = createHash("sha256").update(`${input.tenantId}\n${input.eventRef}\n${input.observedAt}\n${artifactSha}`).digest("hex");
      return { external_receipt_ref: `provider-receipt://luma/${receiptId}`, artifact_ref: `object://sha256/${artifactSha}` };
    },
    async readExternalReceipt() { throw new Error("not needed"); },
    async readArtifact() { return png; },
  };
  const candidate = {
    provider: "luma", event_ref: "luma-event://event/evidence-direct", canonical_url: "https://luma.com/evidence-direct",
    title: "Evidence Direct", starts_at: "2026-08-10T10:00:00.000Z", ends_at: "2026-08-10T11:00:00.000Z", venue_name: "Tokyo",
  };
  const page = {
    async goto() {}, url() { return "about:blank"; }, async evaluate() { return true; }, async screenshot() { return png; },
  };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-evidence-direct-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "123456789",
      telegramToken: "fixture-telegram-token", lumaFormProfilePath: "/private/form-profile.json",
      lunaEvidenceDir: "/private/luna-evidence", calendar: {
        async findConnectorEvents() { return calendarReads++ === 0 ? [] : [calendarReceipt]; },
        async createConnectorEvent() { return calendarReceipt; },
      },
      calendarReader: { async readCalendarGaps() { return []; } }, browserRail: {}, evidenceStore,
      providerRouter: { discoverCandidates() {}, runCachedAction() {}, runDirectAction() {}, runAgentFallback() {}, readProviderState() {}, saveRepairedActions() {} },
    });
    const bundle = await dependencies.completeEvidence({ provider: "luma", candidate, page, providerState: { status: "registered" } });
    assert.deepEqual([bundle.telegram_message_provider_id, bundle.telegram_photo_provider_id], ["9011", "9012"]);
    assert.deepEqual(requests.map(({ url }) => url), [
      "https://api.telegram.org/botfixture-telegram-token/sendMessage",
      "https://api.telegram.org/botfixture-telegram-token/sendPhoto",
    ]);
    assert.equal(fs.readdirSync(path.join(stateDir, "evidence", "delivery-claims")).filter((name) => name.endsWith(".claim")).length, 2);
    assert.equal(JSON.stringify(dependencies).includes("fixture-telegram-token"), false);
  } finally {
    globalThis.fetch = originalFetch;
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory exposes the complete minimal wake dependency contract", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-deps-"));
  try {
    const browserRail = Object.freeze({ open() {}, navigate() {}, close() {} });
    const calendarReader = Object.freeze({ async readCalendarGaps() { return []; } });
    const providerRouter = Object.freeze({
      discoverCandidates() {}, runCachedAction() {}, runDirectAction() {}, runAgentFallback() {},
      readProviderState() {}, saveRepairedActions() {},
    });
    const evidenceChain = Object.freeze({ completeEvidence() {} });
    const operations = Object.freeze({ reportWake() {}, recordAction() {} });
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo",
      stateDir,
      wakeId: "wake-production-deps-1",
      calendarAccount: "private-account",
      gogKeyring: "private-keyring",
      telegramTarget: "private-target",
      tenantId: "dais-local",
      calendarId: "primary",
      lumaFormProfilePath: "/private/form-profile.json",
      lunaEvidenceDir: "/private/luna-evidence",
      browserRail,
      calendarReader,
      providerRouter,
      evidenceChain,
      operations,
      now: () => new Date("2026-08-07T08:30:00.000Z"),
    });

    assert.equal(dependencies.browserRail, browserRail);
    assert.deepEqual(Object.keys(dependencies).sort(), [
      "browserRail", "completeEvidence", "completeTalkEvidence", "discoverCandidates", "now", "readCalendarGaps",
      "readProviderState", "recordAction", "reportConnpassActionBoundary", "reportWake", "runAgentFallback", "runCachedAction",
      "runDirectAction", "runTalkApplication", "saveRepairedActions",
    ]);
    assert.deepEqual(await dependencies.readCalendarGaps(), await calendarReader.readCalendarGaps());
    assert.equal(dependencies.discoverCandidates, providerRouter.discoverCandidates);
    assert.equal(dependencies.completeEvidence, evidenceChain.completeEvidence);
    assert.equal(dependencies.reportWake, operations.reportWake);
    assert.equal(dependencies.now(), "2026-08-07T08:30:00.000Z");
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory installs the Connpass workflow into the default router", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-connpass-"));
  const candidate = { provider: "connpass", event_ref: "connpass-event://event/401001", canonical_url: "https://tokyo.connpass.com/event/401001/" };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() {}, async readProviderState() { return { status: "absent" }; } };
  const connpassWorkflow = { ...emptyWorkflow, async discoverCandidates() { return [candidate]; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-connpass-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      browserRail: { open() {}, navigate() {}, close() {} },
      calendarReader: { async readCalendarGaps() { return []; } },
      lumaWorkflow: emptyWorkflow, connpassWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} },
      browserHarness: { async runFallback() {}, async performAction() {} },
      evidenceChain: { async completeEvidence() {} },
      operations: { async reportWake() {}, async recordAction() {} },
    });
    assert.deepEqual(await dependencies.discoverCandidates("connpass", [], {}), [candidate]);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory exposes the manual Connpass boundary only while automated submit lacks permission", () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-connpass-boundary-mode-"));
  const workflow = { async discoverCandidates() { return []; }, async runDirectAction() {}, async readProviderState() { return { status: "absent" }; } };
  const common = {
    repoRoot: "/private/repo", stateDir, wakeId: "wake-connpass-boundary-mode",
    calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
    lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
    browserRail: { open() {}, navigate() {}, close() {} }, calendarReader: { async readCalendarGaps() { return []; } },
    providerRouter: { discoverCandidates() {}, runCachedAction() {}, runDirectAction() {}, runAgentFallback() {}, readProviderState() {}, saveRepairedActions() {} },
    lumaWorkflow: workflow, connpassWorkflow: workflow,
    evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} },
  };
  try {
    assert.equal(typeof createMinimalProductionDependencies(common).reportConnpassActionBoundary, "function");
    assert.equal(createMinimalProductionDependencies({ ...common, connpassAutomatedSubmitAllowed: true }).reportConnpassActionBoundary, undefined);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory persists one safe audit for its default Gemini ranking", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-ranking-audit-"));
  const audits = [];
  const originalFetch = globalThis.fetch;
  const ranked = {
    provider: "luma", event_ref: "luma-event://event/ranking-audit", canonical_url: "https://luma.com/ranking-audit",
    title: "Tokyo AI Builders", body: "AI engineering event",
  };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() {}, async readProviderState() { return { status: "absent" }; } };
  try {
    globalThis.fetch = async (_url, request) => {
      const prompt = JSON.parse(request.body).contents[0].parts[0].text;
      const rows = JSON.parse(prompt.match(/EVENT_DATA_START\n([\s\S]+)\nEVENT_DATA_END/)[1]);
      return { ok: true, async json() { return { candidates: [{ content: { parts: [{ text: JSON.stringify({
        ranked_events: rows.map((row) => ({ event_ref: row.event_ref, priority_class: "ai", preference_fit: "strong", preference_reason: "Direct AI fit." })),
      }) }] } }] }; } };
    };
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-ranking-audit",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      eventPreferences: "Tokyo AI events", geminiApiKey: "fixture-key",
      browserRail: { open() {}, navigate() {}, close() {} }, calendarReader: { async readCalendarGaps() { return []; } },
      lumaWorkflow: { ...emptyWorkflow, async discoverCandidates() { return [ranked]; } }, connpassWorkflow: emptyWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {}, async performAction() {} },
      evidenceChain: { async completeEvidence() {} },
      operations: { async reportWake() {}, async recordAction() {}, async recordRankingAudit(value) { audits.push(value); } },
    });
    assert.equal((await dependencies.discoverCandidates("luma", [], {})).length, 1);
    assert.equal(audits.length, 1);
    assert.deepEqual(Object.keys(audits[0]), [
      "schema_version", "request_count", "retry_count", "bisect_count",
      "total_request_ms", "max_request_ms", "elapsed_ms",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("production provider router ranks only twelve candidates round-robin across Tokyo dates and retains every reconciliation candidate", async () => {
  const reconcile = [
    rankingCandidate("reconcile-one", null, { registration_status: "registered" }),
    rankingCandidate("reconcile-two", "not-a-date", { rsvp_status: "registered" }),
  ];
  const august = Array.from({ length: 13 }, (_, index) => rankingCandidate(
    `august-${index}`, "2026-08-31T09:00:00.000+09:00",
  ));
  const september = Array.from({ length: 3 }, (_, index) => rankingCandidate(
    `september-${index}`, "2026-09-01T09:00:00.000+09:00",
  ));
  const candidates = [...reconcile, ...august, ...september];
  const rankingInputs = [];
  const emptyWorkflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() {},
    async readProviderState() { return { status: "absent" }; },
  };
  const connpassWorkflow = { ...emptyWorkflow, async discoverCandidates() { return candidates; } };
  const router = createProductionProviderRouter({
    lumaWorkflow: emptyWorkflow,
    connpassWorkflow,
    eventPreferences: "Tokyo AI events",
    async rankCandidates(input) {
      rankingInputs.push(input.candidates);
      return validateProviderCandidateRanking({ ranked_events: input.candidates.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "strong",
        preference_reason: "Direct AI fit.",
      })) }, input);
    },
    actionCache: { async replay() {}, async saveVerifiedRepair() {} },
    browserHarness: { async runFallback() {} },
    async performAction() {},
  });

  const result = await router.discoverCandidates("connpass", [], {});

  assert.equal(rankingInputs.length, 1);
  assert.equal(rankingInputs[0].length, 12);
  assert.deepEqual(rankingInputs[0].slice(0, 6).map((candidate) => candidate.event_ref), [
    "connpass-event://event/august-0",
    "connpass-event://event/september-0",
    "connpass-event://event/august-1",
    "connpass-event://event/september-1",
    "connpass-event://event/august-2",
    "connpass-event://event/september-2",
  ]);
  assert.equal(rankingInputs[0].some((candidate) => candidate.event_ref.endsWith("august-12")), false);
  assert.deepEqual(result.slice(0, 2).map((candidate) => candidate.event_ref), reconcile.map((candidate) => candidate.event_ref));
  assert.equal(result.slice(2).length, 12);
});

test("production provider router samples the full candidate window instead of starving later weeks", async () => {
  const candidates = Array.from({ length: 28 }, (_, index) => rankingCandidate(
    `day-${index + 1}`,
    new Date(Date.UTC(2026, 8, index + 1, 0)).toISOString(),
  ));
  const rankingInputs = [];
  const emptyWorkflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() {},
    async readProviderState() { return { status: "absent" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: emptyWorkflow,
    connpassWorkflow: { ...emptyWorkflow, async discoverCandidates() { return candidates; } },
    eventPreferences: "Tokyo AI events",
    async rankCandidates(input) {
      rankingInputs.push(input.candidates);
      return validateProviderCandidateRanking({ ranked_events: input.candidates.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "strong",
        preference_reason: "Direct AI fit.",
      })) }, input);
    },
    actionCache: { async replay() {}, async saveVerifiedRepair() {} },
    browserHarness: { async runFallback() {} },
    async performAction() {},
  });

  await router.discoverCandidates("connpass", [], {});
  const selected = rankingInputs[0];
  assert.equal(selected.length, 12);
  assert.equal(selected[0].event_ref, "connpass-event://event/day-1");
  assert.equal(selected.at(-1).event_ref, "connpass-event://event/day-28");
  assert.ok(selected.some((candidate) => candidate.event_ref === "connpass-event://event/day-15"));
});

test("production provider router places invalid and missing starts_at candidates at the bounded selection tail", async () => {
  const dated = [
    ...Array.from({ length: 5 }, (_, index) => rankingCandidate(`dated-august-${index}`, "2026-08-31T09:00:00.000+09:00")),
    ...Array.from({ length: 3 }, (_, index) => rankingCandidate(`dated-september-${index}`, "2026-09-01T09:00:00.000+09:00")),
  ];
  const late = rankingCandidate("later-date", "2026-09-02T09:00:00.000+09:00");
  const invalid = [
    rankingCandidate("missing-date", null),
    rankingCandidate("invalid-date", "not-a-date"),
    rankingCandidate("empty-date", ""),
    rankingCandidate("second-invalid-date", "still-not-a-date"),
    rankingCandidate("third-invalid-date", "also-not-a-date"),
  ];
  const rankingInputs = [];
  const emptyWorkflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() {},
    async readProviderState() { return { status: "absent" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: emptyWorkflow,
    connpassWorkflow: { ...emptyWorkflow, async discoverCandidates() { return [...dated, late, ...invalid]; } },
    eventPreferences: "Tokyo AI events",
    async rankCandidates(input) {
      rankingInputs.push(input.candidates);
      return validateProviderCandidateRanking({ ranked_events: input.candidates.map((candidate) => ({
        event_ref: candidate.event_ref,
        priority_class: "ai",
        preference_fit: "strong",
        preference_reason: "Direct AI fit.",
      })) }, input);
    },
    actionCache: { async replay() {}, async saveVerifiedRepair() {} },
    browserHarness: { async runFallback() {} },
    async performAction() {},
  });

  await router.discoverCandidates("connpass", [], {});
  assert.equal(rankingInputs[0].length, 12);
  assert.deepEqual(rankingInputs[0].slice(-3).map((candidate) => candidate.event_ref), invalid.slice(0, 3).map((candidate) => candidate.event_ref));
  assert.equal(rankingInputs[0].some((candidate) => candidate.event_ref === late.event_ref), true);
});

// Fake Connpass join page, shaped exactly like joinFlowFixture in
// connpass-browser-provider.test.js (sole free tier, one required 氏名
// question) — reused here at the wiring layer instead of stubbing
// connpassWorkflow, so the factory's own readAttendeeName closure actually
// runs end to end, the same way the Peatix harness test above
// ("...composes the default Peatix Harness with the attendee profile")
// drives its real default workflow through a fake page rather than a stub.
function connpassNameQuestionnairePageFixture({ canonicalUrl, nameField }) {
  const joinLink = { first() { return this; }, async count() { return 1; }, async isVisible() { return true; }, async click() {} };
  const radioLocator = {
    async count() { return 1; },
    async evaluateAll() { return [{ disabled: false, label: "参加 無料 先着順 5/10人" }]; },
    nth() { return { async check() {} }; },
  };
  const confirmLocator = {
    async count() { return 1; }, async isVisible() { return true; },
    async innerText() { return "申し込みを確定する"; }, async click() {},
  };
  const nameQuestionGroup = {
    querySelector(selector) {
      if (selector === 'input[name="participation_type"]') return null;
      if (selector === ":scope > .question") return { textContent: "必須 氏名" };
      return null;
    },
    querySelectorAll(selector) { return selector === "input,textarea,select" ? [nameField] : []; },
  };
  const questionnaireLocator = { async evaluateAll(callback, arg) { return callback([nameQuestionGroup], arg); } };
  const stateQueue = [{ state: "absent" }, { state: "registered" }];
  return {
    url() { return canonicalUrl; },
    async goto() {},
    getByRole() { return joinLink; },
    locator(selector) {
      if (selector === 'input[name="participation_type"]') return radioLocator;
      if (selector === "button#FreeButton") return confirmLocator;
      if (selector === ".question_list") return questionnaireLocator;
      throw new Error(`unexpected selector ${selector}`);
    },
    async waitForTimeout() {},
    async evaluate() { return stateQueue.shift(); },
  };
}

// Placeholder name/email only — never Dais's real identity in a test
// fixture (see connpass-browser-provider.test.js's PLACEHOLDER_IDENTITY
// comment). Proves the same fact reaches the Connpass questionnaire that
// the existing "composes the default Peatix Harness with the attendee
// profile" test above proves for Peatix: both read
// options.peatixAttendeeProfile, nothing hardcoded or duplicated.
test("official production factory threads the Peatix attendee profile's name into the default Connpass workflow's required 氏名 question", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-connpass-name-"));
  const profile = { name: "Placeholder Taro", email: "placeholder@example.test", accept_organizer_privacy: true };
  const candidate = {
    provider: "connpass",
    event_ref: "connpass-event://event/501001",
    canonical_url: "https://tokyo-builders.connpass.com/event/501001/",
    title: "Wiring Test Event",
    starts_at: "2026-08-10T10:00:00.000Z",
    ends_at: "2026-08-10T11:00:00.000Z",
  };
  const nameField = { tagName: "INPUT", type: "text", name: "q_name", value: "", disabled: false };
  const page = connpassNameQuestionnairePageFixture({ canonicalUrl: candidate.canonical_url, nameField });
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-connpass-name-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      browserRail: { open() {}, navigate() {}, close() {} },
      calendarReader: { async readCalendarGaps() { return []; } },
      lumaWorkflow: emptyWorkflow,
      connpassApiClient: { async searchTokyoInventory() { return []; } },
      connpassAutomatedSubmitAllowed: true,
      actionCache: { async replay() {}, saveVerifiedRepair() {} },
      browserHarness: { async runFallback() {}, async performAction() {} },
      evidenceChain: { async completeEvidence() {} },
      operations: { async reportWake() {}, async recordAction() {} },
      peatixAttendeeProfile: profile,
    });
    const result = await dependencies.runDirectAction({ provider: "connpass", candidate, page });
    assert.deepEqual(result, { status: "completed", method: "connpass_direct_submit" });
    assert.equal(nameField.value, profile.name);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory persists the Connpass discovery audit from its default workflow", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-connpass-audit-"));
  const audits = [];
  const emptyWorkflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() {},
    async readProviderState() { return { status: "absent" }; },
  };
  const operations = {
    async recordConnpassDiscoveryAudit(value) { audits.push(value); },
    async reportWake() {},
    async recordAction() {},
  };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-connpass-audit-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      browserRail: { open() {}, navigate() {}, close() {} },
      calendar: { ready() { return true; } },
      calendarReader: { async readCalendarGaps() { return []; } },
      lumaWorkflow: emptyWorkflow,
      connpassApiClient: { async searchTokyoInventory() { return []; } },
      actionCache: { async replay() {}, saveVerifiedRepair() {} },
      browserHarness: { async runFallback() {}, async performAction() {} },
      evidenceChain: { async completeEvidence() {} },
      operations,
      now: () => new Date("2026-08-10T08:30:00.000Z"),
    });
    const page = { async goto() { assert.fail("official API discovery must not navigate"); } };

    assert.deepEqual(await dependencies.discoverCandidates("connpass", [], page), []);
    assert.deepEqual(audits, [{
      observed_count: 0,
      normalized_count: 0,
      window_count: 0,
      free_open_count: 0,
      calendar_free_count: 0,
    }]);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official production factory installs Peatix audit and keeps attendee profile lazy", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-peatix-audit-"));
  const audits = [], profile = { name: "Private Name", email: "private@example.test", accept_organizer_privacy: true };
  let profileReads = 0;
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() {}, async readProviderState() { return { status: "absent" }; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-peatix-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      browserRail: { open() {}, navigate() {}, close() {} }, calendar: { ready() { return true; } },
      calendarReader: { async readCalendarGaps() { return []; } }, lumaWorkflow: emptyWorkflow,
      connpassWorkflow: emptyWorkflow, browserHarness: { async runFallback() {}, async performAction() {} },
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, evidenceChain: { async completeEvidence() {} },
      operations: { async recordPeatixDiscoveryAudit(value) { audits.push(value); }, async reportWake() {}, async recordAction() {} },
      get peatixAttendeeProfile() { profileReads += 1; return profile; },
      now: () => new Date("2026-08-10T08:30:00.000Z"),
    });
    const page = {
      waitForResponse() {
        return Promise.resolve({
          url() { return "https://peatix.com/search/events?p=1&size=20"; },
          ok() { return true; }, async json() { return { json_data: { page: 1, events: [] } }; },
        });
      },
      async goto() {},
    };
    assert.deepEqual(await dependencies.discoverCandidates("peatix", [], page), []);
    assert.deepEqual(audits, [{ observed_count: 0, normalized_count: 0, window_count: 0, free_open_count: 0, calendar_free_count: 0 }]);
    const candidate = {
      provider: "peatix", event_ref: "peatix-event://event/1", canonical_url: "https://peatix.com/event/1",
      title: "Public Event", starts_at: "2026-08-10T01:00:00.000Z", ends_at: "2026-08-10T02:00:00.000Z",
      registration_status: "available", ticket_price_status: "free", ticket_price_minor: 0, ticket_id: "9",
    };
    const direct = await dependencies.runDirectAction({ provider: "peatix", candidate, page: {} });
    assert.equal(direct.status, "failed");
    assert.equal(profileReads, 1);
    assert.doesNotMatch(JSON.stringify({ audits, direct }), /Private Name|private@example\.test/);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("official factory composes the default Peatix Harness with the attendee profile", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-peatix-harness-")); let filled = null; let reads = 0;
  const profile = { name: "Private Name", email: "private@example.test", family_name_kana: "サクラ", given_name_kana: "テスト", accept_organizer_privacy: true };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: filled ? "pending" : "absent" }; } };
  const element = { tagName: "INPUT", type: "text", required: true, dataset: {}, labels: [{ innerText: "Name" }], innerText: "", getAttribute() { return ""; } };
  const page = { locator(selector) {
    if (selector === "input, textarea, select, button, a[role=button], a#confirm-button") return { async evaluateAll(callback) { return callback([element]); } };
    return { async count() { return 1; }, async fill(value) { filled = value; } };
  } };
  try {
    const dependencies = createMinimalProductionDependencies({ repoRoot: "/private/repo", stateDir, wakeId: "wake-production-peatix-harness-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target", lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence", calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } }, lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, peatixWorkflow: emptyWorkflow, peatixAttendeeProfile: profile, proposeAction: async () => ({ purpose: "fill", method: "ax_fill", control: "control_1" }), actionCache: { async replay() { return { status: "cache_miss" }; }, saveVerifiedRepair() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} }, now: () => new Date("2026-08-10T08:30:00.000Z") });
    const result = await dependencies.runAgentFallback({ provider: "peatix", candidate: { event_ref: "peatix-event://event/1" }, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1", maxSteps: 1, expectedState: "registered_or_pending" });
    assert.equal(result.status, "completed"); assert.equal(filled, profile.name); assert.equal(JSON.stringify(result).includes(profile.email), false); reads += 1;
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
  assert.equal(reads, 1);
});

test("production provider router keeps Luma cache direct fallback and readback on one page", async () => {
  const calls = [];
  const page = Object.freeze({ page_id: "owned-page-1" });
  const candidate = Object.freeze({
    provider: "luma",
    event_ref: "luma-event://event/one",
    canonical_url: "https://luma.com/one",
  });
  const calendar = Object.freeze([{ kind: "timed", start_at: "2026-08-10T10:00:00.000Z", end_at: "2026-08-10T11:00:00.000Z" }]);
  const lumaWorkflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "completed" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "registered" }; },
  };
  const actionCache = {
    async replay(input) {
      calls.push(["cache-replay", input]);
      return { status: "cache_miss" };
    },
    saveVerifiedRepair(input) {
      calls.push(["cache-save", input]);
      return { status: "saved", cache_entry_id: "cache-entry-1" };
    },
  };
  const browserHarness = {
    async runFallback(input) { calls.push(["fallback", input]); return { status: "failed" }; },
  };
  let performed; const performAction = async (input) => { performed = input; return { status: "success" }; };
  const router = createProductionProviderRouter({
    lumaWorkflow,
    connpassWorkflow: {
      async discoverCandidates() { return []; },
      async runDirectAction() { return { status: "failed" }; },
      async readProviderState() { return { status: "unavailable" }; },
    },
    actionCache,
    browserHarness,
    performAction,
    now: () => new Date("2026-08-07T08:30:00.000Z"),
  });

  assert.deepEqual(await router.discoverCandidates("luma", calendar, page), [candidate]);
  assert.deepEqual(await router.discoverCandidates("connpass", calendar, page), []);
  assert.deepEqual(await router.runCachedAction({ provider: "luma", candidate, page }), { status: "cache_miss" });
  assert.deepEqual(await router.runDirectAction({ provider: "luma", candidate, page }), { status: "completed" });
  assert.deepEqual(await router.runAgentFallback({
    provider: "luma",
    candidate,
    page,
    pageWebsocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET1",
    maxSteps: 10,
    expectedState: "registered_or_pending",
  }), { status: "failed" });
  assert.deepEqual(await router.readProviderState({ provider: "luma", candidate, page }), { status: "registered" });
  assert.deepEqual(await router.saveRepairedActions({
    provider: "luma",
    candidate,
    page,
    providerState: { status: "registered" },
    repairedActions: [{ purpose: "submit", method: "ax_click", control: "register_button" }],
  }), { status: "saved", cache_entry_id: "cache-entry-1" });

  const replay = calls.find(([name]) => name === "cache-replay")[1];
  assert.equal(replay.provider, "luma");
  assert.equal(replay.workflowVersion, "luma_registration_v1");
  assert.equal(replay.pageState, "registration_page_v1");
  assert.equal(replay.expectedEffect, "registered_or_pending");
  assert.equal(replay.page, page);
  await replay.performAction({ purpose: "fill", method: "ax_fill", control: "control_1" }); assert.equal(performed.provider, "luma");
  assert.equal(typeof replay.readExpectedState, "function");
  assert.equal(calls.find(([name]) => name === "discover")[1].calendar, calendar);
  assert.equal(calls.find(([name]) => name === "fallback")[1].page, page);
  assert.equal(calls.find(([name]) => name === "fallback")[1].candidate, candidate);
  assert.equal(calls.find(([name]) => name === "cache-save")[1].observedAt, "2026-08-07T08:30:00.000Z");
});

test("production provider router continues from Luma to Connpass on the same page", async () => {
  const calls = []; let performed;
  const page = Object.freeze({ page_id: "owned-page-1" });
  const candidate = Object.freeze({
    provider: "connpass",
    event_ref: "connpass-event://event/401001",
    canonical_url: "https://tokyo-builders.connpass.com/event/401001/",
  });
  const workflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "completed" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "pending" }; },
  };
  const actionCache = {
    async replay(input) { calls.push(["cache", input]); return { status: "cache_miss" }; },
    saveVerifiedRepair(input) { calls.push(["save", input]); return { status: "saved" }; },
  };
  const browserHarness = {
    async runFallback(input) { calls.push(["fallback", input]); return { status: "failed" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow,
    connpassWorkflow: workflow,
    connpassAutomatedSubmitAllowed: true,
    actionCache,
    browserHarness,
    async performAction(input) { performed = input; return { status: "success" }; },
    now: () => new Date("2026-08-07T08:30:00.000Z"),
  });

  assert.deepEqual(await router.discoverCandidates("connpass", [], page), [candidate]);
  await router.runCachedAction({ provider: "connpass", candidate, page });
  await router.runDirectAction({ provider: "connpass", candidate, page });
  await router.readProviderState({ provider: "connpass", candidate, page });

  const cached = calls.find(([name]) => name === "cache")[1];
  assert.equal(cached.provider, "connpass");
  assert.equal(cached.workflowVersion, "connpass_registration_v1");
  assert.equal(cached.pageState, "registration_page_v1");
  assert.equal(cached.page, page);
  await cached.performAction({ purpose: "fill", method: "ax_fill", control: "control_1" }); assert.equal(performed.provider, "connpass");
  assert.equal(calls.filter(([name]) => name === "discover")[0][1].page, page);
});

test("production router blocks every Connpass submit path until provider permission is verified", async () => {
  const workflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() { return { status: "completed" }; },
    async readProviderState() { return { status: "absent" }; },
  };
  for (const permission of [undefined, false]) {
    let effects = 0;
    const options = {
      lumaWorkflow: workflow, connpassWorkflow: workflow,
      actionCache: { async replay() { effects += 1; }, saveVerifiedRepair() {} },
      browserHarness: { async runFallback() { effects += 1; } },
      async performAction() { effects += 1; },
      ...(permission === undefined ? {} : { connpassAutomatedSubmitAllowed: permission }),
    };
    const router = createProductionProviderRouter(options);
    const input = { provider: "connpass", candidate: { event_ref: "connpass-event://event/1" }, page: {}, pageWebsocket: "ws://fixture", maxSteps: 1, expectedState: "registered_or_pending" };
    assert.equal((await router.runCachedAction(input)).safe_reason, "connpass_action_permission_required");
    assert.equal((await router.runDirectAction(input)).safe_reason, "connpass_action_permission_required");
    assert.equal((await router.runAgentFallback(input)).safe_reason, "connpass_action_permission_required");
    assert.equal(effects, 0);
  }
});

test("production provider router submits only strong or moderate ranked candidates and fails closed when ranking fails", async () => {
  const strong = Object.freeze({
    provider: "luma", event_ref: "luma-event://event/ai-agents", canonical_url: "https://luma.com/ai-agents",
    title: "AI Agents Tokyo", body: "LLM agent builders", participation_slot_status: "available",
    lightning_talk_status: "unknown", participant_limit: 100, application_deadline_at: null,
  });
  const weak = Object.freeze({
    provider: "luma", event_ref: "luma-event://event/pottery", canonical_url: "https://luma.com/pottery",
    title: "Pottery Social", body: "Make a bowl",
  });
  const workflow = {
    async discoverCandidates() { return [weak, strong]; },
    async runDirectAction() { return { status: "failed" }; },
    async readProviderState() { return { status: "absent" }; },
  };
  let fail = false;
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow,
    connpassWorkflow: { ...workflow, async discoverCandidates() { return []; } },
    actionCache: { async replay() {}, async saveVerifiedRepair() {} },
    browserHarness: { async runFallback() {} },
    async performAction() {},
    async rankCandidates(input) {
      if (fail) throw new Error("ranking unavailable");
      return validateProviderCandidateRanking({ ranked_events: [
        { event_ref: weak.event_ref, priority_class: "other", preference_fit: "weak", preference_reason: "Unrelated topic." },
        { event_ref: strong.event_ref, priority_class: "ai", preference_fit: "strong", preference_reason: "Direct AI fit." },
      ] }, input);
    },
    eventPreferences: "Tokyo AI crypto startup events",
  });

  assert.deepEqual(await router.discoverCandidates("luma", [], {}), [{
    ...strong,
    priority_class: "ai",
    preference_fit: "strong",
    preference_reason: "Direct AI fit.",
    auto_apply_eligible: true,
  }]);
  fail = true;
  await assert.rejects(router.discoverCandidates("luma", [], {}), /ranking unavailable/);
});

test("production provider router promotes a verified open talk within equally fitted AI candidates", async () => {
  const plain = Object.freeze({ provider: "luma", event_ref: "luma-event://event/plain-ai", canonical_url: "https://luma.com/plain-ai", title: "AI Builders", body: "AI meetup for builders." });
  const talk = Object.freeze({ provider: "luma", event_ref: "luma-event://event/ai-lt", canonical_url: "https://luma.com/ai-lt", title: "AI Builders LT", body: "AI meetup. 5 minute LT applications are open at https://forms.example.com/ai-lt" });
  const workflow = { async discoverCandidates() { return [plain, talk]; }, async runDirectAction() {}, async readProviderState() { return { status: "absent" }; } };
  const classified = [];
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow,
    eventPreferences: "Tokyo AI and open lightning talks",
    async rankCandidates(input) {
      return validateProviderCandidateRanking({ ranked_events: [
        { event_ref: plain.event_ref, priority_class: "ai", preference_fit: "strong", preference_reason: "AI event." },
        { event_ref: talk.event_ref, priority_class: "open_talk", preference_fit: "strong", preference_reason: "AI event with an open LT." },
      ] }, input);
    },
    async classifyTalkOpportunity(candidate) {
      classified.push(candidate.event_ref);
      const open = candidate.event_ref === talk.event_ref;
      return validateEventTalkOpportunity(open ? {
        participation_kind: "both", talk_format: "lightning_talk", application_status: "open",
        should_create_talk_application: true, application_url: "https://forms.example.com/ai-lt",
        evidence_excerpt: "5 minute LT applications are open at https://forms.example.com/ai-lt",
        reason: "A public LT application is open.",
      } : {
        participation_kind: "audience_only", talk_format: null, application_status: "not_offered",
        should_create_talk_application: false, application_url: null,
        evidence_excerpt: "AI meetup for builders.", reason: "Audience participation only.",
      }, { canonicalUrl: candidate.canonical_url, title: candidate.title, body: candidate.body, now: "2026-08-07T00:00:00.000Z" });
    },
    actionCache: { async replay() {}, saveVerifiedRepair() {} },
    browserHarness: { async runFallback() {} }, async performAction() {},
  });

  const result = await router.discoverCandidates("luma", [], {});
  assert.deepEqual(result.map((candidate) => candidate.event_ref), [talk.event_ref, plain.event_ref]);
  assert.deepEqual(classified, [talk.event_ref]);
  assert.equal(result[0].priority_class, "open_talk");
  assert.equal(result[0].talk_opportunity.application_url, "https://forms.example.com/ai-lt");
});

test("production provider router verifies independent open-talk candidates with at most three concurrent classifiers", async () => {
  const candidates = Array.from({ length: 12 }, (_, index) => Object.freeze({
    provider: "connpass", event_ref: `connpass-event://event/${610000 + index}`,
    canonical_url: `https://tokyo-ai.connpass.com/event/${610000 + index}/`,
    title: `AI LT ${index}`, body: "Public lightning talk applications are open.",
  }));
  const workflow = { async discoverCandidates() { return candidates; }, async runDirectAction() {}, async readProviderState() { return { status: "absent" }; } };
  let active = 0;
  let maximum = 0;
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow,
    eventPreferences: "Tokyo AI lightning talks",
    async rankCandidates(input) {
      return validateProviderCandidateRanking({ ranked_events: input.candidates.map((candidate) => ({
        event_ref: candidate.event_ref, priority_class: "open_talk", preference_fit: "strong", preference_reason: "Open AI LT.",
      })) }, input);
    },
    async classifyTalkOpportunity(candidate) {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setImmediate(resolve));
      active -= 1;
      return validateEventTalkOpportunity({
        participation_kind: "both", talk_format: "lightning_talk", application_status: "open",
        should_create_talk_application: true, application_url: candidate.canonical_url,
        evidence_excerpt: "Public lightning talk applications are open.", reason: "Public LT application is open.",
      }, { canonicalUrl: candidate.canonical_url, title: candidate.title, body: candidate.body, now: "2026-08-27T00:00:00.000Z" });
    },
    actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {} }, async performAction() {},
  });
  const result = await router.discoverCandidates("connpass", [], {});
  assert.equal(result.length, 12);
  assert.equal(maximum, 3);
  assert.deepEqual(result.map((row) => row.event_ref), candidates.map((row) => row.event_ref));
});

test("production provider router routes Peatix cache direct and readback on one page", async () => {
  const calls = [];
  const page = Object.freeze({ page_id: "owned-page-peatix" });
  const candidate = Object.freeze({ provider: "peatix", event_ref: "peatix-event://event/1", canonical_url: "https://peatix.com/event/1" });
  const workflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "completed" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "registered" }; },
  };
  const actionCache = {
    async replay(input) { calls.push(["cache", input]); return { status: "cache_miss" }; },
    saveVerifiedRepair(input) { calls.push(["save", input]); return { status: "saved" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow, peatixWorkflow: workflow, actionCache,
    browserHarness: { async runFallback() { return { status: "failed" }; } },
    async performAction() { return { status: "success" }; }, now: () => new Date("2026-08-10T08:30:00.000Z"),
  });

  assert.deepEqual(await router.discoverCandidates("peatix", [], page), [candidate]);
  await router.runCachedAction({ provider: "peatix", candidate, page });
  await router.runDirectAction({ provider: "peatix", candidate, page });
  await router.readProviderState({ provider: "peatix", candidate, page });
  assert.throws(() => router.discoverCandidates("meetup", [], page));

  const cached = calls.find(([name]) => name === "cache")[1];
  assert.equal(cached.provider, "peatix");
  assert.equal(cached.workflowVersion, "peatix_registration_v1");
  assert.equal(cached.pageState, "registration_page_v1");
  assert.equal(cached.page, page);
  assert.equal(calls.find(([name]) => name === "direct")[1].page, page);
  assert.equal(calls.find(([name]) => name === "readback")[1].page, page);
  assert.doesNotMatch(JSON.stringify(cached), /Private Name|private@example\.test/);
});

test("official production factory routes Meetup after Peatix on the same page", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-meetup-"));
  const page = Object.freeze({ page_id: "owned-page-meetup" });
  const candidate = Object.freeze({
    provider: "meetup", event_ref: "meetup-event://event/315756352",
    canonical_url: "https://www.meetup.com/tokyo-builders/events/315756352/",
  });
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  const meetupWorkflow = {
    async discoverCandidates(input) { assert.equal(input.page, page); return [candidate]; },
    async runDirectAction(input) { assert.equal(input.page, page); return { status: "failed", safe_reason: "meetup_direct_requires_harness" }; },
    async readProviderState(input) { assert.equal(input.page, page); return { status: "registered" }; },
  };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-meetup-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      browserRail: { open() {}, navigate() {}, close() {} }, calendar: { ready() { return true; } },
      calendarReader: { async readCalendarGaps() { return []; } }, lumaWorkflow: emptyWorkflow,
      connpassWorkflow: emptyWorkflow, peatixWorkflow: emptyWorkflow, meetupWorkflow,
      browserHarness: { async runFallback(input) { assert.equal(input.page, page); return { status: "completed" }; }, async performAction() {} },
      actionCache: { async replay() { return { status: "cache_miss" }; }, saveVerifiedRepair() {} },
      evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} },
    });
    assert.deepEqual(await dependencies.discoverCandidates("meetup", [], page), [candidate]);
    assert.deepEqual(await dependencies.runDirectAction({ provider: "meetup", candidate, page }), { status: "failed", safe_reason: "meetup_direct_requires_harness" });
    assert.deepEqual(await dependencies.runAgentFallback({ provider: "meetup", candidate, page, maxSteps: 10 }), { status: "completed" });
    assert.deepEqual(await dependencies.readProviderState({ provider: "meetup", candidate, page }), { status: "registered" });
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("production provider router routes Doorkeeper through every action path without private cache metadata", async () => {
  const calls = [], page = Object.freeze({ page_id: "owned-page-doorkeeper" });
  const calendar = Object.freeze([{ kind: "timed", start_at: "2026-08-11T10:00:00.000Z", end_at: "2026-08-11T11:00:00.000Z" }]);
  const candidate = Object.freeze({ provider: "doorkeeper", event_ref: "doorkeeper-event://event/1001", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1001", attendee_name: "Private Name", attendee_email: "private@example.test" });
  const workflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "failed", safe_reason: "doorkeeper_direct_requires_harness" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "registered" }; },
  };
  const actionCache = { async replay(input) { calls.push(["cache", input]); return { status: "cache_miss" }; }, saveVerifiedRepair(input) { calls.push(["save", input]); return { status: "saved" }; } };
  let performed;
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow, doorkeeperWorkflow: workflow, actionCache,
    browserHarness: { async runFallback(input) { calls.push(["fallback", input]); return { status: "failed" }; } },
    async performAction(input) { performed = input; return { status: "success" }; },
    now: () => new Date("2026-08-11T08:30:00.000Z"),
  });

  assert.deepEqual(await router.discoverCandidates("doorkeeper", calendar, page), [candidate]);
  assert.deepEqual(await router.runCachedAction({ provider: "doorkeeper", candidate, page }), { status: "cache_miss" });
  assert.deepEqual(await router.runDirectAction({ provider: "doorkeeper", candidate, page }), { status: "failed", safe_reason: "doorkeeper_direct_requires_harness" });
  assert.deepEqual(await router.runAgentFallback({ provider: "doorkeeper", candidate, page, pageWebsocket: "ws://page", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed" });
  assert.deepEqual(await router.readProviderState({ provider: "doorkeeper", candidate, page }), { status: "registered" });
  assert.deepEqual(await router.saveRepairedActions({ provider: "doorkeeper", candidate, page, providerState: { status: "registered" }, repairedActions: [{ purpose: "submit", method: "ax_click", control: "register" }] }), { status: "saved" });

  const replay = calls.find(([name]) => name === "cache")[1];
  assert.deepEqual(Object.keys(replay).sort(), ["expectedEffect", "page", "performAction", "provider", "readExpectedState", "workflowVersion", "pageState"].sort());
  assert.deepEqual({ provider: replay.provider, version: replay.workflowVersion, pageState: replay.pageState, effect: replay.expectedEffect, page: replay.page }, { provider: "doorkeeper", version: "doorkeeper_registration_v1", pageState: "registration_page_v1", effect: "registered_or_pending", page });
  await replay.performAction({ purpose: "fill", method: "ax_fill", control: "field" }); assert.equal(performed.provider, "doorkeeper");
  assert.deepEqual(await replay.readExpectedState({ page }), { status: "registered" });
  assert.deepEqual({ discoverPage: calls.find(([name]) => name === "discover")[1].page, discoverCalendar: calls.find(([name]) => name === "discover")[1].calendar, directPage: calls.find(([name]) => name === "direct")[1].page, readbackPage: calls.find(([name]) => name === "readback")[1].page, fallbackPage: calls.find(([name]) => name === "fallback")[1].page, fallbackCandidate: calls.find(([name]) => name === "fallback")[1].candidate }, { discoverPage: page, discoverCalendar: calendar, directPage: page, readbackPage: page, fallbackPage: page, fallbackCandidate: candidate });
  const saved = calls.find(([name]) => name === "save")[1];
  assert.deepEqual(Object.keys(saved).sort(), ["actions", "expectedEffect", "observedAt", "pageState", "provider", "providerState", "workflowVersion"].sort());
  assert.deepEqual({ provider: saved.provider, version: saved.workflowVersion, pageState: saved.pageState, effect: saved.expectedEffect }, { provider: "doorkeeper", version: "doorkeeper_registration_v1", pageState: "registration_page_v1", effect: "registered_or_pending" });
  assert.doesNotMatch(JSON.stringify({ replay, saved }), /Private Name|private@example\.test/);
});

test("production provider router routes Eventbrite through every closed path on one page without private cache metadata", async () => {
  const calls = [], page = Object.freeze({ page_id: "owned-page-eventbrite" });
  const calendar = Object.freeze([{ kind: "timed", start_at: "2026-08-11T10:00:00.000Z", end_at: "2026-08-11T11:00:00.000Z" }]);
  const candidate = Object.freeze({ provider: "eventbrite", event_ref: "eventbrite-event://event/1901", canonical_url: "https://www.eventbrite.com/e/tokyo-free-tickets-1901", attendee_name: "Private Name", attendee_email: "private@example.test" });
  const workflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "failed", safe_reason: "eventbrite_direct_requires_harness" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "registered" }; },
  };
  const actionCache = {
    async replay(input) { calls.push(["cache", input]); return { status: "cache_miss" }; },
    saveVerifiedRepair(input) { calls.push(["save", input]); return { status: "saved" }; },
  };
  const browserHarness = { async runFallback(input) { calls.push(["fallback", input]); return { status: "failed" }; } };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow, eventbriteWorkflow: workflow, actionCache, browserHarness,
    async performAction() { return { status: "success" }; }, now: () => new Date("2026-08-11T08:30:00.000Z"),
  });

  assert.deepEqual(await router.discoverCandidates("eventbrite", calendar, page), [candidate]);
  assert.deepEqual(await router.runCachedAction({ provider: "eventbrite", candidate, page }), { status: "cache_miss" });
  assert.deepEqual(await router.runDirectAction({ provider: "eventbrite", candidate, page }), { status: "failed", safe_reason: "eventbrite_direct_requires_harness" });
  assert.deepEqual(await router.runAgentFallback({ provider: "eventbrite", candidate, page, pageWebsocket: "ws://page", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed" });
  assert.deepEqual(await router.readProviderState({ provider: "eventbrite", candidate, page }), { status: "registered" });
  assert.deepEqual(await router.saveRepairedActions({ provider: "eventbrite", candidate, page, providerState: { status: "registered" }, repairedActions: [{ purpose: "submit", method: "ax_click", control: "register" }] }), { status: "saved" });

  const cached = calls.find(([name]) => name === "cache")[1];
  assert.deepEqual({ provider: cached.provider, version: cached.workflowVersion, page: cached.page }, { provider: "eventbrite", version: "eventbrite_registration_v1", page });
  assert.doesNotMatch(JSON.stringify(cached), /Private Name|private@example\.test/);
  assert.equal(calls.find(([name]) => name === "discover")[1].page, page);
  assert.equal(calls.find(([name]) => name === "discover")[1].calendar, calendar);
  assert.equal(calls.find(([name]) => name === "fallback")[1].candidate, candidate);
  const saved = calls.find(([name]) => name === "save")[1];
  assert.deepEqual({ provider: saved.provider, version: saved.workflowVersion, pageState: saved.pageState }, { provider: "eventbrite", version: "eventbrite_registration_v1", pageState: "registration_page_v1" });
  assert.doesNotMatch(JSON.stringify(saved), /Private Name|private@example\.test/);
});

test("official production factory injects Eventbrite on the supplied page without opening a browser rail", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-eventbrite-"));
  const page = Object.freeze({ page_id: "injected-eventbrite-page" });
  const calendar = Object.freeze([]);
  const candidate = Object.freeze({ provider: "eventbrite", event_ref: "eventbrite-event://event/1902", canonical_url: "https://www.eventbrite.com/e/tokyo-free-tickets-1902" });
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  let discoveryInput; const railCalls = [];
  const eventbriteWorkflow = { ...emptyWorkflow, async discoverCandidates(input) { discoveryInput = input; return [candidate]; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-eventbrite-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence", calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: { open() { railCalls.push("open"); }, navigate() { railCalls.push("navigate"); }, close() { railCalls.push("close"); } },
      lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, peatixWorkflow: emptyWorkflow, meetupWorkflow: emptyWorkflow, doorkeeperWorkflow: emptyWorkflow, eventbriteWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {}, async performAction() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} },
    });
    assert.deepEqual(await dependencies.discoverCandidates("eventbrite", calendar, page), [candidate]);
    assert.equal(discoveryInput.page, page); assert.equal(discoveryInput.calendar, calendar); assert.deepEqual(railCalls, []);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory default Harness uses the Eventbrite workflow for final readback", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-eventbrite-default-harness-"));
  const candidate = Object.freeze({ provider: "eventbrite", event_ref: "eventbrite-event://event/1903", canonical_url: "https://www.eventbrite.com/e/tokyo-free-tickets-1903" });
  const element = ({ tagName = "INPUT", type = "text", name = "", value = "complete", testId = "", text = "" } = {}) => ({ tagName, type, name, value, required: tagName === "INPUT", disabled: false, hidden: false, isConnected: true, innerText: text, textContent: text, dataset: testId ? { testid: testId } : {}, style: {}, parentElement: null, ownerDocument: { defaultView: { getComputedStyle() { return {}; } } }, getAttribute(key) { return key === "data-testid" ? testId : key === "name" ? name : null; }, hasAttribute() { return false; }, getBoundingClientRect() { return { width: 120, height: 32 }; } });
  const fields = [element({ name: "buyer.N-first_name" }), element({ name: "buyer.N-last_name" }), element({ name: "buyer.N-email", type: "email" }), element({ name: "buyer.confirmEmailAddress", type: "email" })];
  const primary = element({ tagName: "BUTTON", type: "button", testId: "eds-modal__primary-button", text: "Register" }); const elements = [...fields, primary]; let readbacks = 0;
  const handle = { async evaluate(callback, context) { return callback(primary, context); }, async click() {} };
  const frame = { url() { return "https://www.eventbrite.com/checkout-external?eid=1903"; }, parentFrame() { return {}; }, locator(selector) { if (String(selector).includes("data-lm-connector-control") && String(selector).includes("eds-modal__primary-button")) return { async count() { return 1; }, async elementHandles() { return [handle]; } }; return { async count() { return 0; }, async evaluateAll(callback, context) { return callback(elements, context); } }; } };
  const page = { url() { return candidate.canonical_url; }, frames() { return [frame]; }, locator() { return { async evaluateAll() { return []; } }; } };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  const eventbriteWorkflow = { ...emptyWorkflow, async readProviderState(input) { readbacks += 1; assert.equal(input.page, page); assert.equal(input.candidate, candidate); return { status: "registered" }; } };
  const actionCache = { async replay({ page: ownedPage, performAction }) { const result = await performAction({ page: ownedPage, candidate, action: { purpose: "submit", method: "ax_click", control: "eventbrite_attendee_register_1903" } }); assert.deepEqual(result, { status: "success", provider_state: { status: "registered" } }); return { status: "completed" }; }, saveVerifiedRepair() {} };
  try {
    const dependencies = createMinimalProductionDependencies({ repoRoot: "/private/repo", stateDir, wakeId: "wake-production-eventbrite-default-harness-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target", lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence", calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } }, browserRail: { open() {}, navigate() {}, close() {} }, lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, peatixWorkflow: emptyWorkflow, meetupWorkflow: emptyWorkflow, doorkeeperWorkflow: emptyWorkflow, eventbriteWorkflow, actionCache, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} } });
    assert.deepEqual(await dependencies.runCachedAction({ provider: "eventbrite", candidate, page }), { status: "completed" });
    assert.equal(readbacks, 1);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("production provider router routes TECH PLAY through every closed path without private cache metadata", async () => {
  const calls = [], page = Object.freeze({ page_id: "owned-page-techplay" });
  const calendar = Object.freeze([{ kind: "timed", start_at: "2026-08-11T10:00:00.000Z", end_at: "2026-08-11T11:00:00.000Z" }]);
  const candidate = Object.freeze({ provider: "techplay", event_ref: "techplay-event://event/999190", canonical_url: "https://techplay.jp/event/999190", ticket_id: "98036", attendee_name: "Private Name", attendee_email: "private@example.test" });
  const workflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "failed", safe_reason: "techplay_direct_requires_harness" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "registered" }; },
  };
  const actionCache = { async replay(input) { calls.push(["cache", input]); return { status: "cache_miss" }; }, saveVerifiedRepair(input) { calls.push(["save", input]); return { status: "saved" }; } };
  let performed;
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow, techplayWorkflow: workflow, actionCache,
    browserHarness: { async runFallback(input) { calls.push(["fallback", input]); return { status: "failed" }; } },
    async performAction(input) { performed = input; return { status: "success" }; }, now: () => new Date("2026-08-11T08:30:00.000Z"),
  });

  assert.deepEqual(await router.discoverCandidates("techplay", calendar, page), [candidate]);
  assert.deepEqual(await router.runCachedAction({ provider: "techplay", candidate, page }), { status: "cache_miss" });
  assert.deepEqual(await router.runDirectAction({ provider: "techplay", candidate, page }), { status: "failed", safe_reason: "techplay_direct_requires_harness" });
  assert.deepEqual(await router.runAgentFallback({ provider: "techplay", candidate, page, pageWebsocket: "ws://page", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed" });
  assert.deepEqual(await router.readProviderState({ provider: "techplay", candidate, page }), { status: "registered" });
  assert.deepEqual(await router.saveRepairedActions({ provider: "techplay", candidate, page, providerState: { status: "registered" }, repairedActions: [{ purpose: "submit", method: "ax_click", control: "register" }] }), { status: "saved" });
  assert.throws(() => router.discoverCandidates("unknown", calendar, page));

  const replay = calls.find(([name]) => name === "cache")[1];
  assert.deepEqual(Object.keys(replay).sort(), ["expectedEffect", "page", "performAction", "provider", "readExpectedState", "workflowVersion", "pageState"].sort());
  assert.deepEqual({ provider: replay.provider, version: replay.workflowVersion, pageState: replay.pageState, effect: replay.expectedEffect, page: replay.page }, { provider: "techplay", version: "techplay_registration_v1", pageState: "registration_page_v1", effect: "registered_or_pending", page });
  await replay.performAction({ purpose: "fill", method: "ax_fill", control: "field" }); assert.equal(performed.provider, "techplay");
  assert.deepEqual(await replay.readExpectedState({ page }), { status: "registered" });
  const saved = calls.find(([name]) => name === "save")[1];
  assert.deepEqual(Object.keys(saved).sort(), ["actions", "expectedEffect", "observedAt", "pageState", "provider", "providerState", "workflowVersion"].sort());
  assert.deepEqual({ provider: saved.provider, version: saved.workflowVersion, pageState: saved.pageState, effect: saved.expectedEffect }, { provider: "techplay", version: "techplay_registration_v1", pageState: "registration_page_v1", effect: "registered_or_pending" });
  assert.doesNotMatch(JSON.stringify({ replay, saved }), /Private Name|private@example\.test/);
});

test("production provider router clamps normal Harness fallback to ten steps but preserves TECH PLAY fifteen", async () => {
  const fallbackCalls = [];
  const workflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() { return { status: "failed" }; },
    async readProviderState() { return { status: "absent" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow,
    connpassWorkflow: workflow,
    techplayWorkflow: workflow,
    actionCache: { async replay() { return { status: "cache_miss" }; }, saveVerifiedRepair() {} },
    browserHarness: { async runFallback(input) { fallbackCalls.push(input); return { status: "failed" }; } },
    async performAction() { return { status: "success" }; },
    now: () => new Date("2026-08-12T08:30:00.000Z"),
  });
  const page = Object.freeze({ page_id: "budget-page" });
  const candidate = Object.freeze({ event_ref: "fixture-event://budget" });

  await router.runAgentFallback({
    provider: "luma", candidate, page, pageWebsocket: "ws://page/luma", maxSteps: 15,
    expectedState: "registered_or_pending",
  });
  await router.runAgentFallback({
    provider: "techplay", candidate, page, pageWebsocket: "ws://page/techplay", maxSteps: 15,
    expectedState: "registered_or_pending",
  });

  assert.deepEqual(fallbackCalls.map(({ provider, maxSteps }) => [provider, maxSteps]), [
    ["luma", 10],
    ["techplay", 15],
  ]);
});

test("production provider router rejects invalid Harness step budgets before touching the Harness", async () => {
  let fallbackCalls = 0;
  const workflow = {
    async discoverCandidates() { return []; },
    async runDirectAction() { return { status: "failed" }; },
    async readProviderState() { return { status: "absent" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow,
    connpassWorkflow: workflow,
    techplayWorkflow: workflow,
    actionCache: { async replay() { return { status: "cache_miss" }; }, saveVerifiedRepair() {} },
    browserHarness: { async runFallback() { fallbackCalls += 1; return { status: "failed" }; } },
    async performAction() { return { status: "success" }; },
    now: () => new Date("2026-08-12T08:30:00.000Z"),
  });
  const candidate = Object.freeze({ event_ref: "fixture-event://invalid-budget" });
  const page = Object.freeze({ page_id: "invalid-budget-page" });
  const invalidBudgets = ["15", Number.NaN, Number.POSITIVE_INFINITY, 0, -1, 1.5];

  for (const maxSteps of invalidBudgets) {
    assert.throws(() => router.runAgentFallback({
      provider: "luma", candidate, page, pageWebsocket: "ws://page/luma", maxSteps,
      expectedState: "registered_or_pending",
    }));
    assert.equal(fallbackCalls, 0);
  }
});

test("official production factory injects TECH PLAY on the supplied page without opening a browser rail", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-techplay-"));
  const page = Object.freeze({ page_id: "injected-techplay-page" }); const calendar = Object.freeze([]);
  const candidate = Object.freeze({ provider: "techplay", event_ref: "techplay-event://event/999190", canonical_url: "https://techplay.jp/event/999190", ticket_id: "98036" });
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  let discoveryInput; const railCalls = []; const techplayWorkflow = { ...emptyWorkflow, async discoverCandidates(input) { discoveryInput = input; return [candidate]; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-techplay-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence", calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: { open() { railCalls.push("open"); }, navigate() { railCalls.push("navigate"); }, close() { railCalls.push("close"); } },
      lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, techplayWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {}, async performAction() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} },
    });
    assert.deepEqual(await dependencies.discoverCandidates("techplay", calendar, page), [candidate]);
    assert.equal(discoveryInput.page, page); assert.equal(discoveryInput.calendar, calendar); assert.deepEqual(railCalls, []);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory wires the default TECH PLAY audit callback", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-techplay-audit-")); const audits = []; let current = "";
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  const page = { url() { return current; }, async goto(url) { current = url; }, async evaluate() { return []; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-techplay-audit-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence", calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: { open() {}, navigate() {}, close() {} }, lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {}, async recordTechPlayDiscoveryAudit(value) { audits.push(value); } },
      now: () => new Date("2026-08-10T08:30:00.000Z"),
    });
    assert.deepEqual(await dependencies.discoverCandidates("techplay", [], page), []);
    assert.deepEqual(audits, [{ discovered_count: 0, within_window_count: 0, eligible_count: 0, calendar_free_count: 0, selected_count: 0 }]);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory default Harness reaches injected TECH PLAY registered readback for the exact final action with proposer zero", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-techplay-default-harness-")); const candidate = Object.freeze({ provider: "techplay", event_ref: "techplay-event://event/999190", canonical_url: "https://techplay.jp/event/999190", ticket_id: "98036" });
  const final = { tagName: "BUTTON", type: "button", id: "confirm-final", innerText: "申し込みを確定する", textContent: "申し込みを確定する", disabled: false, hidden: false, isConnected: true, style: {}, dataset: {}, parentElement: null, ownerDocument: { defaultView: { getComputedStyle() { return {}; } } }, getAttribute(name) { return name === "id" ? this.id : name === "aria-disabled" || name === "aria-enabled" || name === "aria-hidden" ? null : ""; }, hasAttribute(name) { return name === "hidden" && this.hidden; }, getBoundingClientRect() { return { width: 120, height: 32 }; } };
  let clicks = 0; let readbacks = 0; let proposerCalls = 0;
  const handle = { async evaluate(callback, context) { return callback(final, context); }, async click() { clicks += 1; } };
  const page = { url() { return "https://techplay.jp/event/join/999190/confirm"; }, locator(selector) { if (String(selector).startsWith("[data-lm-connector-control=")) return { async count() { return 1; }, async elementHandles() { return [handle]; } }; return { async evaluateAll(callback, context) { return callback([final], context); } }; } };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  const techplayWorkflow = { ...emptyWorkflow, async readProviderState(input) { readbacks += 1; assert.equal(input.page, page); assert.equal(input.candidate, candidate); return { status: "registered", receipt_id: "techplay-1" }; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-techplay-default-harness-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target", lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } }, browserRail: { open() {}, navigate() {}, close() {} }, lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, techplayWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} }, proposeAction: async () => { proposerCalls += 1; throw new Error("TECH PLAY final must not use proposer"); }, resolveValue: async () => null,
    });
    const result = await dependencies.runAgentFallback({ provider: "techplay", candidate, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/TECHPLAYFACTORY1", maxSteps: 15, expectedState: "registered_or_pending" });
    assert.deepEqual(result, { status: "completed", provider_state: { status: "registered", receipt_id: "techplay-1" }, repaired_actions: [{ purpose: "submit", method: "ax_click", control: "techplay_final_999190" }] });
    assert.equal(readbacks, 1); assert.equal(clicks, 1); assert.equal(proposerCalls, 0);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory routes an injected Doorkeeper workflow without opening a browser rail", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-doorkeeper-"));
  const page = Object.freeze({ page_id: "injected-doorkeeper-page" }), calendar = Object.freeze([]);
  const candidate = Object.freeze({ provider: "doorkeeper", event_ref: "doorkeeper-event://event/1002", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1002" });
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  let discoveryInput; const doorkeeperWorkflow = { ...emptyWorkflow, async discoverCandidates(input) { discoveryInput = input; return [candidate]; } }; const railCalls = [];
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-doorkeeper-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence", calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: { open() { railCalls.push("open"); }, navigate() { railCalls.push("navigate"); }, close() { railCalls.push("close"); } },
      lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, peatixWorkflow: emptyWorkflow, meetupWorkflow: emptyWorkflow, doorkeeperWorkflow,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {}, async performAction() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} },
    });
    assert.deepEqual(await dependencies.discoverCandidates("doorkeeper", calendar, page), [candidate]);
    assert.equal(discoveryInput.page, page); assert.equal(discoveryInput.calendar, calendar); assert.deepEqual(railCalls, []);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory default Harness reaches injected Doorkeeper parent readback", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-doorkeeper-default-harness-"));
  const candidate = Object.freeze({ provider: "doorkeeper", event_ref: "doorkeeper-event://event/1003", canonical_url: "https://tokyo-builders.doorkeeper.jp/events/1003" });
  const railCalls = []; let clicks = 0; let readbacks = 0;
  const element = {
    tagName: "A", href: "#new_registration_modal", innerText: "申し込む", textContent: "申し込む",
    dataset: {}, hidden: false, isConnected: true, style: {}, parentElement: null,
    ownerDocument: { defaultView: { getComputedStyle() { return {}; } } },
    getAttribute(name) { return name === "href" ? this.href : null; },
    hasAttribute() { return false; },
    getBoundingClientRect() { return { width: 120, height: 32 }; },
  };
  const page = {
    url() { return candidate.canonical_url; },
    locator(selector) {
      if (selector.startsWith("input, textarea")) return { async evaluateAll(callback, context) { return callback([element], context); } };
      return { async count() { return 1; }, async click() { clicks += 1; } };
    },
  };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  const doorkeeperWorkflow = {
    ...emptyWorkflow,
    async readProviderState(input) { readbacks += 1; assert.equal(input.page, page); assert.equal(input.candidate, candidate); return { status: "registered" }; },
  };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-doorkeeper-default-harness-1",
      calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target",
      lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } },
      browserRail: { open() { railCalls.push("open"); }, navigate() { railCalls.push("navigate"); }, close() { railCalls.push("close"); } },
      lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, peatixWorkflow: emptyWorkflow, meetupWorkflow: emptyWorkflow,
      doorkeeperWorkflow, proposeAction: async () => ({ control: "control_1" }), resolveValue: async () => null,
      actionCache: { async replay() {}, saveVerifiedRepair() {} }, evidenceChain: { async completeEvidence() {} },
      operations: { async reportWake() {}, async recordAction() {} },
    });
    const result = await dependencies.runAgentFallback({ provider: "doorkeeper", candidate, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/DOORKEEPERFACTORY1", maxSteps: 1, expectedState: "registered_or_pending" });
    assert.equal(result.status, "completed");
    assert.equal(readbacks, 1);
    assert.equal(clicks, 1);
    assert.deepEqual(railCalls, []);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("production calendar reader uses gog for exactly twenty-eight Tokyo calendar days", async () => {
  const calls = [];
  const calendar = Object.freeze({ kind: "gog", ready: () => true });
  const verifiedInventory = Object.freeze({
    busy_inventory_id: "google-calendar-busy-inventory:test",
    busy_intervals: Object.freeze([
      Object.freeze({
        kind: "timed",
        start_at: "2026-08-10T10:00:00.000Z",
        end_at: "2026-08-10T11:00:00.000Z",
      }),
    ]),
  });
  const reader = createProductionCalendarReader({
    gogBin: "/usr/local/bin/gog",
    account: "private-account",
    keyring: "private-keyring",
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    makeCalendar(input) {
      calls.push(["make-calendar", input]);
      return calendar;
    },
    async inspectBusyInventory(input) {
      calls.push(["inspect", input]);
      return verifiedInventory;
    },
    isVerifiedBusyInventory(value) { return value === verifiedInventory; },
  });

  const intervals = await reader.readCalendarGaps();

  assert.deepEqual(intervals, verifiedInventory.busy_intervals);
  assert.equal(calls[0][0], "make-calendar");
  assert.deepEqual(calls[0][1], {
    bin: "/usr/local/bin/gog",
    account: "private-account",
    keyring: "private-keyring",
  });
  assert.equal(calls[1][0], "inspect");
  assert.equal(calls[1][1].calendar, calendar);
  assert.equal(calls[1][1].timeZone, "Asia/Tokyo");
  assert.equal(calls[1][1].timeMin, "2026-08-06T15:00:00.000Z");
  assert.equal(calls[1][1].timeMax, "2026-09-03T15:00:00.000Z");
  assert.equal(calls[1][1].now, "2026-08-07T08:30:00.000Z");
});

test("production calendar reader retries one transient busy-inventory failure", async () => {
  let attempts = 0;
  const verifiedInventory = Object.freeze({
    busy_inventory_id: "google-calendar-busy-inventory:retry",
    busy_intervals: Object.freeze([]),
  });
  const reader = createProductionCalendarReader({
    now: () => new Date("2026-08-07T08:30:00.000Z"),
    makeCalendar: () => Object.freeze({ ready: () => true }),
    async inspectBusyInventory() {
      attempts += 1;
      if (attempts === 1) throw new Error("transient calendar read failure");
      return verifiedInventory;
    },
    isVerifiedBusyInventory(value) { return value === verifiedInventory; },
  });

  assert.deepEqual(await reader.readCalendarGaps(), []);
  assert.equal(attempts, 2);
});

test("Tokyo twenty-eight-day horizon has unique local dates and one shared end-exclusive boundary", () => {
  const dates = Array.from({ length: 28 }, (_, offset) => (
    new Date(Date.UTC(2026, 7, 7 + offset)).toISOString().slice(0, 10)
  ));
  assert.equal(new Set(dates).size, 28);
  assert.equal(dates[0], "2026-08-07");
  assert.equal(dates.at(-1), "2026-09-03");
  assert.equal(new Date(Date.UTC(2026, 7, 7 + dates.length)).toISOString().slice(0, 10), "2026-09-04");
  assert.equal(Date.parse("2026-09-03T15:00:00.000Z"), Date.parse("2026-09-04T00:00:00+09:00"));
});

test("production browser rail owns exactly one :9222 target without closing the browser", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-rail-"));
  const calls = [];
  const page = {
    async goto(url, options) { calls.push(["goto", url, options]); },
  };
  const browser = {
    close() { calls.push(["browser-close"]); },
  };
  const controller = {
    async create() {
      calls.push(["target-create"]);
      return Object.freeze({
        target_id: "OWNEDTARGET1",
        page_websocket: "ws://[::1]:9222/devtools/page/OWNEDTARGET1",
        page,
      });
    },
    async close(targetId) { calls.push(["target-close", targetId]); return true; },
    async probe() { return true; },
  };
  const owner = {
    async reapStale(input) { calls.push(["reap-stale", input]); return { reaped_target_ids: [], retained_target_ids: [] }; },
    async claimExact(input) {
      calls.push(["claim", input]);
      return Object.freeze({
        schema_version: 1,
        owner_token: "owner-token-production-rail",
        generation: 1,
        target_id: input.targetId,
        page_websocket: input.pageWebsocket,
        canonical_url: "https://luma.com/tokyo?k=p",
        claimed_at: "2026-08-07T02:00:00.000Z",
      });
    },
    async probe() { calls.push(["probe"]); return true; },
    async heartbeat() { calls.push(["heartbeat"]); return true; },
    async release(receipt) { calls.push(["release", receipt.target_id]); return true; },
  };

  try {
    const rail = createProductionBrowserRail({
      stateDir,
      connectOverCDP: async (endpoint, options) => {
        assert.equal(endpoint, "http://127.0.0.1:9222");
        assert.deepEqual(options, { timeout: 120_000 });
        calls.push(["connect", endpoint]);
        return browser;
      },
      createTargetController: (input) => {
        assert.equal(input.browser, browser);
        return controller;
      },
      createTargetOwnership: ({ ownerToken }) => {
        assert.equal(ownerToken, "owner-token-production-rail");
        calls.push(["ownership"]);
        return owner;
      },
      makeSessionId: () => "session-production-rail-1",
    });

    const owned = await rail.open({ ownerToken: "owner-token-production-rail" });
    await rail.navigate(owned, "https://luma.com/event-one");
    await rail.close(owned);

    assert.equal(owned.page, page);
    assert.equal(owned.target_id, "OWNEDTARGET1");
    assert.equal(calls.filter(([name]) => name === "connect").length, 1);
    assert.equal(calls.filter(([name]) => name === "reap-stale").length, 1);
    assert.equal(calls.filter(([name]) => name === "target-create").length, 1);
    assert.equal(calls.filter(([name]) => name === "claim").length, 1);
    assert.equal(calls.filter(([name]) => name === "goto").length, 1);
    assert.equal(calls.filter(([name]) => name === "release").length, 1);
    assert.equal(calls.filter(([name]) => name === "target-close").length, 0);
    assert.equal(calls.filter(([name]) => name === "browser-close").length, 0);
    const ownershipIndex = calls.findIndex(([name]) => name === "ownership");
    const reapIndex = calls.findIndex(([name]) => name === "reap-stale");
    const createIndex = calls.findIndex(([name]) => name === "target-create");
    assert.ok(ownershipIndex < reapIndex && reapIndex < createIndex);
    assert.deepEqual(calls[reapIndex], ["reap-stale", { maxIdleMs: 660_000 }]);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("production browser rail does not create a target until stale reap succeeds", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-rail-reap-failure-"));
  const calls = [];
  const page = { async goto() {} };
  const controller = {
    async create() {
      calls.push(["target-create"]);
      return { target_id: "OWNEDTARGET2", page_websocket: "ws://127.0.0.1:9222/devtools/page/OWNEDTARGET2", page };
    },
    async close(targetId) { calls.push(["target-close", targetId]); return true; },
  };
  const owner = {
    async reapStale() { calls.push(["reap-stale"]); throw new Error("stale reap failed"); },
    async claimExact() { throw new Error("claim must not run"); },
    async probe() { throw new Error("probe must not run"); },
    async heartbeat() { throw new Error("heartbeat must not run"); },
    async release() { throw new Error("release must not run"); },
  };

  try {
    const rail = createProductionBrowserRail({
      stateDir,
      connectOverCDP: async () => { calls.push(["connect"]); return {}; },
      createTargetController: () => controller,
      createTargetOwnership: () => { calls.push(["ownership"]); return owner; },
      makeSessionId: () => "session-production-rail-reap-failure",
    });

    await assert.rejects(() => rail.open({ ownerToken: "owner-token-production-rail-reap-failure" }), /stale reap failed/);
    assert.deepEqual(calls.map(([name]) => name), ["connect", "ownership", "reap-stale"]);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("composed cached action self-heal repairs one stale submit and replays it on the next wake", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-self-heal-"));
  const cachePath = path.join(stateDir, "action-cache.json");
  const observedAt = "2026-08-11T08:30:00.000Z";
  const cacheKey = {
    provider: "luma", workflowVersion: "luma_registration_v1", pageState: "registration_page_v1", expectedEffect: "registered_or_pending",
  };
  const staleAction = { purpose: "submit", method: "ax_click", control: "stale_register_button" };
  const replacementAction = { purpose: "submit", method: "ax_click", control: "replacement_register_button" };
  const seededCache = createConnectorActionCache({ path: cachePath });
  const page = { registrationState: "absent" };
  const candidate = Object.freeze({ provider: "luma", event_ref: "luma-event://event/self-heal", canonical_url: "https://luma.com/self-heal" });
  const events = [], touchedPages = [], cacheAttempts = [], reports = [];
  let directCalls = 0; let fallbackCalls = 0; let proposerCalls = 0; let harnessActionCalls = 0; let harnessReadbackCalls = 0; let saveCalls = 0; let evidenceCalls = 0; let openCalls = 0; let closeCalls = 0;
  await seededCache.saveVerifiedRepair({ ...cacheKey, providerState: { status: "registered" }, actions: [staleAction], observedAt });
  assert.deepEqual(seededCache.read(cacheKey).actions, [staleAction]);
  assert.equal(fs.statSync(cachePath).mode & 0o777, 0o600);

  const touch = (suppliedPage) => { touchedPages.push(suppliedPage); assert.equal(suppliedPage, page); };
  const applyAction = async (source, input) => {
    touch(input.page);
    const control = input.action.control;
    cacheAttempts.push(`${source}:${control}`);
    events.push(`${source}:${control}`);
    if (control === staleAction.control) return { status: "failed" };
    if (control === replacementAction.control) { page.registrationState = "registered"; return { status: "success" }; }
    return { status: "failed" };
  };
  const workflow = {
    async discoverCandidates({ page: suppliedPage }) { touch(suppliedPage); return [candidate]; },
    async runDirectAction({ page: suppliedPage }) { touch(suppliedPage); directCalls += 1; events.push("direct"); return { status: "failed", safe_reason: "direct_action_failed" }; },
    async readProviderState({ page: suppliedPage }) { touch(suppliedPage); const status = page.registrationState; events.push(`parent:${status}`); return { status }; },
  };
  const browserHarness = createBrowserHarnessAdapter({
    async observePage({ page: suppliedPage, target_id }) { touch(suppliedPage); assert.equal(target_id, "SELFHEALTHTARGET"); return { controls: [staleAction.control, replacementAction.control] }; },
    async proposeAction({ step, target_id }) { proposerCalls += 1; assert.equal(step, 1); assert.equal(target_id, "SELFHEALTHTARGET"); return replacementAction; },
    async performAction(input) { harnessActionCalls += 1; return applyAction("harness", input); },
    async readExpectedState({ page: suppliedPage }) { touch(suppliedPage); harnessReadbackCalls += 1; events.push(`harness:${page.registrationState}`); return { status: page.registrationState }; },
  });
  const realCache = createConnectorActionCache({ path: cachePath });
  const actionCache = {
    replay: realCache.replay,
    saveVerifiedRepair(input) { saveCalls += 1; assert.equal(page.registrationState, "registered"); assert.deepEqual(input.actions, [replacementAction]); events.push("cache-save"); return realCache.saveVerifiedRepair(input); },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow,
    connpassWorkflow: { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } },
    actionCache,
    browserHarness,
    async performAction(input) { return applyAction("cache", input); },
    now: () => new Date(observedAt),
  });
  const dependencies = {
    now: () => observedAt,
    browserRail: {
      async open() { openCalls += 1; return { session_id: "session-self-heal", target_id: "SELFHEALTHTARGET", page_websocket: "ws://127.0.0.1:9222/devtools/page/SELFHEALTHTARGET", page }; },
      async navigate(owned, url) { touch(owned.page); events.push(`navigate:${url}`); },
      async close(owned) { touch(owned.page); closeCalls += 1; },
    },
    async readCalendarGaps() { return []; },
    discoverCandidates: router.discoverCandidates,
    runCachedAction: router.runCachedAction,
    runDirectAction: router.runDirectAction,
    runAgentFallback(input) { fallbackCalls += 1; return router.runAgentFallback(input); },
    readProviderState: router.readProviderState,
    saveRepairedActions: router.saveRepairedActions,
    async completeEvidence({ page: suppliedPage, providerState }) { touch(suppliedPage); assert.equal(providerState.status, "registered"); evidenceCalls += 1; events.push("evidence"); return { status: "applied_bundle", bundle_id: `self-heal-bundle-${evidenceCalls}`, completion_disposition: "created" }; },
    async reportWake(report) { reports.push(report); events.push(`report:${report.status}`); return { telegram_provider_id: `self-heal-${reports.length}` }; },
    async recordAction() {},
  };
  const runWake = () => runMinimalConnectorWake({ ownerToken: "owner-token-connector-self-heal", providers: ["luma"] }, dependencies);
  try {
    const first = await runWake();
    assert.deepEqual(first, { status: "applied_bundle", bundle_id: "self-heal-bundle-1", telegram_provider_id: "self-heal-1" });
    assert.deepEqual(realCache.read(cacheKey).actions, [replacementAction]);
    assert.equal(JSON.stringify(realCache.read(cacheKey)).includes(staleAction.control), false);
    assert.ok(events.indexOf("parent:registered") < events.indexOf("cache-save"));
    const cacheAfterRepair = fs.readFileSync(cachePath);
    page.registrationState = "absent";
    const directAfterFirst = directCalls; const fallbackAfterFirst = fallbackCalls; const proposerAfterFirst = proposerCalls;
    const second = await runWake();
    assert.deepEqual(second, { status: "applied_bundle", bundle_id: "self-heal-bundle-2", telegram_provider_id: "self-heal-2" });
    assert.deepEqual(realCache.read(cacheKey).actions, [replacementAction]);
    assert.deepEqual(fs.readFileSync(cachePath), cacheAfterRepair);
    assert.equal(directCalls, directAfterFirst); assert.equal(fallbackCalls, fallbackAfterFirst); assert.equal(proposerCalls, proposerAfterFirst);
    assert.deepEqual(cacheAttempts, ["cache:stale_register_button", "harness:replacement_register_button", "cache:replacement_register_button"]);
    assert.deepEqual([directCalls, fallbackCalls, proposerCalls, harnessActionCalls, harnessReadbackCalls, saveCalls, evidenceCalls, reports.length, openCalls, closeCalls], [1, 1, 1, 1, 1, 1, 2, 2, 2, 2]);
    assert.equal(new Set(touchedPages).size, 1);
    assert.equal(fs.statSync(cachePath).mode & 0o777, 0o600);
    assert.deepEqual(reports.map(({ status, safe_reason }) => [status, safe_reason]), [["applied_bundle", "applied_bundle"], ["applied_bundle", "applied_bundle"]]);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test("production provider router routes an injected KokuchPro workflow with an exact version", async () => {
  const calls = [];
  const page = Object.freeze({ page_id: "owned-page-kokuchpro" });
  const candidate = Object.freeze({ provider: "kokuchpro", event_ref: "kokuchpro-event://event/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", canonical_url: "https://www.kokuchpro.com/event/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/" });
  const workflow = {
    async discoverCandidates(input) { calls.push(["discover", input]); return [candidate]; },
    async runDirectAction(input) { calls.push(["direct", input]); return { status: "failed", safe_reason: "kokuchpro_direct_requires_harness" }; },
    async readProviderState(input) { calls.push(["readback", input]); return { status: "absent" }; },
  };
  const actionCache = {
    async replay(input) { calls.push(["cache", input]); return { status: "cache_miss" }; },
    saveVerifiedRepair(input) { calls.push(["save", input]); return { status: "saved" }; },
  };
  const router = createProductionProviderRouter({
    lumaWorkflow: workflow, connpassWorkflow: workflow, kokuchproWorkflow: workflow, actionCache,
    browserHarness: { async runFallback(input) { calls.push(["fallback", input]); return { status: "failed" }; } },
    async performAction(input) { calls.push(["perform", input]); return { status: "success" }; },
    now: () => new Date("2026-08-12T08:30:00.000Z"),
  });
  const calendar = Object.freeze([]);
  assert.deepEqual(await router.discoverCandidates("kokuchpro", calendar, page), [candidate]);
  assert.deepEqual(await router.runCachedAction({ provider: "kokuchpro", candidate, page }), { status: "cache_miss" });
  assert.deepEqual(await router.runDirectAction({ provider: "kokuchpro", candidate, page }), { status: "failed", safe_reason: "kokuchpro_direct_requires_harness" });
  assert.deepEqual(await router.runAgentFallback({ provider: "kokuchpro", candidate, page, pageWebsocket: "ws://page", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed" });
  assert.deepEqual(await router.readProviderState({ provider: "kokuchpro", candidate, page }), { status: "absent" });
  assert.deepEqual(await router.saveRepairedActions({ provider: "kokuchpro", candidate, page, providerState: { status: "registered" }, repairedActions: [] }), { status: "saved" });
  const cached = calls.find(([name]) => name === "cache")[1];
  assert.deepEqual({ provider: cached.provider, version: cached.workflowVersion, page: cached.page }, { provider: "kokuchpro", version: "kokuchpro_registration_v1", page });
  assert.equal(calls.find(([name]) => name === "discover")[1].page, page);
  assert.equal(calls.find(([name]) => name === "direct")[1].page, page);
  assert.equal(calls.find(([name]) => name === "readback")[1].page, page);
  assert.equal(calls.find(([name]) => name === "fallback")[1].page, page);
  assert.throws(() => router.discoverCandidates("KokuchPro", calendar, page));
  assert.throws(() => router.discoverCandidates("unknown", calendar, page));
});

test("official production factory creates the default KokuchPro workflow with audit and one-page routes", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-kokuchpro-default-"));
  const eventKey = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  const canonicalUrl = `https://www.kokuchpro.com/event/${eventKey}/`;
  const candidate = Object.freeze({ provider: "kokuchpro", event_ref: `kokuchpro-event://event/${eventKey}`, canonical_url: canonicalUrl, title: "Tokyo free event", starts_at: "2026-08-20T10:00:00.000Z", ends_at: "2026-08-20T11:30:00.000Z", venue: "Hall", address: "東京都豊島区", registration_status: "available", ticket_id: "1", ticket_price_status: "free", ticket_price_minor: 0 });
  const listingUrlPrefix = "https://www.kokuchpro.com/s/area-";
  const detail = { jsonld: { "@type": "Event", url: canonicalUrl, name: "Tokyo free event", startDate: "2026-08-20T19:00:00+09:00", endDate: "2026-08-20T20:30:00+09:00", eventAttendanceMode: "https://schema.org/OfflineEventAttendanceMode", location: { "@type": "Place", name: "Hall", address: { addressLocality: "東京都豊島区" } }, offers: { "@type": "Offer", url: canonicalUrl, price: 0, priceCurrency: "JPY", availability: "https://schema.org/InStock" } }, fee_rows: [{ label: "料金制度", value: "無料イベント" }], ticket_rows: [{ id: "ticket-1", label: "無料", status: "募集中" }], canonical_url: canonicalUrl, availability_values: ["1"], entry_actions: [`${canonicalUrl}entry/`] };
  const page = { current: "", mode: "detail", url() { return this.current; }, async goto(url) { this.current = url; }, async evaluate(_callback, argument) { if (this.current === "https://www.kokuchpro.com/mypage/participant/") return []; if (this.current.startsWith(listingUrlPrefix)) return [[canonicalUrl]]; if (this.current === canonicalUrl && this.mode === "detail") return detail; if (this.current === canonicalUrl) return { entry_forms: [{ action: `${canonicalUrl}entry/`, method: "POST" }] }; return argument; } };
  const audits = [], fallbackPages = [];
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-kokuchpro-default-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target", lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } }, browserRail: { open() {}, navigate() {}, close() {} }, lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow,
      actionCache: { async replay() { return { status: "cache_miss" }; }, saveVerifiedRepair() {} }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {}, async recordKokuchProDiscoveryAudit(input) { audits.push(input); } },
      browserHarness: { async runFallback(input) { fallbackPages.push(input.page); return { status: "failed" }; }, async performAction() {} }, now: () => new Date("2026-08-12T08:30:00.000Z"),
    });
    assert.deepEqual(await dependencies.discoverCandidates("kokuchpro", [], page), [candidate]);
    assert.deepEqual(audits, [{ discovered_count: 1, within_window_count: 1, eligible_count: 1, calendar_free_count: 1, selected_count: 1 }]);
    assert.deepEqual(await dependencies.runDirectAction({ provider: "kokuchpro", candidate, page }), { status: "failed", safe_reason: "kokuchpro_direct_requires_harness" });
    page.mode = "readback";
    assert.deepEqual(await dependencies.readProviderState({ provider: "kokuchpro", candidate, page }), { status: "absent" });
    assert.deepEqual(await dependencies.runAgentFallback({ provider: "kokuchpro", candidate, page, pageWebsocket: "ws://page", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed" });
    assert.deepEqual(fallbackPages, [page]);
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("official production factory default KokuchPro Harness stops at auth before proposer, operation, private value, or cache save", async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "connector-production-kokuchpro-auth-"));
  const eventKey = "cccccccccccccccccccccccccccccccc";
  const canonicalUrl = `https://www.kokuchpro.com/event/${eventKey}/`;
  const candidate = Object.freeze({ provider: "kokuchpro", event_ref: `kokuchpro-event://event/${eventKey}`, canonical_url: canonicalUrl, title: "Tokyo free event", starts_at: "2026-08-20T10:00:00.000Z", ends_at: "2026-08-20T11:00:00.000Z", venue: "Hall", address: "東京都豊島区", registration_status: "available", ticket_id: "ticket-1", ticket_price_status: "free", ticket_price_minor: 0 });
  const entryPath = `${new URL(canonicalUrl).pathname}entry/`;
  const page = { url() { return `https://www.kokuchpro.com/auth/login/?continue=${encodeURIComponent(entryPath)}`; }, async evaluate() { return { password_count: 1, login_forms: [{ action: "https://www.kokuchpro.com/auth/login/", method: "POST" }] }; } };
  const counts = { inspect: 0, propose: 0, operate: 0, resolve: 0, save: 0 };
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  try {
    const dependencies = createMinimalProductionDependencies({
      repoRoot: "/private/repo", stateDir, wakeId: "wake-production-kokuchpro-auth-1", calendarAccount: "private-account", gogKeyring: "private-keyring", telegramTarget: "private-target", lumaFormProfilePath: "/private/form-profile.json", lunaEvidenceDir: "/private/luna-evidence",
      calendar: { ready() { return true; } }, calendarReader: { async readCalendarGaps() { return []; } }, browserRail: { open() {}, navigate() {}, close() {} }, lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow,
      actionCache: { async replay() { return { status: "cache_miss" }; }, saveVerifiedRepair() { counts.save += 1; } }, evidenceChain: { async completeEvidence() {} }, operations: { async reportWake() {}, async recordAction() {} },
      inspectControls: async () => { counts.inspect += 1; return []; }, proposeAction: async () => { counts.propose += 1; return { control: "register" }; }, operateControl: async () => { counts.operate += 1; return { status: "success" }; }, resolveValue: async () => { counts.resolve += 1; return "private"; },
    });
    assert.deepEqual(await dependencies.runAgentFallback({ provider: "kokuchpro", candidate, page, pageWebsocket: "ws://127.0.0.1:9222/devtools/page/KOKUCHPROAUTH1", maxSteps: 1, expectedState: "registered_or_pending" }), { status: "failed", safe_reason: "auth_required", repaired_actions: [] });
    assert.deepEqual(counts, { inspect: 0, propose: 0, operate: 0, resolve: 0, save: 0 });
  } finally { fs.rmSync(stateDir, { recursive: true, force: true }); }
});

test("production provider router rejects a partial KokuchPro workflow without affecting existing routes", () => {
  const emptyWorkflow = { async discoverCandidates() { return []; }, async runDirectAction() { return { status: "failed" }; }, async readProviderState() { return { status: "absent" }; } };
  assert.throws(() => createProductionProviderRouter({
    lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, kokuchproWorkflow: { async discoverCandidates() {}, async runDirectAction() {} },
    actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {} }, performAction() {}, now: () => new Date(),
  }), /Connector minimal production unavailable/);
  assert.doesNotThrow(() => createProductionProviderRouter({
    lumaWorkflow: emptyWorkflow, connpassWorkflow: emptyWorkflow, actionCache: { async replay() {}, saveVerifiedRepair() {} }, browserHarness: { async runFallback() {} }, performAction() {}, now: () => new Date(),
  }));
});
