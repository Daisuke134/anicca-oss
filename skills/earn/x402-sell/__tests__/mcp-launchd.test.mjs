import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);

const INSTANCES = [
  {
    name: "franklin1",
    home: "$HOME/.blockrun",
    payTo: "0x3EcCAD24794ca298D25378E9902A251322ea8749",
    upstreamPort: "8414",
    mcpPort: "8090",
    funnelPort: "443",
  },
  {
    name: "franklin2",
    home: "$HOME/.franklin2-home/.blockrun",
    payTo: "0xe7747Fd899D8987821Bb4CB3D6aDf22565F87ce9",
    upstreamPort: "8413",
    mcpPort: "8091",
    funnelPort: "10000",
  },
  {
    name: "claude-p",
    home: "$HOME/.anicca-founder",
    payTo: "0x810F6D61F7606dEEE2657d3083E150a222Bc29C5",
    upstreamPort: "8412",
    mcpPort: "8092",
    funnelPort: "8443",
  },
];

for (const instance of INSTANCES) {
  test(`${instance.name} MCP boot pins identity, ports, and funnel path`, () => {
    const bootPath = path.join(ROOT, `mcp-${instance.name}-boot.sh`);
    const boot = fs.readFileSync(bootPath, "utf8");

    assert.match(boot, /source .*runtime-env\.sh/);
    assert.ok(boot.includes(`export ANICCA_HOME="${instance.home}"`));
    assert.match(boot, /unset BLOCKRUN_WALLET_KEY/);
    assert.ok(boot.includes(`export X402_PAYTO="${instance.payTo}"`));
    assert.ok(boot.includes(`export X402_PORT="${instance.upstreamPort}"`));
    assert.ok(boot.includes(`export PORT="${instance.mcpPort}"`));
    if (instance.funnelPort) {
      assert.ok(
        boot.includes(
          `tailscale funnel --bg --https=${instance.funnelPort} --set-path=/mcp http://127.0.0.1:${instance.mcpPort}/mcp`
        )
      );
    }
    assert.match(boot, /exec \/usr\/bin\/env node "\$DIR\/mcp-server\.mjs"/);
  });

}

test("MCP listeners use three distinct ports", () => {
  assert.equal(new Set(INSTANCES.map((instance) => instance.mcpPort)).size, INSTANCES.length);
});

test("clean install includes the pinned MonetizedMCP SDK", () => {
  const packageJson = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  assert.equal(packageJson.dependencies?.["monetizedmcp-sdk"], "0.1.23");
  assert.equal(packageJson.dependencies?.["@modelcontextprotocol/sdk"], "1.29.0");
});
