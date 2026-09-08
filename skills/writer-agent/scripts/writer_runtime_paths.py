"""Shared mutable-path contract for Writer Python entrypoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def life_manager_env_file(
    environ: Mapping[str, str] | None = None, *, home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = str(values.get("LIFE_MANAGER_ENV_FILE", "")).strip()
    if configured:
        return Path(configured).expanduser()
    base = Path.home() if home is None else home
    return base / ".local/state/life-manager/.env"
