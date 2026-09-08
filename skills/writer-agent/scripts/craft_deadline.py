#!/usr/bin/env python3
"""Compute the next local wall-clock deadline using the host timezone rules."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta


def next_deadline(hour: int, minute: int, *, now_epoch: int | None = None) -> tuple[int, int]:
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    now = datetime.fromtimestamp(now_epoch)
    deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if deadline <= now:
        deadline += timedelta(days=1)
    return now_epoch, int(time.mktime(deadline.timetuple()))


if __name__ == "__main__":
    now, deadline = next_deadline(
        int(sys.argv[1]), int(sys.argv[2]),
        now_epoch=int(os.environ["CRAFT_TRAIN_NOW_EPOCH"])
        if "CRAFT_TRAIN_NOW_EPOCH" in os.environ else None,
    )
    print(now, deadline)
