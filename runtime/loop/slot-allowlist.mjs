// slot-allowlist.mjs — restrict the wake menu to an explicit slot set (x402-zero-to-one 2026-07-14).
//
// WHY: focused earn tests ("limit the tools to just this skill and watch what the loop does")
// need a deterministic menu. The loop's two menu paths (index.mjs's activeSkillSlots derivation
// AND always-act-router.mjs's assembleAlwaysActMenu) both read the parsed registry object, so the
// single correct choke point is filtering that object once at load — not re-filtering per path.
//
// Contract:
//   applySlotAllowlist(registry, envValue) -> { registry, applied: string[] | null }
//   - envValue empty/blank            -> registry returned UNTOUCHED, applied = null (production default)
//   - envValue "a,b"                  -> every slot NOT in {a,b} is removed, EXCEPT slots with
//                                        alwaysAvailable === true (the same maintainer-designated
//                                        utility carve-out catalog-gate.mjs honors — telemetry/report
//                                        must never be hidden, REQ-201) — those always survive.
//   - names not present in registry   -> tolerated (no throw; they just match nothing)
//   - registry malformed / no slots   -> returned untouched (fail-open to the existing loop fallback)
// Pure: never mutates the input object.
export function applySlotAllowlist(registry, envValue, { preserveAlwaysAvailable = true } = {}) {
  const raw = String(envValue || '').trim();
  if (!raw) return { registry, applied: null };
  if (!registry || typeof registry !== 'object' || !registry.slots || typeof registry.slots !== 'object') {
    return { registry, applied: null };
  }
  const allow = new Set(raw.split(',').map((s) => s.trim()).filter(Boolean));
  if (!allow.size) return { registry, applied: null };
  const slots = {};
  for (const [name, def] of Object.entries(registry.slots)) {
    if (allow.has(name) || (preserveAlwaysAvailable && def && def.alwaysAvailable === true)) slots[name] = def;
  }
  return { registry: { ...registry, slots }, applied: [...allow] };
}
