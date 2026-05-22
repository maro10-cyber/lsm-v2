"""
Liquidity Engine — builds and maintains liquidity zones, detects sweeps.

Liquidity pools:
  1. Session highs/lows   (from SessionEngine)
  2. Previous day high/low
  3. Equal highs / equal lows   (detected from rolling price history)

Sweep definition (strict):
  Bearish sweep (of a high):
    candle.high > level  AND  candle.close < level  AND  penetration >= min_ticks
  Bullish sweep (of a low):
    candle.low  < level  AND  candle.close > level  AND  penetration >= min_ticks

Rejection confirmation:
  Bearish: upper_wick >= body * rejection_ratio
  Bullish: lower_wick >= body * rejection_ratio
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from core.types import Candle, Direction, LiquiditySweep, LiquidityZone, SessionRange

logger = logging.getLogger(__name__)


class LiquidityEngine:

    def __init__(self, config: dict) -> None:
        cfg = config["liquidity"]
        sym = config["symbol"]
        self._tick_size        = sym["tick_size"]
        self._equal_tol        = cfg["equal_tolerance_ticks"] * self._tick_size
        self._min_sweep_ticks  = cfg["sweep_min_ticks"] * self._tick_size
        self._rejection_ratio  = cfg["rejection_ratio"]
        self._lookback         = cfg["lookback_bars"]

        self._zones:   List[LiquidityZone] = []
        self._history: List[Candle] = []          # rolling window for equal H/L
        self._sweeps:  List[LiquiditySweep] = []  # confirmed sweeps (not yet consumed)

    # ── Level management ─────────────────────────────────────────────────────

    def update_session_levels(self, levels: Dict[str, Optional[SessionRange]]) -> None:
        """
        Merge the latest session ranges into the zone list.
        Existing zones with the same type and price are kept to preserve sweep state.
        """
        mapping = {
            "asia":         ("session_high", "session_low"),
            "london":       ("session_high", "session_low"),
            "previous_day": ("prev_day_high", "prev_day_low"),
        }

        existing: Dict[str, LiquidityZone] = {z.zone_type + f"@{z.price}": z for z in self._zones
                                               if not z.swept}
        new_zones: List[LiquidityZone] = []

        for key, (high_type, low_type) in mapping.items():
            rng = levels.get(key)
            if rng is None:
                continue
            # Tag with session to avoid collisions across sessions
            htag = f"{key}_{high_type}@{rng.high}"
            ltag = f"{key}_{low_type}@{rng.low}"

            new_zones.append(
                existing.get(htag) or LiquidityZone(
                    zone_type=f"{key}_high", price=rng.high, created_at=rng.high_time
                )
            )
            new_zones.append(
                existing.get(ltag) or LiquidityZone(
                    zone_type=f"{key}_low", price=rng.low, created_at=rng.low_time
                )
            )

        # Keep equal-H/L zones (they're detected separately)
        eq_zones = [z for z in self._zones if z.zone_type in ("equal_highs", "equal_lows") and not z.swept]
        self._zones = new_zones + eq_zones

    def detect_equal_levels(self, candle: Candle) -> None:
        """
        Compare the most recent candle's high/low against the single prior
        local extreme (highest high / lowest low in the lookback window,
        excluding the current bar).  This produces "double top / double bottom"
        levels rather than flooding with every near-equal pair.
        """
        self._history.append(candle)
        if len(self._history) > self._lookback:
            self._history.pop(0)

        if len(self._history) < 3:
            return

        prior = self._history[:-1]
        prior_high = max(c.high for c in prior)
        prior_low  = min(c.low  for c in prior)
        recent_high = candle.high
        recent_low  = candle.low

        already_eq_highs = {round(z.price, 4) for z in self._zones
                            if z.zone_type == "equal_highs" and not z.swept}
        already_eq_lows  = {round(z.price, 4) for z in self._zones
                            if z.zone_type == "equal_lows"  and not z.swept}

        # Equal highs: recent high ≈ prior swing high
        if abs(recent_high - prior_high) <= self._equal_tol:
            price = round((recent_high + prior_high) / 2, 4)
            if price not in already_eq_highs:
                self._zones.append(LiquidityZone(
                    zone_type="equal_highs",
                    price=price,
                    strength=2,
                    created_at=candle.timestamp,
                ))
                logger.debug(f"Equal highs detected @ {price}")

        # Equal lows: recent low ≈ prior swing low
        if abs(recent_low - prior_low) <= self._equal_tol:
            price = round((recent_low + prior_low) / 2, 4)
            if price not in already_eq_lows:
                self._zones.append(LiquidityZone(
                    zone_type="equal_lows",
                    price=price,
                    strength=2,
                    created_at=candle.timestamp,
                ))
                logger.debug(f"Equal lows detected @ {price}")

    # ── Sweep detection ───────────────────────────────────────────────────────

    def process_candle(self, candle: Candle) -> List[LiquiditySweep]:
        """
        Check all active zones for sweeps on this candle.
        Returns list of newly confirmed sweeps.
        """
        new_sweeps: List[LiquiditySweep] = []

        for zone in self._zones:
            if zone.swept:
                continue

            sweep = self._detect_sweep(candle, zone)
            if sweep:
                zone.swept = True
                zone.swept_at = candle.timestamp
                self._sweeps.append(sweep)
                new_sweeps.append(sweep)
                logger.info(
                    f"SWEEP | {sweep.direction.value.upper()} | "
                    f"{zone.zone_type} @ {zone.price} | "
                    f"Pen={sweep.penetration:.2f} | Rejected={sweep.rejected} | "
                    f"{candle.timestamp}"
                )

        return new_sweeps

    def _detect_sweep(self, candle: Candle, zone: LiquidityZone) -> Optional[LiquiditySweep]:
        is_high_level = zone.zone_type.endswith("high") or zone.zone_type == "equal_highs"
        is_low_level  = zone.zone_type.endswith("low")  or zone.zone_type == "equal_lows"

        if is_high_level:
            # Bearish sweep: wick above level, close BELOW it
            if candle.high > zone.price and candle.close < zone.price:
                pen = candle.high - zone.price
                if pen >= self._min_sweep_ticks:
                    rejected = candle.upper_wick >= candle.body * self._rejection_ratio
                    return LiquiditySweep(
                        zone=zone, direction=Direction.BEARISH,
                        candle=candle, penetration=pen, rejected=rejected,
                    )

        elif is_low_level:
            # Bullish sweep: wick below level, close ABOVE it
            if candle.low < zone.price and candle.close > zone.price:
                pen = zone.price - candle.low
                if pen >= self._min_sweep_ticks:
                    rejected = candle.lower_wick >= candle.body * self._rejection_ratio
                    return LiquiditySweep(
                        zone=zone, direction=Direction.BULLISH,
                        candle=candle, penetration=pen, rejected=rejected,
                    )

        return None

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_active_zones(self) -> List[LiquidityZone]:
        return [z for z in self._zones if not z.swept]

    def pop_new_sweeps(self) -> List[LiquiditySweep]:
        """Consume and return all pending new sweeps."""
        sweeps = list(self._sweeps)
        self._sweeps.clear()
        return sweeps

    def get_sweep_extreme(self, sweep: LiquiditySweep) -> float:
        """The worst price reached by the sweep candle."""
        if sweep.direction == Direction.BEARISH:
            return sweep.candle.high   # swept a high
        return sweep.candle.low        # swept a low
