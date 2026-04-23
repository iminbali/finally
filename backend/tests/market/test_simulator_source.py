"""Integration tests for SimulatorDataSource."""

import asyncio
import os
from unittest.mock import patch

import pytest

from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:
    """Integration tests for the SimulatorDataSource."""

    async def test_start_populates_cache(self):
        """Test that start() immediately populates the cache."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        # Cache should have seed prices immediately (before first loop tick)
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None

        await source.stop()

    async def test_prices_update_over_time(self):
        """Test that prices are updated periodically."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        initial_version = cache.version
        await asyncio.sleep(0.3)  # Several update cycles

        # Version should have incremented (prices updated)
        assert cache.version > initial_version

        await source.stop()

    async def test_stop_is_clean(self):
        """Test that stop() is clean and idempotent."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()
        # Double stop should not raise
        await source.stop()

    async def test_add_ticker(self):
        """Test adding a ticker dynamically."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.stop()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "TSLA"])

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        tickers = source.get_tickers()
        assert set(tickers) == {"AAPL", "GOOGL"}

        await source.stop()

    async def test_empty_start(self):
        """Test starting with no tickers."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start([])

        assert len(cache) == 0
        assert source.get_tickers() == []

        await source.stop()

    async def test_exception_resilience(self):
        """Test that simulator continues running after errors."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)

        # Start with a valid ticker
        await source.start(["AAPL"])

        # Wait for some updates
        await asyncio.sleep(0.15)

        # Task should still be running
        assert source._task is not None
        assert not source._task.done()

        await source.stop()

    async def test_custom_update_interval(self):
        """Test using a custom update interval."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
        await source.start(["AAPL"])

        initial_version = cache.version
        await asyncio.sleep(0.05)  # Should get ~5 updates

        # Should have multiple updates with fast interval
        assert cache.version > initial_version + 2

        await source.stop()

    async def test_custom_event_probability(self):
        """Test creating source with custom event probability."""
        cache = PriceCache()
        # Very high event probability for testing
        source = SimulatorDataSource(
            price_cache=cache, update_interval=0.1, event_probability=1.0
        )
        await source.start(["AAPL"])

        # Just verify it starts and stops cleanly
        await asyncio.sleep(0.2)
        await source.stop()

    async def test_start_is_idempotent(self):
        """Calling start() twice should not start a second background task."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=1.0)
        await source.start(["AAPL"])
        first_task = source._task

        await source.start(["GOOGL"])  # second call should be a no-op
        assert source._task is first_task

        await source.stop()

    async def test_simulator_seed_produces_deterministic_initial_prices(self):
        """SIMULATOR_SEED env var should produce repeatable initial prices."""
        with patch.dict(os.environ, {"SIMULATOR_SEED": "42"}):
            cache_a = PriceCache()
            src_a = SimulatorDataSource(price_cache=cache_a, update_interval=10.0)
            await src_a.start(["AAPL", "GOOGL"])
            prices_a = {t: cache_a.get_price(t) for t in src_a.get_tickers()}
            await src_a.stop()

        with patch.dict(os.environ, {"SIMULATOR_SEED": "42"}):
            cache_b = PriceCache()
            src_b = SimulatorDataSource(price_cache=cache_b, update_interval=10.0)
            await src_b.start(["AAPL", "GOOGL"])
            prices_b = {t: cache_b.get_price(t) for t in src_b.get_tickers()}
            await src_b.stop()

        assert prices_a == prices_b

    async def test_no_simulator_seed_is_non_deterministic(self):
        """Without SIMULATOR_SEED, two sources should very likely produce different prices."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove SIMULATOR_SEED if set
            os.environ.pop("SIMULATOR_SEED", None)

            cache_a = PriceCache()
            src_a = SimulatorDataSource(price_cache=cache_a, update_interval=10.0)
            await src_a.start(["AAPL"])
            # Drive several steps manually so internal prices diverge.
            # Note: step() updates internal state but not the cache, so we
            # compare simulator-internal prices, not cache values.
            for _ in range(50):
                src_a._sim.step()
            price_a = src_a._sim.get_price("AAPL")
            await src_a.stop()

            cache_b = PriceCache()
            src_b = SimulatorDataSource(price_cache=cache_b, update_interval=10.0)
            await src_b.start(["AAPL"])
            for _ in range(50):
                src_b._sim.step()
            price_b = src_b._sim.get_price("AAPL")
            await src_b.stop()

        # With overwhelming probability, un-seeded sources diverge after 50 steps
        assert price_a != price_b
