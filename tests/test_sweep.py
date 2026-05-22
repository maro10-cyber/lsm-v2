"""Unit tests for LiquidityEngine sweep detection."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone
import pytest

from core.types import Candle, LiquidityZone, Direction
from strategy.liquidity import LiquidityEngine

CFG = {
    "symbol":    {"tick_size": 0.25, "point_value": 2.0},
    "liquidity": {
        "equal_tolerance_ticks": 2,
        "sweep_min_ticks": 4,
        "rejection_ratio": 1.5,
        "lookback_bars": 50,
    },
}

TS = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)


def _candle(o, h, l, c):
    return Candle(timestamp=TS, open=o, high=h, low=l, close=c)


def _zone(price, zone_type="session_high"):
    return LiquidityZone(zone_type=zone_type, price=price, created_at=TS)


def test_bearish_sweep_detected():
    eng = LiquidityEngine(CFG)
    eng._zones.append(_zone(100.0, "session_high"))
    # High 1.5 pts (6 ticks) above level, close below → bearish sweep
    candle = _candle(o=99.5, h=101.5, l=99.0, c=99.2)
    sweeps = eng.process_candle(candle)
    assert len(sweeps) == 1
    assert sweeps[0].direction == Direction.BEARISH
    assert sweeps[0].penetration >= 4 * 0.25


def test_bearish_sweep_requires_min_penetration():
    eng = LiquidityEngine(CFG)
    eng._zones.append(_zone(100.0, "session_high"))
    # Only 1 tick above level (< 4 tick min)
    candle = _candle(o=99.5, h=100.25, l=99.0, c=99.2)
    sweeps = eng.process_candle(candle)
    assert len(sweeps) == 0


def test_bullish_sweep_detected():
    eng = LiquidityEngine(CFG)
    eng._zones.append(_zone(99.0, "session_low"))
    # Low below level, close above → bullish sweep
    candle = _candle(o=99.5, h=100.0, l=97.9, c=99.5)
    sweeps = eng.process_candle(candle)
    assert len(sweeps) == 1
    assert sweeps[0].direction == Direction.BULLISH


def test_zone_only_swept_once():
    eng = LiquidityEngine(CFG)
    eng._zones.append(_zone(100.0, "session_high"))
    candle = _candle(o=99.5, h=101.5, l=99.0, c=99.2)
    sweeps1 = eng.process_candle(candle)
    sweeps2 = eng.process_candle(candle)
    assert len(sweeps1) == 1
    assert len(sweeps2) == 0   # already swept


def test_rejection_flag():
    eng = LiquidityEngine(CFG)
    eng._zones.append(_zone(100.0, "session_high"))
    # Large upper wick: open=99, close=99.2, high=101 → upper_wick=1.8, body=0.2
    candle = _candle(o=99.0, h=101.0, l=98.5, c=99.2)
    sweeps = eng.process_candle(candle)
    assert sweeps[0].rejected is True
