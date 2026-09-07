#!/usr/bin/env python3
"""Provider-neutral facts and decision policy for marketplace Apply owners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


POLICY = {
    "selection": "maximize_truthful_submissions",
    "posting_qualifications": "ranking_signals_not_pre_submission_rejections",
    "form_answers": "answer_only_from_verified_facts_never_fabricate",
    "person_bound_step": {
        "disposition": "notify_human_with_exact_job_url_action_then_resume",
        "scope": "candidate_local_pending",
        "pass_behavior": "continue_other_candidates_without_waiting",
    },
    "candidate_failure": "record_and_continue",
}


def build_apply_context(profile_path: Path) -> dict[str, Any]:
    value = json.loads(Path(profile_path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("candidate_profile_invalid")
    candidate = value.get("candidate")
    facts = value.get("facts")
    if not isinstance(candidate, Mapping) or not isinstance(facts, list):
        raise ValueError("candidate_profile_invalid")
    verified_facts = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        identifier = fact.get("id")
        claim = fact.get("claim")
        evidence = fact.get("evidence")
        if not all(isinstance(item, str) and item.strip() for item in (identifier, claim, evidence)):
            continue
        verified_facts.append({
            "id": identifier.strip(),
            "claim": claim.strip(),
            "evidence": evidence.strip(),
        })
    if not verified_facts:
        raise ValueError("candidate_verified_facts_empty")
    return {
        "policy": dict(POLICY),
        "candidate": {
            key: candidate[key]
            for key in (
                "base", "citizenships", "location_preferences", "start_date",
                "travel_authorizations", "work_authorizations",
            )
            if key in candidate
        },
        "verified_facts": verified_facts,
    }


__all__ = ["POLICY", "build_apply_context"]
