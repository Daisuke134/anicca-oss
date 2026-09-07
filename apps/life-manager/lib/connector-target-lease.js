"use strict";

const { createBrowserTargetLease } = require("../../../runtime/browser/target-lease.cjs");
const { CONNECTOR_CDP_WEBSOCKET_ORIGIN } = require("./connector-browser-target-controller.js");

function unavailable(message) {
  throw new Error(message || "Connector target lease unavailable");
}

function pageWebsocket(value, expectedTargetId) {
  const text = String(value || "");
  let parsed;
  try { parsed = new URL(text); } catch { unavailable("Connector page websocket invalid"); }
  if (
    parsed.protocol !== "ws:"
    || parsed.origin !== CONNECTOR_CDP_WEBSOCKET_ORIGIN
    || parsed.pathname !== `/devtools/page/${expectedTargetId}`
    || parsed.username || parsed.password || parsed.search || parsed.hash
  ) unavailable("Connector page websocket invalid");
  return text;
}

function canonicalUrl(value) {
  let parsed;
  try { parsed = new URL(String(value || "")); } catch { unavailable("Connector canonical URL invalid"); }
  const providerHost = ["luma.com", "lu.ma", "connpass.com"].includes(parsed.hostname)
    || parsed.hostname.endsWith(".connpass.com");
  if (
    parsed.protocol !== "https:"
    || !providerHost
    || parsed.username || parsed.password || parsed.hash
  ) unavailable("Connector canonical URL invalid");
  parsed.hash = "";
  return parsed.toString();
}

function createConnectorTargetLease(options = {}) {
  return createBrowserTargetLease({
    ...options,
    validatePageWebsocket: pageWebsocket,
    validateCanonicalUrl: canonicalUrl,
  });
}

module.exports = { createConnectorTargetLease };
