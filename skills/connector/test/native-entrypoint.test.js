"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { nativeExitCode, runNativePass } = require("../native-pass.js");

const REPO_ROOT = path.resolve(__dirname, "../../..");
const VALID_KANA = Object.freeze({ family: "サクラ", given: "テスト" });
const VALID_NAME_JA = "桜 太郎";
const BASE_ENV = Object.freeze({ CONNPASS_API_KEY: "fixture-connpass-api-key-0000", GOG_ACCOUNT: "private@example.com", DAIS_LEGAL_NAME_ROMAJI: "Dais Example", GEMINI_API_KEY: "fixture-ranking-key", GOG_KEYRING_PASSWORD: "private-keyring", LM_CONNECTOR_TELEGRAM_TARGET: "private-target", TELEGRAM_BOT_TOKEN: "fixture-telegram-token" });

function writeKanaProfile(home, value = VALID_KANA, mode = 0o600, nameJa, identity = { name: BASE_ENV.DAIS_LEGAL_NAME_ROMAJI, preferred_name: "Dais" }) {
  const file = path.join(home, ".config", "anicca", "job-search", "profile.json");
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const candidate = { name_kana: value, name_ja: arguments.length < 4 ? VALID_NAME_JA : nameJa, ...identity };
  const raw = typeof value === "string" ? value : JSON.stringify({ candidate });
  fs.writeFileSync(file, `${raw}\n`, { mode });
  fs.chmodSync(file, mode);
}

function writeSharedConnectorEnv(home, token = BASE_ENV.TELEGRAM_BOT_TOKEN) {
  const file = path.join(home, "connector.env");
  fs.writeFileSync(file, `TELEGRAM_BOT_TOKEN=${token}\n`, { mode: 0o600 });
  fs.chmodSync(file, 0o600);
  return file;
}
test("official foreground entrypoint is directly executable", () => {
  const mode = fs.statSync(path.join(REPO_ROOT, "skills", "connector", "run.sh")).mode;
  assert.notEqual(mode & 0o111, 0);
});

test("native terminal exit treats only healthy statuses as success", () => {
  for (const [result, expected] of [
    [{ status: "applied_bundle" }, 0],
    [{ status: "completed_no_effect" }, 0],
    [{ status: "circuit_open" }, 1],
    [{ status: "malformed" }, 1],
    [undefined, 1],
    [null, 1],
    [[], 1],
  ]) assert.equal(nativeExitCode(result), expected);
});

test("official native pass forwards only the bounded minimal wake contract", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-minimal-"));
  const observed = [];
  try {
    const result = await runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(directory, "state"),
      ownerToken: "native-pass-minimal-owner-123456",
      dependencies: Object.freeze({ boundary: "fixture" }),
      async runWake(input, dependencies) {
        observed.push({ input, dependencies });
        return Object.freeze({ status: "circuit_open", safe_reason: "fixture" });
      },
    });

    assert.deepEqual(result, { status: "circuit_open", safe_reason: "fixture" });
    assert.equal(observed.length, 1);
    assert.deepEqual(observed[0].input.providers, ["luma", "connpass", "peatix", "meetup", "doorkeeper", "eventbrite", "techplay", "kokuchpro"]);
    assert.equal(observed[0].input.maxConsecutiveFailures, 3);
    assert.equal(observed[0].input.maxWakeMs, 600_000);
    assert.equal(observed[0].input.maxAgentSteps, 15);
    assert.deepEqual(observed[0].dependencies, { boundary: "fixture" });
    assert.equal(fs.existsSync(path.join(directory, "state", "provider-cursor.json")), false);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("official native pass builds the production dependency boundary from allowlisted config", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-minimal-"));
  const observed = [];
  try {
    writeKanaProfile(directory);
    const sharedEnvFile = writeSharedConnectorEnv(directory);
    const result = await runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(directory, "state"),
      ownerToken: "native-pass-minimal-owner-123456",
      env: {
        HOME: directory,
        CONNPASS_API_KEY: BASE_ENV.CONNPASS_API_KEY,
        GOG_ACCOUNT: "private@example.com",
        DAIS_LEGAL_NAME_ROMAJI: "Dais Example",
        GEMINI_API_KEY: "fixture-ranking-key",
        GOG_KEYRING_PASSWORD: "private-keyring",
        LM_CONNECTOR_TELEGRAM_TARGET: "private-target",
        TELEGRAM_BOT_TOKEN: "inline-token-must-not-override-shared",
        LM_CONNECTOR_SHARED_ENV_FILE: sharedEnvFile,
        LM_CONNECTOR_TENANT_ID: "dais-local",
        LM_CONNECTOR_CALENDAR_ID: "primary",
      },
      createDependencies(input) {
        observed.push(["factory", input]);
        return Object.freeze({ boundary: "production" });
      },
      async runWake(input, dependencies) {
        observed.push(["wake", input, dependencies]);
        return Object.freeze({ status: "completed_no_effect", safe_reason: "providers_exhausted" });
      },
    });
    assert.deepEqual(result, { status: "completed_no_effect", safe_reason: "providers_exhausted" });
    assert.equal(observed[0][0], "factory");
    assert.equal(observed[0][1].calendarAccount, "private@example.com");
    assert.equal(observed[0][1].gogKeyring, "private-keyring");
    assert.equal(observed[0][1].telegramTarget, "private-target");
    assert.equal(observed[0][1].telegramToken, "fixture-telegram-token");
    assert.match(observed[0][1].eventPreferences, /YC.*Lightning Talk.*AI.*crypto.*startup/i);
    assert.equal(observed[0][1].geminiApiKey, "fixture-ranking-key");
    assert.equal(observed[0][1].connpassApiKey, BASE_ENV.CONNPASS_API_KEY);
    assert.equal(observed[0][1].connpassAutomatedSubmitAllowed, false);
    assert.match(observed[0][1].wakeId, /^wake-[0-9a-f]{24}$/);
    assert.equal(observed[0][1].wakeId.includes("native-pass-minimal-owner"), false);
    assert.deepEqual(observed[1][1].providers, ["luma", "connpass", "peatix", "meetup", "doorkeeper", "eventbrite", "techplay", "kokuchpro"]);
    assert.equal("geminiApiKey" in observed[1][1], false);
    assert.deepEqual(observed[1][2], { boundary: "production" });
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("native production config forwards the explicit Connpass submit opt-in", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-connpass-submit-opt-in-"));
  let factoryInput;
  try {
    writeKanaProfile(directory);
    const sharedEnvFile = writeSharedConnectorEnv(directory);
    fs.appendFileSync(sharedEnvFile, "LM_CONNECTOR_CONNPASS_AUTOMATED_SUBMIT_ALLOWED=true\n");
    await runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(directory, "state"),
      ownerToken: "native-pass-connpass-submit-opt-in-123456",
      env: { ...BASE_ENV, HOME: directory, LM_CONNECTOR_SHARED_ENV_FILE: sharedEnvFile },
      createDependencies(input) { factoryInput = input; return Object.freeze({ boundary: "production" }); },
      async runWake() { return { status: "completed_no_effect" }; },
    });
    assert.equal(factoryInput.connpassAutomatedSubmitAllowed, true);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("native production config fails closed when Telegram bot token is missing", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-no-telegram-token-"));
  let factoryCalls = 0;
  try {
    writeKanaProfile(directory);
    const { TELEGRAM_BOT_TOKEN: _token, ...env } = BASE_ENV;
    await assert.rejects(runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(directory, "state"),
      ownerToken: "native-pass-no-telegram-token-123456",
      env: { ...env, HOME: directory },
      createDependencies() { factoryCalls += 1; return {}; },
      async runWake() { return { status: "unexpected" }; },
    }), /Connector minimal pass unavailable/);
    assert.equal(factoryCalls, 0);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("native production config ignores an inline Telegram token when the shared env omits it", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-inline-telegram-token-"));
  let factoryCalls = 0;
  try {
    writeKanaProfile(directory);
    const sharedEnvFile = path.join(directory, "connector.env");
    fs.writeFileSync(sharedEnvFile, "\n", { mode: 0o600 });
    const env = { ...BASE_ENV, HOME: directory, LM_CONNECTOR_SHARED_ENV_FILE: sharedEnvFile };
    await assert.rejects(runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(directory, "state"),
      ownerToken: "native-pass-inline-telegram-token-123456",
      env,
      createDependencies() { factoryCalls += 1; return {}; },
      async runWake() { return { status: "unexpected" }; },
    }), /Connector minimal pass unavailable/);
    assert.equal(factoryCalls, 0);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("native Peatix profile is frozen at the factory boundary and invalid identity never reaches it", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-profile-"));
  const baseEnv = { ...BASE_ENV, HOME: directory };
  let factoryInput; const wakeInputs = [];
  try {
    writeKanaProfile(directory);
    const result = await runNativePass({ repoRoot: REPO_ROOT, stateDir: path.join(directory, "state"), ownerToken: "native-pass-profile-owner-123456", env: baseEnv, createDependencies(input) { factoryInput = input; return Object.freeze({ boundary: "production" }); }, async runWake(input) { wakeInputs.push(input); return { status: "completed_no_effect" }; } });
    assert.deepEqual(result, { status: "completed_no_effect" });
    assert.deepEqual(factoryInput.peatixAttendeeProfile, { name: "Dais Example", email: "private@example.com", given_name: "Dais", family_name: "Example", family_name_kana: VALID_KANA.family, given_name_kana: VALID_KANA.given, name_kanji: VALID_NAME_JA, name_hiragana: "さくら てすと", accept_organizer_privacy: true });
    assert.equal(Object.isFrozen(factoryInput), true);
    assert.equal(Object.isFrozen(factoryInput.peatixAttendeeProfile), true);
    assert.deepEqual(wakeInputs[0].providers, ["luma", "connpass", "peatix", "meetup", "doorkeeper", "eventbrite", "techplay", "kokuchpro"]);
    assert.equal("peatixAttendeeProfile" in wakeInputs[0], false);
    assert.doesNotMatch(JSON.stringify(wakeInputs[0]), /Dais Example|private@example\.com|family_name_kana|given_name_kana|name_kanji|name_hiragana|桜 太郎|さくら てすと|サクラ|テスト/);
    for (const override of [{ DAIS_LEGAL_NAME_ROMAJI: "" }, { DAIS_LEGAL_NAME_ROMAJI: "x".repeat(201) }, { GOG_ACCOUNT: "not-an-email" }]) {
      let called = false;
      await assert.rejects(runNativePass({ repoRoot: REPO_ROOT, stateDir: path.join(directory, "invalid-state"), ownerToken: "native-pass-invalid-owner-123456", env: { ...baseEnv, ...override }, createDependencies() { called = true; return {}; }, async runWake() { return { status: "unexpected" }; } }), /Connector minimal pass unavailable/);
      assert.equal(called, false);
    }
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("native Eventbrite attendee identity preserves the matching private name token case", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-eventbrite-identity-")); let profile; let wakeInput;
  try {
    writeKanaProfile(directory, VALID_KANA, 0o600, VALID_NAME_JA, { name: "Dais Example", preferred_name: "dAiS" });
    await runNativePass({ repoRoot: REPO_ROOT, stateDir: path.join(directory, "state"), ownerToken: "native-pass-eventbrite-identity-123456", env: { ...BASE_ENV, HOME: directory }, createDependencies(input) { profile = input.peatixAttendeeProfile; return {}; }, async runWake(input) { wakeInput = input; return { status: "completed_no_effect" }; } });
    assert.deepEqual({ given_name: profile.given_name, family_name: profile.family_name }, { given_name: "Dais", family_name: "Example" });
    assert.equal(profile.name, BASE_ENV.DAIS_LEGAL_NAME_ROMAJI);
    assert.equal("given_name" in wakeInput, false);
    assert.equal("family_name" in wakeInput, false);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("native Eventbrite attendee identity fails closed for mismatch and ambiguous preferred names", async () => {
  for (const [name, identity] of [
    ["legal-mismatch", { name: "Other Person", preferred_name: "Other" }],
    ["preferred-missing", { name: "Dais Example" }],
    ["preferred-nonmatching", { name: "Dais Example", preferred_name: "Other" }],
    ["preferred-ambiguous", { name: "Dais Example", preferred_name: "Dais Example" }],
    ["name-one-token", { name: "Dais", preferred_name: "Dais" }],
    ["name-control-padding", { name: "\nDais Example", preferred_name: "Dais" }],
    ["preferred-control-padding", { name: "Dais Example", preferred_name: "Dais\n" }],
    ["name-overlength-padding", { name: `Dais Example${" ".repeat(201)}`, preferred_name: "Dais" }],
    ["preferred-overlength-padding", { name: "Dais Example", preferred_name: `Dais${" ".repeat(201)}` }],
  ]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-eventbrite-identity-invalid-")); let factoryCalls = 0; let wakeCalls = 0;
    try {
      writeKanaProfile(directory, VALID_KANA, 0o600, VALID_NAME_JA, identity);
      await assert.rejects(runNativePass({ repoRoot: REPO_ROOT, stateDir: path.join(directory, "state"), ownerToken: "native-pass-eventbrite-identity-invalid-123456", env: { ...BASE_ENV, HOME: directory }, createDependencies() { factoryCalls += 1; return {}; }, async runWake() { wakeCalls += 1; return { status: "unexpected" }; } }), /Connector minimal pass unavailable/, name);
      assert.equal(factoryCalls, 0, name); assert.equal(wakeCalls, 0, name);
    } finally { fs.rmSync(directory, { recursive: true, force: true }); }
  }
});

test("native config requires the shared Telegram target instead of an OpenClaw owner file", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-owner-"));
  const ownerFile = path.join(directory, ".openclaw", "credentials", "telegram-default-allowFrom.json");
  fs.mkdirSync(path.dirname(ownerFile), { recursive: true, mode: 0o700 });
  fs.writeFileSync(ownerFile, `${JSON.stringify({ allowFrom: ["123456789"] })}\n`, { mode: 0o600 });
  writeKanaProfile(directory);
  let factoryInput;
  try {
    await runNativePass({
      repoRoot: REPO_ROOT,
      stateDir: path.join(directory, "state"),
      ownerToken: "native-pass-minimal-owner-123456",
      env: {
        HOME: directory,
        GOG_ACCOUNT: "private@example.com",
        DAIS_LEGAL_NAME_ROMAJI: "Dais Example",
        GEMINI_API_KEY: "fixture-ranking-key",
        GOG_KEYRING_PASSWORD: "private-keyring",
        LM_CONNECTOR_TELEGRAM_TARGET: "987654321",
        TELEGRAM_BOT_TOKEN: "fixture-telegram-token",
      },
      createDependencies(input) {
        factoryInput = input;
        return Object.freeze({ boundary: "production" });
      },
      async runWake() { return { status: "completed_no_effect" }; },
    });
    assert.equal(factoryInput.telegramTarget, "987654321");
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("native config fails closed when the shared Telegram target is missing even if an OpenClaw owner file exists", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-owner-fallback-forbidden-"));
  const ownerFile = path.join(directory, ".openclaw", "credentials", "telegram-default-allowFrom.json");
  fs.mkdirSync(path.dirname(ownerFile), { recursive: true, mode: 0o700 });
  fs.writeFileSync(ownerFile, `${JSON.stringify({ allowFrom: ["123456789"] })}\n`, { mode: 0o600 });
  writeKanaProfile(directory);
  let factoryCalls = 0;
  try {
    await assert.rejects(runNativePass({
      repoRoot: REPO_ROOT, stateDir: path.join(directory, "state"), ownerToken: "native-pass-owner-fallback-forbidden-123456",
      env: { ...BASE_ENV, HOME: directory, LM_CONNECTOR_TELEGRAM_TARGET: undefined },
      createDependencies() { factoryCalls += 1; return {}; },
      async runWake() { return { status: "unexpected" }; },
    }), /Connector minimal pass unavailable/);
    assert.equal(factoryCalls, 0);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("native Kana identity fails closed before dependency or wake creation", async () => {
  const invalidCases = [
    ["missing file"], ["permissive mode", VALID_KANA, 0o644], ["invalid JSON", "{"],
    ["missing family", { given: VALID_KANA.given }], ["missing given", { family: VALID_KANA.family }],
    ["empty value", { family: "", given: VALID_KANA.given }], ["Hiragana", { family: "さくら", given: VALID_KANA.given }],
    ["Kanji", { family: "桜", given: VALID_KANA.given }], ["Latin", { family: "Sakura", given: VALID_KANA.given }],
    ["digit", { family: "サク1", given: VALID_KANA.given }], ["punctuation", { family: "サク!", given: VALID_KANA.given }],
    ["control", { family: "サク\nラ", given: VALID_KANA.given }], ["outside-katakana-range", { family: "ヷ", given: VALID_KANA.given }], ["overlength", { family: "サ".repeat(101), given: VALID_KANA.given }],
  ];
  for (const [name, value, mode] of invalidCases) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-kana-invalid-")); let factoryCalls = 0; let wakeCalls = 0;
    try {
      if (value !== undefined) writeKanaProfile(directory, value, mode);
      await assert.rejects(runNativePass({
        repoRoot: REPO_ROOT,
        stateDir: path.join(directory, "state"),
        ownerToken: "native-pass-kana-invalid-owner-123456",
        env: { ...BASE_ENV, HOME: directory },
        createDependencies() { factoryCalls += 1; return {}; },
        async runWake() { wakeCalls += 1; return { status: "unexpected" }; },
      }), /Connector minimal pass unavailable/, name);
      assert.equal(factoryCalls, 0, name); assert.equal(wakeCalls, 0, name);
    } finally {
      fs.rmSync(directory, { recursive: true, force: true });
    }
  }
});

test("native Japanese identity is required, bounded, and control-free", async () => {
  const invalidCases = [["missing", undefined], ["empty", ""], ["whitespace", "   "], ["padded", " 桜 太郎"], ["overlength", "桜".repeat(201)], ["control", "桜\n太郎"], ["c1-control", "桜\u0080太郎"], ["non-string", { family: "桜", given: "太郎" }]];
  for (const [name, nameJa] of invalidCases) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-name-ja-invalid-")); let factoryCalls = 0; let wakeCalls = 0;
    try {
      writeKanaProfile(directory, VALID_KANA, 0o600, nameJa);
      await assert.rejects(runNativePass({ repoRoot: REPO_ROOT, stateDir: path.join(directory, "state"), ownerToken: "native-pass-name-ja-invalid-owner-123456", env: { ...BASE_ENV, HOME: directory }, createDependencies() { factoryCalls += 1; return {}; }, async runWake() { wakeCalls += 1; return { status: "unexpected" }; } }), /Connector minimal pass unavailable/, name);
      assert.equal(factoryCalls, 0, name); assert.equal(wakeCalls, 0, name);
    } finally { fs.rmSync(directory, { recursive: true, force: true }); }
  }
});

test("native Japanese identity preserves the Katakana long-vowel mark", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "connector-native-name-ja-long-vowel-")); let profile;
  try { writeKanaProfile(directory, { family: "サー", given: "テスト" }, 0o600, "佐藤 太郎"); await runNativePass({ repoRoot: REPO_ROOT, stateDir: path.join(directory, "state"), ownerToken: "native-pass-name-ja-long-vowel-123456", env: { ...BASE_ENV, HOME: directory }, createDependencies(input) { profile = input.peatixAttendeeProfile; return {}; }, async runWake() { return { status: "completed_no_effect" }; } }); assert.equal(profile.name_hiragana, "さー てすと"); } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
