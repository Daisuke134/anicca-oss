"use strict";

const os = require("node:os");
const path = require("node:path");
const { readLumaFormProfile } = require("./luma-form-profile.js");

// Same private profile file the Luma join path already reads (see
// luma-form-profile.js / connector-minimal-production.js's
// lumaFormProfilePath), reused here rather than inventing a second
// mechanism. form_answers is keyed by the organizer's own Japanese label
// (e.g. "所属企業（学校）名"), exactly like the Luma answer policy expects.
const DEFAULT_FORM_PROFILE_PATH = path.join(
  os.homedir(), ".local", "state", "life-manager", "private", "connector-luma-form-profile.json",
);

function safeIdentityText(value) {
  const text = String(value == null ? "" : value).trim();
  return text && text.length <= 200 && !/[\x00-\x1f\x7f]/.test(text) ? text : "";
}

// Owner-decided policy (2026-08-17): a required Connpass question is only
// ever pre-filled when it is free text asking for the two facts Dais already
// keeps on file — his name and his affiliation — never anything that
// expresses a choice, commitment, or consent (see the knownLabel matcher in
// planConnpassQuestionnaire below, which is only ever applied to a group's
// single text/textarea field — never to a radio/checkbox/select).
//
// `injectedName` is the attendee name resolved upstream (same value Peatix
// already threads in as peatixAttendeeProfile.name — see native-pass.js's
// productionConfig -> attendeeName, and connector-minimal-production.js's
// readAttendeeName wiring for the Connpass workflow). This function never
// reads process.env itself: called with no injected name (e.g. a test, or a
// caller that never wired one), name resolves to "" and any required
// name-shaped question stays unanswered — fails closed, not silently wrong.
function defaultIdentityAnswers(injectedName) {
  let profile = null;
  try { profile = readLumaFormProfile({ path: DEFAULT_FORM_PROFILE_PATH }); } catch { profile = null; }
  const answers = (profile && profile.form_answers) || {};
  const affiliation = safeIdentityText(answers["所属企業（学校）名"]);
  // The profile file is checked first for a name-shaped answer (there is
  // none today); only when it has nothing does this fall back to the
  // injected name above.
  const nameFromProfile = ["氏名", "お名前", "Name", "name"]
    .map((key) => answers[key]).find((value) => typeof value === "string" && value.trim());
  const name = safeIdentityText(nameFromProfile) || safeIdentityText(injectedName);
  return Object.freeze({ name, affiliation });
}

function providerError(message, code, unknownEffect) {
  const error = new Error(message);
  error.code = code;
  error.unknownEffect = unknownEffect === true;
  return error;
}

function verifiedContract(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value)
    || !/^[a-z0-9][a-z0-9._-]{0,63}$/.test(String(value.tenant_id || ""))
    || !/^connpass-event:\/\/event\/[1-9][0-9]*$/.test(String(value.event_ref || ""))
  ) throw providerError("Connpass contract unavailable", "CONNPASS_CONTRACT_UNAVAILABLE", false);
  let url;
  try { url = new URL(String(value.canonical_url || "")); } catch {
    throw providerError("Connpass contract unavailable", "CONNPASS_CONTRACT_UNAVAILABLE", false);
  }
  if (
    url.protocol !== "https:" || url.username || url.password
    || !(url.hostname === "connpass.com" || url.hostname.endsWith(".connpass.com"))
  ) throw providerError("Connpass contract unavailable", "CONNPASS_CONTRACT_UNAVAILABLE", false);
  return value;
}

async function readConnpassRegistrationStateOnPage(page) {
  if (!page || typeof page.evaluate !== "function") {
    throw providerError("Connpass page unavailable", "CONNPASS_PAGE_UNAVAILABLE", false);
  }
  let value;
  try {
    value = await page.evaluate(() => {
      const rawPath = String(location.pathname || "");
      const path = rawPath.toLowerCase();
      const bodyText = String(document.body && document.body.innerText || "");
      const body = bodyText.replace(/\s+/g, " ").trim();
      const lines = bodyText.split(/\r?\n/).map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
      const controls = [...document.querySelectorAll('button,a[role="button"],a.btn,input[type="submit"]')]
        .map((element) => String(element.innerText || element.value || element.getAttribute("aria-label") || "")
          .replace(/\s+/g, " ").trim()).filter(Boolean);
      const exact = (values) => controls.some((control) => values.includes(control));
      if (/\/(?:login|signin)(?:\/|$)/.test(path) || exact(["ログイン", "Login"])) {
        return { state: "login_required" };
      }
      if (/^\/event\/[1-9][0-9]*\/join\/complete\/$/.test(rawPath)) {
        return { state: "registered" };
      }
      if (exact(["参加票を表示", "受付票を見る", "申し込みをキャンセル", "キャンセルする", "Registered"])) {
        return { state: "registered" };
      }
      if (/^\/event\/[1-9][0-9]*\/$/.test(rawPath) && ["抽選待ち", "補欠", "承認待ち", "キャンセル待ち"].some((marker) => lines.includes(marker))) return { state: "pending" };
      if (/受付終了|募集終了|満員|定員に達しました/.test(body)) return { state: "unavailable", reason: "closed" };
      if (exact(["このイベントに申し込む", "イベントに申し込む", "参加申し込み", "申し込む"])) {
        return { state: "absent" };
      }
      return { state: "unknown" };
    });
  } catch {
    throw providerError("Connpass readback unavailable", "CONNPASS_READBACK_UNAVAILABLE", false);
  }
  if (
    !value || typeof value !== "object" || Array.isArray(value)
    || !["absent", "login_required", "registered", "pending", "unavailable", "unknown"].includes(value.state)
  ) throw providerError("Connpass readback unavailable", "CONNPASS_READBACK_UNAVAILABLE", false);
  return Object.freeze({ state: value.state, ...(value.state === "unavailable" ? { reason: "closed" } : {}) });
}

// Rules (owner-decided): a tier qualifies only if it is free (no yen amount),
// not disabled, has room (an "n/m人" cap with n < m, or no cap at all),
// positively identifies a general-attendee tier, and carries none of Dais's
// disqualifying restriction markers. Among qualifying tiers, an in-person one
// is required; first qualifying tier in document order wins. Online-only or
// unlabeled tiers fail closed before confirmation.
const TIER_RESTRICTION_PATTERN = /学生|招待|女性|男性|限定|会員|member|limited|登壇|発表|LT枠|スタッフ|関係者|主催|懇親会のみ/i;
const TIER_GENERAL_PATTERN = /(?:一般(?:参加|枠|席|チケット|受付)|だれでも(?:参加|枠)?|誰でも(?:参加|枠)?|通常(?:参加|枠)?|オープン(?:参加|枠)?|^参加(?:枠)?(?=\s|$)|public\b|open\b|anyone\b|\ball\b)/i;
const TIER_YEN_PATTERN = /¥|\d[,\d]*\s*円/;
const TIER_ONLINE_PATTERN = /オンライン|リモート|配信|視聴|Zoom|Google\s*meet/i;
const TIER_CAPACITY_PATTERN = /(\d+)\s*\/\s*(\d+)\s*人/;

function selectParticipationTierIndex(tiers) {
  const qualifying = (Array.isArray(tiers) ? tiers : [])
    .map((tier, index) => ({
      index,
      disabled: tier && tier.disabled === true,
      label: String((tier && tier.label) || ""),
    }))
    .filter((tier) => {
      if (tier.disabled) return false;
      if (!/無料/.test(tier.label) || TIER_YEN_PATTERN.test(tier.label)) return false;
      if (TIER_RESTRICTION_PATTERN.test(tier.label)) return false;
      if (!TIER_GENERAL_PATTERN.test(tier.label)) return false;
      const capacity = tier.label.match(TIER_CAPACITY_PATTERN);
      if (capacity && !(Number(capacity[1]) < Number(capacity[2]))) return false;
      return true;
    });
  if (qualifying.length === 0) return -1;
  const inPerson = qualifying.find((tier) => !TIER_ONLINE_PATTERN.test(tier.label));
  return inPerson ? inPerson.index : -1;
}

async function selectParticipationTier(participationGroup) {
  const tiers = await participationGroup.evaluateAll((radios) => radios.map((radio) => {
    const row = radio.closest("li,tr,div,label");
    const label = String((row && row.innerText) || "").replace(/\s+/g, " ").trim();
    return { disabled: radio.disabled === true, label };
  }));
  const index = selectParticipationTierIndex(tiers);
  if (index === -1) {
    throw providerError("Connpass participation tier unavailable", "CONNPASS_TIER_UNAVAILABLE", false);
  }
  await participationGroup.nth(index).check();
}

// Real Connpass join pages (measured live, see
// connector-production-browser-harness.js's connpassJoin handling and
// docs/superpowers/plans/2026-08-11-connector-connpass-real-dom-observation-14e.md)
// wrap the participation-type tier radios AND any organizer-added custom
// questions in the same `.question_list` container structure, each with a
// direct `.question` child whose text starts with `必須` (required) or `任意`
// (optional). The tier group is excluded here — it is validated and clicked
// by selectParticipationTier above, not by this check — leaving only
// organizer questionnaire groups. Connpass silently no-ops the confirm click
// when a required questionnaire field is empty, so this must run before any
// radio is checked or the confirm button is clicked.
// Extends the original hard-fail guard: a required organizer question is
// only ever answered by this code when ALL of these hold — (1) it is left
// unanswered, (2) its group holds exactly one free-text (input[type=text] or
// textarea) field, never a radio/checkbox/select — those always express a
// choice, commitment, or consent (the slide-sharing radio on
// mobilus.connpass.com/event/395464/join/ is exactly that: it commits Dais
// to presenting) and this code never touches them — and (3) its stripped
// question label conservatively matches a known factual field (name or
// affiliation). If ANY required question fails that test, nothing is
// filled and nothing is clicked (matches the original all-or-nothing
// fail-closed behaviour) — see submitConnpassOnPage below.
async function planConnpassQuestionnaire(page, identity) {
  const groups = page.locator(".question_list");
  return groups.evaluateAll((elements, ctx) => {
    const knownLabel = {
      name: /^(?:氏名|お名前|名前)$/,
      affiliation: /所属.*(?:学校|会社|企業)|(?:学校|会社|企業).*所属/,
    };
    const pending = [];
    for (const group of elements) {
      if (group.querySelector('input[name="participation_type"]')) continue;
      const questionElement = group.querySelector(":scope > .question");
      const questionText = String((questionElement && (questionElement.textContent || questionElement.innerText)) || "")
        .replace(/\s+/g, " ").trim();
      if (!/^必須/.test(questionText)) continue;
      const fields = [...group.querySelectorAll("input,textarea,select")]
        .filter((field) => String(field.type || "").toLowerCase() !== "hidden" && field.disabled !== true);
      if (fields.length === 0) continue;
      const answered = (field) => {
        const type = String(field.type || "").toLowerCase();
        if (type === "radio" || type === "checkbox") {
          const name = String(field.name || "");
          return name
            ? fields.some((candidate) => String(candidate.name || "") === name && candidate.checked === true)
            : field.checked === true;
        }
        return String(field.value || "").trim().length > 0;
      };
      if (fields.every(answered)) continue;
      const label = questionText.replace(/^必須\s*/, "").trim();
      const key = knownLabel.name.test(label) ? "name" : knownLabel.affiliation.test(label) ? "affiliation" : null;
      const kind = fields.length === 1
        ? (String(fields[0].tagName || "").toLowerCase() === "textarea" ? "textarea" : String(fields[0].type || "text").toLowerCase())
        : null;
      const value = key ? String(ctx[key] || "") : "";
      const eligible = Boolean(key) && ["text", "textarea"].includes(kind) && value.length > 0;
      pending.push({ field: eligible ? fields[0] : null, value, eligible });
    }
    if (pending.some((entry) => !entry.eligible)) return { blocked: true, filled: 0 };
    let filled = 0;
    for (const entry of pending) {
      entry.field.value = entry.value;
      if (typeof entry.field.dispatchEvent === "function" && typeof Event === "function") {
        try {
          entry.field.dispatchEvent(new Event("input", { bubbles: true }));
          entry.field.dispatchEvent(new Event("change", { bubbles: true }));
        } catch { /* best-effort reactivity nudge only */ }
      }
      filled += 1;
    }
    return { blocked: false, filled };
  }, { name: identity.name, affiliation: identity.affiliation });
}

async function submitConnpassOnPage(page, _contract, dependencies = {}) {
  const readState = dependencies.readState || readConnpassRegistrationStateOnPage;
  const identity = dependencies.identity || defaultIdentityAnswers(dependencies.attendeeName);
  const before = await readState(page);
  if (["registered", "pending"].includes(before.state)) {
    return { status: before.state, effect_started: false };
  }
  // The join control is already known to be a login wall for anonymous
  // visitors (see readConnpassRegistrationStateOnPage's own "login_required"
  // detection above) — distinguished from every other non-absent state so
  // the caller can report a session problem instead of a generic
  // registration-unavailable symptom. Nothing has been clicked yet.
  if (before.state === "login_required") {
    throw providerError("Connpass session expired", "CONNPASS_SESSION_EXPIRED", false);
  }
  if (before.state !== "absent") {
    throw providerError(`Connpass registration ${before.state}`, "CONNPASS_REGISTRATION_UNAVAILABLE", false);
  }
  if (
    !page || typeof page.getByRole !== "function" || typeof page.waitForTimeout !== "function"
    || typeof page.locator !== "function" || typeof page.url !== "function" || typeof page.goto !== "function"
  ) {
    throw providerError("Connpass control unavailable", "CONNPASS_CONTROL_UNAVAILABLE", false);
  }
  // Captured before anything is clicked: the confirm click lands on
  // connpass's own post-submission page, not the event page, so the
  // readback below needs to navigate back here to see the registered
  // controls at all.
  const eventUrl = String(page.url() || "");
  if (!eventUrl) {
    throw providerError("Connpass control unavailable", "CONNPASS_CONTROL_UNAVAILABLE", false);
  }
  const joinControl = page.getByRole("link", {
    name: /^(?:このイベントに申し込む|イベントに申し込む|参加申し込み|申し込む)$/,
    exact: true,
  }).first();
  if (await joinControl.count() !== 1 || !await joinControl.isVisible()) {
    throw providerError("Connpass control unavailable", "CONNPASS_CONTROL_UNAVAILABLE", false);
  }

  // Clicking the join link only navigates to the /join/ page — it does not
  // register anyone. Nothing below this point has an external effect until
  // the confirm control is clicked, so failures here are safe to retry.
  let confirmControl;
  try {
    await joinControl.click();
    await page.waitForTimeout(1_000);

    // Fail closed before touching anything else on the join page: a required
    // organizer questionnaire field left empty makes Connpass silently no-op
    // the confirm click later (page stays on /join/, nothing registers). See
    // planConnpassQuestionnaire above for what this matches on and fills.
    const questionnairePlan = await planConnpassQuestionnaire(page, identity);
    if (!questionnairePlan || questionnairePlan.blocked !== false) {
      throw providerError("Connpass questionnaire requires an answer", "CONNPASS_QUESTIONNAIRE_REQUIRED", false);
    }

    // On the join page: validate BOTH the participation-type radio and the
    // free-confirm control before touching either. Fail closed — nothing is
    // clicked on this page until both targets are proven present.
    const participationGroup = page.locator('input[name="participation_type"]');
    if (await participationGroup.count() < 1) {
      throw providerError("Connpass control unavailable", "CONNPASS_CONTROL_UNAVAILABLE", false);
    }
    confirmControl = page.locator("button#FreeButton");
    const confirmLabel = (await confirmControl.count() === 1 && await confirmControl.isVisible())
      ? String((await confirmControl.innerText()) || "").replace(/\s+/g, " ").trim()
      : "";
    if (confirmLabel !== "申し込みを確定する") {
      throw providerError("Connpass confirm control unavailable", "CONNPASS_CONFIRM_UNAVAILABLE", false);
    }
    // Pick the first free, open, unrestricted in-person tier in document
    // order (see selectParticipationTierIndex). Fails closed with
    // CONNPASS_TIER_UNAVAILABLE if none qualify — nothing below this point is
    // clicked in that case.
    await selectParticipationTier(participationGroup);
  } catch (error) {
    if (error && typeof error.unknownEffect === "boolean") throw error;
    throw providerError("Connpass control unavailable", "CONNPASS_CONTROL_UNAVAILABLE", false);
  }

  // Past this point the confirm control is about to be clicked. Any failure
  // from here on is an unknown external effect — never safe to retry — even
  // if the underlying error would otherwise look like a known/safe one. That
  // includes the navigation back to the event page and the readback below:
  // the registration itself already happened, so neither can be treated as
  // safe-to-retry just because their own error code would normally be.
  try {
    await confirmControl.click();
    await page.waitForTimeout(1_000);
    await page.goto(eventUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
    const after = await readState(page);
    return { status: after.state, effect_started: true };
  } catch {
    throw providerError("Connpass browser action failed", "CONNPASS_BROWSER_ACTION_FAILED", true);
  }
}

function createConnpassBrowserProvider(options = {}) {
  const dailyDriver = options.dailyDriver;
  const evidenceStore = options.evidenceStore;
  const readState = options.readState || readConnpassRegistrationStateOnPage;
  const submitOnPage = options.submitOnPage || ((page, contract) => (
    submitConnpassOnPage(page, contract, { readState })
  ));
  const now = options.now || (() => new Date().toISOString());
  if (!dailyDriver || typeof dailyDriver.withEventPage !== "function") {
    throw new Error("Connpass browser provider daily-driver unavailable");
  }
  if (!evidenceStore || typeof evidenceStore.record !== "function") {
    throw new Error("Connpass browser provider evidence store unavailable");
  }

  async function proof(page, contract, registrationStatus) {
    if (!page || typeof page.screenshot !== "function") {
      throw providerError("Connpass screenshot unavailable", "CONNPASS_EVIDENCE_UNAVAILABLE", true);
    }
    const screenshot = await page.screenshot({ type: "png", fullPage: true });
    const refs = await evidenceStore.record({
      tenantId: contract.tenant_id, eventRef: contract.event_ref, observedAt: now(), screenshot,
    });
    return Object.freeze({
      state: "registered", registration_status: registrationStatus,
      external_receipt_ref: refs.external_receipt_ref,
      artifact_ref: refs.artifact_ref,
      canonical_url: contract.canonical_url,
    });
  }

  return Object.freeze({
    inspectRegistration(rawContract) {
      const contract = verifiedContract(rawContract);
      return dailyDriver.withEventPage("connpass", contract.canonical_url, async (page) => {
        const state = await readState(page);
        if (["registered", "pending"].includes(state.state)) return proof(page, contract, state.state);
        return state;
      });
    },
    submitRegistration(rawContract) {
      const contract = verifiedContract(rawContract);
      return dailyDriver.withEventPage("connpass", contract.canonical_url, async (page) => {
        const before = await readState(page);
        if (["registered", "pending"].includes(before.state)) return proof(page, contract, before.state);
        if (before.state !== "absent") {
          throw providerError("Connpass registration unavailable", "CONNPASS_REGISTRATION_UNAVAILABLE", false);
        }
        const outcome = await submitOnPage(page, contract);
        if (!outcome || !["registered", "pending"].includes(outcome.status)) {
          throw providerError("Connpass result unverified", "CONNPASS_RESULT_UNVERIFIED", outcome && outcome.effect_started);
        }
        return proof(page, contract, outcome.status);
      });
    },
  });
}

module.exports = {
  createConnpassBrowserProvider,
  readConnpassRegistrationStateOnPage,
  submitConnpassOnPage,
};
