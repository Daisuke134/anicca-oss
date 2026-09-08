"use strict";

const { createHash, randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { createLumaEvidenceStore } = require("./luma-evidence-store.js");
const { createPeatixEvidenceStore } = require("./peatix-evidence-store.js");
const { createKokuchProDiscoveryWorkflow } = require("./connector-kokuchpro-workflow.js");
const { createConnpassEvidenceStore, createMeetupEvidenceStore, createDoorkeeperEvidenceStore, createEventbriteEvidenceStore, createTechPlayEvidenceStore, createKokuchProEvidenceStore } = require("./connpass-evidence-store.js");

const EVENT_REF = /^luma-event:\/\/event\/[A-Za-z0-9_-]+$/;
const RECEIPT_REF = /^provider-receipt:\/\/luma\/([0-9a-f]{64})$/;
const PEATIX_EVENT_REF = /^peatix-event:\/\/event\/([1-9][0-9]*)$/;
const PEATIX_RECEIPT_REF = /^provider-receipt:\/\/peatix\/([0-9a-f]{64})$/;
const CONNPASS_EVENT_REF = /^connpass-event:\/\/event\/([1-9][0-9]*)$/;
const CONNPASS_RECEIPT_REF = /^provider-receipt:\/\/connpass\/([0-9a-f]{64})$/;
const MEETUP_EVENT_REF = /^meetup-event:\/\/event\/([1-9][0-9]*)$/;
const MEETUP_RECEIPT_REF = /^provider-receipt:\/\/meetup\/([0-9a-f]{64})$/;
const DOORKEEPER_EVENT_REF = /^doorkeeper-event:\/\/event\/([1-9][0-9]*)$/;
const DOORKEEPER_RECEIPT_REF = /^provider-receipt:\/\/doorkeeper\/([0-9a-f]{64})$/;
const EVENTBRITE_EVENT_REF = /^eventbrite-event:\/\/event\/([1-9][0-9]*)$/;
const EVENTBRITE_RECEIPT_REF = /^provider-receipt:\/\/eventbrite\/([0-9a-f]{64})$/;
const EVENTBRITE_EVENT_PATH = /^\/e\/(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)-tickets-([1-9][0-9]*)|([1-9][0-9]*))$/i;
const TECHPLAY_EVENT_REF = /^techplay-event:\/\/event\/([1-9][0-9]*)$/;
const TECHPLAY_RECEIPT_REF = /^provider-receipt:\/\/techplay\/([0-9a-f]{64})$/;
const KOKUCHPRO_EVENT_REF = /^kokuchpro-event:\/\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]*))?$/;
const KOKUCHPRO_RECEIPT_REF = /^provider-receipt:\/\/kokuchpro\/([0-9a-f]{64})$/;
const ARTIFACT_REF = /^object:\/\/sha256\/([0-9a-f]{64})$/;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const RECEIPT_ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const NO_FOLLOW = fs.constants.O_NOFOLLOW || 0;
const DELIVERY_CLAIM_DIR = "delivery-claims";

function invalid() {
  throw new Error("Connector minimal evidence invalid");
}

// Stage codes for the delivery block only (screenshot capture through bundle write). Same
// stageError/preserveSafe shape as connector-doorkeeper-workflow.js etc: stageError() tags a thrown
// error with a stable .code; the runner reads .code (never the message) to pick a safe_reason so the
// wake report and action-history row say WHICH stage failed instead of one generic
// "evidence_completion_failed" for every cause.
const EVIDENCE_SAFE_CODES = new Set([
  "EVIDENCE_SCREENSHOT_CAPTURE_FAILED",
  "EVIDENCE_CALENDAR_CREATE_FAILED",
  "EVIDENCE_CALENDAR_READBACK_FAILED",
  "EVIDENCE_TELEGRAM_MESSAGE_FAILED",
  "EVIDENCE_TELEGRAM_PHOTO_FAILED",
  "EVIDENCE_BUNDLE_WRITE_FAILED",
]);

function stageError(code) {
  const error = new Error("Connector minimal evidence stage failed");
  error.code = code;
  return error;
}

function preserveSafe(error, fallback) {
  const code = String(error && error.code || "");
  return stageError(EVIDENCE_SAFE_CODES.has(code) ? code : fallback);
}

function receiptHtml(provider, status, eventRef) {
  const safe = (value) => String(value).replace(/[&<>"']/g, (char) => RECEIPT_ESCAPE[char]);
  return `<!doctype html><html><body><dl><dt>provider</dt><dd>${safe(provider)}</dd><dt>status</dt><dd>${safe(status)}</dd><dt>event reference</dt><dd>${safe(eventRef)}</dd></dl></body></html>`;
}

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => RECEIPT_ESCAPE[char]); }

function text(value, max = 2_000) {
  const result = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  if (!result || result.length > max || /[\x00-\x1f\x7f]/.test(result)) invalid();
  return result;
}

function exactInstant(value) {
  const instant = value instanceof Date ? value.toISOString() : String(value || "");
  if (!Number.isFinite(Date.parse(instant)) || new Date(Date.parse(instant)).toISOString() !== instant) invalid();
  return instant;
}

function providerMessageId(value) {
  const raw = value && typeof value === "object" && !Array.isArray(value)
    ? Object.prototype.hasOwnProperty.call(value, "messageId")
      ? value.messageId
      : value.ok === true && value.result && typeof value.result === "object" && !Array.isArray(value.result)
        ? value.result.message_id : null
    : null;
  if (typeof raw === "number" && Number.isSafeInteger(raw) && raw > 0) return String(raw);
  if (typeof raw === "string" && /^[1-9][0-9]*$/.test(raw) && Number.isSafeInteger(Number(raw))) return raw;
  invalid();
}

// The only timezone connector-minimal-production.js verifies for a live run (its PRODUCTION_TIME_ZONE
// gate rejects any other value). Callers should thread the real run timezone in via options.timeZone;
// this is the fallback for callers (tests, the restart harness) that predate per-run timezone threading.
const DEFAULT_TIME_ZONE = "Asia/Tokyo";

function timeZoneName(value) {
  const result = text(value, 100);
  try { new Intl.DateTimeFormat("en-US", { timeZone: result }); } catch { invalid(); }
  return result;
}

function localizedStartsAt(startsAt, timeZone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("ja-JP", {
    timeZone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(startsAt)).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${parts.year}/${parts.month}/${parts.day}(${parts.weekday}) ${parts.hour}:${parts.minute} ${timeZone}`;
}

function assertNoSymlinkPath(root, target) {
  const base = path.resolve(root); const resolved = path.resolve(target); const relative = path.relative(base, resolved);
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) invalid();
  let current = base;
  for (const component of ["", ...relative.split(path.sep)]) {
    current = path.join(current, component);
    try { if (fs.lstatSync(current).isSymbolicLink()) invalid(); }
    catch (error) { if (error && error.code === "ENOENT") break; invalid(); }
  }
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function lumaUrl(value) {
  let url;
  try { url = new URL(String(value || "")); } catch { invalid(); }
  if (
    url.protocol !== "https:" || !["luma.com", "www.luma.com", "lu.ma"].includes(url.hostname.toLowerCase())
    || !/^\/[A-Za-z0-9_-]+\/?$/.test(url.pathname) || url.username || url.password
  ) invalid();
  return `https://${url.hostname.toLowerCase()}${url.pathname.replace(/\/$/, "")}`;
}

function peatixUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const match = PEATIX_EVENT_REF.exec(eventRef);
  const expected = match ? `https://peatix.com/event/${match[1]}` : "";
  if (!match || value !== expected) invalid();
  return expected;
}

function connpassUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const match = CONNPASS_EVENT_REF.exec(eventRef);
  let url;
  try { url = new URL(value); } catch { invalid(); }
  const hostname = url.hostname.toLowerCase();
  const expected = match ? `https://${hostname}/event/${match[1]}/` : "";
  if (!match || url.protocol !== "https:" || url.username || url.password || url.port
    || !/^(?:[a-z0-9-]+\.)?connpass\.com$/.test(hostname)
    || url.pathname !== `/event/${match[1]}/` || value !== expected) invalid();
  return expected;
}

function meetupUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const eventMatch = MEETUP_EVENT_REF.exec(eventRef);
  let url;
  try { url = new URL(value); } catch { invalid(); }
  const slugMatch = /^\/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\/events\/([1-9][0-9]*)\/$/.exec(url.pathname);
  const expected = eventMatch && slugMatch && slugMatch[2] === eventMatch[1]
    ? `https://www.meetup.com/${slugMatch[1]}/events/${eventMatch[1]}/` : "";
  if (!expected || url.protocol !== "https:" || url.hostname !== "www.meetup.com"
    || url.username || url.password || url.port || value !== expected) invalid();
  return expected;
}

function doorkeeperUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const eventMatch = DOORKEEPER_EVENT_REF.exec(eventRef);
  let url;
  try { url = new URL(value); } catch { invalid(); }
  const groupMatch = /^([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.doorkeeper\.jp$/.exec(url.hostname);
  const expected = eventMatch && groupMatch
    ? `https://${groupMatch[1]}.doorkeeper.jp/events/${eventMatch[1]}` : "";
  if (!expected || groupMatch[1] === "www" || url.protocol !== "https:" || url.username || url.password || url.port
    || url.pathname !== `/events/${eventMatch[1]}` || value !== expected) invalid();
  return expected;
}

function eventbriteUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const eventMatch = EVENTBRITE_EVENT_REF.exec(eventRef);
  let url;
  try { url = new URL(value); } catch { invalid(); }
  const pathMatch = EVENTBRITE_EVENT_PATH.exec(url.pathname);
  const eventId = pathMatch ? pathMatch[1] || pathMatch[2] : "";
  const expected = eventMatch && eventId === eventMatch[1]
    ? `https://www.eventbrite.com${url.pathname}` : "";
  if (!expected || url.protocol !== "https:" || url.hostname !== "www.eventbrite.com"
    || url.username || url.password || url.port || url.search || url.hash || value !== expected) invalid();
  return value;
}

function techplayUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const eventMatch = TECHPLAY_EVENT_REF.exec(eventRef);
  const expected = eventMatch ? `https://techplay.jp/event/${eventMatch[1]}` : "";
  if (!eventMatch || value !== expected) invalid();
  return expected;
}

function kokuchproUrl(value, eventRef) {
  if (typeof eventRef !== "string" || typeof value !== "string") invalid();
  const eventMatch = KOKUCHPRO_EVENT_REF.exec(eventRef);
  const eventPath = eventMatch ? `/event/${eventMatch[1]}${eventMatch[2] ? `/${eventMatch[2]}` : ""}/` : "";
  const expected = eventMatch ? `https://www.kokuchpro.com${eventPath}` : "";
  let url;
  try { url = new URL(value); } catch { invalid(); }
  if (!eventMatch || url.protocol !== "https:" || url.hostname !== "www.kokuchpro.com"
    || url.username || url.password || url.port || url.search || url.hash
    || url.pathname !== eventPath || value !== expected) invalid();
  return expected;
}

function kokuchproPageUrl(value, eventUrl) {
  if (typeof value !== "string" || typeof eventUrl !== "string") invalid();
  if (value === eventUrl) return eventUrl;
  let url;
  try { url = new URL(value); } catch { invalid(); }
  const canonicalPath = new URL(eventUrl).pathname;
  if (!value.startsWith("https://www.kokuchpro.com/") || url.protocol !== "https:"
    || url.hostname !== "www.kokuchpro.com" || url.username || url.password || url.port || url.search || url.hash
    || url.toString() !== value || !url.pathname.startsWith(canonicalPath) || url.pathname.length <= canonicalPath.length) invalid();
  return value;
}

async function verifyKokuchProPage({ provider, page, candidate, eventUrl }) {
  if (!provider || typeof provider.readProviderState !== "function" || !page || typeof page.url !== "function") invalid();
  let beforeUrl;
  try { beforeUrl = String(page.url()); } catch { invalid(); }
  kokuchproPageUrl(beforeUrl, eventUrl);
  let providerState;
  try { providerState = await provider.readProviderState({ page, candidate }); } catch { invalid(); }
  if (!providerState || typeof providerState !== "object" || Array.isArray(providerState) || providerState.status !== "registered") invalid();
  let afterReadbackUrl;
  try { afterReadbackUrl = String(page.url()); } catch { invalid(); }
  if (afterReadbackUrl !== beforeUrl) invalid();
  kokuchproPageUrl(afterReadbackUrl, eventUrl);
  return beforeUrl;
}

function calendarReceipt(value) {
  const id = text(value && value.id, 1_024);
  let url;
  try { url = new URL(String(value && value.htmlLink || "")); } catch { invalid(); }
  if (
    url.protocol !== "https:" || !["www.google.com", "google.com", "calendar.google.com"].includes(url.hostname)
    || url.pathname !== "/calendar/event" || !url.searchParams.get("eid") || url.username || url.password
  ) invalid();
  return Object.freeze({ id, htmlLink: url.toString() });
}

function privateStateDir(value) {
  const directory = path.resolve(String(value || ""));
  if (!path.isAbsolute(directory) || directory === path.parse(directory).root) invalid();
  assertNoSymlinkPath(path.dirname(directory), directory);
  let existing;
  try { existing = fs.lstatSync(directory); } catch (error) {
    if (!error || error.code !== "ENOENT") invalid();
  }
  if (!existing) {
    try { fs.mkdirSync(directory, { recursive: true, mode: 0o700 }); } catch { invalid(); }
    try { existing = fs.lstatSync(directory); } catch { invalid(); }
  }
  if (!existing.isDirectory() || existing.isSymbolicLink() || (existing.mode & 0o777) !== 0o700) invalid();
  return directory;
}

function sameIdentity(left, right) { return left.dev === right.dev && left.ino === right.ino; }

function assertPrivateDirectoryStat(stat, expectedMode) {
  if (!stat || !stat.isDirectory() || stat.isSymbolicLink() || (expectedMode != null && (stat.mode & 0o777) !== expectedMode)) invalid();
}

function openDirectoryIdentity(directory, expectedMode) {
  let expected;
  try { expected = fs.lstatSync(directory); } catch { invalid(); }
  assertPrivateDirectoryStat(expected, expectedMode);
  let descriptor;
  try {
    descriptor = fs.openSync(directory, fs.constants.O_RDONLY | NO_FOLLOW);
    const opened = fs.fstatSync(descriptor);
    assertPrivateDirectoryStat(opened, expectedMode);
    if (!sameIdentity(expected, opened)) invalid();
    const current = fs.lstatSync(directory);
    assertPrivateDirectoryStat(current, expectedMode);
    if (!sameIdentity(opened, current)) invalid();
    return { directory, descriptor, dev: opened.dev, ino: opened.ino, expectedMode };
  } catch (error) {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* preserve the identity error */ }
    }
    throw error;
  }
}

function verifyDirectoryIdentity(identity) {
  const opened = fs.fstatSync(identity.descriptor);
  assertPrivateDirectoryStat(opened, identity.expectedMode);
  if (opened.dev !== identity.dev || opened.ino !== identity.ino) invalid();
  let current;
  try { current = fs.lstatSync(identity.directory); } catch { invalid(); }
  assertPrivateDirectoryStat(current, identity.expectedMode);
  if (!sameIdentity(opened, current)) invalid();
}

function syncDirectoryIdentity(identity) {
  verifyDirectoryIdentity(identity);
  fs.fsyncSync(identity.descriptor);
  verifyDirectoryIdentity(identity);
}

function syncDirectory(directory, expectedMode) {
  const identity = openDirectoryIdentity(directory, expectedMode);
  try { syncDirectoryIdentity(identity); }
  finally { fs.closeSync(identity.descriptor); }
}

function ensurePrivateDirectory(directory) {
  let created = false;
  let existing;
  try { existing = fs.lstatSync(directory); } catch (error) {
    if (!error || error.code !== "ENOENT") invalid();
  }
  if (!existing) {
    try { fs.mkdirSync(directory, { recursive: true, mode: 0o700 }); } catch { invalid(); }
    try { existing = fs.lstatSync(directory); } catch { invalid(); }
    syncDirectory(path.dirname(directory));
    created = true;
  }
  if (!existing.isDirectory() || existing.isSymbolicLink() || (existing.mode & 0o777) !== 0o700) invalid();
  let parent;
  try { parent = fs.lstatSync(path.dirname(directory)); } catch { invalid(); }
  if (!parent.isDirectory() || parent.isSymbolicLink() || (parent.mode & 0o777) !== 0o700) invalid();
  return created;
}

function readPrivateBytes(root, file, maxSize) {
  assertNoSymlinkPath(root, file);
  let expected;
  try { expected = fs.lstatSync(file); } catch (error) {
    if (error && error.code === "ENOENT") return undefined;
    invalid();
  }
  if (!expected.isFile() || (expected.mode & 0o777) !== 0o600 || expected.size > maxSize) invalid();
  let descriptor;
  try {
    descriptor = fs.openSync(file, fs.constants.O_RDONLY | NO_FOLLOW);
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || (stat.mode & 0o777) !== 0o600 || stat.size > maxSize
      || stat.dev !== expected.dev || stat.ino !== expected.ino) invalid();
    return fs.readFileSync(descriptor);
  } catch { invalid(); }
  finally { if (descriptor !== undefined) fs.closeSync(descriptor); }
}

function readPrivateJson(root, file, maxSize) {
  const bytes = readPrivateBytes(root, file, maxSize);
  if (bytes === undefined) return undefined;
  try { return JSON.parse(bytes.toString("utf8")); } catch { invalid(); }
}

function immutableJson(root, file, value) {
  const bytes = Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8");
  const directory = path.dirname(file);
  assertNoSymlinkPath(root, file);
  ensurePrivateDirectory(directory);
  const existing = readPrivateBytes(root, file, 16_384);
  if (existing !== undefined) {
    if (!existing.equals(bytes)) invalid();
    return;
  }
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  assertNoSymlinkPath(root, temporary);
  let descriptor;
  try {
    descriptor = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | NO_FOLLOW, 0o600);
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || (stat.mode & 0o777) !== 0o600) invalid();
    fs.fchmodSync(descriptor, 0o600);
    let offset = 0;
    while (offset < bytes.length) offset += fs.writeSync(descriptor, bytes, offset, bytes.length - offset, null);
    fs.fsyncSync(descriptor);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
  try {
    fs.renameSync(temporary, file);
    const written = fs.lstatSync(file);
    if (!written.isFile() || (written.mode & 0o777) !== 0o600) invalid();
    syncDirectory(directory, 0o700);
  } finally {
    try { fs.unlinkSync(temporary); } catch (error) { if (!error || error.code !== "ENOENT") throw error; }
  }
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function providerReceiptId(tenantId, eventRef, observedAt, artifactSha) { return sha256(`${tenantId}\n${eventRef}\n${observedAt}\n${artifactSha}`); }

function checkpointPath(stateDir, provider, eventRef, eventUrl) {
  const canonicalUrlSha256 = sha256(eventUrl); const identity = sha256(`${provider}\n${eventRef}\n${canonicalUrlSha256}`);
  const file = path.join(stateDir, "evidence", "checkpoints", `${identity}.json`); assertNoSymlinkPath(stateDir, file);
  return { canonicalUrlSha256, file };
}

const CHECKPOINT_KEYS = "artifact_ref,artifact_sha256,canonical_url_sha256,event_ref,observed_at,provider,provider_receipt_ref,provider_status,schema_version,stage";
const MESSAGE_DELIVERY_KEYS = "artifact_ref,artifact_sha256,calendar_event_id,calendar_event_url,calendar_readback_at,canonical_url_sha256,event_ref,provider,provider_receipt_ref,schema_version,stage,telegram_message_provider_id";
const PHOTO_DELIVERY_KEYS = "artifact_ref,artifact_sha256,calendar_event_id,calendar_event_url,calendar_readback_at,canonical_url_sha256,event_ref,message_checkpoint_sha256,provider,provider_receipt_ref,schema_version,stage,telegram_message_provider_id,telegram_photo_provider_id";

function readCheckpoint(stateDir, file, provider, candidate, canonicalUrlSha256) {
  const value = readPrivateJson(stateDir, file, 16_384);
  if (value === undefined) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  if (value.stage !== "evidence" || Object.keys(value).sort().join(",") !== CHECKPOINT_KEYS || value.schema_version !== 1
    || value.provider !== provider.name || value.event_ref !== candidate.event_ref || value.canonical_url_sha256 !== canonicalUrlSha256 || !provider.states.includes(value.provider_status)) invalid();
  const receiptRef = String(value.provider_receipt_ref || ""); const artifactRef = String(value.artifact_ref || "");
  const artifactMatch = ARTIFACT_REF.exec(artifactRef);
  if (!provider.receiptRef.test(receiptRef) || !artifactMatch || artifactMatch[1] !== value.artifact_sha256
    || !/^[0-9a-f]{64}$/.test(String(value.artifact_sha256 || ""))) invalid();
  return Object.freeze({ stage: value.stage, eventRef: value.event_ref, observedAt: exactInstant(value.observed_at), receiptRef, artifactRef, artifactSha256: value.artifact_sha256, status: value.provider_status });
}

async function validateCheckpointEvidence(provider, checkpoint, tenantId) {
  const store = provider.store;
  if (!store || typeof store.readExternalReceipt !== "function" || typeof store.readArtifact !== "function") invalid();
  let receipt;
  try { receipt = await store.readExternalReceipt(tenantId, checkpoint.receiptRef); } catch { invalid(); }
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || receipt.kind !== "provider_response") invalid();
  if (["connpass", "meetup", "doorkeeper", "eventbrite", "techplay", "kokuchpro"].includes(provider.name) && (
    Object.keys(receipt).sort().join(",") !== "artifact_sha256,event_ref,kind,observed_at,provider_id"
    || typeof receipt.observed_at !== "string" || typeof receipt.event_ref !== "string" || typeof receipt.artifact_sha256 !== "string"
  )) invalid();
  const receiptMatch = provider.receiptRef.exec(checkpoint.receiptRef);
  if (!receiptMatch || receipt.provider_id !== receiptMatch[1] || receipt.provider_id !== providerReceiptId(tenantId, checkpoint.eventRef, checkpoint.observedAt, checkpoint.artifactSha256)) invalid();
  if (receipt.observed_at != null && exactInstant(receipt.observed_at) !== checkpoint.observedAt || receipt.event_ref != null && receipt.event_ref !== checkpoint.eventRef || receipt.artifact_sha256 != null && receipt.artifact_sha256 !== checkpoint.artifactSha256) invalid();
  let bytes;
  try { bytes = await store.readArtifact(tenantId, checkpoint.artifactRef); } catch { invalid(); }
  if (!Buffer.isBuffer(bytes) || bytes.length < 5_000 || !bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE) || sha256(bytes) !== checkpoint.artifactSha256) invalid();
  return bytes;
}

function deliveryPaths(stateDir, provider, candidate, identity, checkpoint) {
  const deliveryIdentity = sha256(`${provider.name}\n${candidate.event_ref}\n${identity.canonicalUrlSha256}\n${checkpoint.receiptRef}\n${checkpoint.artifactSha256}`);
  const root = path.join(stateDir, "evidence", "checkpoints");
  const claimRoot = path.join(stateDir, "evidence", DELIVERY_CLAIM_DIR);
  const message = path.join(root, `${deliveryIdentity}.message.json`);
  const photo = path.join(root, `${deliveryIdentity}.photo.json`);
  const messageClaim = path.join(claimRoot, `${deliveryIdentity}.message.claim`);
  const photoClaim = path.join(claimRoot, `${deliveryIdentity}.photo.claim`);
  assertNoSymlinkPath(stateDir, message); assertNoSymlinkPath(stateDir, photo);
  assertNoSymlinkPath(stateDir, messageClaim); assertNoSymlinkPath(stateDir, photoClaim);
  return { message, photo, messageClaim, photoClaim };
}

function deliveryKeys(stage) { return stage === "telegram_message" ? MESSAGE_DELIVERY_KEYS : PHOTO_DELIVERY_KEYS; }

function deliveryId(value) { return providerMessageId({ messageId: value }); }

function acquireDeliveryClaim(file, stage, stateDir) {
  const directory = path.dirname(file);
  if (ensurePrivateDirectory(directory)) syncDirectory(stateDir, 0o700);
  const parent = openDirectoryIdentity(directory, 0o700);
  let descriptor;
  try {
    verifyDirectoryIdentity(parent);
    try {
      descriptor = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | NO_FOLLOW, 0o600);
    } catch (error) {
      if (error && error.code === "EEXIST") return false;
      throw error;
    }
    const opened = fs.fstatSync(descriptor);
    if (!opened.isFile() || opened.isSymbolicLink() || (opened.mode & 0o777) !== 0o600) invalid();
    const current = fs.lstatSync(file);
    if (!current.isFile() || current.isSymbolicLink() || (current.mode & 0o777) !== 0o600 || !sameIdentity(opened, current)) invalid();
    const bytes = Buffer.from(`${JSON.stringify({ schema_version: 1, stage })}\n`, "utf8");
    fs.fchmodSync(descriptor, 0o600);
    let offset = 0;
    while (offset < bytes.length) offset += fs.writeSync(descriptor, bytes, offset, bytes.length - offset, null);
    fs.fsyncSync(descriptor);
    const written = fs.fstatSync(descriptor);
    const writtenPath = fs.lstatSync(file);
    if (!written.isFile() || (written.mode & 0o777) !== 0o600 || !sameIdentity(written, writtenPath)) invalid();
    syncDirectoryIdentity(parent);
    return true;
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.closeSync(parent.descriptor);
  }
}

function releaseDeliveryClaim(file, stage) {
  const directory = path.dirname(file);
  ensurePrivateDirectory(directory);
  const parent = openDirectoryIdentity(directory, 0o700);
  let descriptor;
  try {
    verifyDirectoryIdentity(parent);
    let stat;
    try { stat = fs.lstatSync(file); } catch (error) { if (error && error.code === "ENOENT") throw new Error("delivery claim disappeared"); throw error; }
    if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o600) invalid();
    descriptor = fs.openSync(file, fs.constants.O_RDONLY | NO_FOLLOW);
    const opened = fs.fstatSync(descriptor);
    if (!opened.isFile() || opened.isSymbolicLink() || (opened.mode & 0o777) !== 0o600 || !sameIdentity(stat, opened)) invalid();
    verifyDirectoryIdentity(parent);
    fs.unlinkSync(file);
    verifyDirectoryIdentity(parent);
    fs.fsyncSync(parent.descriptor);
    verifyDirectoryIdentity(parent);
    try { fs.lstatSync(file); throw new Error("delivery claim remained"); }
    catch (error) { if (!error || error.code !== "ENOENT") throw error; }
  } catch (error) {
    try { acquireDeliveryClaim(file, stage, path.dirname(path.dirname(directory))); } catch { /* caller records a stable unknown result */ }
    throw error;
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.closeSync(parent.descriptor);
  }
}

function knownProviderRejection(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && value.ok === false && value.delivery_unknown !== true;
}

function readDeliveryCheckpoint(stateDir, file, stage, provider, candidate, identity, checkpoint) {
  const value = readPrivateJson(stateDir, file, 16_384);
  if (value === undefined) return null;
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== deliveryKeys(stage)) invalid();
  if (value.schema_version !== 1 || value.stage !== stage || value.provider !== provider.name || value.event_ref !== candidate.event_ref
    || value.canonical_url_sha256 !== identity.canonicalUrlSha256 || value.provider_receipt_ref !== checkpoint.receiptRef
    || value.artifact_ref !== checkpoint.artifactRef || value.artifact_sha256 !== checkpoint.artifactSha256) invalid();
  const calendar = calendarReceipt({ id: value.calendar_event_id, htmlLink: value.calendar_event_url });
  const readbackAt = exactInstant(value.calendar_readback_at);
  const messageId = deliveryId(value.telegram_message_provider_id, "telegram_message_provider_id");
  const photoId = stage === "telegram_photo" ? deliveryId(value.telegram_photo_provider_id, "telegram_photo_provider_id") : null;
  const messageCheckpointSha256 = stage === "telegram_photo" ? String(value.message_checkpoint_sha256 || "") : null;
  if (stage === "telegram_photo" && !/^[0-9a-f]{64}$/.test(messageCheckpointSha256)) invalid();
  return Object.freeze({
    ...value,
    calendarEventId: calendar.id,
    calendarEventUrl: calendar.htmlLink,
    calendarReadbackAt: readbackAt,
    telegramMessageProviderId: messageId,
    telegramPhotoProviderId: photoId,
    messageCheckpointSha256,
    checkpointSha256: sha256(stableJson(value)),
  });
}

function deliveryValue({ stage, provider, candidate, identity, checkpoint, calendar, calendarReadbackAt, messageId, photoId, messageCheckpointSha256 }) {
  const value = {
    schema_version: 1,
    stage,
    provider: provider.name,
    event_ref: candidate.event_ref,
    canonical_url_sha256: identity.canonicalUrlSha256,
    provider_receipt_ref: checkpoint.receiptRef,
    artifact_ref: checkpoint.artifactRef,
    artifact_sha256: checkpoint.artifactSha256,
    calendar_event_id: calendar.id,
    calendar_event_url: calendar.htmlLink,
    calendar_readback_at: calendarReadbackAt,
    telegram_message_provider_id: messageId,
  };
  if (stage === "telegram_photo") {
    value.message_checkpoint_sha256 = messageCheckpointSha256;
    value.telegram_photo_provider_id = photoId;
  }
  return Object.freeze(value);
}

const BUNDLE_KEYS = "artifact_ref,artifact_sha256,bundle_id,calendar_event_id,calendar_event_url,calendar_readback_at,created_at,event_ref,provider,provider_receipt_ref,provider_status,schema_version,status,telegram_message_provider_id,telegram_photo_provider_id";
const BUNDLE_FILE = /^([0-9a-f]{64})\.json$/;
const MAX_BUNDLE_ENTRIES = 128;
const MAX_BUNDLE_BYTES = 16_384;

function validateAppliedBundle(value, file, providers) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).sort().join(",") !== BUNDLE_KEYS
    || value.schema_version !== 1 || value.status !== "applied_bundle") invalid();
  const provider = providers[value.provider];
  if (!provider || typeof value.event_ref !== "string" || !provider.eventRef.test(value.event_ref)
    || typeof value.provider_status !== "string" || !provider.states.includes(value.provider_status)) invalid();
  const receiptRef = String(value.provider_receipt_ref || "");
  const receiptMatch = provider.receiptRef.exec(receiptRef);
  const artifactRef = String(value.artifact_ref || "");
  const artifactMatch = ARTIFACT_REF.exec(artifactRef);
  const artifactSha = String(value.artifact_sha256 || "");
  if (!receiptMatch || !artifactMatch || !/^[0-9a-f]{64}$/.test(artifactSha) || artifactMatch[1] !== artifactSha) invalid();
  const calendar = calendarReceipt({ id: value.calendar_event_id, htmlLink: value.calendar_event_url });
  if (calendar.htmlLink !== value.calendar_event_url) invalid();
  const calendarReadbackAt = exactInstant(value.calendar_readback_at);
  const createdAt = exactInstant(value.created_at);
  const messageId = deliveryId(value.telegram_message_provider_id, "telegram_message_provider_id");
  const photoId = deliveryId(value.telegram_photo_provider_id, "telegram_photo_provider_id");
  const core = {
    schema_version: value.schema_version,
    status: value.status,
    provider: value.provider,
    event_ref: value.event_ref,
    provider_status: value.provider_status,
    provider_receipt_ref: receiptRef,
    artifact_ref: artifactRef,
    artifact_sha256: artifactSha,
    calendar_event_id: calendar.id,
    calendar_event_url: calendar.htmlLink,
    calendar_readback_at: calendarReadbackAt,
    telegram_message_provider_id: messageId,
    telegram_photo_provider_id: photoId,
    created_at: createdAt,
  };
  const digest = sha256(stableJson(core));
  const filename = path.basename(file, ".json");
  if (filename !== digest || value.bundle_id !== `applied-bundle:${digest}`) invalid();
  return Object.freeze({ ...value, ...core, bundle_id: value.bundle_id });
}

function scanAppliedBundles(stateDir, providers, provider, candidate, status) {
  const directory = path.join(stateDir, "applied-bundles");
  assertNoSymlinkPath(stateDir, directory);
  let stat;
  try { stat = fs.lstatSync(directory); }
  catch (error) { if (error && error.code === "ENOENT") return []; invalid(); }
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o700) invalid();
  let entries;
  try { entries = fs.readdirSync(directory); } catch { invalid(); }
  if (entries.length > MAX_BUNDLE_ENTRIES) invalid();
  const matches = [];
  for (const name of entries) {
    const match = BUNDLE_FILE.exec(name);
    if (!match) invalid();
    const file = path.join(directory, name);
    assertNoSymlinkPath(stateDir, file);
    let fileStat;
    try { fileStat = fs.lstatSync(file); } catch { invalid(); }
    if (!fileStat.isFile() || fileStat.isSymbolicLink() || (fileStat.mode & 0o777) !== 0o600 || fileStat.size > MAX_BUNDLE_BYTES) invalid();
    let value;
    try { value = JSON.parse(fs.readFileSync(file, "utf8")); } catch { invalid(); }
    const bundle = validateAppliedBundle(value, file, providers);
    if (bundle.provider === provider && bundle.event_ref === candidate.event_ref) {
      if (bundle.provider_status !== status) invalid();
      matches.push(bundle);
    }
  }
  if (matches.length > 1) invalid();
  return matches;
}

async function validateBundleEvidence(provider, bundle, tenantId) {
  await validateCheckpointEvidence(provider, {
    eventRef: bundle.event_ref,
    observedAt: bundle.created_at,
    receiptRef: bundle.provider_receipt_ref,
    artifactRef: bundle.artifact_ref,
    artifactSha256: bundle.artifact_sha256,
  }, tenantId);
}

async function validateExistingCheckpoints(stateDir, provider, candidate, identity, bundle, tenantId) {
  const checkpoint = readCheckpoint(stateDir, identity.file, provider, candidate, identity.canonicalUrlSha256);
  if (!checkpoint) return null;
  if (checkpoint.status !== bundle.provider_status || checkpoint.eventRef !== bundle.event_ref
    || checkpoint.observedAt !== bundle.created_at || checkpoint.receiptRef !== bundle.provider_receipt_ref
    || checkpoint.artifactRef !== bundle.artifact_ref || checkpoint.artifactSha256 !== bundle.artifact_sha256) invalid();
  await validateCheckpointEvidence(provider, checkpoint, tenantId);
  const deliveries = deliveryPaths(stateDir, provider, candidate, identity, checkpoint);
  const message = readDeliveryCheckpoint(stateDir, deliveries.message, "telegram_message", provider, candidate, identity, checkpoint);
  const photo = readDeliveryCheckpoint(stateDir, deliveries.photo, "telegram_photo", provider, candidate, identity, checkpoint);
  if (!message || !photo || photo.messageCheckpointSha256 !== message.checkpointSha256
    || photo.telegramMessageProviderId !== message.telegramMessageProviderId
    || photo.calendarReadbackAt !== message.calendarReadbackAt
    || photo.calendarEventId !== bundle.calendar_event_id || photo.calendarEventUrl !== bundle.calendar_event_url
    || message.calendarEventId !== bundle.calendar_event_id || message.calendarEventUrl !== bundle.calendar_event_url
    || message.telegramMessageProviderId !== bundle.telegram_message_provider_id
    || photo.telegramPhotoProviderId !== bundle.telegram_photo_provider_id) invalid();
  return checkpoint;
}

async function captureProviderEvidence({ provider, providerName, page, candidate, providerStatus, tenantId, now, eventUrl, canonicalUrlSha256 }) {
  const observedAt = exactInstant(now());
  // Luma and Peatix are the only two providers left once connpass/meetup/doorkeeper/eventbrite/techplay
  // are excluded, and they now render the receipt with the identical about:blank + document.write
  // technique (see below) because page.setContent() hangs on the live CloakBrowser daily-driver over
  // CDP (verified 2026-08-16). One shared branch, one shared capability requirement.
  if (providerName === "connpass" || providerName === "meetup" || providerName === "doorkeeper" || providerName === "eventbrite" || providerName === "techplay" || providerName === "kokuchpro"
    ? typeof page.screenshot !== "function"
    : typeof page.goto !== "function" || typeof page.url !== "function" || typeof page.evaluate !== "function") invalid();
  if (typeof page.screenshot !== "function") invalid();
  const receipt = providerName === "meetup" || providerName === "kokuchpro" ? null : receiptHtml(providerName, providerStatus, candidate.event_ref);
  if (providerName !== "connpass" && providerName !== "meetup" && providerName !== "doorkeeper" && providerName !== "eventbrite" && providerName !== "techplay" && providerName !== "kokuchpro") {
    try {
      await page.goto("about:blank", { waitUntil: "domcontentloaded", timeout: 30_000 });
      if (String(page.url()) !== "about:blank") invalid();
      const rendered = await page.evaluate((html) => { document.open(); document.write(html); document.close(); const root = document.querySelector("body > dl"); return root !== null && document.querySelectorAll("body > dl > dt").length === 3 && document.querySelectorAll("body > dl > dd").length === 3; }, receipt);
      if (rendered !== true) invalid();
    } catch { invalid(); }
  }
  let captureUrl;
  if (providerName === "kokuchpro") captureUrl = await verifyKokuchProPage({ provider, page, candidate, eventUrl });
  else captureUrl = providerName === "connpass" || providerName === "meetup" || providerName === "doorkeeper" || providerName === "eventbrite" || providerName === "techplay"
    ? eventUrl : "about:blank";
  const exactCapturePage = () => {
    let currentUrl;
    try { currentUrl = String(page.url()); } catch { invalid(); }
    if (currentUrl !== captureUrl) invalid();
  };
  exactCapturePage();
  let screenshot;
  try {
    screenshot = await page.screenshot({ type: "png", fullPage: true });
  } catch {
    exactCapturePage();
    screenshot = await page.screenshot({ type: "png", fullPage: false });
  }
  exactCapturePage();
  if (!Buffer.isBuffer(screenshot) || screenshot.length < 5_000 || !screenshot.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) invalid();
  if (providerName === "kokuchpro") kokuchproPageUrl(captureUrl, eventUrl);
  const artifactSha = sha256(screenshot);
  const evidence = await provider.store.record({ tenantId, eventRef: candidate.event_ref, observedAt, screenshot });
  const artifactMatch = ARTIFACT_REF.exec(String(evidence && evidence.artifact_ref || ""));
  const receiptRef = String(evidence && evidence.external_receipt_ref || ""); const receiptMatch = provider.receiptRef.exec(receiptRef);
  if (!receiptMatch || receiptMatch[1] !== providerReceiptId(tenantId, candidate.event_ref, observedAt, artifactSha) || !artifactMatch || artifactMatch[1] !== artifactSha) invalid();
  return { screenshot, checkpointValue: { schema_version: 1, stage: "evidence", provider: providerName, event_ref: candidate.event_ref, canonical_url_sha256: canonicalUrlSha256, provider_status: providerStatus, provider_receipt_ref: evidence.external_receipt_ref, artifact_ref: evidence.artifact_ref, artifact_sha256: artifactSha, observed_at: observedAt } };
}

function createMinimalEvidenceChain(options = {}) {
  const stateDir = privateStateDir(options.stateDir);
  const tenantId = text(options.tenantId, 128);
  const calendar = options.calendar;
  const calendarId = text(options.calendarId, 1_024);
  const telegramTarget = text(options.telegramTarget, 200);
  const timeZone = timeZoneName(options.timeZone || DEFAULT_TIME_ZONE);
  const lumaEvidenceStore = options.evidenceStore || createLumaEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const peatixEvidenceStore = options.peatixEvidenceStore || createPeatixEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const connpassEvidenceStore = options.connpassEvidenceStore || createConnpassEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const meetupEvidenceStore = options.meetupEvidenceStore || createMeetupEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const doorkeeperEvidenceStore = options.doorkeeperEvidenceStore || createDoorkeeperEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const eventbriteEvidenceStore = options.eventbriteEvidenceStore || createEventbriteEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const techplayEvidenceStore = options.techplayEvidenceStore || createTechPlayEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const kokuchproEvidenceStore = options.kokuchproEvidenceStore || createKokuchProEvidenceStore({
    dataDir: path.join(stateDir, "evidence"),
  });
  const kokuchproWorkflow = createKokuchProDiscoveryWorkflow();
  const providers = {
    luma: { name: "luma", eventRef: EVENT_REF, receiptRef: RECEIPT_REF, url: lumaUrl, states: ["registered", "pending"], store: lumaEvidenceStore },
    peatix: { name: "peatix", eventRef: PEATIX_EVENT_REF, receiptRef: PEATIX_RECEIPT_REF, url: peatixUrl, states: ["registered"], store: peatixEvidenceStore },
    connpass: { name: "connpass", eventRef: CONNPASS_EVENT_REF, receiptRef: CONNPASS_RECEIPT_REF, url: connpassUrl, states: ["registered", "pending"], store: connpassEvidenceStore },
    meetup: { name: "meetup", eventRef: MEETUP_EVENT_REF, receiptRef: MEETUP_RECEIPT_REF, url: meetupUrl, states: ["registered"], store: meetupEvidenceStore },
    doorkeeper: { name: "doorkeeper", eventRef: DOORKEEPER_EVENT_REF, receiptRef: DOORKEEPER_RECEIPT_REF, url: doorkeeperUrl, states: ["registered"], store: doorkeeperEvidenceStore },
    eventbrite: { name: "eventbrite", eventRef: EVENTBRITE_EVENT_REF, receiptRef: EVENTBRITE_RECEIPT_REF, url: eventbriteUrl, states: ["registered"], store: eventbriteEvidenceStore },
    techplay: { name: "techplay", eventRef: TECHPLAY_EVENT_REF, receiptRef: TECHPLAY_RECEIPT_REF, url: techplayUrl, states: ["registered"], store: techplayEvidenceStore },
    kokuchpro: { name: "kokuchpro", eventRef: KOKUCHPRO_EVENT_REF, receiptRef: KOKUCHPRO_RECEIPT_REF, url: kokuchproUrl, states: ["registered"], store: kokuchproEvidenceStore, readProviderState: kokuchproWorkflow.readProviderState },
  };
  const now = options.now || (() => new Date());
  const sendMessage = options.sendMessage;
  const sendPhoto = options.sendPhoto;
  if (
    !calendar || typeof calendar.findConnectorEvents !== "function"
    || typeof calendar.createConnectorEvent !== "function"
    || !lumaEvidenceStore || typeof lumaEvidenceStore.record !== "function"
    || !peatixEvidenceStore || typeof peatixEvidenceStore.record !== "function"
    || !connpassEvidenceStore || typeof connpassEvidenceStore.record !== "function"
    || !meetupEvidenceStore || typeof meetupEvidenceStore.record !== "function"
    || !doorkeeperEvidenceStore || typeof doorkeeperEvidenceStore.record !== "function"
    || !eventbriteEvidenceStore || typeof eventbriteEvidenceStore.record !== "function"
    || !techplayEvidenceStore || typeof techplayEvidenceStore.record !== "function"
    || !kokuchproEvidenceStore || typeof kokuchproEvidenceStore.record !== "function"
    || typeof now !== "function" || typeof sendMessage !== "function" || typeof sendPhoto !== "function"
  ) invalid();

  return Object.freeze({
    // Reconciliation-only lookup: lets a discovery step ask "does this
    // provider+event already have an applied bundle?" without running the
    // rest of the evidence chain. Reuses the exact same bundle store
    // scanAppliedBundles() reads inside completeEvidence(), so the answer
    // can never drift from what completeEvidence() itself would decide.
    async hasAppliedBundle(input = {}) {
      const provider = providers[input.provider];
      if (!provider) invalid();
      const eventRef = String(input.event_ref || "");
      if (!provider.eventRef.test(eventRef)) invalid();
      const status = String(input.provider_status || "");
      if (!provider.states.includes(status)) invalid();
      return scanAppliedBundles(stateDir, providers, provider.name, { event_ref: eventRef }, status).length === 1;
    },

    async completeEvidence(input = {}) {
      const provider = providers[input.provider];
      if (
        !provider || !input.page || typeof input.page !== "object"
        || !input.providerState || !provider.states.includes(input.providerState.status)
      ) invalid();
      const candidate = input.candidate;
      if (!candidate || !provider.eventRef.test(String(candidate.event_ref || ""))) invalid();
      const eventUrl = provider.url(candidate.canonical_url, candidate.event_ref);
      const title = text(candidate.title, 500);
      const startsAt = exactInstant(candidate.starts_at);
      const endsAt = exactInstant(candidate.ends_at);
      if (Date.parse(endsAt) <= Date.parse(startsAt)) invalid();
      const venue = text(candidate.venue_address || candidate.address || candidate.venue || candidate.venue_name || "See event page", 2_000);
      const identity = checkpointPath(stateDir, input.provider, candidate.event_ref, eventUrl);
      if (input.provider === "kokuchpro") {
        if (typeof input.page.url !== "function") invalid();
        let currentUrl;
        try { currentUrl = String(input.page.url()); } catch { invalid(); }
        kokuchproPageUrl(currentUrl, eventUrl);
      } else if (input.provider === "connpass" || input.provider === "meetup" || input.provider === "doorkeeper" || input.provider === "eventbrite" || input.provider === "techplay") {
        if (typeof input.page.url !== "function") invalid();
        let currentUrl;
        try { currentUrl = String(input.page.url()); } catch { invalid(); }
        if (currentUrl !== eventUrl) invalid();
      }
      const existing = scanAppliedBundles(stateDir, providers, provider.name, candidate, input.providerState.status);
      if (existing.length === 1) {
        const bundle = existing[0];
        const checkpoint = await validateExistingCheckpoints(stateDir, provider, candidate, identity, bundle, tenantId);
        if (!checkpoint) await validateBundleEvidence(provider, bundle, tenantId);
        const idempotencyValue = identity.canonicalUrlSha256;
        const timeMin = new Date(Date.parse(startsAt) - 60_000).toISOString();
        const timeMax = new Date(Date.parse(endsAt) + 60_000).toISOString();
        const current = await calendar.findConnectorEvents({ calendarId, idempotencyValue, timeMin, timeMax });
        if (!Array.isArray(current) || current.length !== 1) invalid();
        const verifiedCalendar = calendarReceipt(current[0]);
        if (verifiedCalendar.id !== bundle.calendar_event_id || verifiedCalendar.htmlLink !== bundle.calendar_event_url) invalid();
        return Object.freeze({ ...bundle, completion_disposition: "reused" });
      }
      let checkpoint;
      let screenshot;
      try {
        checkpoint = readCheckpoint(stateDir, identity.file, provider, candidate, identity.canonicalUrlSha256);
        if (checkpoint) {
          if (checkpoint.status !== input.providerState.status) invalid();
          screenshot = await validateCheckpointEvidence(provider, checkpoint, tenantId);
        } else {
          const captured = await captureProviderEvidence({ provider, providerName: input.provider, page: input.page, candidate, providerStatus: input.providerState.status, tenantId, now, eventUrl, canonicalUrlSha256: identity.canonicalUrlSha256 });
          assertNoSymlinkPath(stateDir, identity.file);
          immutableJson(stateDir, identity.file, captured.checkpointValue);
          checkpoint = readCheckpoint(stateDir, identity.file, provider, candidate, identity.canonicalUrlSha256);
          screenshot = captured.screenshot;
          if (input.provider === "meetup" || input.provider === "doorkeeper" || input.provider === "eventbrite" || input.provider === "techplay" || input.provider === "kokuchpro") screenshot = await validateCheckpointEvidence(provider, checkpoint, tenantId);
        }
      } catch (error) { throw preserveSafe(error, "EVIDENCE_SCREENSHOT_CAPTURE_FAILED"); }

      const deliveries = deliveryPaths(stateDir, provider, candidate, identity, checkpoint);
      let messageCheckpoint = readDeliveryCheckpoint(stateDir, deliveries.message, "telegram_message", provider, candidate, identity, checkpoint);
      let photoCheckpoint = readDeliveryCheckpoint(stateDir, deliveries.photo, "telegram_photo", provider, candidate, identity, checkpoint);
      if (photoCheckpoint && !messageCheckpoint) invalid();
      if (photoCheckpoint && (
        photoCheckpoint.messageCheckpointSha256 !== messageCheckpoint.checkpointSha256
        || photoCheckpoint.telegramMessageProviderId !== messageCheckpoint.telegramMessageProviderId
        || photoCheckpoint.calendarReadbackAt !== messageCheckpoint.calendarReadbackAt
      )) invalid();

      const idempotencyValue = identity.canonicalUrlSha256;
      const timeMin = new Date(Date.parse(startsAt) - 60_000).toISOString();
      const timeMax = new Date(Date.parse(endsAt) + 60_000).toISOString();
      let before;
      try {
        before = await calendar.findConnectorEvents({ calendarId, idempotencyValue, timeMin, timeMax });
        if (!Array.isArray(before) || before.length > 1) invalid();
      } catch (error) { throw preserveSafe(error, "EVIDENCE_CALENDAR_READBACK_FAILED"); }
      let expectedCalendar;
      if (before.length === 1) {
        try { expectedCalendar = calendarReceipt(before[0]); }
        catch (error) { throw preserveSafe(error, "EVIDENCE_CALENDAR_READBACK_FAILED"); }
      } else {
        try {
          expectedCalendar = calendarReceipt(await calendar.createConnectorEvent({ calendarId, idempotencyValue, title, startAt: startsAt, endAt: endsAt, location: venue, canonicalUrl: eventUrl }));
        } catch (error) { throw preserveSafe(error, "EVIDENCE_CALENDAR_CREATE_FAILED"); }
      }
      let verifiedCalendar;
      try {
        const after = await calendar.findConnectorEvents({ idempotencyValue, calendarId, timeMin, timeMax });
        if (!Array.isArray(after) || after.length !== 1) invalid();
        verifiedCalendar = calendarReceipt(after[0]);
        if (verifiedCalendar.id !== expectedCalendar.id || verifiedCalendar.htmlLink !== expectedCalendar.htmlLink) invalid();
      } catch (error) { throw preserveSafe(error, "EVIDENCE_CALENDAR_READBACK_FAILED"); }
      const calendarReadbackAt = exactInstant(now());
      for (const stored of [messageCheckpoint, photoCheckpoint]) {
        if (stored && (stored.calendarEventId !== verifiedCalendar.id || stored.calendarEventUrl !== verifiedCalendar.htmlLink)) invalid();
      }

      const status = input.providerState.status;
      let messageId = messageCheckpoint && messageCheckpoint.telegramMessageProviderId;
      try {
        if (!messageCheckpoint) {
          const messageText = [
            "Connector::: イベント申込を確認しました",
            `event: ${title}`,
            `starts at: ${localizedStartsAt(startsAt, timeZone)}`,
            `venue: ${venue}`,
            `provider: ${input.provider}`,
            `status: ${status}`,
            `event url: ${eventUrl}`,
            `calendar: ${verifiedCalendar.htmlLink}`,
          ].join("\n");
          if (messageText.length > 4_096) throw stageError("EVIDENCE_TELEGRAM_MESSAGE_FAILED");
          if (!acquireDeliveryClaim(deliveries.messageClaim, "telegram_message", stateDir)) throw stageError("EVIDENCE_TELEGRAM_MESSAGE_FAILED");
          let response;
          try { response = await sendMessage(escapeHtml(messageText), { telegramTarget, idempotencyKey: `connector-evidence:${idempotencyValue}` }); }
          catch (error) {
            if (error && error.knownNoEffect === true) {
              try { releaseDeliveryClaim(deliveries.messageClaim, "telegram_message"); }
              catch { throw stageError("EVIDENCE_TELEGRAM_MESSAGE_FAILED"); }
            }
            throw error;
          }
          if (knownProviderRejection(response)) {
            try { releaseDeliveryClaim(deliveries.messageClaim, "telegram_message"); }
            catch { throw stageError("EVIDENCE_TELEGRAM_MESSAGE_FAILED"); }
            throw stageError("EVIDENCE_TELEGRAM_MESSAGE_FAILED");
          }
          messageId = providerMessageId(response);
          const value = deliveryValue({ stage: "telegram_message", provider, candidate, identity, checkpoint, calendar: verifiedCalendar, calendarReadbackAt, messageId });
          assertNoSymlinkPath(stateDir, deliveries.message);
          immutableJson(stateDir, deliveries.message, value);
          messageCheckpoint = readDeliveryCheckpoint(stateDir, deliveries.message, "telegram_message", provider, candidate, identity, checkpoint);
          if (!messageCheckpoint) invalid();
        }
      } catch (error) { throw preserveSafe(error, "EVIDENCE_TELEGRAM_MESSAGE_FAILED"); }
      let photoId = photoCheckpoint && photoCheckpoint.telegramPhotoProviderId;
      try {
        if (!photoCheckpoint) {
          if (!acquireDeliveryClaim(deliveries.photoClaim, "telegram_photo", stateDir)) throw stageError("EVIDENCE_TELEGRAM_PHOTO_FAILED");
          const photoOptions = {
            telegramTarget,
            idempotencyKey: `connector-evidence-photo:${identity.canonicalUrlSha256}`,
            caption: input.provider === "luma" ? `Connector::: ${title} / ${status}` : `Connector::: ${input.provider} / ${title} / ${status}`,
          };
          let response;
          try { response = await sendPhoto(screenshot, photoOptions); }
          catch (error) {
            if (error && error.knownNoEffect === true) {
              try { releaseDeliveryClaim(deliveries.photoClaim, "telegram_photo"); }
              catch { throw stageError("EVIDENCE_TELEGRAM_PHOTO_FAILED"); }
            }
            throw error;
          }
          if (knownProviderRejection(response)) {
            try { releaseDeliveryClaim(deliveries.photoClaim, "telegram_photo"); }
            catch { throw stageError("EVIDENCE_TELEGRAM_PHOTO_FAILED"); }
            throw stageError("EVIDENCE_TELEGRAM_PHOTO_FAILED");
          }
          photoId = providerMessageId(response);
          const value = deliveryValue({
            stage: "telegram_photo", provider, candidate, identity, checkpoint, calendar: verifiedCalendar,
            calendarReadbackAt: messageCheckpoint.calendarReadbackAt,
            messageId, photoId, messageCheckpointSha256: messageCheckpoint.checkpointSha256,
          });
          assertNoSymlinkPath(stateDir, deliveries.photo);
          immutableJson(stateDir, deliveries.photo, value);
          photoCheckpoint = readDeliveryCheckpoint(stateDir, deliveries.photo, "telegram_photo", provider, candidate, identity, checkpoint);
          if (!photoCheckpoint || photoCheckpoint.messageCheckpointSha256 !== messageCheckpoint.checkpointSha256) invalid();
        }
      } catch (error) { throw preserveSafe(error, "EVIDENCE_TELEGRAM_PHOTO_FAILED"); }

      try {
        const core = Object.freeze({
          schema_version: 1,
          status: "applied_bundle",
          provider: input.provider,
          event_ref: candidate.event_ref,
          provider_status: status,
          provider_receipt_ref: checkpoint.receiptRef,
          artifact_ref: checkpoint.artifactRef,
          artifact_sha256: checkpoint.artifactSha256,
          calendar_event_id: verifiedCalendar.id,
          calendar_event_url: verifiedCalendar.htmlLink,
          calendar_readback_at: messageCheckpoint.calendarReadbackAt,
          telegram_message_provider_id: messageId,
          telegram_photo_provider_id: photoId,
          created_at: checkpoint.observedAt,
        });
        const digest = createHash("sha256").update(stableJson(core)).digest("hex");
        const bundle = Object.freeze({ bundle_id: `applied-bundle:${digest}`, ...core });
        const bundleFile = path.join(stateDir, "applied-bundles", `${digest}.json`);
        assertNoSymlinkPath(stateDir, bundleFile);
        immutableJson(stateDir, bundleFile, bundle);
        return Object.freeze({ ...bundle, completion_disposition: "created" });
      } catch (error) { throw preserveSafe(error, "EVIDENCE_BUNDLE_WRITE_FAILED"); }
    },
  });
}

module.exports = { createMinimalEvidenceChain };
