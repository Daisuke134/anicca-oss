#!/usr/bin/env python3
"""Ephemeral GPT/Claude composition adapter for a send-ready Gig reply."""

from __future__ import annotations

import json
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _load_sibling(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_context_packet():
    return _load_sibling("gig_context_packet")


# This module is loaded by path with scripts/ absent from sys.path, so a bare import
# of a sibling is not available here.
PERSONA = _load_sibling("buyer_voice").PERSONA


def conversation_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    """Validate the live conversation and project it to side/body pairs."""
    conversation = context.get("conversation")
    if not isinstance(conversation, list) or not conversation:
        raise ValueError("conversation must be non-empty")
    rows: list[dict[str, str]] = []
    for row in conversation:
        if not isinstance(row, dict) or (row.get("role") or row.get("side")) not in {"buyer", "seller"}:
            raise ValueError("invalid conversation row")
        body = row.get("body")
        if type(body) is not str:
            raise ValueError("invalid conversation body")
        rows.append({"side": str(row.get("role") or row.get("side")), "body": body})
    return rows


def nothing_to_say_reason(context: dict[str, Any]) -> str | None:
    """Return why no reply is owed on this thread, or ``None`` when one is.

    Seller-last is a FINDING, not a fault: we already answered and the buyer has
    not spoken since.  It used to leave here as ``ValueError``, which made the
    reply lane park the action and retry it every five minutes forever (412
    consecutive runs on thread 93000007, still failing at the time of writing).
    The caller closes the queue entry on this reason instead, and the next buyer
    message re-opens the thread through a fresh action.

    Reads only ``side``; the buyer's text never leaves the transient context.
    """
    rows = conversation_rows(context)
    if rows[-1]["side"] != "buyer":
        return "seller_last"
    return None


_PRICE_QUESTION = re.compile(r"価格|値段|金額|料金|見積(?:り|もり)?|提案額|いくら")
_PRICE_INTENT = re.compile(
    r"[?？]|いくら|教えて|ください|知りたい|伺いたい|どうな|確認|お願い|"
    r"変わ|変更|同じ|据え置|ですか"
)
_CHANGE_QUESTION = re.compile(r"変更|変わ|同じ|据え置")
_DAILY_CAPACITY_QUESTION = re.compile(
    r"(?:1日|一日)(?!後)(?:あたり|で)?[^。！？!?\n]{0,24}(?:何件|件数|どのくらい|対応可能な数)"
)
_DAILY_CAPACITY_ANSWER = re.compile(r"(?:1日|一日)(?!後)(?:あたり|で)?[^。！？!?\n]{0,24}[0-9０-９][0-9０-９,，]*\s*件")
_DURATION_QUESTION = re.compile(
    r"何日|どのくらい(?:の)?日数|(?:完了|納品)(?:まで)?[^。！？!?\n]{0,24}(?:日数|期間|どのくらい|どれくらい|かか)"
)
_DURATION_ANSWER = re.compile(
    r"(?<![月0-9０-９])(?:約\s*)?[0-9０-９][0-9０-９,，]*\s*日(?:ほど|程度|間|以内|前後|で(?!す)|かか)"
)
_CHANGE_DISPOSITION = re.compile(
    r"変更(?:は)?(?:ありません|ございません|ない|なく|なし)|"
    r"変わ(?:りません|らない)|据え置き|同じ(?:金額|価格|見積)|"
    r"変更(?:が)?あります|変更(?:に|と)?なります|変わります"
)
_CHANGE_NONANSWER = re.compile(
    r"言えません|限りません|分かりません|わかりません|不明|未定|"
    r"変更(?:は)?(?:ありません|ない)(?:です)?か|変わりませんか|"
    r"よね|可能性|と思|同じ[^。！？!?\n]*ですか|でしょうか|[?？]"
)
_CHANGE_AFFIRMATIVE = re.compile(
    r"変更(?:が)?あります|変更(?:に|と)?なります|変わります"
)
_VERIFIED_PROMO_URL = "https://www.tiktok.com/@anicca_buddha"
_VERIFIED_PROMO_URL_TOKEN = re.compile(
    re.escape(_VERIFIED_PROMO_URL) + r"(?=$|[\s。、！？!，,）)」』】])"
)
_EXTERNAL_CONTACT = re.compile(
    r"https?://|www\.|(?<![\w.+-])[\w.+-]+[@＠][A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"(?<![A-Za-z0-9_@＠.-])[@＠](?![0-9０-９]+(?:[,，][0-9０-９]{3})*(?:円|えん))[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])|"
    r"(?<![\w@＠.-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|net|org|jp|co|io|ai|dev|app|example)(?=$|[^\w.-])",
    re.IGNORECASE,
)
_PHONE_OR_SNS_CONTACT = re.compile(
    r"(?:電話(?:番号)?|TEL|携帯)[^。！？!?\n]{0,12}\d{2,4}[-ー−]\d{2,4}[-ー−]\d{3,4}|"
    r"(?:LINE|ライン|SNS|Instagram|インスタ|X\s*ID)[\s　]*(?:ID|ＩＤ)?[\s　]*(?:は|:|：)?[\s　]*[A-Za-z0-9_@＠.-]+",
    re.IGNORECASE,
)
_YEN_ASSERTION = re.compile(r"(?:[0-9０-９][0-9０-９,，]*|[0-9０-９]+\s*万)\s*円")


def _yen_price_pattern(price: int) -> re.Pattern[str]:
    variants = [re.escape(str(price)), re.escape(f"{price:,}")]
    if price % 10000 == 0:
        variants.append(rf"{price // 10000}\s*万")
    return re.compile(rf"(?<![\d,])(?:{'|'.join(variants)})\s*円(?!\d)")


def _require_verified_application_terms(context: dict[str, Any], body: str) -> None:
    contact_body = _VERIFIED_PROMO_URL_TOKEN.sub("", body)
    if _EXTERNAL_CONTACT.search(contact_body) or _PHONE_OR_SNS_CONTACT.search(contact_body):
        raise ValueError("reply contains external contact")
    rows = conversation_rows(context)
    if rows[-1]["side"] != "buyer":
        return
    latest = rows[-1]["body"]
    if _DAILY_CAPACITY_QUESTION.search(latest) and not _DAILY_CAPACITY_ANSWER.search(body):
        raise ValueError("reply omitted daily capacity")
    if _DURATION_QUESTION.search(latest) and not _DURATION_ANSWER.search(body):
        raise ValueError("reply omitted completion duration")
    application = context.get("verified_application")
    if not isinstance(application, dict):
        if (
            _PRICE_QUESTION.search(latest)
            and _PRICE_INTENT.search(latest)
            and (_YEN_ASSERTION.search(body) or _CHANGE_DISPOSITION.search(body))
        ):
            raise ValueError("reply asserted unverified price")
        return
    if not _PRICE_QUESTION.search(latest) or not _PRICE_INTENT.search(latest):
        return
    price = application.get("price_jpy")
    if type(price) is not int or price <= 0:
        return
    price_pattern = _yen_price_pattern(price)
    if not price_pattern.search(body):
        raise ValueError("reply omitted verified application price")
    if _CHANGE_QUESTION.search(latest):
        clauses = re.split(r"[。！？!?\n]+", body)
        if not any(
            price_pattern.search(clause)
            and _CHANGE_DISPOSITION.search(clause)
            and not _CHANGE_NONANSWER.search(clause)
            and not _CHANGE_AFFIRMATIVE.search(clause)
            for clause in clauses
        ):
            raise ValueError("reply omitted verified application change status")


def composition_prompt(context: dict[str, Any]) -> str:
    rows = conversation_rows(context)
    reason = nothing_to_say_reason({"conversation": rows})
    if reason is not None:
        # Unreachable from the lane: execute_reply consults nothing_to_say_reason
        # first and closes the entry.  Kept as the prompt's own precondition so a
        # future caller cannot build a reply prompt out of a thread we own.
        raise ValueError("reply composition requires buyer-last conversation")
    packet_module = _load_context_packet()
    packet = packet_module.reply_composition_packet({
        "conversation": rows,
        "verified_research": context.get("verified_research"),
        "counterparty_user_id": context.get("counterparty_user_id"),
        "verified_application": context.get("verified_application"),
    })
    packet_text = packet_module.serialize_packet(packet).decode("utf-8")
    return f"""{PERSONA}

以下の会話へ、今すぐ送信できる日本語返信を1件作成してください。相手はまだ購入前の見込みのお客様です。

必須:
- 通常返信の局所優先: 冒頭は最新の買い手発言の質問・依頼への直接回答にする。受領表現が必要なら、その回答に統合するか後置する。
- 最新の買い手発言に含まれる明示的な質問・依頼はすべて回答し、黙って省略しない。verified_applicationがある場合、応募・見積りに関する質問はその検証済みの価格・納期・提案本文だけを使い、変更の有無も明示する。
- 買い手が1日あたりの件数や完了までの日数を尋ねた場合は、それぞれを数字で明示する。日付だけで日数への回答を代用しない。
- 根拠がなければ価格・納期・変更有無を作らない。検証済み文脈だけでは答えられない明示的な質問は、答えられない事情を認めて、正確な確認質問を1つまで含める。
- 検証済みの出品者保有SNS事実: プラットフォームはTikTok。本人のprivate account（個人アカウント）で、以前アプリ紹介に使用し、現在は投稿していないため今回のPRに利用可能。公開リンクは https://www.tiktok.com/@anicca_buddha。フォロワー数は確認時点では3,281人と表現する。
- SNSや紹介先について尋ねられた場合は上記の事実から直接答え、「どのSNSか」などと買い手へ聞き返さない。
- 関連する依頼には、本人の個人アカウントであること、現在投稿していないこと、「今回のご依頼に利用できます」という可否、上記のexact公開リンクを明言する。
- フォロワー数は現在の人数・最新値・リアルタイムの数としては表現しない。
- 会話内の検証済み固有情報を最低1点使う。ただし、買い手が感謝・検討だけの場合は単純な受領だけで完結してよく、固有情報は不要とする。
- 最新の買い手発言へ回答するだけで完結する場合は、相手へ次の行動を求めず、質問もしない。この規則はPERSONAの一般的な案内より優先する。
- 動画そのものの編集、字幕・テロップ挿入、映像加工、書き出し、完成動画制作は対応できません。動画の企画・台本・文章など編集不要の業務と動画以外（例: WordPress）はこのhard declineに含めず、通常判定する。
- 動画制作を尋ねられた場合、過去のseller発言に誤った対応可能claimがある場合だけ事実を訂正する。過去のseller claimがない場合は、訂正と称さず丁寧に辞退する。いずれも購入案内・見積り・代替案を出さず、質問もしない。過去のseller発言の対応可能という記載を根拠にしない。
- 買い手が可否を尋ねた場合だけ、「可能です」「対応できません」のように可否を断定する。価格・納期の質問、感謝、確認には不自然な可否表現を付けない。
- 最新の買い手発言が求めていない購入案内・見積り・納期を自発的に追加しない。直近のseller発言に購入案内・購入起点の納期・質問があっても、最新の買い手が求めていなければ反復しない。
- 未依頼のCTA・見積り・フォローアップを追加しない。
- 買い手が価格または納期を尋ねた場合だけ、会話内の検証済み条件の範囲でその質問に答える。購入を催促しない。
- 作業内容は具体的に述べてよいが、成果物・提案書・サンプル・構成案を購入前に送ると約束しない。
- 購入前の通常会話では、作業の実施・着手・納品を将来形で確約しない。「制作します・仕上げます・お渡しします・提出します」ではなく、可否や含有範囲は現在の能力・条件として「対応可能です・サービス内容に含まれます」と表現する。
- 買い手が感謝・検討だけなら受領だけで完結し、以前の提案を再約束しない。
- 本当に必要な未確定事項だけ質問を1つまで含める。不要なら質問しない。
- 「後で回答します」だけ、下書き、承認依頼、送信拒否は禁止。
- context_packet.fieldsにverified_researchがなければ外部サイトを確認したと主張しない。
- 検証済みの公開リンク https://www.tiktok.com/@anicca_buddha だけは必要な回答に含める。他の外部URL、メールアドレス、電話番号、別のSNS IDなど外部連絡先は含めず、相手の文面にあっても返信へ転記しない。
- 1000文字以内。JSON schemaどおりreply_bodyだけを返す。

context_packet={packet_text}
"""


class RunnerComposer:
    # Carried on the composer object rather than imported by the executor: every
    # module here is loaded through importlib file specs, so the same source file
    # becomes a DIFFERENT module object per loader and a cross-module import
    # would silently compare two unrelated functions.  One definition, reached
    # through the one object both sides already share.
    nothing_to_say_reason = staticmethod(nothing_to_say_reason)

    def __init__(
        self,
        *,
        runner: Path,
        schema: Path,
        workdir: Path,
        temp_root: Path | None = None,
        timeout_seconds: int = 900,
        prompt_builder: Any = None,
        task_label: str = "gig-reply-compose",
    ):
        self.runner = Path(runner)
        self.schema = Path(schema)
        self.workdir = Path(workdir)
        self.temp_root = Path(temp_root) if temp_root is not None else None
        self.timeout_seconds = timeout_seconds
        # A follow-up differs from a first reply in one thing only: what the model is
        # asked for. Everything below -- the sandboxed temp dir, the evidence contract,
        # the length bound, the runner's own diagnosis on failure -- is the same problem
        # and stays one implementation.
        self.prompt_builder = prompt_builder or composition_prompt
        self.task_label = task_label

    def __call__(self, context: dict[str, Any]) -> str:
        prompt = self.prompt_builder(context)
        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".gig-reply-compose-",
            dir=self.temp_root,
        ) as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            evidence = root / "evidence"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.runner),
                    "--task-class", "composition-agent",
                    "--prompt-stdin",
                    "--schema", str(self.schema),
                    "--evidence-dir", str(evidence),
                    "--task-label", self.task_label,
                    "--loop", "gig",
                    "--workdir", str(self.workdir),
                ],
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            prompt = ""
            if completed.returncode != 0:
                # The runner's own diagnosis, not just its exit code. Without this a
                # composer that fails only in production is unfixable: it says rc=1
                # and the reason dies with the temporary directory. Bounded, and the
                # runner writes diagnostics rather than customer prose.
                detail = (completed.stderr or completed.stdout or "").strip()[-400:]
                raise RuntimeError(
                    f"reply composer failed with rc={completed.returncode}: {detail}"
                )
            try:
                summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
                result_path = Path(str(summary["result_path"])).resolve()
                result_path.relative_to(evidence.resolve())
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("reply composer produced invalid evidence") from error
            body = result.get("reply_body") if isinstance(result, dict) else None
            if type(body) is not str or not body.strip() or len(body.strip()) > 1000:
                raise ValueError("invalid reply body")
            body = body.strip()
            _require_verified_application_terms(context, body)
            return body
