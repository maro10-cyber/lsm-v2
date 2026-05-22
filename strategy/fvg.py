"""
Fair Value Gap (FVG) Engine — strict 3-candle imbalance detection.

Bullish FVG  (up-gap):   candle[2].low  > candle[0].high   → gap between them
Bearish FVG  (down-gap): candle[2].high < candle[0].low    → gap between them

Mitigation (partial): price trades into the gap by at least 50%.
Invalidation: price closes fully through the gap.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, List, Optional

from core.types import Candle, Direction, FairValueGap

logger = logging.getLogger(__name__)


class FVGEngine:

    def __init__(self, config: dict) -> None:
        cfg = config["fvg"]
        sym = config["symbol"]
        self._tick_size        = sym["tick_size"]
        self._min_size         = cfg["min_size_ticks"] * self._tick_size
        self._max_age_bars     = cfg["max_age_bars"]
        self._require_unmit    = cfg.get("require_unmitigated", True)

        self._window: Deque[Candle] = deque(maxlen=3)
        self._fvgs:   List[FairValueGap] = []
        self._bar_index: int = 0

    def update(self, candle: Candle) -> Optional[FairValueGap]:
        """
        Add one 1m candle.  Returns a newly formed FVG if detected, else None.
        Also ages and invalidates existing FVGs.
        """
        self._window.append(candle)
        self._bar_index += 1

        self._age_fvgs(candle)

        if len(self._window) < 3:
            return None

        c0, c1, c2 = self._window[0], self._window[1], self._window[2]
        return self._detect(c0, c1, c2)

    def _detect(self, c0: Candle, c1: Candle, c2: Candle) -> Optional[FairValueGap]:
        # Bullish FVG: gap above c0.high, below c2.low
        if c2.low > c0.high:
            size = c2.low - c0.high
            if size >= self._min_size:
                fvg = FairValueGap(
                    direction=Direction.BULLISH,
                    top=c2.low,
                    bottom=c0.high,
                    midpoint=(c2.low + c0.high) / 2,
                    formed_at=c2.timestamp,
                    bar_index=self._bar_index,
                    candle1=c0,
                    candle2=c1,
                    candle3=c2,
                )
                self._fvgs.append(fvg)
                logger.debug(
                    f"FVG BULLISH [{c0.high:.2f} – {c2.low:.2f}] size={size:.2f} @ {c2.timestamp}"
                )
                return fvg

        # Bearish FVG: gap below c0.low, above c2.high
        if c2.high < c0.low:
            size = c0.low - c2.high
            if size >= self._min_size:
                fvg = FairValueGap(
                    direction=Direction.BEARISH,
                    top=c0.low,
                    bottom=c2.high,
                    midpoint=(c0.low + c2.high) / 2,
                    formed_at=c2.timestamp,
                    bar_index=self._bar_index,
                    candle1=c0,
                    candle2=c1,
                    candle3=c2,
                )
                self._fvgs.append(fvg)
                logger.debug(
                    f"FVG BEARISH [{c2.high:.2f} – {c0.low:.2f}] size={size:.2f} @ {c2.timestamp}"
                )
                return fvg

        return None

    def _age_fvgs(self, candle: Candle) -> None:
        """Invalidate FVGs that price has fully closed through or that are too old."""
        for fvg in self._fvgs:
            if fvg.invalidated:
                continue

            age = self._bar_index - fvg.bar_index
            if age > self._max_age_bars:
                fvg.invalidated = True
                continue

            # Partial mitigation tracking
            if fvg.direction == Direction.BULLISH:
                if candle.low <= fvg.top and candle.low >= fvg.bottom:
                    fvg.mitigated = True
                # Full close-through invalidates
                if candle.close < fvg.bottom:
                    fvg.invalidated = True

            elif fvg.direction == Direction.BEARISH:
                if candle.high >= fvg.bottom and candle.high <= fvg.top:
                    fvg.mitigated = True
                if candle.close > fvg.top:
                    fvg.invalidated = True

        # Prune list
        if len(self._fvgs) > 200:
            self._fvgs = self._fvgs[-200:]

    def get_active_fvgs(self, direction: Optional[Direction] = None) -> List[FairValueGap]:
        """Return FVGs that are not invalidated (optionally filtered by direction)."""
        active = [f for f in self._fvgs if not f.invalidated]
        if self._require_unmit:
            active = [f for f in active if not f.mitigated]
        if direction is not None:
            active = [f for f in active if f.direction == direction]
        return active

    def find_nearest_fvg(
        self,
        direction: Direction,
        price: float,
        max_age_bars: int,
    ) -> Optional[FairValueGap]:
        """
        Find the most recently formed, unmitigated FVG in the given direction
        that price has not yet entered, within max_age_bars.
        """
        candidates = [
            f for f in self._fvgs
            if not f.invalidated
            and (not self._require_unmit or not f.mitigated)
            and f.direction == direction
            and (self._bar_index - f.bar_index) <= max_age_bars
        ]
        if not candidates:
            return None
        # Closest to current price
        candidates.sort(key=lambda f: abs(f.midpoint - price))
        return candidates[0]

    def find_fvg_above_sweep(
        self,
        sweep_ts,
        direction: Direction,
        max_age_bars: int,
    ) -> Optional[FairValueGap]:
        """Return newest FVG formed AFTER sweep_ts in given direction."""
        candidates = [
            f for f in self._fvgs
            if not f.invalidated
            and (not self._require_unmit or not f.mitigated)
            and f.direction == direction
            and f.formed_at >= sweep_ts
            and (self._bar_index - f.bar_index) <= max_age_bars
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda f: f.bar_index)
