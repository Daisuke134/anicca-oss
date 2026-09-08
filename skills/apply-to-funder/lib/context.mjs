import { contextDigest, auditStartupContext } from "../../../scripts/startup-context/lib.mjs";

const DUPLICATED_FACT_FIELDS = [
  "product_name",
  "homepage",
  "repository",
  "company",
  "company_name",
  "traction",
  "revenue",
];

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

export function validateFunderConfig(config) {
  const errors = [];
  if (config?.schema_version !== 1) errors.push("schema_version must equal 1");
  for (const key of ["id", "program_name", "official_url", "application_url"]) {
    if (!isNonEmptyString(config?.[key])) errors.push(`${key} is required`);
  }
  for (const key of DUPLICATED_FACT_FIELDS) {
    if (Object.hasOwn(config ?? {}, key)) errors.push(`duplicated startup fact field is forbidden: ${key}`);
  }
  if (!isNonEmptyString(config?.program_evidence?.verified_at)) {
    errors.push("program_evidence.verified_at is required");
  }
  if (!Array.isArray(config?.program_evidence?.sources) || config.program_evidence.sources.length === 0) {
    errors.push("program_evidence.sources is required");
  }
  if (!Array.isArray(config?.questions) || config.questions.length === 0) {
    errors.push("questions must be a non-empty array");
  }
  if (!Array.isArray(config?.required_assets)) errors.push("required_assets must be an array");
  if (!Array.isArray(config?.requested_assets)) errors.push("requested_assets must be an array");
  return errors;
}

function ageInDays(value, now) {
  return (now.getTime() - Date.parse(value)) / 86_400_000;
}

export async function compileFunderPreview({ context, funderConfig, now = new Date() }) {
  const configErrors = validateFunderConfig(funderConfig);
  if (configErrors.length > 0) throw new Error(`Invalid funder config:\n${configErrors.join("\n")}`);

  const contextAudit = await auditStartupContext(context, { now, checkLinks: false });
  if (!contextAudit.ok) throw new Error(`Startup context gate failed:\n${contextAudit.errors.join("\n")}`);

  if (ageInDays(funderConfig.program_evidence.verified_at, now) > 14) {
    throw new Error("Program evidence is stale and must be refreshed from official sources");
  }

  const attachments = [];
  for (const key of funderConfig.requested_assets) {
    const link = context.links[key];
    if (!link || link.status !== "verified" || !link.url) {
      throw new Error(`${key} is ${link?.status ?? "missing"}; unverified assets cannot enter preview attachments`);
    }
    attachments.push({ type: key, url: link.url, verified_at: link.verified_at });
  }

  const missingRequiredAssets = funderConfig.required_assets.filter((key) => {
    const link = context.links[key];
    return !link || link.status !== "verified" || !link.url;
  });
  const digest = contextDigest(context);
  const applicationDigest = contextDigest({
    context_digest: digest,
    funder: funderConfig,
    attachments,
  });

  return {
    mode: "preview",
    submit_allowed: false,
    blockers: missingRequiredAssets.map((key) => `required asset is not verified: ${key}`),
    context_version: context.context_version,
    context_digest: digest,
    application_digest: applicationDigest,
    program: {
      id: funderConfig.id,
      name: funderConfig.program_name,
      official_url: funderConfig.official_url,
      application_url: funderConfig.application_url,
      evidence: funderConfig.program_evidence,
    },
    product: {
      name: context.product.name,
      company_legal_name: context.company.legal_name,
      one_liner: context.product.one_liner,
      homepage: context.links.product.url,
      repository: context.links.repository.url,
      telegram: context.links.telegram.url,
    },
    application_kit: {
      answers_en: "fundraising/application-kit/answers.en.md",
      answers_ja: "fundraising/application-kit/answers.ja.md",
      deck: "fundraising/application-kit/deck.pdf",
      one_pager: "fundraising/application-kit/one-pager.md",
      assets: "fundraising/application-kit/assets.json",
    },
    attachments,
    questions: funderConfig.questions,
  };
}

export function assertSubmissionMatchesPreview({ preview, payload }) {
  const errors = [];
  if (!payload?.context_digest) errors.push("submission payload is missing context digest");
  else if (payload.context_digest !== preview.context_digest) errors.push("context digest mismatch");
  if (!payload?.application_digest) errors.push("submission payload is missing application digest");
  else if (payload.application_digest !== preview.application_digest) errors.push("application digest mismatch");
  return errors;
}
