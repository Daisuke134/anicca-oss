// Anicca compute self-pay proxy — OpenAI-compatible on :18402 by default.
// Every inference is paid in USDC via x402 from THIS Anicca's own wallet (no human key).
// br.post settles the x402 payment and returns the OpenAI-shaped completion. Pass a concrete model
// id (e.g. anthropic/claude-sonnet-4-6 for frontier). ClawRouter profile auto-routing (premium/auto)
// needs the full routing config and is wired separately; for now the loop pins a model id.
import http from "http";
import fs from "fs";
import { BlockrunClient } from "@blockrun/llm";
import { loadEvmKey } from "../../skills/earn/lib/resolve-identity.mjs";
import { normalizeRequestBody } from "./model-map.mjs";
// #28: compute-pay with THIS instance's own gated per-instance key — never a borrowed legacy key.
const pk = loadEvmKey();
if (pk) process.env.BASE_CHAIN_WALLET_KEY = pk;
const br = new BlockrunClient();
const PORT = process.env.COMPUTE_PROXY_PORT || 18402;
const HOST = "127.0.0.1";
// Strip any ClawRouter profile prefix/word the caller might send; map to a concrete frontier id.
const FRONTIER = process.env.ANICCA_FRONTIER_MODEL || "anthropic/claude-sonnet-4-6";
const server = http.createServer((req, res) => {
  if (req.method === "POST" && req.url.includes("/chat/completions")) {
    let raw = ""; req.on("data", (c) => (raw += c));
    req.on("end", async () => {
      try {
        const body = normalizeRequestBody(JSON.parse(raw), FRONTIER);
        const out = await br.post("/v1/chat/completions", body);
        res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(out));
      } catch (e) { res.writeHead(502, { "Content-Type": "application/json" }); res.end(JSON.stringify({ error: { message: String(e?.message || e) } })); }
    });
  } else if (req.url.includes("/models")) {
    res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify({ object: "list", data: [] }));
  } else { res.writeHead(404); res.end(); }
});
server.listen(PORT, HOST, () => console.log(`anicca compute-proxy on ${HOST}:${PORT} — x402 self-pay (frontier=${FRONTIER})`));
