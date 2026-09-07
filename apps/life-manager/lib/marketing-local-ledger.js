"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { isDeepStrictEqual } = require("node:util");
const { resolveDataRoot } = require("./runtime-paths.js");
const { isMarketingLaneManifest } = require("./marketing-lane-manifest.js");
const {
  findMarketingDestinationTarget,
  loadMarketingDestinationContract,
} = require("./marketing-destination-contract.js");
const { projectJob } = require("../../../runtime/contracts/common-record.cjs");

const SHADOW_HOLD_AVAILABLE_AT = "9999-12-31T23:59:59.000Z";
const EFFECT_CLASSES = new Set(["none", "publish", "message", "money"]);
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const MAX_RECORD_BYTES = 64 * 1024;
const LOCK_STALE_MS = 30_000;

function required(value, label, max = 200) {
  const text = String(value == null ? "" : value).trim();
  if (!text || text.length > max) throw new Error(`${label} invalid`);
  return text;
}

function identifier(value, label) {
  const text = required(value, label);
  if (!IDENTIFIER.test(text)) throw new Error(`${label} invalid`);
  return text;
}

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function exactInstant(value, label) {
  const text = String(value == null ? "" : value).trim();
  const date = new Date(text);
  if (!Number.isFinite(date.getTime()) || date.toISOString() !== text) {
    throw new Error(`${label} invalid`);
  }
  return text;
}

function nowInstant(now) {
  const value = typeof now === "function" ? now() : now;
  return value == null ? new Date().toISOString() : exactInstant(value, "local ledger clock");
}

function refs(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("local ledger input refs invalid");
  }
  const result = {};
  for (const [key, raw] of Object.entries(value)) {
    if (!/_refs?$/.test(key)) throw new Error("local ledger input refs must be references");
    const values = Array.isArray(raw) ? raw : [raw];
    if (values.length < 1 || values.some((item) => typeof item !== "string" || !item.trim())) {
      throw new Error("local ledger input refs must be references");
    }
    result[key] = Array.isArray(raw) ? [...raw] : raw;
  }
  if (Buffer.byteLength(JSON.stringify(result)) > 16_384) {
    throw new Error("local ledger input refs too large");
  }
  return result;
}

function normalizeJob(input = {}) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("local ledger job invalid");
  }
  const effectClass = required(input.effect_class ?? input.effectClass, "local ledger effect class", 20);
  if (!EFFECT_CLASSES.has(effectClass)) throw new Error("local ledger effect class invalid");
  const effectKeyRaw = input.effect_key ?? input.effectKey;
  const effectKey = effectKeyRaw == null ? null : String(effectKeyRaw).trim();
  if ((effectClass === "none" && effectKey) || (effectClass !== "none" && !effectKey)) {
    throw new Error("local ledger effect key invalid");
  }
  const maxAttempts = Number(input.max_attempts ?? input.maxAttempts ?? 3);
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 20) {
    throw new Error("local ledger max attempts invalid");
  }
  return {
    job_id: identifier(input.job_id ?? input.jobId, "local ledger job id"),
    tenant_id: identifier(input.tenant_id ?? input.tenantId, "local ledger tenant id"),
    loop_id: identifier(input.loop_id ?? input.loopId, "local ledger loop id"),
    capability: identifier(input.capability, "local ledger capability"),
    effect_class: effectClass,
    effect_key: effectKey || null,
    input_refs: refs(input.input_refs ?? input.inputRefs),
    max_attempts: maxAttempts,
    available_at: exactInstant(
      input.available_at ?? input.availableAt ?? SHADOW_HOLD_AVAILABLE_AT,
      "local ledger available_at",
    ),
  };
}

function sameImmutableJob(left, right) {
  return left.job_id === right.job_id
    && left.tenant_id === right.tenant_id
    && left.loop_id === right.loop_id
    && left.capability === right.capability
    && left.effect_class === right.effect_class
    && left.effect_key === right.effect_key
    && isDeepStrictEqual(left.input_refs, right.input_refs)
    && left.max_attempts === right.max_attempts;
}

function dataRoot(options = {}) {
  const source = options.env && typeof options.env === "object" ? options.env : process.env;
  const env = { ...source };
  if (options.dataDir != null) env.LM_DATA_DIR = options.dataDir;
  return resolveDataRoot(env);
}

function keyFor(tenantId, jobId) {
  return crypto.createHash("sha256").update(`${tenantId}\n${jobId}`).digest("hex");
}

function createMarketingLocalLedger(options = {}) {
  const root = dataRoot(options);
  const destinationContract = options.destinationContract || loadMarketingDestinationContract();
  const directory = path.join(root, "marketing");
  const jobsFile = path.join(directory, "jobs.jsonl");
  const receiptsFile = path.join(directory, "receipts.jsonl");
  const lockFile = path.join(directory, ".ledger.lock");
  const reclaimLockFile = path.join(directory, ".ledger.reclaim");
  const publicationFenceFile = path.join(directory, "publication-effect-fence.json");
  const publicationFenceRefusalsFile = path.join(directory, "publication-effect-fence-refusals.jsonl");
  const laneManifestFile = path.join(directory, "lane-manifest.json");
  const lanePolicyRefusalsFile = path.join(directory, "lane-policy-refusals.jsonl");
  const claimsDirectory = path.join(directory, "claims");
  const receiptsDirectory = path.join(directory, "receipts");
  const now = options.now || (() => new Date().toISOString());
  const partialTails = new Map();

  function ensureDirectory() {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    fs.mkdirSync(claimsDirectory, { recursive: true, mode: 0o700 });
    fs.mkdirSync(receiptsDirectory, { recursive: true, mode: 0o700 });
  }

  function publicationFence() {
    let stat;
    let value;
    try {
      stat = fs.statSync(publicationFenceFile);
      value = JSON.parse(fs.readFileSync(publicationFenceFile, "utf8"));
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw new Error("local marketing publication effect fence invalid");
    }
    if ((stat.mode & 0o077) !== 0 || !value || value.schema_version !== 1
      || !["closed", "open"].includes(value.state)) {
      throw new Error("local marketing publication effect fence invalid");
    }
    if (value.state === "open" && (typeof value.allowed_effect_key !== "string" || !value.allowed_effect_key.trim())) {
      throw new Error("local marketing publication effect fence invalid");
    }
    return value;
  }

  function assertPublicationEffectAllowed(job, phase) {
    if (job.capability !== "marketing.video.publish") return;
    const fence = publicationFence();
    let closedProductionLane = false;
    if (fence?.state === "closed") {
      try { closedProductionLane = Boolean(publicationLane(job, laneManifest(true), true)); } catch {}
    }
    if (fence && !(fence.state === "open" && fence.allowed_effect_key === job.effect_key) && !closedProductionLane) {
      const observedAt = nowInstant(now);
      append(publicationFenceRefusalsFile, {
        schema_version: 1,
        kind: "marketing_publication_effect_refusal",
        tenant_id: job.tenant_id,
        job_id: job.job_id,
        effect_key: job.effect_key,
        phase,
        fence_state: fence.state,
        reason: required(fence.reason || "publication effect is not explicitly allowed", "publication effect fence reason", 500),
        recorded_at: observedAt,
      });
      const error = new Error("marketing publication effect fenced");
      error.code = "MARKETING_PUBLICATION_EFFECT_FENCED";
      throw error;
    }
    assertPublicationLaneAllowed(job, phase, fence !== null, fence?.state !== "open");
  }

  function laneManifest(requiredByFence) {
    let stat;
    let value;
    try {
      stat = fs.statSync(laneManifestFile);
      value = JSON.parse(fs.readFileSync(laneManifestFile, "utf8"));
    } catch (error) {
      if (error.code === "ENOENT" && !requiredByFence) return null;
      throw new Error("local marketing lane manifest invalid");
    }
    if ((stat.mode & 0o077) !== 0 || !isMarketingLaneManifest(value)) {
      throw new Error("local marketing lane manifest invalid");
    }
    return value;
  }

  function publicationLane(job, manifest, requireVerifiedCanary = true) {
    const refs = job.input_refs || {};
    const productMatch = /^product:\/\/([A-Za-z0-9._-]+)$/.exec(String(refs.product_ref || ""));
    const formatMatch = /^format:\/\/([A-Za-z0-9._-]+)$/.exec(String(refs.format_ref || ""));
    const formMatch = /^form:\/\/([A-Za-z0-9._-]+)$/.exec(String(refs.form_ref || ""));
    const localeMatch = /^locale:\/\/([a-z]{2}(?:-[A-Z]{2})?)$/.exec(String(refs.locale_ref || ""));
    const platformMatch = /^platform:\/\/(instagram|tiktok|youtube)$/.exec(String(refs.platform_ref || ""));
    if (!productMatch || !formatMatch || !formMatch || !localeMatch || !platformMatch) return null;
    const platform = platformMatch[1];
    const integrationMatch = new RegExp(`^integration://postiz/${platform}/([A-Za-z0-9._:-]+)$`)
      .exec(String(refs[`${platform}_integration_ref`] || ""));
    if (!integrationMatch) return null;
    const product = productMatch[1] === "anicca-ios" ? "anicca" : productMatch[1];
    const destination = findMarketingDestinationTarget(destinationContract, {
      jobProductId: productMatch[1],
      locale: localeMatch[1],
      platform,
      integrationId: integrationMatch[1],
      jobFormatId: formatMatch[1],
      mediaForm: formMatch[1],
    });
    if (!destination || (refs.pack_ref && refs.pack_ref !== destination.approved_pack_ref)) return null;
    return manifest.lanes.find((lane) => (
      lane.tenant_id === job.tenant_id
      && lane.product_id === product
      && lane.locale === localeMatch[1]
      && lane.platform === platform
      && lane.integration_id === integrationMatch[1]
      && lane.account === destination.lane_id
      && lane.profile === destination.postiz_profile
      && lane.renderer === destination.renderer_id
      && lane.approved_pack === destination.approved_pack
      && (!requireVerifiedCanary || typeof lane.canary_state === "string")
      && lane.owner === "life-manager"
      && lane.disposition === "target"
      && lane.disabled === false
      && lane.lane_state === "production-armed"
      && lane.production_armed === true
      && lane.target_daily_limit >= 1
    )) || null;
  }

  function assertPublicationLaneAllowed(job, phase, requiredByFence, requireVerifiedCanary = true) {
    const manifest = laneManifest(requiredByFence);
    if (!manifest || publicationLane(job, manifest, requireVerifiedCanary)) return;
    append(lanePolicyRefusalsFile, {
      schema_version: 1,
      kind: "marketing_publication_lane_refusal",
      tenant_id: job.tenant_id,
      job_id: job.job_id,
      effect_key: job.effect_key,
      phase,
      manifest_id: manifest.manifest_id,
      reason: "exact Life Manager production lane is not armed",
      recorded_at: nowInstant(now),
    });
    const error = new Error("marketing publication lane forbidden");
    error.code = "MARKETING_PUBLICATION_LANE_FORBIDDEN";
    throw error;
  }

  function readLockRecord(file = lockFile) {
    let stat;
    try { stat = fs.statSync(file); } catch (error) {
      if (error.code === "ENOENT") return null;
      throw error;
    }
    let record;
    try { record = JSON.parse(fs.readFileSync(file, "utf8")); } catch { return { invalid: true, stat }; }
    if (
      !record || record.schema_version !== 1
      || typeof record.token !== "string" || !record.token
      || !Number.isSafeInteger(record.pid) || record.pid < 1
      || typeof record.acquired_at !== "string" || !Number.isFinite(Date.parse(record.acquired_at))
    ) return { invalid: true, stat };
    return { record, stat };
  }

  function waitBriefly() {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2);
  }

  function ownerAlive(pid) {
    try {
      process.kill(pid, 0);
      return true;
    } catch (error) {
      if (error.code === "ESRCH") return false;
      if (error.code === "EPERM") return true;
      return null;
    }
  }

  function acquireReclaimMutex() {
    const token = crypto.randomUUID();
    for (let attempt = 0; attempt < 1_000; attempt += 1) {
      try {
        const descriptor = fs.openSync(reclaimLockFile, "wx", 0o600);
        const record = {
          schema_version: 1,
          token,
          pid: process.pid,
          acquired_at: new Date().toISOString(),
        };
        fs.writeSync(descriptor, JSON.stringify(record));
        fs.fsyncSync(descriptor);
        const stat = fs.fstatSync(descriptor);
        return { descriptor, token, inode: stat.ino };
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        const current = readLockRecord(reclaimLockFile);
        if (!current) continue;
        const age = Date.now() - current.stat.mtimeMs;
        if (current.invalid) {
          if (age > LOCK_STALE_MS) throw new Error("local marketing ledger reclaim lock record invalid");
        } else {
          const alive = ownerAlive(current.record.pid);
          if (alive === null) throw new Error("local marketing ledger reclaim lock owner liveness unknown");
          // A reclaim mutex is deliberately never recovered by another process.
          // Reclaiming it by path would recreate the same ABA race it protects.
          // A crashed holder therefore fails closed after the bounded wait.
        }
        waitBriefly();
      }
    }
    throw new Error("local marketing ledger reclaim lock timeout");
  }

  function releaseReclaimMutex(mutex) {
    fs.closeSync(mutex.descriptor);
    const current = readLockRecord(reclaimLockFile);
    if (
      current && !current.invalid
      && current.stat.ino === mutex.inode
      && current.record.token === mutex.token
    ) {
      try { fs.unlinkSync(reclaimLockFile); } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
    }
  }

  function withReclaimMutex(callback) {
    const mutex = acquireReclaimMutex();
    try {
      return callback();
    } finally {
      releaseReclaimMutex(mutex);
    }
  }

  function reclaimDeadLock() {
    return withReclaimMutex(() => {
      const current = readLockRecord(lockFile);
      if (!current) return false;
      const age = Date.now() - current.stat.mtimeMs;
      if (current.invalid) {
        if (age > LOCK_STALE_MS) throw new Error("local marketing ledger lock record invalid");
        return false;
      }
      const alive = ownerAlive(current.record.pid);
      if (alive === null) throw new Error("local marketing ledger lock owner liveness unknown");
      if (alive !== false) return false;
      const confirm = readLockRecord(lockFile);
      if (
        !confirm || confirm.invalid
        || confirm.stat.ino !== current.stat.ino
        || confirm.record.token !== current.record.token
        || ownerAlive(confirm.record.pid) !== false
      ) return false;
      try { fs.unlinkSync(lockFile); } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      return true;
    });
  }

  function acquireLock() {
    ensureDirectory();
    const token = crypto.randomUUID();
    for (let attempt = 0; attempt < 1_000; attempt += 1) {
      try {
        const descriptor = fs.openSync(lockFile, "wx", 0o600);
        const record = {
          schema_version: 1,
          token,
          pid: process.pid,
          acquired_at: new Date().toISOString(),
        };
        fs.writeSync(descriptor, JSON.stringify(record));
        fs.fsyncSync(descriptor);
        const stat = fs.fstatSync(descriptor);
        return { descriptor, token, inode: stat.ino };
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        const current = readLockRecord();
        if (!current) continue;
        const age = Date.now() - current.stat.mtimeMs;
        if (current.invalid) {
          if (age > LOCK_STALE_MS) throw new Error("local marketing ledger lock record invalid");
        } else {
          const alive = ownerAlive(current.record.pid);
          if (alive === null) throw new Error("local marketing ledger lock owner liveness unknown");
          if (alive === false) reclaimDeadLock();
        }
        waitBriefly();
      }
    }
    throw new Error("local marketing ledger lock timeout");
  }

  function withLock(callback) {
    const lock = acquireLock();
    try {
      return callback();
    } finally {
      fs.closeSync(lock.descriptor);
      const current = readLockRecord();
      if (
        current && !current.invalid
        && current.stat.ino === lock.inode
        && current.record.token === lock.token
      ) {
        try { fs.unlinkSync(lockFile); } catch (error) { if (error.code !== "ENOENT") throw error; }
      }
    }
  }

  function readEvents(file) {
    let source;
    try {
      source = fs.readFileSync(file, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") return [];
      throw error;
    }
    const events = [];
    const lines = source.split(/\r?\n/);
    const trailingNewline = /\r?\n$/.test(source);
    let sourceStat;
    try { sourceStat = fs.statSync(file); } catch (error) { if (error.code !== "ENOENT") throw error; }
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      if (!line.trim()) continue;
      if (Buffer.byteLength(line) > MAX_RECORD_BYTES) throw new Error("local ledger record too large");
      try {
        events.push(JSON.parse(line));
      } catch {
        if (!trailingNewline && index === lines.length - 1) {
          const offset = Buffer.byteLength(source.slice(0, source.lastIndexOf("\n") + 1));
          partialTails.set(file, {
            offset,
            size: Buffer.byteLength(source),
            inode: sourceStat && sourceStat.ino,
            tail_hash: crypto.createHash("sha256").update(Buffer.from(source).subarray(offset)).digest("hex"),
          });
          continue;
        }
        throw new Error("local marketing ledger is invalid");
      }
    }
    return events;
  }

  function snapshot() {
    const jobs = new Map();
    for (const event of readEvents(jobsFile)) {
      if (!event || event.schema_version !== 1 || event.kind !== "job" || !event.job) {
        throw new Error("local marketing job ledger is invalid");
      }
      jobs.set(event.job.job_id, event.job);
    }
    const receipts = new Map();
    for (const event of readEvents(receiptsFile)) {
      if (
        !event || event.schema_version !== 1 || event.kind !== "receipt"
        || typeof event.tenant_id !== "string"
        || typeof event.job_id !== "string"
        || !event.receipt || typeof event.receipt !== "object" || Array.isArray(event.receipt)
      ) {
        throw new Error("local marketing receipt ledger is invalid");
      }
      receipts.set(keyFor(event.tenant_id, event.job_id), event.receipt);
    }
    const claims = new Map();
    let files = [];
    try { files = fs.readdirSync(claimsDirectory); } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    for (const file of files.filter((name) => name.endsWith(".json"))) {
      let claim;
      try { claim = JSON.parse(fs.readFileSync(path.join(claimsDirectory, file), "utf8")); }
      catch { throw new Error("local marketing claim ledger is invalid"); }
      if (
        !claim || claim.schema_version !== 1
        || typeof claim.tenant_id !== "string"
        || typeof claim.job_id !== "string"
        || !Number.isSafeInteger(claim.attempt)
        || claim.attempt < 1
        || typeof claim.worker_id !== "string"
        || !Number.isFinite(Date.parse(claim.lease_expires_at))
      ) throw new Error("local marketing claim ledger is invalid");
      claims.set(keyFor(claim.tenant_id, claim.job_id), claim);
    }
    for (const claim of claims.values()) {
      const job = jobs.get(claim.job_id);
      if (!job || job.tenant_id !== claim.tenant_id) continue;
      const markerLease = Date.parse(claim.lease_expires_at);
      const jobLease = Date.parse(job.lease_expires_at);
      const markerWins = (job.status === "queued" || job.status === "running")
        && (claim.attempt > Number(job.attempt || 0)
        || (claim.attempt === Number(job.attempt || 0) && job.status === "queued")
        || (claim.attempt === Number(job.attempt || 0)
          && job.status === "running"
          && markerLease > jobLease));
      if (markerWins) {
        jobs.set(job.job_id, {
          ...job,
          status: "running",
          attempt: claim.attempt,
          lease_owner: claim.worker_id,
          lease_expires_at: claim.lease_expires_at,
        });
      }
    }
    return { jobs, receipts, claims };
  }

  function append(file, event) {
    let stat;
    try { stat = fs.statSync(file); } catch (error) { if (error.code !== "ENOENT") throw error; }
    const partial = partialTails.get(file);
    if (partial != null) {
      let matches = Boolean(
        stat
        && stat.size === partial.size
        && (partial.inode == null || stat.ino === partial.inode),
      );
      if (matches) {
        const current = fs.readFileSync(file);
        matches = crypto.createHash("sha256").update(current.subarray(partial.offset)).digest("hex") === partial.tail_hash;
      }
      if (matches) {
        fs.truncateSync(file, partial.offset);
        stat = fs.statSync(file);
      }
      partialTails.delete(file);
    }
    if (stat && stat.size > 0) {
      const descriptor = fs.openSync(file, "r");
      const last = Buffer.alloc(1);
      fs.readSync(descriptor, last, 0, 1, stat.size - 1);
      fs.closeSync(descriptor);
      if (last[0] !== 0x0a) fs.appendFileSync(file, "\n", { encoding: "utf8", mode: 0o600 });
    }
    const line = `${JSON.stringify(event)}\n`;
    if (Buffer.byteLength(line) > MAX_RECORD_BYTES) throw new Error("local ledger record too large");
    fs.appendFileSync(file, line, { encoding: "utf8", mode: 0o600 });
    fs.chmodSync(file, 0o600);
  }

  function atomicJson(file, value) {
    const temporary = `${file}.tmp-${process.pid}-${crypto.randomUUID()}`;
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 });
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, file);
    fs.chmodSync(file, 0o600);
  }

  function jobFor(snapshotState, tenantId, jobId) {
    const job = snapshotState.jobs.get(jobId);
    if (!job || job.tenant_id !== tenantId) return null;
    return job;
  }

  function receiptFor(snapshotState, tenantId, jobId) {
    const key = keyFor(tenantId, jobId);
    const retained = snapshotState.receipts.get(key);
    if (retained !== undefined) return retained;
    const file = path.join(receiptsDirectory, `${keyFor(tenantId, jobId)}.json`);
    try {
      const stored = JSON.parse(fs.readFileSync(file, "utf8"));
      if (stored.tenant_id !== tenantId || stored.job_id !== jobId) {
        throw new Error("receipt identity mismatch");
      }
      return stored.receipt;
    } catch (error) {
      if (error.code === "ENOENT") return null;
      throw new Error("local marketing receipt ledger is invalid");
    }
  }

  function leaseSeconds(value) {
    const seconds = Number(value == null ? 180 : value);
    if (!Number.isSafeInteger(seconds) || seconds < 30 || seconds > 900) {
      throw new Error("local ledger lease invalid");
    }
    return seconds;
  }

  function identity(input = {}) {
    return {
      tenantId: identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id"),
      jobId: identifier(input.jobId ?? input.job_id, "local ledger job id"),
      attempt: Number(input.attempt),
      workerId: identifier(input.workerId ?? input.worker_id, "local ledger worker id"),
    };
  }

  function assertLease(job, id) {
    if (!job || job.tenant_id !== id.tenantId || job.job_id !== id.jobId) {
      throw new Error("local ledger job is unavailable");
    }
    if (job.status !== "running" || job.attempt !== id.attempt || job.lease_owner !== id.workerId) {
      throw new Error("local ledger lease lost");
    }
  }

  function enqueueJob(input = {}) {
    const normalized = normalizeJob(input);
    if (input.available_at == null && input.availableAt == null) {
      normalized.available_at = nowInstant(now);
    }
    return withLock(() => {
      const state = snapshot();
      const existing = state.jobs.get(normalized.job_id);
      if (existing) {
        if (!sameImmutableJob(existing, normalized)) throw new Error("local ledger job id collision");
        return { created: false, job: clone(existing) };
      }
      assertPublicationEffectAllowed(normalized, "enqueue");
      if (normalized.effect_key && [...state.jobs.values()].some((job) => (
        job.tenant_id === normalized.tenant_id
        && job.effect_key === normalized.effect_key
        && job.job_id !== normalized.job_id
      ))) {
        throw new Error("local ledger effect collision");
      }
      const queued = {
        ...normalized,
        status: "queued",
        attempt: 0,
        lease_owner: null,
        lease_expires_at: null,
        unknown_effect: false,
        enqueued_at: nowInstant(now),
        updated_at: nowInstant(now),
      };
      append(jobsFile, { schema_version: 1, kind: "job", event: "enqueue", job: queued });
      return { created: true, job: clone(queued) };
    });
  }

  function promoteJob(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    if (input.confirmation !== "PROMOTE_HONNE_EN_TIKTOK_CANARY") {
      throw new Error("local ledger promotion confirmation is invalid");
    }
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, tenantId, jobId);
      if (!job || job.status !== "queued" || job.available_at !== SHADOW_HOLD_AVAILABLE_AT
        || job.capability !== "marketing.video.publish" || job.effect_class !== "publish"
        || job.input_refs.product_ref !== "product://honne-ai"
        || job.input_refs.locale_ref !== "locale://en"
        || job.input_refs.platform_ref !== "platform://tiktok") {
        throw new Error("local ledger job is not an eligible shadow TikTok job");
      }
      const promoted = { ...job, available_at: nowInstant(now), updated_at: nowInstant(now) };
      append(jobsFile, { schema_version: 1, kind: "job", event: "promote", job: promoted });
      return clone(promoted);
    });
  }

  function claimJob(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    const capability = identifier(input.capability, "local ledger capability");
    const workerId = identifier(input.workerId ?? input.worker_id, "local ledger worker id");
    const seconds = leaseSeconds(input.leaseSeconds ?? input.lease_seconds);
    return withLock(() => {
      const state = snapshot();
      const observedAt = nowInstant(now);
      let job = jobFor(state, tenantId, jobId);
      if (!job || job.capability !== capability) return null;
      const marker = state.claims.get(keyFor(tenantId, jobId));
      if (job.status === "queued" && marker && marker.attempt >= job.attempt) {
        job = {
          ...job,
          status: "running",
          attempt: marker.attempt,
          lease_owner: marker.worker_id,
          lease_expires_at: marker.lease_expires_at,
        };
      }
      const retainedReceipt = receiptFor(state, tenantId, jobId);
      if (retainedReceipt) return null;
      if (job.status === "running") {
        if (!job.lease_expires_at || Date.parse(job.lease_expires_at) > Date.parse(observedAt)) return null;
        if (job.effect_class !== "none") {
          const reconciling = {
            ...job,
            status: "reconciling",
            unknown_effect: true,
            reconciling_at: observedAt,
            updated_at: observedAt,
            lease_owner: null,
            lease_expires_at: null,
          };
          append(jobsFile, { schema_version: 1, kind: "job", event: "reconcile", job: reconciling });
          return null;
        }
      } else if (job.status !== "queued") {
        return null;
      }
      if (Date.parse(job.available_at) > Date.parse(observedAt) || job.attempt >= job.max_attempts) return null;
      assertPublicationEffectAllowed(job, "claim");
      const claimed = {
        ...job,
        status: "running",
        attempt: job.attempt + 1,
        lease_owner: workerId,
        lease_expires_at: new Date(Date.parse(observedAt) + seconds * 1000).toISOString(),
        updated_at: observedAt,
      };
      atomicJson(path.join(claimsDirectory, `${keyFor(tenantId, jobId)}.json`), {
        schema_version: 1,
        tenant_id: tenantId,
        job_id: jobId,
        attempt: claimed.attempt,
        worker_id: workerId,
        lease_expires_at: claimed.lease_expires_at,
      });
      append(jobsFile, { schema_version: 1, kind: "job", event: "claim", job: claimed });
      return clone(claimed);
    });
  }

  function heartbeatJob(input = {}) {
    const id = identity(input);
    const seconds = leaseSeconds(input.leaseSeconds ?? input.lease_seconds);
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, id.tenantId, id.jobId);
      assertLease(job, id);
      const observedAt = nowInstant(now);
      const updated = {
        ...job,
        lease_expires_at: new Date(Date.parse(observedAt) + seconds * 1000).toISOString(),
        updated_at: observedAt,
      };
      atomicJson(path.join(claimsDirectory, `${keyFor(id.tenantId, id.jobId)}.json`), {
        schema_version: 1,
        tenant_id: id.tenantId,
        job_id: id.jobId,
        attempt: updated.attempt,
        worker_id: id.workerId,
        lease_expires_at: updated.lease_expires_at,
      });
      append(jobsFile, { schema_version: 1, kind: "job", event: "heartbeat", job: updated });
      return clone(updated);
    });
  }

  function completeJob(input = {}) {
    const id = identity(input);
    const receipt = input.receipt;
    if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
      throw new Error("local ledger receipt invalid");
    }
    if (Buffer.byteLength(JSON.stringify(receipt)) > 16_384) throw new Error("local ledger receipt too large");
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, id.tenantId, id.jobId);
      assertLease(job, id);
      const observedAt = nowInstant(now);
      const receiptPath = path.join(receiptsDirectory, `${keyFor(id.tenantId, id.jobId)}.json`);
      atomicJson(receiptPath, { schema_version: 1, tenant_id: id.tenantId, job_id: id.jobId, receipt });
      append(receiptsFile, {
        schema_version: 1,
        kind: "receipt",
        tenant_id: id.tenantId,
        job_id: id.jobId,
        attempt: id.attempt,
        receipt: clone(receipt),
      });
      const completed = {
        ...job,
        status: "completed",
        completed_at: observedAt,
        updated_at: observedAt,
        lease_owner: null,
        lease_expires_at: null,
      };
      append(jobsFile, { schema_version: 1, kind: "job", event: "complete", job: completed });
      return clone(completed);
    });
  }

  function failJob(input = {}) {
    const id = identity(input);
    const errorCode = required(input.errorCode ?? input.error_code, "local ledger error code", 200);
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, id.tenantId, id.jobId);
      assertLease(job, id);
      const failed = {
        ...job,
        status: "failed",
        error_code: errorCode,
        unknown_effect: input.unknownEffect === true || input.unknown_effect === true,
        failed_at: nowInstant(now),
        updated_at: nowInstant(now),
        lease_owner: null,
        lease_expires_at: null,
      };
      append(jobsFile, { schema_version: 1, kind: "job", event: "fail", job: failed });
      return clone(failed);
    });
  }

  function retryJob(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, tenantId, jobId);
      if (!job || job.status !== "failed" || job.unknown_effect === true) {
        throw new Error("local ledger job is not safely retryable");
      }
      if (job.attempt >= job.max_attempts) throw new Error("local ledger job retry limit reached");
      const observedAt = nowInstant(now);
      const queued = {
        ...job,
        status: "queued",
        available_at: observedAt,
        error_code: null,
        failed_at: null,
        updated_at: observedAt,
        lease_owner: null,
        lease_expires_at: null,
      };
      try { fs.unlinkSync(path.join(claimsDirectory, `${keyFor(tenantId, jobId)}.json`)); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
      append(jobsFile, { schema_version: 1, kind: "job", event: "retry", job: queued });
      return clone(queued);
    });
  }

  function resolveReconciliation(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    const attempt = Number(input.attempt);
    if (!Number.isSafeInteger(attempt) || attempt < 1) {
      throw new Error("local ledger reconciliation attempt invalid");
    }
    const decision = String(input.decision || "").trim();
    if (!["present", "absent"].includes(decision)) {
      throw new Error("local ledger reconciliation decision invalid");
    }
    const receipt = input.receipt;
    if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
      throw new Error("local ledger reconciliation receipt invalid");
    }
    if (Buffer.byteLength(JSON.stringify(receipt)) > 16_384) {
      throw new Error("local ledger reconciliation receipt too large");
    }
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, tenantId, jobId);
      if (!job || job.attempt !== attempt) {
        throw new Error("local ledger reconciliation lost job");
      }
      const retained = receiptFor(state, tenantId, jobId);
      if (retained !== null) {
        if (!isDeepStrictEqual(retained, receipt)) {
          throw new Error("local ledger reconciliation receipt collision");
        }
        return clone(job);
      }
      if (![
        "failed",
        "reconciling",
      ].includes(job.status) || job.unknown_effect !== true || !job.effect_key) {
        throw new Error("local ledger job is not awaiting reconciliation");
      }
      const observedAt = nowInstant(now);
      const receiptPath = path.join(receiptsDirectory, `${keyFor(tenantId, jobId)}.json`);
      atomicJson(receiptPath, { schema_version: 1, tenant_id: tenantId, job_id: jobId, receipt });
      append(receiptsFile, {
        schema_version: 1,
        kind: "receipt",
        tenant_id: tenantId,
        job_id: jobId,
        attempt,
        receipt: clone(receipt),
      });
      const completed = {
        ...job,
        status: "completed",
        unknown_effect: false,
        reconciliation_decision: decision,
        reconciled_from_unknown: true,
        reconciled_at: observedAt,
        completed_at: observedAt,
        updated_at: observedAt,
        lease_owner: null,
        lease_expires_at: null,
      };
      append(jobsFile, { schema_version: 1, kind: "job", event: "reconcile_complete", job: completed });
      return clone(completed);
    });
  }

  function correctReceiptDirectUrl(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    if (input.confirmation !== "CORRECT_CAPTION_MATCHED_DIRECT_URL") {
      throw new Error("local ledger receipt correction confirmation is invalid");
    }
    const receipt = input.receipt;
    if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
      throw new Error("local ledger receipt correction is invalid");
    }
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, tenantId, jobId);
      const retained = receiptFor(state, tenantId, jobId);
      if (!job || job.status !== "completed" || job.effect_class === "none" || !retained) {
        throw new Error("local ledger receipt is not correctable");
      }
      if (isDeepStrictEqual(retained, receipt)) return clone(job);
      const { public_url: retainedUrl, ...retainedLineage } = retained;
      const { public_url: correctedUrl, ...correctedLineage } = receipt;
      if (
        !isDeepStrictEqual(retainedLineage, correctedLineage)
        || typeof retainedUrl !== "string" || !retainedUrl
        || typeof correctedUrl !== "string" || !correctedUrl
        || retainedUrl === correctedUrl
      ) {
        throw new Error("local ledger correction may only replace the direct URL");
      }
      const observedAt = nowInstant(now);
      append(receiptsFile, {
        schema_version: 1, kind: "receipt", tenant_id: tenantId, job_id: jobId,
        attempt: job.attempt, receipt: clone(receipt), correction: "caption_matched_direct_url",
        corrected_at: observedAt,
      });
      atomicJson(path.join(receiptsDirectory, `${keyFor(tenantId, jobId)}.json`), {
        schema_version: 1, tenant_id: tenantId, job_id: jobId, receipt,
      });
      const corrected = { ...job, receipt_corrected_at: observedAt, updated_at: observedAt };
      append(jobsFile, { schema_version: 1, kind: "job", event: "receipt_correct", job: corrected });
      return clone(corrected);
    });
  }

  function quarantineCompletedEffectConflict(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    const conflictsWithJobId = identifier(
      input.conflictsWithJobId ?? input.conflicts_with_job_id,
      "local ledger conflicting job id",
    );
    if (jobId === conflictsWithJobId) throw new Error("local ledger conflict must reference another job");
    if (input.confirmation !== "QUARANTINE_CONFIRMED_EFFECT_CONFLICT") {
      throw new Error("local ledger effect conflict confirmation is invalid");
    }
    const reason = identifier(input.reason, "local ledger effect conflict reason");
    const expectedReceipt = input.expectedReceipt ?? input.expected_receipt;
    if (!expectedReceipt || typeof expectedReceipt !== "object" || Array.isArray(expectedReceipt)) {
      throw new Error("local ledger expected conflict receipt is invalid");
    }
    if (Buffer.byteLength(JSON.stringify(expectedReceipt)) > 16_384) {
      throw new Error("local ledger expected conflict receipt is too large");
    }
    return withLock(() => {
      const state = snapshot();
      const job = jobFor(state, tenantId, jobId);
      const retained = receiptFor(state, tenantId, jobId);
      const conflictingJob = jobFor(state, tenantId, conflictsWithJobId);
      const conflictingReceipt = receiptFor(state, tenantId, conflictsWithJobId);
      if (
        job?.status === "conflict"
        && retained?.kind === "marketing_effect_conflict"
        && retained.reason === reason
        && retained.conflicts_with_job_id === conflictsWithJobId
        && isDeepStrictEqual(retained.superseded_receipt, expectedReceipt)
      ) return clone(job);
      if (
        !job || job.status !== "completed" || job.effect_class === "none" || !retained
        || !conflictingJob || conflictingJob.status !== "completed" || !conflictingReceipt
      ) {
        throw new Error("local ledger completed effect is not conflict-quarantinable");
      }
      if (!isDeepStrictEqual(retained, expectedReceipt)) {
        throw new Error("local ledger effect conflict receipt changed");
      }
      const observedAt = nowInstant(now);
      const conflictReceipt = {
        schema_version: 1,
        kind: "marketing_effect_conflict",
        status: "conflict",
        reason,
        conflicts_with_job_id: conflictsWithJobId,
        superseded_receipt: clone(retained),
        quarantined_at: observedAt,
      };
      if (Buffer.byteLength(JSON.stringify(conflictReceipt)) > 16_384) {
        throw new Error("local ledger effect conflict receipt is too large");
      }
      append(receiptsFile, {
        schema_version: 1, kind: "receipt", tenant_id: tenantId, job_id: jobId,
        attempt: job.attempt, receipt: clone(conflictReceipt), correction: "confirmed_effect_conflict",
        corrected_at: observedAt,
      });
      atomicJson(path.join(receiptsDirectory, `${keyFor(tenantId, jobId)}.json`), {
        schema_version: 1, tenant_id: tenantId, job_id: jobId, receipt: conflictReceipt,
      });
      const conflicted = {
        ...job,
        status: "conflict",
        unknown_effect: false,
        conflict_reason: reason,
        conflicts_with_job_id: conflictsWithJobId,
        conflicted_at: observedAt,
        updated_at: observedAt,
      };
      append(jobsFile, { schema_version: 1, kind: "job", event: "conflict_quarantine", job: conflicted });
      return clone(conflicted);
    });
  }

  function readJob(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    const job = jobFor(snapshot(), tenantId, jobId);
    return clone(job);
  }

  function readReceipt(input = {}) {
    const tenantId = identifier(input.tenantId ?? input.tenant_id, "local ledger tenant id");
    const jobId = identifier(input.jobId ?? input.job_id, "local ledger job id");
    return clone(receiptFor(snapshot(), tenantId, jobId));
  }

  function readCommonJob(input = {}) {
    const job = readJob(input);
    return job == null ? null : projectJob(job);
  }

  return Object.freeze({
    dataDir: root,
    enqueueJob: async (input) => enqueueJob(input),
    promoteJob: async (input) => promoteJob(input),
    claimJob: async (input) => claimJob(input),
    heartbeatJob: async (input) => heartbeatJob(input),
    completeJob: async (input) => completeJob(input),
    failJob: async (input) => failJob(input),
    retryJob: async (input) => retryJob(input),
    resolveReconciliation: async (input) => resolveReconciliation(input),
    correctReceiptDirectUrl: async (input) => correctReceiptDirectUrl(input),
    quarantineCompletedEffectConflict: async (input) => quarantineCompletedEffectConflict(input),
    readJob: async (input) => readJob(input),
    readCommonJob: async (input) => readCommonJob(input),
    readReceipt: async (input) => readReceipt(input),
  });
}

module.exports = {
  SHADOW_HOLD_AVAILABLE_AT,
  createMarketingLocalLedger,
};
