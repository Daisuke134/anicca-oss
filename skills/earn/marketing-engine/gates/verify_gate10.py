#!/usr/bin/env python3
"""Fail-closed verifier for Gate 10 product/account routing and safe plans."""

from __future__ import annotations

import argparse
import json
import pathlib

from product_router import load_registry, require
from variation import eligible_hooks, read_jsonl, stable_id


PLAN_FIELDS = {
    "schema_version", "experiment_id", "creative_id", "product_id", "account_id",
    "hook_id", "hook_text", "tactic_id", "renderer_id", "language", "cta",
    "destination_url", "primary_metric", "attribution_method", "idempotency_key",
    "status", "planned_at",
}


def load_json(path: pathlib.Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"expected object: {path}")
    return value


def verify_gate10(engine: pathlib.Path) -> dict:
    engine = pathlib.Path(engine)
    registry = load_registry(engine)
    require(set(registry.products) == {"anicca-ios", "honne-ai", "ebook-en", "ebook-ja"},
            "initial product registry differs")
    require(len(registry.accounts) == 9, "initial account registry differs")
    hooks_path = engine / "intel" / "hook-library.jsonl"
    hooks = {row["id"]: row for row in read_jsonl(hooks_path)}
    tactics = {row["id"] for row in read_jsonl(engine / "intel" / "playbook.jsonl")}

    plans = read_jsonl(engine / "evidence" / "creative" / "gate10" / "plans.jsonl")
    require(len(plans) == 2, "Gate 10 safe-plan evidence must contain exactly EN and JA")
    seen_experiments: set[str] = set()
    seen_creatives: set[str] = set()
    for row in plans:
        require(set(row) == PLAN_FIELDS, "safe plan fields differ")
        require(row["schema_version"] == "marketing.experiment-plan.v1" and
                row["status"] == "planned", "safe plan schema/status differs")
        require(row["experiment_id"] not in seen_experiments and
                row["creative_id"] not in seen_creatives, "duplicate safe plan identity")
        seen_experiments.add(row["experiment_id"])
        seen_creatives.add(row["creative_id"])
        require(row["product_id"] in registry.products and row["account_id"] in registry.accounts,
                "safe plan product/account unknown")
        product = registry.products[row["product_id"]]
        account = registry.accounts[row["account_id"]]
        require(account["product_id"] == row["product_id"], "safe plan account product mismatch")
        require(row["renderer_id"] in account["allowed_renderer_ids"],
                "safe plan renderer not allowed")
        require(row["hook_id"] in hooks and hooks[row["hook_id"]]["product_ids"] == [row["product_id"]],
                "safe plan hook product mismatch")
        require(row["hook_text"] == hooks[row["hook_id"]]["text"], "safe plan hook text differs")
        require(row["tactic_id"] in tactics, "safe plan tactic unknown")
        require(row["cta"] == product["cta"] and
                row["destination_url"] == product["destination_url"] and
                row["primary_metric"] == product["primary_metric"] and
                row["attribution_method"] == product["attribution_method"],
                "safe plan product economics differ")
        identity = [row["product_id"], row["account_id"], row["hook_id"],
                    row["tactic_id"], row["renderer_id"], row["idempotency_key"]]
        require(row["experiment_id"] == stable_id("experiment", identity) and
                row["creative_id"] == stable_id("creative", identity + ["creative"]),
                "safe plan stable identity differs")

    plans_by_product = {
        product_id: sum(row["product_id"] == product_id for row in plans)
        for product_id in ("ebook-en", "ebook-ja")
    }
    require(plans_by_product == {"ebook-en": 1, "ebook-ja": 1},
            "safe plans are not one per ebook language")

    app_accounts = {"anicca-ios": "tiktok.anicca_jp", "honne-ai": "tiktok.honnevideo"}
    app_candidates = {
        product: len(eligible_hooks(registry, hooks_path, product, account))
        for product, account in app_accounts.items()
    }
    require(app_candidates == {"anicca-ios": 0, "honne-ai": 0},
            "app products unexpectedly borrow another product's hooks")

    canonical_sources = [engine / "gates" / "variation.py",
                         engine / "gates" / "product_router.py", engine / "bin" / "lm"]
    forbidden = ["fixed" + "-strings-", "hookPool" + "-ja.txt", ".open" + "claw"]
    legacy_refs = sum(term in path.read_text(encoding="utf-8")
                      for path in canonical_sources for term in forbidden)
    require(legacy_refs == 0, "canonical router retains a legacy hook/runtime dependency")

    quarantine = load_json(engine / "evidence" / "schedulers" /
                           "2026-08-01-post-quarantine-v2.json")
    enabled_publishers = quarantine["summary"]["enabled_publishers"]
    require(enabled_publishers == 0, "legacy publishers were re-enabled")

    return {
        "schema_version": "marketing.gate10-verification.v1",
        "passed": True,
        "counts": {
            "products": len(registry.products), "accounts": len(registry.accounts),
            "renderers": len(registry.renderers), "safe_plans": len(plans),
        },
        "accounts_by_product": {
            product: sum(row["product_id"] == product for row in registry.accounts.values())
            for product in sorted(registry.products)
        },
        "plans_by_product": plans_by_product,
        "safe_experiment_ids": sorted(seen_experiments),
        "app_candidate_counts": app_candidates,
        "legacy_production_references": legacy_refs,
        "enabled_legacy_publishers": enabled_publishers,
        "publication_effects": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        result = verify_gate10(args.engine)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
