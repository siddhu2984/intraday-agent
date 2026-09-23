"""Client-side rate limiting for broker API calls (architecture.md §4.1)."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class RateLimiter:
    """Blocks until a call is allowed: at most `per_second` calls in any 1 s and `per_minute` in any 60 s."""

    def __init__(
        self,
        per_second: int,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._windows = [(1.0, per_second, deque()), (60.0, per_minute, deque())]
        self._clock = clock
        self._sleep = sleep

    def wait(self) -> None:
        while True:
            now = self._clock()
            delay = 0.0
            for span, limit, calls in self._windows:
                while calls and now - calls[0] >= span:
                    calls.popleft()
                if len(calls) >= limit:
                    delay = max(delay, span - (now - calls[0]))
            if delay <= 0:
                break
            self._sleep(delay)
        for _, _, calls in self._windows:
            calls.append(now)
