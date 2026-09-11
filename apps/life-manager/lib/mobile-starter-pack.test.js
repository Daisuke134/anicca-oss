"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { materializeMobileStarterPack } = require("./mobile-starter-pack.js");

const packRoot = path.resolve(__dirname, "../mobile-starter-packs/ios-swiftui-v1");
const generated = {
  schema_version: "mobile.product.v1",
  product_id: "focus-bloom",
  origin: "generated",
  source: { template_id: "ios-swiftui-v1", repository_name: "focus-bloom" },
  workspace_rel: "mobile-products/focus-bloom",
  lifecycle_state: "setup_required",
};
const identity = { productSymbol: "FocusBloom", displayName: "Focus Bloom", bundleId: "app.example.focusbloom" };

function privateRoot(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-mobile-starter-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test("materializes a verified starter into the managed product workspace", (t) => {
  const root = privateRoot(t);
  const result = materializeMobileStarterPack({ product: generated, identity, packRoot, privateRoot: root });
  assert.equal(result.state, "materialized");
  assert.equal(result.workspace_rel, "mobile-products/focus-bloom/source");
  const target = path.join(root, result.workspace_rel);
  assert.equal(fs.existsSync(path.join(target, "project.yml")), true);
  const rendered = fs.readFileSync(path.join(target, "Sources/App.swift"), "utf8");
  assert.match(rendered, /struct FocusBloomApp/);
  assert.doesNotMatch(rendered, /\{\{/);
  assert.doesNotMatch(JSON.stringify(result), /\/Users\/|credential|signing/iu);
  assert.equal(materializeMobileStarterPack({ product: generated, identity, packRoot, privateRoot: root }).state, "replayed");
});

test("escapes a product display name as Swift string content", (t) => {
  const root = privateRoot(t);
  const result = materializeMobileStarterPack({
    product: generated,
    identity: { ...identity, displayName: "Focus \"Bloom\"\nNow" },
    packRoot,
    privateRoot: root,
  });
  const rendered = fs.readFileSync(path.join(root, result.workspace_rel, "Sources/ContentView.swift"), "utf8");
  assert.match(rendered, /Text\("Focus \\\"Bloom\\\"\\nNow"\)/);
});

test("rejects imported products, unsafe workspaces and mutable template input", (t) => {
  const root = privateRoot(t);
  assert.throws(() => materializeMobileStarterPack({
    product: { ...generated, origin: "imported" }, identity, packRoot, privateRoot: root,
  }), /generated product/);
  assert.throws(() => materializeMobileStarterPack({
    product: { ...generated, workspace_rel: "../escape" }, identity, packRoot, privateRoot: root,
  }), /workspace/);

  const copiedPack = path.join(root, "pack");
  fs.cpSync(packRoot, copiedPack, { recursive: true });
  fs.appendFileSync(path.join(copiedPack, "Sources/App.swift"), "// changed\n");
  assert.throws(() => materializeMobileStarterPack({
    product: generated, identity, packRoot: copiedPack, privateRoot: path.join(root, "private"),
  }), /hash mismatch/);

  const noncanonicalPack = path.join(root, "noncanonical-pack");
  fs.cpSync(packRoot, noncanonicalPack, { recursive: true });
  const manifestFile = path.join(noncanonicalPack, "manifest.json");
  const manifest = JSON.parse(fs.readFileSync(manifestFile, "utf8"));
  manifest.files[0].path = `./${manifest.files[0].path}`;
  fs.writeFileSync(manifestFile, JSON.stringify(manifest));
  assert.throws(() => materializeMobileStarterPack({
    product: generated, identity, packRoot: noncanonicalPack, privateRoot: path.join(root, "canonical-private"),
  }), /must be canonical/);
});

test("rejects symlinks and conflicting existing output without replacing it", (t) => {
  const root = privateRoot(t);
  const copiedPack = path.join(root, "pack");
  fs.cpSync(packRoot, copiedPack, { recursive: true });
  fs.rmSync(path.join(copiedPack, "Sources/App.swift"));
  fs.symlinkSync(path.join(packRoot, "Sources/App.swift"), path.join(copiedPack, "Sources/App.swift"));
  assert.throws(() => materializeMobileStarterPack({
    product: generated, identity, packRoot: copiedPack, privateRoot: path.join(root, "private"),
  }), /regular file/);

  const target = path.join(root, generated.workspace_rel, "source");
  fs.mkdirSync(target, { recursive: true });
  fs.writeFileSync(path.join(target, "owner.txt"), "keep\n");
  assert.throws(() => materializeMobileStarterPack({ product: generated, identity, packRoot, privateRoot: root }), /conflicting workspace/);
  assert.equal(fs.readFileSync(path.join(target, "owner.txt"), "utf8"), "keep\n");
});

test("rejects a symlink in the managed workspace parent chain", (t) => {
  const root = privateRoot(t);
  const outside = privateRoot(t);
  fs.symlinkSync(outside, path.join(root, "mobile-products"));
  assert.throws(() => materializeMobileStarterPack({ product: generated, identity, packRoot, privateRoot: root }),
    /workspace directory must not be a symlink/);
  assert.equal(fs.existsSync(path.join(outside, generated.product_id, "source", "project.yml")), false);
});
