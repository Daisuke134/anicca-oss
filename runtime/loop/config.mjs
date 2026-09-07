/**
 * config.mjs — Pure: loadConfig(processEnv, dotenvText) → Config
 *
 * REQ-009: Config via env / .env file.
 * - Minimal dotenv parser (KEY=VALUE lines; # comments; blank lines skipped; malformed lines skipped)
 * - Process env wins over .env values (never overwrite)
 * - All paths derive from ANICCA_HOME (never hard-coded literals)
 * - Returns a Config object with all known knobs, typed where relevant
 *
 * PROP-011, PROP-012
 */

const DEFAULTS = {
  OPENAI_BASE_URL:      'http://127.0.0.1:8402/v1',
  // FREE brain = nvidia/gpt-oss-120b — BlockRun's documented DEFAULT free model (README: "default free
  // model", 128K), OpenAI's open 120B. Verified live via the raw-BlockRun proxy: clean agentic tool-use
  // (valid JSON tool call, 1676ms, resp model matches). $0 forever — the cost-free brain so prepared
  // skills (yield/invest/x402) + a free model = pure profit. The `nvidia/` prefix works on the raw API
  // directly (the `free/` prefix would need ClawRouter; not required since nvidia/gpt-oss-120b works).
  // ALL tiers use the $0 free model so Anicca NEVER burns compute on routine wakes (cost-free thesis:
  // free model + prepared skills = pure profit). Paid frontier was draining the treasury on the funded
  // tier ($14->$10.5) while yield earned ~$0 — net-negative. A free brain + earning skills = the only
  // way a self-paying agent stays net-positive at small capital. (Frontier is for capable instances on
  // flat-rate billing, e.g. Claude on a subscription — NOT a self-paying Anicca burning x402 per wake.)
  // Model = free/glm-4.7 — the best FREE tool-caller, chosen on evidence I verified myself (not a guess
  // or a subagent claim), 4 independent checks 2026-06-21:
  //  1. Berkeley BFCL leaderboard (the function-calling standard): GLM-4.6 (FC thinking) = #4 overall
  //     (72.38, MIT) — top open/free tool-caller, above DeepSeek-V3.2 (#14) / Qwen3-235B (#23) /
  //     Mistral-Large (#46) / Llama-4-Maverick (#50). glm-4.7 is its successor.
  //  2. ClawRouter src/models.ts: `free/glm-4.7` is a REAL listed model (only `nvidia/glm-4.7` aliases
  //     to the weak free/seed-oss-36b — we use the `free/` id, which is the real GLM).
  //  3. Real wallet check: a glm-4.7 call left the treasury USDC unchanged → genuinely $0 (free tier).
  //  4. Live tool-call: glm-4.7 emits {slot:"earn",args:{strategy:...}} — it actually decides.
  // Corrections to earlier mistakes: nemotron-ultra is reasoning-strong but NOT a top tool-caller (wrong
  // for this loop); deepseek-v4-pro is DELISTED (models.ts redirects it to free/deepseek-v4-flash) — so
  // the real failover is free/deepseek-v4-flash, NOT v4-pro. Do NOT use 'auto' (routes to PAID, drains wallet).
  // PREMIUM EXPERIMENT (Dais 2026-06-21): all tools work (no system error) + anicca actively picks
  // x402_sell/hl/cook on free glm-4.7, yet realised earn = $0. Per Dais's rule (tools work + still not
  // earning → test intelligence), switch to a premium model. PAID via x402 from the wallet's liquid USDC,
  // so a compute buffer must stay liquid. The free→premium earnings delta IS the article. Revert to
  // free/glm-4.7 if premium also earns $0 (then the constraint is demand/capital, not intelligence).
  // PREMIUM EXPERIMENT RESULT (Dais 2026-06-21): reverted. gpt-5.4 over ~30 min realised $0, looped on
  // `cook` (explore) instead of acting, AND burned ~$0.17 compute in 15 min (~$0.68/hr) from the wallet
  // → net NEGATIVE (money waste with zero earn), buffer near-empty. Conclusion: at $13 capital the
  // blocker is NOT model intelligence (free glm decides fine + costs $0) — it's demand (x402 has no
  // buyers) + capital (yield = pennies). Premium made it WORSE. Back to free/glm-4.7 ($0 compute).
  ANICCA_FREE_MODEL:    'free/glm-4.7',
  ANICCA_LEAN_MODEL:    'free/glm-4.7',
  ANICCA_FUNDED_MODEL:  'free/glm-4.7',
  SLEEP_BASE_S:         120,
  SLEEP_ERROR_S:        60,
  SLEEP_LOOP_DETECT_S:  300,
  // Cap for the escalating loop_detect cooldown (index.mjs doubles SLEEP_LOOP_DETECT_S per consecutive
  // same-slot re-offense; §24 adversary #4 — hl_trade thrashed ~25x/session against a flat 300s sleep).
  SLEEP_LOOP_DETECT_MAX_S: 3600,
  SKILL_TIMEOUT_S:      120,
  LOOP_DETECT_WINDOW:   3,
  BALANCE_CACHE_TTL_S:  300,
  LEAN_TIER_THRESHOLD:  1.00,
  ANICCA_BRAIN:         'proxy',
  ANICCA_BRAIN_MODEL:   'claude-sonnet-4-6',
};

const INTEGER_KEYS = new Set([
  'SLEEP_BASE_S', 'SLEEP_ERROR_S', 'SLEEP_LOOP_DETECT_S', 'SLEEP_LOOP_DETECT_MAX_S',
  'SKILL_TIMEOUT_S', 'LOOP_DETECT_WINDOW', 'BALANCE_CACHE_TTL_S',
]);

const FLOAT_KEYS = new Set(['LEAN_TIER_THRESHOLD']);

/**
 * Parse a minimal dotenv text.
 * - Lines starting with # are comments
 * - Blank lines are skipped
 * - KEY=VALUE format only; malformed lines are silently skipped
 *
 * Exported (not just internal to loadConfig) so config-delivery-verification's config-drift.mjs can
 * reuse this EXACT parsing logic when it needs to read $ANICCA_HOME/.env as an additional declared-
 * config source, rather than re-implementing dotenv parsing a second time (FIND-003, iteration-2).
 *
 * @param {string} dotenvText
 * @returns {Record<string, string>}
 */
export function parseDotenv(dotenvText) {
  if (!dotenvText || typeof dotenvText !== 'string') return {};
  const result = {};
  for (const rawLine of dotenvText.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eqIdx = line.indexOf('=');
    if (eqIdx <= 0) continue; // malformed: no key or no `=`
    const key = line.slice(0, eqIdx).trim();
    if (!key || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue; // invalid key syntax
    const value = line.slice(eqIdx + 1); // value may contain = itself
    result[key] = value;
  }
  return result;
}

/**
 * Build the final config object.
 * Precedence: processEnv > dotenv > DEFAULTS
 *
 * @param {Record<string, string|undefined>} processEnv - typically process.env
 * @param {string} [dotenvText] - contents of $ANICCA_HOME/.env (may be undefined/empty)
 * @returns {Record<string, string|number|undefined>}
 */
export function loadConfig(processEnv, dotenvText) {
  const dotenvValues = parseDotenv(dotenvText || '');
  const config = {};

  // Merge all known keys with precedence: processEnv > dotenvValues > DEFAULTS
  for (const [key, defaultValue] of Object.entries(DEFAULTS)) {
    const raw = processEnv[key] ?? dotenvValues[key] ?? String(defaultValue);
    config[key] = coerce(key, raw);
  }

  // Pass through ANICCA_HOME (required; no default)
  if (processEnv.ANICCA_HOME) {
    config.ANICCA_HOME = processEnv.ANICCA_HOME;
  } else if (dotenvValues.ANICCA_HOME) {
    config.ANICCA_HOME = dotenvValues.ANICCA_HOME;
  } else {
    config.ANICCA_HOME = undefined;
  }

  // Pass through any other env vars the caller may need
  config.ANICCA_WALLET_ADDRESS = processEnv.ANICCA_WALLET_ADDRESS
    ?? dotenvValues.ANICCA_WALLET_ADDRESS
    ?? undefined;

  config.ANICCA_BALANCE_OVERRIDE = processEnv.ANICCA_BALANCE_OVERRIDE
    ?? dotenvValues.ANICCA_BALANCE_OVERRIDE
    ?? undefined;

  config.ANICCA_EARN_SKILL = processEnv.ANICCA_EARN_SKILL
    ?? dotenvValues.ANICCA_EARN_SKILL
    ?? undefined;

  config.EARN_LEDGER = processEnv.EARN_LEDGER
    ?? dotenvValues.EARN_LEDGER
    ?? undefined;

  config.LIFE_MANAGER_SKILLS_ROOT = processEnv.LIFE_MANAGER_SKILLS_ROOT
    ?? dotenvValues.LIFE_MANAGER_SKILLS_ROOT
    ?? undefined;

  config.EARN_STATE_ROOT = processEnv.EARN_STATE_ROOT
    ?? dotenvValues.EARN_STATE_ROOT
    ?? undefined;

  config.LIFE_MANAGER_SKILLS_STATE_ROOT = processEnv.LIFE_MANAGER_SKILLS_STATE_ROOT
    ?? dotenvValues.LIFE_MANAGER_SKILLS_STATE_ROOT
    ?? undefined;

  config.CLAUDE_BIN = processEnv.CLAUDE_BIN
    ?? dotenvValues.CLAUDE_BIN
    ?? undefined;

  return config;
}

function coerce(key, raw) {
  if (raw == null) return undefined;
  const str = String(raw);
  if (INTEGER_KEYS.has(key)) {
    const n = parseInt(str, 10);
    return Number.isNaN(n) ? DEFAULTS[key] : n;
  }
  if (FLOAT_KEYS.has(key)) {
    const n = parseFloat(str);
    return Number.isNaN(n) ? DEFAULTS[key] : n;
  }
  return str;
}
