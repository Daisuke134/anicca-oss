import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { agentMailSemanticDir } from "./paths.ts";

export type SemanticDecision = { action: "reply" | "ignore"; reply: string | null };

export function semanticDecision(loopId: string, prompt: string): SemanticDecision {
  const runner = process.env.AGENT_RUNNER_BIN
    ?? fileURLToPath(new URL("../agent-runner/agent_runner.py", import.meta.url));
  const schema = fileURLToPath(new URL("./semantic-reply.schema.json", import.meta.url));
  const evidenceRoot = agentMailSemanticDir;
  const evidence = join(evidenceRoot, `${Date.now()}-${process.pid}`);
  const run = spawnSync("/usr/bin/python3", [
    runner, "--task-class", "reply-semantic-agent", "--prompt-stdin",
    "--schema", schema, "--evidence-dir", evidence,
    "--task-label", `${loopId}-decision`, "--loop", loopId,
    "--workdir", fileURLToPath(new URL("../..", import.meta.url)), "--read-only",
  ], { input: prompt, encoding: "utf8", timeout: 150_000 });
  if (run.status !== 0) {
    throw new Error(`semantic runner failed (${run.status}): ${run.stderr || run.stdout}`);
  }
  const summary = JSON.parse(readFileSync(join(evidence, "summary.json"), "utf8"));
  if (typeof summary.result_path !== "string") throw new Error("semantic result missing");
  const value = JSON.parse(readFileSync(summary.result_path, "utf8"));
  if (!(["reply", "ignore"].includes(value.action))
      || !(typeof value.reply === "string" || value.reply === null)) {
    throw new Error("semantic result invalid");
  }
  return value as SemanticDecision;
}
