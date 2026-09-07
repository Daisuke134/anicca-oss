// lib/gig-chat.js — sibling of investment-chat.js for the marketplace ("gig") lanes: Coconala and
// CrowdWorks. Same honesty rules as slash-command.js's header: every reply is built only from data
// this process actually read, and a lifecycle this process could not read renders as "unknown"
// rather than a cheerful default.
//
// STATE READER NOT YET WIRED. This module renders a reply from a snapshot it is HANDED — it does not
// go fetch Coconala/CrowdWorks state itself. Nothing in this repo currently reads that state either
// (there is no getGigState equivalent to server.js's getInvestmentStateStore()), so callers that do
// not inject a reader get { lifecycle: "unknown" } and an honest "could not be read" reply. A future
// slice wires a real reader; when it exists, it must resolve to a snapshot shaped:
//   { lifecycle: "setup_required" | "verification_required" | "active" | "unknown",
//     stats?: { listingCount: number, lastWakeCompleted?: boolean } }
// stats is only meaningful (and only read) when lifecycle is "active".
"use strict";

const { telegramExtra } = require("./investment-chat.js");

const CROWDWORKS_SIGNUP_URL = "https://crowdworks.jp/public/employees/new";
const CROWDWORKS_VERIFICATION_URL = "https://crowdworks.jp/identifications/new";
const COCONALA_SIGNUP_URL = "https://coconala.com/signup";

// No dedicated Coconala identity-verification URL was given to this slice (unlike CrowdWorks, which
// splits signup and 本人確認 into two distinct pages). Rather than invent one, verification_required
// for coconala falls back to the one Coconala URL this module actually knows (see PLATFORMS below),
// and buildGigReply labels that button honestly ("open Coconala") instead of claiming it is a direct
// verification link.
const PLATFORMS = Object.freeze({
  coconala: { label: "ココナラ", signupUrl: COCONALA_SIGNUP_URL, verificationUrl: null },
  crowdworks: { label: "CrowdWorks", signupUrl: CROWDWORKS_SIGNUP_URL, verificationUrl: CROWDWORKS_VERIFICATION_URL },
});

const LIFECYCLES = new Set(["setup_required", "verification_required", "active"]);

function normalizeGigLifecycle(value) {
  if (typeof value !== "string") return "unknown";
  const lifecycle = value.toLowerCase();
  return LIFECYCLES.has(lifecycle) ? lifecycle : "unknown";
}

function validRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function validStats(value) {
  if (!validRecord(value)) return false;
  if (!Number.isInteger(value.listingCount) || value.listingCount < 0) return false;
  return !("lastWakeCompleted" in value) || typeof value.lastWakeCompleted === "boolean";
}

function validGigSnapshot(snapshot) {
  return validRecord(snapshot)
    && normalizeGigLifecycle(snapshot.lifecycle) !== "unknown"
    && (!("stats" in snapshot) || validStats(snapshot.stats));
}

function buildGigReply(platform, snapshot = {}) {
  const config = PLATFORMS[platform];
  if (!config) throw new Error(`Unknown gig platform: ${platform}`);
  if (!snapshot || typeof snapshot !== "object") snapshot = {};
  const lifecycle = normalizeGigLifecycle(snapshot.lifecycle);

  if (lifecycle === "setup_required") {
    return {
      text: [
        config.label, "",
        `${config.label}でアカウントを作成してください。`,
        "作成が完了したらLife Managerが状態を確認し、出品まで進めます。",
      ].join("\n"),
      presentation: { blocks: [{ type: "buttons", buttons: [
        { label: `${config.label}でアカウント作成する`, url: config.signupUrl },
        { label: "今はしない", action: { type: "callback", value: `${platform}:later` } },
      ] }] },
    };
  }

  if (lifecycle === "verification_required") {
    const url = config.verificationUrl || config.signupUrl;
    const buttonLabel = config.verificationUrl
      ? `${config.label}で本人確認を完了する`
      : `${config.label}を開く`;
    return {
      text: [
        config.label, "",
        `${config.label}のアカウントはありますが、本人確認がまだ完了していません。`,
        "本人確認が完了するまで出品・受注は開始しません。",
      ].join("\n"),
      presentation: { blocks: [{ type: "buttons", buttons: [
        { label: buttonLabel, url },
        { label: "今はしない", action: { type: "callback", value: `${platform}:later` } },
      ] }] },
    };
  }

  const lines = [config.label, ""];
  if (lifecycle === "active") {
    const stats = validStats(snapshot.stats) ? snapshot.stats : null;
    if (stats) {
      const wakeLabel = typeof stats.lastWakeCompleted === "boolean"
        ? (stats.lastWakeCompleted ? "完了" : "未完了")
        : "不明";
      lines.push(`稼働中。出品数 ${stats.listingCount} 件、直近のwakeは${wakeLabel}です。`);
    } else {
      lines.push("稼働中ですが、最新の出品数をまだ読み取れていません。");
    }
  } else {
    // unknown — the state could not be read. No cheerful default, no invented count.
    lines.push("状態をまだ確認できません。出品・受注は開始しません。");
  }
  return { text: lines.join("\n") };
}

module.exports = {
  COCONALA_SIGNUP_URL,
  CROWDWORKS_SIGNUP_URL,
  CROWDWORKS_VERIFICATION_URL,
  normalizeGigLifecycle,
  validGigSnapshot,
  buildGigReply,
  telegramExtra,
};
