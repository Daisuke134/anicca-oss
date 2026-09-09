#!/usr/bin/env node
"use strict";

const path = require("node:path");
const { runDailyPass } = require("../lib/daily-dev-loop");

runDailyPass({
  command: process.env.LM_DEV_D0_COMMAND
    || path.join(__dirname, "life-manager-dev-d0.sh"),
  timeoutMs: Number(process.env.LM_DEV_DAILY_TIMEOUT_MS) || 25 * 60 * 1000,
}).then((result) => {
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exitCode = result.outcome === "failed" || result.outcome === "timed_out" ? 1 : 0;
}).catch(() => {
  process.stderr.write("life-manager-dev-daily: bounded runner failed\n");
  process.exitCode = 1;
});
