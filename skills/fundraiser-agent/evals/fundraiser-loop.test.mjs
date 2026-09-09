import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const dailyPrompt = await readFile(new URL("../prompts/daily.md", import.meta.url), "utf8");
const skill = await readFile(new URL("../SKILL.md", import.meta.url), "utf8");
const runtimeScript = await readFile(new URL("../runtime/run.sh", import.meta.url), "utf8");
const runLock = new URL("../runtime/run-lock.sh", import.meta.url);
const fundraiserPlist = await readFile(new URL("../runtime/ai.anicca.fundraiser.plist", import.meta.url), "utf8");
const loopRegistry = JSON.parse(await readFile(new URL("../../../config/loop-registry.json", import.meta.url), "utf8"));
const productionContext = JSON.parse(await readFile(new URL("../../../.agents/startup-context.json", import.meta.url), "utf8"));
const productionOpportunities = JSON.parse(await readFile(new URL("../../../.agents/fundraising-opportunities.json", import.meta.url), "utf8"));
const runnerConfig = JSON.parse(await readFile(new URL("../../../runtime/agent-runner/config.json", import.meta.url), "utf8"));
const emailValidator = new URL("../runtime/validate-outbound-email.py", import.meta.url);
const applicationRecorder = new URL("../runtime/record-application.py", import.meta.url);
const startupContext = Object.freeze({
  product: Object.freeze({
    name: "Life Manager",
    one_liner: "A personal manager that acts within delegated boundaries and reports evidence.",
    mission: "End suffering for humans and all living beings.",
  }),
  traction: Object.freeze({
    founder_attested_revenue: Object.freeze({ display: "approximately $1,000", source: "founder_attested" }),
  }),
  links: Object.freeze({
    product: Object.freeze({ url: "https://aniccaai.com/lm", status: "verified" }),
    repository: Object.freeze({ url: "https://github.com/Daisuke134/life-manager", status: "verified" }),
  }),
  claims: Object.freeze([
    Object.freeze({ id: "public-repository", statement: "Life Manager is developed in a public repository." }),
  ]),
});

function factIndex(context) {
  return new Map([
    ["product.name", context.product.name],
    ["product.one_liner", context.product.one_liner],
    ["product.mission", context.product.mission],
    ["traction.founder_attested_revenue.display", context.traction.founder_attested_revenue.display],
    ["links.product.url", context.links.product.url],
    ["links.repository.url", context.links.repository.url],
    ...context.claims.map((claim) => [`claim:${claim.id}`, claim.statement]),
  ]);
}

function applicationIdentity(candidate, account) {
  return [candidate.organization, candidate.program, candidate.cohort_window, account]
    .map((part) => String(part).trim().toLocaleLowerCase())
    .join("\u001f");
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeBrowser(form, submitResult = { kind: "confirmed", receiptRef: "ui-confirmation-1" }) {
  const state = { fields: new Map(), actions: [], submitCount: 0, submitResult };
  const browser = {
    state,
    async observe() {
      return clone({
        url: form.applicationUrl,
        fields: form.fields.map((field) => ({ ...field, value: state.fields.get(field.id) ?? null })),
        submit: { visible: true, enabled: true },
      });
    },
    async fill(field, value) {
      const current = form.fields.find((item) => item.id === field.id);
      if (!current) throw new Error("stale_field");
      state.fields.set(current.id, value);
      state.actions.push({ kind: "fill", fieldId: current.id, label: current.label, value });
      return this.observe();
    },
    async submit() {
      state.submitCount += 1;
      state.actions.push({ kind: "submit" });
      return clone(state.submitResult);
    },
    async readback() {
      return clone(form.readback || null);
    },
  };
  return browser;
}

function makeReceipts(rows = []) {
  const receipts = rows.map((row) => ({ ...row }));
  return {
    rows: receipts,
    has(identity) {
      return receipts.find((row) => row.identity === identity) || null;
    },
    claim(identity) {
      if (this.has(identity)) return false;
      receipts.push({ identity, status: "submit_claimed" });
      return true;
    },
    finalize(identity, status, readback) {
      const row = this.has(identity);
      if (!row) throw new Error("receipt_claim_missing");
      row.status = status;
      row.readback = readback;
    },
  };
}

/**
 * A provider-neutral behavioral harness. The model chooses candidates and field actions;
 * this harness supplies only generic freshness, context, receipt, and one-shot boundaries.
 */
async function runDailyPass({ discover, receipts, browserFor, context = startupContext, account, decide }) {
  const candidates = await discover();
  const candidate = await decide({ stage: "qualify", candidates: clone(candidates), receipts: clone(receipts.rows) });
  const candidateIndex = candidates.findIndex((item) => (
    item?.applicationUrl === candidate?.applicationUrl
    && item?.organization === candidate?.organization
    && item?.program === candidate?.program
    && item?.cohort_window === candidate?.cohort_window
  ));
  if (candidateIndex < 0) return { status: "not_submitted", reason: "candidate_not_from_live_discovery" };
  const selectedCandidate = candidates[candidateIndex];
  if (selectedCandidate.official?.verified !== true || selectedCandidate.official?.eligible !== true) {
    return { status: "not_submitted", reason: "official_eligibility_unverified" };
  }
  const identity = applicationIdentity(selectedCandidate, account);
  const prior = receipts.has(identity);
  if (prior) return { status: "skipped", reason: prior.status === "submit_unknown" ? "submit_unknown_terminal" : "already_applied", identity };

  const browser = browserFor(selectedCandidate);
  let observation = await browser.observe();
  const facts = factIndex(context);
  for (const field of observation.fields) {
    const action = await decide({ stage: "fill", candidate: clone(selectedCandidate), field: clone(field), observation: clone(observation), context: clone(context) });
    if (action?.kind === "stop") return { status: "human_required", reason: action.reason, identity };
    if (action?.kind !== "fill" || action.fieldId !== field.id) {
      if (field.required) return { status: "not_submitted", reason: "required_field_unresolved", field: field.label, identity };
      continue;
    }
    if (field.guard === "human_required") return { status: "human_required", reason: "human_only_field", identity };
    const inferredJudgment = field.answerClass === "judgment" && action.provenance === "reasonable_inference";
    if (!inferredJudgment && (!facts.has(action.factId) || facts.get(action.factId) !== action.value)) {
      return { status: "not_submitted", reason: "unsupported_claim", field: field.label, identity };
    }
    observation = await browser.fill(field, action.value);
  }
  const final = await decide({ stage: "submit", candidate: clone(selectedCandidate), observation: clone(observation) });
  if (final?.kind !== "submit" || observation.fields.some((field) => field.required && !field.value)) {
    return { status: "not_submitted", reason: "review_not_ready", identity };
  }
  assert.equal(receipts.claim(identity), true, "the submit effect is claimed immediately before the click");
  const outcome = await browser.submit();
  if (outcome.kind === "ambiguous") {
    receipts.finalize(identity, "submit_unknown", await browser.readback());
    return { status: "submit_unknown", identity, submitCount: browser.state.submitCount };
  }
  if (outcome.kind !== "confirmed") {
    receipts.finalize(identity, "not_submitted", await browser.readback());
    return { status: "not_submitted", reason: "submit_failed", identity };
  }
  const readback = await browser.readback();
  if (readback?.official !== true) {
    receipts.finalize(identity, "submit_unknown", readback);
    return { status: "submit_unknown", identity, submitCount: browser.state.submitCount };
  }
  receipts.finalize(identity, "submitted", readback);
  return { status: "submitted", identity, readback, submitCount: browser.state.submitCount };
}

function candidate(overrides = {}) {
  return {
    organization: "Open Program Collective",
    program: "Builder Fellowship",
    cohort_window: "Fall 2026",
    cohort: "Fall 2026",
    applicationUrl: "https://program.example/apply/fall-2026",
    official: { verified: true, eligible: true, deadline: "2026-09-30" },
    ...overrides,
  };
}

function contextPlanner() {
  return async (input) => {
    if (input.stage === "qualify") return input.candidates.find((item) => item.official?.eligible === true);
    if (input.stage === "fill") {
      const byLabel = {
        "Product name": ["product.name", startupContext.product.name],
        "Product summary": ["product.one_liner", startupContext.product.one_liner],
        "Public product URL": ["links.product.url", startupContext.links.product.url],
      };
      const pair = byLabel[input.field.label];
      return pair ? { kind: "fill", fieldId: input.field.id, factId: pair[0], value: pair[1] } : { kind: "skip" };
    }
    return { kind: "submit" };
  };
}

test("production contract runs every 30 minutes and maximizes real applications", () => {
  const contract = `${skill}\n${dailyPrompt}`;
  assert.match(contract, /every 30 minutes/i);
  assert.equal(loopRegistry.loops.fundraiser.cadence.start_interval_seconds, 1800);
  assert.match(fundraiserPlist, /<key>StartInterval<\/key><integer>1800<\/integer>/);
  assert.match(contract, /as many[^\n]*applications[^\n]*as possible/i);
  assert.match(contract, /continue[^\n]*after[^\n]*(?:first|one)[^\n]*(?:submit|application)/i);
  assert.match(contract, /authenticated[^\n]*X[^\n]*CDP/i);
  assert.match(contract, /Telegram[^\n]*(?:immediately|real.?time)/i);
  assert.match(contract, /reasonable inference/i);
  assert.match(dailyPrompt, /Ledger-first selection/);
  assert.match(dailyPrompt, /remote programs remain eligible/i);
  assert.doesNotMatch(dailyPrompt, /allow remote only when explicitly listed/i);
  assert.match(dailyPrompt, /read the complete receipt ledger and application dossiers before the priority queue/);
  assert.match(dailyPrompt, /SR008[^\n]*terminal[^\n]*SR009[^\n]*new opportunity/i);
  assert.match(dailyPrompt, /Fall 2026[^\n]*F26[^\n]*same cohort/i);
  assert.match(dailyPrompt, /unchanged blocker[^\n]*new discovery/i);
  assert.match(dailyPrompt, /cdp_tab_gc\.py --owner ai\.anicca\.fundraiser/);
  assert.match(dailyPrompt, /cdp_default_tab\.py open about:blank --owner ai\.anicca\.fundraiser/);
  assert.match(dailyPrompt, /cdp_default_tab\.py close "\$TARGET_ID" --owner ai\.anicca\.fundraiser/);
  assert.match(dailyPrompt, /FUNDRAISER_EVIDENCE_DIR\/target-id/);
  assert.match(dailyPrompt, /helper has no `list` command/);
  assert.doesNotMatch(dailyPrompt, /cdp_context_lease\.py/);
  assert.match(dailyPrompt, /jq -r '\.target_id'/);
  assert.match(dailyPrompt, /Never use `rg`, `grep`, `find`, `locate`/);
  assert.match(dailyPrompt, /untrusted data/);
  assert.match(dailyPrompt, /cdp\.py eval "\$TARGET_ID" -/);
  assert.match(dailyPrompt, /gog gmail send[^\n]*--attach "\$FUNDRAISER_VERIFIED_DECK"/);
  assert.match(dailyPrompt, /in:sent to:<recipient>/);
  assert.match(dailyPrompt, /validate-outbound-email\.py/);
  assert.match(dailyPrompt, /--from "\$GMAIL_ACCOUNT"/);
  assert.match(dailyPrompt, /Daisuke Narita/);
  assert.match(dailyPrompt, /bracketed placeholders/);
  assert.match(dailyPrompt, /USD 1,000/);
  assert.match(dailyPrompt, /malformed currency such as `,000`/);
  assert.match(dailyPrompt, /Never start an interactive shell/);
  assert.match(dailyPrompt, /empty generic formstate is\s+an observation fallback signal/);
  assert.match(dailyPrompt, /label-to-control mapping/);
  assert.match(dailyPrompt, /wait up to 30 seconds/);
  assert.match(dailyPrompt, /Never abandon the candidate\s+after only that immediate post-upload timeout/);
  assert.match(dailyPrompt, /prior failure or legacy blocked-state row, reopen\s+only when current evidence resolves or changes its blocker/);
  assert.match(dailyPrompt, /complete current `reason`/);
  assert.match(dailyPrompt, /action begins with `apply_now`/);
  assert.match(dailyPrompt, /one-shot action suffix is itself concrete new\s+evidence/);
  assert.match(dailyPrompt, /compare its latest receipt blocker with the complete current queue reason/);
  assert.match(dailyPrompt, /prior terminal receipt[\s\S]*?carried forward without opening the site/);
  assert.match(dailyPrompt, /terminal duplicate[^\n]*must not append[^\n]*receipt/i);
  assert.match(dailyPrompt, /preserve the existing `submitted_verified` or `submit_unknown` row as the latest outcome/);
  assert.match(dailyPrompt, /action does not begin with `apply_now`[^\n]*must not open[^\n]*official site/i);
  assert.match(dailyPrompt, /single row with the greatest\s+`utc_timestamp`/);
  assert.match(dailyPrompt, /changed queue reason is not new evidence when its authorization\s+was already exercised/);
  assert.match(dailyPrompt, /never carry the stale blocked state forward unchanged/i);
  assert.match(dailyPrompt, /cdp\.py screenshot/);
  assert.match(dailyPrompt, /completion\.png" viewport/);
  assert.match(dailyPrompt, /completion\.png/);
  assert.match(dailyPrompt, /TELEGRAM_PHOTO_SENT=true MSGID=<id>/);
  assert.match(dailyPrompt, /submitted_verified/);
  assert.match(dailyPrompt, /Legacy `submitted` rows[\s\S]*?evidence-incomplete/);
  assert.match(dailyPrompt, /must be reprocessed read-only for verifiable evidence/);
  assert.match(dailyPrompt, /Never send, submit, follow up, or otherwise recreate the\s+external effect merely to obtain missing evidence/);
  assert.match(dailyPrompt, /applies equally to Web forms and email pitch\/application/);
  assert.match(runtimeScript, /send-telegram-photo\.sh/);
  assert.match(runtimeScript, /status == "submitted_verified"/);
  assert.match(runtimeScript, /FUNDRAISER_APPLICATIONS_DIR/);
  assert.match(runtimeScript, /record-application\.py/);
  assert.doesNotMatch(runtimeScript, /delete context\./);
  assert.match(runtimeScript, /--prepare --draft <draft> --ledger "\$STATE_ROOT\/application-receipts\.jsonl" --applications-dir "\$STATE_ROOT\/applications"/);
  assert.match(dailyPrompt, /application_digest/);
  assert.match(runtimeScript, /MIN_FREE_KIB=\$\(\(1536 \* 1024\)\)/);
  assert.match(runtimeScript, /PRESSURE_FREE_KIB=\$\(\(2 \* 1024 \* 1024\)\)/);
  assert.match(runtimeScript, /required_kib=\$PRESSURE_FREE_KIB/);
  assert.doesNotMatch(runtimeScript, /\.openclaw/);
  assert.match(runtimeScript, /disk-cleanup/);
  assert.match(runtimeScript, /"\$LOOP_CLI" restart life-manager-disk-cleanup/);
  assert.match(runtimeScript, /exit 75/);
  assert.match(runtimeScript, /cdp_healthy/);
  assert.match(runtimeScript, /"\$LOOP_CLI" restart life-manager-daily-driver/);
  assert.match(runtimeScript, /json\/version/);
  assert.match(runtimeScript, /retry the same candidate observation once/);
  assert.match(dailyPrompt, /every visible question paired with the final rendered answer/);
  assert.match(dailyPrompt, /Never append `submitted_verified` yourself/);
  assert.match(dailyPrompt, /never close it until every fill/);
  assert.match(dailyPrompt, /there is no `upload` command/);
  assert.match(dailyPrompt, /Do not reopen the same video, voice, binding-term/);
  assert.match(dailyPrompt, /solve-recaptcha-v2\.py/);
  assert.match(dailyPrompt, /--target-id "\$TARGET_ID"/);
  assert.match(dailyPrompt, /Require exactly `CALLBACKS=1`/);
  assert.match(dailyPrompt, /Do not traverse or invoke internal reCAPTCHA callbacks/);
  assert.match(dailyPrompt, /scrollIntoView\(\{block:"center"\}\)/);
  assert.match(dailyPrompt, /Never reuse a pre-scroll or\s+off-viewport button coordinate/);
  assert.match(dailyPrompt, /resolves the rendered `data-callback` name/);
  assert.match(dailyPrompt, /textarea\[name="g-recaptcha-response"\]/);
  assert.match(dailyPrompt, /Never include the credential or solution token in logs/);
  assert.match(dailyPrompt, /never print, dump, enumerate, pretty-print/);
  assert.match(dailyPrompt, /assign the selected secret directly to `GOG_KEYRING_PASSWORD` without echoing it/);
  assert.match(dailyPrompt, /cdp\.py fillcss/);
  assert.match(dailyPrompt, /cdp\.py filllabel/);
  assert.match(dailyPrompt, /cdp\.py typelabel/);
  assert.match(dailyPrompt, /absolute path/);
  assert.match(runtimeScript, /--task-class application-lane-agent/);
  assert.doesNotMatch(runtimeScript, /--escalation-reason/);
  assert.equal(runnerConfig.task_classes["application-lane-agent"].timeout_seconds, 3600);
  assert.doesNotMatch(contract, /at most one/i);
  assert.doesNotMatch(contract, /per user-local day/i);
});

test("run lock recovers an abandoned owner and refuses a live owner", async () => {
  const root = await mkdtemp(join(tmpdir(), "fundraiser-run-lock-"));
  const lock = join(root, "run.lock");
  const script = join(root, "check.sh");
  const winners = join(root, "winners");
  const worker = join(root, "worker.sh");
  await writeFile(worker, `#!/bin/bash\nsource ${JSON.stringify(runLock.pathname)}\nif acquire_run_lock ${JSON.stringify(lock)}; then echo "$$" >> ${JSON.stringify(winners)}; sleep 1; release_run_lock ${JSON.stringify(lock)}; fi\n`);
  await writeFile(script, `#!/bin/bash\nset -eu\nsource ${JSON.stringify(runLock.pathname)}\nacquire_run_lock ${JSON.stringify(lock)}\n! acquire_run_lock ${JSON.stringify(lock)}\nrelease_run_lock ${JSON.stringify(lock)}\nmkdir ${JSON.stringify(lock)}\nprintf '999999999\\ndead-start\\n' > ${JSON.stringify(join(lock, "owner"))}\nacquire_run_lock ${JSON.stringify(lock)}\nrelease_run_lock ${JSON.stringify(lock)}\nmkdir ${JSON.stringify(lock)}\nprintf '%s\\n%s\\n' "$$" 'reused-pid-start' > ${JSON.stringify(join(lock, "owner"))}\nacquire_run_lock ${JSON.stringify(lock)}\nrelease_run_lock ${JSON.stringify(lock)}\nmkdir ${JSON.stringify(lock)}\n! acquire_run_lock ${JSON.stringify(lock)}\nrmdir ${JSON.stringify(lock)}\nmkdir ${JSON.stringify(lock)}\ntouch -t 200001010000 ${JSON.stringify(lock)}\nacquire_run_lock ${JSON.stringify(lock)}\nrelease_run_lock ${JSON.stringify(lock)}\n: > ${JSON.stringify(winners)}\n/bin/bash ${JSON.stringify(worker)} &\n/bin/bash ${JSON.stringify(worker)} &\nwait\n[ "$(wc -l < ${JSON.stringify(winners)} | tr -d ' ')" = 1 ]\n`);
  await chmod(worker, 0o700);
  await chmod(script, 0o700);
  const result = spawnSync("/bin/bash", [script], { encoding: "utf8" });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});

test("verified application recorder writes a full dossier and rejects exact replay", async () => {
  const root = await mkdtemp(join(tmpdir(), "fundraiser-recorder-"));
  const png = join(root, "completion.png");
  const draft = join(root, "draft.json");
  const ledger = join(root, "receipts.jsonl");
  const applications = join(root, "applications");
  const contextVersion = "2026-08-27.2";
  const contextDigest = "9fbe6198c6d61da47d68767eec90a1d95d2e07058f024448d86372b5f3035338";
  await writeFile(png, "official completion image");
  await writeFile(draft, JSON.stringify({
    organization: "Example VC", program: "Accelerator", cohort_window: "Cohort 1",
    account: "account:test", official_url: "https://example.test/apply",
    contact: { method: "web_form", destination: "https://example.test/apply" },
    question_answers: [{ question: "What are you building?", answer: "Life Manager" }],
    attachments: [],
    context_used: { "product.name": ".agents/startup-context.json" },
    context_version: contextVersion,
    context_digest: contextDigest,
  }));
  const expectedContextArgs = ["--expected-context-version", contextVersion,
    "--expected-context-digest", contextDigest];
  const prepare = spawnSync("python3", [applicationRecorder.pathname, "--prepare", "--draft", draft,
    "--ledger", ledger, "--applications-dir", applications,
    ...expectedContextArgs], { encoding: "utf8" });
  assert.equal(prepare.status, 0, prepare.stderr);
  const prepared = JSON.parse(await readFile(draft, "utf8"));
  assert.match(prepared.application_digest, /^[a-f0-9]{64}$/);
  prepared.submitted_at = prepared.previewed_at;
  prepared.evidence = { completion_png: png, telegram_photo_message_id: 123,
    provider_readback: "Thank you for applying" };
  const args = [applicationRecorder.pathname, "--draft", draft, "--ledger", ledger,
    "--applications-dir", applications, "--run-id", "test-run", ...expectedContextArgs];
  prepared.question_answers[0].answer = "Tampered after preview";
  await writeFile(draft, JSON.stringify(prepared));
  const tampered = spawnSync("python3", args, { encoding: "utf8" });
  assert.notEqual(tampered.status, 0);
  assert.match(tampered.stderr, /application_digest does not match/);
  prepared.question_answers[0].answer = "Life Manager";
  await writeFile(draft, JSON.stringify(prepared));
  const first = spawnSync("python3", args, { encoding: "utf8" });
  assert.equal(first.status, 0, first.stderr);
  const row = JSON.parse((await readFile(ledger, "utf8")).trim());
  assert.equal(row.status, "submitted_verified");
  assert.equal(row.context_version, contextVersion);
  assert.equal(row.context_digest, contextDigest);
  assert.equal(row.application_digest, prepared.application_digest);
  assert.match(row.application_record_sha256, /^[a-f0-9]{64}$/);
  const dossier = JSON.parse(await readFile(row.application_record_path, "utf8"));
  assert.equal(dossier.question_answers[0].answer, "Life Manager");
  const replay = spawnSync("python3", args, { encoding: "utf8" });
  assert.notEqual(replay.status, 0);
  assert.match(replay.stderr, /duplicate terminal application/);
});

test("application prepare rejects a prior terminal cohort despite date wording drift", async () => {
  const root = await mkdtemp(join(tmpdir(), "fundraiser-recorder-dedupe-"));
  const draft = join(root, "draft.json");
  const ledger = join(root, "receipts.jsonl");
  const applications = join(root, "applications");
  const contextVersion = "2026-08-27.2";
  const contextDigest = "9fbe6198c6d61da47d68767eec90a1d95d2e07058f024448d86372b5f3035338";
  await writeFile(ledger, `${JSON.stringify({
    receipt_identity: "SF Startup Labs|September 14 2026 cohort|rolling application|account:test",
    official_url: "https://www.sfstartuplabs.com/apply",
    status: "submitted_verified",
  })}\n`);
  await writeFile(draft, JSON.stringify({
    organization: "SF Startup Labs",
    program: "SF Startup Labs",
    cohort_window: "2026-09-14 through 2026-10-03",
    account: "account:test",
    official_url: "https://sfstartuplabs.com/apply",
    contact: { method: "web_form", destination: "https://sfstartuplabs.com/apply" },
    question_answers: [{ question: "What are you building?", answer: "Life Manager" }],
    attachments: [],
    context_used: { "product.name": ".agents/startup-context.json" },
    context_version: contextVersion,
    context_digest: contextDigest,
  }));
  const prepare = spawnSync("python3", [applicationRecorder.pathname, "--prepare", "--draft", draft,
    "--ledger", ledger, "--applications-dir", applications,
    "--expected-context-version", contextVersion, "--expected-context-digest", contextDigest],
  { encoding: "utf8" });
  assert.notEqual(prepare.status, 0);
  assert.match(prepare.stderr, /duplicate terminal application/);

  const newCohort = JSON.parse(await readFile(draft, "utf8"));
  newCohort.cohort_window = "2027-09-14 through 2027-10-03";
  await writeFile(draft, JSON.stringify(newCohort));
  const allowed = spawnSync("python3", [applicationRecorder.pathname, "--prepare", "--draft", draft,
    "--ledger", ledger, "--applications-dir", applications,
    "--expected-context-version", contextVersion, "--expected-context-digest", contextDigest],
  { encoding: "utf8" });
  assert.equal(allowed.status, 0, allowed.stderr);
});

test("outbound email preflight rejects rendered spam defects", () => {
  const invalidBodies = [
    "Hello team,\\n\\nPitch\\n\\nBest,\\nDaisuke Narita",
    "Hello team,\n\nFounder: [founder name in sender account]\nEmail: [sender address]\n\nBest,\nDaisuke Narita",
    "Hello team,\n\nRevenue is approximately ,000.\n\nBest,\nDaisuke Narita",
  ];
  for (const body of invalidBodies) {
    const result = spawnSync("python3", [emailValidator.pathname], { input: body, encoding: "utf8" });
    assert.notEqual(result.status, 0, body);
  }

  const valid = "Hello team,\n\nI am sharing Life Manager for your current accelerator.\n\nBest,\nDaisuke Narita";
  const result = spawnSync("python3", [emailValidator.pathname], { input: valid, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, valid);
});

test("production queue retries deck-repaired candidates before inactive work", () => {
  const fundraising = productionOpportunities;
  assert.deepEqual(fundraising.geographies, [
    "Tokyo, Japan",
    "United States, with San Francisco Bay Area first",
  ]);
  assert.match(fundraising.format_priority, /in-person/i);
  assert.deepEqual(fundraising.explicit_format_exceptions, []);
  assert.deepEqual(fundraising.priority_queue.map((item) => item.program), [
    "HF0 Residency",
    "Founder Submission",
    "Submit Your Company",
    "GSAP2026 Enterprise B2B Course Phase 2",
    "3 Month Program + Community",
    "ASAC 4th Pre-seed / 23rd Seed Program",
    "Y Combinator",
  ]);
  assert.deepEqual(fundraising.priority_queue.map((item) => item.action), [
    "apply_now_resume_second_page",
    "apply_now_deck_fix",
    "apply_now_deck_fix",
    "inactive_deadline_passed",
    "terminal_ledger_owned",
    "retry_when_provider_replies_to_password_recovery",
    "retry_when_current_email_verification_available",
  ]);
  assert.match(fundraising.priority_queue[0].reason, /page two/);
  assert.match(fundraising.priority_queue[1].reason, /FUNDRAISER_VERIFIED_DECK/);
  assert.match(fundraising.priority_queue[2].reason, /FUNDRAISER_VERIFIED_DECK/);
  assert.match(fundraising.priority_queue[3].reason, /deadline has passed/);
  assert.match(fundraising.priority_queue[4].reason, /terminal receipt/);
  assert.match(fundraising.priority_queue[5].reason, /password-recovery request/);
  assert.match(fundraising.priority_queue[6].reason, /email verification/);
  assert.match(dailyPrompt, /format and geography[\s\S]*ranking preferences, not automatic rejection rules/i);
  assert.match(dailyPrompt, /Never submit a `hold_do_not_submit` program/);
  assert.match(dailyPrompt, /Ordinary privacy-policy and data-processing consent/);
  assert.match(dailyPrompt, /Do not infer consent to investment, equity/);
  assert.match(dailyPrompt, /operate on the visible, enabled\s+control inside the active dialog/);
  assert.match(dailyPrompt, /first\(\.\. \| objects \| select/);
  assert.match(dailyPrompt, /never serialize the JSON\s+object into a public form/);
  assert.match(dailyPrompt, /Every coordinate passed to `cdp\.py clickxy` must be a validated base-10 integer/);
  assert.match(dailyPrompt, /prefer an exact visible `aria-label`/);
  assert.match(dailyPrompt, /Structural position selectors are a last resort/);
  assert.match(dailyPrompt, /top-level `facts\[\]` claim records/);
  assert.match(dailyPrompt, /Never infer legal ownership merely from the words/);
});

test("unseen rendered fields are filled from startup context and read back after one submit", async () => {
  assert.ok(dailyPrompt.trim().length > 200);
  const form = {
    applicationUrl: "https://program.example/apply/fall-2026",
    fields: [
      { id: "question-a", label: "Product summary", required: true },
      { id: "question-b", label: "Product name", required: true },
      { id: "question-c", label: "Public product URL", required: true },
    ],
    readback: { official: true, confirmation: "Application received", ref: "confirmation-42" },
  };
  const browser = makeBrowser(form);
  const receipts = makeReceipts();
  const result = await runDailyPass({
    discover: async () => [candidate()],
    receipts,
    browserFor: () => browser,
    account: "runtime-account",
    decide: contextPlanner(),
  });
  assert.equal(result.status, "submitted");
  assert.equal(result.readback.ref, "confirmation-42");
  assert.equal(browser.state.submitCount, 1);
  assert.deepEqual(browser.state.actions.filter((action) => action.kind === "fill").map((action) => action.label), [
    "Product summary", "Product name", "Public product URL",
  ]);
});

test("an already-applied cohort is skipped before opening its form", async () => {
  const old = candidate({ cohort_window: "Spring 2026", cohort: "Spring 2026", applicationUrl: "https://program.example/apply/spring-2026" });
  const receipts = makeReceipts([{ identity: applicationIdentity(old, "runtime-account"), status: "submitted" }]);
  let opened = false;
  const result = await runDailyPass({
    discover: async () => [old],
    receipts,
    browserFor: () => { opened = true; return makeBrowser({ applicationUrl: old.applicationUrl, fields: [] }); },
    account: "runtime-account",
    decide: async (input) => input.stage === "qualify" ? input.candidates[0] : { kind: "submit" },
  });
  assert.equal(result.reason, "already_applied");
  assert.equal(opened, false);
});

test("a genuinely new cohort remains eligible after an earlier cohort receipt", async () => {
  const old = candidate({ cohort_window: "Spring 2026", cohort: "Spring 2026", applicationUrl: "https://program.example/apply/spring-2026" });
  const fresh = candidate();
  const receipts = makeReceipts([{ identity: applicationIdentity(old, "runtime-account"), status: "submitted" }]);
  const browser = makeBrowser({ applicationUrl: fresh.applicationUrl, fields: [], readback: { official: true } });
  const result = await runDailyPass({
    discover: async () => [old, fresh],
    receipts,
    browserFor: () => browser,
    account: "runtime-account",
    decide: async (input) => input.stage === "qualify" ? input.candidates[1] : { kind: "submit" },
  });
  assert.equal(result.status, "submitted");
  assert.equal(receipts.rows.filter((row) => row.status === "submitted").length, 2);
});

test("two eligible candidates are both submitted in one continuous pass", async () => {
  const candidates = [
    candidate({ organization: "Alpha Fund", program: "Alpha Accelerator", applicationUrl: "https://alpha.example/apply" }),
    candidate({ organization: "Beta Fund", program: "Beta Fellowship", applicationUrl: "https://beta.example/apply" }),
  ];
  const receipts = makeReceipts();
  const results = [];
  for (const current of candidates) {
    const browser = makeBrowser({ applicationUrl: current.applicationUrl, fields: [], readback: { official: true, ref: current.organization } });
    results.push(await runDailyPass({
      discover: async () => [current], receipts, browserFor: () => browser, account: "runtime-account",
      decide: async (input) => input.stage === "qualify" ? input.candidates[0] : { kind: "submit" },
    }));
  }
  assert.deepEqual(results.map((result) => result.status), ["submitted", "submitted"]);
  assert.equal(receipts.rows.filter((row) => row.status === "submitted").length, 2);
});

test("an unfamiliar required judgment uses reasonable inference and still submits", async () => {
  const form = {
    applicationUrl: "https://program.example/apply/fall-2026",
    fields: [{ id: "impact", label: "Long-term impact", required: true, answerClass: "judgment" }],
    readback: { official: true },
  };
  const browser = makeBrowser(form);
  const receipts = makeReceipts();
  const result = await runDailyPass({
    discover: async () => [candidate()], receipts, browserFor: () => browser, account: "runtime-account",
    decide: async (input) => {
      if (input.stage === "qualify") return input.candidates[0];
      if (input.stage === "fill") return {
        kind: "fill", fieldId: input.field.id, value: "End suffering by making dependable agency available.", provenance: "reasonable_inference",
      };
      return { kind: "submit" };
    },
  });
  assert.equal(result.status, "submitted");
  assert.equal(browser.state.submitCount, 1);
});

test("an invented legal registration still blocks before any external effect", async () => {
  const form = {
    applicationUrl: "https://program.example/apply/fall-2026",
    fields: [{ id: "registration", label: "Legal registration number", required: true, answerClass: "exact_fact" }],
    readback: { official: true },
  };
  const browser = makeBrowser(form);
  const receipts = makeReceipts();
  const result = await runDailyPass({
    discover: async () => [candidate()],
    receipts,
    browserFor: () => browser,
    account: "runtime-account",
    decide: async (input) => {
      if (input.stage === "qualify") return input.candidates[0];
      if (input.stage === "fill") return { kind: "fill", fieldId: input.field.id, factId: "invented.registration", value: "FAKE-123" };
      return { kind: "submit" };
    },
  });
  assert.equal(result.reason, "unsupported_claim");
  assert.equal(browser.state.submitCount, 0);
  assert.equal(receipts.rows.length, 0);
});

test("a human-only field stops the loop before Submit", async () => {
  const form = {
    applicationUrl: "https://program.example/apply/fall-2026",
    fields: [{ id: "video", label: "Founder video", required: true, guard: "human_required" }],
    readback: { official: true },
  };
  const browser = makeBrowser(form);
  const receipts = makeReceipts();
  const result = await runDailyPass({
    discover: async () => [candidate()],
    receipts,
    browserFor: () => browser,
    account: "runtime-account",
    decide: async (input) => {
      if (input.stage === "qualify") return input.candidates[0];
      if (input.stage === "fill") return { kind: "stop", reason: "human_only_field" };
      return { kind: "submit" };
    },
  });
  assert.equal(result.status, "human_required");
  assert.equal(browser.state.submitCount, 0);
  assert.equal(receipts.rows.length, 0);
});

test("ambiguous Submit becomes terminal submit_unknown and is never retried", async () => {
  const form = {
    applicationUrl: "https://program.example/apply/fall-2026",
    fields: [],
    readback: null,
  };
  const browser = makeBrowser(form, { kind: "ambiguous" });
  const receipts = makeReceipts();
  const input = {
    discover: async () => [candidate()],
    receipts,
    browserFor: () => browser,
    account: "runtime-account",
    decide: async (value) => value.stage === "qualify" ? value.candidates[0] : { kind: "submit" },
  };
  const first = await runDailyPass(input);
  const second = await runDailyPass(input);
  assert.equal(first.status, "submit_unknown");
  assert.equal(second.reason, "submit_unknown_terminal");
  assert.equal(browser.state.submitCount, 1);
  assert.equal(receipts.rows[0].status, "submit_unknown");
});
