"""
Market Structure Engine — swing highs/lows and MSS detection.

Swing definition (single-bar pivot):
    Swing high: high[i] > high[i-1] AND high[i] > high[i+1]
    Swing low:  low[i]  < low[i-1]  AND low[i]  < low[i+1]

MSS (Market Structure Shift):
    Bearish MSS: after a bearish sweep, a candle closes below a recent swing low
    Bullish MSS: after a bullish sweep, a candle closes above a recent swing high
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, List, Optional

from core.types import (
    Candle, Direction, LiquiditySweep,
    MarketStructureShift, SwingPoint,
)

logger = logging.getLogger(__name__)


class StructureEngine:

    def __init__(self, config: dict) -> None:
        cfg = self._cfg = config["structure"]
        self._swing_lookback  = cfg.get("swing_lookback", 1)
        self._mss_lookback    = cfg.get("mss_lookback_bars", 30)
        # Buffer of recent candles for swing detection (need lookback*2 + 1)
        self._window: Deque[Candle] = deque(maxlen=self._swing_lookback * 2 + 1)
        self._bar_index: int = 0

        self._swing_highs: List[SwingPoint] = []
        self._swing_lows:  List[SwingPoint] = []

    def update(self, candle: Candle) -> None:
        """Add a new 1m candle; detect swing points from the completed pivot."""
        self._window.append(candle)
        self._bar_index += 1

        # We need at least lookback*2+1 bars to confirm a pivot
        if len(self._window) < self._swing_lookback * 2 + 1:
            return

        pivot = self._window[self._swing_lookback]   # middle of the window
        lb    = self._swing_lookback

        left_highs  = [self._window[i].high for i in range(lb)]
        right_highs = [self._window[i].high for i in range(lb + 1, lb * 2 + 1)]
        left_lows   = [self._window[i].low  for i in range(lb)]
        right_lows  = [self._window[i].low  for i in range(lb + 1, lb * 2 + 1)]

        # Swing high
        if all(pivot.high > h for h in left_highs) and all(pivot.high > h for h in right_highs):
            sp = SwingPoint(
                price=pivot.high,
                direction=Direction.BEARISH,
                timestamp=pivot.timestamp,
                bar_index=self._bar_index - lb,
            )
            self._swing_highs.append(sp)
            logger.debug(f"Swing HIGH @ {pivot.high} | {pivot.timestamp}")

        # Swing low
        if all(pivot.low < l for l in left_lows) and all(pivot.low < l for l in right_lows):
            sp = SwingPoint(
                price=pivot.low,
                direction=Direction.BULLISH,
                timestamp=pivot.timestamp,
                bar_index=self._bar_index - lb,
            )
            self._swing_lows.append(sp)
            logger.debug(f"Swing LOW  @ {pivot.low} | {pivot.timestamp}")

        # Prune old swings (keep last 100)
        if len(self._swing_highs) > 100:
            self._swing_highs = self._swing_highs[-100:]
        if len(self._swing_lows) > 100:
            self._swing_lows = self._swing_lows[-100:]

    def check_mss(
        self,
        sweep: LiquiditySweep,
        candle: Candle,
        max_age_bars: int,
    ) -> Optional[MarketStructureShift]:
        """
        After a sweep, check if this candle constitutes an MSS.

        Bearish sweep → look for close below a recent swing low.
        Bullish sweep → look for close above a recent swing high.
        """
        if sweep.direction == Direction.BEARISH:
            # Find swing lows formed AFTER the sweep candle
            candidates = [
                s for s in self._swing_lows
                if s.timestamp > sweep.candle.timestamp
                and (self._bar_index - s.bar_index) <= max_age_bars
                and not s.broken
            ]
            for swing in candidates:
                if candle.close < swing.price:
                    swing.broken = True
                    swing.broken_at = candle.timestamp
                    mss = MarketStructureShift(
                        direction=Direction.BEARISH,
                        sweep=sweep,
                        broken_swing=swing,
                        candle=candle,
                        mss_price=swing.price,
                    )
                    logger.info(
                        f"MSS BEARISH | Broke swing low {swing.price} @ {candle.timestamp}"
                    )
                    return mss

        elif sweep.direction == Direction.BULLISH:
            candidates = [
                s for s in self._swing_highs
                if s.timestamp > sweep.candle.timestamp
                and (self._bar_index - s.bar_index) <= max_age_bars
                and not s.broken
            ]
            for swing in candidates:
                if candle.close > swing.price:
                    swing.broken = True
                    swing.broken_at = candle.timestamp
                    mss = MarketStructureShift(
                        direction=Direction.BULLISH,
                        sweep=sweep,
                        broken_swing=swing,
                        candle=candle,
                        mss_price=swing.price,
                    )
                    logger.info(
                        f"MSS BULLISH | Broke swing high {swing.price} @ {candle.timestamp}"
                    )
                    return mss

        return None

    # ── Accessors ─────────────────────────────────────────────────────────────

    def recent_swing_highs(self, n: int = 5) -> List[SwingPoint]:
        return self._swing_highs[-n:]

    def recent_swing_lows(self, n: int = 5) -> List[SwingPoint]:
        return self._swing_lows[-n:]

    def last_swing_high(self) -> Optional[SwingPoint]:
        return self._swing_highs[-1] if self._swing_highs else None

    def last_swing_low(self) -> Optional[SwingPoint]:
        return self._swing_lows[-1] if self._swing_lows else None
