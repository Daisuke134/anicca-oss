"use strict";

// Shared CommonJS core used by host-specific browser target adapters.

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
function unavailable(message) {
  throw new Error(message || "Browser target lease unavailable");
}

function exactInstant(value) {
  const text = value instanceof Date ? value.toISOString() : String(value || "");
  if (!Number.isFinite(Date.parse(text)) || new Date(Date.parse(text)).toISOString() !== text) {
    unavailable("Connector target lease timestamp invalid");
  }
  return text;
}

function targetId(value) {
  const text = String(value || "");
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(text)) unavailable("Connector target ID invalid");
  return text;
}

function defaultPageWebsocket(value, expectedTargetId) {
  const text = String(value || "");
  let parsed;
  try { parsed = new URL(text); } catch { unavailable("Connector page websocket invalid"); }
  if (
    parsed.protocol !== "ws:"
    || parsed.pathname !== `/devtools/page/${expectedTargetId}`
    || parsed.username || parsed.password || parsed.search || parsed.hash
  ) unavailable("Connector page websocket invalid");
  return text;
}

function defaultCanonicalUrl(value) {
  let parsed;
  try { parsed = new URL(String(value || "")); } catch { unavailable("Connector canonical URL invalid"); }
  if (
    parsed.protocol !== "https:"
    || parsed.username || parsed.password || parsed.hash
  ) unavailable("Connector canonical URL invalid");
  parsed.hash = "";
  return parsed.toString();
}

function ownerToken(value) {
  const text = String(value || "");
  if (!/^[A-Za-z0-9._-]{16,200}$/.test(text)) unavailable("Connector owner token invalid");
  return text;
}

function privateParent(filePath) {
  const parent = path.dirname(filePath);
  fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
  fs.chmodSync(parent, 0o700);
}

function readLedger(filePath) {
  try {
    const stat = fs.statSync(filePath);
    if (stat.size > 1_000_000) unavailable("Connector target lease ledger invalid");
    const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (!value || value.schema_version !== 1 || !value.targets || typeof value.targets !== "object") {
      unavailable("Connector target lease ledger invalid");
    }
    return value;
  } catch (error) {
    if (error && error.code === "ENOENT") return { schema_version: 1, targets: {} };
    throw error;
  }
}

function writeLedger(filePath, value) {
  const temporary = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { flag: "wx", mode: 0o600 });
    fs.renameSync(temporary, filePath);
    fs.chmodSync(filePath, 0o600);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

const DEFAULT_LOCK_STALE_MS = 10 * 60 * 1000;

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error && error.code === "EPERM";
  }
}

function readLockSnapshot(lockPath) {
  let stat;
  try {
    stat = fs.statSync(lockPath);
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }

  let metadata = null;
  try {
    metadata = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  } catch {
    // Empty and legacy/unparseable locks use their mtime as the age signal.
  }
  return { metadata, mtimeMs: stat.mtimeMs };
}

function canRecoverLock(snapshot, nowMs, staleMs, pidAlive) {
  const metadata = snapshot && snapshot.metadata;
  const acquiredAtMs = metadata ? Number(metadata.acquired_at_ms) : Number.NaN;
  const referenceMs = Number.isFinite(acquiredAtMs) ? acquiredAtMs : snapshot.mtimeMs;
  const ageMs = nowMs - referenceMs;
  if (!(ageMs > staleMs)) return false;
  return !pidAlive(Number(metadata && metadata.pid));
}

function writeLockMetadata(handle, metadata) {
  fs.writeFileSync(handle, JSON.stringify(metadata), { encoding: "utf8" });
  fs.fsyncSync(handle);
  fs.fchmodSync(handle, 0o600);
}

function releaseLedgerLock(lockPath, lockOwnerToken) {
  try {
    const owner = JSON.parse(fs.readFileSync(lockPath, "utf8"));
    if (owner && owner.owner_token === lockOwnerToken) fs.unlinkSync(lockPath);
  } catch {
    // A missing, replaced, or legacy lock is not ours to remove.
  }
}

async function withLedgerLock(filePath, task) {
  privateParent(filePath);
  const lockPath = `${filePath}.lock`;
  let recoveredStale = false;

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let handle;
    let lockOwnerToken;
    try {
      handle = fs.openSync(lockPath, "wx", 0o600);
      lockOwnerToken = crypto.randomUUID();
      writeLockMetadata(handle, {
        schema_version: 1,
        pid: process.pid,
        owner_token: lockOwnerToken,
        acquired_at_ms: Date.now(),
      });
      fs.closeSync(handle);
      handle = undefined;
    } catch (error) {
      if (handle !== undefined) {
        try { fs.closeSync(handle); } catch {}
      }
      if (error && error.code !== "EEXIST") {
        if (lockOwnerToken) releaseLedgerLock(lockPath, lockOwnerToken);
        throw error;
      }

      const snapshot = readLockSnapshot(lockPath);
      if (
        attempt === 0
        && snapshot
        && canRecoverLock(snapshot, Date.now(), DEFAULT_LOCK_STALE_MS, isPidAlive)
      ) {
        try {
          fs.unlinkSync(lockPath);
        } catch (unlinkError) {
          if (unlinkError && unlinkError.code !== "ENOENT") throw unlinkError;
        }
        recoveredStale = true;
        continue;
      }
      if (!snapshot && attempt === 0) continue;
      unavailable("Connector target lease ledger busy");
    }

    try {
      return await task(readLedger(filePath));
    } finally {
      releaseLedgerLock(lockPath, lockOwnerToken);
    }
  }

  if (recoveredStale) unavailable("Connector target lease ledger busy");
  unavailable("Connector target lease ledger busy");
}

function assertFence(record, fence) {
  if (
    !record
    || record.target_id !== targetId(fence && fence.target_id)
    || record.owner_token !== ownerToken(fence && fence.owner_token)
    || record.generation !== (fence && fence.generation)
  ) unavailable("Connector target lease fence mismatch");
  return record;
}

function createBrowserTargetLease(options = {}) {
  const ledgerPath = path.resolve(String(options.ledgerPath || ""));
  const now = options.now || (() => new Date());
  const makeOwnerToken = options.ownerToken || (() => crypto.randomUUID());
  const probeTarget = options.probeTarget;
  const closeTarget = options.closeTarget;
  const validatePageWebsocket = options.validatePageWebsocket || defaultPageWebsocket;
  const validateCanonicalUrl = options.validateCanonicalUrl || defaultCanonicalUrl;
  if (!path.isAbsolute(ledgerPath) || ledgerPath === path.parse(ledgerPath).root) unavailable();
  if (typeof probeTarget !== "function" || typeof closeTarget !== "function") unavailable();

  return Object.freeze({
    async claim(input = {}) {
      const id = targetId(input.targetId);
      const websocket = validatePageWebsocket(input.pageWebsocket, id);
      const url = validateCanonicalUrl(input.canonicalUrl);
      return withLedgerLock(ledgerPath, async (ledger) => {
        if (ledger.targets[id]) unavailable("Connector target already claimed");
        const observedAt = exactInstant(now());
        const record = Object.freeze({
          schema_version: 1,
          owner_token: ownerToken(makeOwnerToken()),
          generation: 1,
          target_id: id,
          page_websocket: websocket,
          canonical_url: url,
          claimed_at: observedAt,
          heartbeat_at: observedAt,
        });
        ledger.targets[id] = record;
        writeLedger(ledgerPath, ledger);
        return record;
      });
    },

    async heartbeat(fence) {
      return withLedgerLock(ledgerPath, async (ledger) => {
        const record = assertFence(ledger.targets[fence && fence.target_id], fence);
        const heartbeatAt = exactInstant(now());
        if (Date.parse(heartbeatAt) < Date.parse(record.heartbeat_at)) {
          unavailable("Browser target lease heartbeat moved backwards");
        }
        const next = { ...record, heartbeat_at: heartbeatAt };
        ledger.targets[record.target_id] = next;
        writeLedger(ledgerPath, ledger);
        return Object.freeze(next);
      });
    },

    async probe(fence) {
      return withLedgerLock(ledgerPath, async (ledger) => {
        const record = assertFence(ledger.targets[fence && fence.target_id], fence);
        return (await probeTarget(record.page_websocket)) === true;
      });
    },

    async release(fence) {
      return withLedgerLock(ledgerPath, async (ledger) => {
        const record = assertFence(ledger.targets[fence && fence.target_id], fence);
        if ((await closeTarget(record.target_id)) !== true) unavailable("Connector target close failed");
        delete ledger.targets[record.target_id];
        writeLedger(ledgerPath, ledger);
        return true;
      });
    },

    async reapStale(input = {}) {
      const maxIdleMs = input.maxIdleMs;
      if (!Number.isInteger(maxIdleMs) || maxIdleMs < 1_000 || maxIdleMs > 86_400_000) {
        unavailable("Connector target lease stale threshold invalid");
      }
      return withLedgerLock(ledgerPath, async (ledger) => {
        const observedMs = Date.parse(exactInstant(now()));
        const reaped = [];
        const retained = [];
        for (const id of Object.keys(ledger.targets).sort()) {
          const record = ledger.targets[id];
          const heartbeatMs = Date.parse(exactInstant(record && record.heartbeat_at));
          if (observedMs - heartbeatMs <= maxIdleMs) {
            retained.push(id);
            continue;
          }
          if ((await closeTarget(id)) !== true) unavailable("Connector stale target close failed");
          delete ledger.targets[id];
          reaped.push(id);
        }
        writeLedger(ledgerPath, ledger);
        return Object.freeze({
          reaped_target_ids: Object.freeze(reaped),
          retained_target_ids: Object.freeze(retained),
        });
      });
    },
  });
}

module.exports = { createBrowserTargetLease };
