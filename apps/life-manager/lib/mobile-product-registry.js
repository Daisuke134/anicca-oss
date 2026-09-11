"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ID = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const PINNED_REVISION = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/u;

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function portableRelative(value, field) {
  requireValue(typeof value === "string" && value.length > 0, `${field} required`);
  requireValue(!path.isAbsolute(value) && !path.win32.isAbsolute(value)
    && !value.split(/[\\/]/u).includes(".."), `${field} must be portable`);
  return value;
}

function normalizeSource(origin, raw) {
  requireValue(raw && typeof raw === "object" && !Array.isArray(raw), "source required");
  if (origin === "generated") {
    requireValue(ID.test(String(raw.template_id || "")), "template_id invalid");
    requireValue(ID.test(String(raw.repository_name || "")), "repository_name invalid");
    return Object.freeze({
      template_id: raw.template_id,
      repository_name: raw.repository_name,
    });
  }
  requireValue(origin === "imported", "origin invalid");
  let remote;
  try {
    remote = new URL(raw.git_remote);
  } catch {
    throw new Error("git_remote must be an absolute HTTPS URL");
  }
  requireValue(remote.protocol === "https:", "git_remote must be an absolute HTTPS URL");
  requireValue(PINNED_REVISION.test(String(raw.revision || "")), "revision must be a full commit SHA");
  const source = { git_remote: remote.toString(), revision: raw.revision };
  if (raw.subdirectory !== undefined) source.subdirectory = portableRelative(raw.subdirectory, "subdirectory");
  return Object.freeze(source);
}

function normalizeProduct(raw) {
  requireValue(raw && typeof raw === "object" && !Array.isArray(raw), "mobile product required");
  requireValue(ID.test(String(raw.product_id || "")), "product_id invalid");
  const origin = raw.origin;
  return Object.freeze({
    schema_version: "mobile.product.v1",
    product_id: raw.product_id,
    origin,
    source: normalizeSource(origin, raw.source),
    workspace_rel: `mobile-products/${raw.product_id}`,
    lifecycle_state: "setup_required",
  });
}

function readDocument(registryFile) {
  if (!fs.existsSync(registryFile)) return { schema_version: 1, products: [] };
  const document = JSON.parse(fs.readFileSync(registryFile, "utf8"));
  requireValue(document?.schema_version === 1 && Array.isArray(document.products), "mobile product registry invalid");
  return document;
}

function readMobileProducts(registryFile) {
  return readDocument(registryFile).products.map(normalizeProduct)
    .sort((left, right) => left.product_id.localeCompare(right.product_id));
}

function registerMobileProduct(registryFile, raw) {
  const product = normalizeProduct(raw);
  const directory = path.dirname(registryFile);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
  const lockFile = `${registryFile}.lock`;
  let lock;
  try {
    lock = fs.openSync(lockFile, "wx", 0o600);
  } catch (error) {
    if (error?.code === "EEXIST") throw new Error("mobile product registry is busy");
    throw error;
  }
  try {
    const products = readMobileProducts(registryFile);
    const existing = products.find((item) => item.product_id === product.product_id);
    if (existing) {
      requireValue(JSON.stringify(existing) === JSON.stringify(product), "conflicting mobile product");
      return existing;
    }
    products.push(product);
    products.sort((left, right) => left.product_id.localeCompare(right.product_id));
    const temporary = `${registryFile}.tmp-${process.pid}`;
    fs.writeFileSync(temporary, `${JSON.stringify({ schema_version: 1, products }, null, 2)}\n`, { mode: 0o600 });
    fs.renameSync(temporary, registryFile);
    fs.chmodSync(registryFile, 0o600);
    return product;
  } finally {
    fs.closeSync(lock);
    fs.unlinkSync(lockFile);
  }
}

module.exports = { normalizeProduct, readMobileProducts, registerMobileProduct };
