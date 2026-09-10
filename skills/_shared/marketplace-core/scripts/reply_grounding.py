#!/usr/bin/env python3
"""Verified, provider-neutral candidate facts for marketplace Reply."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping


PRIVATE_IDENTITY_FIELDS = (
    "full_name", "name", "name_ja", "preferred_name", "name_kana",
    "application_email", "email", "phone", "mailing_address", "mailing_address_ja",
)


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


def _private_identity_values(candidate: Mapping[str, Any]) -> list[str]:
    values = set()
    for field in PRIVATE_IDENTITY_FIELDS:
        value = candidate.get(field)
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    for field in ("name_ja_parts", "name_romaji_parts"):
        parts = candidate.get(field)
        if isinstance(parts, Mapping):
            values.update(
                value.strip() for value in parts.values()
                if isinstance(value, str) and value.strip()
            )
    return sorted(values, key=lambda value: (value.casefold(), value))


def _contains_private_identity(text: str, values: list[str]) -> bool:
    folded = text.casefold()
    return any(value.casefold() in folded for value in values)


def build_reply_grounding(
    *, candidate_profile_path: Path,
    provider_profile_path: Path | None = None,
    provider_profile: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    private = _object(candidate_profile_path)
    candidate_raw = private.get("candidate")
    facts_raw = private.get("facts")
    if not isinstance(candidate_raw, Mapping) or not isinstance(facts_raw, list):
        raise ValueError("reply_grounding_invalid")
    private_identity_values = _private_identity_values(candidate_raw)

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
        if (all(isinstance(value, str) and value.strip()
                for value in (identifier, claim, evidence))
                and not _contains_private_identity(claim, private_identity_values)):
            verified_facts.append({"id": identifier.strip(), "claim": claim.strip()})

    if provider_profile is not None and not isinstance(provider_profile, Mapping):
        raise ValueError("reply_grounding_invalid")
    provider_raw = dict(provider_profile) if provider_profile is not None else _object(provider_profile_path)
    provider_facts = {
        key: provider_raw[key]
        for key in ("display_name", "hours_limit", "status", "occupation", "skills")
        if key in provider_raw
    }
    prompt_facts = list(verified_facts)
    labels = {
        "gender": "性別", "age_band": "年代", "base": "拠点", "nationality": "国籍",
    }
    for key, label in labels.items():
        if key in candidate:
            prompt_facts.append({"id": f"candidate_{key}", "claim": f"{label}: {candidate[key]}"})
    public_labels = {
        "hours_limit": "週あたりの稼働時間",
        "status": "現在の稼働状態",
        "occupation": "職種",
    }
    for key, label in public_labels.items():
        value = provider_facts.get(key)
        if isinstance(value, str) and value.strip():
            suffix = "時間" if key == "hours_limit" else ""
            prompt_facts.append({
                "id": f"provider_{key}", "claim": f"{label}: {value.strip()}{suffix}",
            })
    for index, skill in enumerate(provider_facts.get("skills", [])):
        if not isinstance(skill, Mapping) or not isinstance(skill.get("name"), str):
            continue
        name = skill["name"].strip()
        if not name:
            continue
        identifier = "".join(character.lower() for character in name if character.isalnum())
        years = skill.get("years")
        note = skill.get("note")
        parts = [name]
        if isinstance(years, (int, float)):
            parts.append(f"{years:g}年")
        if isinstance(note, str) and note.strip():
            parts.append(note.strip())
        prompt_facts.append({
            "id": f"provider_skill_{identifier or index}", "claim": ": ".join(parts),
        })
    return {
        "candidate": candidate,
        "verified_facts": verified_facts,
        "provider_public_facts": provider_facts,
        "private_identity_values": private_identity_values,
        "missing_candidate_fields": ["gender"] if "gender" not in candidate else [],
        "prompt_facts": prompt_facts,
    }


__all__ = ["build_reply_grounding"]
