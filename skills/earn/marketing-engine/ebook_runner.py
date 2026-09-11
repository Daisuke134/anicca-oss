#!/usr/bin/env python3
"""Shared, receipt-first Ebook Seller runner (shadow stage)."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "brain"))
sys.path.insert(0, str(HERE / "gates"))
sys.path.insert(0, str(HERE / "render_eval"))
sys.path.insert(0, str(HERE / "measure"))
sys.path.insert(0, str(HERE.parents[2]))
from script_ledger import ScriptLedger, preflight  # noqa: E402
from ebook_packs import load_ebook_packs  # noqa: E402
from watercolor_candidate import render as render_watercolor  # noqa: E402
from heygen_candidate import render as render_heygen  # noqa: E402
from ebook_asset_pack import (  # noqa: E402
    WATERCOLOR_CLIP_NAMES,
    default_asset_root,
    default_pack_root,
    provision_default_pack,
)
from skills._shared.telegram import TelegramClient  # noqa: E402
from attribution import campaign_token  # noqa: E402


def watercolor_clip_paths(asset_root: Path) -> list[Path]:
    clips = Path(asset_root) / "watercolor-monk/clips"
    return [clips / name for name in WATERCOLOR_CLIP_NAMES]


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def claim_receipt(path: Path, value: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def run(*, engine: Path, product: str, slot_at: str, script_id: str, ledger_path: Path,
        state_root: Path, render_output: Path | None = None, telegram_preview: bool = False,
        asset_root: Path | None = None) -> dict:
    packs = load_ebook_packs(engine)
    pack = next((item for item in packs.values() if item["product_id"] == product), None)
    require(pack is not None, "ebook product pack missing")
    timestamp = dt.datetime.fromisoformat(slot_at.replace("Z", "+00:00"))
    require(timestamp.tzinfo is not None, "slot timestamp timezone required")
    require(timestamp.astimezone(dt.timezone(dt.timedelta(hours=9))).strftime("%H:%M") in pack["slots_jst"], "slot not allowed for product")
    script = ScriptLedger(ledger_path).get(script_id)
    preflight(script)
    require(script["product_id"] == product and script["account_id"] == f"product:{product}", "script product scope mismatch")
    require(script["renderer_id"] == pack["renderer_id"], "script renderer does not match ebook pack")
    key = hashlib.sha256(f"{product}|{slot_at}".encode()).hexdigest()[:24]
    receipt = {"schema_version": "marketing.ebook-run.v1", "run_id": f"ebook-run.{key}",
               "product_id": product, "slot_at": slot_at, "script_id": script_id,
               "creative_id": script["creative_id"], "renderer_id": script["renderer_id"],
               "state": "script_preflighted", "external_effects": [],
               "accounts": pack["accounts"],
               "setup_required_accounts": pack.get("setup_required_accounts", []),
               "recorded_at": slot_at}
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / f"{receipt['run_id']}.json"
    if not claim_receipt(path, receipt):
        existing = json.loads(path.read_text(encoding="utf-8"))
        require(all(existing.get(key) == receipt[key] for key in receipt if key not in {"state", "external_effects"}), "conflicting run replay")
        receipt = existing
        if existing.get("state") in {"render_reconciliation_required", "telegram_delivery_unknown"}:
            return existing
    if render_output is not None:
        if receipt.get("state") not in {"rendered", "telegram_delivery_pending"}:
            if product == "ebook-ja":
                resolved_assets = default_pack_root(asset_root or default_asset_root())
                provisioned = provision_default_pack(asset_root=resolved_assets)
                if provisioned.get("state") == "setup_required":
                    receipt.update({"state": "setup_required", "setup": provisioned})
                    write_receipt(path, receipt)
                    return receipt
                clips = watercolor_clip_paths(resolved_assets)
                rendered = render_watercolor(script=script["body"], output=render_output, clips=clips)
            else:
                rendered = render_heygen(script=script["body"], output=render_output,
                                         intent_path=state_root / f"{receipt['run_id']}.heygen-effect.json")
            if rendered.get("state") == "setup_required":
                receipt.update({"state": "setup_required", "setup": rendered})
                write_receipt(path, receipt)
                return receipt
            if rendered.get("state") == "reconciliation_required":
                receipt.update({"state": "render_reconciliation_required", "render": rendered})
                write_receipt(path, receipt)
                return receipt
            receipt.update({"state": "rendered", "render": rendered,
                            "external_effects": rendered.get("external_effects", [])})
            write_receipt(path, receipt)
        if telegram_preview:
            rendered = receipt["render"]
            delivery_path = state_root / f"{receipt['run_id']}.telegram-effect.json"
            delivery_key = hashlib.sha256(f"{receipt['run_id']}|{rendered['sha256']}|telegram-preview".encode()).hexdigest()
            delivery = {"schema_version": "marketing.telegram-effect.v1", "effect_key": delivery_key,
                        "state": "sending"}
            if not claim_receipt(delivery_path, delivery):
                existing_delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
                require(existing_delivery.get("effect_key") == delivery_key, "conflicting Telegram effect replay")
                if existing_delivery.get("state") == "completed":
                    receipt.update({"state": "rendered", "telegram_preview": existing_delivery["receipt"]})
                    write_receipt(path, receipt)
                    return receipt
                receipt["state"] = "telegram_delivery_unknown"
                write_receipt(path, receipt)
                return receipt
            receipt["state"] = "telegram_delivery_pending"
            write_receipt(path, receipt)
            try:
                message = TelegramClient.from_env().send_video(
                    rendered["output"], caption=(f"Life Manager::: Ebook Seller {script['language'].upper()} candidate — "
                                                 f"{script['hook']} | {rendered['renderer_id']} | "
                                                 f"SHA {rendered['sha256']} | not posted yet"))
                require(message.get("status") == "delivered" and message.get("message_ids")
                        and all(isinstance(item, int) and item > 0 for item in message["message_ids"]),
                        "Telegram preview has no provider receipt")
            except BaseException:
                receipt["state"] = "telegram_delivery_unknown"
                write_receipt(path, receipt)
                raise
            write_receipt(delivery_path, {**delivery, "state": "completed", "receipt": message})
            receipt.update({"state": "rendered", "telegram_preview": message})
            write_receipt(path, receipt)
    return receipt


def stage_intents(receipt: dict) -> list[dict]:
    """Create zero-effect per-platform intent prerequisites from a rendered receipt."""
    require(receipt.get("state") == "rendered", "rendered receipt required")
    render = receipt.get("render") or {}
    return [{"product_id": receipt["product_id"], "creative_id": receipt["creative_id"],
             "script_id": receipt["script_id"], "renderer_id": receipt["renderer_id"],
             "asset_path": render["output"], "asset_sha256": render["sha256"],
             "campaign_id": receipt["creative_id"], "attribution_token": campaign_token(receipt["product_id"], receipt["creative_id"]), "account_id": account["account_id"],
             "integration_id": account["integration_id"], "state": "awaiting_visual_approval",
             "external_effects": []} for account in receipt["accounts"]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, choices=("ebook-ja", "ebook-en"))
    parser.add_argument("--slot-at", required=True)
    parser.add_argument("--script-id", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--render-output", type=Path)
    parser.add_argument(
        "--asset-root", type=Path,
        help="Life Manager ebook asset base; the versioned pack is read below packs/default-v1",
    )
    parser.add_argument("--telegram-preview", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(engine=HERE, product=args.product, slot_at=args.slot_at,
                         script_id=args.script_id, ledger_path=args.ledger,
                         state_root=args.state_root, render_output=args.render_output,
                         telegram_preview=args.telegram_preview,
                         asset_root=args.asset_root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
