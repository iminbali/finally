"""SSE streaming endpoint tests.

Testing SSE through HTTP clients (TestClient, httpx) is unreliable because
the generator is an infinite loop and sync/async transports struggle with
cleanup. Instead, we test the generator function directly and verify the
router factory separately.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _make_mock_request(*, disconnected: bool = False) -> MagicMock:
    """Create a mock Request object for the SSE generator."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "test-client"
    request.is_disconnected = AsyncMock(return_value=disconnected)
    return request


async def _collect_events(
    cache: PriceCache,
    max_events: int = 5,
    poll_interval: float = 0.01,
) -> list[str]:
    """Run the SSE generator and collect up to max_events yields."""
    request = _make_mock_request()
    gen = _generate_events(cache, request, interval=poll_interval)
    events: list[str] = []
    try:
        async for event in gen:
            events.append(event)
            if len(events) >= max_events:
                break
    except asyncio.CancelledError:
        pass
    return events


@pytest.mark.asyncio
class TestSSEGenerator:
    async def test_first_yield_is_retry_directive(self):
        cache = PriceCache()
        events = await _collect_events(cache, max_events=1)
        assert events[0] == "retry: 1000\n\n"

    async def test_emits_data_frame_when_cache_has_prices(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)

        events = await _collect_events(cache, max_events=2)
        assert len(events) >= 2
        # First event is retry directive, second should be data
        data_event = events[1]
        assert data_event.startswith("data: ")
        payload = json.loads(data_event.removeprefix("data: ").strip())
        assert isinstance(payload, list)
        assert any(u["ticker"] == "AAPL" for u in payload)

    async def test_array_format_contains_all_required_fields(self):
        cache = PriceCache()
        cache.update("AAPL", 191.42)

        events = await _collect_events(cache, max_events=2)
        data_event = events[1]
        payload = json.loads(data_event.removeprefix("data: ").strip())
        aapl = next(u for u in payload if u["ticker"] == "AAPL")

        required_fields = {
            "ticker", "price", "previous_price", "timestamp",
            "change", "change_percent", "direction",
        }
        assert required_fields.issubset(aapl.keys())
        assert aapl["price"] == 191.42
        assert aapl["direction"] == "flat"  # first update

    async def test_multiple_tickers_in_one_frame(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("GOOGL", 175.0)

        events = await _collect_events(cache, max_events=2)
        data_event = events[1]
        payload = json.loads(data_event.removeprefix("data: ").strip())
        tickers = {u["ticker"] for u in payload}
        assert "AAPL" in tickers
        assert "GOOGL" in tickers

    async def test_empty_cache_emits_no_data_frames(self):
        """With an empty cache, generator yields retry then no data frames."""
        cache = PriceCache()
        request = _make_mock_request()
        gen = _generate_events(cache, request, interval=0.01)

        events: list[str] = []
        # Collect for a short window — we expect only the retry directive
        async def collect():
            async for event in gen:
                events.append(event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)  # let several poll cycles pass
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert events[0] == "retry: 1000\n\n"
        data_events = [e for e in events if e.startswith("data: ")]
        assert data_events == [], "Empty cache should produce no data frames"

    async def test_direction_reflects_price_change(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("AAPL", 191.0)  # price went up

        events = await _collect_events(cache, max_events=2)
        data_event = events[1]
        payload = json.loads(data_event.removeprefix("data: ").strip())
        aapl = next(u for u in payload if u["ticker"] == "AAPL")
        assert aapl["direction"] == "up"
        assert aapl["change"] > 0

    async def test_no_emit_when_version_unchanged(self):
        """If cache version doesn't change, no data frames should be emitted."""
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        request = _make_mock_request()
        gen = _generate_events(cache, request, interval=0.01)

        events: list[str] = []

        async def collect():
            async for event in gen:
                events.append(event)

        task = asyncio.create_task(collect())
        # Wait for initial retry + first data frame, then let it run
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        data_events = [e for e in events if e.startswith("data: ")]
        # Should have exactly 1 data frame (initial version change),
        # not repeated frames for the same version.
        assert len(data_events) == 1

    async def test_disconnect_stops_generator(self):
        """Generator should stop when client disconnects."""
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        request = _make_mock_request()
        # After first call, mark as disconnected
        call_count = 0

        async def is_disconnected():
            nonlocal call_count
            call_count += 1
            return call_count > 2  # disconnect after 2 checks

        request.is_disconnected = is_disconnected

        gen = _generate_events(cache, request, interval=0.01)
        events = []
        async for event in gen:
            events.append(event)

        # Generator should have terminated due to disconnect
        assert len(events) >= 1  # at least the retry directive


class TestStreamRouterFactory:
    def test_create_stream_router_produces_fresh_router(self):
        """Factory must return a fresh APIRouter each call to avoid duplicate routes."""
        cache = PriceCache()
        r1 = create_stream_router(cache)
        r2 = create_stream_router(cache)
        assert r1 is not r2

    def test_router_has_prices_endpoint(self):
        """The router should have the /prices GET endpoint registered."""
        cache = PriceCache()
        router = create_stream_router(cache)
        routes = [r.path for r in router.routes]
        assert any("/prices" in r for r in routes)
