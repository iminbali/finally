"""HTTP routes for the watchlist.

Adding/removing a watchlist ticker also synchronises the live MarketDataSource
so price updates flow (or stop) for the new set immediately.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import db
from .state import AppState, get_state
from .validation import normalize_ticker

logger = logging.getLogger(__name__)


class WatchlistAddRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)


class WatchlistEntry(BaseModel):
    ticker: str
    price: float | None
    previous_price: float | None
    change: float | None
    change_percent: float | None
    direction: str | None


router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("", response_model=list[WatchlistEntry])
def get_watchlist(state: AppState = Depends(get_state)) -> list[WatchlistEntry]:
    out: list[WatchlistEntry] = []
    for ticker in db.watchlist.list_tickers():
        update = state.price_cache.get(ticker)
        if update is None:
            out.append(WatchlistEntry(
                ticker=ticker, price=None, previous_price=None,
                change=None, change_percent=None, direction=None,
            ))
        else:
            out.append(WatchlistEntry(
                ticker=update.ticker,
                price=update.price,
                previous_price=update.previous_price,
                change=update.change,
                change_percent=update.change_percent,
                direction=update.direction,
            ))
    return out


@router.post("", response_model=WatchlistEntry, status_code=status.HTTP_201_CREATED)
async def add_watchlist(
    body: WatchlistAddRequest, state: AppState = Depends(get_state)
) -> WatchlistEntry:
    try:
        ticker = normalize_ticker(body.ticker)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    added = db.watchlist.add_ticker(ticker)
    if not added:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{ticker} is already in the watchlist",
        )
    # DB first, source second — roll back if source fails.
    try:
        await state.market_source.add_ticker(ticker)
    except Exception:
        db.watchlist.remove_ticker(ticker)
        logger.exception("add_ticker failed for %s; DB change rolled back", ticker)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not start tracking this ticker. Please try again.",
        )
    # Return live price if the source eagerly seeded the cache.
    quote = state.price_cache.get(ticker)
    return WatchlistEntry(
        ticker=ticker,
        price=quote.price if quote else None,
        previous_price=quote.previous_price if quote else None,
        change=quote.change if quote else None,
        change_percent=quote.change_percent if quote else None,
        direction=quote.direction if quote else None,
    )


@router.delete("/{ticker}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_watchlist(
    ticker: str, state: AppState = Depends(get_state)
) -> None:
    ticker = ticker.upper().strip()
    removed = db.watchlist.remove_ticker(ticker)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{ticker} is not in the watchlist",
        )
    # Only stop tracking if the user doesn't hold shares — otherwise
    # portfolio valuation would show stale prices.
    position = db.positions.get_position(ticker)
    if position is None or position.quantity == 0:
        try:
            await state.market_source.remove_ticker(ticker)
        except Exception:
            # Roll back the DB change to keep watchlist and source in sync.
            db.watchlist.add_ticker(ticker)
            logger.exception("remove_ticker failed for %s; DB change rolled back", ticker)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not stop tracking this ticker. Please try again.",
            )
