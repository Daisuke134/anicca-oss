#!/usr/bin/env python3
"""Verified, provider-neutral candidate facts for marketplace Reply."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping


def _object(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("reply_grounding_invalid")
    return value


def _age_band(value: Any, today: date) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        born = date.fromisoformat(value)
    except ValueError:
        return None
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return f"{age // 10 * 10}代" if 0 <= age < 100 else None


def build_reply_grounding(
    *, candidate_profile_path: Path,
    provider_profile_path: Path | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    private = _object(candidate_profile_path)
    candidate_raw = private.get("candidate")
    facts_raw = private.get("facts")
    if not isinstance(candidate_raw, Mapping) or not isinstance(facts_raw, list):
        raise ValueError("reply_grounding_invalid")

    candidate: dict[str, Any] = {}
    age_band = _age_band(candidate_raw.get("date_of_birth"), today or date.today())
    if age_band:
        candidate["age_band"] = age_band
    for key in ("gender", "base", "nationality"):
        value = candidate_raw.get(key)
        if isinstance(value, str) and value.strip():
            candidate[key] = value.strip()

    verified_facts = []
    for raw in facts_raw:
        if not isinstance(raw, Mapping):
            continue
        identifier, claim, evidence = raw.get("id"), raw.get("claim"), raw.get("evidence")
        if all(isinstance(value, str) and value.strip()
               for value in (identifier, claim, evidence)):
            verified_facts.append({"id": identifier.strip(), "claim": claim.strip()})

    provider_raw = _object(provider_profile_path)
    provider_facts = {
        key: provider_raw[key]
        for key in ("hours_limit", "status", "occupation", "skills")
        if key in provider_raw
    }
    return {
        "candidate": candidate,
        "verified_facts": verified_facts,
        "provider_public_facts": provider_facts,
        "missing_candidate_fields": ["gender"] if "gender" not in candidate else [],
    }


__all__ = ["build_reply_grounding"]
