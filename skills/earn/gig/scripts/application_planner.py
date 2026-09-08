#!/usr/bin/env python3
"""Strict, browser-isolated model-planner contract for Gig applications.

The planner receives an immutable snapshot envelope and returns only feasibility
judgments plus a proposed offer. It has no browser, lease, or mutation interface.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from application_snapshot import stable_request_text, validate_snapshot
from buyer_voice import PERSONA, check_style
from proposal_feedback import fragment as proposal_feedback_fragment


MAX_BATCH = 40
MIN_PROPOSAL_CHARS = 200
MAX_PROPOSAL_CHARS = 3000
_ROOT_FIELDS = frozenset({"decisions"})
_DECISION_FIELDS = frozenset({
    "request_id", "business_class", "reason_codes", "proposal_text", "price_jpy", "deliver_date",
})
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
BUSINESS_CLASSES = frozenset({"submit_required", "hard_prohibited"})
def _work_fit():
    """The refusals are shared with Lancers and CrowdWorks.

    Coconala kept its own eleven classes here. Four of them existed nowhere else -- music, outreach
    and desktop operations, plus explicit_ai_prohibition -- and two that the others had were
    missing: manual_marketplace_operation and original_illustration_or_modelling. So this lane
    would still have bid on 出品代行 and on Live2D character work, which is most of what it was
    applying to in the week before Coconala restricted the account. The first three moved into
    marketplace-core; explicit_ai_prohibition is gone (Dais 2026-09-07).
    """
    import importlib.util as _importlib
    path = Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts" / "work_fit.py"
    spec = _importlib.spec_from_file_location("marketplace_work_fit", path)
    module = _importlib.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARD_PROHIBITION_CLASSES = _work_fit().HARD_PROHIBITION_CLASSES


def common_marketplace_feasibility_policy() -> str:
    """One semantic admission policy for every gig market; providers add no capability gate."""
    return """COMMON MARKETPLACE FEASIBILITY POLICY:
- Apply broadly to every legal opportunity whose required outcome the general agent can truthfully
  complete using computer, browser, coding, research, writing, design, data and other available tools.
- Installed Skills are execution recipes after selection, never an application whitelist. Missing an
  exact Skill, tool history, domain job, testimonial, portfolio item or prior client result is never by
  itself a reason to skip. Compose or build the execution method after contract while making no false
  claim about prior experience.
- Submit is the default for every feasible job, especially high-value work. A proposal is not contract
  acceptance: missing budget/rate, unverified payment, a new client, low hire history, competition,
  high application-token cost, long advertised duration or unclear ordinary implementation details
  are ranking/price/question inputs, never standalone skip reasons. Skip for economics only when the
  official displayed compensation makes every truthful scoped offer clearly negative after cost.
- Skip only when the actual required outcome is illegal/scam, requires unavoidable physical/on-site
  work, mandatory human face/voice/phone/live presence, a legal qualification or immutable identity
  fact that cannot be supplied truthfully, off-platform payment/contact, explicit AI prohibition, or
  scope/deadline/economics the general agent truly cannot complete.
- Preserve scope fidelity: do not make infeasible work appear feasible by silently replacing the
  buyer's required outcome with a smaller or different deliverable. Ask concise pre-contract questions
  when ordinary implementation details are missing.
- Never invent experience or credentials. State verified transferable facts and a concrete plan, but
  missing experience does not convert feasible work into prohibited work."""


def _keys_equal(value: object, expected: frozenset[str], at: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{at}_must_be_object")
        return False
    actual = set(value)
    missing = sorted(expected - actual)
    additional = sorted(actual - expected)
    if missing:
        errors.append(f"{at}_missing:{','.join(missing)}")
    if additional:
        errors.append(f"{at}_additional:{','.join(additional)}")
    return not missing and not additional


def id_mismatch_error(expected_ids: list[str], actual_ids: list[str]) -> str:
    """Name which requests were dropped and which were invented.

    This contract fired ten times on 2026-08-04/05 saying only that the sets differed, with
    batches of ten and no record of the expected set beside the result — a bare assertion
    that something was wrong somewhere. A dropped request and a hallucinated one are
    different bugs with different fixes, so both directions are reported. The lists are
    capped because this ends up in a log line.
    """
    missing = sorted(set(expected_ids) - set(actual_ids))
    unexpected = sorted(set(actual_ids) - set(expected_ids))

    def brief(ids: list[str]) -> str:
        if len(ids) <= 6:
            return ",".join(ids)
        return ",".join(ids[:6]) + f",+{len(ids) - 6} more"

    return (
        "decision_request_ids_not_one_to_one: "
        f"missing=[{brief(missing)}] unexpected=[{brief(unexpected)}]"
    )


def validate_decisions(
    snapshot: object, decisions: object, *, require_complete: bool = True
) -> list[str]:
    """Validate deterministic identity/completeness rules, never semantic business meaning.

    require_complete=False accepts a decisions set that omits some expected ids -- the
    parent already recorded those as not-attempted-this-wake rather than discarding
    every other well-formed decision in the batch. An id outside the snapshot is always
    rejected regardless: it was never judged against real detail data.
    """
    errors = [f"snapshot:{item}" for item in validate_snapshot(snapshot)]
    if errors:
        return errors
    assert isinstance(snapshot, dict)
    if not _keys_equal(decisions, _ROOT_FIELDS, "decisions", errors):
        return errors
    assert isinstance(decisions, dict)
    rows = decisions["decisions"]
    if not isinstance(rows, list) or len(rows) > MAX_BATCH:
        errors.append("decisions_array_invalid")
        return errors
    expected_ids = [item["request_id"] for item in snapshot["request_details"]]
    detail_by_id = {item["request_id"]: item for item in snapshot["request_details"]}
    actual_ids: list[str] = []
    for index, row in enumerate(rows):
        if not _keys_equal(row, _DECISION_FIELDS, f"decision[{index}]", errors):
            continue
        assert isinstance(row, dict)
        request_id = row["request_id"]
        actual_ids.append(str(request_id))
        if not isinstance(request_id, str) or not request_id.isdigit():
            errors.append(f"decision[{index}]_request_id_invalid")
        business_class = row["business_class"]
        if business_class not in BUSINESS_CLASSES:
            errors.append(f"decision[{index}]_business_class_invalid")
            continue
        reasons = row["reason_codes"]
        if not isinstance(reasons, list) or not all(
            isinstance(item, str) and item.strip() for item in reasons
        ) or len(set(reasons)) != len(reasons):
            errors.append(f"decision[{index}]_reason_codes_invalid")
        proposal = row["proposal_text"]
        price = row["price_jpy"]
        date = row["deliver_date"]
        row_detail = detail_by_id.get(request_id) if isinstance(request_id, str) else None
        if business_class == "submit_required":
            if reasons:
                errors.append(f"decision[{index}]_submit_required_reason_codes_must_be_empty")
            if not isinstance(proposal, str) or not proposal.strip():
                errors.append(f"decision[{index}]_submit_required_proposal_required")
            elif not MIN_PROPOSAL_CHARS <= len(proposal) <= MAX_PROPOSAL_CHARS:
                errors.append(f"decision[{index}]_submit_required_proposal_length_invalid")
            # proposal_text is read by the buyer. An internal token in it is the same leak
            # that reached order 91000002, and it is objective, so it fails closed here.
            # Reported rather than raised: this function's whole contract is to name every
            # defect in a batch of up to 40 at once (see id_mismatch_error), and a raise
            # would discard the other 39 diagnoses. A non-empty errors list already blocks
            # the batch, so this is fail-closed either way.
            if isinstance(proposal, str):
                violations = check_style(proposal)
                if violations:
                    errors.append(
                        f"decision[{index}]_buyer_style_violation:{violations[0]}"
                    )
            if isinstance(price, bool) or not isinstance(price, int) or price < 1:
                errors.append(f"decision[{index}]_submit_required_price_required")
            maximum = row_detail.get("budget_max_jpy") if isinstance(row_detail, dict) else None
            if (
                isinstance(price, int)
                and not isinstance(price, bool)
                and isinstance(maximum, int)
                and not isinstance(maximum, bool)
                and price > maximum
            ):
                errors.append(f"decision[{index}]_submit_required_price_exceeds_budget_max")
            if not isinstance(date, str) or not _DATE.fullmatch(date):
                errors.append(f"decision[{index}]_submit_required_date_required")
            else:
                try:
                    dt.date.fromisoformat(date)
                except ValueError:
                    errors.append(f"decision[{index}]_submit_required_date_invalid")
        else:
            if not isinstance(reasons, list) or not reasons:
                errors.append(f"decision[{index}]_hard_prohibited_reason_required")
            else:
                if (
                    not isinstance(reasons[0], str)
                    or reasons[0] not in HARD_PROHIBITION_CLASSES
                ):
                    errors.append(f"decision[{index}]_hard_prohibited_reason_class_invalid")
                if len(reasons) < 2 or not isinstance(reasons[1], str):
                    errors.append(f"decision[{index}]_hard_prohibited_evidence_required")
                else:
                    excerpt = stable_request_text(reasons[1])
                    if not excerpt:
                        errors.append(f"decision[{index}]_hard_prohibited_evidence_required")
                    elif len(excerpt) > MIN_PROPOSAL_CHARS:
                        errors.append(f"decision[{index}]_hard_prohibited_evidence_length_invalid")
                    elif (
                        not isinstance(row_detail, dict)
                        or excerpt not in stable_request_text(row_detail["visible_text"])
                    ):
                        errors.append(
                            f"decision[{index}]_hard_prohibited_evidence_not_in_visible_text"
                        )
            if proposal is not None or price is not None or date is not None:
                errors.append(f"decision[{index}]_hard_prohibited_offer_must_be_null")
    if len(set(actual_ids)) != len(actual_ids):
        errors.append("decision_request_ids_duplicate")
    # The model owns the judgment and semantic priority for each immutable request
    # identity. Row order may differ from snapshot order, but identity coverage may not.
    unexpected = set(actual_ids) - set(expected_ids)
    missing = set(expected_ids) - set(actual_ids)
    if unexpected or (require_complete and missing):
        # Name the difference. This fired ten times on 2026-08-04/05 saying only that the
        # sets differed, with batches of ten and no record of the expected set beside the
        # result — a bare assertion that something was wrong somewhere. A dropped request and
        # an invented one are different bugs with different fixes, so both directions are
        # reported, and the list is capped because this lands in a log line.
        errors.append(id_mismatch_error(expected_ids, actual_ids))
    return errors


def planner_prompt(envelope: dict) -> str:
    """Right-altitude instructions plus small canonical examples; no browser guidance."""
    hard_prohibition_section = "\n".join(
        f"- {reason_code}: {description}"
        for reason_code, description in HARD_PROHIBITION_CLASSES.items()
    )
    common_policy = common_marketplace_feasibility_policy()
    instructions = (
        # proposal_text is read by a human deciding whether to hire us. Until 2026-08-06
        # this prompt said nothing at all about who is writing, and the model answered as
        # the system it could infer from the schema: a status reporter.
        f"{PERSONA}\n\n"
        f"{common_policy}\n\n"
        "以下はあなたが書く proposal_text に適用される人格です。判定（submit_required / hard_prohibited）そのものは\n"
        "下記の業務ルールに従ってください。\n\n"
        "You are the application-intent planner. Read the immutable marketplace snapshot below.\n"
        "For every request, make your own feasibility judgment from its actual visible details. Do not claim\n"
        "that anything was opened, filled, clicked, submitted, verified, or saved. Return JSON that matches\n"
        "the supplied schema exactly, with one decision for every request ID. Order the decision rows for execution: first\n"
        "Every decision object has exactly these six fields: request_id, business_class, reason_codes, proposal_text, price_jpy, deliver_date.\n"
        "submit_required coding, AI, system, automation, and other high-reward work; within that group prefer higher expected\n"
        "reward, then place every other submit_required row. Never omit lower-priority feasible work. Put hard_prohibited rows\n"
        "after submit_required rows. If more than 20 rows are feasible, the first 20 submit_required rows must be the strongest\n"
        "opportunities. This is semantic ordering from the whole listing, never a keyword rule.\n\n"
        "When work is feasible, choose `submit_required`, leave reason_codes empty, and provide a concrete 200〜3000文字 proposal, honest price, and realistic\n"
        "deliver date. 納期には安全マージンとして日数を足さず、正直に実行可能な最短日を選ぶ。 "
        "When it is hard-prohibited, choose `hard_prohibited`, give concise reason codes, and set proposal,\n"
        "price and date to null. Reason from the listing as a whole; do not use a mechanical keyword rule.\n\n"
        "Scope fidelity is a hard gate. Judge the work and participation the buyer actually requires; never make an\n"
        "infeasible request feasible by replacing it with a different remote, digital, advisory, documentary, or reduced\n"
        "deliverable in proposal_text. Before choosing submit_required, identify the buyer's required outcome, required\n"
        "means of performing it, and required place/presence from the whole listing, then verify that the installed seller\n"
        "can complete all three without unverified human labor. Physical work can still be feasible when the buyer explicitly\n"
        "requests only a digital design, drawing, data file, written guide, or remote advice and does not require handling the\n"
        "object or being present. Conversely, a request for a local/resident practitioner, in-person teaching, site visit,\n"
        "physical making, handling, repair, packing, shipping, performance, or other embodied participation is hard-prohibited\n"
        "unless the listing itself explicitly makes that participation optional and accepts a fully remote digital outcome.\n"
        "Do not infer acceptance of a remote substitute merely because one could imagine it. In the final self-check, compare\n"
        "the proposal's promised deliverable sentence with the buyer's required outcome; if they differ in medium, place,\n"
        "participation, or responsibility, return hard_prohibited with the applicable evidence instead.\n\n"
        "Use only seller facts explicitly supplied in the verified-facts fragment below or facts stated by this listing.\n"
        "Never invent or inflate qualifications, personal attributes, employers, career length, numeric achievements,\n"
        "portfolio items, or prior work. After scope feasibility has passed, if no exact-domain result is verified, still submit: use the nearest verified\n"
        "transferable experience and a concrete listing-specific sample/plan, clearly describing unbuilt work as a plan.\n"
        "A connected external account supports only bounded marketplace application, reply, delivery, and official readback. "
        "Do not treat credentials as a reason to accept ongoing account operations, bulk outreach, or browser labor.\n"
        "Never volunteer or promise a live call, video meeting, face appearance, or voice recording in proposal_text. If live\n"
        "consultation is optional or only preferred, offer asynchronous requirements gathering through Coconala messages and\n"
        "documents instead; if it is mandatory, use the applicable hard-prohibition class.\n"
        "Use hard_prohibited only when the whole listing requires one hard-prohibition class below. Distinguish required\n"
        "terms from optional, negated, or quoted text; do not route a decision from an isolated phrase. For a hard_prohibited\n"
        "decision, reason_codes[0] must be the exact class key and reason_codes[1] a bounded exact evidence excerpt from the\n"
        "listing. Copy reason_codes[1] as one contiguous substring exactly as it appears in visible_text: do not shorten,\n"
        "summarize, join separate phrases, correct punctuation, or paraphrase it. Before returning JSON, verify that the exact\n"
        "reason_codes[1] characters occur in that request's visible_text. The only hard-prohibition classes are:\n"
        f"{hard_prohibition_section}\n"
        "A requested skill, work history, domain experience, portfolio, numeric achievement, prior client result, or tool experience is never a personal-attribute-fabrication prohibition. Even when the listing says it is required, limited, preferred, experienced-only, or asks the applicant to describe it, choose submit_required and answer honestly with verified transferable capability plus a concrete sample/plan. Do not claim the missing experience.\n"
        "A numbered application question or field label such as `5 年代` is not evidence that the buyer requires fabrication. Answer it from verified facts when available. Use mandatory_attribute_fabrication only when the listing requires a specific immutable/current attribute value that conflicts with verified facts or cannot be answered truthfully; the evidence excerpt must include that required value or condition, not merely the field name.\n"
        "A generic meeting, discussion, interview, consultation, explanation, coordination, or communication requirement is not mandatory_human_presence unless the listing explicitly requires synchronous phone, live voice, live video, face appearance, performance, or human voice recording. Ambiguous modality remains submit_required and the proposal offers asynchronous Coconala messages/documents.\n"
        "Coconala application lane music boundary: generated or prompted music/audio is prohibited as a required deliverable; do not produce music or audio through prompts. An original song, BGM, performance, mix, master, or edited audio deliverable is music_or_audio_production even when generative tools could create it; music software, music research, or writing about music is not music_or_audio_production and remains submit_required only when no other hard-prohibition class applies.\n"
        "Experience uncertainty, weak portfolio, broad scope, low budget, difficulty, unclear production scope, optional consultation, and unverified achievements remain discretionary weaknesses. Missing Adobe experience alone is not a refusal reason, but required desktop-application operation is hard-prohibited.\n\n"
        "狙う仕事の順序: ①software / landing_page / article / strategyとしてcode・file・documentで非同期完結する仕事 "
        "②その他の非同期成果物。継続性より、現在のskillで高品質な完成成果を自律納品できることを優先する。\n"
        "候補探索・採用代行・大量DM・SNS運用・account warming・反復browser入力を主成果とする仕事は選ばない。\n"
        "低単価の単発でも、非同期で確実に完遂できるなら請けてよい。\n"
        "\n"
        "既知の budget_max_jpy がある案件では、price_jpy は budget_max_jpy を超えない。案件規模と競争状況に合う、\n"
        "買い手が応募額・提案額・報酬額を具体的に指定している場合は、その額をprice_jpyへそのまま入れる。指定がない場合は、安すぎて納品不能にならない範囲で予算上限よりおおむね20%安い競争価格にする。\n"
        "budget_min_jpy と budget_max_jpy が両方 null の見積依頼（予算「応相談」「未定」）は、\n"
        "それだけで hard_prohibited にしない。ソフトウェア・コード・IT・システム・API・AI・自動化系は、scope と市場相場に応じて\n"
        "おおむね ¥50,000〜¥300,000 を目安に price_jpy を決める（小規模は下限寄り、広い・難しい・継続性のある案件は上限寄り）。\n"
        "これは見積もりの目安であり固定の hard ceiling ではない。その他は成果物・作業量・リスク・通常のカテゴリ相場から案件ごとに\n"
        "見積もり、一律の最低価格や固定の価格帯を設けず、実行可能なら submit_required にする。\n"
        "price_jpyが送信価格になる。非公開の競合価格を推測しない。提案は対応可能、金額、\n"
        "納品日、buyer固有の実行方法、契約後の情報整理の順にし、購入前の質問・疑問文を0件にする。deliver_dateも見積の規模に見合った現実的な\n"
        "日数にする（小規模なら短く、大規模なら長く）。\n"
        f"{proposal_feedback_fragment()}"
        "\n"
        "Snapshot envelope (the only marketplace input):\n"
    )
    planner_envelope = dict(envelope)
    planner_details: list[object] = []
    for detail in envelope.get("request_details", []):
        if isinstance(detail, dict):
            planner_detail = dict(detail)
            planner_detail["visible_text"] = stable_request_text(detail.get("visible_text", ""))
            planner_details.append(planner_detail)
        else:
            planner_details.append(detail)
    planner_envelope["request_details"] = planner_details
    return instructions + json.dumps(
        planner_envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--snapshot", required=True, type=Path)
    validate.add_argument("--decisions", required=True, type=Path)
    prompt = subparsers.add_parser("prompt")
    prompt.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if args.command == "prompt":
            snapshot_errors = validate_snapshot(snapshot)
            if snapshot_errors:
                print(json.dumps({"ok": False, "errors": snapshot_errors}, separators=(",", ":")))
                return 2
            print(planner_prompt(snapshot))
            return 0
        decisions = json.loads(args.decisions.read_text(encoding="utf-8"))
        errors = validate_decisions(snapshot, decisions)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, separators=(",", ":")))
            return 2
        print(json.dumps({"ok": True}, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
