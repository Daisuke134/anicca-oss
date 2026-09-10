#!/usr/bin/env node
"use strict";

const { readInvestmentCloudWiring } = require("../lib/investment-dry-run.js");

readInvestmentCloudWiring().then((result) => {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}).catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
