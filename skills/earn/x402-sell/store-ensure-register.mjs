#!/usr/bin/env node
// store-ensure-register.mjs — SELF-STORE-1 idempotent x402scan listing for the `ensure` action of
// the x402_sell loop slot. Probes the seller's own /.well-known/x402.json for its live product
// count and re-runs register-x402scan.mjs (SIWX registration) ONLY when something actually
// changed — a store with an unchanged catalog does not need to re-sign the SIWX challenge every
// wake. Honest degraded output (never throws) when there's no public URL yet: a fresh instance
// with no tunnel isn't a bug, just not discoverable yet.
//
// Env:
//   X402_PUBLIC_URL   the seller's live https origin (required to probe/register; else degrades)
//   X402_PAYTO        this instance's own receiving wallet (default: derived from the per-instance key)
//
// Loop child-script contract: prints exactly ONE JSON line on stdout, never throws, always exit 0.
//   {"registered":bool,"productCount":n,"reregistered":bool,"reason"?:string}
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { privateKeyToAccount } from "viem/accounts";
import { loadEvmKey } from "../lib/resolve-identity.mjs";
import { X402_STATE_DIR } from "./state-paths.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
export const STATE_DIR = X402_STATE_DIR;
const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;

// Mirrors serve-v2.mjs's payTo() (#28: this instance's OWN gated key, never a borrowed one).
export function resolvePayTo(env = process.env) {
  if (env.X402_PAYTO) return env.X402_PAYTO;
  const pk = loadEvmKey({ env });
  if (pk) return privateKeyToAccount(pk).address;
  return null;
}

function loadState(path) {
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return null; }
}

// Pure decision, unit-tested directly (no disk/network involved):
//   missing state -> true, product count changed -> true, last register >7 days ago -> true,
//   else (fresh state + same count) -> false.
export function shouldReregister(state, now, productCount) {
  if (!state) return true;
  if (typeof state.productCount === "number" && state.productCount !== productCount) return true;
  if (typeof state.ts !== "number" || now - state.ts > SEVEN_DAYS_MS) return true;
  return false;
}

export async function fetchProductCount(publicUrl) {
  const res = await fetch(`${publicUrl.replace(/\/+$/, "")}/.well-known/x402.json`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`manifest fetch HTTP ${res.status}`);
  const body = await res.json();
  return Array.isArray(body?.resources) ? body.resources.length : 0;
}

// Spawns register-x402scan.mjs as a child (same pattern the caller-supplied env expects) — never
// throws; resolves {ok:boolean}.
export function runRegister(origin, env = process.env) {
  return new Promise((resolve) => {
    const child = spawn("/bin/bash", [join(HERE, "register-x402scan-boot.sh")], {
      env: { ...env, ORIGIN: origin },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.on("close", (code) => resolve({ ok: code === 0 }));
    child.on("error", () => resolve({ ok: false }));
  });
}

export function stateFilePath(payTo) {
  return join(STATE_DIR, `x402scan-registered-${payTo.toLowerCase()}.json`);
}

export async function ensureRegistered(env = process.env, now = Date.now()) {
  const publicUrl = env.X402_PUBLIC_URL || "";
  if (!publicUrl) return { registered: false, reason: "no public URL" };

  const payTo = resolvePayTo(env);
  if (!payTo) return { registered: false, reason: "no payTo resolvable" };

  let productCount;
  try {
    productCount = await fetchProductCount(publicUrl);
  } catch (e) {
    return { registered: false, reason: `manifest unreachable: ${e.message}` };
  }

  const path = stateFilePath(payTo);
  const state = loadState(path);
  const needsReregister = shouldReregister(state, now, productCount);

  if (!needsReregister) {
    return { registered: true, productCount, reregistered: false };
  }

  const { ok } = await runRegister(publicUrl, env);
  if (ok) {
    try {
      mkdirSync(STATE_DIR, { recursive: true });
      writeFileSync(path, JSON.stringify({ ts: now, productCount, origin: publicUrl }), "utf8");
    } catch { /* best-effort — a write failure must not flip a real registration to failed */ }
    return { registered: true, productCount, reregistered: true };
  }
  return { registered: false, productCount, reregistered: false, reason: "register-x402scan failed" };
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  ensureRegistered()
    .then((out) => console.log(JSON.stringify(out)))
    .catch((e) => console.log(JSON.stringify({ registered: false, reason: e.message || String(e) })));
}
