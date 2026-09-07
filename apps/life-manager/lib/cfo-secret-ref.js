"use strict";

const SECRET_REF = /^secret:\/\/[a-z0-9][a-z0-9._-]*(?:\/[a-z0-9][a-z0-9._-]*)*$/i;
const TENANT_ID = /^[a-z0-9][a-z0-9._-]{0,127}$/i;
const REF_KEYS = Object.freeze(["supabase_service_role", "telegram_bot", "moneytree_link_client_id", "moneytree_link_client_secret", "moneytree_link_refresh_token"]);
const CFO_SECRET_REFS = Object.freeze({
  supabase_service_role: "secret://life-manager/supabase-service-role",
  telegram_bot: "secret://life-manager/telegram-bot",
  moneytree_link_client_id: "secret://moneytree/link-client-id",
  moneytree_link_client_secret: "secret://moneytree/link-client-secret",
  moneytree_link_refresh_token: "secret://moneytree/link-refresh-token",
});

function fail() { throw new Error("cfo_secret_ref_invalid:reference"); }
function validateTenantSecretReferences(tenantId, refs = CFO_SECRET_REFS) {
  if (typeof tenantId !== "string" || !TENANT_ID.test(tenantId) || refs === null || typeof refs !== "object" || Array.isArray(refs)) fail();
  const keys = Object.keys(refs);
  if (keys.length !== REF_KEYS.length || keys.some((key) => !REF_KEYS.includes(key))) fail();
  for (const key of REF_KEYS) if (typeof refs[key] !== "string" || !SECRET_REF.test(refs[key])) fail();
  return Object.freeze({ tenantId, refs: Object.freeze(Object.fromEntries(REF_KEYS.map((key) => [key, refs[key]]))) });
}

async function readCfoSecret(secretProvider, tenantId, ref) {
  if (!secretProvider || typeof secretProvider.get !== "function") throw new Error("cfo_secret_ref_invalid:provider");
  const identity = validateTenantSecretReferences(tenantId);
  if (typeof ref !== "string" || !SECRET_REF.test(ref) || !Object.values(identity.refs).includes(ref)) fail();
  const value = await secretProvider.get(identity.tenantId, ref);
  if (typeof value !== "string" || value.length === 0 || value.length > 8192 || /[\r\n]/.test(value)) fail();
  return value;
}

module.exports = { CFO_SECRET_REFS, REF_KEYS, validateTenantSecretReferences, readCfoSecret };
