"""
Data feed — CSV replay (backtest) and live websocket (paper/live).

CSV format expected (header row):
    timestamp,open,high,low,close,volume

Timestamps must be UTC ISO-8601 or Unix seconds.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from core.types import Candle

logger = logging.getLogger(__name__)


class CSVFeed:
    """
    Async generator that yields 1m Candles from a CSV file.
    Skips rows outside the optional date range.
    """

    def __init__(
        self,
        path: str | Path,
        start: Optional[datetime] = None,
        end:   Optional[datetime] = None,
    ) -> None:
        self._path  = Path(path)
        self._start = start
        self._end   = end

    async def candles(self) -> AsyncIterator[Candle]:
        with open(self._path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = self._parse_ts(row.get("timestamp") or row.get("timestamp_utc") or "")
                if ts is None:
                    continue
                if self._start and ts < self._start:
                    continue
                if self._end and ts >= self._end:
                    break
                yield Candle(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0) or 0),
                )

    @staticmethod
    def _parse_ts(value: str) -> Optional[datetime]:
        value = value.strip()
        if not value:
            return None
        # Unix seconds
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except ValueError:
            pass
        # ISO-8601 with or without timezone offset (Python 3.7+ fromisoformat)
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
        # Fallback strptime formats
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        logger.warning(f"Cannot parse timestamp: {value}")
        return None


class HTFAggregator:
    """
    Aggregates 1m candles into N-minute candles on the fly.
    Call update(candle_1m) — returns a closed HTF candle or None.
    """

    def __init__(self, minutes: int) -> None:
        self._minutes = minutes
        self._current: Optional[Candle] = None
        self._bar_start: Optional[datetime] = None

    def update(self, c: Candle) -> Optional[Candle]:
        """Returns a closed HTF candle when a new period starts, else None."""
        # Determine which HTF bar this 1m candle belongs to
        total_minutes = c.timestamp.hour * 60 + c.timestamp.minute
        bar_start_min = (total_minutes // self._minutes) * self._minutes
        bar_hour = bar_start_min // 60
        bar_min  = bar_start_min % 60

        bar_start = c.timestamp.replace(hour=bar_hour, minute=bar_min, second=0, microsecond=0)

        if self._bar_start is None:
            self._bar_start = bar_start
            self._current   = Candle(
                timestamp=bar_start,
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
            )
            return None

        if bar_start == self._bar_start:
            # Same bar — update
            self._current.high   = max(self._current.high, c.high)
            self._current.low    = min(self._current.low,  c.low)
            self._current.close  = c.close
            self._current.volume += c.volume
            return None

        # New bar — emit closed candle
        closed = self._current
        self._bar_start = bar_start
        self._current   = Candle(
            timestamp=bar_start,
            open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
        )
        return closed
