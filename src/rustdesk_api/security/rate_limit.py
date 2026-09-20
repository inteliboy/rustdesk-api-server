"""In-memory sliding-window rate limiter for authentication endpoints.

Deliberately simple and dependency-free (CLAUDE.md section 26: do not
introduce Redis as a mandatory dependency). This only limits requests
within a single process. If the application is ever run with multiple
worker processes, each worker enforces its own independent limit - document
this clearly rather than pretending it is a distributed limiter.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                return False
            hits.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
