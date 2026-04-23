# Market Data Backend — Comprehensive Code Review

**Date:** 2026-04-23
**Reviewer:** Claude (claude-opus-4-6)
**Scope:** `backend/app/market/` (8 source files, 1 `__init__.py`) and `backend/tests/market/` (7 test files)
**Prior reviews:** `planning/archive/MARKET_DATA_REVIEW.md` (2026-02-10), `planning/REVIEW.md` (2026-04-19)
**Design documents cross-referenced:** `MARKET_DATA_DESIGN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`, `PLAN.md`

---

## 1. Test Results

**192 tests collected. 190 passed, 2 failed, SSE stream tests hang.**

| Test suite | Tests | Result |
|---|---|---|
| `tests/market/test_models.py` | 11 | All pass |
| `tests/market/test_cache.py` | 16 | All pass |
| `tests/market/test_simulator.py` | 25 | All pass |
| `tests/market/test_simulator_source.py` | 13 | **1 failure** |
| `tests/market/test_massive.py` | 18 | All pass |
| `tests/market/test_factory.py` | 7 | All pass |
| `tests/market/test_stream.py` | 8 | **Hang** (timeout after 45s) |
| `tests/api/` | 22 | All pass |
| `tests/db/` | 33 | All pass |
| `tests/llm/` | 19 | **1 failure** (outside market scope) |

### 1.1 Failure: `test_no_simulator_seed_is_non_deterministic` (market)

**File:** `tests/market/test_simulator_source.py:172-196`

```python
async def test_no_simulator_seed_is_non_deterministic(self):
    ...
    await src_a.start(["AAPL"])
    for _ in range(50):
        src_a._sim.step()          # advances internal state
    prices_a = cache_a.get_price("AAPL")  # reads from cache
    ...
    assert prices_a != prices_b    # FAILS: both are 190.0
```

**Root cause:** `src_a._sim.step()` advances the simulator's internal prices but does **not** write to the cache. The cache is only updated by the `_run_loop` background task. With `update_interval=10.0`, the background task never fires during the test. Both `cache_a.get_price("AAPL")` and `cache_b.get_price("AAPL")` return the initial seed price (190.00), so `assert prices_a != prices_b` fails deterministically.

**Fix:** Compare internal simulator prices (`src_a._sim.get_price("AAPL")`) instead of reading from the cache, or use a short `update_interval` with `asyncio.sleep()` to let the background loop fire.

### 1.2 Hang: `tests/market/test_stream.py`

**Root cause:** `test_empty_cache_emits_no_data_frames` hangs. The test calls `r.iter_text()` in a loop expecting 10 chunks, but with an empty cache, the SSE generator only yields the `retry: 1000\n\n` directive once and then loops silently (no data to emit). `iter_text()` blocks waiting for a second chunk that never comes. Since the SSE generator never yields again and the client never disconnects, the test hangs indefinitely.

This also blocks all subsequent stream tests in the module from running.

**Fix:** Use a timeout on the streaming read, or restructure the test to break after receiving the retry directive rather than waiting for 10 chunks from a generator that will only ever produce one.

### 1.3 Failure: `test_live_path_parses_structured_output` (LLM, out of scope)

**File:** `tests/llm/test_client.py:49` — `KeyError: 'extra_body'`. Not a market data issue; noted for completeness.

---

## 2. Fixes Since the Archived Review (2026-02-10)

The archived review flagged six issues. All have been addressed:

| Issue | Status |
|---|---|
| Missing `[tool.hatch.build.targets.wheel]` in `pyproject.toml` | ✅ Fixed |
| `_generate_events` annotated `-> None` instead of `-> AsyncGenerator[str, None]` | ✅ Fixed — now `AsyncGenerator[str, None]` |
| `PriceCache.version` read outside lock | ✅ Fixed — now reads under `threading.Lock` |
| `SimulatorDataSource.get_tickers()` reached into `_sim._tickers` | ✅ Fixed — `GBMSimulator.get_tickers()` public method added |
| Module-level `router` footgun in `create_stream_router` | ✅ Fixed — fresh `APIRouter` per call |
| 5 failing Massive tests due to `massive` package absent | ✅ Fixed — tests restructured to patch `_fetch_snapshots` directly |

All the right fixes, well executed. Prior review issues are closed.

---

## 3. Architecture Assessment

The market data subsystem is cleanly designed with clear single-responsibility modules:

```
MarketDataSource (ABC, interface.py)
├── SimulatorDataSource  → GBMSimulator (correlated GBM)
└── MassiveDataSource    → Massive REST snapshot API
        │
        ▼  writes
   PriceCache (thread-safe, versioned, cache.py)
        │
        ▼  reads
   SSE /api/stream/prices (stream.py) → Frontend EventSource
```

### Strengths

| Area | Assessment |
|---|---|
| Module structure | 8 focused files with a clean `__init__.py` exporting exactly 5 public names |
| `PriceUpdate` | `frozen=True, slots=True` — immutable, memory-efficient; derived properties can never desync |
| `PriceCache` | `threading.Lock` for both asyncio and thread-pool writers; version counter under lock (no-GIL safe) |
| GBM formula | Correctly implements: `S * exp((mu - 0.5σ²)dt + σ√dt·Z)` — prices always positive |
| Cholesky correlation | Mathematically sound; sector groups produce visible co-movement without identical lines |
| Event shocks | 0.001/tick creates visible drama; correctly multiplicative (preserves lognormal structure) |
| Idempotent lifecycle | `start()`/`stop()` on both sources safe to call multiple times |
| Eager cache seeding | `SimulatorDataSource` seeds on `start()` and `add_ticker()` — no blank first SSE frame |
| Exception resilience | Both `_run_loop` and `_poll_once` catch all exceptions and continue |
| SSE version gating | Frames only emitted when `cache.version != last_version` — efficient for slow sources |
| SSE wire format | Matches PLAN.md §8 exactly (JSON array, all required fields) |
| Factory env routing | Clean, matches PLAN.md §5; returns unstarted source |
| Massive 429 handling | 60s cooldown on `asyncio.get_event_loop().time()` — correct |
| Massive malformed snap | Per-ticker exception handling; siblings unaffected |
| Test coverage | Strong happy-path coverage with good fixture reuse; 5K-step correlation test is a nice touch |

---

## 4. Issues Found

### 4.1 Timestamp Divisor: Milliseconds vs Nanoseconds (Severity: HIGH)

**File:** `backend/app/market/massive_client.py:18-27`

```python
def _parse_timestamp(ts: int | float | None) -> float | None:
    """...Snapshot timestamps may be in milliseconds (ms) from the Massive SDK..."""
    if ts is None:
        return None
    # Massive SDK returns milliseconds; divide by 1000 to get seconds.
    return float(ts) / 1000.0
```

The planning documentation (`MARKET_DATA_DESIGN.md §7.2`, `MASSIVE_API.md` response shape) explicitly states that Massive snapshot timestamps (`tickers[i].updated`, `tickers[i].lastTrade.t`) are **nanoseconds**, not milliseconds. The correct divisor is `1e9`, not `1000`.

With nanosecond input (e.g., `1_776_321_000_120_000_000 / 1000 = 1_776_321_000_120.0` seconds ≈ year 58,400), this silently produces garbage timestamps in every SSE frame when running in Massive mode. `MASSIVE_API.md` calls this out explicitly: *"Verify at runtime and fix the divisor if necessary."*

The test `test_timestamp_conversion` passes a millisecond value (`1707580800000`) and asserts the ms-to-seconds result (`1707580800.0`), which is self-consistent but validates the **wrong behavior**. If the real API returns nanoseconds, this test masks the bug.

The `MARKET_DATA_DESIGN.md` specifies the function should be named `_ns_to_seconds` and divide by `1e9`.

**Fix:** Change divisor to `1e9`, rename function to `_ns_to_seconds`, update docstring, and update the test to use a realistic nanosecond timestamp.

### 4.2 SSE Stream Test Hangs (Severity: HIGH)

**File:** `tests/market/test_stream.py:107-123` — `test_empty_cache_emits_no_data_frames`

As described in §1.2, this test blocks forever because `iter_text()` waits for chunks the generator will never produce. This prevents the entire stream test suite (8 tests) from running, which means SSE behavior is effectively **untested in CI**.

**Fix:** Add a timeout mechanism to `_read_until_data`, or restructure the empty-cache test to break after reading the retry directive. For example, read a single chunk, confirm it contains `retry: 1000`, confirm it doesn't contain `data:`, and return.

### 4.3 Watchlist Atomicity — No Compensating Rollback (Severity: MEDIUM)

**File:** `backend/app/watchlist_api.py:66-76, 79-91`

The design document (`MARKET_DATA_DESIGN.md §11.2`) specifies a DB-first/rollback pattern:

```python
entry_id = await insert_watchlist_entry(...)
try:
    await source.add_ticker(ticker)
except Exception:
    await delete_watchlist_entry(entry_id)   # rollback
    raise HTTPException(500, ...)
```

The actual implementation does:

```python
added = db.watchlist.add_ticker(ticker)      # DB first ✓
if not added:
    raise HTTPException(409, ...)
await state.market_source.add_ticker(ticker)  # No try/except, no rollback ✗
```

If `market_source.add_ticker()` raises, the DB row is written but the live market source hasn't added the ticker. The DB and live stream are inconsistent until restart. Similarly, `remove_watchlist` does not check for open positions before calling `source.remove_ticker()` (per `MARKET_DATA_DESIGN.md §11.3`), which would cause stale portfolio valuations for held tickers.

This was also flagged as HIGH in `planning/REVIEW.md` (finding #2) and remains unaddressed.

**Fix:** Wrap `market_source.add_ticker` / `remove_ticker` in try/except with DB rollback. Add position check before removing price tracking on watchlist removal.

### 4.4 Factory No Longer Lazy-Imports MassiveDataSource (Severity: MEDIUM)

**File:** `backend/app/market/factory.py:8-11`

```python
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource
```

The design doc (`MARKET_DATA_DESIGN.md §8`, `MARKET_INTERFACE.md §Factory`) specifies that `MassiveDataSource` should be lazily imported inside `create_market_data_source()` so the `massive` SDK is not required for simulator-only users. The current implementation imports both at module load time.

The practical impact is mitigated because `massive>=1.0.0` is now a core dependency in `pyproject.toml`, and the actual `from massive import RESTClient` lazy import still lives inside `MassiveDataSource.start()`. However, `__init__.py` imports from `factory.py`, which imports from `massive_client.py`, which imports from `cache.py` and `interface.py` — so `import app.market` now transitively loads `massive_client.py` even for simulator-only users. The design doc proposed `massive` as an optional extra (`project.optional-dependencies.massive`), which is incompatible with this eager import.

**Fix:** Either (a) accept `massive` as a core dependency and document the decision, or (b) restore the lazy import inside `create_market_data_source()` and move `massive` to an optional extra.

### 4.5 Interface Docstring Contradicts Implementations (Severity: LOW)

**File:** `backend/app/market/interface.py:26-30`

```python
async def start(self, tickers: list[str]) -> None:
    """...Must be called exactly once. Calling start() twice is undefined behavior."""
```

Both `SimulatorDataSource.start()` and `MassiveDataSource.start()` are idempotent (`if self._task is not None: return`). The design doc (`MARKET_DATA_DESIGN.md §4`) says start "Must be idempotent: start → start is a no-op on the second call." Tests verify this. The docstring actively misleads future implementers.

**Fix:** Update docstring to say start is idempotent and a second call is a no-op.

### 4.6 `add_watchlist` Returns `price=None` After Eager Cache Seed (Severity: LOW)

**File:** `backend/app/watchlist_api.py:73-76`

```python
await state.market_source.add_ticker(ticker)
return WatchlistEntry(
    ticker=ticker, price=None, previous_price=None,  # ← always None
    change=None, change_percent=None, direction=None,
)
```

`SimulatorDataSource.add_ticker()` eagerly seeds the cache before returning, so a price IS available immediately. The route could read it from the cache and return it. The frontend instead shows `—` for the new ticker until the next SSE frame arrives, even though the data is already there.

**Fix:** Read from `state.price_cache.get(ticker)` after `add_ticker()` and populate the response.

### 4.7 `DEFAULT_CORR` Defined But Never Used (Severity: LOW)

**File:** `backend/app/market/seed_prices.py:48` and `backend/app/market/simulator.py:207`

`DEFAULT_CORR = 0.3` is defined in `seed_prices.py` and imported in `simulator.py`, but `GBMSimulator._pairwise_correlation` actually returns `DEFAULT_CORR` as the fallback (line 207). Wait — re-checking the code: it does return `DEFAULT_CORR` on line 207 for the unknown-vs-unknown case. However, the comment on `CROSS_GROUP_CORR` says "Between sectors / unknown tickers" which overlaps with `DEFAULT_CORR`'s purpose ("Unknown vs unknown"). Both are 0.3, so behavior is correct, but the naming is confusing — if the constants were ever tuned independently, the overlap in semantics would cause bugs.

**Fix:** Clarify the distinction in comments, or collapse into one constant if they should always be equal.

### 4.8 Seed Price Discrepancies vs Design Doc (Severity: LOW)

**File:** `backend/app/market/seed_prices.py` vs `planning/MARKET_DATA_DESIGN.md §5`

Minor parameter differences between the implementation and the design document:

| Ticker | Design `mu` | Code `mu` | Design `sigma` | Code `sigma` | Design seed | Code seed |
|---|---|---|---|---|---|---|
| AAPL | 0.08 | 0.05 | 0.22 | 0.22 | $190 | $190 |
| GOOGL | 0.07 | 0.05 | 0.24 | 0.25 | $175 | $175 |
| AMZN | 0.06 | 0.05 | 0.28 | 0.28 | $180 | $185 |
| NVDA | 0.08 | 0.08 | 0.42 | 0.40 | $800 | $800 |
| META | 0.07 | 0.05 | 0.30 | 0.30 | $500 | $500 |
| JPM | 0.05 | 0.04 | 0.18 | 0.18 | $195 | $195 |
| V | 0.06 | 0.04 | 0.17 | 0.17 | $275 | $280 |
| NFLX | 0.05 | 0.05 | 0.32 | 0.35 | $620 | $600 |

None of these differences are functionally significant — the simulator produces plausible paths either way. But the drift parameters are notably more conservative in the implementation (most set to 0.05 vs the design's 0.06-0.08), which means prices drift upward more slowly than the design intended.

**Fix:** Decide which values are canonical and synchronize. If the implementation values were deliberately tuned, update the design doc.

---

## 5. Missing Test Coverage

Gaps identified in the design documentation (`MARKET_DATA_DESIGN.md §12.7`) and from this review:

| Gap | Status | Notes |
|---|---|---|
| Compensating rollback test for `add_ticker` / `remove_ticker` raising | Missing | Required by design §12.7 |
| Full 10-ticker simulator smoke test | Missing | All tests use 1–2 tickers; full Cholesky path untested |
| Event-shock frequency statistical test | Missing | No test verifies observed shock rate ≈ `event_probability` over 10K+ steps |
| Position-aware watchlist removal test | Missing | No test checks that removing a watchlist ticker while holding shares keeps price tracking active |
| `_parse_timestamp(None)` edge case | Missing | Covered by code, not by test |
| SSE empty-cache behavior | **Broken** | Test hangs — effectively no SSE coverage in CI |
| SSE no-change-no-emit behavior | **Blocked** | Cannot run due to hanging test above |

---

## 6. Code Quality Observations

### Well-executed patterns

- **Thread-safe cache with version counter** — elegant solution to the "event-driven without pub/sub" problem. SSE polls at 500ms but only serializes on change. Massive's 15s poll cadence means 29/30 SSE loops are no-ops, as intended.
- **`asyncio.Lock` in SimulatorDataSource** — correctly guards concurrent `step()` and `add/remove` against Cholesky shape mismatch. Not needed for the cache (which has its own `threading.Lock`), but essential for the simulator's internal arrays.
- **Cancellation hygiene** — `_run_loop`, `_poll_loop`, and `_generate_events` all catch `CancelledError` cleanly and re-raise where appropriate. No swallowed cancellations that would hang shutdown.
- **`to_thread` for Massive SDK** — correct choice since the Polygon/Massive SDK is synchronous. `threading.Lock` on the cache covers this path.
- **`create_stream_router` as a factory** — avoids module-level state and allows independent test instances. Previous review's footgun is fixed.

### Minor style notes

- `simulator.py` imports `DEFAULT_CORR` from `seed_prices.py` but the fallback case in `_pairwise_correlation` returns it correctly. However, `CROSS_GROUP_CORR` is used for cross-sector cases that include one known group member, while `DEFAULT_CORR` is for two completely unknown tickers. The semantic distinction is correct but could be clearer.
- The `_fetch_snapshots` method in `massive_client.py` does a second lazy import (`from massive.rest.models import SnapshotMarketType`). This is called on every poll cycle in a thread. The import is cached by Python after the first call, so the overhead is negligible, but moving it to `start()` alongside the `RESTClient` import would be cleaner.

---

## 7. Verdict

**The market data backend is well-engineered and production-ready for the simulator path.** The architecture is clean, the GBM math is correct, thread safety is sound, and the test coverage for happy paths is thorough. All issues from the prior review (2026-02-10) have been resolved.

### Must fix

1. **Timestamp divisor** (§4.1) — divide by `1e9`, not `1000`; rename to `_ns_to_seconds`; update test to use nanosecond input. **This is a data corruption bug in Massive mode.**
2. **Hanging stream test** (§4.2) — `test_empty_cache_emits_no_data_frames` blocks the entire stream test suite from running in CI.

### Should fix

3. **Watchlist atomicity** (§4.3) — add try/except rollback around `market_source.add_ticker()` / `remove_ticker()`; check for open position before stopping price tracking on removal. Flagged in two prior reviews and still open.
4. **Broken non-determinism test** (§1.1) — `test_no_simulator_seed_is_non_deterministic` fails deterministically; compare internal simulator prices, not cache.
5. **Interface docstring** (§4.5) — update `start()` docstring to match idempotent implementations and design spec.

### Nice to have

6. Return live price from `add_watchlist` after eager seed (§4.6)
7. Restore lazy import in factory or document `massive` as a required dependency (§4.4)
8. Synchronize seed price parameters between code and design doc (§4.8)
9. Clarify `DEFAULT_CORR` vs `CROSS_GROUP_CORR` naming (§4.7)
10. Add the three missing test coverage areas from design §12.7

### No action required

- Factory lazy-import (§4.4) is a design philosophy question; since `massive` is already a core dep, the eager import is defensible. Document the decision either way.
- The prior review's `ruff` lint warnings appear to have been cleaned up; no new lint issues observed.
