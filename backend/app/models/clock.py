"""Time is int milliseconds UTC, everywhere (docs/conventions.md §2).

Local time is never persisted; formatting for a human is the frontend's problem.
"""

import time


def now_ms() -> int:
    return int(time.time() * 1000)
