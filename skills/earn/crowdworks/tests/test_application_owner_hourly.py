from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


PATH = Path(__file__).resolve().parents[1] / "scripts" / "application_owner.py"


def load():
    spec = importlib.util.spec_from_file_location("crowdworks_application_owner_hourly_test", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hourly_rate_uses_top_of_official_range():
    module = load()

    assert module._hourly_rate("仕事の概要 時間単価制 1,500円 〜 2,000円") == 2000
    assert module._hourly_rate("仕事の概要 固定報酬制 50,000円") is None
