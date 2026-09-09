#!/usr/bin/env python3
"""Build and atomically publish the compact CEO unit-economics snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from ceo_unit_economics import build_snapshot  # noqa: E402
from registry_write_gate import atomic_write_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", type=Path)
    parser.add_argument("--config-root", type=Path, default=ROOT)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config_path = args.config_root / "config" / "ceo-unit-economics.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config is not an object")
        snapshot = build_snapshot(args.base, config, args.date, config_root=args.config_root)
        output = args.output or args.base / "ledgers" / "ceo-unit-economics.latest.json"
        atomic_write_registry(str(output), snapshot)
    except Exception as error:
        print(f"ceo-unit-economics: snapshot failed ({error})", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "ok", "mode": snapshot["mode"], "window": snapshot["window"],
        "loops": len(snapshot["loops"]), "output": str(output),
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
