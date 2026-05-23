"""
Interactive Brokers real-time feed via ib_insync.

Connects to IB Gateway (running in Docker on port 4002 for paper).
Subscribes to MNQ front-month continuous futures 1m bars.
Yields closed Candle objects as they complete.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from ib_insync import IB, Contract, RealTimeBar, util

from core.types import Candle

logger = logging.getLogger(__name__)

util.logToConsole(logging.WARNING)   # suppress ib_insync noise


class IBKRFeed:
    """
    Async iterator yielding 1m Candles from IB Gateway.

    Uses reqRealTimeBars (5s bars) aggregated to 1m internally,
    so the first complete candle arrives after the first full minute.
    """

    def __init__(self, config: dict) -> None:
        ib_cfg = config.get("ibkr", {})
        self._host    = ib_cfg.get("host", "ib-gateway")   # Docker service name
        self._port    = ib_cfg.get("port", 4002)            # 4002 = paper, 4001 = live
        self._client_id = ib_cfg.get("client_id", 1)
        self._account = ib_cfg.get("account", "")           # e.g. DU1234567

        sym = config["symbol"]
        self._symbol   = sym.get("ibkr_symbol", "MNQ")
        self._exchange = sym.get("ibkr_exchange", "CME")

        self._ib = IB()

        # 1m aggregation state
        self._bar_open:  Optional[float] = None
        self._bar_high:  Optional[float] = None
        self._bar_low:   Optional[float] = None
        self._bar_close: Optional[float] = None
        self._bar_vol:   float = 0.0
        self._bar_minute: Optional[int] = None
        self._bar_ts:    Optional[datetime] = None
        self._queue: asyncio.Queue = asyncio.Queue()

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        max_attempts = 30          # retry for up to ~5 minutes
        delay = 10                 # seconds between attempts
        for attempt in range(1, max_attempts + 1):
            try:
                await self._ib.connectAsync(
                    self._host, self._port, clientId=self._client_id, timeout=20
                )
                logger.info(f"IB Gateway connected | {self._host}:{self._port}")
                return
            except Exception as exc:
                if attempt == max_attempts:
                    raise
                logger.warning(
                    f"IB Gateway not ready (attempt {attempt}/{max_attempts}): {exc} "
                    f"— retrying in {delay}s..."
                )
                await asyncio.sleep(delay)

    async def disconnect(self) -> None:
        self._ib.disconnect()

    # ── Feed ──────────────────────────────────────────────────────────────────

    async def candles(self) -> AsyncIterator[Candle]:
        await self.connect()

        contract = await self._resolve_contract()
        logger.info(f"Subscribed: {contract.localSymbol} @ {contract.exchange}")

        # Subscribe to 5s real-time bars
        bars = self._ib.reqRealTimeBars(
            contract, 5, "TRADES", useRTH=False
        )
        bars.updateEvent += self._on_bar

        try:
            while True:
                # Yield completed 1m candles from the queue
                candle = await self._queue.get()
                yield candle
        finally:
            self._ib.cancelRealTimeBars(bars)
            await self.disconnect()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _resolve_contract(self) -> Contract:
        """Find the front-month MNQ contract."""
        contract = Contract(
            symbol=self._symbol,
            secType="FUT",
            exchange=self._exchange,
            currency="USD",
        )
        contracts = await self._ib.qualifyContractsAsync(contract)
        if not contracts:
            raise RuntimeError(f"No contract found for {self._symbol}")
        # Pick the nearest expiry
        contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
        return contracts[0]

    def _on_bar(self, bars, has_new_bar: bool) -> None:
        """Called every 5 seconds by ib_insync with updated bar data."""
        if not bars:
            return
        bar: RealTimeBar = bars[-1]

        # Bar timestamp (IB gives Unix seconds)
        try:
            ts = datetime.fromtimestamp(bar.time, tz=timezone.utc)
        except Exception:
            return

        minute = ts.hour * 60 + ts.minute

        if self._bar_minute is None:
            # First bar
            self._start_bar(bar, ts, minute)
            return

        if minute == self._bar_minute:
            # Same minute — update running candle
            self._bar_high  = max(self._bar_high,  bar.high)
            self._bar_low   = min(self._bar_low,   bar.low)
            self._bar_close = bar.close
            self._bar_vol  += bar.volume
        else:
            # New minute — emit completed candle
            closed = Candle(
                timestamp=self._bar_ts,
                open=self._bar_open,
                high=self._bar_high,
                low=self._bar_low,
                close=self._bar_close,
                volume=self._bar_vol,
            )
            asyncio.get_event_loop().call_soon_threadsafe(
                self._queue.put_nowait, closed
            )
            logger.debug(
                f"1m candle: {closed.timestamp} O={closed.open} H={closed.high} "
                f"L={closed.low} C={closed.close}"
            )
            self._start_bar(bar, ts, minute)

    def _start_bar(self, bar: RealTimeBar, ts: datetime, minute: int) -> None:
        bar_ts = ts.replace(second=0, microsecond=0)
        self._bar_open  = bar.open
        self._bar_high  = bar.high
        self._bar_low   = bar.low
        self._bar_close = bar.close
        self._bar_vol   = float(bar.volume)
        self._bar_minute = minute
        self._bar_ts    = bar_ts
