// lib/transport/calendar-gog.js — LOCAL calendar transport (#74 slice4). BYOK: drives the user's own
// `gog` CLI (gog 0.17.0) against their own Google account/keychain — no Composio. Same adapter interface
// as calendar-composio.js, so the SAME life-logic runs locally with LIFE_TRANSPORT=gog. `run` is
// injectable (execFileSync by default) for unit tests. uid is ignored — gog acts on the configured account.
"use strict";

const { execFileSync } = require("node:child_process");
const { canonicalEventUrl } = require("../canonical-event-url.js");

function isoZ(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

// argv-injection guard: a value starting with "-" could be parsed by gog as a FLAG, not data.
// Positionals (calendarId, eventId) never legitimately start with "-" → reject. Option values are
// passed as a single glued `--flag=value` token (see opt()) so a leading "-" can never smuggle a flag.
const isFlaglike = (s) => /^-/.test(String(s == null ? "" : s));
const opt = (flag, value) => `${flag}=${value}`;
const CONNECTOR_KEY = "lm_connector_event";

function connectorInvalid() { throw new Error("Connector calendar invalid"); }
function connectorUnavailable() { throw new Error("Connector calendar unavailable"); }

function connectorInstant(value) {
  const text = String(value == null ? "" : value).trim();
  if (!Number.isFinite(Date.parse(text)) || !/[zZ]|[+-]\d\d:\d\d$/.test(text)) connectorInvalid();
  return text;
}

function connectorText(value, max) {
  const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  if (!text || text.length > max || /[\x00-\x1f\x7f]/.test(text)) connectorInvalid();
  return text;
}

function connectorCalendarId(value) {
  const text = connectorText(value, 1_024);
  if (isFlaglike(text)) connectorInvalid();
  return text;
}

function connectorIdempotency(value) {
  const text = String(value == null ? "" : value).trim();
  if (!/^[0-9a-f]{64}$/.test(text)) connectorInvalid();
  return text;
}

function connectorCanonicalUrl(value) {
  const raw = String(value == null ? "" : value);
  const url = canonicalEventUrl(value);
  if (!url) connectorInvalid();
  const parsed = new URL(url);
  const host = parsed.hostname.toLowerCase();
  if (["luma.com", "www.luma.com", "lu.ma"].includes(host)) return Object.freeze({ url, sourceTitle: "Luma" });
  const connpassMatch = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?connpass\.com$/i.test(host)
    && /^\/event\/([1-9][0-9]*)\/$/.exec(parsed.pathname);
  if (connpassMatch) {
    const expected = `https://${host}/event/${connpassMatch[1]}/`;
    if (raw !== expected || url !== expected) connectorInvalid();
    return Object.freeze({ url: expected, sourceTitle: "Connpass" });
  }
  const meetupMatch = host === "www.meetup.com"
    && /^\/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\/events\/([1-9][0-9]*)\/$/.exec(parsed.pathname);
  if (meetupMatch) {
    const expected = `https://www.meetup.com/${meetupMatch[1]}/events/${meetupMatch[2]}/`;
    if (raw !== expected || url !== expected) connectorInvalid();
    return Object.freeze({ url: expected, sourceTitle: "Meetup" });
  }
  const doorkeeperMatch = /^(?!www\.doorkeeper\.jp$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.doorkeeper\.jp$/.test(host)
    && /^\/events\/([1-9][0-9]*)$/.exec(parsed.pathname);
  if (doorkeeperMatch) {
    const expected = `https://${host}/events/${doorkeeperMatch[1]}`;
    if (raw !== expected || url !== expected) connectorInvalid();
    return Object.freeze({ url: expected, sourceTitle: "Doorkeeper" });
  }
  const eventbriteMatch = host === "www.eventbrite.com"
    && /^\/e\/(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)-tickets-[1-9][0-9]*|[1-9][0-9]*)$/i.exec(parsed.pathname);
  if (eventbriteMatch) {
    const expected = `https://www.eventbrite.com${parsed.pathname}`;
    if (raw !== expected || url !== expected) connectorInvalid();
    return Object.freeze({ url: expected, sourceTitle: "Eventbrite" });
  }
  const techPlayMatch = host === "techplay.jp"
    && /^\/event\/([1-9][0-9]*)$/.exec(parsed.pathname);
  if (techPlayMatch) {
    const expected = `https://techplay.jp/event/${techPlayMatch[1]}`;
    if (raw !== expected || url !== expected) connectorInvalid();
    return Object.freeze({ url: expected, sourceTitle: "TECH PLAY" });
  }
  const kokuchProMatch = host === "www.kokuchpro.com"
    && /^\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]{0,19}))?\/$/.exec(parsed.pathname);
  if (kokuchProMatch) {
    const expected = `https://www.kokuchpro.com/event/${kokuchProMatch[1]}/${kokuchProMatch[2] ? `${kokuchProMatch[2]}/` : ""}`;
    if (raw !== expected || url !== expected) connectorInvalid();
    return Object.freeze({ url: expected, sourceTitle: "KokuchPro" });
  }
  const match = host === "peatix.com" && /^\/event\/([1-9][0-9]*)$/.exec(parsed.pathname);
  const expected = match ? `https://peatix.com/event/${match[1]}` : "";
  if (!expected || raw !== expected || url !== expected) connectorInvalid();
  return Object.freeze({ url: expected, sourceTitle: "Peatix" });
}

function googleCalendarEventUrl(url) {
  return url.protocol === "https:"
    && !url.username
    && !url.password
    && ["calendar.google.com", "www.google.com"].includes(url.hostname)
    && url.pathname === "/calendar/event"
    && Boolean(url.searchParams.get("eid"));
}

function connectorProviderReceipt(value) {
  const source = value && value.event && typeof value.event === "object" ? value.event : value;
  const id = source && connectorText(source.id, 1_024);
  let link;
  try { link = new URL(connectorText(source && source.htmlLink, 2_000)); } catch { connectorUnavailable(); }
  if (!id || isFlaglike(id) || !googleCalendarEventUrl(link)) connectorUnavailable();
  return Object.freeze({ id, htmlLink: link.toString() });
}

function makeGogCalendar({ bin, account, keyring, calId = "primary", run } = {}) {
  const gogBin = bin || process.env.GOG_BIN || "gog";
  const acct = account !== undefined ? account : (process.env.GOG_ACCOUNT || "");
  const keyringPwd = keyring != null ? keyring : (process.env.GOG_KEYRING_PASSWORD || "");
  // every gog call appends --account; gog flags are order-independent (verified gog 0.17.0).
  const exec = run || ((args, timeout = 60000) =>
    execFileSync(gogBin, [...args, "--account", acct], {
      env: { ...process.env, GOG_KEYRING_PASSWORD: keyringPwd, GOG_ACCOUNT: acct },
      encoding: "utf8", timeout,
    }));

  return {
    kind: "gog",
    ready: () => !!acct,
    async listCalendarsRaw({ strict } = {}) {
      if (!acct) {
        if (strict) throw new Error("calendar transport not ready (missing gog account)");
        return [];
      }
      try {
        const data = JSON.parse(exec(["calendar", "calendars", "-j", "--all", "--no-input"]));
        return Array.isArray(data) ? data : (data.calendars || data.items || []);
      } catch (error) {
        if (strict) throw error;
        return [];
      }
    },
    async listAllEventsRaw(_uid, { timeMin, timeMax, maxResults, strict } = {}) {
      if (!acct) {
        if (strict) throw new Error("calendar transport not ready (missing gog account)");
        return [];
      }
      const args = [
        "calendar", "events", "--all", "-j", "--all-pages", "--no-input",
        opt("--max", String(maxResults || 2500)),
      ];
      if (timeMin) args.push(opt("--from", timeMin));
      if (timeMax) args.push(opt("--to", timeMax));
      try {
        const data = JSON.parse(exec(args));
        return Array.isArray(data) ? data : (data.events || data.items || []);
      } catch (error) {
        if (strict) throw error;
        return [];
      }
    },
    async findConnectorEvents(input = {}) {
      if (!acct) connectorUnavailable();
      const calendarId = connectorCalendarId(input.calendarId);
      const idempotencyValue = connectorIdempotency(input.idempotencyValue);
      const timeMin = connectorInstant(input.timeMin);
      const timeMax = connectorInstant(input.timeMax);
      if (Date.parse(timeMax) <= Date.parse(timeMin)) connectorInvalid();
      let data;
      try {
        data = JSON.parse(exec([
          "calendar", "events", calendarId, "-j", "--all-pages", "--no-input",
          opt("--from", timeMin), opt("--to", timeMax),
          opt("--private-prop-filter", `${CONNECTOR_KEY}=${idempotencyValue}`),
        ]));
      } catch { connectorUnavailable(); }
      const events = Array.isArray(data) ? data : (data.events || data.items || []);
      if (!Array.isArray(events)) connectorUnavailable();
      return Object.freeze(events.map(connectorProviderReceipt));
    },
    async createConnectorEvent(input = {}) {
      if (!acct) connectorUnavailable();
      const calendarId = connectorCalendarId(input.calendarId);
      const idempotencyValue = connectorIdempotency(input.idempotencyValue);
      const title = connectorText(input.title, 500);
      const startAt = connectorInstant(input.startAt);
      const endAt = connectorInstant(input.endAt);
      const location = connectorText(input.location, 2_000);
      const { url: canonicalUrl, sourceTitle } = connectorCanonicalUrl(input.canonicalUrl);
      if (Date.parse(endAt) <= Date.parse(startAt)) connectorInvalid();
      let data;
      try {
        data = JSON.parse(exec([
          "calendar", "create", calendarId, "-j", "--no-input",
          opt("--summary", title), opt("--from", startAt), opt("--to", endAt),
          opt("--location", location), opt("--description", canonicalUrl),
          opt("--source-url", canonicalUrl), opt("--source-title", sourceTitle),
          opt("--private-prop", `${CONNECTOR_KEY}=${idempotencyValue}`),
        ], 30_000));
      } catch { connectorUnavailable(); }
      return connectorProviderReceipt(data);
    },
    // Raw Google-Calendar-shaped items for [timeMin, timeMax]. gog --from/--to accept RFC3339 directly.
    // strict (history path): failure throws instead of masquerading as an empty calendar — see
    // calendar-composio.js listEventsRaw for the rationale. Default (wake path) swallows to [].
    async listEventsRaw(_uid, { timeMin, timeMax, maxResults, strict } = {}) {
      if (!acct) {
        if (strict) throw new Error("calendar transport not ready (missing gog account)");
        return [];
      }
      const args = ["calendar", "events", "list", "-j", "--all-pages", opt("--max", String(maxResults || 250))];
      if (timeMin) args.push(opt("--from", timeMin)); // ISO (RFC3339) — never flag-like
      if (timeMax) args.push(opt("--to", timeMax));
      try {
        const d = JSON.parse(exec(args));
        return Array.isArray(d) ? d : (d.events || d.items || []);
      } catch (e) {
        if (strict) throw e;
        return [];
      }
    },
    // Accepts the same arg shape travel.js builds (Composio dialect) and translates to gog flags.
    async createEvent(_uid, args = {}) {
      if (!acct) return { successful: false };
      const cal2 = args.calendar_id || calId;
      if (isFlaglike(cal2)) return { successful: false }; // positional can't start with "-"
      const startMs = Date.parse(/Z$/.test(args.start_datetime || "") ? args.start_datetime : `${args.start_datetime}Z`);
      if (!Number.isFinite(startMs)) return { successful: false };
      const durMs = ((args.event_duration_hour || 0) * 60 + (args.event_duration_minutes || 0)) * 60000;
      const a = ["calendar", "create", cal2, "-j",
        opt("--summary", args.summary || "予定"),
        opt("--from", isoZ(startMs)), opt("--to", isoZ(startMs + durMs))];
      if (args.location) a.push(opt("--location", args.location));
      if (args.description) a.push(opt("--description", args.description));
      try { exec(a, 30000); return { successful: true }; } catch { return { successful: false }; }
    },
    // Accepts {calendar_id, event_id, location, ...} (Composio dialect) → gog calendar update.
    async patchEvent(_uid, args = {}) {
      if (!acct || !args.event_id) return { successful: false };
      const cal2 = args.calendar_id || calId;
      if (isFlaglike(cal2) || isFlaglike(args.event_id)) return { successful: false }; // positionals
      const a = ["calendar", "update", cal2, args.event_id, "-j"];
      if (args.location != null) a.push(opt("--location", args.location));
      if (args.summary != null) a.push(opt("--summary", args.summary));
      try { exec(a, 30000); return { successful: true }; } catch { return { successful: false }; }
    },
  };
}

module.exports = { makeGogCalendar };
