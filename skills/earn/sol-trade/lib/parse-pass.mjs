// parse-pass.mjs — deterministic, total parser for franklin-trading's own fixed-format
// "Signature: <base58>" receipt line (REQ-002 / PROP-010). NOT judgment: the trading decision
// already happened; this only reads the CLI's own receipt back (parsing of a fixed machine
// format, consistent with the project's regex-for-judgment ban, which targets decision-making,
// not fixed-format log parsing). Multiple "Signature:" lines can appear in one pass's stdout
// (a multi-step swap chain in a single session). The complete ordered set is needed to calculate
// round-trip P&L; the last-only helper remains for callers that need the terminal receipt.
const ANSI_RE = /\x1b\[[0-9;]*m/g;
// base58 alphabet (no 0 O I l), 60-100 chars covers real Solana signatures (~87-88 chars) with
// slack for format drift, mirroring solana-verify.mjs's own B58_RE discipline.
const SIG_RE = /signature:\s*([1-9A-HJ-NP-Za-km-z]{60,100})/gi;

export function extractLastSignature(stdout) {
  return extractSignatures(stdout).at(-1) || null;
}

export function extractSignatures(stdout) {
  if (typeof stdout !== "string" || stdout.length === 0) return [];
  const clean = stdout.replace(ANSI_RE, "");
  const signatures = [];
  for (const m of clean.matchAll(SIG_RE)) {
    if (!signatures.includes(m[1])) signatures.push(m[1]);
  }
  return signatures;
}

// CLI: `printf '%s' "$OUT" | node parse-pass.mjs` reads the full captured pass stdout from
// stdin and prints the complete JSON signature list (nothing if none).
import path from "node:path";
import { fileURLToPath } from "node:url";

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => { input += chunk; });
  process.stdin.on("end", () => {
    const signatures = extractSignatures(input);
    if (signatures.length) process.stdout.write(JSON.stringify(signatures) + "\n");
  });
}
