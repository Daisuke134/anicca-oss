// LM-33c: server-rendered, read-only mirror for the Life Manager panel.
"use strict";

const { SECRET_PATTERNS } = require("./panel-display-policy.js");
const { renderMoneyPrinterWebMcpScript } = require("./money-printer-webmcp.js");
const { roundedScoreValue } = require("./panel-score-semantics.js");
const {
  SCORE_COMPONENT_KEYS,
  SCORE_COMPONENT_LABELS,
  SCORE_LABELS,
  SCORE_PERIOD_KINDS,
  scoreAsDate,
  scoreComponentRatio,
  scoreExactKeys,
  scoreNonNegativeInteger,
  validScoreComponents,
  validScoreOrgan,
} = require("./panel-score-display-contract.js");
const DISPLAY_SECRET_PATTERNS = Object.freeze(SECRET_PATTERNS.map((pattern) => Object.freeze({
  source: pattern.source,
  flags: pattern.flags,
})));

function scoreEscapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
  });
}

function scoreFormatDate(value) {
  const date = scoreAsDate(value);
  if (!date) return "";
  return new Intl.DateTimeFormat("ja-JP", { month: "short", day: "numeric" }).format(date);
}

function renderScoreComponents(name, organ) {
  const rows = Object.keys(organ.components).filter(function (key) { return key !== "timezone"; }).map(function (key) {
    const value = organ.components[key];
    const displayValue = value == null ? "—" : (typeof value === "boolean" ? (value ? "はい" : "いいえ") : value);
    return '<li><span>' + scoreEscapeHtml(SCORE_COMPONENT_LABELS[name][key]) + '</span>: ' + scoreEscapeHtml(displayValue) + '</li>';
  }).join("");
  return rows ? '<ul class="score-components">' + rows + '</ul>' : "";
}

function renderScoreCards(data) {
  if (!data || typeof data !== "object" || Array.isArray(data) || !scoreExactKeys(data, ["organs"])) throw new Error("invalid score payload");
  const organs = data.organs;
  if (!organs || typeof organs !== "object" || Array.isArray(organs) || !scoreExactKeys(organs, Object.keys(SCORE_LABELS))) throw new Error("invalid score payload");
  const rows = ["daily", "physical", "mental", "financial"].map(function (name) {
    const organ = organs[name];
    if (!validScoreOrgan(name, organ)) throw new Error("invalid score payload");
    const value = organ.status === "measured"
      ? '<span class="score-value">' + organ.value + '<small>/100</small></span><div class="score-track" aria-label="' + scoreEscapeHtml(SCORE_LABELS[name]) + ' score"><span style="width:' + organ.value + '%"></span></div>'
      : '<span class="score-ready">' + (organ.status === "insufficient_data" ? "insufficient data" : "invalid data") + '</span>';
    const ratio = organ.status === "invalid_data" ? "not measurable" : organ.numerator + " / " + organ.denominator;
    const period = organ.period.kind.replace(/_/g, " ") + " · " + scoreFormatDate(organ.period.start_at) + " → " + scoreFormatDate(organ.period.end_at) + " · " + String(organ.components.timezone || "UTC");
    return '<article class="score-item" data-score-organ="' + name + '"><p class="score-name">' + SCORE_LABELS[name] + '</p>' + value + '<p class="score-caption">outcomes ' + scoreEscapeHtml(ratio) + '</p><p class="score-reason">' + scoreEscapeHtml(organ.reason) + '</p><p class="score-period">' + scoreEscapeHtml(period) + '</p>' + renderScoreComponents(name, organ) + '<p class="score-sources">根拠 ' + organ.source_outcome_ids.length + '件</p></article>';
  }).join("");
  return '<div class="score-grid">' + rows + '</div>';
}

function renderPanelOnboardingPage(options = {}) {
  const csrf = scoreEscapeHtml(options.csrf || "");
  return `<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="referrer" content="no-referrer">
  <title>Life Manager</title>
  <style>
    :root { --paper:#f3efe5; --paper-bright:#fbf8f0; --ink:#122238; --ink-soft:#536070; --line:#cfc7b8; --line-dark:#9d9484; --accent:#c94a32; --success:#26735b; }
    * { box-sizing:border-box; }
    html { background:var(--paper); }
    body { margin:0; min-width:0; min-height:100vh; overflow-x:hidden; color:var(--ink); background:radial-gradient(circle at 8% 4%,rgba(201,74,50,.1),transparent 26rem),var(--paper); font-family:"Avenir Next",Avenir,"Hiragino Sans","Yu Gothic",sans-serif; -webkit-font-smoothing:antialiased; }
    a { color:inherit; }
    .onboarding-shell { width:min(640px,calc(100% - 32px)); margin:0 auto; padding:clamp(28px,9vw,80px) 0 64px; }
    .onboarding-wordmark { margin:0 0 32px; font-size:.72rem; font-weight:700; letter-spacing:.19em; text-transform:uppercase; }
    .onboarding-card { padding:clamp(24px,7vw,48px); border:1px solid var(--line-dark); background:rgba(251,248,240,.9); box-shadow:0 24px 70px rgba(38,35,30,.12); }
    h1 { margin:0; font-family:"Iowan Old Style","YuMincho","Hiragino Mincho ProN",serif; font-size:clamp(2.1rem,9vw,4.5rem); font-weight:500; line-height:1; letter-spacing:-.04em; overflow-wrap:anywhere; }
    .onboarding-copy { margin:20px 0 0; color:var(--ink-soft); line-height:1.7; overflow-wrap:anywhere; }
    .onboarding-form { display:grid; gap:14px; margin-top:32px; }
    .onboarding-form label { display:grid; gap:8px; font-size:.78rem; font-weight:700; }
    .onboarding-form input { width:100%; min-height:48px; padding:10px 12px; border:1px solid var(--line-dark); border-radius:0; background:white; color:var(--ink); font:inherit; }
    .onboarding-actions { display:grid; gap:10px; margin-top:30px; }
    .onboarding-actions button, .onboarding-actions a { display:inline-flex; min-height:48px; align-items:center; justify-content:center; padding:12px 18px; border:1px solid var(--ink); border-radius:0; background:transparent; color:var(--ink); font:700 .82rem/1.2 inherit; text-align:center; text-decoration:none; cursor:pointer; overflow-wrap:anywhere; }
    .onboarding-actions .primary-action { border-color:var(--accent); background:var(--accent); color:white; }
    .onboarding-actions .secondary-action { border-color:var(--line-dark); color:var(--ink-soft); }
    .onboarding-status { min-height:1.5em; margin:22px 0 0; color:var(--ink-soft); font-size:.8rem; line-height:1.6; }
    @media (max-width:375px) { .onboarding-shell { width:calc(100% - 24px); padding-top:24px; } .onboarding-card { padding:22px 18px; } .onboarding-actions button, .onboarding-actions a { width:100%; } }
  </style>
</head>
<body>
  <main class="onboarding-shell" data-panel-onboarding data-csrf="${csrf}">
    <p class="onboarding-wordmark">Life Manager / life operations</p>
    <section class="onboarding-card" aria-live="polite">
      <h1 data-onboarding-title>Life Manager</h1>
      <p class="onboarding-copy" data-onboarding-copy>現在の準備状態を確認しています。</p>
      <div class="onboarding-form" data-onboarding-form></div>
      <div class="onboarding-actions" data-onboarding-actions></div>
      <p class="onboarding-status" data-onboarding-status role="status"></p>
    </section>
  </main>
  <script>
  (() => {
    const root = document.querySelector("[data-panel-onboarding]");
    if (!root) return;
    const title = root.querySelector("[data-onboarding-title]");
    const copy = root.querySelector("[data-onboarding-copy]");
    const form = root.querySelector("[data-onboarding-form]");
    const actions = root.querySelector("[data-onboarding-actions]");
    const status = root.querySelector("[data-onboarding-status]");
    const csrf = root.dataset.csrf || "";
    const endpoint = "/api/panel/onboarding";
    const calendarStart = "/api/panel/onboarding/calendar/start";
    const calendarStatus = "/api/panel/onboarding/calendar/status";
    const titles = Object.freeze({ name:"名前を確認", calendar:"カレンダーをつなぐ", home:"自宅の住所", notifications:"通知を有効にする", phone:"電話通知（任意）", call:"電話通知の確認", dashboard:"準備完了" });
    const copies = Object.freeze({ name:"呼びかけに使う名前を確認します。", calendar:"予定を読み取り、必要なタイミングで知らせます。", home:"普段の出発地点として使う自宅の住所を入力してください。", notifications:"通知を受け取れるようにします。", phone:"電話通知は任意です。出発時刻の10分前と5分前に電話します。", call:"登録した番号への電話通知をオンにするか選びます。", dashboard:"準備状態を確認しました。" });
    const readyCopy = () => "準備できました。予定に合わせて移動時間を確保し、出発前にTelegramで経路を知らせます。";
    const key = () => globalThis.crypto && typeof globalThis.crypto.randomUUID === "function" ? globalThis.crypto.randomUUID() : String(Date.now()) + "-onboarding" + Math.random().toString(36).slice(2);
    const text = (node, value) => { node.textContent = String(value == null ? "" : value); };
    const button = (label, action, primary) => { const node = document.createElement("button"); node.type = "button"; node.dataset.onboardingAction = action; node.className = primary ? "primary-action" : "secondary-action"; node.textContent = label; return node; };
    const input = (label, type, value) => { const wrapper = document.createElement("label"); const caption = document.createElement("span"); caption.textContent = label; const field = document.createElement("input"); field.type = type; field.value = String(value || ""); field.autocomplete = type === "tel" ? "tel" : "off"; wrapper.append(caption, field); return wrapper; };
    const safeRedirect = (value) => { try { const url = new URL(value); if (url.protocol !== "https:" || url.username || url.password || /[\\r\\n]/.test(String(value))) return ""; return url.toString(); } catch { return ""; } };
    const request = async (path, init = {}) => { const { mutating, ...requestInit } = init; const headers = { Accept: "application/json", ...(mutating ? { "content-type":"application/json", "x-lm-csrf":csrf, "idempotency-key":key() } : {}) }; const response = await fetch(path, { ...requestInit, credentials:"same-origin", headers }); if (response.status === 401) { window.location.reload(); throw new Error("session expired"); } const data = await response.json().catch(() => ({})); if (!response.ok) { const error = new Error(String(data.error || "request failed")); error.status = response.status; throw error; } return data; };
    const renderReadyValue = (state) => {
      const value = document.createElement("div");
      value.className = "onboarding-ready";
      const next = state && state.nextEvent && typeof state.nextEvent === "object" ? state.nextEvent : null;
      if (next) {
        const preview = document.createElement("p");
        const label = document.createElement("span");
        const summary = document.createElement("span");
        const separator = document.createElement("span");
        const start = document.createElement("span");
        text(label, "次の予定: ");
        text(summary, next.summary || "予定");
        text(separator, " · ");
        text(start, next.startAt || "時刻未定");
        preview.append(label, summary, separator, start);
        value.append(preview);
      }
      return value;
    };
    const retryButton = () => button("再読み込み", "retry", true);
    const render = (state) => {
      const step = String(state && state.step || "");
      title.textContent = titles[step] || "Life Manager";
      copy.textContent = step === "dashboard" ? readyCopy(state) : copies[step] || "現在の準備状態を確認しています。";
      form.replaceChildren(); actions.replaceChildren(); status.textContent = "";
      switch (step) {
        case "name": form.append(input("名前", "text", state.name)); actions.append(button("次へ", "name.save", true)); break;
        case "calendar": actions.append(button("カレンダーをつなぐ", "calendar.start", true)); break;
        case "home": form.append(input("自宅の住所", "text", state.homeAddress)); actions.append(button("次へ", "home.save", true)); break;
        case "notifications": actions.append(button("通知を有効にする", "notifications.enable", true)); break;
        case "phone": form.append(input("電話番号", "tel", state.phone)); actions.append(button("次へ", "phone.save", true), button("電話番号を登録せず続ける", "phone.skip", false)); break;
        case "call": actions.append(button("電話で確認する", "call.enable", true), button("電話での確認をスキップ", "call.skip", false)); break;
        case "dashboard": { form.append(renderReadyValue(state)); const link = document.createElement("a"); link.href = "/panel"; link.className = "primary-action"; link.textContent = "ダッシュボードを開く"; actions.append(link); break; }
        default: text(status, "現在の準備状態を確認できません。"); actions.append(retryButton());
      }
    };
    const load = async () => { try { render(await request(endpoint)); } catch { text(status, "現在の準備状態を取得できません。再読み込みしてください。"); form.replaceChildren(); actions.replaceChildren(retryButton()); } };
    const mutate = async (action, payload = {}) => { try { await request(endpoint, { method:"POST", mutating:true, body:JSON.stringify({ action, payload }) }); await load(); } catch (error) { if (error.status === 409) await load(); else text(status, "更新できませんでした。現在の状態は変更されていません。"); } };
    const startCalendar = async () => { try { const current = await request(calendarStatus); if (current.connected === true || current.state === "connected") return load(); const result = await request(calendarStart, { method:"POST", mutating:true, body:"{}" }); if (result.connected === true || result.state === "connected") return load(); const target = safeRedirect(result.redirectUrl); if (!target) throw new Error("calendar redirect unavailable"); window.location.assign(target); } catch (error) { if (error.status === 409) await load(); else text(status, "カレンダーをつなげませんでした。もう一度お試しください。"); } };
    root.addEventListener("click", (event) => { const target = event.target.closest("[data-onboarding-action]"); if (!target) return; const action = target.dataset.onboardingAction; if (action === "calendar.start") return startCalendar(); if (action === "retry") return window.location.reload(); const field = form.querySelector("input"); const payload = action === "name.save" ? { name: field ? field.value : "" } : action === "home.save" ? { home_address: field ? field.value : "" } : action === "phone.save" ? { phone: field ? field.value : "" } : {}; mutate(action, payload); });
    load();
  })();
  </script>
</body>
</html>`;
}

function renderPanelPage(options = {}) {
  const guest = options.guest === true;
  const guestBanner = guest ? '<p class="guest-banner" data-guest-banner role="status">Judge guest — isolated workroom</p>' : "";
  const mastheadNote = guest
    ? "Add a paid opportunity and follow its cloud Money Printer workroom."
    : "今日はここまで整っています。予定、電話、つながっている context を、ひと目で確認できます。";
  const statusLabel = guest ? "JUDGE GUEST / ISOLATED WORKROOM" : "PERSONAL CONTROL CENTER";
  const mirrorNote = guest
    ? '<p class="mirror-note"><strong>Judge view</strong><span>This isolated guest workroom uses the real cloud agent and keeps other users private.</span></p>'
    : '<p class="mirror-note"><strong>あなたの状態と接続だけを表示しています。</strong><span>対応している設定はここでも Telegram でも同じように変更できます。</span></p>';
  const logoutMarkup = guest ? "" : '<form action="/panel/logout" method="post"><button class="control-action" type="submit">Logout</button></form>';
  const logoutScript = guest ? "" : `    const logout = document.querySelector('form[action="/panel/logout"]');
    if (logout) logout.addEventListener("submit", function (event) { event.preventDefault(); fetch("/panel/logout", { method: "POST", credentials: "same-origin", headers: { "x-lm-csrf": controlCsrf || "${String(options.csrf || "")}" } }).then(function () { window.location.href = "/panel"; }); });`;
  return `<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="referrer" content="no-referrer">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='12' fill='%23122238'/%3E%3Ccircle cx='32' cy='32' r='11' fill='%23c94a32'/%3E%3C/svg%3E">
  <title>Life Manager</title>
  <style>
    :root {
      --paper: #f3efe5;
      --paper-bright: #fbf8f0;
      --ink: #122238;
      --ink-soft: #536070;
      --line: #cfc7b8;
      --line-dark: #9d9484;
      --accent: #c94a32;
      --accent-soft: #f1d7cc;
      --success: #26735b;
      --success-soft: #dcebe3;
      --shadow: 0 24px 70px rgba(38, 35, 30, 0.12);
    }

    * { box-sizing: border-box; }

    html { background: var(--paper); }

    body {
      margin: 0;
      min-width: 0;
      min-height: 100vh;
      overflow-x: hidden;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 4%, rgba(201, 74, 50, 0.10), transparent 26rem),
        linear-gradient(rgba(18, 34, 56, 0.028) 1px, transparent 1px),
        var(--paper);
      background-size: auto, 100% 28px, auto;
      font-family: "Avenir Next", Avenir, "Hiragino Sans", "Yu Gothic", sans-serif;
      -webkit-font-smoothing: antialiased;
    }

    a { color: inherit; }

    .page {
      width: min(1180px, calc(100% - 48px));
      margin: 0 auto;
      padding: 30px 0 64px;
    }

    .masthead {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 28px;
      align-items: end;
      padding: 24px 2px 34px;
      border-bottom: 1px solid var(--ink);
      animation: reveal 480ms ease-out both;
    }

    .wordmark {
      margin: 0 0 30px;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.19em;
      text-transform: uppercase;
    }

    h1 {
      max-width: 18ch;
      margin: 0;
      font-family: "Iowan Old Style", "YuMincho", "Hiragino Mincho ProN", serif;
      font-size: clamp(2.35rem, 6vw, 5.2rem);
      font-weight: 500;
      line-height: 0.94;
      letter-spacing: -0.045em;
    }

    .masthead-note {
      width: min(28rem, 36vw);
      margin: 0;
      color: var(--ink-soft);
      font-size: 0.94rem;
      line-height: 1.75;
    }

    .status-line {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 18px;
      color: var(--ink);
      font-size: 0.77rem;
      font-weight: 700;
      letter-spacing: 0.08em;
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 0 5px var(--success-soft);
    }

    .mirror-note {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      margin: 20px 0;
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
      color: var(--ink-soft);
      font-size: 0.78rem;
      line-height: 1.6;
      animation: reveal 480ms 80ms ease-out both;
    }

    .mirror-note strong { color: var(--ink); }
    .guest-banner { margin: 18px 0 0; padding: 12px 14px; border: 1px solid var(--accent); background: var(--accent-soft); color: var(--ink); font-weight: 800; line-height: 1.5; }

    .panel-grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 16px;
    }

    .panel-section {
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--line-dark);
      background: rgba(251, 248, 240, 0.88);
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.7) inset;
      animation: reveal 540ms ease-out both;
    }

    .panel-section:nth-child(1) { grid-column: span 7; animation-delay: 120ms; }
    .panel-section:nth-child(2) { grid-column: span 5; animation-delay: 170ms; }
    .panel-section:nth-child(3) { grid-column: span 7; animation-delay: 220ms; }
    .panel-section:nth-child(4) { grid-column: span 5; animation-delay: 270ms; }
    .panel-section:nth-child(5) { grid-column: span 12; animation-delay: 320ms; }
    .panel-section:nth-child(6) { grid-column: span 12; animation-delay: 360ms; }
    .panel-section[data-panel-section="money-printer"] { grid-column: span 12; }
    .money-metrics, .money-board { display: grid; gap: 10px; }
    .money-metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 18px; }
    .money-board { grid-template-columns: repeat(6, minmax(9rem, 1fr)); overflow-x: auto; }
    .money-metric, .money-lane { min-width: 0; padding: 12px; border: 1px solid var(--line); }
    .money-metric strong { display: block; margin-top: 7px; font-family: "Iowan Old Style", serif; font-size: 1.45rem; }
    .money-lane h3 { margin: 0 0 10px; font-size: .72rem; letter-spacing: .05em; }
    .money-card { width: 100%; margin: 8px 0 0; padding: 10px; border: 1px solid transparent; border-left: 3px solid var(--accent); background: var(--paper-bright); color: var(--ink); font: inherit; text-align: left; cursor: pointer; }
    .money-card:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }
    .money-card p { margin: 4px 0 0; color: var(--ink-soft); font-size: .7rem; }
    .money-human-task { margin-top: 14px; padding: 14px; border: 1px solid var(--accent); }
    .money-human-actions { display: flex; gap: 8px; margin-top: 10px; }
    .money-browser-takeover { width: 100%; height: min(58vh, 520px); margin-top: 12px; border: 1px solid var(--line-dark); }
    .money-webmcp-status { margin: 14px 0 0; color: var(--ink-soft); font-size: .78rem; }

    .section-head {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: baseline;
      padding: 17px 20px 15px;
      border-bottom: 1px solid var(--line);
    }

    h2 {
      margin: 0;
      font-family: "Iowan Old Style", "YuMincho", "Hiragino Mincho ProN", serif;
      font-size: 1.28rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }

    .section-kicker {
      color: var(--ink-soft);
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .section-body {
      min-height: 136px;
      padding: 20px;
      overflow-wrap: anywhere;
    }

    .loading,
    .empty,
    .error {
      display: grid;
      place-items: center;
      min-height: 98px;
      margin: 0;
      color: var(--ink-soft);
      text-align: center;
      line-height: 1.7;
    }

    .error { color: #8d3527; }

    .timeline-list,
    .gate-list,
    .ledger-list {
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .timeline-summary {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin: 0 0 18px;
      color: var(--ink-soft);
      font-size: 0.78rem;
    }

    .timeline-item {
      position: relative;
      display: grid;
      grid-template-columns: 4.8rem minmax(0, 1fr) auto;
      gap: 14px;
      align-items: start;
      padding: 17px 0;
      border-top: 1px solid var(--line);
    }

    .timeline-item:first-child { border-top-color: var(--ink); }

    .timeline-time {
      font-family: "Iowan Old Style", "YuMincho", serif;
      font-size: 1.15rem;
      font-variant-numeric: tabular-nums;
    }

    .timeline-title { margin: 0; font-weight: 700; line-height: 1.45; }

    .timeline-meta {
      margin: 5px 0 0;
      color: var(--ink-soft);
      font-size: 0.76rem;
      line-height: 1.55;
    }

    .call-mark {
      white-space: nowrap;
      color: var(--success);
      font-size: 0.75rem;
      font-weight: 800;
    }

    .score-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      min-height: 190px;
    }

    .score-item {
      min-width: 0;
      padding: 19px 16px;
      border-left: 1px solid var(--line);
    }

    .score-item:first-child { border-left: 0; }

    .score-name {
      margin: 0;
      color: var(--ink-soft);
      font-size: 0.66rem;
      font-weight: 800;
      letter-spacing: 0.11em;
    }

    .score-value {
      display: block;
      min-height: 3.6rem;
      margin: 16px 0 8px;
      font-family: "Iowan Old Style", "YuMincho", serif;
      font-size: clamp(2rem, 5vw, 3.2rem);
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }

    .score-value small { font-size: 0.82rem; }

    .score-ready {
      display: inline-block;
      margin: 16px 0 8px;
      padding: 8px 10px;
      border: 1px solid var(--line-dark);
      color: var(--ink-soft);
      font-size: 0.73rem;
      font-weight: 700;
    }

    .score-track {
      height: 3px;
      overflow: hidden;
      background: var(--line);
    }

    .score-track > span { display: block; height: 100%; background: var(--accent); }

    .score-caption {
      margin: 11px 0 0;
      color: var(--ink-soft);
      font-size: 0.7rem;
      line-height: 1.55;
    }

    .score-reason { margin: 12px 0 0; color: var(--ink); line-height: 1.55; }
    .score-period, .score-sources { margin: 8px 0 0; color: var(--ink-soft); font-size: 0.78rem; }
    .score-components { margin: 10px 0 0; padding-left: 18px; color: var(--ink-soft); font-size: 0.78rem; line-height: 1.5; }

    .ledger-empty {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 22px;
      align-items: end;
      min-height: 126px;
    }

    .ledger-empty h3 {
      max-width: 14ch;
      margin: 0;
      font-family: "Iowan Old Style", "YuMincho", serif;
      font-size: clamp(1.7rem, 4vw, 2.65rem);
      font-weight: 500;
      line-height: 1.08;
    }

    .ledger-cost {
      margin: 0;
      color: var(--ink-soft);
      font-size: 0.72rem;
      text-align: right;
    }

    .ledger-item {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      padding: 14px 0;
      border-top: 1px solid var(--line);
    }

    .ledger-item:first-child { border-top-color: var(--ink); }
    .ledger-item p { margin: 0; }
    .ledger-item-meta { color: var(--ink-soft); font-size: 0.72rem; margin-top: 4px !important; }
    .ledger-amount { font-family: "Iowan Old Style", serif; font-size: 1.15rem; }
    .ledger-link { color: var(--accent); font-size: 0.72rem; font-weight: 700; }

    .gate-item {
      padding: 17px 0;
      border-top: 1px solid var(--line);
    }

    .gate-item:first-child { padding-top: 0; border-top: 0; }
    .gate-item:last-child { padding-bottom: 0; }

    .gate-title-row {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: baseline;
    }

    .gate-title { margin: 0; font-size: 0.92rem; }

    .gate-status {
      flex: none;
      padding: 4px 7px;
      border: 1px solid var(--accent);
      color: var(--accent);
      font-size: 0.65rem;
      font-weight: 800;
    }

    .gate-status.is-open { border-color: var(--success); color: var(--success); }

    .gate-copy {
      margin: 10px 0 0;
      color: var(--ink-soft);
      font-size: 0.76rem;
      line-height: 1.65;
      white-space: pre-line;
    }

    .settings-grid {
      display: grid;
      grid-template-columns: 1fr 1.45fr 2fr;
      gap: 0;
    }

    .setting-group {
      min-width: 0;
      padding: 0 22px;
      border-left: 1px solid var(--line);
    }

    .setting-group:first-child { padding-left: 0; border-left: 0; }
    .setting-group:last-child { padding-right: 0; }
    .setting-label { margin: 0 0 10px; color: var(--ink-soft); font-size: 0.7rem; font-weight: 800; letter-spacing: 0.08em; }
    .setting-value { margin: 0; font-size: 0.92rem; line-height: 1.6; }

    .connection-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .connection {
      display: inline-flex;
      gap: 7px;
      align-items: center;
      padding: 7px 9px;
      border: 1px solid var(--line);
      font-size: 0.7rem;
    }

    .connection::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--line-dark); }
    .connection.is-on::before { background: var(--success); }

    .control-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .control-card { display: grid; align-content: start; gap: 10px; min-height: 170px; padding: 16px; border: 1px solid var(--line); background: var(--paper-bright); }
    .control-card h3, .control-card p { margin: 0; }
    .control-state { color: var(--ink-soft); font-size: .72rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
    .control-reason { color: var(--ink-soft); font-size: .78rem; line-height: 1.6; }
    .control-action, .setting-switch, .setting-select select { min-height: 44px; border: 1px solid var(--ink); border-radius: 0; padding: 8px 12px; background: var(--ink); color: var(--paper-bright); font: inherit; font-weight: 700; cursor: pointer; }
    .control-action:disabled, .setting-switch:disabled, .setting-select select:disabled { cursor: wait; opacity: .55; }
    .control-action:focus-visible, .setting-switch:focus-visible, .setting-select select:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }
    .settings-controls { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 18px; }
    .setting-switch[aria-checked="true"] { background: var(--success); }
    .setting-select { display: grid; gap: 6px; color: var(--ink-soft); font-size: .68rem; font-weight: 800; letter-spacing: .08em; }
    .setting-select select { width: 100%; min-width: 0; letter-spacing: 0; }
    .action-status { min-height: 1.5rem; margin: 14px 0 0; color: var(--ink-soft); font-size: .78rem; }

    @keyframes reveal {
      from { opacity: 0; transform: translateY(9px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @media (max-width: 820px) {
      .masthead { grid-template-columns: 1fr; }
      .masthead-note { width: min(36rem, 100%); }
      .panel-section:nth-child(n) { grid-column: span 12; }
    }

    @media (max-width: 640px) {
      .page { width: min(100% - 24px, 1180px); padding-top: 14px; }
      .masthead { grid-template-columns: 1fr; gap: 20px; padding: 18px 0 24px; }
      .wordmark { margin-bottom: 22px; }
      h1 { font-size: clamp(2.5rem, 14vw, 4.1rem); }
      .mirror-note { display: block; }
      .mirror-note span { display: block; margin-top: 6px; }
      .panel-grid { grid-template-columns: 1fr; gap: 12px; }
      .panel-section:nth-child(n) { grid-column: 1; }
      .section-head, .section-body { padding-left: 16px; padding-right: 16px; }
      .timeline-item { grid-template-columns: 4rem minmax(0, 1fr); }
      .call-mark { grid-column: 2; }
      .score-grid { grid-template-columns: 1fr; }
      .score-item { border-left: 0; border-top: 1px solid var(--line); }
      .score-item:first-child { border-top: 0; }
      .ledger-empty { grid-template-columns: 1fr; }
      .ledger-cost { text-align: left; }
      .settings-grid { grid-template-columns: 1fr; }
      .control-grid, .settings-controls { grid-template-columns: 1fr; }
      .setting-group { padding: 17px 0; border-left: 0; border-top: 1px solid var(--line); }
      .setting-group:first-child { padding-top: 0; border-top: 0; }
      .setting-group:last-child { padding-bottom: 0; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.01ms !important; animation-delay: 0ms !important; }
    }
  </style>
</head>
<body${guest ? ' data-guest-mode="true"' : ""}>
  <div class="page">
    <header class="masthead">
      <div>
        <p class="wordmark">Life Manager / life operations</p>
        <h1>Life Manager</h1>
      </div>
      <div>
        <p class="masthead-note">${mastheadNote}</p>
        <div class="status-line"><span class="status-dot" aria-hidden="true"></span>${statusLabel}</div>
        ${logoutMarkup}
      </div>
    </header>

    ${guestBanner}
    ${mirrorNote}

    <main class="panel-grid">
      <section class="panel-section" data-panel-section="money-printer" data-state="loading" aria-labelledby="money-printer-title">
        <header class="section-head"><h2 id="money-printer-title">Money Printer</h2><span class="section-kicker">24/7 earning work</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">Money Printerの状態を確認しています。</p></div>
        <p class="money-webmcp-status" data-money-webmcp-status aria-live="polite"></p>
      </section>
      ${guest ? "" : `<section class="panel-section" data-panel-section="timeline" data-state="loading" aria-labelledby="timeline-title">
        <header class="section-head"><h2 id="timeline-title">今日の timeline</h2><span class="section-kicker">Today</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">今日の予定を確認しています。</p></div>
      </section>`}

      ${guest ? "" : `<section class="panel-section" data-panel-section="scores" data-state="loading" aria-labelledby="scores-title">
        <header class="section-head"><h2 id="scores-title">4 organ スコア</h2><span class="section-kicker">Outcomes</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">記録を確認しています。</p></div>
      </section>`}

      ${guest ? "" : `<section class="panel-section" data-panel-section="ledger" data-state="loading" aria-labelledby="ledger-title">
        <header class="section-head"><h2 id="ledger-title">FINANCIAL 台帳</h2><span class="section-kicker">Ledger</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">台帳を確認しています。</p></div>
      </section>`}

      ${guest ? "" : `<section class="panel-section" data-panel-section="gates" data-state="loading" aria-labelledby="gates-title">
        <header class="section-head"><h2 id="gates-title">gates 状態</h2><span class="section-kicker">Context</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">つながっている context を確認しています。</p></div>
      </section>`}

      ${guest ? "" : `<section class="panel-section" data-panel-section="settings" data-state="loading" aria-labelledby="settings-title">
        <header class="section-head"><h2 id="settings-title">設定</h2><span class="section-kicker">Read only</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">設定を確認しています。</p></div>
      </section>`}

      ${guest ? "" : `<section class="panel-section" data-panel-section="control-center" data-state="loading" aria-labelledby="control-center-title">
        <header class="section-head"><h2 id="control-center-title">接続と automation</h2><span class="section-kicker">Control</span></header>
        <div class="section-body" data-panel-body aria-live="polite"><p class="loading">あなたの接続と設定を確認しています。</p></div>
      </section>`}
    </main>
  </div>

  <script>
    "use strict";

    const panelEndpoints = Object.freeze({
      "money-printer": "/api/panel/money-printer",
      ${guest ? "" : 'timeline: "/api/panel/timeline",'}
      ${guest ? "" : 'scores: "/api/panel/scores",'}
      ${guest ? "" : 'ledger: "/api/panel/ledger",'}
      ${guest ? "" : 'gates: "/api/panel/gates",'}
      ${guest ? "" : 'settings: "/api/panel/settings",'}
      ${guest ? "" : '"control-center": "/api/panel/control-center",'}
    });
    const displaySecretPatterns = Object.freeze(${JSON.stringify(DISPLAY_SECRET_PATTERNS)}.map(function (pattern) {
      return new RegExp(pattern.source, pattern.flags);
    }));

    function escapeHtml(value) {
      return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
      });
    }

    function displayRecord(value) {
      return Boolean(value && typeof value === "object" && !Array.isArray(value));
    }

    function displayExactKeys(value, expected) {
      if (!displayRecord(value)) return false;
      const actual = Object.keys(value).sort();
      const wanted = expected.slice().sort();
      return actual.length === wanted.length && actual.every(function (key, index) {
        return key === wanted[index];
      });
    }

    function displayContainsSensitiveValue(value) {
      if (typeof value === "string") {
        return displaySecretPatterns.some(function (pattern) { return pattern.test(value); });
      }
      if (Array.isArray(value)) return value.some(displayContainsSensitiveValue);
      if (!displayRecord(value)) return false;
      return Object.entries(value).some(function (entry) {
        return displayContainsSensitiveValue(entry[0]) || displayContainsSensitiveValue(entry[1]);
      });
    }

    function displaySafeText(value, allowEmpty) {
      return typeof value === "string"
        && value.length <= 1000
        && (allowEmpty || value.trim().length > 0)
        && !displayContainsSensitiveValue(value);
    }

    function displayValidTimeZone(value) {
      if (!displaySafeText(value, false)) return false;
      try {
        new Intl.DateTimeFormat("en", { timeZone: value }).format(0);
        return true;
      } catch {
        return false;
      }
    }

    function displaySafeLink(value) {
      if (value === null) return null;
      if (!displaySafeText(value, false)) return null;
      try {
        const url = new URL(value);
        if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.search) return null;
        return url.toString();
      } catch {
        return null;
      }
    }

    function validateTimelineData(data) {
      if (
        !displayExactKeys(data, ["date", "timezone", "items"])
        || !/^\\d{4}-\\d{2}-\\d{2}$/.test(data.date)
        || !displayValidTimeZone(data.timezone)
        || !Array.isArray(data.items)
        || data.items.some(function (item) {
          return !displayExactKeys(item, ["sentence", "status"])
            || !displaySafeText(item.sentence, false)
            || !displaySafeText(item.status, false);
        })
      ) throw new Error("invalid timeline payload");
      return data;
    }

    function validLedgerItem(item) {
      return displayExactKeys(item, ["label", "date", "amount", "link"])
        && displaySafeText(item.label, false)
        && displaySafeText(item.date, false)
        && displaySafeText(item.amount, false)
        && (item.link === null || displaySafeLink(item.link) === item.link);
    }

    const financialReportMoneyFields = Object.freeze([
      "gross_usd_micros", "realized_loss_usd_micros", "financial_fee_usd_micros",
      "api_cost_usd_micros", "operating_net_usd_micros", "balance_usdc_atomic",
      "distributable_usdc_atomic"
    ]);

    function validFinancialReport(report, kind) {
      if (report === null) return true;
      const keys = [
        "period_key", "snapshot_hash", "telegram_message_id",
        "gross_usd_micros", "realized_loss_usd_micros", "financial_fee_usd_micros",
        "api_cost_usd_micros", "operating_net_usd_micros", "balance_usdc_atomic",
        "distributable_usdc_atomic", "self_funded_bps", "stop_reason", "rail_pnl"
      ];
      return displayExactKeys(report, keys)
        && (kind === "daily" ? /^\\d{4}-\\d{2}-\\d{2}$/.test(report.period_key) : /^\\d{4}-W\\d{2}$/.test(report.period_key))
        && /^[0-9a-f]{64}$/.test(report.snapshot_hash)
        && Number.isInteger(report.telegram_message_id)
        && report.telegram_message_id > 0
        && financialReportMoneyFields.every(function (field) {
          return (field === "operating_net_usd_micros" ? /^-?\\d+$/ : /^\\d+$/).test(report[field]);
        })
        && (report.self_funded_bps === null || (Number.isInteger(report.self_funded_bps) && report.self_funded_bps >= 0))
        && ["running", "negative_net", "no_external_income", "reserve_floor"].includes(report.stop_reason)
        && Array.isArray(report.rail_pnl)
        && report.rail_pnl.every(function (row) {
          return displayExactKeys(row, ["rail", "net_usd_micros"])
            && ["SELL", "WORK", "CAPITAL", "UNCLASSIFIED"].includes(row.rail)
            && /^-?\\d+$/.test(row.net_usd_micros);
        });
    }

    function validateLedgerData(data) {
      if (
        !displayExactKeys(data, ["api_cost", "financial", "reports"])
        || !displayExactKeys(data.api_cost, ["no_data", "total", "items"])
        || typeof data.api_cost.no_data !== "boolean"
        || !displaySafeText(data.api_cost.total, false)
        || !Array.isArray(data.api_cost.items)
        || data.api_cost.items.some(function (item) { return !validLedgerItem(item); })
        || !displayExactKeys(data.financial, ["no_data", "items"])
        || typeof data.financial.no_data !== "boolean"
        || !Array.isArray(data.financial.items)
        || data.financial.items.some(function (item) { return !validLedgerItem(item); })
        || !displayExactKeys(data.reports, ["daily", "weekly"])
        || !validFinancialReport(data.reports.daily, "daily")
        || !validFinancialReport(data.reports.weekly, "weekly")
      ) throw new Error("invalid ledger payload");
      return data;
    }

    function validateGatesData(data) {
      const expectedIds = ["location", "payout"];
      if (
        !displayExactKeys(data, ["gates"])
        || !Array.isArray(data.gates)
        || data.gates.length !== expectedIds.length
        || data.gates.some(function (gate, index) {
          return !displayExactKeys(gate, ["id", "unlocked", "unlock_method"])
            || gate.id !== expectedIds[index]
            || typeof gate.unlocked !== "boolean"
            || !displaySafeText(gate.unlock_method, false);
        })
      ) throw new Error("invalid gates payload");
      return data;
    }

    function validCallLanguage(value) {
      return value === null || value === "ja" || value === "en";
    }

    function validateSettingsData(data) {
      if (
        !displayExactKeys(data, ["call_language", "call_schedule", "connections"])
        || !validCallLanguage(data.call_language)
        || !displayExactKeys(data.call_schedule, ["time_zone", "minutes_before", "wake_policy"])
        || !displayValidTimeZone(data.call_schedule.time_zone)
        || !Array.isArray(data.call_schedule.minutes_before)
        || data.call_schedule.minutes_before.length !== 2
        || data.call_schedule.minutes_before[0] !== 10
        || data.call_schedule.minutes_before[1] !== 5
        || !["travel-only", "all-events"].includes(data.call_schedule.wake_policy)
        || !displayExactKeys(data.connections, ["calendar", "gmail", "telegram"])
        || Object.values(data.connections).some(function (value) { return typeof value !== "boolean"; })
      ) throw new Error("invalid settings payload");
      return data;
    }

    const controlConnectionNames = Object.freeze(["calendar", "telegram", "location", "call", "email", "wallet"]);
    const controlConnectionStates = Object.freeze(["connected", "action_required", "error", "unavailable"]);

    function validControlConnection(item) {
      if (!displayRecord(item)) return false;
      const allowed = ["state", "reason", "actions", "actionLabel"];
      if (Object.keys(item).some(function (key) { return !allowed.includes(key); })) return false;
      return controlConnectionStates.includes(item.state)
        && displaySafeText(item.reason, false)
        && (item.actions === undefined || (Array.isArray(item.actions) && item.actions.every(function (action) { return displaySafeText(action, false); })))
        && (item.actionLabel === undefined || displaySafeText(item.actionLabel, false));
    }

    function validateControlCenterData(data) {
      const settingsKeys = ["call_enabled", "notifications_enabled", "daily_automation_enabled", "call_time_zone", "call_language", "wake_policy"];
      if (
        !displayExactKeys(data, ["identity", "context", "connections", "settings", "controls", "csrf"])
        || !displayExactKeys(data.identity, ["name", "uidRef"])
        || data.identity.name !== "Life Manager user"
        || !/^user:[0-9a-f]{12}$/.test(data.identity.uidRef)
        || !displayExactKeys(data.context, ["timeZone", "locationAvailable"])
        || !displayValidTimeZone(data.context.timeZone)
        || typeof data.context.locationAvailable !== "boolean"
        || !displayExactKeys(data.connections, controlConnectionNames)
        || controlConnectionNames.some(function (name) { return !validControlConnection(data.connections[name]); })
        || !displayExactKeys(data.settings, settingsKeys)
        || typeof data.settings.call_enabled !== "boolean"
        || typeof data.settings.notifications_enabled !== "boolean"
        || typeof data.settings.daily_automation_enabled !== "boolean"
        || !displayValidTimeZone(data.settings.call_time_zone)
        || !validCallLanguage(data.settings.call_language)
        || !["travel-only", "all-events"].includes(data.settings.wake_policy)
        || !displayExactKeys(data.controls, ["delegation", "physical_automation", "mental_automation", "financial_automation"])
        || !displayExactKeys(data.controls.delegation, ["state", "reason"])
        || data.controls.delegation.state !== "unavailable"
        || !displaySafeText(data.controls.delegation.reason, false)
        || ["physical_automation", "mental_automation", "financial_automation"].some(function (name) {
          return !displayExactKeys(data.controls[name], ["state"]) || data.controls[name].state !== "unavailable";
        })
        || !displaySafeText(data.csrf, false)
      ) throw new Error("invalid control-center payload");
      return data;
    }

    function bodyFor(name) {
      return document.querySelector('[data-panel-section="' + name + '"] [data-panel-body]');
    }

    function markLoaded(name, html) {
      const section = document.querySelector('[data-panel-section="' + name + '"]');
      if (!section) return;
      bodyFor(name).innerHTML = html;
      section.dataset.state = "loaded";
    }

    function markError(name) {
      const section = document.querySelector('[data-panel-section="' + name + '"]');
      if (!section) return;
      bodyFor(name).innerHTML = '<p class="error">いま情報を読み込めませんでした。少し時間をおいて、もう一度開いてください。</p>';
      section.dataset.state = "error";
    }

    function renderTimeline(data) {
      validateTimelineData(data);
      const summary = '<p class="timeline-summary"><span>' + escapeHtml(data.date) + ' · ' + escapeHtml(data.timezone) + '</span><span>予定と電話 ' + data.items.length + '件</span></p>';
      if (!data.items.length) return summary + '<p class="empty">今日は表示する予定や電話がありません。</p>';
      const rows = data.items.map(function (item) {
        return '<li class="timeline-item"><div><p class="timeline-title">' + escapeHtml(item.sentence) + '</p></div><span class="call-mark">' + escapeHtml(item.status) + '</span></li>';
      }).join("");
      return summary + '<ol class="timeline-list">' + rows + '</ol>';
    }

    const SCORE_LABELS = Object.freeze({ daily: "DAILY", physical: "PHYSICAL", mental: "MENTAL", financial: "FINANCIAL" });
    const SCORE_PERIOD_KINDS = Object.freeze(${JSON.stringify(SCORE_PERIOD_KINDS)});
    const SCORE_COMPONENT_KEYS = Object.freeze(${JSON.stringify(SCORE_COMPONENT_KEYS)});
    const SCORE_COMPONENT_LABELS = Object.freeze(${JSON.stringify(SCORE_COMPONENT_LABELS)});
    ${scoreEscapeHtml.toString()}
    ${scoreAsDate.toString()}
    ${scoreFormatDate.toString()}
    ${scoreExactKeys.toString()}
    ${scoreNonNegativeInteger.toString()}
    ${roundedScoreValue.toString()}
    ${validScoreComponents.toString()}
    ${scoreComponentRatio.toString()}
    ${validScoreOrgan.toString()}
    ${renderScoreComponents.toString()}
    ${renderScoreCards.toString()}
    const renderScores = renderScoreCards;

    function renderLedger(data) {
      validateLedgerData(data);
      function money(value, signed) {
        const amount = BigInt(value);
        const negative = amount < 0n;
        const absolute = negative ? -amount : amount;
        const whole = absolute / 1000000n;
        const fraction = String(absolute % 1000000n).padStart(6, "0");
        const trimmed = fraction.replace(/0+$/, "");
        const decimals = trimmed.length < 2 ? fraction.slice(0, 2) : trimmed;
        const body = "$" + whole + "." + decimals;
        return negative ? "-" + body : (signed ? "+" + body : body);
      }
      const stopLabels = { running: "稼働中", negative_net: "赤字", no_external_income: "外部収入なし", reserve_floor: "reserve floor" };
      const reportCards = ["daily", "weekly"].map(function (kind) {
        const report = data.reports[kind];
        if (!report) return "";
        if (kind === "daily") {
          const cost = BigInt(report.realized_loss_usd_micros) + BigInt(report.financial_fee_usd_micros) + BigInt(report.api_cost_usd_micros);
          return '<article class="ledger-item"><div><p>DAILY ' + escapeHtml(report.period_key) + '</p><p class="ledger-item-meta">Gross ' + escapeHtml(money(report.gross_usd_micros, true)) + ' · Cost ' + escapeHtml(money(String(cost), false)) + ' · ' + escapeHtml(stopLabels[report.stop_reason]) + '</p></div><p class="ledger-amount">' + escapeHtml(money(report.operating_net_usd_micros, true)) + '</p></article>';
        }
        const rails = report.rail_pnl.map(function (row) { return row.rail + " " + money(row.net_usd_micros, true); }).join(" · ");
        const ratio = report.self_funded_bps === null ? "未計測" : (report.self_funded_bps / 100).toFixed(2) + "%";
        return '<article class="ledger-item"><div><p>WEEKLY ' + escapeHtml(report.period_key) + '</p><p class="ledger-item-meta">' + escapeHtml(rails || "rail収支なし") + ' · Self-funded ' + escapeHtml(ratio) + '</p></div><p class="ledger-amount">分配可能 ' + escapeHtml(money(report.distributable_usdc_atomic, false)) + '</p></article>';
      }).join("");
      const entries = data.financial.items.concat(data.api_cost.items);
      if (!entries.length) {
        const cost = data.api_cost.no_data ? "運用実費の記録もまだありません" : "運用実費（累計） " + data.api_cost.total;
        return reportCards + '<div class="ledger-empty"><h3>まだ収支の記録はありません</h3><p class="ledger-cost">' + escapeHtml(cost) + '</p></div>';
      }
      const rows = entries.map(function (entry) {
        const link = displaySafeLink(entry.link);
        return '<li class="ledger-item"><div><p>' + escapeHtml(entry.label) + '</p><p class="ledger-item-meta">' + escapeHtml(entry.date) + (link ? ' · <a class="ledger-link" href="' + escapeHtml(link) + '" target="_blank" rel="noopener noreferrer">外部記録で確認</a>' : "") + '</p></div><p class="ledger-amount">' + escapeHtml(entry.amount) + '</p></li>';
      }).join("");
      return reportCards + '<p class="ledger-cost">運用実費（累計） ' + escapeHtml(data.api_cost.total) + '</p><ul class="ledger-list">' + rows + '</ul>';
    }

    const gateLabels = Object.freeze({ location: "位置情報", payout: "送金先" });

    function renderGates(data) {
      validateGatesData(data);
      return '<ul class="gate-list">' + data.gates.map(function (gate) {
        const status = gate.unlocked ? "解錠済み" : "まだ未解錠";
        const copy = gate.unlocked ? "必要な context がつながっています。" : (gate.unlock_method || "解錠方法を準備しています。");
        return '<li class="gate-item"><div class="gate-title-row"><h3 class="gate-title">' + escapeHtml(gateLabels[gate.id] || gate.id || "gate") + '</h3><span class="gate-status ' + (gate.unlocked ? "is-open" : "") + '">' + status + '</span></div><p class="gate-copy">' + escapeHtml(copy) + '</p></li>';
      }).join("") + '</ul>';
    }

    function languageLabel(value) {
      if (value === "ja") return "日本語";
      if (value === "en") return "English";
      return "未設定";
    }

    function renderSettings(data) {
      validateSettingsData(data);
      const schedule = data.call_schedule || {};
      const minutes = Array.isArray(schedule.minutes_before) ? schedule.minutes_before : [];
      const scheduleText = minutes.length ? "予定の" + minutes.map(function (minute) { return minute + "分前"; }).join("と") : "call 時間帯は未設定";
      const connections = data.connections || {};
      const connectionLabels = { calendar: "Calendar", gmail: "Gmail", telegram: "Telegram" };
      const chips = ["calendar", "gmail", "telegram"].map(function (name) {
        const on = Boolean(connections[name]);
        return '<span class="connection ' + (on ? "is-on" : "") + '">' + connectionLabels[name] + ' ' + (on ? "接続済み" : "未接続") + '</span>';
      }).join("");
      return '<div class="settings-grid"><div class="setting-group"><p class="setting-label">CALL LANGUAGE</p><p class="setting-value">' + languageLabel(data.call_language) + '</p></div><div class="setting-group"><p class="setting-label">CALL SCHEDULE</p><p class="setting-value">' + escapeHtml(scheduleText) + '<br><span style="color:var(--ink-soft)">' + escapeHtml(schedule.time_zone || "timezone 未設定") + '</span></p></div><div class="setting-group"><p class="setting-label">接続状態</p><div class="connection-list">' + chips + '</div></div></div>';
    }

    let controlCsrf = "";
    const connectionLabels = Object.freeze({ calendar: "Calendar", telegram: "Telegram", location: "Location", call: "Call", email: "Email", wallet: "Payout / wallet" });

    function actionButton(action, item) {
      if (action === "connection.start:calendar") { const label = item && item.actionLabel === "Reconnect calendar" ? "Reconnect calendar" : "Connect Calendar"; return '<button class="control-action" type="button" aria-label="' + label + '" data-action="connect-calendar">' + label + '</button>'; }
      if (action === "connection.disconnect:calendar") return '<button class="control-action" type="button" data-command="connection.disconnect" data-action="disconnect-calendar">Disconnect calendar</button>';
      if (action === "instructions:location") return '<button class="control-action" type="button" data-action="instructions-location">Telegram instructions</button>';
      if (action === "instructions:wallet") return '<button class="control-action" type="button" data-action="instructions-wallet">Telegram instructions</button>';
      if (action === "instructions:call") return '<button class="control-action" type="button" data-action="instructions-call">Telegram instructions</button>';
      return "";
    }

    function switchButton(action, label, enabled) {
      return '<button class="setting-switch" type="button" role="switch" aria-checked="' + String(Boolean(enabled)) + '" data-action="' + action + '">' + escapeHtml(label) + ': ' + (enabled ? "ON" : "OFF") + '</button>';
    }

    function settingSelect(attributes, label, current, options) {
      const hasCurrent = options.some(function (option) { return option.value === current; });
      const placeholder = hasCurrent ? "" : '<option value="" selected disabled>Not configured</option>';
      const choices = options.map(function (option) {
        return '<option value="' + escapeHtml(option.value) + '"' + (option.value === current ? " selected" : "") + '>' + escapeHtml(option.label) + '</option>';
      }).join("");
      return '<label class="setting-select"><span>' + escapeHtml(label) + '</span><select ' + attributes + ' aria-label="' + escapeHtml(label) + '">' + placeholder + choices + '</select></label>';
    }

    function renderControlCenter(data) {
      validateControlCenterData(data);
      controlCsrf = data.csrf || "";
      const connections = data.connections || {};
      const cards = ["calendar", "telegram", "location", "call", "email", "wallet"].map(function (name) {
        const item = connections[name] || { state: "error", reason: "State unavailable", actions: [] };
        const actions = (Array.isArray(item.actions) ? item.actions : []).map(function (action) { return actionButton(action, item); }).join("");
        return '<article class="control-card"><p class="control-state">' + escapeHtml(item.state) + '</p><h3>' + escapeHtml(connectionLabels[name]) + '</h3><p class="control-reason">' + escapeHtml(item.reason) + '</p>' + actions + '</article>';
      }).join("");
      const settings = data.settings || {};
      const switches = [
        switchButton("toggle-calls", "Calls", settings.call_enabled),
        switchButton("toggle-notifications", "Notifications", settings.notifications_enabled),
        switchButton("toggle-daily", "DAILY automation", settings.daily_automation_enabled),
        '<p class="control-unavailable" role="status">Delegation unavailable: no safe delegated-action runtime is available.</p>',
        settingSelect('data-setting="call_language" data-action="call_language"', "Call language", settings.call_language, [{ value: "en", label: "English" }, { value: "ja", label: "日本語" }]),
        settingSelect('data-setting="call_time_zone" data-action="call_time_zone"', "Call timezone", settings.call_time_zone, [{ value: "Asia/Tokyo", label: "Asia/Tokyo" }, { value: "UTC", label: "UTC" }, { value: "Europe/London", label: "Europe/London" }, { value: "America/New_York", label: "America/New_York" }, { value: "America/Los_Angeles", label: "America/Los_Angeles" }]),
        settingSelect('data-setting="wake_policy" data-action="wake_policy"', "Wake policy", settings.wake_policy, [{ value: "travel-only", label: "Travel events only" }, { value: "all-events", label: "All events" }]),
      ].join("");
      return '<p><strong>' + escapeHtml((data.identity || {}).name || "Life Manager user") + '</strong></p><div class="control-grid" id="connection-cards">' + cards + '</div><div class="settings-controls" id="settings-controls">' + switches + '</div><p class="action-status" id="action-status" aria-live="polite"></p>';
    }

    const moneyLaneLabels = Object.freeze({ found: "Found", working: "Working", needs_you: "Needs You", waiting: "Waiting", done: "Done", paid: "Paid" });

    function validMoneyMap(value) {
      return displayRecord(value)
        && Object.keys(value).every(function (key) { return /^[A-Z]{3}$/.test(key) && typeof value[key] === "string" && /^\\d+$/.test(value[key]); });
    }

    function formatMoneyMap(value) {
      const keys = Object.keys(value).sort();
      return keys.length ? keys.map(function (key) { return key + " " + value[key]; }).join(" + ") : "0";
    }

    function moneyOpportunityParts(value) {
      const match = /^opportunity:\\/\\/([a-z0-9][a-z0-9._-]{0,199})\\/([0-9a-f]{64})$/.exec(String(value == null ? "" : value));
      return match ? { tenantId: match[1], opportunityId: match[2] } : null;
    }

    function moneyRuntimeJobRef(tenantId, opportunityId) {
      return "runtime-job://" + encodeURIComponent(tenantId) + "/goal%3A" + opportunityId;
    }

    function validMoneyCard(card) {
      return displayExactKeys(card, ["opportunity_ref", "title", "status", "value_minor", "currency", "source_url"])
        && displaySafeText(card.opportunity_ref, false)
        && Boolean(moneyOpportunityParts(card.opportunity_ref))
        && displaySafeText(card.title, false)
        && displaySafeText(card.status, false)
        && /^\\d+$/.test(card.value_minor)
        && (card.currency === null || /^[A-Z]{3}$/.test(card.currency))
        && (card.source_url === null || (function () { try { const url = new URL(card.source_url); return url.protocol === "https:" && !url.username && !url.password; } catch { return false; } })());
    }

    function validMoneyWorkroomActivity(item, selectedRef, expectedJobRef, tenantId) {
      const base = displayExactKeys(item, ["kind", "ref", "status", "observed_at"]);
      if (item.kind === "opportunity") return base && item.ref === selectedRef;
      if (item.kind === "work") return base && item.ref === expectedJobRef;
      const prefix = "human-task://" + encodeURIComponent(tenantId) + "/";
      return item.kind === "human_task" && displayExactKeys(item, ["kind", "ref", "status", "observed_at", "job_ref"])
        && item.job_ref === expectedJobRef && typeof item.ref === "string" && item.ref.startsWith(prefix)
        && /^[0-9a-f]{64}$/.test(item.ref.slice(prefix.length));
    }

    function validMoneyWorkroomTime(value) { if (typeof value !== "string") return false; const milliseconds = Date.parse(value); return Number.isFinite(milliseconds) && new Date(milliseconds).toISOString() === value; }

    function validMoneyWorkroomUrl(value) {
      if (!displaySafeText(value, false)) return false;
      try {
        const url = new URL(value);
        return url.protocol === "https:" && Boolean(url.hostname) && !url.username && !url.password;
      } catch {
        return false;
      }
    }

    function validateMoneyWorkroom(data, selectedOpportunityRef) {
      const selected = moneyOpportunityParts(selectedOpportunityRef);
      const expectedKeys = ["opportunity_id", "title", "value_minor", "currency", "source_url", "status", "job_ref", "activity"];
      const expectedJobRef = selected && moneyRuntimeJobRef(selected.tenantId, selected.opportunityId);
      if (!selected
        || !displayExactKeys(data, expectedKeys)
        || displayContainsSensitiveValue(data)
        || data.opportunity_id !== selected.opportunityId
        || !displaySafeText(data.title, false)
        || !/^\\d+$/.test(data.value_minor)
        || (data.currency !== null && !/^[A-Z]{3}$/.test(data.currency))
        || (data.source_url !== null && !validMoneyWorkroomUrl(data.source_url))
        || !displaySafeText(data.status, false)
        || data.job_ref !== expectedJobRef
        || !Array.isArray(data.activity) || data.activity.length === 0
        || data.activity.some(function (item) {
          return !displaySafeText(item.kind, false)
            || !displaySafeText(item.status, false)
            || !validMoneyWorkroomActivity(item, selectedOpportunityRef, expectedJobRef, selected.tenantId)
            || !validMoneyWorkroomTime(item.observed_at)
        })) {
        throw new Error("invalid workroom payload");
      }
      return data;
    }

    function renderMoneyWorkroom(data) {
      const activity = data.activity.map(function (item) {
        return '<li class="money-activity-item"><strong>' + escapeHtml(item.kind) + '</strong><span>' + escapeHtml(item.status) + '</span><code>' + escapeHtml(item.ref) + '</code></li>';
      }).join("");
      return '<div class="money-workroom" data-money-workroom-content><p class="section-kicker">Selected workroom</p><h3>' + escapeHtml(data.title) + '</h3><p class="money-workroom-status">' + escapeHtml(data.status) + '</p><ul class="money-activity">' + activity + '</ul></div>';
    }

    function validMoneyHumanTask(task) {
      return displayExactKeys(task, ["task_id", "version", "question", "required_format", "reason_code", "browser_takeover_available"])
        && /^[0-9a-f]{64}$/.test(task.task_id) && Number.isInteger(task.version) && task.version > 0
        && displaySafeText(task.question, false) && displaySafeText(task.reason_code, false)
        && typeof task.browser_takeover_available === "boolean";
    }

    function renderMoneyHumanTask(task) {
      if (task === null) return '<p class="empty">No human action required.</p>';
      if (!validMoneyHumanTask(task)) throw new Error("invalid human task payload");
      const browser = task.browser_takeover_available
        ? '<iframe class="money-browser-takeover" title="Secure provider browser" src="/api/panel/money-printer/browser" sandbox="allow-scripts allow-same-origin" allow="clipboard-read; clipboard-write"></iframe>' : '';
      const actions = task.reason_code === "provider_interview"
        ? '<div class="money-human-actions"><button type="button" data-money-human-answer="approve" data-task-id="' + task.task_id + '" data-task-version="' + task.version + '">I completed it</button><button type="button" data-money-human-answer="request_changes" data-task-id="' + task.task_id + '" data-task-version="' + task.version + '">Cannot complete</button></div>' : '';
      return '<aside class="money-human-task"><p class="section-kicker">Needs You</p><h3>' + escapeHtml(task.question) + '</h3>' + browser + actions + '</aside>';
    }

    async function loadMoneyHumanTask() {
      const target = document.querySelector("[data-money-human-task]");
      if (!target) return;
      const response = await fetch("/api/panel/money-printer/human-task/next", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("human task unavailable");
      const value = await response.json();
      if (!displayExactKeys(value, ["task"])) throw new Error("human task unavailable");
      target.innerHTML = renderMoneyHumanTask(value.task);
    }

    async function answerMoneyHumanTask(button) {
      const response = await fetch("/api/panel/money-printer/human-task/answer", {
        method: "POST", credentials: "same-origin",
        headers: { "content-type": "application/json", "x-lm-csrf": controlCsrf || "${String(options.csrf || "")}", "idempotency-key": (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-human") },
        body: JSON.stringify({ task_id: button.dataset.taskId, version: Number(button.dataset.taskVersion), answer: button.dataset.moneyHumanAnswer }),
      });
      if (!response.ok) throw new Error("human answer unavailable");
      await loadPanelSection("money-printer");
    }

    let moneyWorkroomRequestSequence = 0;
    async function loadMoneyWorkroom(button) {
      const requestSequence = ++moneyWorkroomRequestSequence;
      const section = button && typeof button.closest === "function" ? button.closest('[data-panel-section="money-printer"]') : null;
      const workroom = section && section.querySelector("[data-money-workroom]");
      const selectedRef = button && button.dataset ? button.dataset.moneyOpportunityRef : "";
      const selected = moneyOpportunityParts(selectedRef);
      if (!workroom || !selected || !button.dataset.moneyOpportunityId || button.dataset.moneyOpportunityId !== selected.opportunityId) {
        if (workroom && requestSequence === moneyWorkroomRequestSequence) workroom.innerHTML = '<p class="error">Workroom unavailable</p>';
        return;
      }
      workroom.innerHTML = '<p class="loading">Loading workroom…</p>';
      try {
        const response = await fetch("/api/panel/money-printer/workroom?opportunity_id=" + encodeURIComponent(selected.opportunityId), {
          method: "GET", credentials: "same-origin", headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("workroom unavailable");
        const data = await response.json();
        validateMoneyWorkroom(data, selectedRef);
        if (requestSequence === moneyWorkroomRequestSequence) workroom.innerHTML = renderMoneyWorkroom(data);
      } catch {
        if (requestSequence === moneyWorkroomRequestSequence) workroom.innerHTML = '<p class="error">Workroom unavailable</p>';
      }
    }

    function validateMoneyPrinterData(data) {
      const lanes = Object.keys(moneyLaneLabels);
      if (!displayExactKeys(data, ["observed_at", "metrics", "columns", "activity"])
        || !displayExactKeys(data.metrics, ["agents_working", "needs_you", "opportunity_value", "paid_verified"])
        || !displayExactKeys(data.columns, lanes)
        || !Number.isInteger(data.metrics.agents_working) || data.metrics.agents_working < 0
        || !Number.isInteger(data.metrics.needs_you) || data.metrics.needs_you < 0
        || !validMoneyMap(data.metrics.opportunity_value) || !validMoneyMap(data.metrics.paid_verified)
        || !Array.isArray(data.activity)
        || lanes.some(function (lane) { return !Array.isArray(data.columns[lane]) || data.columns[lane].some(function (card) { return !validMoneyCard(card); }); })) {
        throw new Error("invalid money printer payload");
      }
      return data;
    }

    function renderMoneyPrinter(data) {
      validateMoneyPrinterData(data);
      const intake = '<form class="money-intake" data-money-opportunity-form><label>Opportunity URL<input name="source_url" type="url" required placeholder="https://…"></label><label>Title<input name="title" required maxlength="300"></label><label>What should the agent do?<input name="goal_statement" required maxlength="2000"></label><label>Value in minor units<input name="value_minor" inputmode="numeric" pattern="[0-9]+" required></label><label>Currency<input name="currency" value="USD" pattern="[A-Z]{3}" maxlength="3" required></label><button class="control-action" type="submit">Add opportunity</button><p data-money-opportunity-status aria-live="polite"></p></form>';
      const metrics = [["Paid & verified", formatMoneyMap(data.metrics.paid_verified)], ["Agents working", data.metrics.agents_working], ["Needs You", data.metrics.needs_you], ["Opportunity value", formatMoneyMap(data.metrics.opportunity_value)]]
        .map(function (item) { return '<article class="money-metric"><span>' + escapeHtml(item[0]) + '</span><strong>' + escapeHtml(item[1]) + '</strong></article>'; }).join("");
      const lanes = Object.keys(moneyLaneLabels).map(function (lane) {
        const cards = data.columns[lane].map(function (card) {
          const opportunity = moneyOpportunityParts(card.opportunity_ref);
          return '<button class="money-card" type="button" data-money-opportunity-ref="' + escapeHtml(card.opportunity_ref) + '" data-money-opportunity-id="' + escapeHtml(opportunity.opportunityId) + '"><strong>' + escapeHtml(card.title) + '</strong><p>' + escapeHtml(card.currency || "") + ' ' + escapeHtml(card.value_minor) + '</p></button>';
        }).join("");
        return '<section class="money-lane"><h3>' + escapeHtml(moneyLaneLabels[lane]) + '</h3>' + (cards || '<p class="empty">No work</p>') + '</section>';
      }).join("");
      return intake + '<div class="money-metrics">' + metrics + '</div><div class="money-board">' + lanes + '</div><div data-money-human-task><p class="loading">Checking human tasks…</p></div><div class="money-workroom" data-money-workroom><p class="empty">Select an opportunity to inspect its workroom.</p></div>';
    }

    const renderers = Object.freeze({ "money-printer": renderMoneyPrinter, timeline: renderTimeline, scores: renderScores, ledger: renderLedger, gates: renderGates, settings: renderSettings, "control-center": renderControlCenter });

    async function loadPanelSection(name) {
      const response = await fetch(panelEndpoints[name], { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.status === 401) {
        window.location.reload();
        throw new Error("session expired");
      }
      if (!response.ok) throw new Error(name + " unavailable");
      const data = await response.json();
      if (displayContainsSensitiveValue(data)) throw new Error(name + " unavailable");
      markLoaded(name, renderers[name](data));
      if (name === "money-printer") await loadMoneyHumanTask();
    }

    let moneyPrinterRefresh = Promise.resolve();
    document.addEventListener("money-printer:refresh", function (event) {
      moneyPrinterRefresh = moneyPrinterRefresh.catch(function () {}).then(async function () {
        await loadPanelSection("money-printer");
      }).catch(function (error) {
        console.error("[panel] money-printer", error.message);
        markError("money-printer");
        throw error;
      });
      if (event && event.detail && typeof event.detail === "object") event.detail.promise = moneyPrinterRefresh;
    });

    const moneyPrinterWebMcpTools = Object.freeze(["inspect_money_printer", "inspect_next_human_task", "record_human_answer", "add_opportunity", "inspect_workroom"]);
    const moneyPrinterWebMcpStatuses = Object.freeze(["running", "succeeded", "failed"]);
    document.addEventListener("money-printer:webmcp-call", function (event) {
      const detail = event && event.detail;
      if (!displayExactKeys(detail, ["tool", "status"])
        || !moneyPrinterWebMcpTools.includes(detail.tool)
        || !moneyPrinterWebMcpStatuses.includes(detail.status)) return;
      const status = document.querySelector("[data-money-webmcp-status]");
      if (status) status.textContent = detail.tool + " · " + detail.status;
    });

    async function submitMoneyOpportunity(form) {
      const field = function (name) { const input = form.elements.namedItem(name); return String(input && input.value || "").trim(); };
      const body = { source_url: field("source_url"), title: field("title"), goal_statement: field("goal_statement"), value_minor: field("value_minor"), currency: field("currency") };
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-money-opportunity-status]");
      button.disabled = true;
      status.textContent = "Adding…";
      try {
        const response = await fetch("/api/panel/money-printer/opportunity", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-lm-csrf": controlCsrf || "${String(options.csrf || "")}", "idempotency-key": (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-opportunity") }, body: JSON.stringify(body) });
        const result = await response.json().catch(function () { return {}; });
        if (!response.ok || !/^[0-9a-f]{64}$/.test(String(result.opportunity_id || "")) || typeof result.job_ref !== "string" || typeof result.status !== "string") throw new Error("add failed");
        form.reset();
        await loadPanelSection("money-printer");
        status.textContent = "Opportunity added";
      } catch {
        status.textContent = "Could not add opportunity";
      } finally {
        button.disabled = false;
      }
    }

    function commandForAction(action, button) {
      switch (action) {
        case "connect-calendar": return { type: "connection.start", provider: "calendar" };
        case "disconnect-calendar": return { type: "connection.disconnect", provider: "calendar" };
        case "toggle-calls": return { type: "setting.set", setting: "call_enabled", value: button.getAttribute("aria-checked") !== "true" };
        case "toggle-notifications": return { type: "setting.set", setting: "notifications_enabled", value: button.getAttribute("aria-checked") !== "true" };
        case "toggle-daily": return { type: "setting.set", setting: "daily_automation_enabled", value: button.getAttribute("aria-checked") !== "true" };
        case "toggle-delegation": return { type: "setting.set", setting: "delegation_enabled", value: button.getAttribute("aria-checked") !== "true" };
        case "call_language": return { type: "setting.set", setting: "call_language", value: button.value };
        case "call_time_zone": return { type: "setting.set", setting: "call_time_zone", value: button.value };
        case "wake_policy": return { type: "setting.set", setting: "wake_policy", value: button.value };
        case "instructions-location": window.location.href = "https://t.me/LifeManagerBotbot?start=location"; return null;
        case "instructions-wallet": window.location.href = "https://t.me/LifeManagerBotbot?start=payout"; return null;
        case "instructions-call": window.location.href = "https://t.me/LifeManagerBotbot?start=call"; return null;
        default: return null;
      }
    }

    async function runControlAction(button) {
      const command = commandForAction(button.dataset.action, button);
      if (!command) return;
      const section = document.querySelector('[data-panel-section="control-center"]');
      const before = section.innerHTML;
      const status = document.getElementById("action-status");
      button.disabled = true; button.setAttribute("aria-busy", "true");
      if (status) status.textContent = "Updating…";
      try {
        const response = await fetch("/api/panel/commands", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-lm-csrf": controlCsrf, "idempotency-key": (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-panel") }, body: JSON.stringify(command) });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error("update failed");
        if (result.state && result.state.redirectUrl) { window.location.href = result.state.redirectUrl; return; }
        await loadPanelSection("control-center");
        const nextStatus = document.getElementById("action-status"); if (nextStatus) nextStatus.textContent = "Updated";
      } catch {
        section.innerHTML = before;
        const restored = document.getElementById("action-status"); if (restored) restored.textContent = "Update failed. Your previous setting is unchanged.";
      }
    }

    document.addEventListener("click", function (event) {
      const humanAnswer = event.target.closest("button[data-money-human-answer]");
      if (humanAnswer) { answerMoneyHumanTask(humanAnswer).catch(function () {}); return; }
      const moneyCard = event.target.closest("button[data-money-opportunity-id]");
      if (moneyCard) { loadMoneyWorkroom(moneyCard); return; }
      const button = event.target.closest("button[data-action]");
      if (button) runControlAction(button);
    });
    document.addEventListener("submit", function (event) {
      const form = event.target.closest("form[data-money-opportunity-form]");
      if (!form) return;
      event.preventDefault();
      submitMoneyOpportunity(form);
    });
${logoutScript}
    document.addEventListener("change", function (event) {
      const select = event.target.closest('select[data-action]');
      if (select) runControlAction(select);
    });

    Promise.allSettled(Object.keys(panelEndpoints).map(function (name) {
      return loadPanelSection(name).catch(function (error) {
        console.error("[panel] " + name, error.message);
        markError(name);
      });
    }));
  </script>
  <script>${renderMoneyPrinterWebMcpScript({ csrf: options.csrf })}</script>
</body>
</html>`;
}

module.exports = { renderPanelPage, renderPanelOnboardingPage, renderScoreCards };
