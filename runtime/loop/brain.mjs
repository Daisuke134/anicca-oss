/**
 * brain.mjs — Effectful: unified THINK step with fallback.
 *
 * REQ-011: Pluggable brain backend:
 *   ANICCA_BRAIN=proxy   → HTTP to OPENAI_BASE_URL (default)
 *   ANICCA_BRAIN=claude-p → spawn `claude -p` subprocess
 *   claude binary missing → fall back to proxy, never crash
 *
 * REQ-004: claude -p child env is scrubbed (no private keys).
 */

import https from 'node:https';
import http from 'node:http';
import os from 'node:os';
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { buildSystemPrompt, buildUserMessage, getToolDefinitions } from './prompt.mjs';
// spec 25 O1: expose each live skill as a pickable tool (enum on run_skill.slot).
import { scrubPrivateKeys } from './env-filter.mjs';

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

/**
 * Execute the THINK step for the current wake.
 *
 * @param {object} ctx - WakeContext
 * @param {object} config - loaded config
 * @returns {Promise<object>} - raw response (OpenAI-compatible)
 * @throws only if both backends fail (caller writes wake_error)
 */
export async function think(ctx, config) {
  const brain = config.ANICCA_BRAIN || 'proxy';

  if (brain === 'claude-p') {
    // NO FALLBACK TO THE PROXY. claude-p is human-funded: its brain is an Anthropic subscription
    // that is already paid for, and its crypto wallet exists to TRADE, not to buy inference.
    // Falling back to ClawRouter silently put a free model in charge of the money — and measured
    // 2026-07-12 that model could not even issue the tool call correctly ("run_skill skill not
    // found"), so a wake that fell back was worse than a wake that never happened. Failing loudly
    // is right: the ledger records a wake_error and the next wake retries with the real brain.
    //
    // Franklin and the other SELF-funded instances still take the proxy path below, and that is
    // correct for them: they buy inference out of the very wallet they trade with, so a free model
    // is the only way they stay net-positive.
    return thinkClaudeP(ctx, config);
  }

  return thinkProxy(ctx, config);
}

// ── Proxy brain (HTTP POST) ──────────────────────────────────────────────────

async function thinkProxy(ctx, config) {
  const baseUrl = config.OPENAI_BASE_URL || 'http://127.0.0.1:18402/v1';
  const url = baseUrl.replace(/\/+$/, '') + '/chat/completions';

  // Policy 2026-06-21 (Dais): use the survival-tier model (ctx.model from tier.mjs). For LEAN
  // and FUNDED tiers that resolves to ClawRouter `auto` — paid (kimi-k2.7 ~$0.004/call typical)
  // is OK because net-positive (earn − burn) is the real goal, not zero compute. BROKE tier
  // (balance=0) stays on `free/gpt-oss-120b` as a safety floor since an empty wallet can't pay.
  // Operator override via ANICCA_MODEL.
  const body = JSON.stringify({
    model: config.ANICCA_MODEL || ctx.model || 'auto',
    messages: [
      { role: 'system', content: buildSystemPrompt(ctx) },
      { role: 'user',   content: buildUserMessage(ctx) },
    ],
    // franklin-alwaysact-skill-router REQ-504: an always-act-engaged ctx offers ONLY the (possibly
    // reroute-narrowed) always-act menu, with sleep withheld — a non-engaged ctx (the overwhelming
    // majority of all wakes) produces the exact byte-for-byte current call.
    tools: getToolDefinitions(
      ctx.alwaysActEngaged ? ctx.alwaysActMenu : ctx.activeSkillSlots,
      { omitSleep: ctx.alwaysActEngaged === true },
    ),
    tool_choice: 'auto',
    max_tokens: 512,
  });

  let lastErr;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    if (attempt > 0) await delay(RETRY_DELAY_MS);
    try {
      const raw = await httpPost(url, body);
      return JSON.parse(raw);
    } catch (err) {
      lastErr = err;
    }
  }
  throw new Error(`proxy_down: ${lastErr?.message || 'unknown'}`);
}

// ── Claude -p brain (subprocess) ────────────────────────────────────────────

/**
 * REQ-009: resolves the wall-clock timeout (ms) for a single `claude -p` child.
 *
 * Default = the resolved `SLEEP_BASE_S` (the caller's own already-resolved wake-interval seconds,
 * `config.SLEEP_BASE_S` with its own 120 fallback applied by the caller) converted to milliseconds.
 * `overrideVal` (e.g. `CLAUDE_P_TIMEOUT_S`) is used ONLY when it is a positive, finite INTEGER number
 * of seconds — any other value (missing, non-numeric, zero, negative, non-integer) falls back to the
 * default. An override larger than `resolvedSleepBaseS` is clamped DOWN to `resolvedSleepBaseS` itself
 * (never a borrowed unrelated constant) so a single THINK call can never outrun the next scheduled wake.
 *
 * @param {unknown} overrideVal
 * @param {number} resolvedSleepBaseS
 * @returns {number} timeout in milliseconds
 */
export function resolveClaudePTimeoutMs(overrideVal, resolvedSleepBaseS) {
  const defaultMs = resolvedSleepBaseS * 1000;
  const n = Number(overrideVal);
  const isValidPositiveInteger =
    overrideVal !== null && overrideVal !== undefined && overrideVal !== '' &&
    Number.isFinite(n) && Number.isInteger(n) && n > 0;
  if (!isValidPositiveInteger) return defaultMs;
  const clampedSeconds = Math.min(n, resolvedSleepBaseS);
  return clampedSeconds * 1000;
}

/**
 * REQ-009 / FIND-005: standalone effectful-shell primitive (the `runClaudePWithTimeout` name
 * verification-architecture.md's Purity Boundary Map documents, previously inlined directly inside
 * thinkClaudeP). Spawns `bin` with `args`, enforcing `timeoutMs` via the SAME two-stage
 * SIGTERM -> 2000ms grace -> SIGKILL pattern index.mjs:1037-1041 already uses elsewhere in this repo.
 * Resolves `{stdout, stderr, code}` on a normal exit (any exit code); rejects with a
 * `claude_p_timeout`-tagged error if the timeout fired.
 *
 * FIND-004 fix: the inner 2000ms grace-period timer IS captured (`graceTimer`) and explicitly cleared
 * on both `exit` and `error` — previously it was never stored, so a child that honored SIGTERM and
 * exited cleanly within the grace period left that timer dangling, later firing a no-op `kill()`
 * against an already-dead PID.
 *
 * @param {string} bin
 * @param {string[]} args
 * @param {{env: Record<string, string>, cwd: string, timeoutMs: number}} options
 * @returns {Promise<{stdout: string, stderr: string, code: number|null}>}
 */
export function runClaudePWithTimeout(bin, args, { env, cwd, timeoutMs }) {
  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let graceTimer = null;

    const proc = spawn(bin, args, { env, cwd, stdio: ['ignore', 'pipe', 'pipe'] });

    // REQ-009: today this spawn() has NO timeout at all -- a hung child hangs the wake forever (never
    // resolves, never rejects). Port index.mjs:1037-1041's existing two-stage pattern here: on timeout,
    // request termination first, then force-stop after a 2000ms grace period if it is still alive.
    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill('SIGTERM'); } catch {}
      graceTimer = setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 2000);
    }, timeoutMs);

    proc.stdout.on('data', d => { stdout += d; });
    proc.stderr.on('data', d => { stderr += d; });

    proc.on('error', (err) => {
      clearTimeout(timer);
      clearTimeout(graceTimer);
      reject(new Error(`claude_not_found: ${err.message}`));
    });

    proc.on('exit', (code) => {
      clearTimeout(timer);
      clearTimeout(graceTimer);
      if (timedOut) {
        reject(new Error(`claude_p_timeout: exceeded ${timeoutMs}ms`));
        return;
      }
      resolve({ stdout, stderr, code });
    });
  });
}

export async function thinkClaudeP(ctx, config) {
  const claudeBin = config.CLAUDE_BIN || process.env.CLAUDE_BIN || 'claude';
  const model = config.ANICCA_BRAIN_MODEL || 'claude-sonnet-4-6';
  const resolvedSleepBaseS = config.SLEEP_BASE_S != null ? Number(config.SLEEP_BASE_S) : 120;
  const timeoutMs = resolveClaudePTimeoutMs(config.CLAUDE_P_TIMEOUT_S, resolvedSleepBaseS);

  // TEMPORARY, and known to be the wrong shape. `claude -p` DOES have a tool-call channel:
  // `--mcp-config` / `--strict-mcp-config` (verified in `claude --help`, 2026-07-13). The right fix
  // is a tiny MCP server exposing run_skill/sleep with a Zod schema — exactly how our own
  // blockrun-mcp already registers its tools — which deletes this hand-written envelope entirely.
  // It is not a drop-in: under --dangerously-skip-permissions claude EXECUTES an MCP tool it calls,
  // which would collapse the loop's decide-here / execute-there split, so the handler must return
  // the decision rather than act on it. Tracked as T13. See 36-blockrun-mcp-toolcall-bp.md.
  //
  // Until then: the model must be SHOWN the schema in text, or it guesses at a shape nobody gave it.
  //
  // It was guessing. Measured 2026-07-13: it answered with a tool_calls block that carried no
  // `slot`, parse-tool-call.mjs fell back to `toolCall.function.name`, and every claude-p wake
  // dispatched the literal slot "run_skill" — the TOOL's name, not a skill — and died as
  // `skill_missing`. The brain was working; nobody had told it how to speak.
  const menuSlots = (ctx.alwaysActEngaged ? ctx.alwaysActMenu : ctx.activeSkillSlots) || [];
  const slotList = menuSlots.map((s) => `  - ${s}`).join('\n');
  const canSleep = ctx.alwaysActEngaged !== true;
  const shapeSpec = [
    'Answer with ONE JSON object and nothing else — no prose, no markdown fence:',
    '',
    '{"tool_calls":[{"function":{"name":"run_skill","arguments":"{\\"slot\\":\\"<slot>\\",\\"args\\":{}}"}}]}',
    '',
    'where <slot> is EXACTLY one of:',
    slotList || '  (no slots available this wake)',
    // Canonical few-shot for the slot weak models kept mis-calling. Products/prices remain fixed,
    // while args.action exposes the lifecycle already implemented by skills/earn/run.sh.
    ...(menuSlots.includes('x402_sell')
      ? ['', 'x402_sell args.action choices: ensure / review / improve / update. Products/prices are FIXED; do not invent one.',
         'Example review call:',
         '{"tool_calls":[{"function":{"name":"run_skill","arguments":"{\\"slot\\":\\"x402_sell\\",\\"args\\":{\\"action\\":\\"review\\"}}"}}]}']
      : []),
    ...(canSleep
      ? ['', 'Or, to do nothing this wake:', '{"tool_calls":[{"function":{"name":"sleep","arguments":"{}"}}]}']
      : []),
  ].join('\n');

  const prompt = [
    buildSystemPrompt(ctx),
    '',
    buildUserMessage(ctx),
    '',
    shapeSpec,
    '',
    // franklin-alwaysact-skill-router REQ-504: an always-act-engaged wake must not be told sleep is
    // an option, so the text-mode brain path stays consistent with the tool-schema path.
    ctx.alwaysActEngaged === true
      ? 'Respond with a JSON tool_calls block using run_skill.'
      : 'Respond with a JSON tool_calls block using run_skill or sleep.',
  ].join('\n');

  // MINIMAL env (not the full process.env): a full env + the project cwd makes `claude -p` load this
  // repo's .claude hooks/MCP/CLAUDE.md and HANG >2min (verified 2026-07-04: child at 0% CPU). The proven
  // 4-second recipe = minimal env (HOME+PATH) + a neutral cwd (/tmp), so claude only reads ~/.claude auth.
  // Private keys are excluded by construction here (we allow-list, not scrub). REQ-004 preserved.
  const scrubbed = scrubPrivateKeys(process.env);
  const childEnv = {
    HOME: process.env.HOME,
    PATH: process.env.PATH,
    ...(scrubbed.ANTHROPIC_API_KEY ? { ANTHROPIC_API_KEY: scrubbed.ANTHROPIC_API_KEY } : {}),
    ...(scrubbed.CLAUDE_CODE_OAUTH_TOKEN ? { CLAUDE_CODE_OAUTH_TOKEN: scrubbed.CLAUDE_CODE_OAUTH_TOKEN } : {}),
  };

  // CLAUDE-P-1 (2026-07-17): launchd cannot refresh the Claude subscription OAuth token headlessly
  // (keychain locked) -- measured live: claude-p's THINK died `claude_exit_1` (empty stderr = OAuth
  // session expired) 797x straight from 2026-07-17 01:14. AUTH-1 (306e0f73) already worked around the
  // identical failure for every earn-loop `*-cli.sh` launcher (see gig-cli.sh:47-55) by routing through
  // the local CLIProxyAPI, whose creds are plain files and refresh headlessly, but never touched this
  // spawn site. Same fix, same source of truth. This is NOT the "no fallback to a free model" ban this
  // file documents above (thinkClaudeP is only reached when ANICCA_BRAIN=claude-p) -- CLIProxyAPI still
  // serves the real Sonnet model claude-p is entitled to; only the dead auth transport is swapped out.
  const cliProxyKey = readCliProxyKey(process.env.HOME);
  if (cliProxyKey) {
    childEnv.ANTHROPIC_BASE_URL = 'http://127.0.0.1:8317';
    childEnv.ANTHROPIC_AUTH_TOKEN = cliProxyKey;
  }

  const { stdout, stderr, code } = await runClaudePWithTimeout(claudeBin, [
    '-p', prompt,
    '--output-format', 'json',
    '--model', model,
    // Without this the child stops to ASK for tool permission and answers with advice about
    // settings.json/allowedTools instead of the wake's decision -- observed verbatim 2026-07-12,
    // and it is why every claude-p wake fell through parseToolCall into `narrate` while looking
    // perfectly healthy (zero errors, no fallback). The loops that already run on Sonnet
    // (claude-p-mainloop.sh:78, self-fix.sh) all pass this flag; this one simply never did.
    '--dangerously-skip-permissions',
  ], {
    env: childEnv,
    cwd: os.tmpdir(),   // neutral cwd → no project .claude/hooks loaded → no 2-min hang
    timeoutMs,
  });

  if (code !== 0) {
    throw new Error(`claude_exit_${code}: ${stderr.slice(0, 200)}`);
  }
  if (!stdout.trim()) {
    throw new Error(`claude_empty_output`);
  }
  try {
    const parsed = JSON.parse(stdout);
    // Surface what the brain ACTUALLY said. A wake logged as `narrate` is indistinguishable from a
    // wake whose answer the parser could not read — and we spent a full day unable to tell those
    // two apart. stderr only; stdout is the loop's JSON contract.
    const said = typeof parsed?.result === 'string' ? parsed.result : JSON.stringify(parsed);
    process.stderr.write(`[brain] claude-p said: ${said.slice(0, 300).replace(/\n/g, ' ')}\n`);
    return parsed;
  } catch {
    throw new Error(`claude_invalid_json: ${stdout.slice(0, 200)}`);
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * CLAUDE-P-1: reads the CLIProxyAPI key file (same file gig-cli.sh reads at $HOME/.cli-proxy-api-key)
 * if present. Never logs the key contents. Returns null (never throws) when the file is absent or
 * unreadable, so the caller silently keeps the subscription-OAuth path when no proxy key exists.
 *
 * @param {string|undefined} home
 * @returns {string|null}
 */
export function readCliProxyKey(home) {
  if (!home) return null;
  try {
    const key = fs.readFileSync(`${home}/.cli-proxy-api-key`, 'utf8').trim();
    return key || null;
  } catch {
    return null;
  }
}

function httpPost(url, body) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const lib = parsed.protocol === 'https:' ? https : http;
    const options = {
      hostname: parsed.hostname,
      port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
      path: parsed.pathname + parsed.search,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization': `Bearer ${process.env.OPENAI_API_KEY || 'x402-local'}`,
      },
      timeout: 30000,
    };
    const req = lib.request(options, (res) => {
      let data = '';
      res.on('data', c => { data += c; });
      res.on('end', () => {
        // #F1 (2026-07-06): a non-2xx here is a real upstream failure (observed live:
        // sol.blockrun.ai's free-model NVIDIA passthrough intermittently 403/429s —
        // transient: retrying the identical request succeeds within a few attempts).
        // Resolving unconditionally let that error BODY get JSON.parse'd and returned
        // as if it were a real completion, so the retry loop above never fired and
        // every failure silently surfaced as a harmless "narrate" wake instead of
        // wake_error (confirmed live 2026-07-07: a real 429 body parses as valid JSON
        // with no `choices` array, so parseToolCall sees null and index.mjs treats it
        // as text-only). Reject instead so the existing attempt/back-off loop actually
        // retries transient gateway errors.
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 300)}`));
          return;
        }
        resolve(data);
      });
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('HTTP timeout')); });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

function delay(ms) {
  return new Promise(r => setTimeout(r, ms));
}
