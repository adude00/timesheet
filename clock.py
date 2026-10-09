"""Isolated server clock.

Every timestamp in the application comes from here so that tests can pin the
"now" instant without sleeping.  Values are UTC epoch seconds (integers).
"""
import time


class Clock:
    def __init__(self):
        self._pinned = None

    def now(self) -> int:
        """Current server instant as UTC epoch seconds."""
        if self._pinned is not None:
            return self._pinned
        return int(time.time())

    def set_now(self, value: int) -> None:
        """Pin the clock (tests only)."""
        self._pinned = int(value)

    def unpin(self) -> None:
        self._pinned = None


server_clock = Clock()
