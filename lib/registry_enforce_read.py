#!/usr/bin/env python3
"""registry_enforce_read.py — pure registry read helper for lib/registry-enforce.sh. Prints
bash-`eval`-able `KEY=value` lines describing one loop's registry entry (or a RESULT=missing /
RESULT=corrupt / RESULT=unknown_loop outcome), so registry_enforce_or_exit() can `eval` the output
without any JSON parsing inside bash itself. Values are shell-quoted (shlex.quote) so eval is safe
even when a value contains spaces/quotes (e.g. a JSON parse-error DETAIL message).

    python3 registry_enforce_read.py <registry_path> <loop>
"""
import json
import os
import shlex
import sys


def _emit(**kv):
    for key, value in kv.items():
        print(f"{key}={shlex.quote(str(value))}")


def main():
    registry_path, loop = sys.argv[1], sys.argv[2]

    if not os.path.isfile(registry_path):
        _emit(RESULT="missing")
        return

    try:
        with open(registry_path) as f:
            registry = json.load(f)
    except Exception as e:
        _emit(RESULT="corrupt", DETAIL=str(e))
        return

    if not isinstance(registry, dict):
        _emit(RESULT="corrupt", DETAIL="registry top-level is not a JSON object")
        return

    entry = registry.get("loops", {}).get(loop)
    if entry is None:
        _emit(RESULT="unknown_loop")
        return

    alloc = entry.get("allocation") or {}
    status = alloc.get("status", "normal")
    mult = alloc.get("pass_frequency_multiplier", 1.0)
    cadence = entry.get("cadence") or {}
    base_interval = entry.get("base_interval_seconds")
    if base_interval is None:
        base_interval = cadence.get("start_interval_seconds")
    base_minute = entry.get("base_minute")
    base_hour = entry.get("base_hour")

    fields = {"RESULT": "ok", "STATUS": status, "MULT": mult}
    if isinstance(base_interval, (int, float)) and not isinstance(base_interval, bool):
        fields["BASE_INTERVAL"] = base_interval
    if isinstance(base_minute, (int, float)) and not isinstance(base_minute, bool):
        fields["BASE_MINUTE"] = int(base_minute)
    if isinstance(base_hour, (int, float)) and not isinstance(base_hour, bool):
        fields["BASE_HOUR"] = int(base_hour)
    _emit(**fields)


if __name__ == "__main__":
    main()
