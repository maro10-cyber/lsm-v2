"""
HTF Bias Engine — aggregates 15m signals into a directional bias score.

Score components (each ±1, total range -4..+4):
  1. Structure  : latest 15m swing direction (HH/HL = +1, LH/LL = -1)
  2. VWAP       : close above VWAP = +1, below = -1
  3. Liquidity  : price closer to recent low = +1 (room to run up), near high = -1
  4. Session    : prev-day bias from SessionEngine (bullish = +1, bearish = -1)

Thresholds (from config):
  bias_score >= min_score_bullish  → BULLISH
  bias_score <= min_score_bearish  → BEARISH
  else                             → NEUTRAL
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, List, Optional

from core.types import Candle, Direction, HTFBias

logger = logging.getLogger(__name__)


class BiasEngine:

    def __init__(self, config: dict) -> None:
        cfg = config["bias"]
        self._min_bullish = cfg.get("min_score_bullish", 2)
        self._min_bearish = cfg.get("min_score_bearish", -2)

        # Rolling VWAP state (reset each session/day externally or on demand)
        self._vwap_num: float = 0.0   # sum of typical_price * volume
        self._vwap_den: float = 0.0   # sum of volume
        self._vwap: Optional[float] = None

        # Rolling HTF candle buffer for structure detection
        self._htf_candles: Deque[Candle] = deque(maxlen=20)

        # Latest bias output
        self._latest: Optional[HTFBias] = None

        # Session daily bias string passed in from SessionEngine
        self._session_bias: str = "neutral"

    # ── External updates ──────────────────────────────────────────────────────

    def reset_vwap(self) -> None:
        """Call at start of new trading day."""
        self._vwap_num = 0.0
        self._vwap_den = 0.0
        self._vwap = None

    def set_session_bias(self, bias: str) -> None:
        """Set bias from SessionEngine.get_daily_bias()."""
        self._session_bias = bias

    def update(self, htf_candle: Candle) -> HTFBias:
        """
        Process one closed 15m candle and return an updated HTFBias.
        Call this whenever HTFAggregator emits a new closed candle.
        """
        # Update VWAP
        vol = htf_candle.volume if htf_candle.volume > 0 else 1.0
        self._vwap_num += htf_candle.typical_price * vol
        self._vwap_den += vol
        self._vwap = self._vwap_num / self._vwap_den

        self._htf_candles.append(htf_candle)

        score = self._compute_score(htf_candle)

        if score >= self._min_bullish:
            direction = Direction.BULLISH
        elif score <= self._min_bearish:
            direction = Direction.BEARISH
        else:
            direction = None  # NEUTRAL

        self._latest = HTFBias(
            direction=direction,
            score=score,
            vwap=self._vwap,
            candle=htf_candle,
        )
        logger.debug(
            f"HTF Bias | score={score} dir={direction} vwap={self._vwap:.2f} @ {htf_candle.timestamp}"
        )
        return self._latest

    # ── Score components ──────────────────────────────────────────────────────

    def _compute_score(self, htf_candle: Candle) -> int:
        score = 0
        score += self._structure_score()
        score += self._vwap_score(htf_candle)
        score += self._liquidity_score(htf_candle)
        score += self._session_score()
        return score

    def _structure_score(self) -> int:
        """
        Determine HTF swing structure from the rolling 15m buffer.
        Looks at last 3 swing points: HH/HL = bullish, LH/LL = bearish.
        """
        candles = list(self._htf_candles)
        if len(candles) < 5:
            return 0

        # Simple approach: compare last 3 pivot highs and lows
        highs = [c.high for c in candles]
        lows  = [c.low  for c in candles]

        last_high  = highs[-1]
        prev_high  = max(highs[-4:-1])
        last_low   = lows[-1]
        prev_low   = min(lows[-4:-1])

        bullish_struct = last_high > prev_high and last_low > prev_low
        bearish_struct = last_high < prev_high and last_low < prev_low

        if bullish_struct:
            return 1
        if bearish_struct:
            return -1
        return 0

    def _vwap_score(self, htf_candle: Candle) -> int:
        if self._vwap is None:
            return 0
        if htf_candle.close > self._vwap:
            return 1
        if htf_candle.close < self._vwap:
            return -1
        return 0

    def _liquidity_score(self, htf_candle: Candle) -> int:
        """
        If price is near the recent range low → room to run up → bullish +1.
        If price is near the recent range high → room to run down → bearish -1.
        """
        candles = list(self._htf_candles)
        if len(candles) < 5:
            return 0
        period_high = max(c.high for c in candles)
        period_low  = min(c.low  for c in candles)
        rng = period_high - period_low
        if rng == 0:
            return 0
        pos = (htf_candle.close - period_low) / rng  # 0 = at low, 1 = at high
        if pos < 0.35:
            return 1    # near lows → bullish
        if pos > 0.65:
            return -1   # near highs → bearish
        return 0

    def _session_score(self) -> int:
        if self._session_bias == "bullish":
            return 1
        if self._session_bias == "bearish":
            return -1
        return 0

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_bias(self) -> Optional[HTFBias]:
        return self._latest

    def is_bullish(self) -> bool:
        return self._latest is not None and self._latest.direction == Direction.BULLISH

    def is_bearish(self) -> bool:
        return self._latest is not None and self._latest.direction == Direction.BEARISH

    def current_vwap(self) -> Optional[float]:
        return self._vwap
