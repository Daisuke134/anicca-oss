"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { normalizeProduct, readMobileProducts, registerMobileProduct } = require("./mobile-product-registry.js");
const { materializeMobileStarterPack } = require("./mobile-starter-pack.js");

const ID = /^[a-z0-9][a-z0-9._-]{0,127}$/u;
const PRODUCT_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;
const SYMBOL = /^[A-Za-z][A-Za-z0-9]{0,63}$/u;
const BUNDLE_ID = /^[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+){2,}$/u;
const REQUIREMENT_NAMES = [
  "xcodegen", "xcodebuild", "apple_development_team", "app_store_connect_key_id",
  "app_store_connect_issuer_id", "app_store_connect_key_file",
];

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function validateOpportunity(raw) {
  requireValue(raw && typeof raw === "object" && !Array.isArray(raw), "opportunity required");
  requireValue(typeof raw.opportunity_id === "string" && ID.test(raw.opportunity_id), "opportunity_id invalid");
  requireValue(typeof raw.product_id === "string" && PRODUCT_ID.test(raw.product_id), "product_id invalid");
  requireValue(typeof raw.display_name === "string" && raw.display_name.trim(), "display_name required");
  requireValue(!/[\u0000-\u001f\u007f-\u009f]/u.test(raw.display_name),
    "display_name contains unsupported control characters");
  requireValue(typeof raw.product_symbol === "string" && SYMBOL.test(raw.product_symbol), "product_symbol invalid");
  requireValue(typeof raw.bundle_id === "string" && BUNDLE_ID.test(raw.bundle_id), "bundle_id invalid");
  requireValue(typeof raw.audience === "string" && raw.audience.trim(), "audience required");
  requireValue(typeof raw.problem === "string" && raw.problem.trim(), "problem required");
  requireValue(Array.isArray(raw.evidence_refs) && raw.evidence_refs.length > 0, "evidence_refs required");
  for (const value of raw.evidence_refs) {
    requireValue(typeof value === "string", "evidence_refs must be absolute HTTPS URLs");
    let reference;
    try { reference = new URL(value); } catch { throw new Error("evidence_refs must be absolute HTTPS URLs"); }
    requireValue(reference.protocol === "https:" && !reference.username && !reference.password,
      "evidence_refs must be absolute HTTPS URLs");
  }
  return Object.freeze({
    opportunity_id: raw.opportunity_id,
    product_id: raw.product_id,
    display_name: raw.display_name.trim(),
    product_symbol: raw.product_symbol,
    bundle_id: raw.bundle_id,
    audience: raw.audience.trim(),
    problem: raw.problem.trim(),
    evidence_refs: [...raw.evidence_refs],
  });
}

function defaultFindExecutable(name, environment) {
  for (const directory of String(environment.PATH || "").split(path.delimiter).filter(Boolean)) {
    const candidate = path.join(directory, name);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      if (fs.statSync(candidate).isFile()) return fs.realpathSync(candidate);
    } catch { /* keep searching */ }
  }
  return null;
}

function sourceTreeHash(sourceRoot) {
  const rows = [];
  const visit = (directory, prefix = "") => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => left.name.localeCompare(right.name))) {
      requireValue(!entry.isSymbolicLink(), "generated source must not contain symlinks");
      const relative = path.posix.join(prefix, entry.name);
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(absolute, relative);
      else {
        requireValue(entry.isFile(), "generated source must contain regular files only");
        rows.push(`${relative}\0${sha256(fs.readFileSync(absolute))}`);
      }
    }
  };
  visit(sourceRoot);
  return sha256(rows.join("\n"));
}

function pathContains(parent, candidate) {
  return candidate === parent || candidate.startsWith(`${parent}${path.sep}`);
}

function canonicalPotentialPath(candidate) {
  let current = path.resolve(candidate);
  const missing = [];
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    requireValue(parent !== current, "mobile product path has no existing ancestor");
    missing.unshift(path.basename(current));
    current = parent;
  }
  return path.join(fs.realpathSync(current), ...missing);
}

function potentialPathWithinDirectory(directory, candidate) {
  const owner = fs.statSync(directory);
  let current = path.resolve(candidate);
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) return false;
    current = parent;
  }
  current = fs.realpathSync(current);
  while (true) {
    const observed = fs.statSync(current);
    if (observed.dev === owner.dev && observed.ino === owner.ino) return true;
    const parent = path.dirname(current);
    if (parent === current) return false;
    current = parent;
  }
}

function writeReceipt(receiptFile, receipt) {
  const lockFile = `${receiptFile}.lock`;
  let lock;
  let temporary;
  let temporaryIdentity;
  try {
    lock = fs.openSync(lockFile, "wx", 0o600);
    let existingStat = null;
    try { existingStat = fs.lstatSync(receiptFile); } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    if (existingStat) {
      requireValue(existingStat.isFile() && !existingStat.isSymbolicLink(),
        "conflicting mobile bootstrap receipt");
      const existing = JSON.parse(fs.readFileSync(receiptFile, "utf8"));
      const exactKeys = Object.keys(receipt).sort();
      const canonicalMissing = REQUIREMENT_NAMES.filter((name) => existing.missing?.includes(name));
      requireValue(JSON.stringify(Object.keys(existing).sort()) === JSON.stringify(exactKeys)
        && existing.schema_version === "mobile.bootstrap.v1"
        && ["setup_required", "ready_to_build"].includes(existing.state)
        && Array.isArray(existing.missing)
        && JSON.stringify(existing.missing) === JSON.stringify(canonicalMissing)
        && existing.state === (existing.missing.length ? "setup_required" : "ready_to_build")
        && Array.isArray(existing.external_effects) && existing.external_effects.length === 0
        && existing.product_id === receipt.product_id
        && existing.opportunity_id === receipt.opportunity_id
        && existing.workspace_rel === receipt.workspace_rel
        && existing.source_sha256 === receipt.source_sha256
        && existing.input_fingerprint === receipt.input_fingerprint
        && stableJson(existing.build) === stableJson(receipt.build),
        "conflicting mobile bootstrap receipt");
      if (stableJson(existing) === stableJson(receipt)) {
        return { value: existing, written: false, created: false,
          dev: existingStat.dev, ino: existingStat.ino };
      }
      const nextMissing = new Set(receipt.missing);
      requireValue(existing.state === "setup_required"
        && receipt.missing.every((name) => existing.missing.includes(name))
        && existing.missing.some((name) => !nextMissing.has(name)),
        "mobile bootstrap capability regression");
    }
    const candidateTemporary = `${receiptFile}.tmp-${crypto.randomUUID()}`;
    const handle = fs.openSync(candidateTemporary, "wx", 0o600);
    temporary = candidateTemporary;
    const createdTemporary = fs.fstatSync(handle);
    temporaryIdentity = { dev: createdTemporary.dev, ino: createdTemporary.ino };
    try {
      fs.writeFileSync(handle, `${JSON.stringify(receipt, null, 2)}\n`);
      fs.fsyncSync(handle);
    } finally {
      fs.closeSync(handle);
    }
    if (existingStat) {
      const current = fs.lstatSync(receiptFile);
      requireValue(current.dev === existingStat.dev && current.ino === existingStat.ino,
        "conflicting mobile bootstrap receipt");
    } else {
      requireValue(!fs.existsSync(receiptFile), "conflicting mobile bootstrap receipt");
    }
    fs.renameSync(temporary, receiptFile);
    temporary = null;
    const written = fs.lstatSync(receiptFile);
    return { value: receipt, written: true, created: !existingStat,
      dev: written.dev, ino: written.ino };
  } finally {
    if (temporary) {
      try {
        const currentTemporary = fs.lstatSync(temporary);
        if (temporaryIdentity && currentTemporary.isFile() && !currentTemporary.isSymbolicLink()
            && currentTemporary.dev === temporaryIdentity.dev && currentTemporary.ino === temporaryIdentity.ino) {
          fs.unlinkSync(temporary);
        }
      } catch (error) { if (error?.code !== "ENOENT") throw error; }
    }
    if (lock !== undefined) {
      fs.closeSync(lock);
      fs.unlinkSync(lockFile);
    }
  }
}

function bootstrapGeneratedMobileProduct(options) {
  const opportunity = validateOpportunity(options.opportunity);
  const environment = options.environment || process.env;
  const findExecutable = options.findExecutable
    || ((name) => defaultFindExecutable(name, environment));
  const rawProduct = {
    product_id: opportunity.product_id,
    origin: "generated",
    source: { template_id: "ios-swiftui-v1", repository_name: opportunity.product_id },
  };
  const candidate = normalizeProduct(rawProduct);
  const existing = readMobileProducts(options.registryFile)
    .find((product) => product.product_id === opportunity.product_id);
  if (existing) requireValue(stableJson(existing) === stableJson(candidate), "conflicting mobile product");
  const product = existing || candidate;
  const requestedPrivateRoot = fs.realpathSync(path.resolve(options.privateRoot));
  const plannedSourceRoot = path.resolve(requestedPrivateRoot, product.workspace_rel, "source");
  const canonicalRegistryFile = canonicalPotentialPath(options.registryFile);
  requireValue(!pathContains(plannedSourceRoot, canonicalRegistryFile),
    "mobile product registry must not overlap generated source");
  const materialized = materializeMobileStarterPack({
    product,
    identity: {
      productSymbol: opportunity.product_symbol,
      displayName: opportunity.display_name,
      bundleId: opportunity.bundle_id,
    },
    packRoot: options.packRoot,
    privateRoot: options.privateRoot,
  });
  const sourceRoot = path.join(path.resolve(options.privateRoot), materialized.workspace_rel);
  const observedSourceStat = fs.lstatSync(sourceRoot);
  const observedSourceHash = sourceTreeHash(sourceRoot);
  const required = [
    ["xcodegen", Boolean(findExecutable("xcodegen"))],
    ["xcodebuild", Boolean(findExecutable("xcodebuild"))],
    ["apple_development_team", Boolean(environment.APPLE_DEVELOPMENT_TEAM)],
    ["app_store_connect_key_id", Boolean(environment.APP_STORE_CONNECT_KEY_ID)],
    ["app_store_connect_issuer_id", Boolean(environment.APP_STORE_CONNECT_ISSUER_ID)],
    ["app_store_connect_key_file", Boolean(environment.APP_STORE_CONNECT_KEY_FILE
      && fs.existsSync(environment.APP_STORE_CONNECT_KEY_FILE)
      && fs.lstatSync(environment.APP_STORE_CONNECT_KEY_FILE).isFile())],
  ];
  const missing = required.filter(([, available]) => !available).map(([name]) => name);
  const receipt = Object.freeze({
    schema_version: "mobile.bootstrap.v1",
    state: missing.length ? "setup_required" : "ready_to_build",
    product_id: product.product_id,
    opportunity_id: opportunity.opportunity_id,
    workspace_rel: materialized.workspace_rel,
    source_sha256: sourceTreeHash(sourceRoot),
    input_fingerprint: sha256(stableJson(opportunity)),
    missing,
    build: {
      product_id: product.product_id,
      commands: [
        ["xcodegen", "generate", "--spec", "project.yml"],
        ["xcodebuild", "test", "-project", `${opportunity.product_symbol}.xcodeproj`,
          "-scheme", opportunity.product_symbol,
          "-destination", "platform=iOS Simulator,name=iPhone 16"],
      ],
    },
    external_effects: [],
  });
  let receiptWrite = null;
  const receiptFile = path.join(path.dirname(sourceRoot), "bootstrap-receipt.json");
  try {
    receiptWrite = writeReceipt(receiptFile, receipt);
    if (!existing) {
      requireValue(!potentialPathWithinDirectory(sourceRoot, options.registryFile),
        "mobile product registry must not overlap generated source");
      const registered = registerMobileProduct(options.registryFile, rawProduct);
      requireValue(stableJson(registered) === stableJson(product), "registered mobile product missing");
    }
    return receiptWrite.value;
  } catch (error) {
    if (!existing && receiptWrite) {
      try {
        const committed = readMobileProducts(options.registryFile)
          .find((registeredProduct) => registeredProduct.product_id === opportunity.product_id);
        const retainedSource = fs.lstatSync(sourceRoot);
        const retainedReceiptStat = fs.lstatSync(receiptFile);
        const retainedReceipt = JSON.parse(fs.readFileSync(receiptFile, "utf8"));
        if (committed && stableJson(committed) === stableJson(product)
            && retainedSource.isDirectory() && !retainedSource.isSymbolicLink()
            && retainedSource.dev === observedSourceStat.dev && retainedSource.ino === observedSourceStat.ino
            && sourceTreeHash(sourceRoot) === observedSourceHash
            && observedSourceHash === receipt.source_sha256
            && retainedReceiptStat.isFile() && !retainedReceiptStat.isSymbolicLink()
            && retainedReceiptStat.dev === receiptWrite.dev && retainedReceiptStat.ino === receiptWrite.ino
            && stableJson(retainedReceipt) === stableJson(receipt)) {
          return receiptWrite.value;
        }
      } catch { /* registration is not durably complete; retain prepared files for replay */ }
    }
    throw error;
  }
}

module.exports = { bootstrapGeneratedMobileProduct, validateOpportunity };
