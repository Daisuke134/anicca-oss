#!/usr/bin/env python3
"""Provider-neutral facts and decision policy for marketplace Apply owners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


POLICY = {
    "selection": "maximize_truthful_submissions",
    "posting_qualifications": "ranking_signals_not_pre_submission_rejections",
    "ranking": {
        "objective": "maximize_acceptance_probability_times_expected_revenue",
        "ordered_features": [
            "verified_resume_overlap",
            "japan_and_japanese_eligibility",
            "software_ai_automation_overlap",
            "compensation",
            "absence_of_contradictory_requirements",
        ],
        "bands": ["high", "medium", "low"],
        "band_definitions": {
            "high": "strong_verified_overlap_and_no_material_contradiction",
            "medium": "credible_verified_overlap_with_missing_or_weak_evidence",
            "low": "explicit_language_location_domain_or_seniority_contradiction",
        },
        "missing_evidence_disposition": "rank_later_not_reject",
        "material_contradiction_disposition": "skip_no_reasonable_shot",
        "evidence": "cite_posting_text_and_verified_facts",
    },
    "form_answers": "answer_only_from_verified_facts_never_fabricate",
    "person_bound_step": {
        "disposition": "notify_human_with_exact_job_url_action_then_resume",
        "scope": "candidate_local_pending",
        "pass_behavior": "continue_other_candidates_without_waiting",
        "notify_bands": ["high", "medium"],
        "low_fit_disposition": "skip_no_reasonable_shot_without_notification_and_continue",
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
