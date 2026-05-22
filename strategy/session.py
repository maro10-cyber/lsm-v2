"""
Session Engine — tracks Asia / London / NY ranges and Previous Day levels.

Session times (NY timezone):
    Asia     00:00 → 07:00
    London   07:00 → 09:30
    New York 09:30 → 16:00

Produced levels feed directly into the Liquidity Engine.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pytz

from core.types import Candle, SessionRange, SessionType

logger = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")


def _to_ny(dt: datetime) -> datetime:
    return dt.astimezone(NY_TZ)


def _ny_date(dt: datetime) -> date:
    return _to_ny(dt).date()


def _session_window(
    day: date,
    start_hhmm: str,
    end_hhmm: str,
) -> Tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for a session on a given NY calendar day."""
    sh, sm = int(start_hhmm[:2]), int(start_hhmm[3:])
    eh, em = int(end_hhmm[:2]),   int(end_hhmm[3:])
    naive_start = datetime(day.year, day.month, day.day, sh, sm)
    naive_end   = datetime(day.year, day.month, day.day, eh, em)
    start_ny    = NY_TZ.localize(naive_start, is_dst=None)
    end_ny      = NY_TZ.localize(naive_end,   is_dst=None)
    return start_ny.astimezone(pytz.utc), end_ny.astimezone(pytz.utc)


class SessionEngine:
    """
    Processes 1m candles and maintains session high/low ranges.

    Call update(candle) on every 1m candle.
    Call get_levels() to retrieve current session ranges for the Liquidity Engine.
    """

    SESSION_TIMES = {
        SessionType.ASIA:     ("00:00", "07:00"),
        SessionType.LONDON:   ("07:00", "09:30"),
        SessionType.NEW_YORK: ("09:30", "16:00"),
    }

    def __init__(self, config: dict) -> None:
        self._cfg = config.get("sessions", {})
        tz_str = self._cfg.get("timezone", "America/New_York")
        global NY_TZ
        NY_TZ = pytz.timezone(tz_str)

        # Override session times from config if present
        for key, stype in [("asia", SessionType.ASIA), ("london", SessionType.LONDON), ("new_york", SessionType.NEW_YORK)]:
            if key in self._cfg:
                self.SESSION_TIMES[stype] = (self._cfg[key]["start"], self._cfg[key]["end"])

        self._active:   Dict[SessionType, SessionRange] = {}
        self._completed: List[SessionRange] = []
        self._prev_day: Optional[SessionRange] = None
        self._current_day: Optional[str] = None

    def update(self, candle: Candle) -> List[SessionRange]:
        """
        Process one 1m candle.
        Returns list of SessionRanges newly completed this bar.
        """
        ny_day  = _ny_date(candle.timestamp)
        day_str = ny_day.strftime("%Y-%m-%d")
        newly_closed: List[SessionRange] = []

        self._check_day_rollover(ny_day, day_str)

        for stype, (start_str, end_str) in self.SESSION_TIMES.items():
            result = self._update_session(stype, candle, ny_day, start_str, end_str)
            if result:
                newly_closed.append(result)

        return newly_closed

    def _check_day_rollover(self, ny_day: date, day_str: str) -> None:
        if self._current_day == day_str:
            return

        if self._current_day is not None:
            # Archive previous NY session as "previous day"
            for rng in reversed(self._completed):
                if rng.session == SessionType.NEW_YORK and rng.is_complete:
                    self._prev_day = rng
                    break
            if self._prev_day:
                logger.debug(f"Prev-day updated: H={self._prev_day.high} L={self._prev_day.low} ({self._current_day})")

        self._current_day = day_str
        self._active.pop(SessionType.NEW_YORK, None)

    def _update_session(
        self,
        stype: SessionType,
        candle: Candle,
        ny_day: date,
        start_str: str,
        end_str: str,
    ) -> Optional[SessionRange]:
        start_utc, end_utc = _session_window(ny_day, start_str, end_str)
        ts = candle.timestamp
        within = start_utc <= ts < end_utc
        active  = self._active.get(stype)
        closed  = None

        if within:
            if active is None:
                active = SessionRange(
                    session=stype,
                    date=ny_day.strftime("%Y-%m-%d"),
                    high=candle.high,
                    low=candle.low,
                    high_time=ts,
                    low_time=ts,
                    open_price=candle.open,
                    close_price=candle.close,
                )
                self._active[stype] = active
            else:
                if candle.high > active.high:
                    active.high = candle.high
                    active.high_time = ts
                if candle.low < active.low:
                    active.low = candle.low
                    active.low_time = ts
                active.close_price = candle.close
        else:
            if active is not None and not active.is_complete:
                active.is_complete = True
                self._completed.append(active)
                del self._active[stype]
                closed = active
                logger.info(
                    f"{stype.value} closed | H={active.high} L={active.low} | {active.date}"
                )
                if len(self._completed) > 30:
                    self._completed = self._completed[-30:]

        return closed

    # ── Public API ────────────────────────────────────────────────────────────

    def get_levels(self) -> Dict[str, Optional[SessionRange]]:
        """Return the most recent completed range per session + ny_active."""
        def last(st: SessionType) -> Optional[SessionRange]:
            for r in reversed(self._completed):
                if r.session == st:
                    return r
            return None

        return {
            "asia":         last(SessionType.ASIA),
            "london":       last(SessionType.LONDON),
            "previous_day": self._prev_day,
            "ny_active":    self._active.get(SessionType.NEW_YORK),
        }

    def is_in_session(self, ts: datetime, session: str) -> bool:
        """True if timestamp is inside the given session (e.g. 'new_york')."""
        stype_map = {
            "asia": SessionType.ASIA,
            "london": SessionType.LONDON,
            "new_york": SessionType.NEW_YORK,
        }
        stype = stype_map.get(session)
        if stype is None:
            return False
        start_str, end_str = self.SESSION_TIMES[stype]
        start, end = _session_window(_ny_date(ts), start_str, end_str)
        return start <= ts < end

    def is_tradeable(self, ts: datetime, session_filter: str) -> bool:
        """True if the timestamp is within the configured tradeable window."""
        if session_filter == "ALL":
            return True
        if session_filter == "NY_ONLY":
            return self.is_in_session(ts, "new_york")
        if session_filter == "NY_AND_LONDON":
            return self.is_in_session(ts, "new_york") or self.is_in_session(ts, "london")
        return True

    def get_daily_bias(self) -> str:
        """Simple daily bias from previous NY session candle direction."""
        if self._prev_day is None:
            return "neutral"
        if self._prev_day.close_price > self._prev_day.open_price:
            return "bullish"
        if self._prev_day.close_price < self._prev_day.open_price:
            return "bearish"
        return "neutral"
