"""SSE streaming endpoint tests."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market.cache import PriceCache
from app.market.stream import create_stream_router


def _make_app(cache: PriceCache, poll_interval: float = 0.05) -> FastAPI:
    """Build a minimal FastAPI app with the SSE stream router."""
    app = FastAPI()
    app.include_router(create_stream_router(cache, poll_interval=poll_interval))
    return app


def _read_until_data(response, max_chunks: int = 200) -> list[str]:
    """Read SSE chunks until we find at least one data frame or exhaust max_chunks."""
    buf = ""
    for chunk in response.iter_text():
        buf += chunk
        if max_chunks <= 0:
            break
        max_chunks -= 1
        # Look for complete data lines
        data_lines = [line for line in buf.splitlines() if line.startswith("data: ")]
        if data_lines:
            return data_lines
    return [line for line in buf.splitlines() if line.startswith("data: ")]


class TestStreamRouter:
    def test_returns_correct_content_type(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        app = _make_app(cache)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                assert r.status_code == 200
                assert "text/event-stream" in r.headers["content-type"]

    def test_emits_retry_directive(self):
        cache = PriceCache()
        app = _make_app(cache)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                buf = ""
                for chunk in r.iter_text():
                    buf += chunk
                    if "retry:" in buf:
                        break
                assert "retry: 1000" in buf

    def test_emits_data_frame_when_cache_has_prices(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        app = _make_app(cache)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                data_lines = _read_until_data(r)

        assert data_lines, "Expected at least one data frame"
        payload = json.loads(data_lines[0].removeprefix("data: "))
        assert isinstance(payload, list), "SSE payload must be a JSON array"
        assert any(u["ticker"] == "AAPL" for u in payload)

    def test_array_format_contains_all_required_fields(self):
        cache = PriceCache()
        cache.update("AAPL", 191.42)
        app = _make_app(cache)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                data_lines = _read_until_data(r)

        payload = json.loads(data_lines[0].removeprefix("data: "))
        aapl = next(u for u in payload if u["ticker"] == "AAPL")

        required_fields = {"ticker", "price", "previous_price", "timestamp", "change", "change_percent", "direction"}
        assert required_fields.issubset(aapl.keys())
        assert aapl["price"] == 191.42
        assert aapl["direction"] == "flat"  # first update, no change yet

    def test_multiple_tickers_in_one_frame(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("GOOGL", 175.0)
        app = _make_app(cache)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                data_lines = _read_until_data(r)

        payload = json.loads(data_lines[0].removeprefix("data: "))
        tickers = {u["ticker"] for u in payload}
        assert "AAPL" in tickers
        assert "GOOGL" in tickers

    def test_empty_cache_emits_no_data_frames(self):
        """When the cache is empty, no data frames should be emitted (only retry)."""
        cache = PriceCache()
        app = _make_app(cache, poll_interval=0.01)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                buf = ""
                chunk_count = 0
                for chunk in r.iter_text():
                    buf += chunk
                    chunk_count += 1
                    if chunk_count >= 10:
                        break
        # retry directive present, no data frames
        assert "retry: 1000" in buf
        assert "data: " not in buf

    def test_direction_reflects_price_change(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("AAPL", 191.0)  # price went up
        app = _make_app(cache)

        with TestClient(app) as client:
            with client.stream("GET", "/api/stream/prices") as r:
                data_lines = _read_until_data(r)

        payload = json.loads(data_lines[0].removeprefix("data: "))
        aapl = next(u for u in payload if u["ticker"] == "AAPL")
        assert aapl["direction"] == "up"
        assert aapl["change"] > 0

    def test_create_stream_router_produces_fresh_router(self):
        """Factory must return a fresh APIRouter each call to avoid duplicate routes."""
        cache = PriceCache()
        r1 = create_stream_router(cache)
        r2 = create_stream_router(cache)
        assert r1 is not r2
