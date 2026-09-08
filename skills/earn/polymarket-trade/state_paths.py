"""Fail-closed paths for mutable Polymarket state."""
from pathlib import Path


def external_state_path(value: str, repo_root: Path, name: str) -> str:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise RuntimeError(f"{name} must be absolute")
    resolved = raw.resolve(strict=False)
    repo = repo_root.resolve(strict=False)
    if resolved == repo or repo in resolved.parents:
        raise RuntimeError(f"{name} must resolve outside the repository")
    return str(resolved)
