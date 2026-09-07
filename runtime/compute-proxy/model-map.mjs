const RAW_FREE_MODEL = "nvidia/gpt-oss-120b";
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
