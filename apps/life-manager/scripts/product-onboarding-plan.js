#!/usr/bin/env node
"use strict";

const { planProductOnboarding } = require("../lib/product-onboarding.js");

function parse(argv) {
  let host = "";
  const selected = [];
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--host" && value) host = value;
    else if (key === "--loop" && value) selected.push(value);
    else throw new Error("usage: product-onboarding-plan --host local|cloud --loop ID [--loop ID]");
    index += 1;
  }
  return { host, selected_loop_ids: selected };
}

function main() {
  process.stdout.write(`${JSON.stringify(planProductOnboarding(parse(process.argv.slice(2))), null, 2)}\n`);
}

if (require.main === module) main();

module.exports = { parse };
