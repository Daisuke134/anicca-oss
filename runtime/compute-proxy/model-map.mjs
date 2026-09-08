const RAW_FREE_MODEL = "nvidia/gpt-oss-120b";
const FREE_MODELS = new Set([RAW_FREE_MODEL, "nvidia/llama-4-maverick"]);
const PROFILES = new Set([
  "auto", "premium", "eco", "free",
  "blockrun/auto", "blockrun/premium", "blockrun/eco", "blockrun/free",
]);

export function normalizeModel(model, frontierModel) {
  const value = String(model || "").toLowerCase();
  if (value.startsWith("free/")) return RAW_FREE_MODEL;
  if (PROFILES.has(value)) return frontierModel;
  return model;
}

export function normalizeRequestBody(body, frontierModel) {
  return { ...body, model: normalizeModel(body?.model, frontierModel) };
}

export function normalizeModelSelection(model, frontierModel) {
  const normalized = normalizeModel(model, frontierModel);
  const configured = String(model || "").toLowerCase();
  const tier = configured.startsWith("free/") || FREE_MODELS.has(normalized)
    ? "free"
    : "frontier";
  return { model: normalized, tier };
}
