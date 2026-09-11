#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { bootstrapGeneratedMobileProduct } = require("../lib/mobile-product-bootstrap.js");

function parse(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!["--opportunity-file", "--registry-file", "--private-root"].includes(key) || !value) {
      throw new Error("usage: mobile-product-bootstrap --opportunity-file FILE [--registry-file FILE] [--private-root DIR]");
    }
    values[key.slice(2)] = value;
  }
  if (!values["opportunity-file"]) throw new Error("--opportunity-file required");
  return values;
}

function main() {
  const args = parse(process.argv.slice(2));
  const stateHome = process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
  const dataHome = process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  const privateRoot = path.resolve(args["private-root"] || path.join(dataHome, "life-manager"));
  fs.mkdirSync(privateRoot, { recursive: true, mode: 0o700 });
  const receipt = bootstrapGeneratedMobileProduct({
    opportunity: JSON.parse(fs.readFileSync(path.resolve(args["opportunity-file"]), "utf8")),
    registryFile: path.resolve(args["registry-file"] || path.join(stateHome, "life-manager", "mobile-products.json")),
    privateRoot,
    packRoot: path.resolve(__dirname, "../mobile-starter-packs/ios-swiftui-v1"),
  });
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
}

if (require.main === module) main();

module.exports = { parse };
