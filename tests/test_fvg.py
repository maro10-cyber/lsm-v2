"""Unit tests for FVGEngine."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta, timezone
import pytest

from core.types import Candle, Direction
from strategy.fvg import FVGEngine

CFG = {
    "symbol":  {"tick_size": 0.25, "point_value": 2.0},
    "fvg":     {"min_size_ticks": 4, "max_age_bars": 50, "require_unmitigated": True},
}

BASE_TS = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)


def _c(i, o, h, l, c):
    return Candle(timestamp=BASE_TS + timedelta(minutes=i), open=o, high=h, low=l, close=c)


def test_bullish_fvg_detected():
    eng = FVGEngine(CFG)
    # c0: high=100, c1: anything, c2: low=101 → gap [100, 101]
    c0 = _c(0, 99, 100, 98, 99.5)
    c1 = _c(1, 99.5, 102, 99, 100)
    c2 = _c(2, 100.5, 103, 101, 102)

    eng.update(c0)
    eng.update(c1)
    fvg = eng.update(c2)

    assert fvg is not None
    assert fvg.direction == Direction.BULLISH
    assert fvg.bottom == c0.high   # 100
    assert fvg.top    == c2.low    # 101


def test_bearish_fvg_detected():
    eng = FVGEngine(CFG)
    c0 = _c(0, 101, 103, 101, 102)   # low=101
    c1 = _c(1, 102, 102, 98, 99)
    c2 = _c(2, 100, 100, 97, 98)     # high=100

    eng.update(c0)
    eng.update(c1)
    fvg = eng.update(c2)

    assert fvg is not None
    assert fvg.direction == Direction.BEARISH
    assert fvg.top    == c0.low    # 101
    assert fvg.bottom == c2.high   # 100


def test_fvg_too_small_rejected():
    eng = FVGEngine(CFG)
    # Gap of only 0.5 (< 4 * 0.25 = 1.0)
    c0 = _c(0, 99, 100.0, 98, 99.5)
    c1 = _c(1, 99.5, 101, 99, 100)
    c2 = _c(2, 100.2, 103, 100.5, 102)

    eng.update(c0)
    eng.update(c1)
    fvg = eng.update(c2)
    assert fvg is None


def test_fvg_invalidated_by_close_through():
    eng = FVGEngine(CFG)
    c0 = _c(0, 99, 100, 98, 99.5)
    c1 = _c(1, 99.5, 102, 99, 100)
    c2 = _c(2, 100.5, 103, 101, 102)   # bullish FVG bottom=100, top=101

    eng.update(c0)
    eng.update(c1)
    fvg = eng.update(c2)
    assert fvg is not None

    # Candle that closes below FVG bottom → invalidated
    close_through = _c(3, 101, 101.5, 97, 99)   # close=99 < bottom=100
    eng.update(close_through)

    active = eng.get_active_fvgs(Direction.BULLISH)
    assert len(active) == 0
