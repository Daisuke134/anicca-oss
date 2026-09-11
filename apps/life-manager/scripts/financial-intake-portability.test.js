"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "../../..");
const scripts = [
  "skills/sbi-usdc-monitor/scripts/run.sh",
  "skills/stripe-revenue-listener/scripts/listen.sh",
  "skills/stripe-revenue-poller/scripts/poll.sh",
];

test("registered financial intake uses Life Manager state, CFO and Telegram only", () => {
  for (const relative of scripts) {
    const source = fs.readFileSync(path.join(root, relative), "utf8");
    assert.match(source, /\.local\/state\/life-manager/);
    assert.match(source, /skills\/cfo\/run\.sh/);
    assert.match(source, /skills\/_shared\/send-telegram\.sh/);
    assert.doesNotMatch(source, /openclaw|hermes|profitable-claude|\/Users\/|slack\.com/iu);
  }
});
