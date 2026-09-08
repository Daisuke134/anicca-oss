"use strict";

const CONNECTOR_CDP_ENDPOINT = "http://127.0.0.1:9222";
const CONNECTOR_CDP_WEBSOCKET_ORIGIN = "ws://127.0.0.1:9222";

function unavailable(message) {
  throw new Error(message || "Connector browser target controller unavailable");
}

function exactTargetId(value) {
  const targetId = String(value || "");
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(targetId)) unavailable("Connector target ID invalid");
  return targetId;
}

function targetIdFromWebsocket(value) {
  let parsed;
  try { parsed = new URL(String(value || "")); } catch { unavailable("Connector page websocket invalid"); }
  const prefix = "/devtools/page/";
  if (
    parsed.protocol !== "ws:"
    || parsed.origin !== CONNECTOR_CDP_WEBSOCKET_ORIGIN
    || !parsed.pathname.startsWith(prefix)
    || parsed.username || parsed.password || parsed.search || parsed.hash
  ) unavailable("Connector page websocket invalid");
  return exactTargetId(parsed.pathname.slice(prefix.length));
}

function createConnectorBrowserTargetController(options = {}) {
  const browser = options.browser;
  const endpoint = String(options.endpoint || CONNECTOR_CDP_ENDPOINT);
  const wait = options.wait || ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  const bindTimeoutMs = options.bindTimeoutMs == null ? 5_000 : options.bindTimeoutMs;
  if (endpoint !== CONNECTOR_CDP_ENDPOINT) unavailable("Connector browser endpoint invalid");
  if (!browser || typeof browser.contexts !== "function" || typeof browser.newBrowserCDPSession !== "function") {
    unavailable();
  }
  const contexts = browser.contexts();
  if (!Array.isArray(contexts) || contexts.length !== 1) unavailable("Connector browser context unavailable");
  const context = contexts[0];
  if (!context || typeof context.pages !== "function" || typeof context.newCDPSession !== "function") {
    unavailable("Connector browser context unavailable");
  }
  if (typeof wait !== "function" || !Number.isInteger(bindTimeoutMs) || bindTimeoutMs < 100 || bindTimeoutMs > 30_000) {
    unavailable();
  }

  async function targetIdForPage(page) {
    const session = await context.newCDPSession(page);
    try {
      const result = await session.send("Target.getTargetInfo");
      return exactTargetId(result && result.targetInfo && result.targetInfo.targetId);
    } finally {
      if (session && typeof session.detach === "function") await session.detach();
    }
  }

  async function findPage(targetId, excludedPages = new Set()) {
    const deadline = Date.now() + bindTimeoutMs;
    do {
      const matches = [];
      const pages = context.pages();
      for (let index = pages.length - 1; index >= 0; index -= 1) {
        const page = pages[index];
        if (excludedPages.has(page)) continue;
        try {
          if (await targetIdForPage(page) === targetId) matches.push(page);
        } catch {
          // A disappearing unrelated page is not the owned target.
        }
      }
      if (matches.length === 1) return matches[0];
      if (matches.length > 1) unavailable("Connector target page binding ambiguous");
      if (Date.now() >= deadline) break;
      await wait(25);
    } while (true);
    unavailable("Connector target page binding unavailable");
  }

  async function browserCall(method, params) {
    const session = await browser.newBrowserCDPSession();
    try {
      return await session.send(method, params);
    } finally {
      if (session && typeof session.detach === "function") await session.detach();
    }
  }

  return Object.freeze({
    async create() {
      const baselinePages = new Set(context.pages());
      const result = await browserCall("Target.createTarget", { url: "about:blank" });
      const targetId = exactTargetId(result && result.targetId);
      try {
        const page = await findPage(targetId, baselinePages);
        return Object.freeze({
          target_id: targetId,
          page_websocket: `${CONNECTOR_CDP_WEBSOCKET_ORIGIN}/devtools/page/${targetId}`,
          page,
        });
      } catch (error) {
        try { await browserCall("Target.closeTarget", { targetId }); } catch {}
        throw error;
      }
    },

    async probe(pageWebsocket) {
      const page = await findPage(targetIdFromWebsocket(pageWebsocket));
      try {
        return await page.evaluate(() => 1) === 1;
      } catch {
        return false;
      }
    },

    async close(value) {
      const targetId = exactTargetId(value);
      const inventory = await browserCall("Target.getTargets");
      if (!inventory || !Array.isArray(inventory.targetInfos)) {
        unavailable("Connector target inventory unavailable");
      }
      const targetIds = inventory.targetInfos.map((target) => exactTargetId(target && target.targetId));
      if (!targetIds.includes(targetId)) return true;
      const result = await browserCall("Target.closeTarget", { targetId });
      return result && result.success === true;
    },
  });
}

module.exports = {
  CONNECTOR_CDP_ENDPOINT,
  CONNECTOR_CDP_WEBSOCKET_ORIGIN,
  createConnectorBrowserTargetController,
};
