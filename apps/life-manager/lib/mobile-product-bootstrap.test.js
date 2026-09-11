"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const { bootstrapGeneratedMobileProduct } = require("./mobile-product-bootstrap.js");
const { readMobileProducts } = require("./mobile-product-registry.js");

const packRoot = path.resolve(__dirname, "../mobile-starter-packs/ios-swiftui-v1");
const opportunity = {
  opportunity_id: "sleep-reset-2026",
  product_id: "sleep-reset",
  display_name: "Sleep Reset",
  product_symbol: "SleepReset",
  bundle_id: "app.example.sleepreset",
  audience: "people rebuilding a consistent sleep schedule",
  problem: "sleep routines are difficult to sustain",
  evidence_refs: ["https://example.com/research/sleep-reset"],
};

function sandbox(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lm-mobile-bootstrap-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, registryFile: path.join(root, "state", "products.json") };
}

test("a clean user bootstraps a generated app without an existing repository", (t) => {
  const { root, registryFile } = sandbox(t);
  const receipt = bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });

  assert.equal(receipt.state, "setup_required");
  assert.deepEqual(receipt.missing, [
    "xcodegen", "xcodebuild", "apple_development_team",
    "app_store_connect_key_id", "app_store_connect_issuer_id", "app_store_connect_key_file",
  ]);
  assert.equal(receipt.workspace_rel, "mobile-products/sleep-reset/source");
  assert.equal(fs.existsSync(path.join(root, receipt.workspace_rel, "project.yml")), true);
  assert.deepEqual(readMobileProducts(registryFile).map((item) => item.product_id), ["sleep-reset"]);
  assert.doesNotMatch(JSON.stringify(receipt), /\/Users\/|openclaw|hermes|secret|private.key/iu);
  assert.deepEqual(bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), receipt);
});

test("build preparation consumes the registered product and emits portable commands", (t) => {
  const { root, registryFile } = sandbox(t);
  const keyFile = path.join(root, "AuthKey.p8");
  fs.writeFileSync(keyFile, "private test fixture");
  const receipt = bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {
      APPLE_DEVELOPMENT_TEAM: "TEAM123",
      APP_STORE_CONNECT_KEY_ID: "KEY123",
      APP_STORE_CONNECT_ISSUER_ID: "issuer-123",
      APP_STORE_CONNECT_KEY_FILE: keyFile,
    },
    findExecutable: (name) => `/usr/bin/${name}`,
  });
  assert.equal(receipt.state, "ready_to_build");
  assert.deepEqual(receipt.missing, []);
  assert.equal(receipt.build.product_id, "sleep-reset");
  assert.deepEqual(receipt.build.commands, [
    ["xcodegen", "generate", "--spec", "project.yml"],
    ["xcodebuild", "test", "-project", "SleepReset.xcodeproj", "-scheme", "SleepReset",
      "-destination", "platform=iOS Simulator,name=iPhone 16"],
  ]);
  assert.doesNotMatch(JSON.stringify(receipt), new RegExp(root.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"), "u"));
});

test("invalid or conflicting opportunity evidence fails before replacing output", (t) => {
  const { root, registryFile } = sandbox(t);
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root,
    opportunity: { ...opportunity, evidence_refs: [] }, packRoot,
  }), /evidence_refs/);
  bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root,
    opportunity: { ...opportunity, problem: "different problem" }, packRoot,
    environment: {}, findExecutable: () => null,
  }), /conflicting mobile bootstrap receipt/);
  assert.equal(fs.existsSync(path.join(root, "mobile-products/sleep-reset/source/project.yml")), true);
});

test("downstream-invalid identity fails before registry mutation", (t) => {
  for (const changed of [
    { product_id: "probe_app" },
    { product_id: 123 },
    { display_name: "bad\u0001name" },
    { display_name: "bad\tname" },
    { display_name: "bad\nname" },
    { display_name: "bad\rname" },
    { opportunity_id: 123 },
  ]) {
    const { root, registryFile } = sandbox(t);
    assert.throws(() => bootstrapGeneratedMobileProduct({
      registryFile, privateRoot: root, opportunity: { ...opportunity, ...changed }, packRoot,
    }), /invalid|control characters/);
    assert.equal(fs.existsSync(registryFile), false);
  }
});

test("materialization failure leaves no partial registry", (t) => {
  const { root, registryFile } = sandbox(t);
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity,
    packRoot: path.join(root, "missing-pack"),
  }));
  assert.equal(fs.existsSync(registryFile), false);
  assert.equal(fs.existsSync(path.join(root, "mobile-products/sleep-reset")), false);
});

test("tampered receipt fails closed but a valid capability transition may update", (t) => {
  const { root, registryFile } = sandbox(t);
  const options = { registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null };
  const first = bootstrapGeneratedMobileProduct(options);
  const receiptFile = path.join(root, "mobile-products/sleep-reset/bootstrap-receipt.json");
  fs.writeFileSync(receiptFile, JSON.stringify({ ...first, state: "tampered", external_effects: ["unexpected"] }));
  assert.throws(() => bootstrapGeneratedMobileProduct(options), /conflicting mobile bootstrap receipt/);

  fs.writeFileSync(receiptFile, JSON.stringify(first));
  const keyFile = path.join(root, "AuthKey.p8");
  fs.writeFileSync(keyFile, "fixture");
  const ready = bootstrapGeneratedMobileProduct({
    ...options,
    environment: {
      APPLE_DEVELOPMENT_TEAM: "TEAM123", APP_STORE_CONNECT_KEY_ID: "KEY123",
      APP_STORE_CONNECT_ISSUER_ID: "issuer-123", APP_STORE_CONNECT_KEY_FILE: keyFile,
    },
    findExecutable: (name) => `/usr/bin/${name}`,
  });
  assert.equal(ready.state, "ready_to_build");
  assert.deepEqual(ready.missing, []);
  assert.throws(() => bootstrapGeneratedMobileProduct(options), /capability regression/);
});

test("preplaced receipt temporary symlink cannot overwrite its target", (t) => {
  const { root, registryFile } = sandbox(t);
  const productRoot = path.join(root, "mobile-products/sleep-reset");
  fs.mkdirSync(productRoot, { recursive: true });
  const marker = path.join(productRoot, "owner.txt");
  fs.writeFileSync(marker, "keep\n");
  const victim = path.join(root, "victim.txt");
  fs.writeFileSync(victim, "safe\n");
  const original = crypto.randomUUID;
  crypto.randomUUID = () => "fixed";
  t.after(() => { crypto.randomUUID = original; });
  fs.symlinkSync(victim, path.join(productRoot, "bootstrap-receipt.json.tmp-fixed"));
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /EEXIST/);
  assert.equal(fs.readFileSync(victim, "utf8"), "safe\n");
  assert.equal(fs.readFileSync(marker, "utf8"), "keep\n");
  assert.equal(fs.lstatSync(path.join(productRoot, "bootstrap-receipt.json.tmp-fixed")).isSymbolicLink(), true);
  assert.equal(fs.existsSync(registryFile), false);
  assert.equal(fs.existsSync(path.join(productRoot, "source/project.yml")), true);
});

test("a conflicting pre-existing receipt survives failed bootstrap", (t) => {
  const { root, registryFile } = sandbox(t);
  const productRoot = path.join(root, "mobile-products/sleep-reset");
  fs.mkdirSync(productRoot, { recursive: true });
  const firstRoot = sandbox(t);
  const valid = bootstrapGeneratedMobileProduct({
    registryFile: firstRoot.registryFile, privateRoot: firstRoot.root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  const conflicting = { ...valid, state: "tampered", external_effects: ["keep"] };
  const receiptFile = path.join(productRoot, "bootstrap-receipt.json");
  fs.writeFileSync(receiptFile, JSON.stringify(conflicting));
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /conflicting mobile bootstrap receipt/);
  assert.deepEqual(JSON.parse(fs.readFileSync(receiptFile, "utf8")), conflicting);
  assert.equal(fs.existsSync(registryFile), false);
  assert.equal(fs.existsSync(path.join(productRoot, "source/project.yml")), true);
});

test("registry path may not overlap generated source", (t) => {
  const { root } = sandbox(t);
  const registryFile = path.join(root, "mobile-products/sleep-reset/source/products.json");
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /must not overlap generated source/);
  assert.equal(fs.existsSync(path.join(root, "mobile-products/sleep-reset")), false);
});

test("registry parent symlink cannot alias into generated source", (t) => {
  const { root } = sandbox(t);
  const sourceRoot = path.join(root, "mobile-products/sleep-reset/source");
  fs.mkdirSync(sourceRoot, { recursive: true });
  const alias = path.join(root, "registry-alias");
  fs.symlinkSync(sourceRoot, alias, "dir");
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile: path.join(alias, "products.json"), privateRoot: root,
    opportunity, packRoot, environment: {}, findExecutable: () => null,
  }), /must not overlap generated source/);
  assert.equal(fs.existsSync(path.join(sourceRoot, "products.json")), false);
});

test("case-insensitive registry alias is rechecked after source creation", (t) => {
  const { root } = sandbox(t);
  const registryFile = path.join(root, "MOBILE-PRODUCTS/sleep-reset/source/products.json");
  let caseInsensitive = false;
  const lower = path.join(root, "case-probe");
  fs.mkdirSync(lower);
  caseInsensitive = fs.existsSync(path.join(root, "CASE-PROBE"));
  if (!caseInsensitive) return;
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /must not overlap generated source/);
  assert.equal(fs.existsSync(registryFile), false);
  assert.equal(fs.existsSync(path.join(root, "mobile-products/sleep-reset/source/project.yml")), true);
});

test("registry alias into a generated source child is physically rechecked", (t) => {
  const { root } = sandbox(t);
  const alias = path.join(root, "registry-child-alias");
  const generatedChild = path.join(root, "mobile-products/sleep-reset/source/Sources");
  fs.symlinkSync(generatedChild, alias, "dir");
  const registryFile = path.join(alias, "products.json");
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /must not overlap generated source/);
  assert.equal(fs.existsSync(registryFile), false);
  assert.equal(fs.existsSync(path.join(root, "mobile-products/sleep-reset/source/project.yml")), true);
});

test("cleanup preserves source replaced before registry failure", (t) => {
  const { root, registryFile } = sandbox(t);
  fs.mkdirSync(path.dirname(registryFile), { recursive: true });
  fs.writeFileSync(`${registryFile}.lock`, "other writer\n");
  const sourceRoot = path.join(root, "mobile-products/sleep-reset/source");
  const originalOpen = fs.openSync;
  let replaced = false;
  fs.openSync = function guardedOpen(file, ...args) {
    if (!replaced && file === `${registryFile}.lock`) {
      fs.rmSync(sourceRoot, { recursive: true, force: true });
      fs.mkdirSync(sourceRoot, { recursive: true });
      fs.writeFileSync(path.join(sourceRoot, "owner.txt"), "keep\n");
      replaced = true;
    }
    return originalOpen.call(this, file, ...args);
  };
  t.after(() => { fs.openSync = originalOpen; });
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /registry is busy/);
  assert.equal(fs.readFileSync(path.join(sourceRoot, "owner.txt"), "utf8"), "keep\n");
  assert.equal(fs.existsSync(registryFile), false);
});

test("registry failure retains a replayable receipt beside existing source", (t) => {
  const { root, registryFile } = sandbox(t);
  const firstRegistry = path.join(root, "first-products.json");
  bootstrapGeneratedMobileProduct({
    registryFile: firstRegistry, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  const productRoot = path.join(root, "mobile-products/sleep-reset");
  fs.rmSync(firstRegistry);
  fs.rmSync(path.join(productRoot, "bootstrap-receipt.json"));
  fs.mkdirSync(path.dirname(registryFile), { recursive: true });
  fs.writeFileSync(`${registryFile}.lock`, "other writer\n");
  assert.throws(() => bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  }), /registry is busy/);
  assert.equal(fs.existsSync(path.join(productRoot, "source/project.yml")), true);
  assert.equal(fs.existsSync(path.join(productRoot, "bootstrap-receipt.json")), true);
  assert.equal(fs.existsSync(registryFile), false);
  fs.rmSync(`${registryFile}.lock`);
  const replay = bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  assert.equal(replay.state, "setup_required");
  assert.deepEqual(readMobileProducts(registryFile).map((item) => item.product_id), ["sleep-reset"]);
});

test("post-commit registry cleanup error preserves the complete committed bootstrap", (t) => {
  const { root, registryFile } = sandbox(t);
  const originalChmod = fs.chmodSync;
  let injected = false;
  fs.chmodSync = function failAfterRegistryRename(file, ...args) {
    if (!injected && file === registryFile) {
      injected = true;
      throw new Error("injected post-commit chmod failure");
    }
    return originalChmod.call(this, file, ...args);
  };
  t.after(() => { fs.chmodSync = originalChmod; });
  const receipt = bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  assert.equal(receipt.state, "setup_required");
  assert.deepEqual(readMobileProducts(registryFile).map((item) => item.product_id), ["sleep-reset"]);
  assert.equal(fs.existsSync(path.join(root, receipt.workspace_rel, "project.yml")), true);
  assert.equal(fs.existsSync(path.join(root, "mobile-products/sleep-reset/bootstrap-receipt.json")), true);
});

test("post-commit registry error succeeds with replayed source and receipt", (t) => {
  const { root, registryFile } = sandbox(t);
  const firstRegistry = path.join(root, "first-products.json");
  bootstrapGeneratedMobileProduct({
    registryFile: firstRegistry, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  fs.rmSync(firstRegistry);
  const originalChmod = fs.chmodSync;
  let injected = false;
  fs.chmodSync = function failAfterRegistryRename(file, ...args) {
    if (!injected && file === registryFile) {
      injected = true;
      throw new Error("injected replay post-commit failure");
    }
    return originalChmod.call(this, file, ...args);
  };
  t.after(() => { fs.chmodSync = originalChmod; });
  const receipt = bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {}, findExecutable: () => null,
  });
  assert.equal(receipt.state, "setup_required");
  assert.deepEqual(readMobileProducts(registryFile).map((item) => item.product_id), ["sleep-reset"]);
});

test("the finite CLI starts from one opportunity file and private XDG roots", (t) => {
  const { root } = sandbox(t);
  const input = path.join(root, "opportunity.json");
  fs.writeFileSync(input, JSON.stringify(opportunity));
  const completed = spawnSync(process.execPath, [
    path.resolve(__dirname, "../scripts/mobile-product-bootstrap.js"),
    "--opportunity-file", input,
  ], {
    encoding: "utf8",
    env: { ...process.env, PATH: "", HOME: root,
      XDG_STATE_HOME: path.join(root, "state"), XDG_DATA_HOME: path.join(root, "data") },
  });
  assert.equal(completed.status, 0, completed.stderr);
  const receipt = JSON.parse(completed.stdout);
  assert.equal(receipt.state, "setup_required");
  assert.equal(fs.existsSync(path.join(root, "data/life-manager", receipt.workspace_rel, "project.yml")), true);
  assert.equal(fs.existsSync(path.join(root, "state/life-manager/mobile-products.json")), true);
});

test("tool discovery accepts an executable symlink like a Homebrew command", (t) => {
  const { root, registryFile } = sandbox(t);
  const bin = path.join(root, "bin");
  fs.mkdirSync(bin);
  const executable = path.join(root, "tool");
  fs.writeFileSync(executable, "#!/bin/sh\nexit 0\n", { mode: 0o700 });
  for (const name of ["xcodegen", "xcodebuild"]) fs.symlinkSync(executable, path.join(bin, name));
  const keyFile = path.join(root, "AuthKey.p8");
  fs.writeFileSync(keyFile, "fixture");
  const receipt = bootstrapGeneratedMobileProduct({
    registryFile, privateRoot: root, opportunity, packRoot,
    environment: {
      PATH: bin,
      APPLE_DEVELOPMENT_TEAM: "TEAM123",
      APP_STORE_CONNECT_KEY_ID: "KEY123",
      APP_STORE_CONNECT_ISSUER_ID: "issuer-123",
      APP_STORE_CONNECT_KEY_FILE: keyFile,
    },
  });
  assert.equal(receipt.state, "ready_to_build");
  assert.deepEqual(receipt.missing, []);
});
