"use strict";

const path = require("node:path");
const { canonicalKokuchProBinding } = require("./connector-kokuchpro-workflow.js");

const { createBrowserHarnessAdapter } = require("./connector-browser-harness-adapter.js");
const { runLocalAgentRunner } = require("./connector-luna-judgment.js");

const CONTROL = /^[a-z][a-z0-9_-]{1,63}$/;
const EXTENSION_PROVIDER = /^[a-z][a-z0-9_-]{1,31}$/;
const PAGE_WEBSOCKET = /^ws:\/\/(?:127\.0\.0\.1|\[::1\]):9222\/devtools\/page\/[A-Za-z0-9._-]{3,128}$/;
const TECHPLAY_POSTCHECK_ATTEMPTS = 20;
const TECHPLAY_POSTCHECK_INTERVAL_MS = 25;
const KINDS = new Set(["input", "textarea", "select", "checkbox", "radio", "button", "link"]);
const FILL = new Set(["ax_fill", "dom_fill", "ax_select", "ax_check"]);
const ACTIONS = { input: ["fill", "ax_fill"], textarea: ["fill", "ax_fill"], select: ["fill", "ax_select"], checkbox: ["fill", "ax_check"], radio: ["fill", "ax_check"], button: ["submit", "ax_click"], link: ["submit", "ax_click"] };
const ACTIONABLE_KINDS = new Set(["input", "textarea", "select", "checkbox", "radio"]);
const MUTATING_METHODS = new Set(["ax_fill", "dom_fill", "ax_select", "ax_check", "ax_uncheck", "ax_click", "coordinate_click", "keyboard_submit"]);
const PROVIDERS = new Set(["luma", "connpass", "peatix", "meetup", "doorkeeper", "eventbrite", "techplay"]); const LABEL = { name: /^(?:name|full name|attendee name|氏名|名前|お名前)$/, email: /^(?:email|e-mail|email address|account email|メール|メールアドレス)$/, family: /^(?:family name kana|last name kana|surname kana|lastname_edit|姓（カナ）)$/, given: /^(?:given name kana|first name kana|firstname_edit|名（カナ）)$/, phone: /^(?:phone(?: number)?|telephone|mobile|電話(?:番号)?|携帯)$/, privacy: /^(?:organizer privacy(?: confirmation)?|主催者のプライバシーポリシーに同意する)$/ };
const PEATIX_FORM_SUBMIT_LABEL = "確認画面へ進む";
const PEATIX_CONFIRM_LABEL = "チケットを申し込む";
const PEATIX_FORM_URL = /^https:\/\/peatix\.com\/sales\/event\/([1-9][0-9]*)\/form$/;
const PEATIX_CONFIRM_URL = /^https:\/\/peatix\.com\/sales\/event\/([1-9][0-9]*)\/confirm$/;
const CONNPASS_FINAL_LABEL = "申し込みを確定する";
const CONNPASS_REFERRAL_QUESTION = "このイベントは何を見て知りましたか？";
const CONNPASS_ONLINE_LABEL = /^オンライン視聴枠（YouTube） 無料(?: 参加者数 \d+人)?$/i;
const CONNPASS_JOIN_URL = /^https:\/\/(?:[a-z0-9-]+\.)?connpass\.com\/event\/([1-9][0-9]*)\/join\/$/;
const DOORKEEPER_FINAL_LABEL = "申し込む";
const DOORKEEPER_TRIGGER_CONTROL = /^(?:control_[1-9][0-9]*|(?:doorkeeper_)?(?:modal_)?trigger(?:_[a-z0-9]+)?)$/;
const DOORKEEPER_EVENT_REF = /^doorkeeper-event:\/\/event\/([1-9][0-9]*)$/;
const DOORKEEPER_EVENT_URL = /^https:\/\/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.doorkeeper\.jp\/events\/([1-9][0-9]*)$/;
const EVENTBRITE_EVENT_REF = /^eventbrite-event:\/\/event\/([1-9][0-9]*)$/;
const EVENTBRITE_EVENT_URL = /^https:\/\/www\.eventbrite\.com\/e\/(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)-tickets-([1-9][0-9]*)|([1-9][0-9]*))$/i;
const EVENTBRITE_TRIGGER_CONTROL = /^eventbrite_checkout_([1-9][0-9]*)$/;
const EVENTBRITE_TICKET_REGISTER_CONTROL = /^eventbrite_ticket_register_([1-9][0-9]*)$/;
const EVENTBRITE_ATTENDEE_CONTROL = /^eventbrite_attendee_(first_name|last_name|email|confirm_email)_([1-9][0-9]*)$/;
const EVENTBRITE_ATTENDEE_REGISTER_CONTROL = /^eventbrite_attendee_register_([1-9][0-9]*)$/;
const EVENTBRITE_MARKETING_CONTROL = /^eventbrite_marketing_opt_out_(organization|eventbrite)_([1-9][0-9]*)$/;
const EVENTBRITE_MARKETING_LABELS = Object.freeze({ organization: "Organizer marketing opt-out", eventbrite: "Eventbrite marketing opt-out" });
const EVENTBRITE_ATTENDEE_LABELS = Object.freeze({ first_name: "First name", last_name: "Last name", email: "Email", confirm_email: "Confirm email" });
const EVENTBRITE_CTA_LABELS = new Set(["Get tickets", "Reserve a spot"]);
const TECHPLAY_EVENT_REF = /^techplay-event:\/\/event\/([1-9][0-9]*)$/;
const TECHPLAY_EVENT_URL = /^https:\/\/techplay\.jp\/event\/([1-9][0-9]*)$/;
const TECHPLAY_JOIN_URL = /^https:\/\/techplay\.jp\/event\/join\/([1-9][0-9]*)$/;
const TECHPLAY_CONFIRM_URL = /^https:\/\/techplay\.jp\/event\/join\/([1-9][0-9]*)\/confirm$/;
const TECHPLAY_REVIEW_LABEL = "同意して内容を確認する";
const TECHPLAY_FINAL_LABEL = "申し込みを確定する";
const TECHPLAY_ANSWER_NAME = /^enqueteAnswers\[([1-9][0-9]*)\]$/;
const TECHPLAY_OPT_OUT_ID = /^(?:area_[1-9][0-9]*|tag_[1-9][0-9]*|organizer_[1-9][0-9]*|icon_published|use_as_preset)$/;
const TECHPLAY_QUESTIONS = new Set(["氏名", "メールアドレス", "年齢", "キャリア状況", "所属企業（学校）名", "職種"]);
const EVENTBRITE_ACTIONABLE_ELEMENT_LIMIT = 100;
const EVENTBRITE_ID_SCAN_LIMIT = 256;
const EVENTBRITE_FRAME_TIMEOUT_MS = 30_000;
const EVENTBRITE_FRAME_STABILITY_MS = 500;
const FINAL_EFFECT_TIMEOUT_MS = 30_000;
const FINAL_EFFECT_POLL_MS = 25;
const EVENTBRITE_MARKETING_OPERATION = Symbol("eventbriteMarketingOperation");
const EVENTBRITE_FINAL_OPERATION = Symbol("eventbriteFinalOperation");
const EVENTBRITE_FINAL_ATTEMPTED = Symbol("eventbriteFinalAttempted");
const KOKUCHPRO_ENTRY_SELECTOR = "form, form input, form button";
const KOKUCHPRO_ENTRY_READY_TIMEOUT_MS = 2_000;
const KOKUCHPRO_ENTRY_POLL_INTERVAL_MS = 50;
const KOKUCHPRO_ENTRY_TOKEN = /^kokuchpro_entry_[0-9a-f]{32}(?:_o_[0-9a-z]{1,13})?$/;
const KOKUCHPRO_SEAT_TOKEN = /^kokuchpro_seat_[0-9a-f]{32}(?:_o_[0-9a-z]{1,13})?$/, KOKUCHPRO_FINAL_TOKEN = /^kokuchpro_final_[0-9a-f]{32}(?:_o_[0-9a-z]{1,13})?$/;
const KOKUCHPRO_SPINNER_NAME = /^data\[Entry\]\[seat[1-9][0-9]*\]$/;
const KOKUCHPRO_SPINNER_CLASSES = Object.freeze(["form-control", "spinner", "ui-spinner-input"]);

function invalid() {
  throw new Error("Connector production Browser Harness invalid");
}
function safePageState(page) { try { const url = new URL(String(page && typeof page.url === "function" ? page.url() : "")); return url.origin !== "null" && url.pathname ? `${url.origin}${url.pathname}` : "unavailable"; } catch { return "unavailable"; } }
function candidatePeatixEventId(candidate) {
  const match = /^peatix-event:\/\/event\/([1-9][0-9]*)$/.exec(String(candidate && candidate.event_ref || ""));
  return match ? match[1] : "";
}
function candidateMeetupEventId(candidate) {
  const match = /^meetup-event:\/\/event\/([1-9][0-9]*)$/.exec(String(candidate && candidate.event_ref || ""));
  return match ? match[1] : "";
}
function candidateConnpassEventId(candidate) { const match = /^connpass-event:\/\/event\/([1-9][0-9]*)$/.exec(String(candidate && candidate.event_ref || "")); return match ? match[1] : ""; }
function candidateDoorkeeperBinding(candidate) {
  const ref = DOORKEEPER_EVENT_REF.exec(String(candidate && candidate.event_ref || ""));
  const url = DOORKEEPER_EVENT_URL.exec(String(candidate && candidate.canonical_url || ""));
  return ref && url && url[1] !== "www" && ref[1] === url[2]
    ? Object.freeze({ eventId: ref[1], canonicalUrl: String(candidate.canonical_url) }) : null;
}
function candidateDoorkeeperEventId(candidate) { return candidateDoorkeeperBinding(candidate)?.eventId || ""; }
function candidateEventbriteBinding(candidate) {
  const ref = EVENTBRITE_EVENT_REF.exec(String(candidate && candidate.event_ref || ""));
  const url = EVENTBRITE_EVENT_URL.exec(String(candidate && candidate.canonical_url || ""));
  const eventId = url && (url[1] || url[2]);
  return ref && eventId && ref[1] === eventId
    ? Object.freeze({ eventId: ref[1], canonicalUrl: String(candidate.canonical_url) }) : null;
}
function candidateTechPlayBinding(candidate) {
  if (!candidate || typeof candidate !== "object" || candidate.provider !== "techplay" || typeof candidate.ticket_id !== "string") return null;
  const ref = TECHPLAY_EVENT_REF.exec(String(candidate.event_ref || "")); const url = TECHPLAY_EVENT_URL.exec(String(candidate.canonical_url || ""));
  return ref && url && ref[1] === url[1] && /^[1-9][0-9]*$/.test(candidate.ticket_id)
    ? Object.freeze({ eventId: ref[1], canonicalUrl: String(candidate.canonical_url), ticketId: candidate.ticket_id }) : null;
}
function candidateKokuchProBinding(candidate) {
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate) || candidate.provider !== "kokuchpro"
    || typeof candidate.ticket_id !== "string" || !/^[1-9][0-9]*$/.test(candidate.ticket_id)) return null;
  const binding = canonicalKokuchProBinding(candidate);
  const ref = /^kokuchpro-event:\/\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]{0,19}))?$/.exec(String(binding?.event_ref || ""));
  return ref ? Object.freeze({ eventRef: binding.event_ref, canonicalUrl: binding.canonical_url, eventKey: ref[1], occurrenceId: ref[2] || "", ticketId: candidate.ticket_id }) : null;
}
function kokuchProEntryToken(binding) {
  let suffix = "";
  try { suffix = binding?.occurrenceId ? `_o_${BigInt(binding.occurrenceId).toString(36)}` : ""; } catch { return ""; }
  const token = binding ? `kokuchpro_entry_${binding.eventKey}${suffix}` : "";
  return KOKUCHPRO_ENTRY_TOKEN.test(token) ? token : "";
}
function kokuchProSeatToken(binding) { const token = kokuchProEntryToken(binding).replace("kokuchpro_entry_", "kokuchpro_seat_"); return KOKUCHPRO_SEAT_TOKEN.test(token) ? token : ""; }
function kokuchProFinalToken(binding) { const token = kokuchProEntryToken(binding).replace("kokuchpro_entry_", "kokuchpro_final_"); return KOKUCHPRO_FINAL_TOKEN.test(token) ? token : ""; }
function isEventbriteTriggerMeaning(control) {
  return Boolean(
    control && control.kind === "button" && typeof control.label === "string" && control.label.trim()
    && control.required === false && control.completed === false && control.submittable === true
  );
}
function isEventbriteTriggerSemantic(control) {
  return isEventbriteTriggerMeaning(control)
    && EVENTBRITE_TRIGGER_CONTROL.test(String(control.control || ""))
    && EVENTBRITE_CTA_LABELS.has(control.label);
}
function isEventbriteCheckoutTrigger({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "eventbrite" || !isEventbriteTriggerSemantic(control)) return false;
  const binding = candidateEventbriteBinding(candidate);
  if (!binding || EVENTBRITE_TRIGGER_CONTROL.exec(control.control)[1] !== binding.eventId) return false;
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { href = ""; }
  if (href !== binding.canonicalUrl) return false;
  const matching = Array.isArray(controls) ? controls.filter(isEventbriteTriggerMeaning) : [];
  return matching.length === 1 && matching[0].control === control.control;
}
function eventbriteFrameUrl(frame) {
  let href = "";
  try { href = String(frame && typeof frame.url === "function" ? frame.url() : ""); } catch { return null; }
  try {
    const url = new URL(href);
    if (url.protocol !== "https:" || url.hostname !== "www.eventbrite.com" || url.port || url.username || url.password || url.pathname !== "/checkout-external" || url.hash) return null;
    return Object.freeze({ href, url });
  } catch { return null; }
}
function eventbriteFrameMatches(frame, eventId) {
  const parsed = eventbriteFrameUrl(frame);
  if (!parsed) return false;
  const eventIds = parsed.url.searchParams.getAll("eid");
  return eventIds.length === 1 && eventIds[0] === String(eventId);
}
function eventbriteChildFrame(frame) {
  try { return typeof frame?.parentFrame === "function" && frame.parentFrame() !== null; } catch { return false; }
}
function eventbriteCheckoutFrameSet(page, eventId) {
  let frames = [];
  try { frames = typeof page?.frames === "function" ? page.frames() : []; } catch { frames = []; }
  const official = Array.isArray(frames) ? frames.map((frame) => ({ frame, parsed: eventbriteFrameUrl(frame) })).filter(({ frame, parsed }) => parsed && eventbriteChildFrame(frame)) : [];
  return { official, matching: official.filter(({ frame }) => eventbriteFrameMatches(frame, eventId)) };
}
function eventbriteTicketFrame(page, eventId, canonicalUrl) {
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { return null; }
  if (href !== String(canonicalUrl)) return null;
  const { official, matching } = eventbriteCheckoutFrameSet(page, eventId);
  return official.length === 1 && matching.length === 1 ? matching[0].frame : null;
}
async function inspectEventbriteTicketFrame(frame, eventId) {
  if (!frame || typeof frame.locator !== "function") return null;
  let locator;
  try { locator = frame.locator("[data-testid]"); } catch { return null; }
  if (!locator || typeof locator.evaluateAll !== "function") return null;
  try {
    return await locator.evaluateAll((elements, context) => {
      if (!Array.isArray(elements) || elements.length > 100) return { cardCount: -1, control: null };
      const testIdOf = (element) => String((element.getAttribute && element.getAttribute("data-testid")) || (element.dataset && element.dataset.testid) || "");
      const textOf = (element) => String(element && (element.innerText || element.textContent || element.value) || "").replace(/\s+/g, " ").trim();
      const hiddenStyle = (style) => Boolean(style && (
        [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase()))
        || String(style.opacity ?? "") === "0"
      ));
      const visibleOf = (element) => {
        const view = element && element.ownerDocument && element.ownerDocument.defaultView;
        let current = element;
        while (current) {
          if (current.hidden === true || current.isConnected === false || (typeof current.hasAttribute === "function" && current.hasAttribute("hidden"))) return false;
          if (String((current.getAttribute && current.getAttribute("aria-hidden")) || "").toLowerCase() === "true") return false;
          if (hiddenStyle(current.style || {})) return false;
          let computed = null;
          try { computed = view && typeof view.getComputedStyle === "function" ? view.getComputedStyle(current) : null; } catch { computed = null; }
          if (hiddenStyle(computed)) return false;
          current = current.parentElement || null;
        }
        if (!element || typeof element.getBoundingClientRect !== "function") return false;
        let rect;
        try { rect = element.getBoundingClientRect(); } catch { return false; }
        return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0);
      };
      const descendants = (element) => [element, ...(typeof element.querySelectorAll === "function" ? [...element.querySelectorAll("*")] : [])];
      const cardCandidates = elements.filter((element) => testIdOf(element) === "ticket-display-card-content-full-size");
      const cards = cardCandidates.filter(visibleOf);
      const primaryCandidates = elements.filter((element) => testIdOf(element) === "eds-modal__primary-button");
      const primary = primaryCandidates.filter((element) => {
        const tag = String(element.tagName || "").toLowerCase(); const type = String(element.type || "").toLowerCase();
        return visibleOf(element) && element.disabled !== true && String((element.getAttribute && element.getAttribute("aria-disabled")) || "").toLowerCase() !== "true"
          && tag === "button" && type === "button" && textOf(element) === "Register";
      });
      if (cardCandidates.length !== 1 || cards.length !== 1 || primaryCandidates.length !== 1 || primary.length !== 1) return { cardCount: cards.length, control: null };
      const card = cards[0]; const cardText = textOf(card);
      const stepperCandidates = descendants(card).filter((element) => testIdOf(element) === "eds-stepper");
      const stepper = stepperCandidates.filter(visibleOf);
      const parts = stepper.length === 1 ? descendants(stepper[0]) : [];
      const quantityCandidates = parts.filter((element) => testIdOf(element) === "eds-stepper-quantity");
      const increaseCandidates = parts.filter((element) => testIdOf(element) === "eds-stepper-increase-button");
      const decreaseCandidates = parts.filter((element) => testIdOf(element) === "eds-stepper-decrease-button");
      const prices = descendants(card).filter((element) => testIdOf(element) === "ticket-price__price");
      const quantity = quantityCandidates.filter((element) => visibleOf(element) && textOf(element) === "1");
      const enabledOf = (element) => element.disabled !== true && String((element.getAttribute && element.getAttribute("aria-disabled")) || "").toLowerCase() !== "true";
      const increase = increaseCandidates.filter((element) => visibleOf(element) && String(element.tagName || "").toLowerCase() === "button" && enabledOf(element));
      const decrease = decreaseCandidates.filter((element) => visibleOf(element) && String(element.tagName || "").toLowerCase() === "button");
      const paid = /(?:[$€£¥￥]\s*\d|\b(?:jpy|usd)\s*\d[\d,]*(?:\.\d+)?(?:\b|(?=\s|$))|\b\d[\d,]*(?:\.\d+)?\s*(?:jpy|usd|yen|円)(?![A-Za-z0-9])|\bcash\b|\bpaid\b|\bdoor\s*(?:fee|price)\b|\bat\s+the\s+door\b|\bminimum\s+purchase\b|\bone\s+drink\s+minimum\b|\bpurchase\s+required\b|会場払い|当日払い|有料)/i.test(cardText);
      const free = prices.length === 1 && visibleOf(prices[0]) && textOf(prices[0]) === "Free";
      const valid = stepperCandidates.length === 1 && stepper.length === 1 && quantityCandidates.length === 1 && quantity.length === 1
        && increaseCandidates.length === 1 && increase.length === 1 && decreaseCandidates.length === 1 && decrease.length === 1
        && prices.length === 1 && free && decrease[0].disabled === true && !paid;
      if (valid && primary[0].dataset) primary[0].dataset.lmConnectorControl = `eventbrite_ticket_register_${context.eventId}`;
      return { cardCount: cards.length, control: valid ? `eventbrite_ticket_register_${context.eventId}` : null };
    }, { eventId });
  } catch { return null; }
}
async function inspectEventbriteAttendeeFrame(frame, eventId) {
  if (!frame || typeof frame.locator !== "function") return null;
  let locator; let idLocator;
  try {
    locator = frame.locator("input, textarea, select, button, [data-testid]");
    idLocator = frame.locator("[id]");
  } catch { return null; }
  if (!locator || typeof locator.evaluateAll !== "function" || !idLocator || typeof idLocator.evaluateAll !== "function") return null;
  let idCounts;
  try {
    if (typeof idLocator.count === "function") {
      const idCount = await idLocator.count();
      if (!Number.isSafeInteger(idCount) || idCount < 0 || idCount > EVENTBRITE_ID_SCAN_LIMIT) return [];
    }
    idCounts = await idLocator.evaluateAll((elements, { limit }) => {
      if (!Array.isArray(elements) || elements.length > limit) return null;
      const counts = [];
      for (const element of elements) {
        const id = String((element && element.getAttribute && element.getAttribute("id")) || (element && element.id) || "");
        if (!id) continue;
        const entry = counts.find(([value]) => value === id);
        if (entry) entry[1] += 1;
        else counts.push([id, 1]);
      }
      return counts;
    }, { limit: EVENTBRITE_ID_SCAN_LIMIT });
  } catch { return null; }
  if (!Array.isArray(idCounts)) return [];
  try {
    return await locator.evaluateAll((elements, { eventId: id, idCounts: allIds, actionableLimit }) => {
      if (!Array.isArray(elements) || elements.length > actionableLimit) return [];
      const globalIdCount = (id) => allIds.find(([value]) => value === id)?.[1] || 0;
      const hidden = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style.opacity ?? "") === "0");
      const visibleOf = (element) => {
        const view = element?.ownerDocument?.defaultView;
        for (let current = element; current; current = current.parentElement || null) {
          let computed = null; try { computed = view?.getComputedStyle?.(current); } catch { /* fail closed below */ }
          if (current.hidden === true || current.isConnected === false || (typeof current.hasAttribute === "function" && current.hasAttribute("hidden")) || String(current.getAttribute?.("aria-hidden") || "").toLowerCase() === "true" || hidden(current.style) || hidden(computed)) return false;
        }
        let rect; try { rect = element?.getBoundingClientRect?.(); } catch { return false; }
        return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0);
      };
      const tagOf = (element) => String(element && element.tagName || "").toLowerCase();
      const typeOf = (element) => String(element && element.type || "").toLowerCase();
      const nameOf = (element) => String((element && element.getAttribute && element.getAttribute("name")) || (element && element.name) || "");
      const requiredOf = (element) => element.required === true || element.hasAttribute?.("required") || String(element.getAttribute?.("aria-required") || "").toLowerCase() === "true";
      const enabledOf = (element) => element.disabled !== true && !element.hasAttribute?.("disabled") && String(element.getAttribute?.("aria-disabled") || "").toLowerCase() !== "true";
      const textOf = (element) => String(element && (element.innerText || element.textContent || "")).replace(/\s+/g, " ").trim();
      const fields = [{ key: "first_name", label: "First name", pattern: /^buyer\.N-first_name$/, type: "text" }, { key: "last_name", label: "Last name", pattern: /^buyer\.N-last_name$/, type: "text" }, { key: "email", label: "Email", pattern: /^buyer\.N-email$/, type: "email" }, { key: "confirm_email", label: "Confirm email", pattern: /^buyer\.confirmEmailAddress$/, type: "email" }];
      const candidates = fields.map((field) => elements.filter((element) => tagOf(element) === "input" && field.pattern.test(nameOf(element)) && typeOf(element) === field.type));
      const required = elements.filter((element) => {
        const tag = tagOf(element); const type = typeOf(element);
        return ["input", "select", "textarea"].includes(tag) && (tag !== "input" || type !== "hidden") && visibleOf(element) && enabledOf(element) && requiredOf(element);
      });
      if (required.length !== 4 || candidates.some((items) => items.length !== 1 || !required.includes(items[0]))) return [];
      if (candidates.some(([element]) => !element || !element.dataset)) return [];
      candidates.forEach(([element], index) => { element.dataset.lmConnectorControl = `eventbrite_attendee_${fields[index].key}_${id}`; });
      const completedOf = (element) => Boolean(String(element && element.value || "").trim());
      const controls = fields.map((field, index) => ({
        control: `eventbrite_attendee_${field.key}_${id}`,
        kind: "input",
        label: field.label,
        required: true,
        completed: completedOf(candidates[index][0]),
        submittable: false,
      }));
      if (!controls.every((control) => control.completed === true)) return controls;
      const idOf = (element) => String((element && element.getAttribute && element.getAttribute("id")) || (element && element.id) || "");
      const marketingSpecs = [
        { key: "organization", name: "organizationMarketingOptIn", label: "Organizer marketing opt-out" },
        { key: "eventbrite", name: "ebMarketingOptIn", label: "Eventbrite marketing opt-out" },
      ];
      const checkedMarketing = [];
      let marketingValid = true;
      for (const spec of marketingSpecs) {
        const matches = elements.filter((element) => nameOf(element) === spec.name);
        if (matches.length > 1) marketingValid = false;
        for (const input of matches) {
          if (tagOf(input) !== "input" || typeOf(input) !== "checkbox" || !visibleOf(input) || !enabledOf(input) || requiredOf(input)) marketingValid = false;
          if (input.checked === true) {
            const id = idOf(input);
            if (!id || globalIdCount(id) !== 1 || !input.dataset) marketingValid = false;
            else checkedMarketing.push({ key: spec.key, input, label: spec.label });
          }
        }
      }
      const primaryCandidates = elements.filter((element) => String((element.getAttribute && element.getAttribute("data-testid")) || (element.dataset && element.dataset.testid) || "") === "eds-modal__primary-button");
      const primary = primaryCandidates.length === 1 ? primaryCandidates[0] : null;
      const primaryValid = Boolean(primary && primary.dataset && tagOf(primary) === "button" && typeOf(primary) === "button" && visibleOf(primary) && enabledOf(primary) && textOf(primary) === "Register");
      if (!marketingValid) return controls;
      for (const item of checkedMarketing) {
        item.input.dataset.lmConnectorControl = `eventbrite_marketing_opt_out_${item.key}_${id}`;
        controls.push({ control: `eventbrite_marketing_opt_out_${item.key}_${id}`, kind: "checkbox", label: item.label, required: true, completed: false, submittable: false });
      }
      if (checkedMarketing.length > 0) return controls;
      if (!primaryValid) return controls;
      primary.dataset.lmConnectorControl = `eventbrite_attendee_register_${id}`;
      controls.push({ control: `eventbrite_attendee_register_${id}`, kind: "button", label: "Register", required: false, completed: false, submittable: true });
      return controls;
    }, { eventId, idCounts, actionableLimit: EVENTBRITE_ACTIONABLE_ELEMENT_LIMIT });
  } catch { return null; }
}
async function waitForEventbriteCheckoutFrame(page, eventId, canonicalUrl) {
  if (!page || typeof page.frames !== "function" || !canonicalUrl) return false;
  const deadline = Date.now() + EVENTBRITE_FRAME_TIMEOUT_MS;
  let stableSignature = null;
  let stableSince = null;
  while (Date.now() <= deadline) {
    let pageHref = "";
    try { pageHref = String(typeof page.url === "function" ? page.url() : ""); } catch { return false; }
    if (pageHref !== String(canonicalUrl)) return false;
    const { official, matching } = eventbriteCheckoutFrameSet(page, eventId);
    if (official.length === 1 && matching.length === 1) {
      const signature = official[0].parsed.href;
      if (stableSignature !== signature) {
        stableSignature = signature;
        stableSince = Date.now();
      } else if (stableSince != null && Date.now() - stableSince >= EVENTBRITE_FRAME_STABILITY_MS) return true;
    } else {
      stableSignature = null;
      stableSince = null;
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await new Promise((resolve) => setTimeout(resolve, Math.min(FINAL_EFFECT_POLL_MS, remaining)));
  }
  return false;
}
function isEventbriteTicketRegister({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "eventbrite" || !control || control.kind !== "button" || control.label !== "Register"
    || control.required !== false || control.completed !== false || control.submittable !== true) return false;
  const binding = candidateEventbriteBinding(candidate);
  if (!binding) return false;
  const match = EVENTBRITE_TICKET_REGISTER_CONTROL.exec(String(control.control || ""));
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { return false; }
  const semantic = Array.isArray(controls) ? controls.filter((item) => item && item.kind === "button" && item.label === "Register"
    && item.required === false && item.completed === false && item.submittable === true && EVENTBRITE_TICKET_REGISTER_CONTROL.test(String(item.control || ""))) : [];
  return Boolean(match && match[1] === binding.eventId && href === binding.canonicalUrl && semantic.length === 1 && semantic[0].control === control.control);
}
function eventbriteAttendeeControlMeaning(control) {
  const match = EVENTBRITE_ATTENDEE_CONTROL.exec(String(control && control.control || ""));
  return Boolean(control && match && EVENTBRITE_ATTENDEE_LABELS[match[1]] === control.label && control.kind === "input" && control.required === true && control.submittable === false);
}
function eventbriteAttendeeControlsForEvent(controls, eventId) {
  return Array.isArray(controls) ? controls.filter((control) => eventbriteAttendeeControlMeaning(control) && EVENTBRITE_ATTENDEE_CONTROL.exec(String(control.control))[2] === String(eventId)) : [];
}
function eventbriteFinalControlMeaning(control) {
  const match = EVENTBRITE_ATTENDEE_REGISTER_CONTROL.exec(String(control && control.control || ""));
  return Boolean(control && match && control.kind === "button" && control.label === "Register"
    && control.required === false && control.completed === false && control.submittable === true);
}
function eventbriteFinalControlsForEvent(controls, eventId) {
  return Array.isArray(controls) ? controls.filter((control) => eventbriteFinalControlMeaning(control) && EVENTBRITE_ATTENDEE_REGISTER_CONTROL.exec(String(control.control))[1] === String(eventId)) : [];
}
function eventbriteMarketingControlMeaning(control) {
  const match = EVENTBRITE_MARKETING_CONTROL.exec(String(control && control.control || ""));
  return Boolean(control && match && EVENTBRITE_MARKETING_LABELS[match[1]] === control.label
    && control.kind === "checkbox" && control.required === true && control.completed === false && control.submittable === false);
}
function eventbriteMarketingControlsForEvent(controls, eventId) {
  return Array.isArray(controls) ? controls.filter((control) => eventbriteMarketingControlMeaning(control) && EVENTBRITE_MARKETING_CONTROL.exec(String(control.control))[2] === String(eventId)) : [];
}
function isEventbriteAttendeeObservation(controls, eventId) {
  const matching = eventbriteAttendeeControlsForEvent(controls, eventId); const marketing = eventbriteMarketingControlsForEvent(controls, eventId);
  return Array.isArray(controls) && matching.length === 4 && new Set(matching.map((control) => control.control)).size === 4
    && matching.length + marketing.length === controls.length && new Set(marketing.map((control) => control.control)).size === marketing.length;
}
function isEventbriteFinalObservation(controls, eventId) {
  const attendee = eventbriteAttendeeControlsForEvent(controls, eventId);
  const marketing = eventbriteMarketingControlsForEvent(controls, eventId);
  const final = eventbriteFinalControlsForEvent(controls, eventId);
  return Array.isArray(controls) && attendee.length === 4 && attendee.every((control) => control.completed === true)
    && new Set(attendee.map((control) => control.control)).size === 4
    && marketing.length === 0 && final.length === 1
    && controls.length === attendee.length + final.length
    && new Set(final.map((control) => control.control)).size === 1;
}
function isEventbriteAttendeeFill({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "eventbrite" || !eventbriteAttendeeControlMeaning(control) || control.completed !== false) return false;
  const binding = candidateEventbriteBinding(candidate);
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { return false; }
  if (!binding || href !== binding.canonicalUrl) return false;
  const match = EVENTBRITE_ATTENDEE_CONTROL.exec(control.control);
  const matching = eventbriteAttendeeControlsForEvent(controls, binding.eventId);
  return Boolean(match && match[2] === binding.eventId && isEventbriteAttendeeObservation(controls, binding.eventId) && matching.some((item) => item.control === control.control));
}
function isEventbriteMarketingOptOut({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "eventbrite" || !eventbriteMarketingControlMeaning(control) || control.completed !== false) return false;
  const binding = candidateEventbriteBinding(candidate);
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { return false; }
  if (!binding || href !== binding.canonicalUrl) return false;
  const match = EVENTBRITE_MARKETING_CONTROL.exec(control.control);
  const matching = eventbriteMarketingControlsForEvent(controls, binding.eventId);
  return Boolean(match && match[2] === binding.eventId && isEventbriteAttendeeObservation(controls, binding.eventId) && matching.some((item) => item.control === control.control));
}
function isEventbriteFinalRegister({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "eventbrite" || !eventbriteFinalControlMeaning(control)) return false;
  const binding = candidateEventbriteBinding(candidate);
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { return false; }
  const match = EVENTBRITE_ATTENDEE_REGISTER_CONTROL.exec(String(control.control || ""));
  return Boolean(binding && match && match[1] === binding.eventId && href === binding.canonicalUrl
    && isEventbriteFinalObservation(controls, binding.eventId)
    && eventbriteFinalControlsForEvent(controls, binding.eventId).some((item) => item.control === control.control));
}
async function eventbriteMarketingInputChecked(frame, key, expected) {
  const names = { organization: "organizationMarketingOptIn", eventbrite: "ebMarketingOptIn" };
  const name = names[key];
  if (!name || !frame || typeof frame.locator !== "function") return false;
  let locator;
  try { locator = frame.locator("input"); } catch { return false; }
  if (!locator || typeof locator.evaluateAll !== "function") return false;
  try {
    return await locator.evaluateAll((elements, { target, checked }) => {
      const matches = Array.isArray(elements) ? elements.filter((element) => String(element && element.tagName || "").toLowerCase() === "input" && String(element && element.type || "").toLowerCase() === "checkbox" && String((element && element.getAttribute && element.getAttribute("name")) || (element && element.name) || "") === target) : [];
      return matches.length === 1 && matches[0].checked === checked;
    }, { target: name, checked: expected }) === true;
  } catch { return false; }
}
async function readEventbriteMarketingHandle(handle, { token, name = null, id = "", checked = null } = {}) {
  if (!handle || typeof handle.evaluate !== "function" || !token || (name != null && !name) || (checked != null && typeof checked !== "boolean")) return null;
  try {
    return await handle.evaluate((element) => {
      const tag = String(element && element.tagName || "").toLowerCase();
      const type = String(element && element.type || "").toLowerCase();
      const testId = String((element && element.getAttribute && element.getAttribute("data-testid")) || (element && element.dataset && element.dataset.testid) || "");
      const text = String(element && (element.innerText || element.textContent || "")).replace(/\s+/g, " ").trim();
      const nameOf = String((element && element.getAttribute && element.getAttribute("name")) || (element && element.name) || "");
      const idOf = String((element && element.getAttribute && element.getAttribute("id")) || (element && element.id) || "");
      const tokenOf = String(element && element.dataset && element.dataset.lmConnectorControl || "");
      const required = element?.required === true
        || element?.hasAttribute?.("required")
        || String(element?.getAttribute?.("aria-required") || "").toLowerCase() === "true";
      const enabled = element?.disabled !== true
        && !element?.hasAttribute?.("disabled")
        && String(element?.getAttribute?.("aria-disabled") || "").toLowerCase() !== "true";
      const hiddenStyle = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style?.opacity ?? "") === "0");
      const visible = (() => {
        const view = element?.ownerDocument?.defaultView;
        for (let current = element; current; current = current.parentElement || null) {
          let computed = null;
          try { computed = view?.getComputedStyle?.(current); } catch { return false; }
          if (current.hidden === true || current.isConnected === false || current.hasAttribute?.("hidden") || String(current.getAttribute?.("aria-hidden") || "").toLowerCase() === "true" || hiddenStyle(current.style) || hiddenStyle(computed)) return false;
        }
        let rect;
        try { rect = element?.getBoundingClientRect?.(); } catch { return false; }
        return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0);
      })();
      return {
        tag,
        type,
        name: nameOf,
        id: idOf,
        token: tokenOf,
        testId,
        text,
        optional: !required,
        visible,
        enabled,
        connected: element?.isConnected === true,
        checked: element?.checked === true,
      };
    }, { token, name, id, checked });
  } catch { return null; }
}
function validEventbriteMarketingHandleState(state, { token, name, id, checked } = {}) {
  return Boolean(state && state.tag === "input" && state.type === "checkbox" && state.token === token && state.name === name
    && state.id === id && state.optional === true && state.visible === true && state.enabled === true
    && state.connected === true && state.checked === checked);
}
async function waitForEventbriteMarketingOptOut(page, eventId, canonicalUrl, frame, operation) {
  if (!page || !frame || !canonicalUrl || !operation || operation.page !== page || operation.frame !== frame
    || operation.eventId !== String(eventId) || !operation.handle || !operation.id || !operation.name || !operation.token) return false;
  const deadline = Date.now() + EVENTBRITE_FRAME_STABILITY_MS + FINAL_EFFECT_POLL_MS;
  let stableSince = null;
  while (Date.now() <= deadline) {
    if (eventbriteTicketFrame(page, eventId, canonicalUrl) !== frame) return false;
    const refreshed = await inspectEventbriteAttendeeFrame(frame, eventId);
    const selected = Array.isArray(refreshed) ? refreshed.find((item) => item.control === operation.token) : null;
    const frameControls = Array.isArray(refreshed) ? refreshed.filter((item) => item.control !== `eventbrite_attendee_register_${eventId}`) : null;
    const state = await readEventbriteMarketingHandle(operation.handle, { token: operation.token, name: operation.name, id: operation.id, checked: false });
    const stable = isEventbriteAttendeeObservation(frameControls, eventId) && !selected && validEventbriteMarketingHandleState(state, { token: operation.token, name: operation.name, id: operation.id, checked: false });
    if (!stable) stableSince = null;
    else if (stableSince == null) stableSince = Date.now();
    else if (Date.now() - stableSince >= EVENTBRITE_FRAME_STABILITY_MS) return true;
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await new Promise((resolve) => setTimeout(resolve, Math.min(FINAL_EFFECT_POLL_MS, remaining)));
  }
  return false;
}
async function waitForEventbriteTicketStep(page, eventId, canonicalUrl) {
  const deadline = Date.now() + EVENTBRITE_FRAME_TIMEOUT_MS;
  let stableSince = null;
  while (Date.now() <= deadline) {
    const frame = eventbriteTicketFrame(page, eventId, canonicalUrl);
    const state = frame ? await inspectEventbriteTicketFrame(frame, eventId) : null;
    if (state && state.cardCount === 0) {
      if (stableSince == null) stableSince = Date.now();
      if (Date.now() - stableSince >= EVENTBRITE_FRAME_STABILITY_MS) return true;
    } else stableSince = null;
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await new Promise((resolve) => setTimeout(resolve, Math.min(FINAL_EFFECT_POLL_MS, remaining)));
  }
  return false;
}
function isConnpassJoin(provider, href) {
  return provider === "connpass" && CONNPASS_JOIN_URL.test(String(href || ""));
}
function isDoorkeeperTriggerSemantic(control) {
  return Boolean(
    control && control.kind === "link" && control.label === DOORKEEPER_FINAL_LABEL
    && control.required === false && control.completed === false && control.submittable === false
  );
}
function isDoorkeeperTriggerControl(control) {
  return isDoorkeeperTriggerSemantic(control) && DOORKEEPER_TRIGGER_CONTROL.test(String(control.control || ""));
}
function isDoorkeeperModalTrigger({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "doorkeeper" || !isDoorkeeperTriggerControl(control)) return false;
  const binding = candidateDoorkeeperBinding(candidate);
  if (!binding) return false;
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { href = ""; }
  if (href !== binding.canonicalUrl) return false;
  const matching = Array.isArray(controls)
    ? controls.filter(isDoorkeeperTriggerSemantic)
    : [];
  return matching.length === 1 && matching[0].control === control.control;
}
function isDoorkeeperFinalSubmit({ provider, page, candidate, control, controls = [] } = {}) {
  if (provider !== "doorkeeper" || !control || control.kind !== "button" || control.submittable !== true || control.label !== DOORKEEPER_FINAL_LABEL) return false;
  const binding = candidateDoorkeeperBinding(candidate);
  if (!binding) return false;
  let href = "";
  try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch { href = ""; }
  if (href !== binding.canonicalUrl) return false;
  const matching = Array.isArray(controls)
    ? controls.filter((item) => item && item.kind === "button" && item.submittable === true && item.label === DOORKEEPER_FINAL_LABEL)
    : [];
  return matching.length === 1 && matching[0].control === control.control;
}

function startPeatixConfirmWait(page, provider, control, candidate, controls = []) {
  if (provider === "techplay") {
    const binding = candidateTechPlayBinding(candidate); const token = `techplay_review_${binding?.eventId || ""}`;
    const review = binding && control && control.kind === "button" && control.label === TECHPLAY_REVIEW_LABEL
      && control.required === false && control.completed === false && control.submittable === true && control.control === token;
    const matching = Array.isArray(controls) ? controls.filter((item) => item && item.control === token && item.kind === "button"
      && item.label === TECHPLAY_REVIEW_LABEL && item.required === false && item.completed === false && item.submittable === true) : [];
    let href = ""; try { href = String(typeof page?.url === "function" ? page.url() : ""); } catch {}
    if (!review || matching.length !== 1 || href !== `${binding.canonicalUrl.replace("/event/", "/event/join/")}` || typeof page?.waitForURL !== "function") return { unavailable: true, promise: Promise.resolve(false) };
    let wait; try { wait = page.waitForURL((url) => { const match = TECHPLAY_CONFIRM_URL.exec(String(url || "")); return Boolean(match && match[1] === binding.eventId); }, { waitUntil: "domcontentloaded", timeout: 30_000 }); } catch { return { unavailable: true, promise: Promise.resolve(false) }; }
    return { promise: Promise.resolve(wait).then(() => { try { const match = TECHPLAY_CONFIRM_URL.exec(String(typeof page.url === "function" ? page.url() : "")); return Boolean(match && match[1] === binding.eventId); } catch { return false; } }, () => false) };
  }
  if (
    provider !== "peatix" || !control || control.kind !== "button" || control.submittable !== true
    || control.label !== PEATIX_FORM_SUBMIT_LABEL
  ) return null;
  let currentHref = "";
  try { currentHref = String(typeof page.url === "function" ? page.url() : ""); } catch { currentHref = ""; }
  const formMatch = PEATIX_FORM_URL.exec(currentHref);
  if (!formMatch) return null;
  if (typeof page.waitForURL !== "function") return { unavailable: true, promise: Promise.resolve(false) };
  let wait;
  try {
    wait = page.waitForURL((url) => {
      const match = PEATIX_CONFIRM_URL.exec(String(url || ""));
      return Boolean(match && match[1] === formMatch[1]);
    }, { waitUntil: "domcontentloaded", timeout: 30_000 });
  } catch {
    return { unavailable: true, promise: Promise.resolve(false) };
  }
  return {
    promise: Promise.resolve(wait).then((settled) => {
      if (settled === false) return false;
      try {
        const match = PEATIX_CONFIRM_URL.exec(String(typeof page.url === "function" ? page.url() : ""));
        return Boolean(match && match[1] === formMatch[1]);
      } catch {
        return false;
      }
    }, () => false),
  };
}

function readStateWithinDeadline(page, candidate, readProviderState, deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) return Promise.resolve({ expired: true });
  return new Promise((resolve) => {
    let timer;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      resolve(value);
    };
    try { timer = setTimeout(() => finish({ expired: true }), remaining); }
    catch { finish({ expired: true }); return; }
    Promise.resolve()
      .then(() => readProviderState({ page, candidate }))
      .then((state) => finish({ state }), () => finish({ error: true }));
  });
}

function startFinalEffectWait(page, provider, control, candidate, readProviderState, controls = [], acceptedStatuses) {
  if (!control || control.kind !== "button" || control.submittable !== true) return null;
  const peatixFinal = provider === "peatix" && control.label === PEATIX_CONFIRM_LABEL;
  const connpassFinal = provider === "connpass" && control.label === CONNPASS_FINAL_LABEL;
  const doorkeeperFinal = provider === "doorkeeper" && control.label === DOORKEEPER_FINAL_LABEL;
  const eventbriteFinal = provider === "eventbrite" && eventbriteFinalControlMeaning(control);
  const techplayFinal = provider === "techplay" && control.label === TECHPLAY_FINAL_LABEL;
  if (!peatixFinal && !connpassFinal && !doorkeeperFinal && !eventbriteFinal && !techplayFinal) return null;
  let href = "";
  try { href = String(typeof page.url === "function" ? page.url() : ""); } catch { href = ""; }
  if (eventbriteFinal) {
    const binding = candidateEventbriteBinding(candidate);
    if (!binding || !isEventbriteFinalRegister({ provider, page, candidate, control, controls }) || typeof readProviderState !== "function") return { unavailable: true, promise: Promise.resolve({ status: "unknown" }) };
  } else if (techplayFinal) {
    const binding = candidateTechPlayBinding(candidate); const token = `techplay_final_${binding?.eventId || ""}`;
    const exact = (item) => item && item.control === token && item.kind === "button" && item.label === TECHPLAY_FINAL_LABEL && item.required === false && item.completed === false && item.submittable === true;
    const match = TECHPLAY_CONFIRM_URL.exec(href);
    if (!binding || !match || match[1] !== binding.eventId || !exact(control) || !Array.isArray(controls) || controls.length !== 1 || !exact(controls[0]) || typeof readProviderState !== "function") return { unavailable: true, promise: Promise.resolve({ status: "unknown" }) };
  } else if (peatixFinal) {
    const eventId = candidatePeatixEventId(candidate);
    const confirmMatch = PEATIX_CONFIRM_URL.exec(href);
    if (!eventId || !confirmMatch || confirmMatch[1] !== eventId || typeof readProviderState !== "function") return { unavailable: true, promise: Promise.resolve({ status: "unknown" }) };
  } else if (connpassFinal) {
    const eventId = candidateConnpassEventId(candidate);
    const joinMatch = CONNPASS_JOIN_URL.exec(href);
    const matchingControls = Array.isArray(controls) ? controls.filter((item) => item && item.kind === "button" && item.submittable === true && item.label === CONNPASS_FINAL_LABEL) : [];
    if (!eventId || !joinMatch || joinMatch[1] !== eventId || matchingControls.length !== 1 || matchingControls[0].control !== control.control || typeof readProviderState !== "function") return { unavailable: true, promise: Promise.resolve({ status: "unknown" }) };
  } else {
    if (!isDoorkeeperFinalSubmit({ provider, page, candidate, control, controls }) || typeof readProviderState !== "function") return { unavailable: true, promise: Promise.resolve({ status: "unknown" }) };
  }
  const statuses = Array.isArray(acceptedStatuses) && acceptedStatuses.length ? acceptedStatuses : ["registered", "pending"];
  let releaseClick;
  let cancelled = false;
  const clickStarted = new Promise((resolve) => { releaseClick = resolve; });
  const promise = (async () => {
    await clickStarted;
    const deadline = Date.now() + FINAL_EFFECT_TIMEOUT_MS;
    while (!cancelled) {
      const readback = await readStateWithinDeadline(page, candidate, readProviderState, deadline);
      if (readback.expired) return { status: "unknown" };
      if (readback.state && statuses.includes(readback.state.status)) {
        return { status: readback.state.status, providerState: readback.state };
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await new Promise((resolve) => setTimeout(resolve, Math.min(FINAL_EFFECT_POLL_MS, remaining)));
    }
    return { status: cancelled ? "cancelled" : "unknown" };
  })();
  return { markClicked: releaseClick, cancel() { cancelled = true; releaseClick(); }, promise };
}

async function settleFinalEffect(wait, acceptedStatuses) {
  let settled = null;
  try { settled = await wait.promise; } catch { settled = null; }
  const statuses = Array.isArray(acceptedStatuses) && acceptedStatuses.length ? acceptedStatuses : ["registered", "pending"];
  if (!settled || !statuses.includes(settled.status)) {
    return Object.freeze({ status: "failed", safe_reason: "effect_unknown" });
  }
  const providerState = settled.providerState && typeof settled.providerState === "object" && !Array.isArray(settled.providerState)
    ? Object.freeze({ ...settled.providerState }) : null;
  return Object.freeze({ status: "success", ...(providerState ? { provider_state: providerState } : {}) });
}

function safeControl(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  const control = String(input.control || "");
  const kind = String(input.kind || "");
  const label = String(input.label || "").replace(/\s+/g, " ").trim();
  const question = String(input.question || "").replace(/\s+/g, " ").trim();
  const completed = input.completed == null ? false : input.completed;
  const submittable = input.submittable == null ? false : input.submittable;
  if (
    !CONTROL.test(control) || !KINDS.has(kind) || !label || label.length > 300
    || /[\x00-\x1f\x7f]/.test(label) || question.length > 300
    || /[\x00-\x1f\x7f]/.test(question) || (question && !["checkbox", "radio"].includes(kind))
    || typeof input.required !== "boolean" || typeof completed !== "boolean"
    || typeof submittable !== "boolean" || (submittable && kind !== "button")
  ) invalid();
  return Object.freeze({ control, kind, label, required: input.required, completed, submittable, ...(question ? { question } : {}) });
}

function actionForControl(control) {
  if (eventbriteMarketingControlMeaning(control)) return Object.freeze({ purpose: "fill", method: "ax_uncheck", control: control.control });
  const action = ACTIONS[control.kind]; return action ? Object.freeze({ purpose: action[0], method: action[1], control: control.control }) : null;
}

function inspectTechPlayInput(elements, context = {}) {
  if (!Array.isArray(elements) || elements.length > 150) return [];
  try { elements.forEach((element) => { if (element && element.dataset) delete element.dataset.lmConnectorControl; }); } catch { return []; }
  const TECHPLAY_EVENT_URL = /^https:\/\/techplay\.jp\/event\/([1-9][0-9]*)$/; const TECHPLAY_JOIN_URL = /^https:\/\/techplay\.jp\/event\/join\/([1-9][0-9]*)$/;
  const TECHPLAY_ANSWER_NAME = /^enqueteAnswers\[([1-9][0-9]*)\]$/; const TECHPLAY_OPT_OUT_ID = /^(?:area_[1-9][0-9]*|tag_[1-9][0-9]*|organizer_[1-9][0-9]*|icon_published|use_as_preset)$/;
  const TECHPLAY_QUESTIONS = new Set(["氏名", "メールアドレス", "年齢", "キャリア状況", "所属企業（学校）名", "職種"]); const TECHPLAY_RADIO_COUNTS = { "キャリア状況": 3, "職種": 33 }; const TECHPLAY_SCALAR_TYPES = { "氏名": "text", "メールアドレス": "text", "年齢": "number", "所属企業（学校）名": "text" };
  const eventId = String(context.eventId || ""); const ticketId = String(context.ticketId || "");
  const page = TECHPLAY_JOIN_URL.exec(String(context.href || "")); const candidate = TECHPLAY_EVENT_URL.exec(String(context.canonicalUrl || ""));
  if (!page || !candidate || page[1] !== eventId || candidate[1] !== eventId || !/^[1-9][0-9]*$/.test(ticketId)) return [];
  const tag = (element) => String(element && element.tagName || "").toLowerCase();
  const type = (element) => String(element && element.type || "").toLowerCase();
  const attr = (element, name) => String(element && typeof element.getAttribute === "function" ? element.getAttribute(name) || "" : element && element[name] || "");
  const text = (element) => String(element && (element.innerText || element.textContent) || "").replace(/\s+/g, " ").trim();
  const visible = (element) => {
    const view = element && element.ownerDocument && element.ownerDocument.defaultView; let current = element;
    const hiddenStyle = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style && style.opacity || "") === "0");
    while (current) {
      if (current.hidden === true || current.isConnected === false || attr(current, "aria-hidden").toLowerCase() === "true" || hiddenStyle(current.style)) return false;
      let computed = null; try { computed = view && typeof view.getComputedStyle === "function" ? view.getComputedStyle(current) : null; } catch { computed = null; }
      if (hiddenStyle(computed)) return false;
      if (current !== element && typeof current.getBoundingClientRect === "function") { let ancestorRect; try { ancestorRect = current.getBoundingClientRect(); } catch { ancestorRect = null; } if (!ancestorRect || Number(ancestorRect.width) <= 0 || Number(ancestorRect.height) <= 0) return false; }
      current = current.parentElement || null;
    }
    let rect; try { rect = element && typeof element.getBoundingClientRect === "function" ? element.getBoundingClientRect() : null; } catch { rect = null; }
    return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0);
  };
  const label = (element) => [...(element.labels || [])].map((item) => text(item)).concat(attr(element, "aria-label")).map((value) => value.trim()).find(Boolean) || "";
  const groupQuestion = (element) => { let current = element.parentElement || null; while (current) { const value = text(current); const matches = [...TECHPLAY_QUESTIONS].filter((question) => value.includes(`${question}*`) || value.includes(`${question} *`)); if (matches.length === 1) return matches[0]; current = current.parentElement || null; } return ""; };
  const seenDomIds = new Set();
  for (const element of elements) { const id = attr(element, "id") || String(element.id || ""); const hiddenCompanion = !visible(element) && tag(element) === "input" && type(element) === "checkbox" && TECHPLAY_OPT_OUT_ID.test(id); if (id && !hiddenCompanion && seenDomIds.has(id)) return []; if (id && !hiddenCompanion) seenDomIds.add(id); }
  const answerElements = elements.filter((element) => /^enqueteAnswers\[/.test(attr(element, "name") || element.name || ""));
  const groups = new Map(); const seenAnswerIds = new Set();
  for (const element of answerElements) {
    const name = attr(element, "name") || String(element.name || ""); const match = TECHPLAY_ANSWER_NAME.exec(name); const radio = type(element) === "radio"; const raw = label(element);
    const question = radio ? groupQuestion(element) : raw.replace(/\s*\*\s*/g, " ").replace(/\s+/g, " ").trim();
    const id = String(element.id || attr(element, "id") || ""); const group = groups.get(name);
    if (!match || (id && seenAnswerIds.has(id)) || !TECHPLAY_QUESTIONS.has(question) || (radio && tag(element) !== "input") || (!radio && (tag(element) !== "input" || type(element) !== TECHPLAY_SCALAR_TYPES[question])) || (!radio && !raw.includes("*")) || (!radio && group) || !raw && radio || element.disabled === true || !visible(element)) return [];
    if (id) seenAnswerIds.add(id);
    if (group && group.question !== question) return [];
    (group || groups.set(name, { id: match[1], question, radio, elements: [], labels: new Set() }).get(name)).elements.push(element);
    if (radio && (group?.labels.has(raw) || !raw)) return [];
    if (radio) groups.get(name).labels.add(raw);
  }
  const answers = [];
  for (const group of groups.values()) {
    const selected = group.radio ? group.elements.filter((element) => element.checked === true).length : 0;
    if (group.radio && selected > 1) return [];
    answers.push({ ...group, completed: group.radio ? selected === 1 : Boolean(String(group.elements[0].value || "").trim()) });
  }
  if (answers.length !== TECHPLAY_QUESTIONS.size || new Set(answers.map(({ question }) => question)).size !== TECHPLAY_QUESTIONS.size || answers.some(({ radio, question, elements: groupElements }) => radio && groupElements.length !== TECHPLAY_RADIO_COUNTS[question])) return [];
  const radios = elements.filter((element) => type(element) === "radio" && !TECHPLAY_ANSWER_NAME.test(attr(element, "name") || element.name || ""));
  if (radios.length !== 1 || tag(radios[0]) !== "input" || !visible(radios[0]) || radios[0].disabled === true || radios[0].checked !== true || String(radios[0].value || "") !== ticketId) return [];
  const checkboxes = elements.filter((element) => attr(element, "role").toLowerCase() === "checkbox"); const optouts = []; const seenIds = new Set();
  for (const element of checkboxes) {
    const id = attr(element, "id") || String(element.id || "");
    if (!visible(element)) { if (tag(element) === "input" && type(element) === "checkbox" && TECHPLAY_OPT_OUT_ID.test(id)) continue; return []; }
    if (tag(element) !== "button" || !TECHPLAY_OPT_OUT_ID.test(id) || seenIds.has(id) || element.disabled === true) return [];
    const checked = attr(element, "aria-checked").toLowerCase(); if (!["true", "false"].includes(checked)) return [];
    seenIds.add(id); optouts.push({ id, completed: checked === "false" });
  }
  if (elements.some((element) => tag(element) === "input" && type(element) === "checkbox" && visible(element))) return [];
  const reviews = elements.filter((element) => visible(element) && text(element) === "同意して内容を確認する");
  if (reviews.length !== 1 || tag(reviews[0]) !== "button" || type(reviews[0]) !== "submit" || reviews[0].disabled === true || attr(reviews[0], "aria-disabled").toLowerCase() === "true") return [];
  const ticketCompleted = radios[0].checked === true; const complete = ticketCompleted && answers.every(({ completed }) => completed) && optouts.every(({ completed }) => completed);
  const bindings = [
    ...answers.flatMap(({ elements: groupElements, id, radio }) => radio
      ? groupElements.map((element, index) => [element, `techplay_answer_${id}_${index + 1}`])
      : [[groupElements[0], `techplay_answer_${id}`]]),
    ...optouts.map(({ id }, index) => [checkboxes.filter((element) => (attr(element, "id") || String(element.id || "")) === id)[0], `techplay_optout_${id}`]),
    [reviews[0], `techplay_review_${eventId}`],
  ];
  if (bindings.some(([element]) => !element || !element.dataset)) return [];
  try { for (const [element, control] of bindings) element.dataset.lmConnectorControl = control; } catch { for (const [element] of bindings) { try { if (element?.dataset) delete element.dataset.lmConnectorControl; } catch {} } return []; }
  return [
    { control: `techplay_ticket_${eventId}`, kind: "radio", label: "無料チケット", required: true, completed: ticketCompleted, submittable: false },
    ...answers.flatMap(({ elements: groupElements, question, id, radio, completed }) => radio ? groupElements.map((element, index) => ({ control: `techplay_answer_${id}_${index + 1}`, kind: "radio", label: label(element), required: true, completed, submittable: false, question })) : [{ control: `techplay_answer_${id}`, kind: tag(groupElements[0]) === "textarea" ? "textarea" : tag(groupElements[0]) === "select" ? "select" : "input", label: question, required: true, completed, submittable: false }]),
    ...optouts.map(({ id, completed }) => ({ control: `techplay_optout_${id}`, kind: "checkbox", label: "通知設定", required: true, completed, submittable: false })),
    { control: `techplay_review_${eventId}`, kind: "button", label: "同意して内容を確認する", required: false, completed: false, submittable: complete },
  ];
}

function inspectTechPlayConfirm(elements, context = {}) {
  if (!Array.isArray(elements) || elements.length > 150) return [];
  try { elements.forEach((element) => { if (element && element.dataset) delete element.dataset.lmConnectorControl; }); } catch { return []; }
  const eventUrl = /^https:\/\/techplay\.jp\/event\/([1-9][0-9]*)$/; const confirmUrl = /^https:\/\/techplay\.jp\/event\/join\/([1-9][0-9]*)\/confirm$/; const answerName = /^enqueteAnswers\[/;
  const eventId = String(context.eventId || ""); const page = confirmUrl.exec(String(context.href || "")); const candidate = eventUrl.exec(String(context.canonicalUrl || ""));
  if (!page || !candidate || page[1] !== eventId || candidate[1] !== eventId || !/^[1-9][0-9]*$/.test(String(context.ticketId || ""))) return [];
  const tag = (element) => String(element && element.tagName || "").toLowerCase(); const type = (element) => String(element && element.type || "").toLowerCase(); const attr = (element, name) => String(element && typeof element.getAttribute === "function" ? element.getAttribute(name) || "" : element && element[name] || ""); const text = (element) => String(element && (element.innerText || element.textContent) || "").replace(/\s+/g, " ").trim();
  const visible = (element) => { const view = element && element.ownerDocument && element.ownerDocument.defaultView; let current = element; const hidden = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style && style.opacity || "") === "0"); while (current) { if (current.hidden === true || current.isConnected === false || attr(current, "aria-hidden").toLowerCase() === "true" || hidden(current.style)) return false; let computed = null; try { computed = view && typeof view.getComputedStyle === "function" ? view.getComputedStyle(current) : null; } catch { computed = null; } if (hidden(computed)) return false; if (current !== element && typeof current.getBoundingClientRect === "function") { let rect; try { rect = current.getBoundingClientRect(); } catch { rect = null; } if (!rect || Number(rect.width) <= 0 || Number(rect.height) <= 0) return false; } current = current.parentElement || null; } let rect; try { rect = element && typeof element.getBoundingClientRect === "function" ? element.getBoundingClientRect() : null; } catch { rect = null; } return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0); };
  const ids = new Set(); for (const element of elements) { const id = attr(element, "id") || String(element.id || ""); if (id && ids.has(id)) return []; if (id) ids.add(id); }
  if (elements.some((element) => answerName.test(attr(element, "name") || element.name || "") || (tag(element) === "input" && type(element) === "radio") || attr(element, "role").toLowerCase() === "checkbox" || (tag(element) === "input" && type(element) === "checkbox" && visible(element)))) return [];
  const finals = elements.filter((element) => text(element) === "申し込みを確定する");
  if (finals.length !== 1 || tag(finals[0]) !== "button" || type(finals[0]) !== "button" || !visible(finals[0]) || finals[0].disabled === true || attr(finals[0], "aria-disabled").toLowerCase() === "true" || attr(finals[0], "aria-enabled").toLowerCase() === "false") return [];
  let region = finals[0]; while (region && tag(region) !== "main") region = region.parentElement; const inRegion = (element) => { if (!region) return true; let current = element; while (current) { if (current === region) return true; current = current.parentElement || null; } return false; };
  if (elements.some((element) => element !== finals[0] && visible(element) && inRegion(element) && (["input", "textarea", "select"].includes(tag(element)) || (tag(element) === "button" && text(element) !== "内容を修正する")))) return [];
  if (!finals[0].dataset) return [];
  const token = `techplay_final_${eventId}`;
  try { finals[0].dataset.lmConnectorControl = token; }
  catch { try { delete finals[0].dataset.lmConnectorControl; } catch {} return []; }
  return [{ control: token, kind: "button", label: TECHPLAY_FINAL_LABEL, required: false, completed: false, submittable: true }];
}

function inspectKokuchProCanonical(elements, context = {}) {
  if (!Array.isArray(elements) || elements.length > 100) return [];
  const canonicalUrl = String(context.canonicalUrl || ""), eventRef = String(context.eventRef || "");
  const ticketId = String(context.ticketId || ""), eventKey = String(context.eventKey || ""), token = String(context.token || "");
  const url = /^https:\/\/www\.kokuchpro\.com\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]{0,19}))?\/$/.exec(canonicalUrl);
  const ref = /^kokuchpro-event:\/\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]{0,19}))?$/.exec(eventRef);
  if (String(context.href || "") !== canonicalUrl || !url || !ref || url[1] !== eventKey || ref[1] !== eventKey || (url[2] || "") !== (ref[2] || "")
    || !/^[1-9][0-9]*$/.test(ticketId) || !/^kokuchpro_entry_[0-9a-f]{32}(?:_o_[0-9a-z]{1,13})?$/.test(token)) return [];
  const tag = (element) => String(element && element.tagName || "").toLowerCase();
  const attr = (element, name) => { try { const property = element && element[name]; if (property != null && String(property) !== "") return String(property); const value = element?.getAttribute?.(name); return value == null ? "" : String(value); } catch { return ""; } };
  const text = (element) => String(element && (element.innerText || element.textContent || element.value) || "").replace(/\s+/g, " ").trim();
  const hiddenStyle = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style && style.opacity || "") === "0");
  const visible = (element) => {
    let current = element; const view = element?.ownerDocument?.defaultView;
    while (current) {
      if (current.hidden === true || current.isConnected === false || attr(current, "aria-hidden").toLowerCase() === "true" || hiddenStyle(current.style)) return false;
      let computed = null; try { computed = view?.getComputedStyle?.(current); } catch { return false; } if (hiddenStyle(computed)) return false;
      current = current.parentElement || null;
    }
    try { const rect = element?.getBoundingClientRect?.(); return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0); } catch { return false; }
  };
  const controlsOf = (form) => { try { return form?.elements != null ? Array.from(form.elements) : Array.from(form?.querySelectorAll?.("input, button") || []); } catch { return []; } };
  const forms = [], seen = new Set();
  for (const element of elements) { const form = tag(element) === "form" ? element : element?.form; if (form && !seen.has(form)) { seen.add(form); forms.push(form); } }
  if (!forms.length) return [];
  try { forms.flatMap(controlsOf).forEach((element) => { if (element?.dataset) delete element.dataset.lmConnectorControl; }); } catch { return []; }
  const entryAction = canonicalUrl + "entry/";
  const candidates = forms.filter((form) => attr(form, "action") === entryAction || controlsOf(form).some((element) => tag(element) === "input" && (attr(element, "id") === "FormEntryAvailability" || attr(element, "name") === "data[Form][entry_availability]")));
  if (candidates.length < 1 || candidates.length > 2) return [];
  const selected = [];
  for (const form of candidates) {
    const controls = controlsOf(form);
    const methods = controls.filter((element) => tag(element) === "input" && attr(element, "name") === "_method");
    const markers = controls.filter((element) => tag(element) === "input" && (attr(element, "id") === "FormEntryAvailability" || attr(element, "name") === "data[Form][entry_availability]"));
    if (attr(form, "action") !== entryAction || attr(form, "method").toUpperCase() !== "POST" || methods.length !== 1 || markers.length !== 1) return [];
    const method = methods[0], marker = markers[0];
    if (attr(method, "type").toLowerCase() !== "hidden" || attr(method, "value") !== "POST"
      || attr(marker, "id") !== "FormEntryAvailability" || attr(marker, "name") !== "data[Form][entry_availability]"
      || attr(marker, "type").toLowerCase() !== "hidden" || attr(marker, "value") !== ticketId) return [];
    const submits = controls.filter((element) => tag(element) === "button" && attr(element, "type").toLowerCase() === "submit"
      && element.isConnected === true && element.disabled !== true && attr(element, "aria-disabled").toLowerCase() !== "true"
      && attr(element, "aria-enabled").toLowerCase() !== "false" && visible(element));
    if (submits.length !== 1 || text(submits[0]) !== "申込む" || !submits[0].dataset) return [];
    selected.push(submits[0]);
  }
  try { selected[0].dataset.lmConnectorControl = token; } catch { return []; }
  return [{ control: token, kind: "link", label: "申込む", required: false, completed: false, submittable: false }];
}

function inspectKokuchProEntry(elements, context = {}) {
  if (!Array.isArray(elements) || elements.length > 100) return [];
  const canonicalUrl = String(context.canonicalUrl || ""), eventRef = String(context.eventRef || ""), href = String(context.href || "");
  const eventKey = String(context.eventKey || ""), seatToken = String(context.seatToken || ""), finalToken = String(context.finalToken || "");
  const url = /^https:\/\/www\.kokuchpro\.com\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]{0,19}))?\/$/.exec(canonicalUrl);
  const ref = /^kokuchpro-event:\/\/event\/([0-9a-f]{32})(?:\/([1-9][0-9]{0,19}))?$/.exec(eventRef);
  if (!url || !ref || url[1] !== eventKey || ref[1] !== eventKey || (url[2] || "") !== (ref[2] || "")
    || href !== canonicalUrl + "entry/"
    || !/^kokuchpro_seat_[0-9a-f]{32}(?:_o_[0-9a-z]{1,13})?$/.test(seatToken)
    || !/^kokuchpro_final_[0-9a-f]{32}(?:_o_[0-9a-z]{1,13})?$/.test(finalToken)) return [];
  const tag = (element) => String(element && element.tagName || "").toLowerCase(); const attr = (element, name) => { try { const property = element && element[name]; if (property != null && String(property) !== "") return String(property); const value = element?.getAttribute?.(name); return value == null ? "" : String(value); } catch { return ""; } };
  const text = (element) => String(element && (element.innerText || element.textContent) || "").replace(/\s+/g, " ").trim(); const hiddenStyle = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style && style.opacity || "") === "0");
  const visible = (element) => { let current = element; const view = element?.ownerDocument?.defaultView; while (current) { if (current.hidden === true || current.isConnected === false || attr(current, "aria-hidden").toLowerCase() === "true" || hiddenStyle(current.style)) return false; let computed = null; try { computed = view?.getComputedStyle?.(current); } catch { return false; } if (hiddenStyle(computed)) return false; current = current.parentElement || null; } try { const rect = element?.getBoundingClientRect?.(); return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0); } catch { return false; } };
  const controlsOf = (form) => { try { return form?.elements != null ? Array.from(form.elements) : Array.from(form?.querySelectorAll?.("input, textarea, select, button") || []); } catch { return []; } }; const forms = [], seen = new Set();
  for (const element of elements) { const form = tag(element) === "form" ? element : element?.form; if (form && !seen.has(form)) { seen.add(form); forms.push(form); } } if (!forms.length) return [];
  try { forms.flatMap(controlsOf).forEach((element) => { if (element?.dataset) delete element.dataset.lmConnectorControl; }); } catch { return []; }
  const entryAction = canonicalUrl + "entry/", entryForms = forms.filter((form) => attr(form, "action") === entryAction && attr(form, "method").toUpperCase() === "POST"); if (entryForms.length !== 1) return [];
  const controls = controlsOf(entryForms[0]), identity = [["EntryEmail", "data[Entry][email]"], ["EntryName", "data[Entry][name]"], ["EntryNamePublish", "data[Entry][name_publish]"]].map(([id, name]) => controls.filter((element) => tag(element) === "input" && attr(element, "type").toLowerCase() === "hidden" && attr(element, "id") === id && attr(element, "name") === name));
  const methods = controls.filter((element) => tag(element) === "input" && attr(element, "name") === "_method"), message = controls.filter((element) => attr(element, "id") === "EntryMessage" || attr(element, "name") === "data[Entry][message]"), radioSeats = controls.filter((element) => tag(element) === "input" && attr(element, "type").toLowerCase() === "radio" && attr(element, "name") === "data[Entry][seat]"), finals = controls.filter((element) => tag(element) === "button" && attr(element, "id") === "entry_submit_button" && attr(element, "type").toLowerCase() === "submit"), spinnerName = /^data\[Entry\]\[seat[1-9][0-9]*\]$/;
  const valueOf = (element) => { try { return element && element.value != null ? String(element.value) : attr(element, "value"); } catch { return ""; } };
  const spinnerSeats = controls.filter((element) => tag(element) === "input" && spinnerName.test(attr(element, "name")));
  const spinnerClasses = ["form-control", "spinner", "ui-spinner-input"];
  const spinnerShape = (element) => {
    if (!element || attr(element, "type").toLowerCase() !== "text" || attr(element, "class").trim().split(/\s+/).length !== spinnerClasses.length) return false;
    const classes = attr(element, "class").trim().split(/\s+/);
    if (new Set(classes).size !== spinnerClasses.length || !spinnerClasses.every((name) => classes.includes(name))) return false;
    const max = attr(element, "data-spinner-max");
    return attr(element, "data-spinner-min") === "0" && /^[1-9][0-9]*$/.test(max) && Number.isSafeInteger(Number(max)) && Number(max) > 0 && ["0", "1"].includes(valueOf(element));
  };
  if (methods.length !== 1 || attr(methods[0], "type").toLowerCase() !== "hidden" || attr(methods[0], "value") !== "POST" || identity.some((fields) => fields.length !== 1 || !String(attr(fields[0], "value")).trim()) || message.length > 1 || (message.length === 1 && (tag(message[0]) !== "textarea" || attr(message[0], "id") !== "EntryMessage" || attr(message[0], "name") !== "data[Entry][message]")) || (radioSeats.length !== 1 && spinnerSeats.length !== 1) || (radioSeats.length === 1 && spinnerSeats.length === 1) || (spinnerSeats.length === 1 && !spinnerShape(spinnerSeats[0])) || finals.length !== 1) return [];
  const spinner = spinnerSeats[0], seat = radioSeats[0] || spinner, final = finals[0], seatLabels = Array.from(radioSeats[0]?.labels || []), seatLabelElement = seatLabels.length === 1 ? seatLabels[0] : null;
  const seatLabel = text(seatLabelElement); const labelOwnsSeat = Boolean(seatLabelElement && (seatLabelElement.control === seat || seatLabelElement.contains?.(seat)));
  const radioReady = !radioSeats.length || Boolean(seatLabel && labelOwnsSeat && visible(seatLabelElement) && seatLabelElement.dataset && seat.isConnected === true && seat.disabled !== true && attr(seat, "aria-disabled").toLowerCase() !== "true" && attr(seat, "aria-enabled").toLowerCase() !== "false");
  const spinnerReady = !spinnerSeats.length || Boolean(visible(spinner) && spinner.isConnected === true && spinner.disabled !== true && attr(spinner, "aria-disabled").toLowerCase() !== "true" && attr(spinner, "aria-enabled").toLowerCase() !== "false");
  if (!radioReady || !spinnerReady || final.isConnected !== true || final.disabled === true || attr(final, "aria-disabled").toLowerCase() === "true" || attr(final, "aria-enabled").toLowerCase() === "false" || !visible(final) || text(final) !== "申込む" || !final.dataset) return [];
  if (controls.some((element) => element !== methods[0] && !identity.flat().includes(element) && !message.includes(element) && element !== seat && element !== final && visible(element) && ["input", "textarea", "select", "button"].includes(tag(element)))) return [];
  try { (spinner ? spinner : seatLabelElement).dataset.lmConnectorControl = seatToken; final.dataset.lmConnectorControl = finalToken; }
  catch { try { delete (spinner ? spinner : seatLabelElement).dataset.lmConnectorControl; delete final.dataset.lmConnectorControl; } catch {} return []; }
  return [
    spinner
      ? { control: seatToken, kind: "input", label: "参加人数", required: true, completed: valueOf(spinner) === "1", submittable: false }
      : { control: seatToken, kind: "radio", label: seatLabel, required: true, completed: seat.checked === true, submittable: false },
    { control: finalToken, kind: "button", label: "申込む", required: false, completed: false, submittable: true },
  ];
}

async function inspectKokuchProEntryWhenReady(target, formsLocator, context, { signal, sleep, inspector = inspectKokuchProEntry, expectedLength = 2 } = {}) {
  const wait = typeof sleep === "function" ? sleep
    : typeof target?.waitForTimeout === "function" ? (milliseconds) => target.waitForTimeout(milliseconds)
      : (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const aborted = () => Boolean(signal && signal.aborted === true);
  const inspect = async () => {
    if (aborted()) return [];
    try { return await formsLocator.evaluateAll(inspector, context); }
    catch { return []; }
  };
  const first = await inspect();
  if (aborted()) return [];
  if (Array.isArray(first) && first.length === expectedLength) return first;
  const deadline = Date.now() + KOKUCHPRO_ENTRY_READY_TIMEOUT_MS;
  const maxPolls = Math.ceil(KOKUCHPRO_ENTRY_READY_TIMEOUT_MS / KOKUCHPRO_ENTRY_POLL_INTERVAL_MS);
  const waitWithAbort = async (milliseconds) => {
    if (aborted()) return false;
    if (!signal || typeof signal.addEventListener !== "function") {
      await wait(milliseconds);
      return true;
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        try { signal.removeEventListener?.("abort", onAbort); } catch {}
      };
      const finish = (value, error) => {
        if (settled) return;
        settled = true;
        cleanup();
        if (error) reject(error); else resolve(value);
      };
      const onAbort = () => finish(false);
      try { signal.addEventListener("abort", onAbort, { once: true }); }
      catch (error) { finish(false, error); return; }
      Promise.resolve().then(() => wait(milliseconds)).then(() => finish(true), (error) => finish(false, error));
    });
  };
  for (let poll = 0; poll < maxPolls; poll += 1) {
    if (aborted()) return [];
    const remaining = deadline - Date.now();
    if (remaining <= 0) return [];
    let waited;
    try { waited = await waitWithAbort(Math.min(KOKUCHPRO_ENTRY_POLL_INTERVAL_MS, remaining)); }
    catch { return []; }
    if (!waited || aborted()) return [];
    const current = await inspect();
    if (aborted()) return [];
    if (Array.isArray(current) && current.length === expectedLength) return current;
  }
  return [];
}

async function inspectPageControls(input = {}) {
  const page = input.page;
  if (!page || typeof page.locator !== "function") invalid();
  const provider = String(input.provider || "");
  const selector = provider === "eventbrite"
    ? '[data-testid="conversion-bar-checkout-button"]'
    : provider === "techplay"
    ? "input, textarea, select, button, [role=checkbox]"
    : provider === "kokuchpro"
    ? KOKUCHPRO_ENTRY_SELECTOR
    : provider === "doorkeeper"
    ? "input, textarea, select, button, a[role=button], a#confirm-button, a[href=\"#new_registration_modal\"]"
    : "input, textarea, select, button, a[role=button], a#confirm-button";
  let locator;
  try { locator = page.locator(selector); } catch { locator = null; }
  if (!locator || typeof locator.evaluateAll !== "function") invalid();
  const href = (() => { try { return String(typeof page.url === "function" ? page.url() : ""); } catch { return ""; } })();
  if (provider === "techplay") {
    const binding = input.candidate == null ? null : candidateTechPlayBinding(input.candidate);
    if (input.candidate != null && !binding) return Object.freeze([]);
    const eventId = binding?.eventId || String(input.event_id || "");
    const canonicalUrl = binding?.canonicalUrl || String(input.canonical_url || input.candidate_url || "");
    const ticketId = binding?.ticketId || String(input.ticket_id || input.candidate_ticket_id || "");
    if ((input.event_id != null && String(input.event_id) !== eventId) || (input.canonical_url != null && String(input.canonical_url) !== canonicalUrl) || (input.ticket_id != null && String(input.ticket_id) !== ticketId)) return Object.freeze([]);
    const confirm = /^https:\/\/techplay\.jp\/event\/join\/[1-9][0-9]*\/confirm$/.test(href);
    const observed = await locator.evaluateAll(confirm ? inspectTechPlayConfirm : inspectTechPlayInput, { href, eventId, canonicalUrl, ticketId });
    const afterHref = (() => { try { return String(typeof page.url === "function" ? page.url() : ""); } catch { return ""; } })();
    return href === afterHref && Array.isArray(observed) ? Object.freeze(observed.map(safeControl)) : Object.freeze([]);
  }
  if (provider === "kokuchpro") {
    const binding = candidateKokuchProBinding(input.candidate);
    if (!binding
      || (input.canonical_url != null && String(input.canonical_url) !== binding.canonicalUrl)
      || (input.ticket_id != null && String(input.ticket_id) !== binding.ticketId)) return Object.freeze([]);
    const context = { href, canonicalUrl: binding.canonicalUrl, eventRef: binding.eventRef, eventKey: binding.eventKey, ticketId: binding.ticketId };
    const observed = href === binding.canonicalUrl
      ? await inspectKokuchProEntryWhenReady(page, locator, { ...context, token: kokuchProEntryToken(binding) }, { signal: input.signal, sleep: input.sleep, inspector: inspectKokuchProCanonical, expectedLength: 1 })
      : href === `${binding.canonicalUrl}entry/`
      ? await inspectKokuchProEntryWhenReady(page, locator, { ...context, seatToken: kokuchProSeatToken(binding), finalToken: kokuchProFinalToken(binding) }, { signal: input.signal, sleep: input.sleep })
      : [];
    const afterHref = (() => { try { return String(typeof page.url === "function" ? page.url() : ""); } catch { return ""; } })();
    return href === afterHref && Array.isArray(observed) ? Object.freeze(observed.map(safeControl)) : Object.freeze([]);
  }
  const eventId = String(input.event_id || "");
  const canonicalUrl = String(input.canonical_url || input.candidate_url || "");
  const connpassJoin = isConnpassJoin(provider, href);
  const eventbriteUrl = EVENTBRITE_EVENT_URL.exec(canonicalUrl);
  const eventbriteEventId = eventbriteUrl && (eventbriteUrl[1] || eventbriteUrl[2]);
  if (provider === "eventbrite" && (!eventbriteEventId || eventbriteEventId !== eventId)) return Object.freeze([]);
  if (provider === "eventbrite") {
    const { official, matching } = eventbriteCheckoutFrameSet(page, eventId);
    if (official.length > 0) {
      if (href !== canonicalUrl || official.length !== 1 || matching.length !== 1) return Object.freeze([]);
      const ticket = await inspectEventbriteTicketFrame(matching[0].frame, eventId);
      if (!ticket) return Object.freeze([]);
      if (ticket.control) return Object.freeze([{ control: ticket.control, kind: "button", label: "Register", required: false, completed: false, submittable: true }]);
      if (ticket.cardCount !== 0) return Object.freeze([]);
      const attendee = await inspectEventbriteAttendeeFrame(matching[0].frame, eventId);
      if (!Array.isArray(attendee)) return Object.freeze([]);
      return Object.freeze(attendee.map(safeControl));
    }
  }
  const observed = await locator.evaluateAll((elements, context) => {
    if (context && context.provider === "eventbrite" && Array.isArray(elements) && elements.length > 100) return [];
    const visibleElements = elements.slice(0, 100);
    const connpassJoin = Boolean(context && context.connpassJoin === true);
    const kindOf = (element) => {
      const tag = String(element.tagName || "").toLowerCase();
      const type = String(element.type || "").toLowerCase();
      return tag === "input" && ["checkbox", "radio"].includes(type)
        ? type : tag === "input" && ["button", "submit", "reset", "image"].includes(type) ? "button" : tag === "a" ? "link" : tag;
    };
    const submitTypeOf = (element) => {
      const tag = String(element.tagName || "").toLowerCase(); const type = String(element.type || "").toLowerCase();
      return tag === "button" ? !type || type === "submit" : ["submit", "image"].includes(type);
    };
    const groupOf = (element) => {
      if (typeof element.closest !== "function") return null;
      if (connpassJoin) return element.closest(".question_list");
      return element.closest("fieldset, dl.field, [role='group'], .field");
    };
    const radioNameOf = (element) => String((element.getAttribute && element.getAttribute("name")) || element.name || "");
    const requiredOf = (element) => {
      const group = groupOf(element);
      const radioName = radioNameOf(element);
      return connpassJoin && String(element.type || "").toLowerCase() === "radio" && radioName === "participation_type"
        || element.required === true || String((element.getAttribute && element.getAttribute("aria-required")) || "").toLowerCase() === "true" || Boolean(group && (((group.classList && typeof group.classList.contains === "function") && group.classList.contains("required")) || /(?:^|\s)required(?:\s|$)/.test(String(group.className || "")) || String((group.getAttribute && group.getAttribute("aria-required")) || "").toLowerCase() === "true"));
    };
    const labelOf = (element, allowKnownValue = false) => {
      const tag = String(element.tagName || "").toLowerCase(); const type = String(element.type || "").toLowerCase();
      const labels = Array.from(element.labels || []).map((label) => label.innerText || label.textContent || "");
      return [
        ...labels,
        element.getAttribute && element.getAttribute("aria-label"),
        element.getAttribute && element.getAttribute("placeholder"),
        element.getAttribute && element.getAttribute("name"),
        element.innerText,
        tag === "input" && (["submit", "image"].includes(type) || allowKnownValue) ? element.value : null,
      ].map((value) => String(value || "").replace(/\s+/g, " ").trim()).find(Boolean) || "";
    };
    const visibleOf = (element) => {
      const view = element.ownerDocument && element.ownerDocument.defaultView;
      const hiddenStyle = (style) => Boolean(style && (
        [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase()))
        || String(style.opacity || "") === "0"
      ));
      let current = element;
      while (current) {
        if (current.hidden === true || current.isConnected === false || (typeof current.hasAttribute === "function" && current.hasAttribute("hidden"))) return false;
        if (String((current.getAttribute && current.getAttribute("aria-hidden")) || "").toLowerCase() === "true") return false;
        if (hiddenStyle(current.style || {})) return false;
        let computed = null;
        try { computed = view && typeof view.getComputedStyle === "function" ? view.getComputedStyle(current) : null; } catch { computed = null; }
        if (hiddenStyle(computed)) return false;
        current = current.parentElement || null;
      }
      if (typeof element.getBoundingClientRect !== "function") return false;
      let rect;
      try { rect = element.getBoundingClientRect(); } catch { return false; }
      return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0);
    };
    const answerKinds = ["input", "textarea", "select", "checkbox", "radio"];
    const requiredAnswers = visibleElements.filter((element) => String(element.type || "").toLowerCase() !== "hidden" && element.disabled !== true && answerKinds.includes(kindOf(element)) && requiredOf(element));
    const requiredAnswersRepresentable = requiredAnswers.every((element) => !!labelOf(element));
    const completedOf = (element) => {
      const kind = kindOf(element);
      const radioName = radioNameOf(element);
      return ["input", "textarea", "select"].includes(kind) ? Boolean(String(element.value || "").trim()) : kind === "checkbox" ? element.checked === true : kind === "radio" ? radioName.trim() ? elements.some((candidate) => String(candidate.type || "").toLowerCase() === "radio" && String((candidate.getAttribute && candidate.getAttribute("name")) || candidate.name || "") === radioName && candidate.checked === true) : element.checked === true : false;
    };
    const doorkeeperFormIdOf = (form) => String(form && (form.id || (form.getAttribute && form.getAttribute("id")) || ""));
    const doorkeeperHrefOf = (element) => String((element.getAttribute && element.getAttribute("href")) || element.href || "");
    const doorkeeperTextOf = (element) => String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim();
    const doorkeeperTriggerOf = (element) => String(element.tagName || "").toLowerCase() === "a" && doorkeeperHrefOf(element) === "#new_registration_modal" && doorkeeperTextOf(element) === "申し込む";
    const doorkeeperVisibleOf = (element) => {
      if (!visibleOf(element)) return false;
      let ancestor = element.parentElement || null;
      while (ancestor) {
        if (typeof ancestor.getBoundingClientRect === "function") {
          let rect;
          try { rect = ancestor.getBoundingClientRect(); } catch { return false; }
          if (!rect || Number(rect.width) <= 0 || Number(rect.height) <= 0) return false;
        }
        ancestor = ancestor.parentElement || null;
      }
      return true;
    };
    const doorkeeperVisibleElements = visibleElements.filter(doorkeeperVisibleOf);
    if (context && context.provider === "eventbrite") {
      if (!context.eventId || !context.canonicalUrl || String(context.href || "") !== String(context.canonicalUrl)) return [];
      const testIdOf = (element) => String((element.getAttribute && element.getAttribute("data-testid")) || (element.dataset && element.dataset.testid) || "");
      const visibleEventbriteCtas = visibleElements.filter((element) => testIdOf(element) === "conversion-bar-checkout-button" && visibleOf(element));
      if (visibleEventbriteCtas.length !== 1) return [];
      const [element] = visibleEventbriteCtas;
      const eventbriteCta = [element].filter((element) => {
        const tag = String(element.tagName || "").toLowerCase();
        const type = String(element.type || "").toLowerCase();
        const ariaDisabled = String((element.getAttribute && element.getAttribute("aria-disabled")) || "").toLowerCase() === "true";
        return tag === "button" && type === "button" && testIdOf(element) === "conversion-bar-checkout-button"
          && new Set(["Get tickets", "Reserve a spot"]).has(String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim())
          && element.disabled !== true && !ariaDisabled;
      });
      if (eventbriteCta.length !== 1) return [];
      const control = `eventbrite_checkout_${context.eventId}`;
      if (element.dataset) element.dataset.lmConnectorControl = control;
      return [{ control, kind: "button", label: String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim(), required: false, completed: false, submittable: true }];
    }
    const doorkeeperPageMatch = /^https:\/\/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.doorkeeper\.jp\/events\/([1-9][0-9]*)$/.exec(String(context && context.href || ""));
    const knownDoorkeeperPage = Boolean(context && context.provider === "doorkeeper" && context.eventId && doorkeeperPageMatch && doorkeeperPageMatch[1] !== "www" && doorkeeperPageMatch[2] === String(context.eventId));
    if (context && context.provider === "doorkeeper") {
      if (!knownDoorkeeperPage) return [];
      const idOf = (element) => String(element.id || (element.getAttribute && element.getAttribute("id")) || "");
      const nameOf = (element) => String((element.getAttribute && element.getAttribute("name")) || element.name || "");
      const requiredEmailOf = (element) => String(element.tagName || "").toLowerCase() === "input"
        && String(element.type || "").toLowerCase() === "email"
        && idOf(element) === "event_registration_email"
        && nameOf(element) === "event_registration[email]"
        && element.required === true
        && doorkeeperFormIdOf(element.form) === "new_event_registration";
      const submitOf = (element) => String(element.tagName || "").toLowerCase() === "input"
        && String(element.type || "").toLowerCase() === "submit"
        && nameOf(element) === "commit"
        && String(element.value || "") === "申し込む";
      const triggerCandidates = doorkeeperVisibleElements.filter(doorkeeperTriggerOf);
      const emailCandidates = doorkeeperVisibleElements.filter(requiredEmailOf);
      const requiredAnswers = doorkeeperVisibleElements.filter((element) => element.disabled !== true && ["input", "textarea", "select", "checkbox", "radio"].includes(kindOf(element)) && requiredOf(element));
      const answerForms = new Set(requiredAnswers.map((element) => element.form).filter(Boolean));
      const registrationForm = answerForms.size === 1 ? answerForms.values().next().value : null;
      const requiredAnswersComplete = requiredAnswers.length === 1 && requiredAnswers.every(completedOf);
      const requiredAnswersRepresentable = requiredAnswers.length === 1 && emailCandidates.length === 1 && requiredAnswers[0] === emailCandidates[0];
      const requiredAnswersBound = Boolean(registrationForm && requiredAnswers.every((element) => element.form === registrationForm) && doorkeeperFormIdOf(registrationForm) === "new_event_registration");
      const submitCandidates = doorkeeperVisibleElements.filter(submitOf);
      const submitReady = requiredAnswersRepresentable && requiredAnswersComplete && requiredAnswersBound && submitCandidates.length === 1 && submitCandidates[0].form === registrationForm;
      const publicControl = (element, kind, label, required, completed, submittable) => {
        const index = visibleElements.indexOf(element);
        const control = `control_${index + 1}`;
        if (element.dataset) element.dataset.lmConnectorControl = control;
        return { control, kind, label, required, completed, submittable };
      };
      if (triggerCandidates.length !== 1) return [];
      const controls = [];
      controls.push(publicControl(triggerCandidates[0], "link", "申し込む", false, false, false));
      for (const email of emailCandidates) controls.push(publicControl(email, "input", "Email", true, completedOf(email), false));
      for (const submit of submitCandidates) controls.push(publicControl(submit, "button", "申し込む", false, false, submitReady && submit === submitCandidates[0]));
      return controls;
    }
    const requiredAnswersComplete = requiredAnswers.every(completedOf);
    const answerForms = new Set(requiredAnswers.map((element) => element.form).filter(Boolean));
    const registrationForm = answerForms.size === 1 ? answerForms.values().next().value : null;
    const requiredAnswerFormUnique = answerForms.size <= 1;
    const requiredAnswersBound = Boolean(registrationForm && requiredAnswers.every((element) => element.form === registrationForm));
    const idOf = (element) => String(element.id || (element.getAttribute && element.getAttribute("id")) || "");
    const knownSubmitCount = visibleElements.filter((element) => idOf(element) === "form-submit-button").length;
    const formPageMatch = /^https:\/\/peatix\.com\/sales\/event\/([1-9][0-9]*)\/form$/.exec(String(context && context.href || ""));
    const knownPage = Boolean(context && context.provider === "peatix" && formPageMatch && (!context.eventId || formPageMatch[1] === String(context.eventId)));
    const confirmPageMatch = /^https:\/\/peatix\.com\/sales\/event\/([1-9][0-9]*)\/confirm$/.exec(String(context && context.href || ""));
    const knownConfirmPage = Boolean(context && context.provider === "peatix" && context.eventId && confirmPageMatch && confirmPageMatch[1] === String(context.eventId));
    const knownConfirmCount = visibleElements.filter((element) => idOf(element) === "confirm-button").length;
    const submitCounts = new Map();
    for (const element of visibleElements) {
      const kind = kindOf(element);
      if (kind === "button" && element.disabled !== true && !!element.form && element.form === registrationForm && submitTypeOf(element)) submitCounts.set(element.form, (submitCounts.get(element.form) || 0) + 1);
    }
    return visibleElements.flatMap((element, index) => {
      const tag = String(element.tagName || "").toLowerCase();
      const type = String(element.type || "").toLowerCase();
      if (type === "hidden" || element.disabled === true) return [];
      const knownPeatixConfirm = knownConfirmPage && requiredAnswersRepresentable && requiredAnswersComplete && requiredAnswerFormUnique && requiredAnswersBound && !element.form && visibleOf(element) && tag === "a" && idOf(element) === "confirm-button" && knownConfirmCount === 1 && element.disabled !== true && labelOf(element) === "チケットを申し込む";
      const kind = knownPeatixConfirm ? "button" : kindOf(element);
      if (!["input", "textarea", "select", "checkbox", "radio", "button", "link"].includes(kind)) return [];
      const knownPeatixSubmit = knownPage && requiredAnswersRepresentable && tag === "input" && type === "button" && idOf(element) === "form-submit-button" && element.disabled !== true && !!registrationForm && knownSubmitCount === 1 && labelOf(element, true) === "確認画面へ進む";
      const label = labelOf(element, knownPeatixSubmit);
      if (!label) return [];
      const control = `control_${index + 1}`;
      element.dataset.lmConnectorControl = control;
      const group = groupOf(element);
      const radioName = radioNameOf(element);
      const questionElement = connpassJoin && group && typeof group.querySelector === "function" ? group.querySelector(":scope > .question") : null;
      const question = ["checkbox", "radio"].includes(kind) && connpassJoin
        ? kind === "radio" && radioName === "participation_type"
          ? "参加枠"
          : String(questionElement?.textContent || questionElement?.innerText || "").replace(/\s+/g, " ").trim().replace(/^(?:必須|任意)\s*/, "").trim()
        : ["checkbox", "radio"].includes(kind) && group && typeof group.querySelector === "function"
          ? String(group.querySelector("legend,dt,[data-question]")?.textContent || "").replace(/\s+/g, " ").trim()
          : "";
      const completed = completedOf(element);
      const required = requiredOf(element);
      const submittable = requiredAnswersRepresentable && (knownPeatixSubmit || knownPeatixConfirm || kind === "button" && !!element.form && element.form === registrationForm && submitTypeOf(element) && submitCounts.get(element.form) === 1);
      return [{ control, kind, label, required, completed, submittable, ...(question ? { question } : {}) }];
    });
  }, { provider, href, eventId, canonicalUrl, connpassJoin });
  if (!Array.isArray(observed)) invalid();
  return Object.freeze(observed.map(safeControl));
}

function eventbriteMarketingInputName(key) {
  return key === "organization" ? "organizationMarketingOptIn" : key === "eventbrite" ? "ebMarketingOptIn" : "";
}

async function operateEventbriteMarketing(target, token, page) {
  const match = EVENTBRITE_MARKETING_CONTROL.exec(String(token || ""));
  if (!match || !target || !page || typeof target.locator !== "function") return null;
  const name = eventbriteMarketingInputName(match[1]);
  if (!name) return null;
  let locator;
  try { locator = target.locator(`input[data-lm-connector-control="${token}"][name="${name}"]`); } catch { return null; }
  if (!locator || (typeof locator.elementHandles !== "function" && typeof locator.elementHandle !== "function")) return null;
  let handles;
  try {
    if (typeof locator.elementHandles === "function") handles = await locator.elementHandles();
    else {
      const handle = await locator.elementHandle();
      handles = handle ? [handle] : [];
    }
  } catch { return null; }
  if (!Array.isArray(handles) || handles.length !== 1) return null;
  const handle = handles[0];
  const initial = await readEventbriteMarketingHandle(handle, { token, name, checked: true });
  if (!initial || !initial.id || !validEventbriteMarketingHandleState(initial, { token, name, id: initial.id, checked: true })) return null;
  if (typeof locator.count !== "function") return null;
  let count;
  try { count = await locator.count(); } catch { return null; }
  if (count !== 1) return null;
  const rebound = await readEventbriteMarketingHandle(handle, { token, name, id: initial.id, checked: true });
  if (!rebound || !validEventbriteMarketingHandleState(rebound, { token, name, id: initial.id, checked: true })) return null;
  if (typeof handle.press !== "function") return null;
  try {
    await handle.press("Space");
    return Object.freeze({ page, frame: target, handle, eventId: match[2], token, name, id: initial.id });
  } catch { return null; }
}

function validEventbriteFinalHandleState(state, { token, testId } = {}) {
  return Boolean(state && state.tag === "button" && state.type === "button" && state.testId === testId && state.token === token
    && state.text === "Register" && state.visible === true && state.enabled === true && state.connected === true);
}
function validTechPlayFinalHandleState(state, { token, id } = {}) {
  return Boolean(state && state.tag === "button" && state.type === "button" && state.text === TECHPLAY_FINAL_LABEL
    && state.id === id && state.token === token && state.visible === true && state.enabled === true && state.connected === true);
}
async function operateEventbriteFinal(target, token, page, { eventId, canonicalUrl, signal, beforeDispatch } = {}) {
  if (!target || !page || !EVENTBRITE_ATTENDEE_REGISTER_CONTROL.test(String(token || "")) || typeof target.locator !== "function") return null;
  if (eventId && canonicalUrl && eventbriteTicketFrame(page, eventId, canonicalUrl) !== target) return null;
  const testId = "eds-modal__primary-button";
  let locator;
  try { locator = target.locator(`button[data-lm-connector-control="${token}"][data-testid="${testId}"]`); } catch { return null; }
  if (!locator || (typeof locator.elementHandles !== "function" && typeof locator.elementHandle !== "function") || typeof locator.count !== "function") return null;
  let handles;
  try {
    if (typeof locator.elementHandles === "function") handles = await locator.elementHandles();
    else { const handle = await locator.elementHandle(); handles = handle ? [handle] : []; }
  } catch { return null; }
  if (signal && signal.aborted) return null;
  if (!Array.isArray(handles) || handles.length !== 1) return null;
  const handle = handles[0];
  const initial = await readEventbriteMarketingHandle(handle, { token });
  if (signal && signal.aborted) return null;
  if (!validEventbriteFinalHandleState(initial, { token, testId })) return null;
  try { if (await locator.count() !== 1) return null; } catch { return null; }
  if (signal && signal.aborted) return null;
  const rebound = await readEventbriteMarketingHandle(handle, { token });
  if (signal && signal.aborted) return null;
  if (!validEventbriteFinalHandleState(rebound, { token, testId })) return null;
  if (typeof handle.click !== "function") return null;
  if (eventId && canonicalUrl && eventbriteTicketFrame(page, eventId, canonicalUrl) !== target) return null;
  if (signal && signal.aborted) return null;
  if (typeof beforeDispatch === "function") beforeDispatch();
  if (signal && signal.aborted) return null;
  try { await handle.click(); } catch { return { attempted: true }; }
  return Object.freeze({ page, frame: target, handle, eventId: EVENTBRITE_ATTENDEE_REGISTER_CONTROL.exec(token)[1], token, testId });
}

async function readKokuchProEntryHandle(handle, context = {}) {
  if (!handle || typeof handle.evaluate !== "function") return null;
  try {
    return await handle.evaluate((element, expected) => {
      const tag = (value) => String(value && value.tagName || "").toLowerCase();
      const attr = (value, name) => {
        try {
          const property = value && value[name];
          if (property != null && String(property) !== "") return String(property);
          const result = value?.getAttribute?.(name);
          return result == null ? "" : String(result);
        } catch { return ""; }
      };
      const text = (value) => String(value && (value.innerText || value.textContent) || "").replace(/\s+/g, " ").trim();
      const hiddenStyle = (style) => Boolean(style && [style.display, style.visibility, style.contentVisibility].some((value) => ["none", "hidden", "collapse"].includes(String(value || "").toLowerCase())) || String(style && style.opacity || "") === "0");
      const visible = (value) => {
        let current = value; const view = value?.ownerDocument?.defaultView;
        while (current) {
          if (current.isConnected !== true || current.hidden === true || attr(current, "aria-hidden").toLowerCase() === "true" || hiddenStyle(current.style)) return false;
          let computed = null; try { computed = view?.getComputedStyle?.(current); } catch { return false; }
          if (hiddenStyle(computed)) return false;
          current = current.parentElement || null;
        }
        try { const rect = value?.getBoundingClientRect?.(); return Boolean(rect && Number(rect.width) > 0 && Number(rect.height) > 0); } catch { return false; }
      };
      if (!element || !["button", "input", "label"].includes(tag(element))) return null;
      const labelSeat = tag(element) === "label" ? (element.control || element.querySelector?.('input[type="radio"][name="data[Entry][seat]"]')) : null;
      const form = labelSeat?.form || element.form || element.closest?.("form");
      const controls = form?.elements != null ? Array.from(form.elements) : Array.from(form?.querySelectorAll?.("input, textarea, select, button") || []);
      const pageForms = (() => { try { const values = Array.from(element.ownerDocument?.querySelectorAll?.("form") || []); return values.length ? values : [form]; } catch { return [form]; } })();
      const entryForms = pageForms.filter((value) => attr(value, "action") === `${String(expected?.canonicalUrl || "")}entry/` && attr(value, "method").toUpperCase() === "POST");
      const methodFields = controls.filter((value) => tag(value) === "input" && attr(value, "name") === "_method");
      const identities = [
        ["EntryEmail", "data[Entry][email]"], ["EntryName", "data[Entry][name]"], ["EntryNamePublish", "data[Entry][name_publish]"],
      ].map(([id, name]) => controls.filter((value) => tag(value) === "input" && attr(value, "type").toLowerCase() === "hidden" && attr(value, "id") === id && attr(value, "name") === name));
      const messages = controls.filter((value) => attr(value, "id") === "EntryMessage" || attr(value, "name") === "data[Entry][message]");
      const spinnerName = /^data\[Entry\]\[seat[1-9][0-9]*\]$/;
      const valueOf = (value) => { try { return value && value.value != null ? String(value.value) : attr(value, "value"); } catch { return ""; } };
      const spinnerClasses = ["form-control", "spinner", "ui-spinner-input"];
      const radioSeats = controls.filter((value) => tag(value) === "input" && attr(value, "type").toLowerCase() === "radio" && attr(value, "name") === "data[Entry][seat]");
      const spinnerSeats = controls.filter((value) => tag(value) === "input" && spinnerName.test(attr(value, "name")));
      const spinner = spinnerSeats.length === 1 ? spinnerSeats[0] : null;
      const spinnerClassValid = Boolean(spinner && attr(spinner, "type").toLowerCase() === "text" && attr(spinner, "class").trim().split(/\s+/).length === spinnerClasses.length && new Set(attr(spinner, "class").trim().split(/\s+/)).size === spinnerClasses.length && spinnerClasses.every((name) => attr(spinner, "class").trim().split(/\s+/).includes(name)));
      const spinnerMax = attr(spinner, "data-spinner-max");
      const spinnerShapeValid = Boolean(spinnerClassValid && attr(spinner, "data-spinner-min") === "0" && /^[1-9][0-9]*$/.test(spinnerMax) && Number.isSafeInteger(Number(spinnerMax)) && Number(spinnerMax) > 0 && ["0", "1"].includes(valueOf(spinner)));
      const seatVariant = radioSeats.length === 1 && spinnerSeats.length === 0 ? "radio" : spinnerSeats.length === 1 && radioSeats.length === 0 && spinnerShapeValid ? "spinner" : "";
      const seat = labelSeat || (seatVariant === "radio" ? radioSeats[0] : seatVariant === "spinner" ? spinner : null);
      const seatLabels = radioSeats.length === 1 ? Array.from(radioSeats[0].labels || []) : []; const seatLabel = seatLabels.length === 1 ? seatLabels[0] : null;
      const seatLabelVisible = Boolean(seatLabel && (seatLabel.control === radioSeats[0] || seatLabel.contains?.(radioSeats[0])) && visible(seatLabel));
      const seatValue = seatVariant === "spinner" ? valueOf(spinner) : "";
      const seatChecked = seatVariant === "radio" ? radioSeats[0].checked === true : seatVariant === "spinner" ? seatValue === "1" : false;
      const seatVisible = Boolean(seat && visible(seat));
      const seatEnabled = Boolean(seat && seat.disabled !== true && attr(seat, "aria-disabled").toLowerCase() !== "true" && attr(seat, "aria-enabled").toLowerCase() !== "false");
      const identityReady = identities.every((fields) => fields.length === 1 && Boolean(String(attr(fields[0], "value")).trim()));
      const messageValid = messages.length <= 1 && (messages.length === 0 || (tag(messages[0]) === "textarea" && attr(messages[0], "id") === "EntryMessage" && attr(messages[0], "name") === "data[Entry][message]"));
      const finals = controls.filter((value) => tag(value) === "button" && attr(value, "id") === "entry_submit_button" && attr(value, "type").toLowerCase() === "submit");
      const final = finals.length === 1 ? finals[0] : null;
      const finalVisible = Boolean(final && visible(final));
      const finalEnabled = Boolean(final && final.disabled !== true && attr(final, "aria-disabled").toLowerCase() !== "true" && attr(final, "aria-enabled").toLowerCase() !== "false");
      const finalValid = Boolean(final && final.isConnected === true && finalVisible && finalEnabled && text(final) === "申込む" && final.dataset);
      const seatControl = radioSeats.length === 1 ? seatLabel : spinner;
      const extraVisibleActionable = controls.some((value) => value !== methodFields[0] && !identities.flat().includes(value) && !messages.includes(value) && value !== radioSeats[0] && value !== spinner && value !== final && visible(value) && ["input", "textarea", "select", "button"].includes(tag(value)));
      return {
        tag: tag(element), type: attr(labelSeat || element, "type").toLowerCase(), id: attr(element, "id"), name: attr(labelSeat || element, "name"), label: text(element),
        formAction: attr(form, "action"), formMethod: attr(form, "method").toUpperCase(), entryFormCount: entryForms.length,
        methodCount: methodFields.length, methodType: attr(methodFields[0], "type").toLowerCase(), methodName: attr(methodFields[0], "name"), methodValue: attr(methodFields[0], "value"),
        identityReady, identityShapeValid: identityReady, messageCount: messages.length, messageValid,
        seatCount: radioSeats.length + spinnerSeats.length, seatVariant, seatChecked, seatLabelVisible, seatVisible, seatEnabled,
        seatToken: String(seatControl?.dataset?.lmConnectorControl || ""),
        spinnerShapeValid, spinnerValue: seatValue, spinnerClass: spinner ? attr(spinner, "class") : "", spinnerMin: spinner ? attr(spinner, "data-spinner-min") : "", spinnerMax: spinner ? spinnerMax : "",
        finalCount: finals.length, finalId: attr(final, "id"), finalType: attr(final, "type").toLowerCase(), finalLabel: text(final), finalVisible, finalEnabled, finalConnected: final?.isConnected === true, finalValid, finalToken: String(final?.dataset?.lmConnectorControl || ""), extraVisibleActionable,
        token: String(element.dataset?.lmConnectorControl || ""), visible: visible(element),
        enabled: (labelSeat || element).disabled !== true && attr(labelSeat || element, "aria-disabled").toLowerCase() !== "true" && attr(labelSeat || element, "aria-enabled").toLowerCase() !== "false",
        connected: element.isConnected === true && (!labelSeat || labelSeat.isConnected === true) && (!seat || seat.isConnected === true), ticketId: String(expected?.ticketId || ""), expectedToken: String(expected?.token || ""),
      };
    }, context);
  } catch { return null; }
}

function validKokuchProFormContract(state, { canonicalUrl, seatToken, finalToken } = {}) {
  return Boolean(state
    && state.formAction === `${canonicalUrl}entry/` && state.formMethod === "POST" && state.entryFormCount === 1
    && state.methodCount === 1 && state.methodType === "hidden" && state.methodName === "_method" && state.methodValue === "POST"
    && state.identityReady === true && state.identityShapeValid === true
    && state.messageCount >= 0 && state.messageCount <= 1 && state.messageValid === true
    && state.seatCount === 1 && ["radio", "spinner"].includes(state.seatVariant) && state.seatToken === seatToken
    && state.finalCount === 1 && state.finalId === "entry_submit_button" && state.finalType === "submit" && state.finalLabel === "申込む"
    && state.finalVisible === true && state.finalEnabled === true && state.finalConnected === true && state.finalValid === true && state.finalToken === finalToken
    && state.extraVisibleActionable === false);
}

function validKokuchProEntryHandleState(state, { canonicalUrl, ticketId, token, seatToken } = {}) {
  return Boolean(validKokuchProFormContract(state, { canonicalUrl, seatToken, finalToken: token })
    && state.tag === "button" && state.type === "submit" && state.label === "申込む"
    && state.id === "entry_submit_button"
    && state.seatChecked === true
    && (state.seatVariant !== "spinner" || state.spinnerShapeValid === true && state.spinnerValue === "1" && state.seatVisible === true && state.seatEnabled === true)
    && state.token === token && state.visible === true && state.enabled === true && state.connected === true);
}

function validKokuchProSeatControl(control, binding, token) {
  return Boolean(control && binding && token === kokuchProSeatToken(binding) && control.control === token && control.kind === "radio" && control.required === true && control.completed === false && control.submittable === false);
}
function validKokuchProSpinnerControl(control, binding, token) {
  return Boolean(control && binding && token === kokuchProSeatToken(binding) && control.control === token && control.kind === "input" && control.label === "参加人数" && control.required === true && control.completed === false && control.submittable === false);
}
function validKokuchProFinalControl(control, binding, token) {
  return Boolean(control && binding && token === kokuchProFinalToken(binding) && control.control === token && control.kind === "button" && control.label === "申込む" && control.required === false && control.completed === false && control.submittable === true);
}

function validKokuchProSeatHandleState(state, { canonicalUrl, token, finalToken, checked = false } = {}) {
  return Boolean(validKokuchProFormContract(state, { canonicalUrl, seatToken: token, finalToken })
    && state.tag === "label" && state.type === "radio" && state.name === "data[Entry][seat]"
    && state.seatVariant === "radio" && state.seatChecked === checked && state.seatLabelVisible === true
    && state.token === token && state.expectedToken === token && state.enabled === true && state.connected === true);
}

function validKokuchProSpinnerHandleState(state, { canonicalUrl, token, finalToken, value = "0" } = {}) {
  const classes = String(state?.spinnerClass || "").trim().split(/\s+/);
  const exactClasses = classes.length === KOKUCHPRO_SPINNER_CLASSES.length && new Set(classes).size === KOKUCHPRO_SPINNER_CLASSES.length && KOKUCHPRO_SPINNER_CLASSES.every((name) => classes.includes(name));
  return Boolean(validKokuchProFormContract(state, { canonicalUrl, seatToken: token, finalToken })
    && state.tag === "input" && state.type === "text" && KOKUCHPRO_SPINNER_NAME.test(state.name)
    && state.seatVariant === "spinner" && exactClasses && state.spinnerShapeValid === true && state.spinnerMin === "0" && /^[1-9][0-9]*$/.test(state.spinnerMax) && Number.isSafeInteger(Number(state.spinnerMax)) && Number(state.spinnerMax) > 0 && state.spinnerValue === value
    && state.seatVisible === true && state.seatEnabled === true && state.token === token && state.expectedToken === token && state.visible === true && state.enabled === true && state.connected === true);
}

async function operateKokuchProEntryRoute(input, token) {
  const binding = candidateKokuchProBinding(input.candidate), action = input.action || {}, control = input.control;
  if (!binding || kokuchProEntryToken(binding) !== token || action.purpose !== "submit" || action.method !== "ax_click" || !control || control.control !== token || control.kind !== "link" || control.label !== "申込む" || control.required !== false || control.completed !== false || control.submittable !== false) return Object.freeze({ status: "failed" });
  const current = () => { try { return String(input.page && typeof input.page.url === "function" ? input.page.url() : ""); } catch { return ""; } };
  if (input.signal?.aborted || current() !== binding.canonicalUrl || typeof input.page.goto !== "function") return Object.freeze({ status: "failed", ...(input.signal?.aborted ? { safe_reason: "time_limit" } : {}) });
  try { await input.page.goto(`${binding.canonicalUrl}entry/`, { waitUntil: "domcontentloaded" }); } catch { return Object.freeze({ status: "failed" }); }
  const after = candidateKokuchProBinding(input.candidate); return current() === `${binding.canonicalUrl}entry/` && after?.eventRef === binding.eventRef && after?.canonicalUrl === binding.canonicalUrl && after?.eventKey === binding.eventKey && after?.occurrenceId === binding.occurrenceId && after?.ticketId === binding.ticketId ? Object.freeze({ status: "success" }) : Object.freeze({ status: "failed" });
}

async function operateKokuchProEntry(input, target, token, beforeDispatch) {
  const binding = candidateKokuchProBinding(input.candidate), action = input.action || {}, control = input.control;
  const seat = validKokuchProSeatControl(control, binding, token);
  const spinner = validKokuchProSpinnerControl(control, binding, token);
  const final = validKokuchProFinalControl(control, binding, token);
  if (!seat && !spinner && !final) return Object.freeze({ status: "failed" });
  if (spinner && (action.purpose !== "fill" || action.method !== "ax_fill" || input.value !== "1")) return Object.freeze({ status: "failed" });
  const href = () => { try { return String(input.page && typeof input.page.url === "function" ? input.page.url() : ""); } catch { return ""; } };
  const sameCandidate = () => { const current = candidateKokuchProBinding(input.candidate); return Boolean(current && current.eventRef === binding.eventRef && current.canonicalUrl === binding.canonicalUrl && current.eventKey === binding.eventKey && current.occurrenceId === binding.occurrenceId && current.ticketId === binding.ticketId); };
  if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
  if (href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
  let formsLocator;
  try { formsLocator = target.locator(KOKUCHPRO_ENTRY_SELECTOR); } catch { return Object.freeze({ status: "failed" }); }
  if (!formsLocator || typeof formsLocator.evaluateAll !== "function") return Object.freeze({ status: "failed" });
  const controls = await inspectKokuchProEntryWhenReady(target, formsLocator, { href: href(), canonicalUrl: binding.canonicalUrl, eventRef: binding.eventRef, eventKey: binding.eventKey, seatToken: kokuchProSeatToken(binding), finalToken: kokuchProFinalToken(binding) }, { signal: input.signal, sleep: input.sleep });
  const selected = Array.isArray(controls) ? controls.find((item) => item.control === token) : null;
  const selectedValid = seat ? validKokuchProSeatControl(selected, binding, token) : spinner ? validKokuchProSpinnerControl(selected, binding, token) : validKokuchProFinalControl(selected, binding, token);
  if (!selectedValid || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
  let locator, count;
  try { locator = target.locator(`[data-lm-connector-control="${token}"]`); count = await locator?.count?.(); } catch { return Object.freeze({ status: "failed" }); }
  if (count !== 1 || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
  let handles = [];
  try {
    if (typeof locator.elementHandles === "function") handles = await locator.elementHandles();
    else if (typeof locator.elementHandle === "function") { const handle = await locator.elementHandle(); if (handle) handles = [handle]; }
  } catch { return Object.freeze({ status: "failed" }); }
  if (handles.length !== 1) return Object.freeze({ status: "failed" });
  if (spinner) {
    const handleContext = { canonicalUrl: binding.canonicalUrl, token, finalToken: kokuchProFinalToken(binding) };
    const initial = await readKokuchProEntryHandle(handles[0], handleContext);
    if (!validKokuchProSpinnerHandleState(initial, { ...handleContext, value: "0" }) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
    const stableSpinner = (state) => Boolean(state && state.name === initial.name && state.spinnerClass === initial.spinnerClass && state.spinnerMin === initial.spinnerMin && state.spinnerMax === initial.spinnerMax);
    let reboundCount; try { reboundCount = await locator.count(); } catch { return Object.freeze({ status: "failed" }); }
    if (reboundCount !== 1) return Object.freeze({ status: "failed" });
    const rebound = await readKokuchProEntryHandle(handles[0], handleContext);
    if (!validKokuchProSpinnerHandleState(rebound, { ...handleContext, value: "0" }) || !stableSpinner(rebound) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
    if (typeof handles[0].fill !== "function") return Object.freeze({ status: "failed" });
    try { await handles[0].fill("1"); } catch { return Object.freeze({ status: "failed" }); }
    const completed = await readKokuchProEntryHandle(handles[0], handleContext);
    if (!validKokuchProSpinnerHandleState(completed, { ...handleContext, value: "1" }) || !stableSpinner(completed) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
    return Object.freeze({ status: "success" });
  }
  if (seat) {
    const handleContext = { canonicalUrl: binding.canonicalUrl, token, finalToken: kokuchProFinalToken(binding) };
    const initial = await readKokuchProEntryHandle(handles[0], handleContext);
    if (!validKokuchProSeatHandleState(initial, handleContext) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate() || input.value !== true) return Object.freeze({ status: "failed" });
    let reboundCount; try { reboundCount = await locator.count(); } catch { return Object.freeze({ status: "failed" }); }
    if (reboundCount !== 1) return Object.freeze({ status: "failed" });
    const rebound = await readKokuchProEntryHandle(handles[0], handleContext);
    if (!validKokuchProSeatHandleState(rebound, handleContext) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
    if (typeof handles[0].click !== "function") return Object.freeze({ status: "failed" });
    try { await handles[0].click(); } catch { return Object.freeze({ status: "failed" }); }
    const completed = await readKokuchProEntryHandle(handles[0], handleContext);
    if (!validKokuchProSeatHandleState(completed, { ...handleContext, checked: true }) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
    return Object.freeze({ status: "success" });
  }
  const handleContext = { canonicalUrl: binding.canonicalUrl, ticketId: binding.ticketId, token, seatToken: kokuchProSeatToken(binding) };
  const initial = await readKokuchProEntryHandle(handles[0], handleContext);
  if (!validKokuchProEntryHandleState(initial, handleContext) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
  if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
  let reboundCount; try { reboundCount = await locator.count(); } catch { return Object.freeze({ status: "failed" }); }
  if (reboundCount !== 1 || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
  const rebound = await readKokuchProEntryHandle(handles[0], handleContext);
  if (!validKokuchProEntryHandleState(rebound, handleContext) || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed" });
  if (input.signal && input.signal.aborted || !beforeDispatch()) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
  let postCount; try { postCount = await locator.count(); } catch { return Object.freeze({ status: "failed", attempted: true }); }
  if (postCount !== 1 || href() !== `${binding.canonicalUrl}entry/` || !sameCandidate()) return Object.freeze({ status: "failed", attempted: true });
  const post = await readKokuchProEntryHandle(handles[0], handleContext);
  if (!validKokuchProEntryHandleState(post, handleContext)) return Object.freeze({ status: "failed", attempted: true });
  if (typeof handles[0].click !== "function") return Object.freeze({ status: "failed", attempted: true });
  try { await handles[0].click(); } catch { return Object.freeze({ status: "failed", attempted: true }); }
  return Object.freeze({ status: "success" });
}
async function operatePageControl(input = {}) {
  const target = input.frame || input.page;
  if (!input.page || !target || typeof target.locator !== "function" || !input.control || !input.action) invalid();
  const token = String(input.control.control || "");
  if (!CONTROL.test(token) || input.action.control !== token) invalid();
  const beforeDispatch = () => {
    if (input.signal && input.signal.aborted) return false;
    if (input.action.purpose === "submit" && typeof input.beforeDispatch === "function") input.beforeDispatch();
    return !(input.signal && input.signal.aborted);
  };
  if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
  if (input.provider === "kokuchpro") {
    const binding = candidateKokuchProBinding(input.candidate);
    if (binding && token === kokuchProEntryToken(binding)) return operateKokuchProEntryRoute(input, token);
    return operateKokuchProEntry(input, target, token, beforeDispatch);
  }
  if (input.provider === "techplay") {
    const binding = candidateTechPlayBinding(input.candidate); let href = "";
    try { href = String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { href = ""; }
    const bindingMatches = () => { const current = candidateTechPlayBinding(input.candidate); return Boolean(current && current.eventId === binding?.eventId && current.canonicalUrl === binding?.canonicalUrl && current.ticketId === binding?.ticketId); };
    const scalar = /^techplay_answer_[1-9][0-9]*$/.test(token) && input.control.kind === "input" && input.action.purpose === "fill" && input.action.method === "ax_fill";
    const radio = /^techplay_answer_[1-9][0-9]*_[1-9][0-9]*$/.test(token) && input.control.kind === "radio" && input.action.purpose === "fill" && input.action.method === "ax_check";
    const optout = /^techplay_optout_(?:area_[1-9][0-9]*|tag_[1-9][0-9]*|organizer_[1-9][0-9]*|icon_published|use_as_preset)$/.test(token) && input.control.kind === "checkbox" && input.action.purpose === "fill" && input.action.method === "ax_uncheck";
    const final = binding && token === `techplay_final_${binding.eventId}` && input.control.kind === "button" && input.control.label === TECHPLAY_FINAL_LABEL
      && input.control.required === false && input.control.completed === false && input.control.submittable === true
      && input.action.purpose === "submit" && input.action.method === "ax_click";
    const review = binding && /^techplay_review_[1-9][0-9]*$/.test(token) && token === `techplay_review_${binding.eventId}`
      && input.control.kind === "button" && input.control.label === TECHPLAY_REVIEW_LABEL
      && input.control.required === false && input.control.completed === false && input.control.submittable === true
      && input.action.purpose === "submit" && input.action.method === "ax_click";
    const joinHref = `${binding?.canonicalUrl.replace("/event/", "/event/join/") || ""}`;
    const confirmHref = `https://techplay.jp/event/join/${binding?.eventId || ""}/confirm`;
    if (!binding || (final ? href !== confirmHref : href !== joinHref)
      || (!review && !final && (input.control.required !== true || input.control.completed !== false || input.control.submittable !== false || (!scalar && !radio && !optout)))) return Object.freeze({ status: "failed" });
    let locator; try { locator = target.locator(`[data-lm-connector-control="${token}"]`); } catch { return Object.freeze({ status: "failed" }); }
    try {
      if (!locator || typeof locator.count !== "function") return Object.freeze({ status: "failed" });
      if (final) {
        let handles;
        try {
          if (typeof locator.elementHandles === "function") handles = await locator.elementHandles();
          else if (typeof locator.elementHandle === "function") { const handle = await locator.elementHandle(); handles = handle ? [handle] : []; }
          else handles = [];
        } catch { return Object.freeze({ status: "failed" }); }
        if (!Array.isArray(handles) || handles.length !== 1) return Object.freeze({ status: "failed" });
        const handle = handles[0];
        const initial = await readEventbriteMarketingHandle(handle, { token });
        if (!initial || !validTechPlayFinalHandleState(initial, { token, id: initial.id })) return Object.freeze({ status: "failed" });
        try { if (await locator.count() !== 1) return Object.freeze({ status: "failed" }); } catch { return Object.freeze({ status: "failed" }); }
        let currentHref = ""; try { currentHref = String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { return Object.freeze({ status: "failed" }); }
        if (currentHref !== confirmHref || !bindingMatches()) return Object.freeze({ status: "failed" });
        const rebound = await readEventbriteMarketingHandle(handle, { token });
        if (!rebound || !validTechPlayFinalHandleState(rebound, { token, id: initial.id }) || typeof handle.click !== "function") return Object.freeze({ status: "failed" });
        if (!beforeDispatch()) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
        try { await handle.click(); } catch { return Object.freeze({ status: "failed", attempted: true }); }
      } else if (review) {
        if (await locator.count() !== 1) return Object.freeze({ status: "failed" });
        if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
        let currentHref = ""; try { currentHref = String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { return Object.freeze({ status: "failed" }); }
        if (currentHref !== joinHref || typeof locator.click !== "function") return Object.freeze({ status: "failed" });
        if (!beforeDispatch()) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
        try { await locator.click(); } catch { return Object.freeze({ status: "failed", attempted: true }); }
      } else {
        if (await locator.count() !== 1) return Object.freeze({ status: "failed" });
        if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
        if (scalar) { if (typeof input.value !== "string" || typeof locator.fill !== "function") return Object.freeze({ status: "failed" }); await locator.fill(input.value); }
        else if (radio) { if (typeof locator.check !== "function") return Object.freeze({ status: "failed" }); await locator.check(); }
        else if (typeof locator.press === "function") await locator.press("Space");
        else if (typeof locator.click === "function") await locator.click();
        else return Object.freeze({ status: "failed" });
      }
    } catch { return Object.freeze({ status: "failed" }); }
    return Object.freeze({ status: "success" });
  }
  if (input.action.method === "ax_uncheck") {
    if (!eventbriteMarketingControlMeaning(input.control)) return Object.freeze({ status: "failed" });
    const operation = await operateEventbriteMarketing(target, token, input.page);
    return operation ? Object.freeze({ status: "success", [EVENTBRITE_MARKETING_OPERATION]: operation }) : Object.freeze({ status: "failed" });
  }
  if (input.action.method === "ax_click" && eventbriteFinalControlMeaning(input.control)) {
    const operation = await operateEventbriteFinal(target, token, input.page, {
      eventId: input.eventId,
      canonicalUrl: input.canonicalUrl,
      signal: input.signal,
      beforeDispatch: input.beforeDispatch,
    });
    return operation?.attempted ? Object.freeze({ status: "failed", [EVENTBRITE_FINAL_ATTEMPTED]: true })
      : operation ? Object.freeze({ status: "success", [EVENTBRITE_FINAL_OPERATION]: operation }) : Object.freeze({ status: "failed", [EVENTBRITE_FINAL_ATTEMPTED]: false });
  }
  const locator = target.locator(`[data-lm-connector-control="${token}"]`);
  if (!locator || typeof locator.count !== "function" || await locator.count() !== 1) {
    return Object.freeze({ status: "failed" });
  }
  if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
  switch (input.action.method) {
    case "ax_fill":
    case "dom_fill":
      if (typeof input.value !== "string" || typeof locator.fill !== "function") return Object.freeze({ status: "failed" });
      await locator.fill(input.value);
      break;
    case "ax_check":
      if (typeof locator.check !== "function") return Object.freeze({ status: "failed" });
      await locator.check();
      break;
    case "ax_select":
      if (typeof locator.selectOption !== "function") return Object.freeze({ status: "failed" });
      await locator.selectOption(input.value);
      break;
    case "ax_click":
    case "coordinate_click":
      if (typeof locator.click !== "function") return Object.freeze({ status: "failed" });
      if (!beforeDispatch()) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
      await locator.click();
      break;
    case "keyboard_submit":
      if (!input.page.keyboard || typeof input.page.keyboard.press !== "function") return Object.freeze({ status: "failed" });
      if (!beforeDispatch()) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
      await input.page.keyboard.press("Enter");
      break;
    case "ax_inspect":
    case "dom_inspect":
    case "parent_readback":
      break;
    default:
      return Object.freeze({ status: "failed" });
  }
  return Object.freeze({ status: "success" });
}

function normalizedLabel(value) {
  return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
}
function connpassSafeRadioCategory(control) {
  if (!control || control.kind !== "radio") return null;
  const label = normalizedLabel(control.label);
  const question = normalizedLabel(control.question);
  if (CONNPASS_ONLINE_LABEL.test(label)) return question === normalizedLabel("参加枠") ? "online" : null;
  return label === "connpass" && question === normalizedLabel(CONNPASS_REFERRAL_QUESTION) ? "referral" : null;
}

function nativeConnpassControl(provider, state, controls) {
  if (provider !== "connpass" || state !== "connpass_join") return null;
  const pending = controls.filter((control) => ACTIONABLE_KINDS.has(control.kind) && control.required && !control.completed);
  if (pending.length > 0) {
    for (const category of ["online", "referral"]) {
      const matches = pending.filter((control) => connpassSafeRadioCategory(control) === category);
      if (matches.length > 0) return matches.length === 1 ? matches[0].control : null;
    }
    return null;
  }
  const submits = controls.filter((control) => control.kind === "button" && control.submittable === true && normalizedLabel(control.label) === normalizedLabel(CONNPASS_FINAL_LABEL));
  return submits.length === 1 ? submits[0].control : null;
}
function nativeKokuchProControl(provider, controls) {
  if (provider !== "kokuchpro" || !Array.isArray(controls)) return null;
  const pendingSeat = controls.filter((control) => KOKUCHPRO_SEAT_TOKEN.test(String(control?.control || "")) && ((control.kind === "radio") || (control.kind === "input" && control.label === "参加人数")) && control.required === true && control.completed === false && control.submittable === false);
  if (pendingSeat.length === 1) return pendingSeat[0].control;
  const route = controls.filter((control) => KOKUCHPRO_ENTRY_TOKEN.test(String(control?.control || "")) && control.kind === "link" && control.label === "申込む" && control.required === false && control.completed === false && control.submittable === false);
  if (route.length === 1 && controls.length === 1) return route[0].control;
  const final = controls.filter((control) => KOKUCHPRO_FINAL_TOKEN.test(String(control?.control || "")) && control.kind === "button" && control.label === "申込む" && control.required === false && control.completed === false && control.submittable === true);
  return final.length === 1 && controls.length === 2 && pendingSeat.length === 0 ? final[0].control : null;
}
function nativeDoorkeeperTrigger(provider, controls) {
  if (provider !== "doorkeeper") return null;
  if (controls.some((control) => ACTIONABLE_KINDS.has(control.kind) && control.required && !control.completed)) return null;
  if (controls.some((control) => control.kind === "button" && control.submittable === true)) return null;
  const triggers = controls.filter(isDoorkeeperTriggerControl);
  const semanticTriggers = controls.filter(isDoorkeeperTriggerSemantic);
  return triggers.length === 1 && semanticTriggers.length === 1 ? triggers[0].control : null;
}

async function safeProfile(read) { try { const value = await read(); return value && typeof value === "object" && !Array.isArray(value) ? value : null; } catch { return null; } } function answerFor(profile, label) { const answers = profile && profile.form_answers; const value = answers && typeof answers === "object" && !Array.isArray(answers) ? Object.entries(answers).find(([key]) => normalizedLabel(key) === label)?.[1] : null; return typeof value === "string" || Array.isArray(value) ? value : null; }
function approvedOption(profile, question, label) { const answers = profile && profile.form_answers; const exactQuestion = normalizedLabel(question); if (!exactQuestion) return false; const value = answers && typeof answers === "object" && !Array.isArray(answers) && Object.entries(answers).find(([key]) => normalizedLabel(key) === exactQuestion)?.[1]; return typeof value === "string" ? normalizedLabel(value) === label : Array.isArray(value) && value.some((item) => typeof item === "string" && normalizedLabel(item) === label); }

function techPlayPrivateString(value) { return typeof value === "string" && value.length > 0 && value.length <= 2_000 && value === value.trim() && !/[\x00-\x1f\x7f]/.test(value) ? value : null; }
function techPlayExactAnswer(profile, key) { try { const answers = profile && profile.form_answers; return answers && typeof answers === "object" && !Array.isArray(answers) && Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : null; } catch { return null; } }
function techPlayPrivateAge(profile, now) {
  try {
    const answers = profile && profile.form_answers; if (!answers || typeof answers !== "object" || Array.isArray(answers)) return null;
    const keys = ["生年月日", "Date of Birth"].filter((key) => Object.prototype.hasOwnProperty.call(answers, key)); const values = keys.map((key) => answers[key]);
    if (!values.length || values.some((value) => typeof value !== "string" || value !== values[0])) return null;
    const dob = values[0]; if (!/^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$/.test(dob)) return null;
    const birth = new Date(`${dob}T00:00:00.000Z`); if (!Number.isFinite(birth.getTime()) || birth.toISOString().slice(0, 10) !== dob || typeof now !== "function") return null;
    let current; try { current = now(); } catch { return null; }
    if (!(current instanceof Date) || !Number.isFinite(current.getTime())) return null;
    let parts; try { parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(current).filter(({ type }) => type !== "literal").map(({ type, value }) => [type, value])); } catch { return null; }
    if (!/^\d{4}$/.test(parts.year || "") || !/^\d{2}$/.test(parts.month || "") || !/^\d{2}$/.test(parts.day || "")) return null;
    let age = Number(parts.year) - Number(dob.slice(0, 4)); if (`${parts.month}-${parts.day}` < dob.slice(5)) age -= 1;
    return age >= 18 && age <= 100 ? String(age) : null;
  } catch { return null; }
}
async function resolveTechPlayPrivateValue(input, readPeatixProfile, readFormProfile, now) {
  try {
    if (!input || typeof input !== "object" || Array.isArray(input) || !input.control || typeof input.control !== "object" || Array.isArray(input.control)) return null;
    if (input.control.required !== true || input.control.completed !== false || input.control.submittable !== false) return null;
    let control; try { control = safeControl(input && input.control); } catch { return null; }
    if (!control || control.required !== true || control.completed !== false || control.submittable !== false || !["input", "radio"].includes(control.kind)) return null;
    const label = input.control.label; const question = input.control.question;
    if (control.kind === "radio") {
      if (!/^techplay_answer_[1-9][0-9]*_[1-9][0-9]*$/.test(control.control) || !["キャリア状況", "職種"].includes(question) || typeof label !== "string") return null;
      const profile = await safeProfile(readFormProfile); const expected = techPlayExactAnswer(profile, question);
      return techPlayPrivateString(expected) && expected === label ? true : null;
    }
    if (!/^techplay_answer_[1-9][0-9]*$/.test(control.control)) return null;
    const identityKey = label === "氏名" ? "name_kanji" : label === "メールアドレス" ? "email" : null;
    if (identityKey) { const profile = await safeProfile(readPeatixProfile); try { return techPlayPrivateString(profile && profile[identityKey]); } catch { return null; } }
    if (!["年齢", "所属企業（学校）名"].includes(label)) return null;
    const profile = await safeProfile(readFormProfile); if (label === "年齢") return techPlayPrivateAge(profile, now);
    return techPlayPrivateString(techPlayExactAnswer(profile, "所属企業（学校）名"));
  } catch { return null; }
}

function createLumaPrivateValueResolver(options = {}) {
  const readProfile = options.readProfile;
  if (typeof readProfile !== "function") invalid();
  return async function resolveValue(input = {}) {
    const control = safeControl(input.control);
    const profile = await readProfile();
    if (!profile || typeof profile !== "object" || Array.isArray(profile)) invalid();
    const label = normalizedLabel(control.label);
    if (/\b(phone|telephone|mobile|電話|携帯)\b/i.test(label)) {
      return typeof profile.phone === "string" ? profile.phone : null;
    }
    const answers = profile.form_answers;
    if (!answers || typeof answers !== "object" || Array.isArray(answers)) return null;
    const match = Object.entries(answers).find(([key]) => normalizedLabel(key) === label);
    return match ? match[1] : null;
  };
}

function createPrivateValueResolver(options = {}) {
  const readPeatixProfile = options.readPeatixProfile || (() => null);
  const readFormProfile = options.readFormProfile || (() => null);
  const now = Object.prototype.hasOwnProperty.call(options, "now") ? options.now : () => new Date();
  if (typeof readPeatixProfile !== "function" || typeof readFormProfile !== "function") invalid();
  return async function resolveValue(input = {}) {
    if (input && input.provider === "techplay") return resolveTechPlayPrivateValue(input, readPeatixProfile, readFormProfile, now);
    const control = safeControl(input.control);
    if (input && input.provider === "kokuchpro") {
      const binding = candidateKokuchProBinding(input.candidate);
      if (!binding || kokuchProSeatToken(binding) !== control.control || control.required !== true || control.completed !== false || control.submittable !== false || input.action?.purpose !== "fill") return null;
      if (control.kind === "radio" && input.action?.method === "ax_check") return true;
      return control.kind === "input" && control.label === "参加人数" && input.action?.method === "ax_fill" ? "1" : null;
    }
    const label = normalizedLabel(control.label); const question = normalizedLabel(control.question);
    if (input.provider === "eventbrite") {
      if (control.kind !== "input" || control.required !== true || control.completed !== false || control.submittable !== false) return null;
      const key = control.label === "First name" ? "given_name" : control.label === "Last name" ? "family_name" : ["Email", "Confirm email"].includes(control.label) ? "email" : null;
      if (!key) return null;
      const profile = await safeProfile(readPeatixProfile); const value = profile && profile[key];
      return typeof value === "string" && value.length > 0 && value.length <= 2_000 && value === value.trim() ? value : null;
    }
    if (["checkbox", "radio"].includes(control.kind)) {
      if (input.provider === "connpass") return input.state === "connpass_join" && connpassSafeRadioCategory(control) ? true : null;
      const profile = await safeProfile(readPeatixProfile);
      const knownPrivacyOption = label === normalizedLabel("確認し同意する。") && /^.+のプライバシーポリシーを読んだ・確認した$/.test(question);
      if ((LABEL.privacy.test(label) || knownPrivacyOption) && profile && profile.accept_organizer_privacy === true) return true;
      return approvedOption(await safeProfile(readFormProfile), question, label) ? true : null;
    }
    const key = label === normalizedLabel("お名前（漢字）") ? "name_kanji" : label === normalizedLabel("お名前（ひらがな）") ? "name_hiragana" : LABEL.name.test(label) ? "name" : LABEL.email.test(label) ? "email" : LABEL.family.test(label) ? "family_name_kana" : LABEL.given.test(label) ? "given_name_kana" : null;
    if (key) { const profile = await safeProfile(readPeatixProfile); return profile && typeof profile[key] === "string" ? profile[key] : null; }
    const profile = await safeProfile(readFormProfile);
    return LABEL.phone.test(label) ? profile && typeof profile.phone === "string" ? profile.phone : null : answerFor(profile, label);
  };
}

function absoluteDirectory(value) {
  const directory = path.resolve(String(value || ""));
  if (!path.isAbsolute(directory) || directory === path.parse(directory).root) invalid();
  return directory;
}

function createBoundedActionProposer(options = {}) {
  const repoRoot = absoluteDirectory(options.repoRoot);
  const evidenceDir = absoluteDirectory(options.evidenceDir);
  const runAgentRunner = options.runAgentRunner || runLocalAgentRunner;
  const extensionProvider = options.extensionProvider;
  const stepTokenBudget = Number(options.stepTokenBudget == null ? 24_576 : options.stepTokenBudget);
  if (extensionProvider != null && (typeof extensionProvider !== "string" || !EXTENSION_PROVIDER.test(extensionProvider) || PROVIDERS.has(extensionProvider))) invalid();
  const supportsProvider = (provider) => PROVIDERS.has(provider) || (extensionProvider != null && provider === extensionProvider);
  if (typeof runAgentRunner !== "function" || !Number.isSafeInteger(stepTokenBudget) || stepTokenBudget < 1 || stepTokenBudget > 1_000_000) invalid();
  const fallbackSequences = new Map();
  return async function proposeAction(input = {}) {
    const targetId = String(input.target_id || "");
    const step = Number(input.step);
    if (
      !supportsProvider(input.provider) || !/^[A-Za-z0-9._-]{3,128}$/.test(targetId)
      || input.expected_state !== "registered_or_pending"
      || !Number.isInteger(step) || step < 1 || step > 10
      || !input.observation || !Array.isArray(input.observation.controls)
    ) invalid();
    if (input.provider === "techplay") return null;
    const controls = input.observation.controls.map(safeControl);
    const observationState = String(input.observation.state || "");
    const nativeControl = nativeConnpassControl(input.provider, observationState, controls);
    if (nativeControl) return Object.freeze({ control: nativeControl });
    const nativeKokuchPro = nativeKokuchProControl(input.provider, controls);
    if (nativeKokuchPro) return Object.freeze({ control: nativeKokuchPro });
    const nativeTrigger = nativeDoorkeeperTrigger(input.provider, controls);
    if (nativeTrigger) return Object.freeze({ control: nativeTrigger });
    const pendingAnswers = controls.filter((control) => ACTIONABLE_KINDS.has(control.kind) && control.required && !control.completed);
    const actionableControls = pendingAnswers.length ? pendingAnswers : controls.filter((control) => control.kind === "button" && control.submittable === true);
    if (!actionableControls.length) return null;
    const sequence = step === 1 ? (fallbackSequences.get(targetId) || 0) + 1 : (fallbackSequences.get(targetId) || 1);
    fallbackSequences.set(targetId, sequence);
    const state = observationState === "connpass_join" ? "connpass_join" : "registration_page";
    const result = await runAgentRunner({
      prompt: [
        `Choose exactly one browser action for the current ${input.provider} registration page.`,
        "Return only one control token from the supplied list.",
        "Choose only an incomplete required answer field, or after all required answers are complete, a submittable form button. Never invent answers, navigate, open or close pages, run commands, or edit code.",
        "The parent process owns all private values, executes the action, and verifies registered or pending state.",
        `Step: ${step} of 10`,
        `Page state: ${state}`,
        `Controls: ${JSON.stringify(controls)}`,
      ].join("\n"),
      schema: {
        type: "object",
        additionalProperties: false,
        required: ["control"],
        properties: {
          control: { type: "string", enum: actionableControls.map((control) => control.control) },
        },
      },
      taskClass: "browser-lane-agent",
      timeoutMs: 30_000,
      signal: input.signal,
      readOnly: true,
      tokenBudget: stepTokenBudget,
      budgetScopeId: `connector-step-${input.provider}-${targetId}-${sequence}-${step}`,
      evidenceDir: path.join(evidenceDir, `target-${targetId}`, `fallback-${sequence}`, `step-${step}`),
      repoRoot,
    });
    if (
      !result || !result.summary || result.summary.status !== "success"
      || result.summary.selected_provider !== "codex" || result.summary.selected_model !== "gpt-5.6-terra"
      || !result.value || typeof result.value !== "object" || Array.isArray(result.value)
    ) invalid();
    const control = String(result.value.control || "");
    if (!actionableControls.some((item) => item.control === control)) return null;
    return Object.freeze({ control });
  };
}

function createProductionBrowserHarness(options = {}) {
  const lumaWorkflow = options.lumaWorkflow;
  const connpassWorkflow = options.connpassWorkflow;
  const peatixWorkflow = options.peatixWorkflow;
  const meetupWorkflow = options.meetupWorkflow;
  const doorkeeperWorkflow = options.doorkeeperWorkflow;
  const eventbriteWorkflow = options.eventbriteWorkflow;
  const techplayWorkflow = options.techplayWorkflow;
  const extensionProvider = options.extensionProvider;
  const extensionWorkflow = options.extensionWorkflow;
  const extensionConfigured = extensionProvider != null || extensionWorkflow != null;
  const inspectControls = options.inspectControls;
  const proposeAction = options.proposeAction;
  const operateControl = options.operateControl;
  const resolveValue = options.resolveValue;
  const sleep = typeof options.sleep === "function" ? options.sleep : (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  if (
    !lumaWorkflow || typeof lumaWorkflow.readProviderState !== "function"
    || (connpassWorkflow != null && typeof connpassWorkflow.readProviderState !== "function")
    || (peatixWorkflow != null && typeof peatixWorkflow.readProviderState !== "function")
    || (meetupWorkflow != null && typeof meetupWorkflow.readProviderState !== "function")
    || (doorkeeperWorkflow != null && typeof doorkeeperWorkflow.readProviderState !== "function")
    || (eventbriteWorkflow != null && typeof eventbriteWorkflow.readProviderState !== "function")
    || (techplayWorkflow != null && typeof techplayWorkflow.readProviderState !== "function")
    || (extensionConfigured && (extensionProvider == null || extensionWorkflow == null || typeof extensionProvider !== "string"
      || !EXTENSION_PROVIDER.test(extensionProvider) || PROVIDERS.has(extensionProvider) || !extensionWorkflow || typeof extensionWorkflow.readProviderState !== "function"))
    || typeof inspectControls !== "function" || typeof proposeAction !== "function"
    || typeof operateControl !== "function" || typeof resolveValue !== "function"
    || (options.sleep != null && typeof options.sleep !== "function")
  ) invalid();
  const supportsProvider = (provider) => PROVIDERS.has(provider) || provider === extensionProvider;
  const registry = new WeakMap();

  async function observed(page, provider, candidate = null, allowEmpty = false, runtime = {}) {
    const eventbriteBinding = provider === "eventbrite" ? candidateEventbriteBinding(candidate) : null;
    const techplayBinding = provider === "techplay" ? candidateTechPlayBinding(candidate) : null;
    const eventId = candidatePeatixEventId(candidate) || candidateMeetupEventId(candidate) || candidateDoorkeeperEventId(candidate) || eventbriteBinding?.eventId || techplayBinding?.eventId || "";
    const href = (() => { try { return String(typeof page.url === "function" ? page.url() : ""); } catch { return ""; } })();
    const connpassJoin = isConnpassJoin(provider, href);
    const values = await inspectControls({ page, provider, candidate: provider === "techplay" || provider === "kokuchpro" || provider === extensionProvider ? candidate : undefined, event_id: eventId || undefined, canonical_url: eventbriteBinding?.canonicalUrl || techplayBinding?.canonicalUrl, ticket_id: techplayBinding?.ticketId, connpass_join: connpassJoin, signal: runtime.signal, sleep });
    if (!Array.isArray(values) || values.length > 100 || (values.length < 1 && provider !== "eventbrite" && !allowEmpty)) invalid();
    const controls = values.map(safeControl);
    if (new Set(controls.map((item) => item.control)).size !== controls.length) invalid();
    const eventbriteAttendeeObservation = provider === "eventbrite" && isEventbriteAttendeeObservation(controls, eventId);
    const eventbriteFinalObservation = provider === "eventbrite" && isEventbriteFinalObservation(controls, eventId);
    const eventbriteFrame = (eventbriteAttendeeObservation || eventbriteFinalObservation) ? eventbriteTicketFrame(page, eventId, eventbriteBinding.canonicalUrl) : null;
    const eventbriteTicket = provider === "eventbrite" && controls.some((item) => EVENTBRITE_TICKET_REGISTER_CONTROL.test(item.control));
    const observation = Object.freeze({
      state: eventbriteTicket ? "eventbrite_ticket_step" : connpassJoin ? "connpass_join" : "registration_page",
      controls: Object.freeze(controls),
    });
    registry.set(page, { provider, eventId, observation, ...((eventbriteAttendeeObservation || eventbriteFinalObservation) ? { frame: eventbriteFrame } : {}) });
    return observation;
  }

  async function performTechPlayInputAction(input, binding) {
    const token = String(input.action && input.action.control || ""); const scalar = /^techplay_answer_[1-9][0-9]*$/.test(token); const radio = /^techplay_answer_[1-9][0-9]*_[1-9][0-9]*$/.test(token); const optout = /^techplay_optout_(?:area_[1-9][0-9]*|tag_[1-9][0-9]*|organizer_[1-9][0-9]*|icon_published|use_as_preset)$/.test(token);
    const action = input.action || {}; if (!binding || action.purpose !== "fill" || (!scalar && !radio && !optout) || (scalar && action.method !== "ax_fill") || (radio && action.method !== "ax_check") || (optout && action.method !== "ax_uncheck")) return Object.freeze({ status: "failed" });
    const joinUrl = `${binding.canonicalUrl.replace("/event/", "/event/join/")}`; const readHref = () => { try { return String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { return ""; } }; const bindingMatches = () => { const current = candidateTechPlayBinding(input.candidate); return Boolean(current && current.eventId === binding.eventId && current.canonicalUrl === binding.canonicalUrl && current.ticketId === binding.ticketId); };
    if (!bindingMatches() || readHref() !== joinUrl) return Object.freeze({ status: "failed" });
    let before; try { before = await observed(input.page, "techplay", input.candidate); } catch { return Object.freeze({ status: "failed" }); }
    const selected = before.controls.find((control) => control.control === token);
    if (!selected || selected.required !== true || selected.completed !== false || selected.submittable !== false || (scalar && selected.kind !== "input") || (radio && selected.kind !== "radio") || (optout && selected.kind !== "checkbox")) return Object.freeze({ status: "failed" });
    let value = null;
    if (scalar) { try { value = await resolveValue({ provider: "techplay", page: input.page, candidate: input.candidate, control: selected, action, state: "registration_page" }); } catch { return Object.freeze({ status: "failed" }); } if (typeof value !== "string" || !value.trim() || value.length > 2_000) return Object.freeze({ status: "failed" }); }
    if (radio) {
      const group = before.controls.filter((control) => control.kind === "radio" && control.question === selected.question && /^techplay_answer_[1-9][0-9]*_[1-9][0-9]*$/.test(control.control)); const approved = [];
      for (const control of group) { let result = null; try { result = await resolveValue({ provider: "techplay", page: input.page, candidate: input.candidate, control, action, state: "registration_page" }); } catch { return Object.freeze({ status: "failed" }); } if (result === true) approved.push(control); }
      if (approved.length !== 1 || approved[0].control !== token) return Object.freeze({ status: "failed" });
    }
    if (!bindingMatches() || readHref() !== joinUrl) return Object.freeze({ status: "failed" });
    let current; try { current = await observed(input.page, "techplay", input.candidate); } catch { return Object.freeze({ status: "failed" }); }
    const rebound = current.controls.find((control) => control.control === token);
    if (!rebound || rebound.kind !== selected.kind || rebound.label !== selected.label || rebound.question !== selected.question || rebound.required !== true || rebound.completed !== false || rebound.submittable !== false) return Object.freeze({ status: "failed" });
    let result; try { result = await operateControl({ page: input.page, provider: "techplay", candidate: input.candidate, control: rebound, action, value, signal: input.signal, ...(typeof input.beforeDispatch === "function" ? { beforeDispatch: input.beforeDispatch } : {}) }); } catch { return Object.freeze({ status: "failed" }); }
    if (!result || result.status !== "success" || !bindingMatches() || readHref() !== joinUrl) return Object.freeze({ status: "failed" });
    const completed = (observation) => { const control = observation?.controls?.find((item) => item.control === token); return Boolean(control && control.kind === rebound.kind && control.label === rebound.label && control.question === rebound.question && control.required === true && control.completed === true && control.submittable === false); };
    for (let attempt = 0; attempt < TECHPLAY_POSTCHECK_ATTEMPTS; attempt += 1) {
      if (!bindingMatches() || readHref() !== joinUrl) return Object.freeze({ status: "failed" });
      let after; try { after = await observed(input.page, "techplay", input.candidate, true); } catch { return Object.freeze({ status: "failed" }); }
      if (completed(after)) return Object.freeze({ status: "success" });
      if (attempt + 1 < TECHPLAY_POSTCHECK_ATTEMPTS) { try { await sleep(TECHPLAY_POSTCHECK_INTERVAL_MS); } catch { return Object.freeze({ status: "failed" }); } }
    }
    return Object.freeze({ status: "failed" });
  }

  async function performAction(input = {}) {
    if (!input.page || !input.action || !CONTROL.test(String(input.action.control || ""))) invalid();
    const provider = input.provider == null ? "luma" : String(input.provider);
    if (!supportsProvider(provider)) invalid();
    if (input.signal && input.signal.aborted) return Object.freeze({ status: "failed", safe_reason: "time_limit" });
    if (provider === "techplay") {
      const binding = candidateTechPlayBinding(input.candidate);
      if (!binding) return Object.freeze({ status: "failed" });
      return String(input.action.control || "") === `techplay_final_${binding.eventId}`
        ? performTechPlayFinalAction(input, binding)
        : /^techplay_review_[1-9][0-9]*$/.test(String(input.action.control || ""))
        ? performTechPlayReviewAction(input, binding) : performTechPlayInputAction(input, binding);
    }
    const doorkeeperBinding = provider === "doorkeeper" ? candidateDoorkeeperBinding(input.candidate) : null;
    if (provider === "doorkeeper" && !doorkeeperBinding) return Object.freeze({ status: "failed" });
    const eventbriteBinding = provider === "eventbrite" ? candidateEventbriteBinding(input.candidate) : null;
    if (provider === "eventbrite" && !eventbriteBinding) return Object.freeze({ status: "failed" });
    const eventId = candidatePeatixEventId(input.candidate) || candidateMeetupEventId(input.candidate) || candidateDoorkeeperEventId(input.candidate) || eventbriteBinding?.eventId || String(input.event_id || "");
    const cached = registry.get(input.page);
    const observation = provider === "eventbrite" || !cached || cached.provider !== provider || cached.eventId !== eventId
      ? await observed(input.page, provider, input.candidate) : cached.observation;
    const currentHref = (() => { try { return String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { return ""; } })();
    const state = observation.state === "connpass_join" && isConnpassJoin(provider, currentHref) ? "connpass_join" : observation.state === "connpass_join" ? "registration_page" : observation.state;
    const control = observation.controls.find((item) => item.control === input.action.control);
    if (!control) return Object.freeze({ status: "failed" });
    if (provider !== "eventbrite" && EVENTBRITE_TRIGGER_CONTROL.test(control.control)) return Object.freeze({ status: "failed" });
    if (provider !== "eventbrite" && EVENTBRITE_ATTENDEE_CONTROL.test(control.control)) return Object.freeze({ status: "failed" });
    if (provider !== "eventbrite" && EVENTBRITE_MARKETING_CONTROL.test(control.control)) return Object.freeze({ status: "failed" });
    const doorkeeperModalTrigger = isDoorkeeperModalTrigger({ provider, page: input.page, candidate: input.candidate, control, controls: observation.controls });
    const eventbriteTrigger = isEventbriteCheckoutTrigger({ provider, page: input.page, candidate: input.candidate, control, controls: observation.controls });
    const eventbriteTicket = isEventbriteTicketRegister({ provider, page: input.page, candidate: input.candidate, control, controls: observation.controls });
    const eventbriteAttendee = isEventbriteAttendeeFill({ provider, page: input.page, candidate: input.candidate, control, controls: observation.controls });
    const eventbriteMarketing = isEventbriteMarketingOptOut({ provider, page: input.page, candidate: input.candidate, control, controls: observation.controls });
    const eventbriteFinal = isEventbriteFinalRegister({ provider, page: input.page, candidate: input.candidate, control, controls: observation.controls });
    const kokuchProBinding = provider === "kokuchpro" ? candidateKokuchProBinding(input.candidate) : null;
    const kokuchProRoute = Boolean(kokuchProBinding && currentHref === kokuchProBinding.canonicalUrl && control.control === kokuchProEntryToken(kokuchProBinding)
      && control.kind === "link" && control.label === "申込む" && control.required === false && control.completed === false && control.submittable === false);
    const eventbriteMarketingMatch = eventbriteMarketing ? EVENTBRITE_MARKETING_CONTROL.exec(control.control) : null;
    const attendeeFrame = eventbriteAttendee || eventbriteMarketing ? registry.get(input.page)?.frame : null;
    const finalFrame = eventbriteFinal ? registry.get(input.page)?.frame : null;
    const ticketFrame = eventbriteTicket ? eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) : null;
    if (provider === "eventbrite" && !eventbriteTrigger && !eventbriteTicket && !eventbriteAttendee && !eventbriteMarketing && !eventbriteFinal) return Object.freeze({ status: "failed" });
    if (eventbriteTicket && !ticketFrame) return Object.freeze({ status: "failed" });
    if ((eventbriteAttendee || eventbriteMarketing) && !attendeeFrame) return Object.freeze({ status: "failed" });
    if (eventbriteFinal && !finalFrame) return Object.freeze({ status: "failed" });
    const pendingRequiredAnswer = observation.controls.some((item) => ACTIONABLE_KINDS.has(item.kind) && item.required && !item.completed);
    if (control.kind === "button" && pendingRequiredAnswer) return Object.freeze({ status: "failed" });
    if (ACTIONABLE_KINDS.has(control.kind) && (!control.required || control.completed)) return Object.freeze({ status: "failed" });
    if ((control.kind === "link" && !doorkeeperModalTrigger && !kokuchProRoute) || (control.kind === "button" && control.submittable !== true)) return Object.freeze({ status: "failed" });
    if (doorkeeperModalTrigger && (input.action.purpose !== "submit" || input.action.method !== "ax_click")) return Object.freeze({ status: "failed" });
    if (eventbriteTrigger && (input.action.purpose !== "submit" || input.action.method !== "ax_click")) return Object.freeze({ status: "failed" });
    if (eventbriteTicket && (input.action.purpose !== "submit" || input.action.method !== "ax_click")) return Object.freeze({ status: "failed" });
    if (eventbriteAttendee && (input.action.purpose !== "fill" || input.action.method !== "ax_fill")) return Object.freeze({ status: "failed" });
    if (eventbriteMarketing && (input.action.purpose !== "fill" || input.action.method !== "ax_uncheck")) return Object.freeze({ status: "failed" });
    if (eventbriteFinal && (input.action.purpose !== "submit" || input.action.method !== "ax_click")) return Object.freeze({ status: "failed" });
    if (provider === "connpass" && state === "connpass_join" && control.kind === "button" && control.label !== CONNPASS_FINAL_LABEL) return Object.freeze({ status: "failed" });
    if (provider === "doorkeeper" && control.kind === "button" && control.label !== DOORKEEPER_FINAL_LABEL) return Object.freeze({ status: "failed" });
    const action = actionForControl(control); if (!action) return Object.freeze({ status: "failed" });
    let value = null;
    if (FILL.has(action.method)) {
      value = await resolveValue({ provider, page: input.page, candidate: input.candidate, control, action, state });
      if (
        !(typeof value === "string" || typeof value === "boolean" || Array.isArray(value))
        || (typeof value === "string" && (!value.trim() || value.length > 2_000))
        || (Array.isArray(value) && (value.length < 1 || value.length > 3))
      ) return Object.freeze(provider === "luma"
        ? { status: "failed", safe_reason: "private_value_unavailable" }
        : { status: "failed" });
    }
    if (eventbriteAttendee) {
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== attendeeFrame) return Object.freeze({ status: "failed" });
      const refreshed = await inspectEventbriteAttendeeFrame(attendeeFrame, eventbriteBinding.eventId);
      const selected = Array.isArray(refreshed) ? refreshed.find((item) => item.control === control.control) : null;
      if (!isEventbriteAttendeeObservation(refreshed, eventbriteBinding.eventId) || !selected || selected.completed !== false) return Object.freeze({ status: "failed" });
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== attendeeFrame) return Object.freeze({ status: "failed" });
    }
    if (eventbriteMarketing) {
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== attendeeFrame) return Object.freeze({ status: "failed" });
      const refreshed = await inspectEventbriteAttendeeFrame(attendeeFrame, eventbriteBinding.eventId);
      const selected = Array.isArray(refreshed) ? refreshed.find((item) => item.control === control.control) : null;
      if (!isEventbriteAttendeeObservation(refreshed, eventbriteBinding.eventId) || !selected || selected.completed !== false || !await eventbriteMarketingInputChecked(attendeeFrame, eventbriteMarketingMatch[1], true)) return Object.freeze({ status: "failed" });
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== attendeeFrame) return Object.freeze({ status: "failed" });
    }
    if (eventbriteFinal) {
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== finalFrame) return Object.freeze({ status: "failed" });
      const refreshed = await inspectEventbriteAttendeeFrame(finalFrame, eventbriteBinding.eventId);
      const selected = Array.isArray(refreshed) ? refreshed.find((item) => item.control === control.control) : null;
      if (!isEventbriteFinalObservation(refreshed, eventbriteBinding.eventId) || !selected || selected.completed !== false) return Object.freeze({ status: "failed" });
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== finalFrame) return Object.freeze({ status: "failed" });
    }
    const navigationWait = startPeatixConfirmWait(input.page, provider, control);
    if (navigationWait && navigationWait.unavailable) return Object.freeze({ status: "failed" });
    const finalEffectStatuses = provider === "doorkeeper" || eventbriteFinal ? ["registered"] : undefined;
    const finalEffectWait = startFinalEffectWait(
      input.page,
      provider,
      control,
      input.candidate,
      provider === "connpass" && connpassWorkflow ? connpassWorkflow.readProviderState
        : provider === "peatix" && peatixWorkflow ? peatixWorkflow.readProviderState
          : provider === "doorkeeper" && doorkeeperWorkflow ? doorkeeperWorkflow.readProviderState
            : eventbriteFinal && eventbriteWorkflow ? eventbriteWorkflow.readProviderState : null,
      observation.controls,
      finalEffectStatuses,
    );
    if (finalEffectWait && finalEffectWait.unavailable) return Object.freeze({ status: "failed" });
    if (finalEffectWait) finalEffectWait.markClicked();
    let result;
    if (eventbriteTicket) {
      const ticket = await inspectEventbriteTicketFrame(ticketFrame, eventbriteBinding.eventId);
      if (!ticket || ticket.control !== control.control) return Object.freeze({ status: "failed" });
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== ticketFrame) return Object.freeze({ status: "failed" });
    }
    if (input.signal && input.signal.aborted) {
      if (finalEffectWait) finalEffectWait.cancel();
      return Object.freeze({ status: "failed", safe_reason: "time_limit" });
    }
    try {
      result = await operateControl({
        page: input.page,
        provider,
        candidate: input.candidate,
        ...(eventbriteFinal ? { eventId: eventbriteBinding.eventId, canonicalUrl: eventbriteBinding.canonicalUrl } : {}),
        ...((attendeeFrame || finalFrame || ticketFrame) ? { frame: attendeeFrame || finalFrame || ticketFrame } : {}),
        control,
        action,
        value,
        signal: input.signal,
        sleep,
        ...(typeof input.beforeDispatch === "function" ? { beforeDispatch: input.beforeDispatch } : {}),
      });
    } catch {
      if (finalEffectWait) return settleFinalEffect(finalEffectWait, finalEffectStatuses);
      return Object.freeze({ status: "failed" });
    }
    if (!result || result.status !== "success") {
      if (eventbriteFinal && result && result[EVENTBRITE_FINAL_ATTEMPTED] === false) {
        if (finalEffectWait) finalEffectWait.cancel();
        return Object.freeze({ status: "failed" });
      }
      if (finalEffectWait) return settleFinalEffect(finalEffectWait, finalEffectStatuses);
      return Object.freeze({ status: "failed" });
    }
    if (eventbriteFinal) {
      const operation = result[EVENTBRITE_FINAL_OPERATION];
      const attempted = operation && operation.page === input.page && operation.frame === finalFrame
        && operation.eventId === eventbriteBinding.eventId && operation.token === control.control
        && operation.handle && operation.testId === "eds-modal__primary-button";
      if (!attempted) {
        if (finalEffectWait) finalEffectWait.cancel();
        return Object.freeze({ status: "failed", safe_reason: "effect_unknown" });
      }
      return settleFinalEffect(finalEffectWait, finalEffectStatuses);
    }
    if (eventbriteAttendee) {
      const currentFrame = eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl);
      if (currentFrame !== attendeeFrame) return Object.freeze({ status: "failed" });
      const refreshed = await inspectEventbriteAttendeeFrame(attendeeFrame, eventbriteBinding.eventId);
      const selected = Array.isArray(refreshed) ? refreshed.find((item) => item.control === control.control) : null;
      if (!isEventbriteAttendeeObservation(refreshed, eventbriteBinding.eventId) || !selected || selected.completed !== true) return Object.freeze({ status: "failed" });
      if (eventbriteTicketFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) !== attendeeFrame) return Object.freeze({ status: "failed" });
      return Object.freeze({ status: "success" });
    }
    if (eventbriteMarketing) {
      const operation = result[EVENTBRITE_MARKETING_OPERATION];
      return Object.freeze({ status: await waitForEventbriteMarketingOptOut(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl, attendeeFrame, operation) ? "success" : "failed" });
    }
    if (eventbriteTicket) {
      const settled = await waitForEventbriteTicketStep(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl);
      return Object.freeze(settled ? { status: "success" } : { status: "failed", safe_reason: "effect_unknown" });
    }
    if (eventbriteTrigger) return Object.freeze({ status: await waitForEventbriteCheckoutFrame(input.page, eventbriteBinding.eventId, eventbriteBinding.canonicalUrl) ? "success" : "failed" });
    if (navigationWait && !(await navigationWait.promise)) return Object.freeze({ status: "failed" });
    if (finalEffectWait) return settleFinalEffect(finalEffectWait, finalEffectStatuses);
    return Object.freeze({ status: "success" });
  }

  async function selectTechPlayInputAction(observation, page, candidate) {
    const controls = Array.isArray(observation?.controls) ? observation.controls : [];
    const scalar = controls.find((control) => /^techplay_answer_[1-9][0-9]*$/.test(control.control) && control.kind === "input" && control.required === true && control.completed === false && control.submittable === false);
    if (scalar) return Object.freeze({ purpose: "fill", method: "ax_fill", control: scalar.control });
    const groups = new Map();
    for (const control of controls) if (/^techplay_answer_[1-9][0-9]*_[1-9][0-9]*$/.test(control.control) && control.kind === "radio" && control.required === true && control.completed === false && control.submittable === false) { const list = groups.get(control.question) || []; list.push(control); groups.set(control.question, list); }
    for (const group of groups.values()) {
      const approved = []; for (const control of group) { let value = null; try { value = await resolveValue({ provider: "techplay", page, candidate, control, action: { purpose: "fill", method: "ax_check", control: control.control }, state: "registration_page" }); } catch { return null; } if (value === true) approved.push(control); }
      if (approved.length !== 1) return null;
      return Object.freeze({ purpose: "fill", method: "ax_check", control: approved[0].control });
    }
    const optout = controls.find((control) => /^techplay_optout_(?:area_[1-9][0-9]*|tag_[1-9][0-9]*|organizer_[1-9][0-9]*|icon_published|use_as_preset)$/.test(control.control) && control.kind === "checkbox" && control.required === true && control.completed === false && control.submittable === false);
    return optout ? Object.freeze({ purpose: "fill", method: "ax_uncheck", control: optout.control }) : null;
  }

  async function performTechPlayFinalAction(input, binding) {
    const action = input.action || {}; const token = `techplay_final_${binding.eventId}`;
    if (action.purpose !== "submit" || action.method !== "ax_click" || action.control !== token || !techplayWorkflow || typeof techplayWorkflow.readProviderState !== "function") return Object.freeze({ status: "failed" });
    const confirmUrl = `https://techplay.jp/event/join/${binding.eventId}/confirm`;
    const sameBinding = () => { const current = candidateTechPlayBinding(input.candidate); return Boolean(current && current.eventId === binding.eventId && current.canonicalUrl === binding.canonicalUrl && current.ticketId === binding.ticketId); };
    try { if (String(input.page.url()) !== confirmUrl || !sameBinding()) return Object.freeze({ status: "failed" }); } catch { return Object.freeze({ status: "failed" }); }
    let observation;
    try { observation = await observed(input.page, "techplay", input.candidate, true); } catch { return Object.freeze({ status: "failed" }); }
    if (!Array.isArray(observation.controls) || observation.controls.length !== 1 || observation.controls[0].control !== token) return Object.freeze({ status: "failed" });
    const control = observation.controls.find((item) => item.control === token);
    try { if (String(input.page.url()) !== confirmUrl || !sameBinding()) return Object.freeze({ status: "failed" }); } catch { return Object.freeze({ status: "failed" }); }
    const finalEffectWait = startFinalEffectWait(input.page, "techplay", control, input.candidate, techplayWorkflow.readProviderState, observation.controls, ["registered"]);
    if (!finalEffectWait || finalEffectWait.unavailable) return Object.freeze({ status: "failed" });
    try {
      if (String(input.page.url()) !== confirmUrl || !sameBinding()) {
        finalEffectWait.cancel();
        return Object.freeze({ status: "failed" });
      }
    } catch {
      finalEffectWait.cancel();
      return Object.freeze({ status: "failed" });
    }
    finalEffectWait.markClicked();
    let result; let thrown = false;
    try { result = await operateControl({ page: input.page, provider: "techplay", candidate: input.candidate, control, action, signal: input.signal, ...(typeof input.beforeDispatch === "function" ? { beforeDispatch: input.beforeDispatch } : {}) }); }
    catch { thrown = true; result = null; }
    const attempted = thrown || Boolean(result && result.attempted === true) || Boolean(result && result.status === "success");
    if (!attempted) {
      finalEffectWait.cancel();
      return Object.freeze({ status: "failed" });
    }
    return settleFinalEffect(finalEffectWait, ["registered"]);
  }

  async function performTechPlayReviewAction(input, binding) {
    const action = input.action || {}; const token = `techplay_review_${binding.eventId}`;
    if (action.purpose !== "submit" || action.method !== "ax_click" || action.control !== token) return Object.freeze({ status: "failed" });
    let observation; try { observation = await observed(input.page, "techplay", input.candidate); } catch { return Object.freeze({ status: "failed" }); }
    const rebound = candidateTechPlayBinding(input.candidate); if (!rebound || rebound.eventId !== binding.eventId || rebound.canonicalUrl !== binding.canonicalUrl || rebound.ticketId !== binding.ticketId) return Object.freeze({ status: "failed" });
    const review = observation.controls.find((control) => control.control === token);
    const pending = observation.controls.some((control) => /^(?:techplay_ticket_|techplay_answer_|techplay_optout_)/.test(control.control) && control.completed !== true);
    const reviews = observation.controls.filter((control) => control.control === token && control.kind === "button" && control.label === TECHPLAY_REVIEW_LABEL && control.required === false && control.completed === false && control.submittable === true);
    if (!review || pending || reviews.length !== 1) return Object.freeze({ status: "failed" });
    const navigationWait = startPeatixConfirmWait(input.page, "techplay", review, input.candidate, observation.controls);
    if (!navigationWait || navigationWait.unavailable) return Object.freeze({ status: "failed" });
    const beforeClick = candidateTechPlayBinding(input.candidate); if (!beforeClick || beforeClick.eventId !== binding.eventId || beforeClick.canonicalUrl !== binding.canonicalUrl || beforeClick.ticketId !== binding.ticketId) return Object.freeze({ status: "failed" });
    let result; try { result = await operateControl({ page: input.page, provider: "techplay", candidate: input.candidate, control: review, action, signal: input.signal, ...(typeof input.beforeDispatch === "function" ? { beforeDispatch: input.beforeDispatch } : {}) }); } catch { result = null; }
    if (!result || (result.status !== "success" && result.attempted !== true)) return Object.freeze({ status: "failed" });
    if (!(await navigationWait.promise)) return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" });
    const confirmUrl = `https://techplay.jp/event/join/${binding.eventId}/confirm`;
    const sameBinding = (candidateBinding) => Boolean(candidateBinding && candidateBinding.eventId === binding.eventId && candidateBinding.canonicalUrl === binding.canonicalUrl && candidateBinding.ticketId === binding.ticketId);
    for (let attempt = 0; attempt < TECHPLAY_POSTCHECK_ATTEMPTS; attempt += 1) {
      let href = ""; try { href = String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" }); }
      if (href !== confirmUrl || !sameBinding(candidateTechPlayBinding(input.candidate))) return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" });
      let confirm; try { confirm = await observed(input.page, "techplay", input.candidate, true); } catch { return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" }); }
      let afterHref = ""; try { afterHref = String(typeof input.page.url === "function" ? input.page.url() : ""); } catch { return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" }); }
      if (afterHref !== confirmUrl || !sameBinding(candidateTechPlayBinding(input.candidate))) return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" });
      const final = confirm.controls.filter((control) => control.control === `techplay_final_${binding.eventId}` && control.kind === "button" && control.label === "申し込みを確定する" && control.required === false && control.completed === false && control.submittable === true);
      if (final.length === 1 && confirm.controls.length === 1) return Object.freeze({ status: "success" });
      if (confirm.controls.length !== 0) return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" });
      if (attempt + 1 < TECHPLAY_POSTCHECK_ATTEMPTS) { try { await sleep(TECHPLAY_POSTCHECK_INTERVAL_MS); } catch { return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" }); } }
    }
    return Object.freeze({ status: "failed", attempted: true, safe_reason: "effect_unknown" });
  }

  async function runTechPlayInputFallback(input) {
    const binding = candidateTechPlayBinding(input.candidate); const maxSteps = Number(input.maxSteps);
    if (!binding || !PAGE_WEBSOCKET.test(String(input.pageWebsocket || "")) || !input.page || typeof input.page !== "object" || input.expectedState !== "registered_or_pending" || input.maxSteps == null || !Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 20) invalid();
    const repaired = [];
    for (let step = 0; step < maxSteps; step += 1) {
      let observation; try { observation = await observed(input.page, "techplay", input.candidate); } catch { return Object.freeze({ status: "failed", safe_reason: "agent_action_failed", repaired_actions: Object.freeze([...repaired]) }); }
      if (!observation || !observation.controls.length) return Object.freeze({ status: "failed", safe_reason: "agent_action_failed", repaired_actions: Object.freeze([...repaired]) });
      if (observation.controls.length === 1 && observation.controls[0].control === `techplay_final_${binding.eventId}`) {
        if (maxSteps < 15) return Object.freeze({ status: "failed", safe_reason: "final_blocked", repaired_actions: Object.freeze([...repaired]) });
        const finalAction = { purpose: "submit", method: "ax_click", control: observation.controls[0].control };
        const finalResult = await performTechPlayFinalAction({ ...input, action: finalAction }, binding);
        const finalAttempted = Boolean(finalResult && (finalResult.status === "success" || finalResult.safe_reason === "effect_unknown"));
        const finalHistory = finalAttempted ? [...repaired, finalAction] : [...repaired];
        if (finalResult && finalResult.status === "success" && finalResult.provider_state && finalResult.provider_state.status === "registered") {
          return Object.freeze({ status: "completed", provider_state: Object.freeze({ ...finalResult.provider_state }), repaired_actions: Object.freeze(finalHistory) });
        }
        return Object.freeze({ status: "failed", safe_reason: finalAttempted && finalResult?.safe_reason === "effect_unknown" ? "effect_unknown" : "final_blocked", repaired_actions: Object.freeze(finalHistory) });
      }
      const action = await selectTechPlayInputAction(observation, input.page, input.candidate);
      if (!action) {
        const complete = observation.controls.every((control) => !/^techplay_(?:answer|optout)_/.test(control.control) || control.completed === true);
        if (!complete) return Object.freeze({ status: "failed", safe_reason: "agent_action_failed", repaired_actions: Object.freeze([...repaired]) });
        const review = observation.controls.find((control) => control.control === `techplay_review_${binding.eventId}`);
        if (!review) return Object.freeze({ status: "failed", safe_reason: "review_blocked", repaired_actions: Object.freeze([...repaired]) });
        const reviewAction = { purpose: "submit", method: "ax_click", control: review.control };
        const reviewResult = await performTechPlayReviewAction({ ...input, action: reviewAction }, binding);
        if (!reviewResult || reviewResult.status !== "success") return Object.freeze({ status: "failed", safe_reason: reviewResult?.safe_reason === "effect_unknown" ? "effect_unknown" : "review_blocked", repaired_actions: Object.freeze(reviewResult?.attempted === true ? [...repaired, reviewAction] : [...repaired]) });
        repaired.push(reviewAction);
        if (repaired.length >= maxSteps) return Object.freeze({ status: "failed", safe_reason: "final_blocked", repaired_actions: Object.freeze([...repaired]) });
        continue;
      }
      const result = await performTechPlayInputAction({ ...input, action }, binding);
      if (!result || result.status !== "success") return Object.freeze({ status: "failed", safe_reason: "agent_action_failed", repaired_actions: Object.freeze([...repaired]) });
      repaired.push(action);
    }
    return Object.freeze({ status: "failed", safe_reason: "agent_step_limit", repaired_actions: Object.freeze([...repaired]) });
  }

  async function runFallback(input = {}) {
    if (input.expectedState != null && input.expectedState !== "registered_or_pending") throw new Error("Browser Harness adapter invalid");
    if (!supportsProvider(input.provider) || !input.candidate) invalid();
    if (input.provider === "techplay") return runTechPlayInputFallback(input);
    const workflow = input.provider === extensionProvider ? extensionWorkflow
      : { luma: lumaWorkflow, connpass: connpassWorkflow, peatix: peatixWorkflow, meetup: meetupWorkflow, doorkeeper: doorkeeperWorkflow, eventbrite: eventbriteWorkflow }[input.provider];
    if (!workflow || typeof workflow.readProviderState !== "function") {
      if (input.provider === "doorkeeper") return Object.freeze({ status: "failed", safe_reason: "agent_action_failed", repaired_actions: Object.freeze([]) });
      invalid();
    }
    const seenMutations = new Set();
    let connpassSubmitAttempted = false;
    let doorkeeperSubmitAttempted = false;
    let doorkeeperTriggerAttempted = false;
    let ambiguousEffect = false;
    let finalEffectProviderState = null;
    let extensionAuthPreflightDone = false;
    let extensionAuthPreflightRequired = false;
    let extensionAuthRequired = false;
    let extensionSubmitAttempted = false;
    const adapter = createBrowserHarnessAdapter({
      async observePage({ page, signal }) {
        if (input.provider === extensionProvider && !extensionAuthPreflightDone) {
          extensionAuthPreflightDone = true;
          try {
            const preflight = await workflow.readProviderState({ page, candidate: input.candidate });
            if (preflight && typeof preflight === "object" && !Array.isArray(preflight) && preflight.status === "auth_required") {
              extensionAuthPreflightRequired = true;
              extensionAuthRequired = true;
            }
          } catch { /* non-auth readback remains on the existing fallback path */ }
        }
        return extensionAuthRequired
          ? Object.freeze({ state: "registration_page", controls: Object.freeze([]) })
          : observed(page, input.provider, input.candidate, false, { signal });
      },
      async proposeAction(input) { if (extensionAuthRequired) return null; const proposal = await proposeAction(input); const token = String(proposal && typeof proposal === "object" ? proposal.control || "" : ""); const control = input.observation.controls.find((item) => item.control === token); return CONTROL.test(token) && control ? actionForControl(control) : null; },
      async performAction(action) {
        const selected = action.action || action;
        const cached = registry.get(action.page);
        const selectedControl = cached && cached.provider === input.provider ? cached.observation.controls.find((item) => item.control === selected.control) : null;
        const doorkeeperTrigger = isDoorkeeperModalTrigger({ provider: input.provider, page: action.page, candidate: input.candidate, control: selectedControl, controls: cached?.observation.controls });
        const doorkeeperFinal = isDoorkeeperFinalSubmit({ provider: input.provider, page: action.page, candidate: input.candidate, control: selectedControl, controls: cached?.observation.controls });
        const effect = ["ax_click", "coordinate_click", "keyboard_submit"].includes(selected.method) ? "activate" : ["ax_fill", "dom_fill"].includes(selected.method) ? "fill" : selected.method;
        const signature = MUTATING_METHODS.has(selected.method)
          ? doorkeeperTrigger ? `${safePageState(action.page)}:doorkeeper:modal-trigger`
            : selected.purpose === "submit" ? `${safePageState(action.page)}:submit:form-submit` : `${safePageState(action.page)}:${selected.purpose}:${effect}:${selected.control}`
          : null;
        if (doorkeeperTrigger && doorkeeperTriggerAttempted) return Object.freeze({ status: "success" });
        if (input.provider === "connpass" && selected.purpose === "submit") {
          if (connpassSubmitAttempted) {
            ambiguousEffect = true;
            return Object.freeze({ status: "failed", safe_reason: "effect_unknown" });
          }
          connpassSubmitAttempted = true;
        }
        if (input.provider === "doorkeeper" && doorkeeperFinal) {
          if (doorkeeperSubmitAttempted) {
            ambiguousEffect = true;
            return Object.freeze({ status: "failed", safe_reason: "effect_unknown" });
          }
          doorkeeperSubmitAttempted = true;
        }
        if (signature && seenMutations.has(signature)) return Object.freeze({ status: "failed" });
        const inheritedBeforeDispatch = typeof action.beforeDispatch === "function" ? action.beforeDispatch : null;
        const extensionBeforeDispatch = input.provider === extensionProvider && selected.purpose === "submit"
          ? () => { extensionSubmitAttempted = true; } : null;
        const beforeDispatch = inheritedBeforeDispatch || extensionBeforeDispatch
          ? () => {
            inheritedBeforeDispatch?.();
            extensionBeforeDispatch?.();
          } : null;
        const result = await performAction({
          ...action,
          provider: input.provider,
          candidate: input.candidate,
          sleep,
          ...(beforeDispatch ? { beforeDispatch } : {}),
        });
        if (result && result.safe_reason === "effect_unknown") ambiguousEffect = true;
        if (input.provider !== extensionProvider && result && result.status === "success" && result.provider_state && ["registered", "pending"].includes(result.provider_state.status)) {
          finalEffectProviderState = result.provider_state;
        }
        if (signature && result && result.status === "success") {
          if (doorkeeperTrigger) doorkeeperTriggerAttempted = true;
          else seenMutations.add(signature);
        }
        return result;
      },
      async readExpectedState({ page }) {
        let state;
        try {
          state = finalEffectProviderState || await workflow.readProviderState({ page, candidate: input.candidate });
        } catch (error) {
          if (input.provider === extensionProvider) return Object.freeze({ status: "unavailable" });
          throw error;
        }
        if (input.provider === extensionProvider && state && typeof state === "object" && !Array.isArray(state) && state.status === "auth_required") {
          extensionAuthRequired = true;
          return Object.freeze({ status: "unavailable" });
        }
        if (input.provider === extensionProvider && (!state || typeof state !== "object" || Array.isArray(state)
          || !["registered", "pending"].includes(state.status))) return Object.freeze({ status: "unavailable" });
        if (input.provider === "doorkeeper") {
          return state && typeof state === "object" && !Array.isArray(state) && state.status === "registered"
            ? state : Object.freeze({ status: "unavailable" });
        }
        return input.provider === "meetup" && (!state || state.status !== "registered")
          ? Object.freeze({ status: "unavailable" }) : state;
      },
    });
    const result = await adapter.runFallback(input);
    if (extensionAuthPreflightRequired && !extensionSubmitAttempted) {
      return Object.freeze({ status: "failed", safe_reason: "auth_required", repaired_actions: Object.freeze([...(result?.repaired_actions || [])]) });
    }
    if (extensionSubmitAttempted && result && result.status === "failed") {
      return Object.freeze({ ...result, safe_reason: "effect_unknown" });
    }
    return ambiguousEffect && result && result.status === "failed"
      ? Object.freeze({ ...result, safe_reason: "effect_unknown" })
      : result;
  }

  return Object.freeze({ performAction, runFallback });
}

module.exports = {
  createBoundedActionProposer,
  createPrivateValueResolver,
  createLumaPrivateValueResolver,
  createProductionBrowserHarness,
  inspectPageControls,
  operatePageControl,
};
