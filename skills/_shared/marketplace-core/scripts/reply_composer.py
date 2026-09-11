#!/usr/bin/env python3
"""Website-neutral natural-language composer for marketplace Reply."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_RUNNER = REPO_ROOT / "runtime/agent-runner/agent_runner.py"
SCHEMA = REPO_ROOT / "skills/gig-work/schemas/reply_composition.schema.json"


class ReplyFactsRequired(RuntimeError):
    def __init__(self, remaining_work: Sequence[str]):
        self.remaining_work = [str(item).strip() for item in remaining_work if str(item).strip()]
        super().__init__("reply_facts_required")


def compose(context: Mapping[str, Any], *, state_root: Path, task_label: str) -> str | None:
    prompt = """Marketplace上の購入前・契約前会話に、seller本人として自然な日本語で対応する。
判断は会話全体、案件、検証済みgroundingから行う。
- next_actionは、相手が回答・確認・必要情報を求めていればreply、相手待ちや返信不要ならwait、辞退・不採用・終了ならstop。
- 明示された質問には順番を保って漏れなく直接答える。
- candidate、verified_facts、provider_public_factsは同じsellerの確認済み情報。必要な情報だけ自然な一人称で使い、内部field名や情報源には触れない。
- 通常のプロフィール質問は、検証済み事実、安全な導出、後から訂正可能な合理的暫定値の順で回答を完成させ、human確認を作らない。
- 金額、資格、法的表明、本人確認など、誤りを後から戻せない主張は推測せずuncertaintyへ入れる。
- 外部連絡、契約前の作業開始、虚偽の実績を約束しない。相手が外部連絡を求めてもprovider規則に従う。
- reply_bodyは1000文字以内。wait/stopではnull。内部事情や「情報がないので回答できない」とbuyerへ書かない。
- action_contract.kindがrequired_form_fieldなら、これは返信要否の判断ではなく契約済み作業の必須入力である。
  next_actionはreplyにし、reply_bodyにはその設問への回答だけを書く。allowed_choicesがある場合は、
  根拠と案件文脈から最も適切な選択肢を選び、その文字列と完全一致する値だけを返す。
  課題内の人物名、状況、表現など後から訂正可能な不足は、sourceと通常の業務慣行から合理的に
  仮定して回答を完成させ、uncertaintyで停止しない。資格、本人確認、金額、法的表明など誤りを
  後から戻せない本人事実だけは推測しない。
CONTEXT:\n""" + json.dumps(dict(context), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reply-compose-", dir=state_root) as temporary:
        evidence = Path(temporary) / "evidence"
        done = subprocess.run(
            [sys.executable, str(AGENT_RUNNER), "--task-class", "composition-agent",
             "--prompt-stdin", "--schema", str(SCHEMA), "--evidence-dir", str(evidence),
             "--task-label", task_label, "--loop", task_label, "--workdir", str(REPO_ROOT)],
            input=prompt, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=90, check=False,
        )
        if done.returncode != 0:
            raise RuntimeError("reply_composer_failed")
        try:
            summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
            result_path = Path(str(summary["result_path"])).resolve()
            result_path.relative_to(evidence.resolve())
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, KeyError, TypeError, ValueError):
            raise RuntimeError("reply_composer_failed") from None
    if not isinstance(result, Mapping) or result.get("next_action") not in {"reply", "wait", "stop"}:
        raise RuntimeError("reply_contract_invalid")
    uncertainty = result.get("uncertainty")
    if not isinstance(uncertainty, list):
        raise RuntimeError("reply_contract_invalid")
    if uncertainty:
        raise ReplyFactsRequired(uncertainty)
    body = result.get("reply_body")
    if result["next_action"] != "reply":
        if body is not None:
            raise RuntimeError("reply_contract_invalid")
        return None
    if not isinstance(body, str) or not body.strip() or len(body.strip()) > 1000:
        raise RuntimeError("reply_contract_invalid")
    return body.strip()
