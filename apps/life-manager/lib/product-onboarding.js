"use strict";

const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_CATALOG = path.resolve(__dirname, "../config/product-loop-catalog.json");
const HOSTS = new Set(["local", "cloud"]);

function readProductLoopCatalog(catalogFile = DEFAULT_CATALOG) {
  const value = JSON.parse(fs.readFileSync(catalogFile, "utf8"));
  if (value?.schema_version !== 1 || !value.host_requirements
    || !Array.isArray(value.host_requirements.local) || !Array.isArray(value.host_requirements.cloud)
    || !Array.isArray(value.loops) || value.loops.length !== 14) {
    throw new Error("product loop catalog invalid");
  }
  const ids = new Set();
  for (const loop of value.loops) {
    if (!loop || typeof loop.id !== "string" || !/^[a-z][a-z0-9-]*$/u.test(loop.id)
      || ids.has(loop.id) || typeof loop.name !== "string" || !loop.name.trim()
      || typeof loop.description !== "string" || !loop.description.trim()
      || !Array.isArray(loop.requirements) || !loop.hosts || typeof loop.hosts !== "object") {
      throw new Error("product loop catalog invalid");
    }
    ids.add(loop.id);
    for (const host of HOSTS) {
      const target = loop.hosts[host];
      if (!target || !["guided", "setup_required"].includes(target.availability)
        || !(target.command === null || (Array.isArray(target.command)
          && target.command.length > 0 && target.command.every((part) => typeof part === "string" && part)))) {
        throw new Error("product loop host contract invalid");
      }
      if (target.availability === "guided" && target.command === null) {
        throw new Error("guided product loop command unavailable");
      }
    }
  }
  return Object.freeze({
    host_requirements: Object.freeze({
      local: Object.freeze([...value.host_requirements.local]),
      cloud: Object.freeze([...value.host_requirements.cloud]),
    }),
    loops: Object.freeze(value.loops.map((loop) => Object.freeze(loop))),
  });
}

function planProductOnboarding(input = {}, options = {}) {
  const host = String(input.host || "").trim();
  if (!HOSTS.has(host)) throw new Error("onboarding host must be local or cloud");
  if (!Array.isArray(input.selected_loop_ids) || input.selected_loop_ids.length === 0) {
    throw new Error("select at least one product loop");
  }
  if (input.selected_loop_ids.includes("all")) throw new Error("start all is not an onboarding option");
  const selected = new Set(input.selected_loop_ids);
  if (selected.size !== input.selected_loop_ids.length
    || [...selected].some((id) => typeof id !== "string" || !id)) {
    throw new Error("selected product loops invalid");
  }
  const verified = new Set(Array.isArray(input.verified_requirements) ? input.verified_requirements : []);
  const catalog = readProductLoopCatalog(options.catalogFile);
  const byId = new Map(catalog.loops.map((loop) => [loop.id, loop]));
  const unknown = [...selected].filter((id) => !byId.has(id));
  if (unknown.length) throw new Error(`unknown product loop: ${unknown.join(",")}`);
  const loops = input.selected_loop_ids.map((id) => {
    const loop = byId.get(id);
    const target = loop.hosts[host];
    const missing = [...catalog.host_requirements[host], ...loop.requirements]
      .filter((requirement, index, values) => values.indexOf(requirement) === index && !verified.has(requirement));
    if (target.availability === "setup_required" && !missing.includes("host_adapter")) {
      missing.push("host_adapter");
    }
    return Object.freeze({
      id: loop.id,
      name: loop.name,
      description: loop.description,
      host,
      state: missing.length ? "setup_required" : "ready_to_start",
      missing: Object.freeze(missing),
      command: target.command ? Object.freeze([...target.command]) : null,
    });
  });
  return Object.freeze({
    schema_version: "product.onboarding.v1",
    host,
    selected_loop_ids: Object.freeze([...input.selected_loop_ids]),
    loops: Object.freeze(loops),
    starts_automatically: false,
    external_effects: Object.freeze([]),
  });
}

module.exports = { DEFAULT_CATALOG, planProductOnboarding, readProductLoopCatalog };
