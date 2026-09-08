"""The model process boundary must not hold a Writer owner forever."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_bounded_exec_terminates_a_hung_model_process_group() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "runtime" / "loop" / "bounded-exec.py"),
            "0.1",
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 124
