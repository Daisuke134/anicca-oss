"use strict";

const assert = require("node:assert/strict");
const { once } = require("node:events");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createMarketingLocalLedger } = require("./marketing-local-ledger.js");
const {
  createMarketingLaneManifest,
  writeMarketingLaneManifest,
} = require("./marketing-lane-manifest.js");

const PROMOTION_CONFIRMATION = "PROMOTE_HONNE_EN_TIKTOK_CANARY";
const PUBLIC_URL = "https://www.tiktok.com/@honne_reveal/video/7999999999999999999";

function job(overrides = {}) {
  return {
    job_id: "publication-job",
    tenant_id: "dais-local",
    loop_id: "marketing.video",
    capability: "marketing.video.publish",
    effect_class: "publish",
    effect_key: "tiktok:honne-ai:en:2026-08-21T02:00:00.000Z",
    input_refs: {
      product_ref: "product://honne-ai",
      locale_ref: "locale://en",
      platform_ref: "platform://tiktok",
    },
    max_attempts: 3,
    available_at: "9999-12-31T23:59:59.000Z",
    ...overrides,
  };
}

function tempDataDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "lm-marketing-ledger-"));
}

function writeLanePolicy(dataDir, laneState) {
  const lane = {
    id: "cmoig11ew001zlv0yk6vqo1us",
    provider: "postiz",
    platform: "tiktok",
    profile: "@honne_reveal",
    account: "honne-en",
    product_id: "honne-ai",
    locale: "en",
    disabled: false,
    verified: true,
    owner: "life-manager",
    lane_state: laneState,
    disposition: "target",
    renderer: "reelclaw",
    format: "relationship-confession",
    approved_pack: "honne-ai-reelclaw-en.pack.json",
    canary_state: "verified",
    target_daily_limit: 3,
  };
  const manifest = createMarketingLaneManifest({
    tenant_id: "dais-local",
    integrations: [lane],
    holds: [{
      integration_id: "live-x-hold",
      platform: "x",
      account: "@aniccaxxx",
      provider: "postiz",
      provider_disabled: false,
      owner: "life-manager",
      disposition: "hold",
      target_daily_limit: 0,
      verified: true,
    }],
  }, { tenantId: "dais-local", assignments: [lane] });
  writeMarketingLaneManifest(manifest, { dataDir });
}

test("local ledger uses the portable runtime data root and rejects legacy roots", () => {
  const home = tempDataDir();
  const ledger = createMarketingLocalLedger({ env: { HOME: home, LM_DATA_DIR: "" } });
  assert.equal(ledger.dataDir, path.join(home, ".local", "state", "life-manager"));
  assert.throws(
    () => createMarketingLocalLedger({ dataDir: "/tmp/.openclaw/marketing" }),
    /legacy runtime root/i,
  );
  assert.throws(
    () => createMarketingLocalLedger({ dataDir: "/srv/anicca/marketing" }),
    /legacy runtime root/i,
  );
});

test("local JSONL jobs project through the common Job contract without rewriting storage", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir });
  await ledger.enqueueJob(job());

  assert.deepEqual(await ledger.readCommonJob({ tenantId: "dais-local", jobId: "publication-job" }), {
    schema_version: 1,
    record_type: "job",
    job_id: "publication-job",
    tenant_id: "dais-local",
    loop_id: "marketing.video",
    capability: "marketing.video.publish",
    effect_class: "publish",
    effect_key: "tiktok:honne-ai:en:2026-08-21T02:00:00.000Z",
    input_refs: {
      product_ref: "product://honne-ai",
      locale_ref: "locale://en",
      platform_ref: "platform://tiktok",
    },
    max_attempts: 3,
  });
  assert.match(
    fs.readFileSync(path.join(dataDir, "marketing", "jobs.jsonl"), "utf8"),
    /"available_at":"9999-12-31T23:59:59.000Z"/,
  );
});

test("closed publication effect fence rejects a new provider job and records the refusal", async () => {
  const dataDir = tempDataDir();
  const marketingDir = path.join(dataDir, "marketing");
  fs.mkdirSync(marketingDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(marketingDir, "publication-effect-fence.json"), `${JSON.stringify({
    schema_version: 1,
    state: "closed",
    reason: "MKT-09 incident recovery",
  })}\n`, { mode: 0o600 });
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-26T01:30:00.000Z" });

  await assert.rejects(
    ledger.enqueueJob(job({ available_at: "2026-08-26T01:30:00.000Z" })),
    (error) => error.code === "MARKETING_PUBLICATION_EFFECT_FENCED",
  );

  assert.equal(await ledger.readJob({ tenantId: "dais-local", jobId: "publication-job" }), null);
  const [refusal] = fs.readFileSync(path.join(marketingDir, "publication-effect-fence-refusals.jsonl"), "utf8")
    .trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(refusal.phase, "enqueue");
  assert.equal(refusal.effect_key, "tiktok:honne-ai:en:2026-08-21T02:00:00.000Z");
  assert.equal(refusal.reason, "MKT-09 incident recovery");
});

test("closed publication effect fence rejects claim of an already queued provider job", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-26T01:31:00.000Z" });
  await ledger.enqueueJob(job({ available_at: "2026-08-26T01:31:00.000Z" }));
  const fenceFile = path.join(dataDir, "marketing", "publication-effect-fence.json");
  fs.writeFileSync(fenceFile, `${JSON.stringify({
    schema_version: 1,
    state: "closed",
    reason: "MKT-09 incident recovery",
  })}\n`, { mode: 0o600 });

  await assert.rejects(
    ledger.claimJob({
      tenantId: "dais-local",
      jobId: "publication-job",
      capability: "marketing.video.publish",
      workerId: "fenced-worker",
      leaseSeconds: 30,
    }),
    (error) => error.code === "MARKETING_PUBLICATION_EFFECT_FENCED",
  );

  assert.equal((await ledger.readJob({ tenantId: "dais-local", jobId: "publication-job" })).status, "queued");
});

test("closed canary fence allows an exact production-armed lane", async () => {
  const dataDir = tempDataDir();
  const marketingDir = path.join(dataDir, "marketing");
  fs.mkdirSync(marketingDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(marketingDir, "publication-effect-fence.json"), `${JSON.stringify({
    schema_version: 1,
    state: "closed",
    reason: "canary effects closed; production manifest owns cadence",
  })}\n`, { mode: 0o600 });
  writeLanePolicy(dataDir, "production-armed");
  const value = job({
    available_at: "2026-08-26T01:31:00.000Z",
    input_refs: {
      ...job().input_refs,
      format_ref: "format://reelclaw",
      form_ref: "form://relationship-confession",
      tiktok_integration_ref: "integration://postiz/tiktok/cmoig11ew001zlv0yk6vqo1us",
    },
  });
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-26T01:31:00.000Z" });

  assert.equal((await ledger.enqueueJob(value)).created, true);
  assert.equal((await ledger.claimJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    capability: "marketing.video.publish",
    workerId: "production-worker",
    leaseSeconds: 30,
  })).status, "running");
});

test("an armed integration rejects content from the wrong product format family", async () => {
  const dataDir = tempDataDir();
  const marketingDir = path.join(dataDir, "marketing");
  fs.mkdirSync(marketingDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(marketingDir, "publication-effect-fence.json"), `${JSON.stringify({
    schema_version: 1,
    state: "closed",
    reason: "production manifest owns cadence",
  })}\n`, { mode: 0o600 });
  writeLanePolicy(dataDir, "production-armed");
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-26T01:31:00.000Z" });
  const wrongFamily = job({
    available_at: "2026-08-26T01:31:00.000Z",
    input_refs: {
      ...job().input_refs,
      format_ref: "format://reelclaw-card",
      form_ref: "form://nudge-card",
      tiktok_integration_ref: "integration://postiz/tiktok/cmoig11ew001zlv0yk6vqo1us",
    },
  });
  await assert.rejects(
    ledger.enqueueJob(wrongFamily),
    (error) => error.code === "MARKETING_PUBLICATION_EFFECT_FENCED",
  );
  assert.equal(await ledger.readJob({ tenantId: "dais-local", jobId: "publication-job" }), null);
});

test("open publication fence still requires the exact armed Life Manager lane at enqueue and claim", async () => {
  const dataDir = tempDataDir();
  const marketingDir = path.join(dataDir, "marketing");
  fs.mkdirSync(marketingDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(marketingDir, "publication-effect-fence.json"), `${JSON.stringify({
    schema_version: 1,
    state: "open",
    allowed_effect_key: "tiktok:honne-ai:en:2026-08-21T02:00:00.000Z",
    reason: "one canary",
  })}\n`, { mode: 0o600 });
  const value = job({
    available_at: "2026-08-26T01:31:00.000Z",
    input_refs: {
      ...job().input_refs,
      format_ref: "format://reelclaw",
      form_ref: "form://relationship-confession",
      tiktok_integration_ref: "integration://postiz/tiktok/cmoig11ew001zlv0yk6vqo1us",
    },
  });
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-26T01:31:00.000Z" });

  writeLanePolicy(dataDir, "default-off");
  await assert.rejects(
    ledger.enqueueJob(value),
    (error) => error.code === "MARKETING_PUBLICATION_LANE_FORBIDDEN",
  );

  writeLanePolicy(dataDir, "production-armed");
  assert.equal((await ledger.enqueueJob(value)).created, true);

  writeLanePolicy(dataDir, "default-off");
  await assert.rejects(
    ledger.claimJob({
      tenantId: "dais-local",
      jobId: "publication-job",
      capability: "marketing.video.publish",
      workerId: "policy-worker",
      leaseSeconds: 30,
    }),
    (error) => error.code === "MARKETING_PUBLICATION_LANE_FORBIDDEN",
  );
  assert.equal((await ledger.readJob({ tenantId: "dais-local", jobId: "publication-job" })).status, "queued");
});

test("receipt identity is tenant-scoped even when identifiers contain separators", async () => {
  const ledger = createMarketingLocalLedger({
    dataDir: tempDataDir(),
    now: () => "2026-08-21T02:00:00.000Z",
  });
  await ledger.enqueueJob(job({
    job_id: "colon-job",
    tenant_id: "tenant:one",
    effect_key: "tiktok:colon-scope",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  const claimed = await ledger.claimJob({
    tenantId: "tenant:one",
    jobId: "colon-job",
    capability: "marketing.video.publish",
    workerId: "colon-worker",
    leaseSeconds: 30,
  });
  const receipt = { status: "published", public_url: PUBLIC_URL };
  await ledger.completeJob({
    tenantId: "tenant:one",
    jobId: "colon-job",
    attempt: claimed.attempt,
    workerId: "colon-worker",
    receipt,
  });
  assert.deepEqual(
    await ledger.readReceipt({ tenantId: "tenant:one", jobId: "colon-job" }),
    receipt,
  );
  assert.equal(await ledger.readReceipt({ tenantId: "tenant", jobId: "one:colon-job" }), null);
});

test("a completed external receipt preserves lineage while correcting only its direct URL", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-22T13:50:00.000Z" });
  await ledger.enqueueJob(job({ available_at: "2026-08-22T13:49:00.000Z" }));
  const claim = await ledger.claimJob({ tenantId: "dais-local", jobId: "publication-job", capability: "marketing.video.publish", workerId: "worker", leaseSeconds: 30 });
  const receipt = { status: "published", provider_post_id: "post-1", caption_sha256: "a".repeat(64), public_url: PUBLIC_URL };
  await ledger.completeJob({ tenantId: "dais-local", jobId: "publication-job", attempt: claim.attempt, workerId: "worker", receipt });
  const corrected = { ...receipt, public_url: "https://www.tiktok.com/@honne_reveal/video/7888888888888888888" };
  await ledger.correctReceiptDirectUrl({ tenantId: "dais-local", jobId: "publication-job", confirmation: "CORRECT_CAPTION_MATCHED_DIRECT_URL", receipt: corrected });
  assert.deepEqual(await ledger.readReceipt({ tenantId: "dais-local", jobId: "publication-job" }), corrected);
  await assert.rejects(() => ledger.correctReceiptDirectUrl({ tenantId: "dais-local", jobId: "publication-job", confirmation: "CORRECT_CAPTION_MATCHED_DIRECT_URL", receipt: { ...corrected, provider_post_id: "post-2" } }), /only replace the direct URL/i);
  assert.equal(fs.readFileSync(path.join(dataDir, "marketing", "receipts.jsonl"), "utf8").trim().split("\n").length, 2);
});

test("a caption-collision receipt becomes a durable non-retryable conflict without erasing history", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-26T02:00:00.000Z" });
  const firstReceipt = {
    status: "published",
    provider_post_id: "postiz-jp4-1",
    video_sha256: "a".repeat(64),
    caption_sha256: "c".repeat(64),
    public_url: "https://www.tiktok.com/@anicca.jp4/video/7677106804039355656",
  };
  const secondReceipt = { ...firstReceipt, video_sha256: "b".repeat(64) };

  for (const [jobId, effectKey, receipt] of [
    ["jp4-first", "tiktok:jp4:first", firstReceipt],
    ["jp4-second", "tiktok:jp4:second", secondReceipt],
  ]) {
    await ledger.enqueueJob(job({
      job_id: jobId,
      effect_key: effectKey,
      available_at: "2026-08-26T01:59:00.000Z",
    }));
    const claim = await ledger.claimJob({
      tenantId: "dais-local", jobId, capability: "marketing.video.publish",
      workerId: "worker", leaseSeconds: 30,
    });
    await ledger.completeJob({
      tenantId: "dais-local", jobId, attempt: claim.attempt, workerId: "worker", receipt,
    });
  }

  const input = {
    tenantId: "dais-local",
    jobId: "jp4-second",
    confirmation: "QUARANTINE_CONFIRMED_EFFECT_CONFLICT",
    expectedReceipt: secondReceipt,
    reason: "caption_only_provider_reuse_conflicts_with_video_lineage",
    conflictsWithJobId: "jp4-first",
  };
  const conflicted = await ledger.quarantineCompletedEffectConflict(input);
  assert.equal(conflicted.status, "conflict");
  assert.equal(conflicted.unknown_effect, false);
  assert.equal(conflicted.conflicts_with_job_id, "jp4-first");
  assert.deepEqual(await ledger.readReceipt({ tenantId: "dais-local", jobId: "jp4-first" }), firstReceipt);
  assert.deepEqual(await ledger.readReceipt({ tenantId: "dais-local", jobId: "jp4-second" }), {
    schema_version: 1,
    kind: "marketing_effect_conflict",
    status: "conflict",
    reason: input.reason,
    conflicts_with_job_id: "jp4-first",
    superseded_receipt: secondReceipt,
    quarantined_at: "2026-08-26T02:00:00.000Z",
  });

  const replay = await ledger.quarantineCompletedEffectConflict(input);
  assert.deepEqual(replay, conflicted);
  const history = fs.readFileSync(path.join(dataDir, "marketing", "receipts.jsonl"), "utf8")
    .trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(history.length, 3);
  assert.deepEqual(history[1].receipt, secondReceipt);
  assert.equal(history[2].receipt.status, "conflict");
});

test("JSONL partial tails recover while a complete no-newline record remains appendable", async () => {
  const dataDir = tempDataDir();
  const first = createMarketingLocalLedger({
    dataDir,
    now: () => "2026-08-21T02:00:00.000Z",
  });
  await first.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  const jobsFile = path.join(dataDir, "marketing", "jobs.jsonl");
  fs.appendFileSync(jobsFile, '{"schema_version":1,"kind":"job"');

  const restarted = createMarketingLocalLedger({
    dataDir,
    now: () => "2026-08-21T02:00:00.000Z",
  });
  assert.deepEqual(
    (await restarted.readJob({ tenantId: "dais-local", jobId: "publication-job" })).job_id,
    "publication-job",
  );
  const replay = await restarted.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  assert.equal(replay.created, false);
  assert.doesNotMatch(fs.readFileSync(jobsFile, "utf8"), /\{"schema_version":1,"kind":"job"\}\s*$/);

  const second = await restarted.enqueueJob(job({
    job_id: "second-job",
    effect_key: "tiktok:honne-ai:en:second-slot",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  assert.equal(second.created, true);
  const records = fs.readFileSync(jobsFile, "utf8").trimEnd().split("\n").map((line) => JSON.parse(line));
  assert.equal(records.at(-1).job.job_id, "second-job");

  const completeNoNewline = `${JSON.stringify({ schema_version: 1, kind: "job", event: "marker", job: job({
    job_id: "complete-tail",
    effect_key: "tiktok:honne-ai:en:complete-tail",
    available_at: "2026-08-21T02:00:00.000Z",
  }) })}`;
  fs.writeFileSync(jobsFile, `${completeNoNewline}`);
  const completeTail = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  assert.equal((await completeTail.readJob({ tenantId: "dais-local", jobId: "complete-tail" })).job_id, "complete-tail");
  await completeTail.enqueueJob(job({
    job_id: "after-complete-tail",
    effect_key: "tiktok:honne-ai:en:after-complete-tail",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  const repaired = fs.readFileSync(jobsFile, "utf8").trimEnd().split("\n").map((line) => JSON.parse(line));
  assert.equal(repaired.at(-1).job.job_id, "after-complete-tail");
});

test("mid-file malformed JSON remains fail-closed", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  const jobsFile = path.join(dataDir, "marketing", "jobs.jsonl");
  fs.appendFileSync(jobsFile, '{"broken":\n{"schema_version":1,"kind":"job"}\n');
  const restarted = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await assert.rejects(
    restarted.readJob({ tenantId: "dais-local", jobId: "publication-job" }),
    /ledger is invalid/i,
  );
});

test("receipt JSONL partial tails do not hide the atomic receipt on restart", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  const claimed = await ledger.claimJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    capability: "marketing.video.publish",
    workerId: "receipt-worker",
    leaseSeconds: 30,
  });
  const receipt = { status: "published", public_url: "https://www.tiktok.com/@honne_reveal/video/1" };
  await ledger.completeJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    attempt: claimed.attempt,
    workerId: "receipt-worker",
    receipt,
  });
  fs.appendFileSync(path.join(dataDir, "marketing", "receipts.jsonl"), '{"schema_version":1,"kind":"receipt"');
  const restarted = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  assert.deepEqual(await restarted.readReceipt({ tenantId: "dais-local", jobId: "publication-job" }), receipt);
});

test("a stale partial-tail observation cannot truncate another process's valid append", async () => {
  const dataDir = tempDataDir();
  const base = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await base.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  const jobsFile = path.join(dataDir, "marketing", "jobs.jsonl");
  fs.appendFileSync(jobsFile, '{"schema_version":1,"kind":"job"');
  const staleReader = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await staleReader.readJob({ tenantId: "dais-local", jobId: "publication-job" });
  const writer = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await writer.enqueueJob(job({
    job_id: "writer-job",
    effect_key: "tiktok:honne-ai:en:writer-job",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  await staleReader.enqueueJob(job({
    job_id: "reader-job",
    effect_key: "tiktok:honne-ai:en:reader-job",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  const records = fs.readFileSync(jobsFile, "utf8").trimEnd().split("\n").map((line) => JSON.parse(line));
  assert.ok(records.some((record) => record.job.job_id === "writer-job"));
  assert.ok(records.some((record) => record.job.job_id === "reader-job"));
});

test("a live stale lock owner is never reclaimed across processes", async () => {
  const dataDir = tempDataDir();
  const lockFile = path.join(dataDir, "marketing", ".ledger.lock");
  fs.mkdirSync(path.dirname(lockFile), { recursive: true });
  const child = spawn(process.execPath, ["-e", `
    const fs = require('node:fs');
    const file = process.argv[1];
    const fd = fs.openSync(file, 'wx', 0o600);
    fs.writeFileSync(fd, JSON.stringify({schema_version: 1, token: 'child-token', pid: process.pid, acquired_at: new Date().toISOString()}));
    fs.fsyncSync(fd);
    const old = new Date(Date.now() - 60000);
    fs.utimesSync(file, old, old);
    process.stdout.write('ready\\n');
    setTimeout(() => {}, 5000);
  `, lockFile], { stdio: ["ignore", "pipe", "pipe"] });
  await once(child.stdout, "data");
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await assert.rejects(
    ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" })),
    /lock timeout|lock owner/i,
  );
  child.kill("SIGTERM");
  await once(child, "exit");
  const recovered = await ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  assert.equal(recovered.created, true);
});

test("malformed stale lock fails closed instead of being reclaimed", async () => {
  const dataDir = tempDataDir();
  const lockFile = path.join(dataDir, "marketing", ".ledger.lock");
  fs.mkdirSync(path.dirname(lockFile), { recursive: true });
  fs.writeFileSync(lockFile, "not-json", { mode: 0o600 });
  const old = new Date(Date.now() - 60000);
  fs.utimesSync(lockFile, old, old);
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await assert.rejects(
    ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" })),
    /lock record invalid|lock owner/i,
  );
});

test("a dead lock owner is reclaimed immediately even when its lock is fresh", async () => {
  const dataDir = tempDataDir();
  const lockFile = path.join(dataDir, "marketing", ".ledger.lock");
  fs.mkdirSync(path.dirname(lockFile), { recursive: true });
  fs.writeFileSync(lockFile, JSON.stringify({
    schema_version: 1,
    token: "dead-token",
    pid: 99999999,
    acquired_at: new Date().toISOString(),
  }), { mode: 0o600 });
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  const recovered = await ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  assert.equal(recovered.created, true);
});

test("a dead reclaim mutex fails closed instead of being path-reclaimed", async () => {
  const dataDir = tempDataDir();
  const lockFile = path.join(dataDir, "marketing", ".ledger.lock");
  const reclaimLockFile = path.join(dataDir, "marketing", ".ledger.reclaim");
  fs.mkdirSync(path.dirname(reclaimLockFile), { recursive: true });
  fs.writeFileSync(lockFile, JSON.stringify({
    schema_version: 1,
    token: "dead-main-token",
    pid: 99999999,
    acquired_at: new Date().toISOString(),
  }), { mode: 0o600 });
  fs.writeFileSync(reclaimLockFile, JSON.stringify({
    schema_version: 1,
    token: "dead-reclaim-token",
    pid: 99999999,
    acquired_at: new Date().toISOString(),
  }), { mode: 0o600 });
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await assert.rejects(
    ledger.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" })),
    /reclaim lock timeout/i,
  );
});

test("concurrent dead-lock reclaimers serialize so one external job is claimed", async () => {
  const dataDir = tempDataDir();
  const base = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await base.enqueueJob(job({ available_at: "2026-08-21T02:00:00.000Z" }));
  const lockFile = path.join(dataDir, "marketing", ".ledger.lock");
  fs.writeFileSync(lockFile, JSON.stringify({
    schema_version: 1,
    token: "dead-concurrent-token",
    pid: 99999999,
    acquired_at: new Date().toISOString(),
  }), { mode: 0o600 });
  const modulePath = path.join(__dirname, "marketing-local-ledger.js");
  const childScript = `
    const { createMarketingLocalLedger } = require(process.argv[1]);
    (async () => {
      try {
        const ledger = createMarketingLocalLedger({ dataDir: process.argv[2], now: () => '2026-08-21T02:00:00.000Z' });
        const result = await ledger.claimJob({ tenantId: 'dais-local', jobId: 'publication-job', capability: 'marketing.video.publish', workerId: process.argv[3], leaseSeconds: 30 });
        process.stdout.write(JSON.stringify({ result }) + '\\n');
      } catch (error) {
        process.stderr.write(String(error && error.message || error));
        process.exitCode = 2;
      }
    })();
  `;
  const run = (workerId) => new Promise((resolve) => {
    const child = spawn(process.execPath, ["-e", childScript, modulePath, dataDir, workerId], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
  const results = await Promise.all([run("concurrent-worker-a"), run("concurrent-worker-b")]);
  const claims = results
    .filter((result) => result.code === 0)
    .map((result) => JSON.parse(result.stdout).result)
    .filter(Boolean);
  assert.equal(claims.length, 1, results.map((result) => result.stderr).join(" | "));
  assert.equal(claims[0].attempt, 1);
  assert.equal((await base.readJob({ tenantId: "dais-local", jobId: "publication-job" })).attempt, 1);
});

test("local ledger is idempotent, collision-safe, promotes and claims exactly once, and survives restart", async () => {
  const dataDir = tempDataDir();
  const first = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });

  const created = await first.enqueueJob(job());
  assert.equal(created.created, true);
  const replay = await first.enqueueJob(job());
  assert.equal(replay.created, false);
  assert.equal(replay.job.job_id, "publication-job");
  await assert.rejects(
    first.enqueueJob(job({ effect_key: "tiktok:honne-ai:en:another-slot" })),
    /job id collision/i,
  );
  await assert.rejects(
    first.enqueueJob(job({ job_id: "other-job" })),
    /effect collision/i,
  );
  const otherTenant = await first.enqueueJob(job({
    job_id: "other-tenant-job",
    tenant_id: "other-tenant",
  }));
  assert.equal(otherTenant.created, true);

  const promoted = await first.promoteJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    confirmation: PROMOTION_CONFIRMATION,
  });
  assert.equal(promoted.available_at, "2026-08-21T02:00:00.000Z");

  const claimed = await first.claimJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    capability: "marketing.video.publish",
    workerId: "canary-worker",
    leaseSeconds: 180,
  });
  assert.equal(claimed.status, "running");
  assert.equal(claimed.attempt, 1);
  assert.equal(await first.claimJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    capability: "marketing.video.publish",
    workerId: "second-worker",
    leaseSeconds: 180,
  }), null);

  const heartbeat = await first.heartbeatJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    attempt: 1,
    workerId: "canary-worker",
    leaseSeconds: 180,
  });
  assert.equal(heartbeat.status, "running");

  const receipt = {
    schema_version: 1,
    kind: "marketing_video_distribution",
    status: "published",
    product_id: "honne-ai",
    locale: "en",
    platform: "tiktok",
    public_url: PUBLIC_URL,
    provider_post_id: "postiz-7999999999999999999",
    provider_reconciled: true,
  };
  const completed = await first.completeJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    attempt: 1,
    workerId: "canary-worker",
    receipt,
  });
  assert.equal(completed.status, "completed");
  assert.deepEqual(await first.readReceipt({ tenantId: "dais-local", jobId: "publication-job" }), receipt);

  const restarted = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:03:00.000Z" });
  assert.deepEqual(await restarted.readReceipt({ tenantId: "dais-local", jobId: "publication-job" }), receipt);
  assert.equal(await restarted.claimJob({
    tenantId: "dais-local",
    jobId: "publication-job",
    capability: "marketing.video.publish",
    workerId: "restarted-worker",
    leaseSeconds: 180,
  }), null);
});

test("local ledger retains truthful unavailable receipts and unknown failures without converting them", async () => {
  const dataDir = tempDataDir();
  const ledger = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:00:00.000Z" });
  await ledger.enqueueJob(job({
    job_id: "telegram-job",
    capability: "marketing.liveness.telegram",
    effect_class: "message",
    effect_key: "telegram:marketing-liveness:slot-1",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  const claimed = await ledger.claimJob({
    tenantId: "dais-local",
    jobId: "telegram-job",
    capability: "marketing.liveness.telegram",
    workerId: "canary-worker",
    leaseSeconds: 180,
  });
  assert.equal(claimed.status, "running");
  const unavailable = {
    schema_version: 1,
    kind: "telegram_marketing_liveness",
    status: "missed",
    public_url: "unavailable",
    retry_state: "unavailable",
  };
  await ledger.completeJob({
    tenantId: "dais-local",
    jobId: "telegram-job",
    attempt: claimed.attempt,
    workerId: "canary-worker",
    receipt: unavailable,
  });
  const restarted = createMarketingLocalLedger({ dataDir, now: () => "2026-08-21T02:03:00.000Z" });
  assert.deepEqual(await restarted.readReceipt({ tenantId: "dais-local", jobId: "telegram-job" }), unavailable);
  assert.equal((await restarted.readReceipt({ tenantId: "dais-local", jobId: "telegram-job" })).status, "missed");

  await restarted.enqueueJob(job({
    job_id: "unknown-job",
    available_at: "2026-08-21T02:00:00.000Z",
    effect_key: "tiktok:honne-ai:en:unknown-slot",
  }));
  const unknownClaim = await restarted.claimJob({
    tenantId: "dais-local",
    jobId: "unknown-job",
    capability: "marketing.video.publish",
    workerId: "canary-worker",
    leaseSeconds: 180,
  });
  const failed = await restarted.failJob({
    tenantId: "dais-local",
    jobId: "unknown-job",
    attempt: unknownClaim.attempt,
    workerId: "canary-worker",
    errorCode: "provider_unknown",
    unknownEffect: true,
  });
  assert.equal(failed.status, "failed");
  assert.equal(failed.unknown_effect, true);
  assert.equal(await restarted.readReceipt({ tenantId: "dais-local", jobId: "unknown-job" }), null);
  await assert.rejects(
    () => restarted.retryJob({ tenantId: "dais-local", jobId: "unknown-job" }),
    /not safely retryable/i,
  );

  await restarted.enqueueJob(job({
    job_id: "known-failure-job",
    effect_key: "tiktok:honne-ai:en:known-failure",
    available_at: "2026-08-21T02:00:00.000Z",
  }));
  const knownClaim = await restarted.claimJob({
    tenantId: "dais-local", jobId: "known-failure-job",
    capability: "marketing.video.publish", workerId: "canary-worker", leaseSeconds: 180,
  });
  await restarted.failJob({
    tenantId: "dais-local", jobId: "known-failure-job", attempt: knownClaim.attempt,
    workerId: "canary-worker", errorCode: "provider_preflight", unknownEffect: false,
  });
  const requeued = await restarted.retryJob({ tenantId: "dais-local", jobId: "known-failure-job" });
  assert.equal(requeued.status, "queued");
  const retryClaim = await restarted.claimJob({
    tenantId: "dais-local", jobId: "known-failure-job",
    capability: "marketing.video.publish", workerId: "retry-worker", leaseSeconds: 180,
  });
  assert.equal(retryClaim.attempt, 2);

  const reconciledReceipt = {
    schema_version: 1,
    kind: "marketing_video_distribution",
    status: "published",
    public_url: PUBLIC_URL,
    provider_reconciled: true,
  };
  const reconciled = await restarted.resolveReconciliation({
    tenantId: "dais-local",
    jobId: "unknown-job",
    attempt: unknownClaim.attempt,
    decision: "present",
    receipt: reconciledReceipt,
  });
  assert.equal(reconciled.status, "completed");
  assert.equal(reconciled.reconciled_from_unknown, true);
  assert.deepEqual(
    await restarted.readReceipt({ tenantId: "dais-local", jobId: "unknown-job" }),
    reconciledReceipt,
  );
  const replay = await restarted.resolveReconciliation({
    tenantId: "dais-local",
    jobId: "unknown-job",
    attempt: unknownClaim.attempt,
    decision: "present",
    receipt: reconciledReceipt,
  });
  assert.equal(replay.status, "completed");
});

test("expired running claim is recovered once after restart and remains bounded", async () => {
  const dataDir = tempDataDir();
  let clock = "2026-08-21T02:00:00.000Z";
  const first = createMarketingLocalLedger({ dataDir, now: () => clock });
  await first.enqueueJob(job({
    job_id: "restart-job",
    capability: "runtime.noop",
    effect_class: "none",
    effect_key: null,
    available_at: clock,
    max_attempts: 2,
  }));
  const initial = await first.claimJob({
    tenantId: "dais-local",
    jobId: "restart-job",
    capability: "runtime.noop",
    workerId: "first-worker",
    leaseSeconds: 30,
  });
  assert.equal(initial.attempt, 1);

  clock = "2026-08-21T02:00:31.000Z";
  const restarted = createMarketingLocalLedger({ dataDir, now: () => clock });
  const recovered = await restarted.claimJob({
    tenantId: "dais-local",
    jobId: "restart-job",
    capability: "runtime.noop",
    workerId: "restarted-worker",
    leaseSeconds: 30,
  });
  assert.equal(recovered.attempt, 2);
  assert.equal(await restarted.claimJob({
    tenantId: "dais-local",
    jobId: "restart-job",
    capability: "runtime.noop",
    workerId: "third-worker",
    leaseSeconds: 30,
  }), null);

  clock = "2026-08-21T02:01:02.000Z";
  assert.equal(await restarted.claimJob({
    tenantId: "dais-local",
    jobId: "restart-job",
    capability: "marketing.video.publish",
    workerId: "bounded-worker",
    leaseSeconds: 30,
  }), null);
});

test("expired external-effect leases reconcile instead of retrying provider work", async () => {
  for (const [index, effectClass] of ["publish", "message", "money"].entries()) {
    const dataDir = tempDataDir();
    let clock = "2026-08-21T02:00:00.000Z";
    const ledger = createMarketingLocalLedger({ dataDir, now: () => clock });
    const jobId = `expired-${effectClass}-${index}`;
    const capability = effectClass === "publish"
      ? "marketing.video.publish"
      : effectClass === "message" ? "marketing.liveness.telegram" : "payout.send";
    await ledger.enqueueJob(job({
      job_id: jobId,
      capability,
      effect_class: effectClass,
      effect_key: `${effectClass}:crash-after-lease:${index}`,
      available_at: clock,
    }));
    const initial = await ledger.claimJob({
      tenantId: "dais-local", jobId, capability, workerId: "provider-worker", leaseSeconds: 30,
    });
    assert.equal(initial.attempt, 1);
    clock = "2026-08-21T02:00:31.000Z";
    assert.equal(await ledger.claimJob({
      tenantId: "dais-local", jobId, capability, workerId: "retry-worker", leaseSeconds: 30,
    }), null);
    const recovered = await ledger.readJob({ tenantId: "dais-local", jobId });
    assert.equal(recovered.status, "reconciling");
    assert.equal(recovered.attempt, 1);
    assert.equal(recovered.unknown_effect, true);
    assert.equal(await ledger.claimJob({
      tenantId: "dais-local", jobId, capability, workerId: "third-worker", leaseSeconds: 30,
    }), null);
  }
});

test("running marker lease is authoritative when heartbeat marker beats the JSONL event", async () => {
  const dataDir = tempDataDir();
  let clock = "2026-08-21T02:00:00.000Z";
  const first = createMarketingLocalLedger({ dataDir, now: () => clock });
  await first.enqueueJob(job({
    job_id: "heartbeat-crash-job",
    effect_key: "tiktok:honne-ai:en:heartbeat-crash",
    available_at: clock,
  }));
  const claimed = await first.claimJob({
    tenantId: "dais-local",
    jobId: "heartbeat-crash-job",
    capability: "marketing.video.publish",
    workerId: "worker-one",
    leaseSeconds: 30,
  });
  assert.equal(claimed.lease_expires_at, "2026-08-21T02:00:30.000Z");
  const claimsDirectory = path.join(dataDir, "marketing", "claims");
  const claimFile = path.join(claimsDirectory, fs.readdirSync(claimsDirectory)[0]);
  fs.writeFileSync(claimFile, `${JSON.stringify({
    schema_version: 1,
    tenant_id: "dais-local",
    job_id: "heartbeat-crash-job",
    attempt: 1,
    worker_id: "worker-one",
    lease_expires_at: "2026-08-21T02:00:50.000Z",
  })}\n`);
  clock = "2026-08-21T02:00:31.000Z";
  const restarted = createMarketingLocalLedger({ dataDir, now: () => clock });
  assert.equal(await restarted.claimJob({
    tenantId: "dais-local",
    jobId: "heartbeat-crash-job",
    capability: "marketing.video.publish",
    workerId: "worker-two",
    leaseSeconds: 30,
  }), null);
  assert.equal((await restarted.readJob({ tenantId: "dais-local", jobId: "heartbeat-crash-job" })).lease_expires_at, "2026-08-21T02:00:50.000Z");
});

test("older marker attempts never override a newer job, and receipts suppress stale markers", async () => {
  const dataDir = tempDataDir();
  let clock = "2026-08-21T02:00:00.000Z";
  const ledger = createMarketingLocalLedger({ dataDir, now: () => clock });
  await ledger.enqueueJob(job({
    job_id: "marker-order-job",
    capability: "runtime.noop",
    effect_class: "none",
    effect_key: null,
    available_at: clock,
    max_attempts: 3,
  }));
  const first = await ledger.claimJob({
    tenantId: "dais-local", jobId: "marker-order-job", capability: "runtime.noop", workerId: "worker-one", leaseSeconds: 30,
  });
  clock = "2026-08-21T02:00:31.000Z";
  const second = await ledger.claimJob({
    tenantId: "dais-local", jobId: "marker-order-job", capability: "runtime.noop", workerId: "worker-two", leaseSeconds: 30,
  });
  assert.equal(first.attempt, 1);
  assert.equal(second.attempt, 2);
  const claimFile = path.join(dataDir, "marketing", "claims", fs.readdirSync(path.join(dataDir, "marketing", "claims"))[0]);
  fs.writeFileSync(claimFile, `${JSON.stringify({
    schema_version: 1,
    tenant_id: "dais-local",
    job_id: "marker-order-job",
    attempt: 1,
    worker_id: "old-worker",
    lease_expires_at: "2026-08-21T03:00:00.000Z",
  })}\n`);
  const restarted = createMarketingLocalLedger({ dataDir, now: () => clock });
  const current = await restarted.readJob({ tenantId: "dais-local", jobId: "marker-order-job" });
  assert.equal(current.attempt, 2);
  assert.equal(current.lease_owner, "worker-two");
  assert.equal(await restarted.claimJob({
    tenantId: "dais-local", jobId: "marker-order-job", capability: "runtime.noop", workerId: "worker-three", leaseSeconds: 30,
  }), null);

  const receiptDataDir = tempDataDir();
  let receiptClock = "2026-08-21T02:00:00.000Z";
  const receiptLedger = createMarketingLocalLedger({ dataDir: receiptDataDir, now: () => receiptClock });
  await receiptLedger.enqueueJob(job({ job_id: "receipt-marker-job", effect_key: "tiktok:honne-ai:en:receipt-marker", available_at: receiptClock }));
  const receiptClaim = await receiptLedger.claimJob({
    tenantId: "dais-local", jobId: "receipt-marker-job", capability: "marketing.video.publish", workerId: "receipt-worker", leaseSeconds: 30,
  });
  await receiptLedger.completeJob({
    tenantId: "dais-local", jobId: "receipt-marker-job", attempt: receiptClaim.attempt, workerId: "receipt-worker",
    receipt: { status: "published", public_url: "https://www.tiktok.com/@honne_reveal/video/2" },
  });
  const receiptClaimFile = path.join(receiptDataDir, "marketing", "claims", fs.readdirSync(path.join(receiptDataDir, "marketing", "claims"))[0]);
  fs.writeFileSync(receiptClaimFile, `${JSON.stringify({
    schema_version: 1, tenant_id: "dais-local", job_id: "receipt-marker-job", attempt: 1, worker_id: "stale-worker", lease_expires_at: "2026-08-21T03:00:00.000Z",
  })}\n`);
  const receiptRestart = createMarketingLocalLedger({ dataDir: receiptDataDir, now: () => receiptClock });
  assert.equal(await receiptRestart.claimJob({
    tenantId: "dais-local", jobId: "receipt-marker-job", capability: "marketing.video.publish", workerId: "worker-two", leaseSeconds: 30,
  }), null);
  assert.equal((await receiptRestart.readJob({ tenantId: "dais-local", jobId: "receipt-marker-job" })).status, "completed");
});
