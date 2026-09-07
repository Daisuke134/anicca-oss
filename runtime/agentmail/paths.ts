import { homedir } from "node:os";
import { join } from "node:path";

export const agentMailRoot = process.env.AGENTMAIL_STATE_ROOT
  ?? join(homedir(), ".local/state/life-manager/agentmail");
export const agentMailDataDir = join(agentMailRoot, "state");
export const agentMailQueuePath = process.env.AGENTMAIL_QUEUE_PATH
  ?? join(agentMailDataDir, "inbox-queue.jsonl");
export const agentMailDbPath = process.env.AGENTMAIL_DB_PATH
  ?? join(agentMailDataDir, "agentmail.db");
export const agentMailAdapterDir = process.env.AGENTMAIL_ADAPTER_STATE_DIR
  ?? join(agentMailDataDir, "adapter");
export const agentMailSemanticDir = process.env.AGENTMAIL_SEMANTIC_STATE_DIR
  ?? join(agentMailDataDir, "semantic");
