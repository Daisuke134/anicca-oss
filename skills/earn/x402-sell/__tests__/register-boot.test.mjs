// node:test — x402scan registration must work from an ANICCA_HOME runtime copy whose
// node_modules are intentionally excluded by the skill sync.
import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);
const REGISTER_BOOT = new URL("../register-x402scan-boot.sh", import.meta.url).pathname;

async function sellerDir(root, { withDeps, bootScript, marker }) {
  const dir = path.join(root, "skills", "earn", "x402-sell");
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, "register-x402scan.mjs"), `console.log(${JSON.stringify(marker)});\n`);
  if (withDeps) {
    const pkg = path.join(dir, "node_modules", "@x402", "extensions");
    await fs.mkdir(pkg, { recursive: true });
    await fs.writeFile(path.join(pkg, "package.json"), '{"name":"@x402/extensions"}');
  }
  if (bootScript) {
    const dest = path.join(dir, "register-x402scan-boot.sh");
    await fs.copyFile(REGISTER_BOOT, dest);
    await fs.chmod(dest, 0o755);
  }
  return dir;
}

const bootEnv = (extra) => ({
  PATH: process.env.PATH,
  HOME: extra.HOME,
  ...extra,
});

test("registration fails closed instead of falling back to another checkout", async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "x402-register-boot-"));
  const home = path.join(tmp, "home");
  const repo = path.join(tmp, "repo");
  const runtime = await sellerDir(home, { withDeps: false, bootScript: true, marker: "RUNTIME" });
  await sellerDir(repo, { withDeps: true, bootScript: false, marker: "REPO" });

  await assert.rejects(
    run(path.join(runtime, "register-x402scan-boot.sh"), [], {
      env: bootEnv({ HOME: home, ANICCA_REPO: repo }),
    }),
    (error) => error.code === 78 && /locked x402 dependencies missing/.test(error.stderr),
  );
});

test("registration prefers the runtime copy when its dependencies exist", async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "x402-register-boot-"));
  const home = path.join(tmp, "home");
  const repo = path.join(tmp, "repo");
  const runtime = await sellerDir(home, { withDeps: true, bootScript: true, marker: "RUNTIME" });
  await sellerDir(repo, { withDeps: true, bootScript: false, marker: "REPO" });

  const { stdout } = await run(path.join(runtime, "register-x402scan-boot.sh"), [], {
    env: bootEnv({ HOME: home, ANICCA_REPO: repo }),
  });

  assert.equal(stdout.trim(), "RUNTIME");
});
