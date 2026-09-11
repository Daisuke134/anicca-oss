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
      revision: "release/1.9.5",
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
    product_id: "honne",
    origin: "imported",
    source: {
      git_remote: "https://github.com/Daisuke134/honne-ai.git",
      revision: "main",
    },
  };
  const first = registerMobileProduct(registryFile, item);
  assert.deepEqual(registerMobileProduct(registryFile, item), first);
  assert.throws(() => registerMobileProduct(registryFile, {
    ...item,
    source: { ...item.source, revision: "other" },
  }), /conflicting mobile product/);
});

test("local paths and malformed source descriptors are rejected", (t) => {
  const registryFile = temporaryRegistry(t);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "bad-local",
    origin: "imported",
    source: { git_remote: "/Users/anicca/anicca-project", revision: "main" },
  }), /git_remote/);
  assert.throws(() => registerMobileProduct(registryFile, {
    product_id: "bad-generated",
    origin: "generated",
    source: { repository_name: "missing-template" },
  }), /template_id/);
});
