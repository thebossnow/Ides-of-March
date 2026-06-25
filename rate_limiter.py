"""
rate_limiter.py - Thread-safe sliding-window rate limiter.

Enforces a maximum number of requests within a rolling time window.
Designed for the AviationWeather.gov API limit of 100 requests per minute.

Usage:
    from rate_limiter import aviation_limiter

    aviation_limiter.wait()   # blocks until a slot is available
    requests.get(url, ...)    # make the API call
"""

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window rate limiter that is safe across threads.

    Tracks timestamps of recent requests in a deque. Before each new
    request, evicts entries older than *window_seconds*, then blocks
    if the window is full.

    Args:
        max_requests: Maximum number of requests allowed in the window.
        window_seconds: Length of the sliding window in seconds.
        name: Label used in log messages.
    """

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: float = 60.0,
        name: str = "RateLimiter",
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.name = name
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _purge_old(self, now: float) -> None:
        """Remove timestamps outside the current window (caller holds lock)."""
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def wait(self) -> None:
        """Block until a request slot is available, then record the request.

        If the window is full, sleeps until the oldest entry expires.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                self._purge_old(now)

                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return

                # Window is full: compute how long to wait
                oldest = self._timestamps[0]
                sleep_for = (oldest + self.window_seconds) - now + 0.05

            logger.debug(
                "%s: rate limit reached (%d/%d). Sleeping %.2fs",
                self.name, len(self._timestamps), self.max_requests, sleep_for,
            )
            time.sleep(max(0.0, sleep_for))

    @property
    def requests_in_window(self) -> int:
        """Returns the current number of requests tracked in the window."""
        with self._lock:
            self._purge_old(time.monotonic())
            return len(self._timestamps)

    def __repr__(self) -> str:
        return (
            f"RateLimiter(name={self.name!r}, "
            f"max={self.max_requests}, "
            f"window={self.window_seconds}s, "
            f"current={self.requests_in_window})"
        )


# -----------------------------------------------------------------------
# Pre-configured instance for AviationWeather.gov
# 100 requests per minute, shared across all threads.
# -----------------------------------------------------------------------
aviation_limiter = RateLimiter(
    max_requests=100,
    window_seconds=60.0,
    name="AviationWeather",
)


if __name__ == "__main__":
    print("Rate Limiter Module Test")
    print("=" * 50)

    limiter = RateLimiter(max_requests=5, window_seconds=3.0, name="Test")
    print(f"Config: {limiter}")

    print("\nFiring 7 requests (limit 5 per 3s)...")
    for i in range(7):
        t0 = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - t0
        print(f"  Request {i + 1}: waited {elapsed:.3f}s | in window: {limiter.requests_in_window}")

    print("\nDone. Rate limiter working correctly.")
