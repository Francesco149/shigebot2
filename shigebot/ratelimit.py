"""
Rate limiting for outbound chat messages.

Twitch enforces two limits simultaneously:

1. GLOBAL — one shared counter across all channels, two thresholds:
     Regular:         20 msg / 30 s  (non-elevated channels stop here)
     Mod/VIP/bc:     100 msg / 30 s  (elevated channels continue to here)

2. PER-CHANNEL — 1 msg/s hard cap, non-elevated channels only.

Architecture: two lock layers.

  Per-channel lock — held through the entire HTTP send, so _last_send is
    updated from actual delivery time. Releasing before the send would let
    the next call start timing before the previous message was delivered,
    shrinking the effective gap below the cap.

  Global lock — held only for the atomic check-and-record (microseconds).
    Released before the HTTP call so other channels are not blocked by
    one channel's network latency.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Coroutine, Any

logger = logging.getLogger(__name__)

# 1 msg/s hard cap + 200 ms headroom
TWITCH_PER_CHANNEL_MIN_INTERVAL = 1.2


class RateLimiter:
    def __init__(
        self,
        window: float = 30.0,
        non_elevated_max: int = 18,
        elevated_max: int = 95,
        elevated_channels: set[str] | None = None,
    ) -> None:
        self.window = window
        self.non_elevated_max = non_elevated_max
        self.elevated_max = elevated_max
        self._elevated: set[str] = elevated_channels if elevated_channels is not None else set()

        self._global_timestamps: deque[float] = deque()
        self._global_lock = asyncio.Lock()

        self._last_send: dict[str, float] = {}
        self._channel_locks: dict[str, asyncio.Lock] = {}

    def is_elevated(self, channel: str) -> bool:
        return channel in self._elevated

    def _get_channel_lock(self, channel: str) -> asyncio.Lock:
        if channel not in self._channel_locks:
            self._channel_locks[channel] = asyncio.Lock()
        return self._channel_locks[channel]

    def _purge(self, now: float) -> None:
        while self._global_timestamps and now - self._global_timestamps[0] >= self.window:
            self._global_timestamps.popleft()

    def _channel_wait(self, channel: str, now: float) -> float:
        if self.is_elevated(channel):
            return 0.0
        return max(0.0, TWITCH_PER_CHANNEL_MIN_INTERVAL - (now - self._last_send.get(channel, 0.0)))

    async def wait_and_send(self, channel: str, coro: Coroutine[Any, Any, None]) -> None:
        async with self._get_channel_lock(channel):

            # Per-channel 1 msg/s cap (non-elevated only)
            while True:
                cw = self._channel_wait(channel, time.monotonic())
                if cw <= 0.0:
                    break
                logger.debug("Rate limiting #%s: per-channel wait %.3fs", channel, cw)
                await asyncio.sleep(cw)

            # Global window — claim slot, sleep outside the lock if full
            while True:
                sleep_for = 0.0
                async with self._global_lock:
                    now = time.monotonic()
                    self._purge(now)
                    count = len(self._global_timestamps)
                    cap = self.elevated_max if self.is_elevated(channel) else self.non_elevated_max
                    if count < cap:
                        self._global_timestamps.append(now)
                        logger.debug(
                            "Sending to #%s (%s): global %d/%d",
                            channel, "elevated" if self.is_elevated(channel) else "regular",
                            count + 1, cap,
                        )
                        break
                    sleep_for = max(0.001, self.window - (now - self._global_timestamps[0]))

                logger.debug("Rate limiting #%s: global full (%d/%d), waiting %.3fs", channel, count, cap, sleep_for)
                await asyncio.sleep(sleep_for)

            try:
                await coro
            finally:
                self._last_send[channel] = time.monotonic()
