# Market Data Backend — Comprehensive Code Review

**Date:** 2026-04-23
**Reviewer:** Claude (claude-sonnet-4-6)
**Branch:** `claude/issue-4-20260423-0319`
**Scope:** `backend/app/market/` (8 source files) and `backend/tests/market/` (7 test files)
**Prior review archived at:** `planning/archive/MARKET_DATA_REVIEW.md` (2026-02-10)

---

## 1. Test Results

**Prior recorded run (2026-04-19, from `planning/REVIEW.md`):** 168 tests, 168 passed.

Note: `uv` was not available in the current CI sandbox, preventing a fresh test run here. All assertions below are from static code analysis cross-referenced against the design documents. Where discrepancies between code and tests exist, they are called out explicitly.

**Lint status (from prior run):** Source code passes `ruff` cleanly. Five unused-import warnings remain in test files (`pytest`, `math`, `asyncio`).

---

## 2. Fixes Since the Archived Review (2026-02-10)

The previous review flagged six issues. All have been addressed in the current implementation:

| Issue | Status |
|---|---|
| Missing `[tool.hatch.build.targets.wheel]` in `pyproject.toml` | ✅ Fixed |
| `_generate_events` annotated `-> None` instead of `-> AsyncGenerator[str, None]` | ✅ Fixed |
| `PriceCache.version` read outside lock | ✅ Fixed — now reads under `threading.Lock` |
| `SimulatorDataSource.get_tickers()` reached into `_sim._tickers` (private) | ✅ Fixed — `GBMSimulator.get_tickers()` public method added |
| Module-level `router` footgun in `create_stream_router` | ✅ Fixed — fresh `APIRouter` per call |
| 5 failing Massive tests due to `massive` package absent | ✅ Resolved — `massive>=1.0.0` is now a core dependency in `pyproject.toml`; tests restructured to patch `_fetch_snapshots` directly |

These are all the right fixes. The code quality has measurably improved.

---

## 3. Architecture Assessment

The market data subsystem is cleanly designed and largely matches the specification in `MARKET_DATA_DESIGN.md`.

```
MarketDataSource (ABC, interface.py)
├── SimulatorDataSource  → GBMSimulator (correlated GBM)
└── MassiveDataSource    → Massive REST snapshot API
        │
        ▼
   PriceCache (thread-safe, versioned)
        │
        ▼
   SSE /api/stream/prices → Frontend EventSource
```

**Strengths:**
- Clear single-responsibility split across 8 focused modules
- `PriceUpdate` is correctly `frozen=True, slots=True` — immutable, memory-efficient
- `PriceCache` uses `threading.Lock` (not `asyncio.Lock`) to handle Massive's thread-dispatch path
- Version counter correctly read under lock (no-GIL safe)
- GBM formula correctly implemented: `S * exp((mu - 0.5*sigma²)*dt + sigma*sqrt(dt)*Z)`
- Cholesky decomposition for cross-sector correlation is mathematically sound
- Both data sources: exception resilience, idempotent start/stop, eager cache seeding
- SSE: version-driven emission (not timer-driven), `retry:1000` directive, nginx buffering disabled
- Factory correctly routes on env var, returns unstarted source
- `__init__.py` exports exactly the right public surface

---

## 4. Issues Found

### 4.1 Timestamp Divisor: Milliseconds vs Nanoseconds (Severity: High)

**File:** `backend/app/market/massive_client.py:18-27`

```python
def _parse_timestamp(ts: int | float | None) -> float | None:
    """Convert a Massive snapshot timestamp to Unix seconds.

    Snapshot timestamps may be in milliseconds (ms) from the Massive SDK.
    We convert to seconds for the PriceCache.
    """
    if ts is None:
        return None
    # Massive SDK returns milliseconds; divide by 1000 to get seconds.
    return float(ts) / 1000.0
```

The planning documentation (`MARKET_DATA_DESIGN.md §7.2`, `MASSIVE_API.md` response shape) explicitly states that Massive snapshot timestamps (`tickers[i].updated`, `tickers[i].lastTrade.t`) are **nanoseconds**, not milliseconds. The correct divisor is `1e9`, not `1000`.

The current implementation would produce timestamps from the year ~57,000 for nanosecond inputs (e.g., `1_776_321_000_120_000_000 / 1000 = 1_776_321_000_120.0` seconds ≈ year 58,400). This silently corrupts every SSE frame in Massive mode.

The design document in `MASSIVE_API.md` notes this explicitly: *"the current MassiveDataSource divides last_trade.timestamp by 1000, assuming milliseconds. Snapshot responses use nanoseconds. Verify at runtime and fix the divisor if necessary."*

The test `test_timestamp_conversion` validates the ms→seconds path (passing `1707580800000` and asserting `1707580800.0`), which is self-consistent but validates the wrong behavior. If the real Massive API returns nanoseconds, this test would mask the bug.

**Fix:** Change divisor to `1e9` and update the test to use a realistic nanosecond timestamp. Also rename the helper to `_ns_to_seconds` to match the design doc.

---

### 4.2 Factory No Longer Lazy-Imports MassiveDataSource (Severity: Medium)

**File:** `backend/app/market/factory.py:8-9`

```python
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource
```

The design document (`MARKET_DATA_DESIGN.md §8`, `MARKET_INTERFACE.md §Factory`) specifies that `MassiveDataSource` should be lazily imported inside `create_market_data_source()` so the `massive` SDK is not required for simulator-only users. The current implementation imports both `MassiveDataSource` and `SimulatorDataSource` at module load time.

The practical impact is mitigated because:
1. `massive>=1.0.0` is now a core (non-optional) dependency in `pyproject.toml`
2. The actual `from massive import RESTClient` lazy import still lives inside `MassiveDataSource.start()`

However, the architectural principle is violated: importing `factory.py` always loads `massive_client.py`, even when no Massive key is configured. The `pyproject.toml` comment in the design doc proposed making `massive` an optional extra (`project.optional-dependencies.massive`). That is incompatible with the current eager import in the factory.

**Fix:** Either (a) keep `massive` as a core dependency and accept the eager import, or (b) restore the lazy import inside `create_market_data_source()` and move `massive` back to an optional extra.

---

### 4.3 Watchlist Atomicity — No Compensating Rollback (Severity: Medium)

**File:** `backend/app/watchlist_api.py:66-76, 84-91`

The design document (`MARKET_DATA_DESIGN.md §11.2`) specifies a DB-first/rollback pattern:

```python
# DB first.
entry_id = await insert_watchlist_entry(user_id="default", ticker=ticker)
# Source second — roll back if it raises.
try:
    await source.add_ticker(ticker)
except Exception:
    await delete_watchlist_entry(entry_id)
    raise HTTPException(500, "Could not start tracking this ticker.")
```

The actual implementation does:

```python
added = db.watchlist.add_ticker(ticker)  # DB first ✓
if not added:
    raise HTTPException(409, ...)
await state.market_source.add_ticker(ticker)  # No try/except, no rollback ✗
```

If `market_source.add_ticker()` raises, the DB row has been written but the live market source hasn't added the ticker. On the next restart, the DB seed will re-add it to the source — so this is recoverable — but until restart, the watchlist DB and live price stream are inconsistent.

Similarly, `remove_watchlist` does not check for open positions before calling `source.remove_ticker()` (`MARKET_DATA_DESIGN.md §11.3`). A user holding shares of a removed watchlist ticker would have its price tracking stopped, producing stale portfolio valuations.

This issue was also flagged as HIGH in `planning/REVIEW.md` (finding #2).

**Fix:** Wrap `market_source.add_ticker` / `remove_ticker` in try/except with DB rollback. Add position check before source removal.

---

### 4.4 Interface Docstring Contradicts All Implementations (Severity: Low)

**File:** `backend/app/market/interface.py:31-32`

```python
@abstractmethod
async def start(self, tickers: list[str]) -> None:
    """Begin producing price updates for the given tickers.

    Starts a background task that periodically writes to the PriceCache.
    Must be called exactly once. Calling start() twice is undefined behavior.
    """
```

Both `SimulatorDataSource.start()` and `MassiveDataSource.start()` are idempotent (`if self._task is not None: return`). The design doc (`MARKET_DATA_DESIGN.md §4`) says start "Must be idempotent: start → start is a no-op on the second call." The ABC docstring actively misleads future implementers.

**Fix:** Update the docstring to say start is idempotent and a second call is a no-op.

---

### 4.5 `add_watchlist` Returns `price=None` After Eager Cache Seed (Severity: Low)

**File:** `backend/app/watchlist_api.py:73-76`

```python
await state.market_source.add_ticker(ticker)
return WatchlistEntry(
    ticker=ticker, price=None, previous_price=None,  # ← always None
    change=None, change_percent=None, direction=None,
)
```

`SimulatorDataSource.add_ticker()` eagerly seeds the cache before returning, so a price IS available immediately after the call. The route could return it:

```python
await state.market_source.add_ticker(ticker)
quote = state.price_cache.get(ticker)
return WatchlistEntry(
    ticker=ticker,
    price=quote.price if quote else None,
    ...
)
```

This is a minor UX issue: the frontend shows `—` for the new ticker until the next SSE frame, even though the price is already in the cache.

---

### 4.6 `DEFAULT_CORR` vs `CROSS_GROUP_CORR` Naming Ambiguity (Severity: Low)

**File:** `backend/app/market/seed_prices.py:46-48`

```python
CROSS_GROUP_CORR = 0.3  # Between sectors / unknown tickers
TSLA_CORR = 0.3  # TSLA does its own thing
DEFAULT_CORR = 0.3  # Unknown vs unknown
```

`DEFAULT_CORR` is defined but never used. `GBMSimulator._pairwise_correlation` returns `CROSS_GROUP_CORR` for all non-matched pairs — which is semantically the same value (0.3) but could become confusing if the constants are ever tuned separately. This was flagged in the archive review and remains.

**Fix:** Either remove `DEFAULT_CORR` and update the `_pairwise_correlation` fallback comment, or actually use it as the fallback case.

---

### 4.7 Test: `test_no_simulator_seed_is_non_deterministic` Is Unreliable (Severity: Low)

**File:** `backend/tests/market/test_simulator_source.py:172-196`

```python
async def test_no_simulator_seed_is_non_deterministic(self):
    ...
    await src_a.start(["AAPL"])
    # Drive several steps manually so prices diverge
    for _ in range(50):
        src_a._sim.step()
    prices_a = cache_a.get_price("AAPL")
    await src_a.stop()
```

`src_a._sim.step()` advances the simulator's internal price state but **does not write to the cache**. The cache is only updated by the `_run_loop` background task. With `update_interval=10.0`, the background task won't fire within the test's runtime. So `cache_a.get_price("AAPL")` returns the initial seed price (190.00) from `start()`, not the stepped price.

Since AAPL's seed price is hardcoded in `SEED_PRICES` at 190.00, **both** `prices_a` and `prices_b` will always be 190.00, and `assert prices_a != prices_b` would fail deterministically. This test passed in prior runs likely because it was not properly exercised, or the background task happened to fire exactly once — neither of which is reliable.

**Fix:** Either compare the internal simulator prices (`src_a._sim.get_price("AAPL")`) instead of the cache, or use `asyncio.sleep` to let the background loop fire before reading the cache.

---

## 5. Missing Test Coverage (Design Doc §12.7 Gaps)

These gaps were identified in the design documentation and remain unimplemented:

| Gap | Notes |
|---|---|
| Compensating rollback test for `add_ticker` / `remove_ticker` raising | Required by design §12.7; no failure-injection test exists |
| Full 10-ticker simulator smoke test | All tests use 1–2 tickers; the full Cholesky path for 10 tickers is untested |
| Event-shock frequency statistical test | No test verifies observed shock rate ≈ `event_probability` over 10K+ steps |
| Explicit test for `_parse_timestamp(None)` | Edge case covered by code, not by test |
| Position-aware watchlist removal test | No test verifies that removing a ticker from the watchlist while holding a position does not stop price tracking |

---

## 6. Things Done Well

| Area | Assessment |
|---|---|
| GBM formula | Correctly implements log-normal: `exp((mu - 0.5σ²)dt + σ√dt·Z)` |
| Cholesky correlation | Mathematically correct; sector groups are reasonable |
| Event shocks | 0.001/tick creates visible but not dominating drama; correctly multiplicative |
| PriceCache concurrency | `threading.Lock` around all reads and writes; version under lock |
| PriceCache snapshot | `get_all()` returns a shallow copy — safe to iterate outside lock |
| Idempotent lifecycle | `start()`/`stop()` on both sources are safe to call twice |
| Eager cache seeding | SimulatorDataSource seeds on `start()` and `add_ticker()` — no blank first frame |
| Exception resilience | `_run_loop` and `_poll_once` both catch all exceptions and continue |
| SSE version gating | Frames only emitted when `cache.version != last_version` |
| SSE wire format | Matches PLAN.md §8 exactly (array, all required fields) |
| Factory env routing | Clean, matches PLAN.md §5 env var spec |
| Massive 429 handling | 60s cooldown correctly set on `asyncio.get_event_loop().time()` |
| Massive malformed snap | `AttributeError`/`TypeError`/`ValueError` caught per-ticker; siblings unaffected |
| Module public API | `__init__.py` exports exactly the five public names |
| Test structure | Well-organized `Test*` classes; good fixture reuse; async tests properly marked |
| Correlation test | Statistical test over 5,000 steps verifies co-movement above random baseline |

---

## 7. Verdict

**The market data backend is production-quality for the capstone's scope.** The architecture is clean, the GBM math is correct, and the test coverage for happy paths is thorough. The design document was carefully followed with all previously flagged issues resolved.

**Must fix before Massive mode goes live:**
1. **Timestamp divisor** (§4.1) — divide by `1e9`, not `1000`; update `test_timestamp_conversion` to use nanosecond input

**Should fix:**
2. **Watchlist atomicity** (§4.3) — add try/except rollback in `add_watchlist` / `remove_watchlist`, check for position before stopping tracker
3. **Unreliable test** (§4.7) — fix `test_no_simulator_seed_is_non_deterministic` to compare simulator-internal prices or yield to the event loop
4. **Interface docstring** (§4.4) — update `start()` docstring to say it is idempotent

**Nice to have:**
5. Return live price from `add_watchlist` after eager seed (§4.5)
6. Remove or use `DEFAULT_CORR` (§4.6)
7. Add the three missing test coverage areas from §12.7

**Not needed:**
- The factory lazy-import question (§4.2) is a matter of design philosophy; since `massive` is already a core dependency, the eager import is arguably cleaner. No action required unless `massive` is made optional again.
