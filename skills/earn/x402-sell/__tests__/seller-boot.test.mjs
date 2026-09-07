// node:test — seller-boot.sh dependency resolution.
//
// WHY this exists: a seller must start from the immutable repository directory that owns its locked
// dependencies. A copied source-only tree previously died instantly:
//   Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@coinbase/x402' imported from
//   <source-only-copy>/skills/earn/x402-sell/serve.mjs
// The canonical repo/release copy has both source and dependencies.
//
// The boot script is copied into each fixture rather than run in place, because its resolution starts
// from $0 — running the real path would resolve against the real repo and assert nothing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);
const SELLER_BOOT = new URL("../seller-boot.sh", import.meta.url).pathname;

// A stub serve.mjs that reports WHICH copy got exec'd. The real serve.mjs would bind a port.
const marker = (name) => `console.log(${JSON.stringify(name)});\n`;

async function sellerDir(root, { withDeps, bootScript }) {
  const dir = path.join(root, "skills", "earn", "x402-sell");
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, "serve.mjs"), marker(withDeps ? "REPO" : "HOME"));
  if (withDeps) {
    const pkg = path.join(dir, "node_modules", "@coinbase", "x402");
    await fs.mkdir(pkg, { recursive: true });
    await fs.writeFile(path.join(pkg, "package.json"), '{"name":"@coinbase/x402"}');
  }
  if (bootScript) {
    const dest = path.join(dir, "seller-boot.sh");
    await fs.copyFile(SELLER_BOOT, dest);
    await fs.chmod(dest, 0o755);
  }
  return dir;
}

// Full env replacement — execFile's `env` replaces the child's entire environment, so nothing leaks
// in from this test process. LIFE_MANAGER_ENV_FILE points at /dev/null because this fixture needs no
// provider credentials.
const bootEnv = (extra) => ({
  PATH: process.env.PATH,
  HOME: extra.HOME,
  LIFE_MANAGER_ENV_FILE: "/dev/null",
  X402_PAYTO: "0x0000000000000000000000000000000000000001",
  X402_PORT: "0",
  ...extra,
});

test("fails closed instead of falling back to another checkout", async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "seller-boot-"));
  const home = path.join(tmp, "home");
  const repo = path.join(tmp, "repo");
  const homeSeller = await sellerDir(home, { withDeps: false, bootScript: true });
  await sellerDir(repo, { withDeps: true, bootScript: false });

  await assert.rejects(
    run(path.join(homeSeller, "seller-boot.sh"), [], {
      env: bootEnv({ HOME: home, ANICCA_REPO: repo }),
    }),
    (error) => error.code === 78 && /locked x402 dependencies missing/.test(error.stderr),
  );
});

test("prefers its own directory when the dependencies are present there", async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "seller-boot-"));
  const home = path.join(tmp, "home");
  const repo = path.join(tmp, "repo");
  // Both have deps; the local one wins. Marker says HOME because withDeps only picks the string.
  const homeSeller = await sellerDir(home, { withDeps: false, bootScript: true });
  const localPkg = path.join(homeSeller, "node_modules", "@coinbase", "x402");
  await fs.mkdir(localPkg, { recursive: true });
  await fs.writeFile(path.join(localPkg, "package.json"), '{"name":"@coinbase/x402"}');
  await sellerDir(repo, { withDeps: true, bootScript: false });

  const { stdout } = await run(path.join(homeSeller, "seller-boot.sh"), [], {
    env: bootEnv({ HOME: home, ANICCA_REPO: repo }),
  });

  assert.equal(stdout.trim(), "HOME", "a self-sufficient instance dir must not be redirected");
});
