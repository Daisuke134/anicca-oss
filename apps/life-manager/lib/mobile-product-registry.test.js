"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  registerMobileProduct,
  readMobileProducts,
} = require("./mobile-product-registry.js");

function temporaryRegistry(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-mobile-products-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return path.join(root, "products.json");
}

test("generated and imported apps share one portable product registry", (t) => {
  const registryFile = temporaryRegistry(t);
  const generated = registerMobileProduct(registryFile, {
    product_id: "new-focus-app",
    origin: "generated",
    source: { template_id: "ios-swiftui-v1", repository_name: "new-focus-app" },
  });
  const imported = registerMobileProduct(registryFile, {
    product_id: "anicca-ios",
    origin: "imported",
    source: {
      git_remote: "https://github.com/Daisuke134/anicca-products.git",
      subdirectory: "aniccaios",
      revision: "a9ab8a17c7dee9af8c3f2ad752a902ce26e7d1d3",
      access: "public",
    },
  });

  assert.equal(generated.workspace_rel, "mobile-products/new-focus-app");
  assert.equal(imported.workspace_rel, "mobile-products/anicca-ios");
  assert.deepEqual(readMobileProducts(registryFile).map((item) => item.product_id), [
    "anicca-ios",
    "new-focus-app",
  ]);
  assert.doesNotMatch(fs.readFileSync(registryFile, "utf8"), /\/Users\/|openclaw|hermes/iu);
});

test("same registration is idempotent and a conflicting duplicate fails closed", (t) => {
  const registryFile = temporaryRegistry(t);
  const item = {
    product_id: "honne-ai",
    origin: "imported",
    source: {
      git_remote: "https://github.com/Daisuke134/honne-ai.git",
      revision: "b57928bb13ef1f9a1e774e4bca2467e3059c9eac",
      access: "private",
    },
  };
  const first = registerMobileProduct(registryFile, item);
  assert.deepEqual(registerMobileProduct(registryFile, item), first);
  assert.throws(() => registerMobileProduct(registryFile, {
    ...item,
    source: { ...item.source, revision: "c".repeat(40) },
  }), /conflicting mobile product/);
});

test("local paths and malformed source descriptors are rejected", (t) => {
  const registryFile = temporaryRegistry(t);
  const localSourceRoot = ["", "Users", "example", "private-mobile-source"].join("/");
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "bad-local",
    origin: "imported",
    source: { git_remote: localSourceRoot, revision: "a".repeat(40), access: "private" },
  }), /git_remote/);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "bad-windows-local",
    origin: "imported",
    source: { git_remote: "https://example.com/app.git", revision: "a".repeat(40), subdirectory: "C:\\Users\\owner\\app", access: "public" },
  }), /subdirectory/);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "mutable-ref",
    origin: "imported",
    source: { git_remote: "https://example.com/app.git", revision: "main", access: "public" },
  }), /revision/);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "credential-remote",
    origin: "imported",
    source: { git_remote: "https://token@example.com/app.git", revision: "a".repeat(40), access: "private" },
  }), /credentials/);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "missing-access",
    origin: "imported",
    source: { git_remote: "https://example.com/app.git", revision: "a".repeat(40) },
  }), /source access/);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "bad-generated",
    origin: "generated",
    source: { repository_name: "missing-template" },
  }), /template_id/);
});

test("an existing writer lock fails closed instead of losing an update", (t) => {
  const registryFile = temporaryRegistry(t);
  fs.writeFileSync(`${registryFile}.lock`, "other writer\n");
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "locked-product",
    origin: "generated",
    source: { template_id: "ios-swiftui-v1", repository_name: "locked-product" },
  }), /registry is busy/);
  assert.equal(fs.existsSync(registryFile), false);
});
