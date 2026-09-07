"use strict";

const os = require("node:os");
const path = require("node:path");
const WebSocket = require("ws");
const { adaptMoneytreeAccounts, adaptMoneytreeTransactions } = require("../lib/cfo-moneytree.js");
const { deriveMoneytreeState, composeMoneytreeRead } = require("../lib/cfo-moneytree-state.js");

const ERROR = "cfo_moneytree_codex_read_failed:unavailable";
const CFO_CWD = path.resolve(__dirname, "..");
const MAX_BUFFER = 2 * 1024 * 1024;
const CALL_TIMEOUT_MS = 120000;
const APP_SERVER = "codex_apps";
const MONEYTREE_TOOL = "moneytree.show-accounts";
const MONEYTREE_TRANSACTIONS_TOOL = "moneytree.show-transactions";

function appServerSocket(env) {
  const explicit = env.CODEX_APP_SERVER_SOCKET;
  if (typeof explicit === "string" && path.isAbsolute(explicit)) return explicit;
  const codexHome = typeof env.CODEX_HOME === "string" && env.CODEX_HOME ? env.CODEX_HOME : path.join(env.HOME || os.homedir(), ".codex");
  return path.join(codexHome, "app-server-control", "app-server-control.sock");
}

function validateAccounts(content) {
  if (!content || typeof content !== "object" || Array.isArray(content) || content.type !== "accounts" || !content.data || typeof content.data !== "object" || Array.isArray(content.data) || !content.data.accountGroups || typeof content.data.accountGroups !== "object" || Array.isArray(content.data.accountGroups)) throw new Error(ERROR);
  return content;
}

function validateTransactions(content) {
  if (!content || typeof content !== "object" || Array.isArray(content) || content.type !== "transactions" || !content.data || typeof content.data !== "object" || Array.isArray(content.data) || !Array.isArray(content.data.transactions) || !Number.isSafeInteger(content.data.totalCount) || content.data.totalCount < content.data.transactions.length) throw new Error(ERROR);
  return content;
}

function categoryName(row) {
  const value = row && row.category_name;
  return typeof value === "string" && value.length > 0 && value.length <= 128 && value.trim() === value ? value : null;
}

function callMoneytreeBundle(options = {}) {
  return new Promise((resolve, reject) => {
    const env = options.env && typeof options.env === "object" ? options.env : process.env;
    const socket = appServerSocket(env);
    if (!path.isAbsolute(socket)) return reject(new Error(ERROR));
    const Client = options.WebSocketImpl || WebSocket;
    let nextId = 1;
    let threadIdValue = null;
    let accountContent = null;
    let settled = false;
    const timer = setTimeout(() => finish(new Error(ERROR)), CALL_TIMEOUT_MS);
    let ws;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { if (ws && typeof ws.close === "function") ws.close(); } catch {}
      if (error) reject(error); else resolve(value);
    };
    const send = (method, params) => {
      const id = nextId++;
      ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      return id;
    };
    try {
      ws = new Client(`ws+unix://${socket}:/rpc`, { perMessageDeflate: false });
      ws.on("open", () => send("initialize", { clientInfo: { name: "life-manager-cfo-hourly", title: "Life Manager CFO hourly loop", version: "1.0.0" }, capabilities: { experimentalApi: true } }));
      ws.on("message", (data) => {
        if (Buffer.byteLength(String(data), "utf8") > MAX_BUFFER) return finish(new Error(ERROR));
        let message;
        try { message = JSON.parse(String(data)); } catch { return finish(new Error(ERROR)); }
        if (message.error) {
          if (message.id === 4 && accountContent) return finish(null, { accounts: accountContent, transactions: null });
          return finish(new Error(ERROR));
        }
        if (message.id === 1) {
          try { ws.send(JSON.stringify({ jsonrpc: "2.0", method: "initialized" })); send("thread/start", { cwd: options.cwd || CFO_CWD, model: "gpt-5.6-luna", approvalPolicy: "never", sandbox: "read-only", ephemeral: true, serviceName: "life-manager-cfo-hourly", config: { features: { apps: true } } }); } catch { finish(new Error(ERROR)); }
        } else if (message.id === 2) {
          const threadId = message.result && message.result.thread && message.result.thread.id;
          if (typeof threadId !== "string" || !threadId) return finish(new Error(ERROR));
          threadIdValue = threadId;
          try { send("mcpServer/tool/call", { threadId, server: APP_SERVER, tool: MONEYTREE_TOOL, arguments: { locale: "ja" } }); } catch { finish(new Error(ERROR)); }
        } else if (message.id === 3) {
          const result = message.result;
          if (!result || result.isError === true) return finish(new Error(ERROR));
          try {
            const accounts = validateAccounts(result.structuredContent || result.structured_content);
            accountContent = accounts;
            if (options.includeTransactions === false) return finish(null, { accounts, transactions: null });
            if (typeof threadIdValue === "string" && threadIdValue) send("mcpServer/tool/call", { threadId: threadIdValue, server: APP_SERVER, tool: MONEYTREE_TRANSACTIONS_TOOL, arguments: { locale: "ja", limit: 20, sort_key: "date", sort_order: "desc" } });
            else return finish(new Error(ERROR));
          } catch { finish(new Error(ERROR)); }
        } else if (message.id === 4) {
          const result = message.result;
          if (!result || result.isError === true) return finish(null, { accounts: accountContent, transactions: null });
          try { finish(null, { accounts: accountContent, transactions: validateTransactions(result.structuredContent || result.structured_content) }); } catch { finish(null, { accounts: accountContent, transactions: null }); }
        }
      });
      ws.on("error", () => finish(new Error(ERROR)));
      ws.on("close", () => finish(new Error(ERROR)));
    } catch { finish(new Error(ERROR)); }
  });
}

function callMoneytreeApp(options = {}) {
  return callMoneytreeBundle({ ...options, includeTransactions: false }).then((value) => value.accounts);
}

function freeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value);
  Object.values(value).forEach((child) => freeze(child, seen));
  return Object.freeze(value);
}

async function readMoneytreeViaCodex(options = {}) {
  try {
    const base = options.env && typeof options.env === "object" ? options.env : process.env;
    const referenceKey = base.LM_UID_SECRET;
    if (typeof referenceKey !== "string" || Buffer.byteLength(referenceKey, "utf8") < 32) throw new Error(ERROR);
    const rawNow = typeof options.now === "function" ? options.now() : (options.now || new Date());
    const observedAt = new Date(rawNow).toISOString();
    const call = options.callAppServer || callMoneytreeApp;
    const content = await call({ env: base, cwd: CFO_CWD });
    const source = adaptMoneytreeAccounts({ accountsJson: JSON.stringify(content), observedAt, referenceKey });
    const state = deriveMoneytreeState({ signal: "interactive_success", observedAt, aggregationAsOf: null, aggregationFreshnessCutoff: null, liabilitiesExposed: false, liabilityCount: null });
    return composeMoneytreeRead({ source, state });
  } catch { throw new Error(ERROR); }
}

async function readMoneytreeBundleViaCodex(options = {}) {
  try {
    const base = options.env && typeof options.env === "object" ? options.env : process.env;
    const referenceKey = base.LM_UID_SECRET;
    if (typeof referenceKey !== "string" || Buffer.byteLength(referenceKey, "utf8") < 32) throw new Error(ERROR);
    const rawNow = typeof options.now === "function" ? options.now() : (options.now || new Date());
    const observedAt = new Date(rawNow).toISOString();
    const call = options.callMoneytreeBundle || callMoneytreeBundle;
    const content = await call({ env: base, cwd: CFO_CWD });
    const accounts = content && content.accounts;
    const transactions = content && content.transactions;
    if (!accounts) throw new Error(ERROR);
    const accountsJson = JSON.stringify(accounts);
    const source = adaptMoneytreeAccounts({ accountsJson, observedAt, referenceKey });
    const state = deriveMoneytreeState({ signal: "interactive_success", observedAt, aggregationAsOf: null, aggregationFreshnessCutoff: null, liabilitiesExposed: false, liabilityCount: null });
    const moneytreeRead = composeMoneytreeRead({ source, state });
    let transactionView = null;
    if (transactions) {
      try {
        const normalized = adaptMoneytreeTransactions({ accountsJson, transactionsJson: JSON.stringify(transactions), observedAt, referenceKey });
        transactionView = freeze(structuredClone({
          schemaVersion: 1,
          sourceId: normalized.sourceId,
          asOf: normalized.asOf,
          pagePartial: normalized.pagePartial,
          categoryCoverage: transactions.data.transactions.length === 0 ? "unavailable" : transactions.data.transactions.every((row) => categoryName(row) !== null) ? "provider_reported" : transactions.data.transactions.some((row) => categoryName(row) !== null) ? "partial" : "unavailable",
          latestBookingDate: normalized.transactions.reduce((latest, row) => latest === null || row.bookingDate > latest ? row.bookingDate : latest, null),
          transactions: normalized.transactions.map(({ bookingDate, amountMinor, flow, verificationStatus }, index) => ({ bookingDate, amountMinor, flow, verificationStatus, category: categoryName(transactions.data.transactions[index]) })),
        }));
      } catch { transactionView = null; }
    }
    return freeze({ moneytreeRead, transactions: transactionView });
  } catch { throw new Error(ERROR); }
}

async function main(options = {}) {
  let envelope;
  try {
    const read = await readMoneytreeViaCodex(options);
    envelope = { ok: true, sourceId: read.source.sourceId, accountCount: read.source.accounts.length, partial: read.state.partial };
  } catch { envelope = { ok: false, sourceId: null, accountCount: 0, partial: true }; }
  const output = `${JSON.stringify(envelope)}\n`;
  if (typeof options.stdout === "function") options.stdout(output); else process.stdout.write(output);
  return envelope.ok ? 0 : 1;
}

if (require.main === module) main().then((exitCode) => { process.exitCode = exitCode; });

module.exports = { callMoneytreeApp, callMoneytreeBundle, readMoneytreeViaCodex, readMoneytreeBundleViaCodex, main };
