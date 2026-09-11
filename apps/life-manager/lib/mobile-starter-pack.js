"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SYMBOL = /^[A-Za-z][A-Za-z0-9]{0,63}$/u;
const BUNDLE_ID = /^[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+){2,}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function safeRelative(value, field) {
  requireValue(typeof value === "string" && value.length > 0, `${field} required`);
  requireValue(!path.isAbsolute(value) && !path.win32.isAbsolute(value)
    && !value.split(/[\\/]/u).includes(".."), `${field} must be portable`);
  return value;
}

function digest(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function render(source, identity) {
  const values = {
    PRODUCT_NAME: identity.productSymbol,
    PRODUCT_SYMBOL: identity.productSymbol,
    DISPLAY_NAME: identity.displayName,
    BUNDLE_ID: identity.bundleId,
  };
  let output = source;
  for (const [key, value] of Object.entries(values)) output = output.replaceAll(`{{${key}}}`, value);
  requireValue(!/\{\{[A-Z_]+\}\}/u.test(output), "starter placeholder unresolved");
  return output;
}

function expectedFiles(packRoot, product, identity) {
  const manifestFile = path.join(packRoot, "manifest.json");
  requireValue(fs.lstatSync(manifestFile).isFile(), "starter manifest must be a regular file");
  const manifest = JSON.parse(fs.readFileSync(manifestFile, "utf8"));
  requireValue(manifest?.schema_version === "mobile.starter-pack.v1"
    && manifest.template_id === product.source.template_id && Array.isArray(manifest.files), "starter manifest invalid");
  return manifest.files.map((item) => {
    const relative = safeRelative(item.path, "starter file path");
    requireValue(SHA256.test(String(item.sha256 || "")), "starter file hash invalid");
    const source = path.join(packRoot, relative);
    requireValue(fs.lstatSync(source).isFile(), "starter input must be a regular file");
    requireValue(digest(source) === item.sha256, `starter hash mismatch: ${relative}`);
    return { relative, content: render(fs.readFileSync(source, "utf8"), identity) };
  });
}

function sameOutput(target, files) {
  if (!fs.existsSync(target)) return false;
  requireValue(!fs.lstatSync(target).isSymbolicLink(), "conflicting workspace");
  const actual = [];
  const visit = (directory, prefix = "") => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const relative = path.posix.join(prefix, entry.name);
      requireValue(!entry.isSymbolicLink(), "conflicting workspace");
      if (entry.isDirectory()) visit(path.join(directory, entry.name), relative);
      else if (entry.isFile()) actual.push(relative);
      else throw new Error("conflicting workspace");
    }
  };
  visit(target);
  const expected = files.map((item) => item.relative).sort();
  if (JSON.stringify(actual.sort()) !== JSON.stringify(expected)) return false;
  return files.every((item) => fs.readFileSync(path.join(target, item.relative), "utf8") === item.content);
}

function ensureManagedDirectory(root, relativeParent) {
  requireValue(fs.existsSync(root), "private root must exist");
  const rootStat = fs.lstatSync(root);
  requireValue(rootStat.isDirectory() && !rootStat.isSymbolicLink(), "private root must be a regular directory");
  let current = root;
  for (const segment of relativeParent.split("/")) {
    current = path.join(current, segment);
    if (!fs.existsSync(current)) fs.mkdirSync(current, { mode: 0o700 });
    const stat = fs.lstatSync(current);
    requireValue(stat.isDirectory() && !stat.isSymbolicLink(), "workspace directory must not be a symlink");
    requireValue(fs.realpathSync(current).startsWith(`${root}${path.sep}`), "workspace directory escapes private root");
  }
  return current;
}

function materializeMobileStarterPack({ product, identity, packRoot, privateRoot }) {
  requireValue(product?.schema_version === "mobile.product.v1" && product.origin === "generated",
    "starter pack requires a generated product");
  requireValue(product.source?.template_id, "template_id required");
  requireValue(SYMBOL.test(String(identity?.productSymbol || "")), "productSymbol invalid");
  requireValue(typeof identity.displayName === "string" && identity.displayName.trim().length > 0,
    "displayName invalid");
  requireValue(BUNDLE_ID.test(String(identity.bundleId || "")), "bundleId invalid");
  const workspace = safeRelative(product.workspace_rel, "workspace");
  requireValue(workspace === `mobile-products/${product.product_id}`, "workspace does not match product");
  const files = expectedFiles(path.resolve(packRoot), product, identity);
  const requestedRoot = path.resolve(privateRoot);
  requireValue(fs.existsSync(requestedRoot) && !fs.lstatSync(requestedRoot).isSymbolicLink(),
    "private root must be a regular directory");
  const root = fs.realpathSync(requestedRoot);
  const target = path.resolve(root, workspace, "source");
  requireValue(target.startsWith(`${root}${path.sep}`), "workspace escapes private root");
  if (fs.existsSync(target)) {
    requireValue(sameOutput(target, files), "conflicting workspace");
    return Object.freeze({ state: "replayed", product_id: product.product_id,
      template_id: product.source.template_id, workspace_rel: `${workspace}/source` });
  }
  const parent = ensureManagedDirectory(root, workspace);
  const stage = `${target}.stage-${process.pid}`;
  requireValue(!fs.existsSync(stage), "starter stage already exists");
  try {
    fs.mkdirSync(stage, { mode: 0o700 });
    for (const item of files) {
      const output = path.join(stage, item.relative);
      fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
      fs.writeFileSync(output, item.content, { mode: 0o600 });
    }
    fs.renameSync(stage, target);
  } finally {
    if (fs.existsSync(stage)) fs.rmSync(stage, { recursive: true, force: true });
  }
  return Object.freeze({ state: "materialized", product_id: product.product_id,
    template_id: product.source.template_id, workspace_rel: `${workspace}/source` });
}

module.exports = { materializeMobileStarterPack };
