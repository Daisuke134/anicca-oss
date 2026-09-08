import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";


const STATES = new Set(["operational", "degraded", "frozen", "not-live"]);
const SELF_DIR = path.dirname(fileURLToPath(import.meta.url));


function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}


function ageSeconds(mtimeMs, nowMs) {
  if (!finiteNumber(mtimeMs) || !finiteNumber(nowMs)) return null;
  return Math.max(0, (nowMs - mtimeMs) / 1000);
}


function result(slot, state, reason, nowMs, evidence = {}) {
  return {
    id: slot.id,
    state,
    probedAt: new Date(nowMs).toISOString(),
    reason,
    evidence,
  };
}


export function classifySlotProbe({ slot, observation, nowMs = Date.now() } = {}) {
  if (!slot?.id || !slot?.probe?.kind || !observation || !finiteNumber(nowMs)) {
    return result(slot || { id: "unknown" }, "degraded", "probe_unavailable", nowMs || Date.now());
  }

  const { probe } = slot;
  if (probe.kind === "trace") {
    if (observation.killPresent === true) {
      return result(slot, "frozen", "intentional_kill_switch", nowMs, {
        traceExists: observation.exists === true,
      });
    }
    if (observation.exists !== true) {
      return result(slot, "degraded", "trace_missing", nowMs, { traceExists: false });
    }
    const traceAgeSeconds = ageSeconds(observation.mtimeMs, nowMs);
    if (traceAgeSeconds == null) {
      return result(slot, "degraded", "trace_time_invalid", nowMs, { traceExists: true });
    }
    if (traceAgeSeconds > Number(probe.maxAgeSeconds)) {
      return result(slot, "degraded", "stale_trace", nowMs, {
        traceAgeSeconds,
        maxAgeSeconds: Number(probe.maxAgeSeconds),
      });
    }
    if (observation.barren === true) {
      return result(slot, "degraded", "sustained_mechanism_failure", nowMs, {
        traceAgeSeconds,
        lastAction: observation.lastAction || null,
      });
    }
    return result(slot, "operational", "fresh_trace", nowMs, {
      traceAgeSeconds,
      lastAction: observation.lastAction || null,
    });
  }

  if (probe.kind === "http") {
    const httpStatus = Number(observation.status);
    if (Number.isInteger(httpStatus) && httpStatus >= 200 && httpStatus < 300) {
      return result(slot, "operational", "http_ready", nowMs, { httpStatus });
    }
    return result(slot, "degraded", "http_not_ready", nowMs, {
      httpStatus: Number.isInteger(httpStatus) ? httpStatus : null,
    });
  }

  if (probe.kind === "heartbeat") {
    if (observation.enabled !== true) {
      return result(slot, "not-live", "loop_not_enabled", nowMs, {
        enabled: false,
        alive: observation.alive === true,
      });
    }
    if (observation.alive !== true) {
      return result(slot, "degraded", "loop_not_alive", nowMs, {
        enabled: true,
        alive: false,
      });
    }
    if (observation.exists !== true) {
      return result(slot, "degraded", "heartbeat_missing", nowMs, {
        enabled: true,
        alive: true,
      });
    }
    const heartbeatAgeSeconds = ageSeconds(observation.mtimeMs, nowMs);
    if (heartbeatAgeSeconds == null) {
      return result(slot, "degraded", "heartbeat_time_invalid", nowMs);
    }
    if (heartbeatAgeSeconds > Number(probe.maxAgeSeconds)) {
      return result(slot, "degraded", "stale_heartbeat", nowMs, {
        heartbeatAgeSeconds,
        maxAgeSeconds: Number(probe.maxAgeSeconds),
      });
    }
    return result(slot, "operational", "fresh_heartbeat", nowMs, {
      heartbeatAgeSeconds,
      enabled: true,
      alive: true,
    });
  }

  if (probe.kind === "launchd-ledger") {
    if (observation.loaded !== true) {
      return result(slot, "degraded", "direct_owner_not_loaded", nowMs, {
        labels: observation.labels || {},
      });
    }
    if (observation.exists !== true) {
      return result(slot, "degraded", "direct_owner_ledger_missing", nowMs);
    }
    const ledgerAgeSeconds = ageSeconds(observation.mtimeMs, nowMs);
    if (ledgerAgeSeconds == null || ledgerAgeSeconds > Number(probe.maxAgeSeconds)) {
      return result(slot, "degraded", "direct_owner_ledger_stale", nowMs, {
        ledgerAgeSeconds,
        maxAgeSeconds: Number(probe.maxAgeSeconds),
      });
    }
    return result(slot, "operational", "direct_owners_loaded_fresh_ledger", nowMs, {
      ledgerAgeSeconds,
      labels: observation.labels || {},
    });
  }

  if (probe.kind === "funded-account") {
    if (observation.ok !== true || !finiteNumber(observation.balanceUsd) || observation.balanceUsd < 0) {
      return result(slot, "degraded", "account_probe_failed", nowMs);
    }
    if (observation.balanceUsd === 0) {
      return result(slot, "not-live", "unfunded", nowMs, { balanceUsd: 0 });
    }
    return result(slot, "operational", "funded", nowMs, {
      balanceUsd: observation.balanceUsd,
    });
  }

  if (probe.kind === "explicit") {
    return observation.enabled === true
      ? result(slot, "operational", "explicitly_enabled", nowMs, { enabled: true })
      : result(slot, "not-live", "not_enabled", nowMs, { enabled: false });
  }

  return result(slot, "degraded", "unknown_probe_kind", nowMs, {
    probeKind: String(probe.kind),
  });
}


export function aggregatePortfolio(portfolio, slotResults, nowMs = Date.now()) {
  const byId = new Map((slotResults || []).map((entry) => [entry.id, entry]));
  const members = {};
  let missing = false;
  for (const id of portfolio.memberIds || []) {
    const state = byId.get(id)?.state;
    if (!STATES.has(state)) missing = true;
    members[id] = STATES.has(state) ? state : "degraded";
  }
  const states = Object.values(members);
  let state = "not-live";
  let reason = "all_members_not_live";
  if (missing || states.includes("degraded")) {
    state = "degraded";
    reason = missing ? "member_probe_missing" : "member_degraded";
  } else if (states.includes("operational")) {
    state = "operational";
    reason = "member_operational";
  } else if (states.includes("frozen")) {
    state = "frozen";
    reason = "available_members_frozen";
  }
  return result({ id: portfolio.id }, state, reason, nowMs, { members });
}


export function resolveProbePath(relativePath, {
  base,
  runtimeRoot,
  homeDir,
} = {}) {
  if (typeof relativePath !== "string" || !relativePath || path.isAbsolute(relativePath)) {
    throw new Error("probe path must be non-empty and relative");
  }
  const root = base === "home" ? homeDir : runtimeRoot;
  if (typeof root !== "string" || !path.isAbsolute(root)) {
    throw new Error("probe root must be absolute");
  }
  const resolved = path.resolve(root, relativePath);
  if (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)) {
    throw new Error("probe path must not escape its root");
  }
  return resolved;
}


export async function buildHealthReport({
  registry,
  observe,
  nowMs = Date.now(),
} = {}) {
  if (!registry || !Array.isArray(registry.slots)) {
    throw new Error("earning health registry is invalid");
  }
  if (typeof observe !== "function") {
    throw new Error("earning health observer is required");
  }
  const slots = [];
  for (const slot of registry.slots) {
    let observation = null;
    try {
      observation = await observe(slot);
    } catch {
      observation = null;
    }
    slots.push(classifySlotProbe({ slot, observation, nowMs }));
  }
  const portfolioSpecs = Array.isArray(registry.portfolios) ? registry.portfolios : [];
  const portfolios = portfolioSpecs.map((portfolio) =>
    aggregatePortfolio(portfolio, slots, nowMs));
  return {
    ok: true,
    generatedAt: new Date(nowMs).toISOString(),
    instrumentedCount: registry.slots.filter((slot) => slot.instrumented === true).length,
    notInstrumentedCount: registry.slots.filter((slot) => slot.instrumented !== true).length,
    slots,
    portfolios,
  };
}


function readTail(filePath, maxBytes = 262_144) {
  const size = fs.statSync(filePath).size;
  const length = Math.min(size, maxBytes);
  const buffer = Buffer.alloc(length);
  const fd = fs.openSync(filePath, "r");
  try {
    fs.readSync(fd, buffer, 0, length, size - length);
  } finally {
    fs.closeSync(fd);
  }
  return buffer.toString("utf8");
}


function defaultBarrenImpl(text, minRun) {
  const checked = spawnSync(
    "python3",
    [path.join(SELF_DIR, "earning-health.py"), "is-barren", String(minRun || 20)],
    {
      input: text,
      encoding: "utf8",
      timeout: 10_000,
      maxBuffer: 1_048_576,
    },
  );
  if (checked.status === 0) return true;
  if (checked.status === 1) return false;
  throw new Error("barren predicate failed");
}


export async function observeSlot(slot, {
  runtimeRoot,
  homeDir = os.homedir(),
  fetchImpl = fetch,
  spawnSyncImpl = spawnSync,
  barrenImpl = defaultBarrenImpl,
  nowMs = Date.now(),
} = {}) {
  const probe = slot?.probe;
  if (!probe?.kind) return null;

  if (probe.kind === "trace") {
    const tracePath = resolveProbePath(probe.tracePath, {
      base: "runtime",
      runtimeRoot,
      homeDir,
    });
    const killPath = resolveProbePath(probe.killPath, {
      base: probe.killPathBase === "home" ? "home" : "runtime",
      runtimeRoot,
      homeDir,
    });
    if (!fs.existsSync(tracePath)) {
      return { exists: false, killPresent: fs.existsSync(killPath) };
    }
    const text = readTail(tracePath);
    const lines = text.split(/\r?\n/).filter(Boolean);
    let last;
    try {
      last = JSON.parse(lines.at(-1));
    } catch {
      return null;
    }
    return {
      exists: true,
      mtimeMs: fs.statSync(tracePath).mtimeMs,
      killPresent: fs.existsSync(killPath),
      barren: barrenImpl(text, slot.minRun || 20),
      lastAction: typeof last.action === "string" ? last.action : null,
    };
  }

  if (probe.kind === "http") {
    try {
      const response = await fetchImpl(probe.url, {
        signal: AbortSignal.timeout(Number(probe.timeoutMs) || 10_000),
      });
      return { status: response.status, checkedAtMs: nowMs };
    } catch {
      return { status: null, checkedAtMs: nowMs };
    }
  }

  if (probe.kind === "heartbeat") {
    const heartbeatPath = resolveProbePath(probe.heartbeatPath, {
      base: "home",
      runtimeRoot,
      homeDir,
    });
    const tmux = spawnSyncImpl(
      "tmux",
      ["-S", probe.sessionSocket, "has-session", "-t", probe.sessionName],
      { stdio: "ignore", timeout: 5_000 },
    );
    let launchdLoaded = false;
    if (probe.launchdLabel && process.platform === "darwin") {
      const launchd = spawnSyncImpl(
        "launchctl",
        ["print", `gui/${process.getuid()}/${probe.launchdLabel}`],
        { stdio: "ignore", timeout: 5_000 },
      );
      launchdLoaded = launchd.status === 0;
    }
    const exists = fs.existsSync(heartbeatPath);
    return {
      enabled: launchdLoaded || tmux.status === 0,
      alive: tmux.status === 0,
      exists,
      mtimeMs: exists ? fs.statSync(heartbeatPath).mtimeMs : null,
    };
  }

  if (probe.kind === "launchd-ledger") {
    // resolveProbePath rejects an empty path, so only resolve the singular spelling when a slot
    // still uses it. Slots that list ledgerPaths do not carry ledgerPath at all.
    const ledgerPath = probe.ledgerPath
      ? resolveProbePath(probe.ledgerPath, { base: "home", runtimeRoot, homeDir })
      : null;
    const labels = Object.fromEntries((probe.launchdLabels || []).map((label) => {
      const checked = process.platform === "darwin"
        ? spawnSyncImpl("launchctl", ["print", `gui/${process.getuid()}/${label}`], {
          stdio: "ignore", timeout: 5_000,
        })
        : { status: 1 };
      return [label, checked.status === 0];
    }));
    // A slot may own several lanes, and each one needs its own heartbeat. With a single ledgerPath
    // the freshest lane spoke for all of them: measured 2026-08-18, the gig slot probed only
    // gig/storefront-direct/wakes.jsonl while apply-direct ran a two-day-old release and fell from
    // 38 applications a day to 1 — loaded was true, storefront's rows were seconds old, and the
    // slot read operational throughout. Take the oldest ledger, so one silent lane cannot hide
    // behind a busy sibling. `ledgerPath` stays valid as the single-lane spelling.
    const ledgerPaths = (Array.isArray(probe.ledgerPaths) ? probe.ledgerPaths : [])
      .map((entry) => resolveProbePath(entry, { base: "home", runtimeRoot, homeDir }));
    const allPaths = ledgerPaths.length > 0 ? ledgerPaths : (ledgerPath ? [ledgerPath] : []);
    const present = allPaths.filter((entry) => fs.existsSync(entry));
    const exists = present.length === allPaths.length;
    const mtimes = present.map((entry) => fs.statSync(entry).mtimeMs);
    return {
      loaded: Object.keys(labels).length > 0 && Object.values(labels).every(Boolean),
      labels,
      exists,
      missingLedgers: allPaths.filter((entry) => !fs.existsSync(entry)),
      mtimeMs: exists && mtimes.length > 0 ? Math.min(...mtimes) : null,
    };
  }

  if (probe.kind === "funded-account" && probe.adapter === "hyperliquid") {
    const python = path.join(runtimeRoot, "skills/earn/hl-trade/.venv/bin/python");
    const script = path.join(runtimeRoot, "skills/earn/hl-trade/hl.py");
    if (!fs.existsSync(python) || !fs.existsSync(script)) {
      return { ok: false, checkedAtMs: nowMs };
    }
    const checked = spawnSyncImpl(python, [script, "account"], {
      encoding: "utf8",
      timeout: 20_000,
      maxBuffer: 1_048_576,
      env: { ...process.env, ANICCA_HOME: runtimeRoot },
    });
    if (checked.status !== 0) return { ok: false, checkedAtMs: nowMs };
    try {
      const parsed = JSON.parse(String(checked.stdout || ""));
      const balanceUsd = Number(parsed.account_value_usd);
      return {
        ok: Number.isFinite(balanceUsd) && balanceUsd >= 0,
        balanceUsd,
        checkedAtMs: nowMs,
      };
    } catch {
      return { ok: false, checkedAtMs: nowMs };
    }
  }

  if (probe.kind === "explicit") {
    const activationPath = resolveProbePath(probe.activationPath, {
      base: "runtime",
      runtimeRoot,
      homeDir,
    });
    return { enabled: fs.existsSync(activationPath), checkedAtMs: nowMs };
  }

  return null;
}


async function main() {
  const registryPath = process.env.EARNHC_REGISTRY
    || path.join(SELF_DIR, "earning-health-registry.json");
  const repoRoot = path.resolve(SELF_DIR, "..", "..");
  const runtimeRoot = process.env.EARNHC_RUNTIME_ROOT || process.env.ANICCA_HOME || repoRoot;
  const registry = JSON.parse(fs.readFileSync(registryPath, "utf8"));
  const nowMs = Date.now();
  const report = await buildHealthReport({
    registry,
    nowMs,
    observe: (slot) => observeSlot(slot, { runtimeRoot, nowMs }),
  });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exitCode = report.notInstrumentedCount === 0 ? 0 : 2;
}


if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    process.stderr.write(`earning-rail-health: ${String(error?.message || error).slice(0, 200)}\n`);
    process.exitCode = 1;
  });
}
