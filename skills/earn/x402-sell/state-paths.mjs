import { homedir } from 'node:os';
import { join } from 'node:path';

export function resolveX402StateDir(env = process.env) {
  return env.X402_STATE_DIR
    || env.LIFE_MANAGER_STATE_ROOT
    || join(env.HOME || homedir(), '.local', 'state', 'life-manager', 'x402-sell');
}

export function resolveThe402ConfigRoot(env = process.env) {
  return env.THE402_CONFIG_ROOT
    || env.ANICCA_HOME
    || join(env.HOME || homedir(), '.anicca');
}

export function resolveThe402PublicOrigin(env = process.env) {
  const origin = String(env.THE402_PUBLIC_URL || '').replace(/\/+$/, '');
  let parsed;
  try {
    parsed = new URL(origin);
  } catch {
    parsed = null;
  }
  if (!parsed
      || parsed.protocol !== 'https:'
      || parsed.origin !== origin
      || parsed.username
      || parsed.password) {
    throw new Error('THE402_PUBLIC_URL must be a public HTTPS origin');
  }
  return origin;
}

export const X402_STATE_DIR = resolveX402StateDir();
