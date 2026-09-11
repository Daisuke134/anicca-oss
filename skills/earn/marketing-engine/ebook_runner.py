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
from skills._shared.telegram import TelegramClient  # noqa: E402
from attribution import campaign_token  # noqa: E402


WATERCOLOR_CLIP_NAMES = (
    "jp_kling_clip_02.mp4", "jp_kling_clip_03.mp4", "jp_kling_clip_05.mp4",
    "jp_kling_clip_07.mp4", "jp_kling_clip_08.mp4", "jp_kling_clip_10.mp4",
)


def default_asset_root() -> Path:
    configured = os.environ.get("LM_EBOOK_ASSET_ROOT")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "life-manager/ebook-assets"


def watercolor_clip_paths(asset_root: Path) -> list[Path]:
    clips = Path(asset_root) / "watercolor-monk/clips"
    return [clips / name for name in WATERCOLOR_CLIP_NAMES]


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


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
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        require(all(existing.get(key) == receipt[key] for key in receipt if key not in {"state", "external_effects"}), "conflicting run replay")
        if existing.get("state") == "rendered":
            return existing
    else:
        path.write_text(encoded, encoding="utf-8")
    if render_output is not None:
        if product == "ebook-ja":
            clips = watercolor_clip_paths(asset_root or default_asset_root())
            rendered = render_watercolor(script=script["body"], output=render_output, clips=clips)
        else:
            rendered = render_heygen(script=script["body"], output=render_output)
        if rendered.get("state") == "setup_required":
            receipt.update({"state": "setup_required", "setup": rendered})
            path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            return receipt
        receipt.update({"state": "rendered", "render": rendered})
        if telegram_preview:
            message = TelegramClient.from_env().send_video(
                rendered["output"], caption=(f"Life Manager::: Ebook Seller {script['language'].upper()} candidate — "
                                             f"{script['hook']} | {rendered['renderer_id']} | "
                                             f"SHA {rendered['sha256']} | not posted yet"))
            receipt["telegram_preview"] = message
        path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
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
    parser.add_argument("--asset-root", type=Path, help="immutable ebook asset pack root")
    parser.add_argument("--telegram-preview", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(engine=HERE, product=args.product, slot_at=args.slot_at,
                         script_id=args.script_id, ledger_path=args.ledger,
                         state_root=args.state_root, render_output=args.render_output,
                         telegram_preview=args.telegram_preview,
                         asset_root=args.asset_root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
