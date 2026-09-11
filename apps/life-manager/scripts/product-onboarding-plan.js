#!/usr/bin/env node
"use strict";

const { planProductOnboarding } = require("../lib/product-onboarding.js");
const { hasTelegramCredentials } = require("../lib/telegram-credentials.js");

function parse(argv) {
  let host = "";
  let envFile = "";
  const selected = [];
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--host" && value) host = value;
    else if (key === "--loop" && value) selected.push(value);
    else if (key === "--env-file" && value) envFile = value;
    else throw new Error("usage: product-onboarding-plan --host local|cloud --loop ID [--loop ID] [--env-file FILE]");
    index += 1;
  }
  const verified = host === "local" && hasTelegramCredentials(envFile)
    ? ["telegram_credentials"] : [];
  return { host, selected_loop_ids: selected, verified_requirements: verified };
}

function main() {
  process.stdout.write(`${JSON.stringify(planProductOnboarding(parse(process.argv.slice(2))), null, 2)}\n`);
}

if (require.main === module) main();

module.exports = { parse };
