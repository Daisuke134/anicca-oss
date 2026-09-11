"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { readMobileProducts } = require("../lib/mobile-product-registry.js");

const VALUE = /^[a-z0-9][a-z0-9._-]{0,127}$/;

function resolveMobileAppLoop(
  loopId,
  manifestFile = path.resolve(__dirname, "../config/mobile-app-loops.json"),
  productRegistryFile = path.resolve(__dirname, "../config/mobile-products.json"),
) {
  if (!VALUE.test(String(loopId || ""))) throw new Error("mobile app loop id invalid");
  const manifest = JSON.parse(fs.readFileSync(manifestFile, "utf8"));
  const item = manifest && manifest.schema_version === 1 && manifest.loops?.[loopId];
  if (!item || !VALUE.test(item.product_id) || !VALUE.test(item.runner)
      || !VALUE.test(item.action) || !item.runner.endsWith(".js")) {
    throw new Error("mobile app loop manifest entry invalid");
  }
  const runner = path.resolve(__dirname, item.runner);
  if (path.dirname(runner) !== __dirname || !fs.existsSync(runner)) {
    throw new Error("mobile app loop runner unavailable");
  }
  const matches = readMobileProducts(productRegistryFile)
    .filter((product) => product.product_id === item.product_id);
  if (matches.length !== 1) throw new Error("mobile app loop product is not registered");
  const product = matches[0];
  if (product.workspace_rel !== `mobile-products/${item.product_id}`) {
    throw new Error("mobile app loop product workspace differs");
  }
  return Object.freeze({
    productId: product.product_id,
    runner,
    action: item.action,
    origin: product.origin,
    workspaceRel: product.workspace_rel,
    source: product.source,
  });
}

if (require.main === module) {
  try {
    const item = resolveMobileAppLoop(process.argv[2]);
    process.stdout.write(
      `${item.runner}\t${item.action}\t${item.productId}\t${item.origin}\t${item.workspaceRel}\n`,
    );
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}

module.exports = { resolveMobileAppLoop };
