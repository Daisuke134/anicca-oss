import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { readFile, writeFile } from "node:fs/promises";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  auditStartupContext,
  contextDigest,
  loadStartupContext,
  validateStartupContext,
  validatePublicArtifact,
} from "../scripts/startup-context/lib.mjs";
import { buildApplicationKit } from "../scripts/startup-context/build-kit.mjs";
import { verifyDeckArtifact } from "../skills/fundraiser-agent/runtime/verify-deck.mjs";

const contextPath = new URL("../.agents/startup-context.json", import.meta.url);
const marketingPath = new URL(
  "../.agents/product-marketing-context.md",
  import.meta.url,
);

function clone(value) {
  return structuredClone(value);
}

test("canonical startup context is valid and names Life Manager as the product", async () => {
  const context = await loadStartupContext(contextPath);

  assert.equal(context.product.name, "Life Manager");
  assert.equal(context.company.legal_name, "Anicca");
  assert.notEqual(context.product.name, context.company.legal_name);
  assert.match(context.product.mission, /end suffering/i);
  assert.match(context.product.mission, /all living beings/i);
  assert.deepEqual(context.delivery, {
    local: "Free, open-source, self-hosted Life Manager.",
    cloud: "Paid monthly subscription for an always-on hosted Life Manager.",
  });
  assert.equal(context.traction.founder_attested_revenue.display, "approximately $1,000");
  assert.equal(context.traction.founder_attested_revenue.source, "founder_attested");
  for (const topic of ["mission", "revenue", "users", "applications", "agi"]) {
    const claim = context.claims.find((candidate) => candidate.topic === topic);
    assert.ok(claim, `missing ${topic} claim`);
    assert.equal(typeof claim.source, "string");
    assert.equal(typeof claim.status, "string");
    assert.equal(typeof claim.as_of, "string");
    assert.equal(typeof claim.public_use, "string");
  }
  assert.deepEqual(validateStartupContext(context), []);
});

test("marketing context defines the audience, pain, three organs, and truthful proof", async () => {
  const markdown = await readFile(marketingPath, "utf8");

  for (const heading of [
    "## Product Overview",
    "## Target Audience",
    "## Core Pain / Job to Be Done",
    "## Physical / Mental / Financial Organs",
    "## Differentiation",
    "## Alternatives / Competition",
    "## Objections",
    "## Customer Language",
    "## Brand Voice",
    "## Current Proof and Unknowns",
    "## Fundraising Goals",
  ]) {
    assert.match(markdown, new RegExp(`^${heading}$`, "m"));
  }
});

test("validator rejects missing required fields", async () => {
  const context = clone(await loadStartupContext(contextPath));
  delete context.links.repository;

  assert.match(validateStartupContext(context).join("\n"), /links\.repository/);
});

test("validator rejects an incomplete canonical application answer set", async () => {
  const context = clone(await loadStartupContext(contextPath));
  delete context.application_answers.progress;

  assert.match(validateStartupContext(context).join("\n"), /application_answers\.progress/);
});

test("validator rejects product and company name confusion", async () => {
  const context = clone(await loadStartupContext(contextPath));
  context.product.name = context.company.legal_name;

  assert.match(validateStartupContext(context).join("\n"), /product.*company/i);
});

test("validator rejects claims without evidence", async () => {
  const context = clone(await loadStartupContext(contextPath));
  context.claims.push({
    id: "unsupported-growth",
    topic: "growth",
    statement: "Life Manager guarantees investment returns.",
    source: "none",
    status: "unsupported",
    as_of: "2026-08-27T00:20:00+09:00",
    public_use: "prohibited",
    evidence: [],
  });

  const errors = validateStartupContext(context).join("\n");
  assert.match(errors, /unsupported-growth/);
  assert.match(errors, /evidence/i);
});

test("validator rejects a claim without provenance", async () => {
  const context = clone(await loadStartupContext(contextPath));
  delete context.claims.find((claim) => claim.topic === "revenue").source;

  assert.match(validateStartupContext(context).join("\n"), /founder-revenue: source is required/);
});

test("context digest is stable and changes with the facts", async () => {
  const context = await loadStartupContext(contextPath);
  const sameFacts = clone(context);
  const changedFacts = clone(context);
  changedFacts.product.name = "Different Product";

  assert.equal(contextDigest(context), contextDigest(sameFacts));
  assert.notEqual(contextDigest(context), contextDigest(changedFacts));
  assert.match(contextDigest(context), /^[a-f0-9]{64}$/);
});

test("audit detects stale facts and reports unverified optional media", async () => {
  const context = clone(await loadStartupContext(contextPath));
  const result = await auditStartupContext(context, {
    now: new Date("2026-10-02T00:00:00+09:00"),
    maxAgeDays: 30,
    checkLinks: false,
  });

  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /stale/i);
  assert.match(result.warnings.join("\n"), /links\.demo.*unverified/i);
  assert.match(result.warnings.join("\n"), /links\.founder_video.*unverified/i);
  assert.match(result.warnings.join("\n"), /links\.dashboard.*legacy/i);
});

test("audit rejects forbidden legacy exact values", async () => {
  const context = clone(await loadStartupContext(contextPath));
  context.links.repository.url = context.forbidden_exact_values.repositories[0];

  const result = await auditStartupContext(context, {
    now: new Date("2026-08-02T13:00:00+09:00"),
    checkLinks: false,
  });

  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /forbidden.*repository/i);
});

test("audit reads back every verified canonical link", async () => {
  const context = clone(await loadStartupContext(contextPath));
  const requested = [];
  const fetchImpl = async (url) => {
    requested.push(url);
    return new Response(
      `Life Manager ${context.context_version} ${contextDigest(context)}`,
      { status: 200 },
    );
  };

  const result = await auditStartupContext(context, {
    now: new Date("2026-08-02T13:00:00+09:00"),
    fetchImpl,
  });

  assert.equal(result.ok, true);
  assert.deepEqual(
    requested.sort(),
    ["product", "repository", "telegram"]
      .map((key) => context.links[key].url)
      .sort(),
  );
  assert.equal(result.link_checks.every((check) => check.ok), true);
});

test("audit rejects a public product page bound to an old startup context", async () => {
  const context = clone(await loadStartupContext(contextPath));
  const result = await auditStartupContext(context, {
    now: new Date("2026-08-02T13:00:00+09:00"),
    fetchImpl: async () => new Response("Life Manager 2026-08-01.1 stale-digest", { status: 200 }),
  });

  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /product.*context digest/i);
});

test("audit rejects a 200 page that does not contain the expected product identity", async () => {
  const context = clone(await loadStartupContext(contextPath));
  const result = await auditStartupContext(context, {
    now: new Date("2026-08-02T13:00:00+09:00"),
    fetchImpl: async () => new Response("Unrelated legacy product", { status: 200 }),
  });

  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /expected text/i);
});

test("English README first view explains the Life Manager product experience", async () => {
  const readme = (await readFile(new URL("../README.md", import.meta.url), "utf8")).slice(0, 4_000);

  assert.match(readme, /body, mind, and money/i);
  assert.match(readme, /Telegram/);
  assert.match(readme, /acts? within/i);
  assert.match(readme, /same core/i);
  assert.match(readme, /https:\/\/aniccaai\.com\/lm/);
  assert.doesNotMatch(readme, /Live Dashboard/);
});

test("Japanese README first view explains the Life Manager product experience", async () => {
  const readme = (await readFile(new URL("../README.ja.md", import.meta.url), "utf8")).slice(0, 4_000);

  assert.match(readme, /身体/);
  assert.match(readme, /心/);
  assert.match(readme, /お金/);
  assert.match(readme, /Telegram/);
  assert.match(readme, /委任された範囲/);
  assert.match(readme, /同じcore/);
  assert.match(readme, /https:\/\/aniccaai\.com\/lm/);
  assert.doesNotMatch(readme, /Live Dashboard/);
});

test("both public READMEs are bound to the canonical startup context", async () => {
  const context = await loadStartupContext(contextPath);

  for (const file of ["README.md", "README.ja.md"]) {
    const content = await readFile(new URL(`../${file}`, import.meta.url), "utf8");
    assert.deepEqual(validatePublicArtifact(content, context), [], file);
  }
});

test("application kit is deterministic and bound to the canonical context digest", async () => {
  const context = await loadStartupContext(contextPath);
  const directory = await mkdtemp(join(tmpdir(), "life-manager-kit-"));

  try {
    const first = await buildApplicationKit({ context, outputDirectory: directory });
    const firstContents = Object.fromEntries(
      await Promise.all(first.files.map(async (file) => [file, await readFile(join(directory, file), "utf8")])),
    );
    const second = await buildApplicationKit({ context, outputDirectory: directory });
    const secondContents = Object.fromEntries(
      await Promise.all(second.files.map(async (file) => [file, await readFile(join(directory, file), "utf8")])),
    );

    assert.deepEqual(second, first);
    assert.deepEqual(secondContents, firstContents);
    assert.deepEqual((await readdir(directory)).sort(), first.files.toSorted());
    for (const content of Object.values(firstContents)) {
      assert.match(content, new RegExp(context.context_version.replaceAll(".", "\\.")));
      assert.match(content, new RegExp(contextDigest(context)));
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("generated application kit describes Life Manager without unverified media", async () => {
  const context = await loadStartupContext(contextPath);
  const directory = await mkdtemp(join(tmpdir(), "life-manager-kit-"));

  try {
    await buildApplicationKit({ context, outputDirectory: directory });
    const answers = await readFile(join(directory, "answers.en.md"), "utf8");
    const deck = await readFile(join(directory, "deck.md"), "utf8");
    const assets = JSON.parse(await readFile(join(directory, "assets.json"), "utf8"));

    assert.match(answers, /body, mind, and money/i);
    assert.match(answers, /Telegram/);
    assert.match(answers, /all living beings/i);
    assert.match(answers, /approximately \$1,000/i);
    assert.match(answers, /open-source/i);
    assert.match(answers, /paid monthly subscription/i);
    assert.match(deck, /Daily Organ/);
    assert.match(deck, /Financial Organ/);
    assert.equal(assets.assets.some((asset) => asset.status === "verified" && asset.type === "video"), false);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("generated pitch deck PDF is bound to the current context and receipt", async () => {
  const context = await loadStartupContext(contextPath);
  const directory = await mkdtemp(join(tmpdir(), "life-manager-kit-"));

  try {
    await buildApplicationKit({ context, outputDirectory: directory });
    const pdf = await readFile(join(directory, "deck.pdf"));
    const receipt = JSON.parse(await readFile(join(directory, "deck.pdf.receipt.json"), "utf8"));

    assert.match(pdf.subarray(0, 8).toString("ascii"), /^%PDF-1\.4/);
    assert.equal(receipt.context_version, context.context_version);
    assert.equal(receipt.context_digest, contextDigest(context));
    assert.equal(receipt.pdf_sha256, createHash("sha256").update(pdf).digest("hex"));
    const verified = await verifyDeckArtifact({
      contextPath: fileURLToPath(contextPath),
      assetsPath: join(directory, "assets.json"),
      receiptPath: join(directory, "deck.pdf.receipt.json"),
      pdfPath: join(directory, "deck.pdf"),
    });
    assert.equal(verified.pages, 10);

    await writeFile(join(directory, "deck.pdf"), Buffer.concat([pdf, Buffer.from("tampered")]));
    await assert.rejects(() => verifyDeckArtifact({
      contextPath: fileURLToPath(contextPath),
      assetsPath: join(directory, "assets.json"),
      receiptPath: join(directory, "deck.pdf.receipt.json"),
      pdfPath: join(directory, "deck.pdf"),
    }), /SHA-256 mismatch|truncated/);

    await writeFile(join(directory, "deck.pdf"), pdf);
    const changedContext = clone(context);
    changedContext.product.one_liner = `${changedContext.product.one_liner} changed`;
    const changedContextPath = join(directory, "changed-context.json");
    await writeFile(changedContextPath, JSON.stringify(changedContext));
    await assert.rejects(() => verifyDeckArtifact({
      contextPath: changedContextPath,
      assetsPath: join(directory, "assets.json"),
      receiptPath: join(directory, "deck.pdf.receipt.json"),
      pdfPath: join(directory, "deck.pdf"),
    }), /context digest is stale/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("the committed fundraising kit matches the canonical startup context", async () => {
  const context = await loadStartupContext(contextPath);

  for (const file of ["README.md", "answers.en.md", "answers.ja.md", "assets.json", "deck.md", "one-pager.md"]) {
    const content = await readFile(new URL(`../fundraising/application-kit/${file}`, import.meta.url), "utf8");
    assert.deepEqual(validatePublicArtifact(content, context), [], file);
  }
});

test("public artifact validator blocks legacy product values, PII, and placeholders", async () => {
  const context = await loadStartupContext(contextPath);
  const digest = contextDigest(context);
  const metadata = `context-version: ${context.context_version}\ncontext-digest: ${digest}\n`;

  assert.match(validatePublicArtifact(`${metadata}Repository: https://github.com/Daisuke134/anicca-oss`, context).join("\n"), /forbidden/i);
  assert.match(validatePublicArtifact(`${metadata}Contact: private-person@example.com`, context).join("\n"), /email/i);
  assert.match(validatePublicArtifact(`${metadata}Answer: {{traction}}`, context).join("\n"), /placeholder/i);
  assert.match(validatePublicArtifact(`${metadata}Life Manager is an AGI.`, context).join("\n"), /achieved-agi/);
  assert.match(validatePublicArtifact(`${metadata}Life Manager has 10,000 users.`, context).join("\n"), /numeric-users/);
  assert.match(validatePublicArtifact(`${metadata}Life Manager was accepted to Example Accelerator.`, context).join("\n"), /unverified-application-outcome/);
  assert.deepEqual(validatePublicArtifact(`${metadata}Product: Life Manager`, context), []);
});
