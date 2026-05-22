"""Unit tests for SessionEngine."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone
import pytest
import pytz

from core.types import SessionType
from strategy.session import SessionEngine

NY_TZ = pytz.timezone("America/New_York")


def _candle(ts_ny_str: str, h=100.0, l=99.0, o=99.5, c=99.5):
    from core.types import Candle
    naive = datetime.strptime(ts_ny_str, "%Y-%m-%d %H:%M")
    ny_ts = NY_TZ.localize(naive)
    utc_ts = ny_ts.astimezone(timezone.utc)
    return Candle(timestamp=utc_ts, open=o, high=h, low=l, close=c)


CFG = {
    "sessions": {
        "timezone": "America/New_York",
        "asia":     {"start": "00:00", "end": "07:00"},
        "london":   {"start": "07:00", "end": "09:30"},
        "new_york": {"start": "09:30", "end": "16:00"},
    }
}


def test_asia_session_updates():
    eng = SessionEngine(CFG)
    c = _candle("2024-01-02 01:00", h=101.0, l=98.0)
    eng.update(c)
    active = eng._active
    assert SessionType.ASIA in active
    rng = active[SessionType.ASIA]
    assert rng.high == 101.0
    assert rng.low == 98.0


def test_session_closes_on_boundary():
    eng = SessionEngine(CFG)
    # Feed bar inside Asia
    eng.update(_candle("2024-01-02 06:58", h=102.0, l=97.0))
    # Feed bar OUTSIDE Asia (07:00 NY)
    closed = eng.update(_candle("2024-01-02 07:00", h=103.0, l=98.0))
    assert len(closed) >= 1
    asia_closed = [r for r in closed if r.session == SessionType.ASIA]
    assert len(asia_closed) == 1
    assert asia_closed[0].is_complete is True


def test_is_tradeable_ny_only():
    eng = SessionEngine(CFG)
    # NY hours
    ny_bar = _candle("2024-01-02 10:00")
    assert eng.is_tradeable(ny_bar.timestamp, "NY_ONLY") is True
    # Asia hours
    asia_bar = _candle("2024-01-02 02:00")
    assert eng.is_tradeable(asia_bar.timestamp, "NY_ONLY") is False


def test_daily_bias_neutral_without_prev_day():
    eng = SessionEngine(CFG)
    assert eng.get_daily_bias() == "neutral"
