"use strict";

function usdMicrosFromDecimal(value) {
  const raw = String(value == null ? "" : value).trim();
  if (/^-/.test(raw)) throw new Error("USD cost must be non-negative");
  const match = raw.match(/^(\d+)(?:\.(\d+))?$/);
  if (!match) throw new Error(`USD cost must be a decimal, got ${JSON.stringify(value)}`);
  const whole = BigInt(match[1]);
  const fraction = match[2] || "";
  const micros = BigInt((fraction.slice(0, 6) || "").padEnd(6, "0") || "0");
  return whole * 1_000_000n + micros + (/[1-9]/.test(fraction.slice(6)) ? 1n : 0n);
}

module.exports = { usdMicrosFromDecimal };
