"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

# Back-off duration after a 429 Too Many Requests response.
_RATE_LIMIT_COOLDOWN_S = 60.0


def _parse_timestamp(ts: int | float | None) -> float | None:
    """Convert a Massive snapshot timestamp to Unix seconds.

    Snapshot timestamps may be in milliseconds (ms) from the Massive SDK.
    We convert to seconds for the PriceCache.
    """
    if ts is None:
        return None
    # Massive SDK returns milliseconds; divide by 1000 to get seconds.
    return float(ts) / 1000.0


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s

    The Massive SDK is lazily imported inside start() so simulator-only
    users don't pay the install cost.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._cooldown_until: float = 0.0  # event-loop time; 0 = no cooldown

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            return  # idempotent

        # Lazy import — avoids requiring the massive SDK for simulator-only users.
        from massive import RESTClient  # type: ignore[import]

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = [t.upper().strip() for t in tickers]

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker and ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            loop_time = asyncio.get_event_loop().time()
            if loop_time < self._cooldown_until:
                remaining = self._cooldown_until - loop_time
                logger.debug("Massive rate-limit cooldown: %.1fs remaining", remaining)
                continue
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    ticker = getattr(snap, "ticker", None)
                    last_trade = getattr(snap, "last_trade", None)
                    if not ticker or last_trade is None:
                        continue
                    price = float(last_trade.price)
                    timestamp = _parse_timestamp(
                        getattr(last_trade, "timestamp", None)
                        or getattr(snap, "updated", None)
                    )
                    self._cache.update(
                        ticker=ticker,
                        price=price,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError, ValueError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            # Detect 429 rate-limit errors from the SDK.
            status = getattr(e, "status", None) or getattr(e, "status_code", None)
            if status == 429:
                self._cooldown_until = asyncio.get_event_loop().time() + _RATE_LIMIT_COOLDOWN_S
                logger.warning("Massive 429 — cooling down for %.0fs", _RATE_LIMIT_COOLDOWN_S)
            else:
                logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        from massive.rest.models import SnapshotMarketType  # type: ignore[import]

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
