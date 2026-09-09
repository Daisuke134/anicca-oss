// Durable state-dir resolution for the colony ledger (children.jsonl) + earn ledger.
// Fail-closed: REFUSE any /tmp-rooted path. The 2026-06 self-spawn E2E wrote children.jsonl to
// /tmp/spawn-live-state, which the OS tmp-cleaner deleted — the colony record was lost and the
// verifier could not reproduce it. A child must persist where it survives reboots and tmp sweeps.

function resolveStateDir({ env = process.env, home = process.env.HOME } = {}) {
  const requested = env.ANICCA_STATE_DIR ||
    `${home}/.local/state/life-manager/agent-economy/instance/state`;
  const norm = String(requested);
  // Reject /tmp and /private/tmp (macOS) and anything under them.
  if (/^\/(private\/)?tmp(\/|$)/.test(norm)) {
    throw new Error(
      `self/spawn: refusing non-durable state dir "${norm}" — /tmp is tmp-cleaned and loses the colony ledger. Use the Life Manager state root or /var/lib/anicca.`
    );
  }
  return norm;
}

module.exports = { resolveStateDir };
