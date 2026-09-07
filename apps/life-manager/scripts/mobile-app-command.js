"use strict";

const fs = require("node:fs");
const path = require("node:path");

const VALUE = /^[a-z0-9][a-z0-9._-]{0,127}$/;

function resolveMobileAppLoop(loopId, manifestFile = path.resolve(__dirname, "../config/mobile-app-loops.json")) {
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
  return Object.freeze({ productId: item.product_id, runner, action: item.action });
}

if (require.main === module) {
  try {
    const item = resolveMobileAppLoop(process.argv[2]);
    process.stdout.write(`${item.runner}\t${item.action}\t${item.productId}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}

module.exports = { resolveMobileAppLoop };
