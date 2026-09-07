/**
 * config-drift.mjs — config-delivery-verification: declared-vs-runtime config drift detector.
 *
 * REQ-001/002: compares a DECLARED config (a plist's `EnvironmentVariables`, or the canonical
 *   `skills/registry.json`) against an OBSERVED runtime value (a live process's own env, or a
 *   runtime registry copy) and reports facts only — never a "safe/dangerous" judgment (REQ-007;
 *   `~/.claude/rules/building-effective-ai-agents.md` — judgment belongs to the model, not to
 *   hardcoded regex/keyword classification in this tool).
 * REQ-003: the SAME comparison function is reused for every instance; each instance's own declared
 *   config is passed in as data — there is no instance-name switch/if-else anywhere below.
 * REQ-004: any runtime state this module cannot observe (missing process, unreadable file, failed
 *   parse) is reported as an explicit FAIL (`reason: "unobservable"`), never silently folded into an
 *   empty/PASS result.
 * REQ-005: this module only ever reads. It never sends a process a termination/restart signal and
 *   never modifies any file it inspects (verified structurally by
 *   __tests__/config-drift.test.mjs's PROP-011 — this file must never contain the literal tokens for
 *   any restart/terminate primitive or a file-write call).
 * REQ-006: the diff/classify/resolve functions below take only plain data (objects, strings, arrays)
 *   and perform no file/process/network access — deterministic, side-effect-free, and they never
 *   mutate their inputs.
 * REQ-009 note: this file is unrelated to `brain.mjs`'s `claude -p` timeout — that lives in brain.mjs.
 * REQ-010: the monitored-target set below (`parseIndexLoopProcesses` / `listRunningIndexLoopProcesses`)
 *   is derived by enumerating live processes, never from a hardcoded instance-name list.
 * NFR-Security (FIND-002): the ONLY data this module ever extracts from a raw process command line is
 *   the small allow-listed set of keys a caller explicitly asks for (`parseAllowedEnvFromCommandLine`).
 *   The raw command-line string itself (which may carry secrets such as ANTHROPIC_API_KEY or a
 *   *_PRIVATE_KEY) is read once, filtered immediately, and then goes out of scope — it is never
 *   returned, logged, or included in any error path.
 */

import { promises as fs } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readDotenvFile } from './dotenv.mjs';
import { parseDotenv } from './config.mjs';

const execFileAsync = promisify(execFile);
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DEFAULT_CANONICAL_REGISTRY_PATH = path.join(REPO_ROOT, 'skills', 'registry.json');
const DEFAULT_PLIST_DIR = path.join(os.homedir(), 'Library', 'LaunchAgents');
// Node's execFile default maxBuffer is 1MB. A real `ps -AwwE -o pid,command` dump of this fleet's full
// process table (every process's FULL env appended, `-E`) is verified LIVE (2026-07-12) to run to
// ~3.2MB — well past that default, which makes execFile throw ERR_CHILD_PROCESS_STDOUT_MAXBUFFER,
// silently caught below and turned into an empty/null result (a real production bug this feature itself
// would otherwise have shipped: `listRunningIndexLoopProcesses` returning zero targets, NOT because no
// process is running, but because the buffer was too small to read the real answer). 16MB gives ample
// headroom as the fleet grows.
const PS_EXEC_OPTIONS = { maxBuffer: 16 * 1024 * 1024 };

// ── Pure core (REQ-006): plain data in, plain data out — no file/process/network access ──────────────

/**
 * Generic key/value diff. Compares every key present in EITHER map (not just declaredMap's own keys —
 * a key that exists only in `actualMap` must still be reported, e.g. a stale registry slot that only
 * exists in a runtime copy). Returns one entry per differing key; never mutates its inputs.
 *
 * @param {Record<string, unknown>} declaredMap
 * @param {Record<string, unknown>} actualMap
 * @returns {{key: string, declared: unknown, actual: unknown}[]}
 */
export function diffDeclaredVsActual(declaredMap, actualMap) {
  const declared = declaredMap || {};
  const actual = actualMap || {};
  const keys = new Set([...Object.keys(declared), ...Object.keys(actual)]);
  const diffs = [];
  for (const key of keys) {
    const declaredValue = declared[key];
    const actualValue = actual[key];
    if (declaredValue !== actualValue) {
      diffs.push({ key, declared: declaredValue, actual: actualValue });
    }
  }
  return diffs;
}

/**
 * REQ-004's pure classification primitive: is this observation usable, or is the underlying runtime
 * state simply unknown (process gone, file unreadable, parse failed)? `null`/`undefined` means the
 * effectful reader could not obtain a value at all.
 *
 * @param {unknown} rawObservation
 * @returns {"OBSERVED" | "UNOBSERVABLE"}
 */
export function classifyObservability(rawObservation) {
  return rawObservation === null || rawObservation === undefined ? 'UNOBSERVABLE' : 'OBSERVED';
}

/**
 * REQ-001 edge case / REQ-003: resolves what an instance's OWN declared config says a key's expected
 * value is. If the key was never explicitly declared, falls back to the code default but says so
 * explicitly (`declaredExplicitly: false`) — an omission is never silently treated as "no drift".
 * Pure pass-through: no instance-name branching of any kind.
 *
 * @param {Record<string, unknown>} declaredConfig
 * @param {string} key
 * @param {unknown} codeDefault
 * @returns {{value: unknown, declaredExplicitly: boolean}}
 */
export function resolveExpectedValue(declaredConfig, key, codeDefault) {
  const config = declaredConfig || {};
  const declaredExplicitly = Object.prototype.hasOwnProperty.call(config, key);
  return { value: declaredExplicitly ? config[key] : codeDefault, declaredExplicitly };
}

/**
 * REQ-002: flattens `registry.slots.<name>.status` into a flat `{name: status}` map so the generic
 * diff function above can compare a canonical registry against a runtime copy.
 *
 * @param {{slots?: Record<string, {status?: unknown}>}} registry
 * @returns {Record<string, unknown>}
 */
export function flattenRegistrySlotStatus(registry) {
  const slots = registry && registry.slots;
  if (!slots || typeof slots !== 'object') return {};
  const flat = {};
  for (const [name, def] of Object.entries(slots)) {
    flat[name] = def ? def.status : undefined;
  }
  return flat;
}

/**
 * Pure JSON-text parser for a plist already converted to JSON (e.g. by `plutil -convert json -o -`).
 * Takes the resulting text as a plain string — no file/process access happens here.
 *
 * @param {string} plutilJsonText
 * @returns {Record<string, unknown>}
 */
export function parsePlistEnvironmentVariables(plutilJsonText) {
  try {
    const parsed = JSON.parse(plutilJsonText);
    return (parsed && typeof parsed.EnvironmentVariables === 'object' && parsed.EnvironmentVariables) || {};
  } catch {
    return {};
  }
}

const ENV_TOKEN_PATTERN = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/;

/**
 * NFR-Security / FIND-002: extracts ONLY the caller-specified allow-listed keys from a raw process
 * command-line string. Every other token on the line (including any secret-bearing key such as
 * ANTHROPIC_API_KEY or a *_PRIVATE_KEY variable) is inspected only long enough to see it does not match
 * the allow-list, then discarded — it is never copied into the returned object, and this function never
 * includes any part of the raw command line in a thrown error message.
 *
 * @param {string} commandLine
 * @param {string[]} allowedKeys
 * @returns {Record<string, string>}
 */
export function parseAllowedEnvFromCommandLine(commandLine, allowedKeys) {
  const result = {};
  if (typeof commandLine !== 'string' || !Array.isArray(allowedKeys) || allowedKeys.length === 0) {
    return result;
  }
  const allowedSet = new Set(allowedKeys);
  const tokens = commandLine.split(/\s+/).filter(Boolean);
  for (const token of tokens) {
    const match = ENV_TOKEN_PATTERN.exec(token);
    if (!match) continue;
    const [, key, value] = match;
    if (!allowedSet.has(key)) continue; // not allow-listed -> discarded immediately, never retained
    result[key] = value;
  }
  return result;
}

/**
 * REQ-010: discovers monitoring targets by enumerating LIVE `runtime/loop/index.mjs` processes from a
 * raw `ps -Awwe -o pid,command` output (passed in as plain string lines — no `ps` invocation happens
 * here). Extracts `ANICCA_HOME=`/`XPC_SERVICE_NAME=` tokens from each matching line. A process missing
 * either token reports `null` for that field rather than being dropped from the result set (REQ-004/
 * REQ-010 edge case: an unobservable half of a target is never silently omitted). This function
 * contains no hardcoded instance-name or absolute-path list — it recognizes targets purely by the
 * `runtime/loop/index.mjs` substring every generation of this loop's own command line carries.
 *
 * @param {string[]} psOutputLines
 * @returns {{pid: number, aniccaHome: string|null, xpcServiceName: string|null}[]}
 */
export function parseIndexLoopProcesses(psOutputLines) {
  if (!Array.isArray(psOutputLines)) return [];
  const results = [];
  for (const line of psOutputLines) {
    if (typeof line !== 'string' || !line.includes('runtime/loop/index.mjs')) continue;
    const tokens = line.trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) continue;
    const pid = Number.parseInt(tokens[0], 10);
    if (!Number.isFinite(pid)) continue;
    let aniccaHome = null;
    let xpcServiceName = null;
    for (const token of tokens) {
      if (token.startsWith('ANICCA_HOME=')) aniccaHome = token.slice('ANICCA_HOME='.length);
      else if (token.startsWith('XPC_SERVICE_NAME=')) xpcServiceName = token.slice('XPC_SERVICE_NAME='.length);
    }
    results.push({ pid, aniccaHome, xpcServiceName });
  }
  return results;
}

/**
 * REQ-001/003/004: reports whether a single instance's declared `ANICCA_BRAIN` (or another key,
 * default parameter kept generic per REQ-006) matches its OWN observed runtime value. `declaredConfig`
 * is that instance's own plist-derived map (REQ-003: never a global/shared expectation). Returns a FAIL
 * (`reason: "unobservable"`) when the runtime value could not be observed at all, and returns exactly
 * one drift entry (or zero, on a match) otherwise. Contains no instance-name branching.
 *
 * FIND-003 fix: a `declaredConfig` that is itself `null`/`undefined` (the plist could not be read at
 * all) is NOT the same as a `declaredConfig` of `{}` (the plist WAS read, it simply never declared this
 * key — REQ-001's own "inherits code default" edge case). The former is fail-closed FAIL (REQ-004): an
 * unreadable declared side must never quietly resolve to "no drift", exactly like an unreadable actual
 * side already does.
 *
 * Live-run correctness fix (found running the real detector against the production fleet 2026-07-12,
 * `com.anicca.daemon`): `ps` can only ever show a key that was EXPLICITLY present in the process's
 * spawned env — it cannot see an in-process JS default like `brain.mjs`'s own
 * `config.ANICCA_BRAIN || 'proxy'`. So an OBSERVED env object that is simply missing `key` (as opposed
 * to `observedEnvOrNull` being `null`, i.e. genuinely unobservable) must resolve the SAME "inherits
 * codeDefault" rule the declared side already gets — otherwise every instance that never explicitly
 * overrides `ANICCA_BRAIN` reports a spurious `declared: 'proxy' vs actual: undefined` "drift" that
 * isn't real (the running process's own effective value is ALSO `codeDefault`, by the identical
 * fallback rule).
 *
 * @param {string} instance
 * @param {Record<string, unknown>|null} declaredConfig
 * @param {Record<string, unknown>|null} observedEnvOrNull
 * @param {string} [key]
 * @param {unknown} [codeDefault]
 * @returns {Array<{key: string, instance: string, declared: unknown, actual: unknown, reason?: string}>}
 */
export function detectBrainDrift(instance, declaredConfig, observedEnvOrNull, key = 'ANICCA_BRAIN', codeDefault = 'proxy') {
  if (classifyObservability(declaredConfig) === 'UNOBSERVABLE') {
    return [{ key, instance, declared: null, actual: null, reason: 'unobservable' }];
  }
  const { value: declared } = resolveExpectedValue(declaredConfig, key, codeDefault);
  if (classifyObservability(observedEnvOrNull) === 'UNOBSERVABLE') {
    return [{ key, instance, declared, actual: null, reason: 'unobservable' }];
  }
  const { value: actual } = resolveExpectedValue(observedEnvOrNull, key, codeDefault);
  return diffDeclaredVsActual({ [key]: declared }, { [key]: actual }).map((d) => ({ ...d, instance }));
}

/**
 * REQ-002/004: compares the canonical registry against N runtime copies, each reported INDEPENDENTLY
 * (PROP-006/PROP-010 — one matching copy must never suppress another copy's drift or its
 * unobservable-ness). A copy that could not be read at all (`registryOrNull === null`) reports a single
 * FAIL for that whole copy rather than a per-slot breakdown (there is nothing slot-level left to say
 * once the copy itself cannot be read).
 *
 * FIND-005 fix (iteration-2, pure-core fail-closed): an unreadable `canonicalRegistry` itself (not just
 * an unreadable copy) is now handled AT THIS PURE-CORE LEVEL — every copy reports `reason:
 * 'unobservable'` (there is nothing to declare a drift against without a canonical to compare to).
 * Previously this was only guarded externally by the composed `runConfigDriftDetector` wrapper; now the
 * REQ-006 pure function itself fails closed, testable in isolation without needing the wrapper at all.
 *
 * @param {{slots?: Record<string, {status?: unknown}>}|null} canonicalRegistry
 * @param {{copyPath: string, registryOrNull: object|null}[]} copies
 * @returns {Array<{key: string, copyPath: string, declared: unknown, actual: unknown} | {copyPath: string, declared: null, actual: null, reason: string}>}
 */
export function detectRegistryStatusDrift(canonicalRegistry, copies) {
  const safeCopies = copies || [];
  if (classifyObservability(canonicalRegistry) === 'UNOBSERVABLE') {
    return safeCopies.map(({ copyPath }) => ({ copyPath, declared: null, actual: null, reason: 'unobservable' }));
  }
  const declaredMap = flattenRegistrySlotStatus(canonicalRegistry);
  const results = [];
  for (const { copyPath, registryOrNull } of safeCopies) {
    if (classifyObservability(registryOrNull) === 'UNOBSERVABLE') {
      results.push({ copyPath, declared: null, actual: null, reason: 'unobservable' });
      continue;
    }
    const actualMap = flattenRegistrySlotStatus(registryOrNull);
    for (const d of diffDeclaredVsActual(declaredMap, actualMap)) {
      results.push({ key: `${d.key}.status`, copyPath, declared: d.declared, actual: d.actual });
    }
  }
  return results;
}

// ── Effectful shell (thin adapters — I/O only, no diff/classification logic of their own) ─────────────

/**
 * Reads and JSON-parses a registry.json file (canonical or a runtime copy). Read-only: never writes to
 * `filePath`. Missing file / malformed JSON both resolve to `null` (REQ-004: the caller treats `null` as
 * unobservable — a FAIL, never a silent PASS).
 *
 * @param {string} filePath
 * @returns {Promise<object|null>}
 */
export async function readRegistryFile(filePath) {
  try {
    const raw = await fs.readFile(filePath, 'utf8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * Reads a deployed plist's `EnvironmentVariables` dict via `plutil`. Read-only: never modifies
 * `plistPath`. Any failure (missing plist, plutil error) resolves to `null` (REQ-004 unobservable).
 *
 * @param {string} plistPath
 * @returns {Promise<Record<string, unknown>|null>}
 */
export async function readPlistDeclaredEnv(plistPath) {
  try {
    const { stdout } = await execFileAsync('plutil', ['-convert', 'json', '-o', '-', plistPath]);
    return parsePlistEnvironmentVariables(stdout);
  } catch {
    return null;
  }
}

/**
 * REQ-010's effectful half: enumerates ALL live processes via `ps -Awwe -o pid,command` and returns the
 * raw output lines (no filtering/parsing here — `parseIndexLoopProcesses` does that, purely). Never
 * filters by a hardcoded instance list; the pure parser above decides which lines are monitoring
 * targets based solely on their own command line.
 *
 * @returns {Promise<string[]>}
 */
export async function listRunningIndexLoopProcesses() {
  try {
    // CAPITAL `-E` (not `-e`) is what actually appends env on macOS (verified live 2026-07-12);
    // `-A` (all processes) is fine here since there is no `-p` filter for it to override.
    const { stdout } = await execFileAsync('ps', ['-AwwE', '-o', 'pid,command'], PS_EXEC_OPTIONS);
    return stdout.split('\n');
  } catch {
    return [];
  }
}

// ── Composed detector (REQ-010 discovery → REQ-001/002 comparison → REQ-004 fail-closed report) ──────

/**
 * The actual, runnable "config-drift detector" every EARS statement in behavioral-spec.md refers to:
 * discovers every live `runtime/loop/index.mjs` instance (REQ-010), compares each one's declared vs
 * observed `ANICCA_BRAIN` (REQ-001) and verifies the one canonical repository registry remains
 * observable (REQ-002/004) — facts only, no severity judgment (REQ-007).
 *
 * Every I/O boundary is injectable via `deps` (defaulting to the real adapters above) specifically so
 * this function's own test can drive it end-to-end with fixture data and NEVER invoke a real `ps`/
 * `plutil`/`launchctl`/filesystem call.
 *
 * @param {object} [deps]
 * @param {() => Promise<string[]>} [deps.listProcesses]
 * @param {(plistPath: string) => Promise<Record<string, unknown>|null>} [deps.readPlistEnv]
 * @param {(filePath: string) => Promise<object|null>} [deps.readRegistry]
 * @param {(aniccaHome: string) => Promise<string>} [deps.readDotenvText]
 * @param {string} [deps.plistDir]
 * @param {string} [deps.canonicalRegistryPath]
 * @param {string} [deps.brainKey]
 * @param {unknown} [deps.codeDefaultBrain]
 * @returns {Promise<{generatedAt: string, targets: object[], brainDrift: object[], registryDrift: object[], overallStatus: "PASS"|"FAIL"|"NO_TARGETS"}>}
 */
export async function runConfigDriftDetector(deps = {}) {
  const {
    listProcesses = listRunningIndexLoopProcesses,
    readPlistEnv = readPlistDeclaredEnv,
    readRegistry = readRegistryFile,
    readDotenvText = readDotenvFile,
    plistDir = DEFAULT_PLIST_DIR,
    canonicalRegistryPath = DEFAULT_CANONICAL_REGISTRY_PATH,
    brainKey = 'ANICCA_BRAIN',
    codeDefaultBrain = 'proxy',
  } = deps;

  const psOutputLines = await listProcesses();
  const targets = parseIndexLoopProcesses(psOutputLines);

  const brainDrift = [];

  for (const target of targets) {
    const instance = target.xpcServiceName || `pid:${target.pid}`;
    // REQ-001's declared side: this target's OWN plist, found via its OWN observed xpcServiceName --
    // never a shared/global expectation (REQ-003). Missing xpcServiceName -> declared side is itself
    // unobservable (REQ-010 edge case), which detectBrainDrift's FIND-003(iter1) fix now reports as a
    // FAIL.
    const rawPlistEnv = target.xpcServiceName
      ? await readPlistEnv(path.join(plistDir, `${target.xpcServiceName}.plist`))
      : null;
    // FIND-003 fix (iteration-2): the plist is only the TOP of config.mjs's real precedence chain
    // (processEnv > dotenv($ANICCA_HOME/.env) > DEFAULTS — config.mjs:104-112). A plist that read fine
    // but simply never mentions this key must still check $ANICCA_HOME/.env before this feature treats
    // it as "inherits codeDefault" -- otherwise a real operator override sourced from .env (which `ps`
    // can NEVER see: index.mjs:81-82 merges dotenv values into the in-memory `config` object without
    // ever writing them back to process.env) would be silently invisible to this detector, reporting a
    // false PASS for a real drift. A plist that could not be read AT ALL stays unobservable regardless
    // of .env (an unreadable identity source isn't rescued by a lower-precedence layer).
    let declaredEnvOrNull = rawPlistEnv;
    if (classifyObservability(rawPlistEnv) !== 'UNOBSERVABLE' && target.aniccaHome) {
      const envFileValues = parseDotenv(await readDotenvText(target.aniccaHome));
      declaredEnvOrNull = { ...envFileValues, ...rawPlistEnv }; // plist (highest, mirrors processEnv) wins over .env
    }
    // REQ-001's actual side: re-derives ONLY the allow-listed brainKey from the SAME raw ps line this
    // target was discovered from -- no other token on that line (e.g. a secret) is ever retained here
    // (NFR-Security/FIND-002 from iteration-1).
    const rawLine = psOutputLines.find((l) => typeof l === 'string' && l.trim().split(/\s+/)[0] === String(target.pid));
    const observedEnv = rawLine ? parseAllowedEnvFromCommandLine(rawLine, [brainKey]) : null;
    brainDrift.push(...detectBrainDrift(instance, declaredEnvOrNull, observedEnv, brainKey, codeDefaultBrain));

  }

  const canonicalRegistry = await readRegistry(canonicalRegistryPath);
  // There are intentionally no per-instance registry copies. Comparing the canonical observation
  // with itself keeps the existing fail-closed report shape while an observable registry is clean.
  const registryDrift = detectRegistryStatusDrift(canonicalRegistry, [{
    copyPath: canonicalRegistryPath,
    registryOrNull: canonicalRegistry,
  }]);

  const hasDrift = brainDrift.length > 0 || registryDrift.length > 0;
  // REQ-010 edge case: zero discovered targets is its OWN explicit status, never silently folded into
  // "PASS" (an empty fleet is not the same fact as "everything I checked matched").
  const overallStatus = targets.length === 0 ? 'NO_TARGETS' : (hasDrift ? 'FAIL' : 'PASS');

  return {
    generatedAt: new Date().toISOString(),
    targets,
    brainDrift,
    registryDrift,
    overallStatus,
  };
}

// ── CLI entrypoint ─────────────────────────────────────────────────────────────────────────────────────
// `node runtime/loop/config-drift.mjs` runs the real detector against the live fleet. stdout carries
// ONLY the JSON report (this repo's own rule: a loop's child script must never mix diagnostic text into
// stdout, or the parent misparses it -- any diagnostic here goes to stderr instead). Exit code: 0 only
// for a genuine PASS; non-zero for FAIL (drift found, or anything unobservable) and for NO_TARGETS
// (never let "nothing was even checked" look like success to a caller only checking the exit code).
const isMainModule = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  runConfigDriftDetector()
    .then((report) => {
      process.stdout.write(`${JSON.stringify(report)}\n`);
      process.exit(report.overallStatus === 'PASS' ? 0 : 1);
    })
    .catch((err) => {
      process.stderr.write(`[config-drift] fatal: ${err && err.message}\n`);
      process.exit(2);
    });
}
