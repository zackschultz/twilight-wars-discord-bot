import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


class AsyncioRateLimiter:
    """Super-simple rate limiting class.

    Before doing something that should be rate limited, simple await `wait`
    and it will ensure that each coroutine sleeps until its turn
    """

    name: str
    delay: float
    warning_threshold: float = 1.5
    _next_ts: float

    def __init__(self, name: str, delay: float):
        self.name = name
        self.delay = delay
        self._next_ts = 0

    async def wait(self):
        cur_ts = time.time()
        if self._next_ts <= cur_ts:
            self._next_ts = cur_ts + self.delay
        else:
            wait_time = self._next_ts - cur_ts
            self._next_ts = self._next_ts + self.delay
            if wait_time > self.warning_threshold:
                logger.warning(
                    "Long rate limiter wait", name=self.name, wait_time=wait_time
                )
            await asyncio.sleep(wait_time)
