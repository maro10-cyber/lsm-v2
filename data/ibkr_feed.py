"""
Interactive Brokers real-time feed via ib_insync.

Connects to IB Gateway (running in Docker on port 4004 for paper).
Uses reqHistoricalData(keepUpToDate=True) for 1m bars — this path works
without a paid CME real-time data subscription.

On startup it replays the last 2 trading days so the strategy engines
(equal levels, structure, HTF bias) have historical context immediately.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from ib_insync import IB, BarData, Contract, util

from core.types import Candle

logger = logging.getLogger(__name__)

util.logToConsole(logging.WARNING)   # suppress ib_insync noise


class IBKRFeed:
    """
    Async iterator yielding 1m Candles from IB Gateway.

    Uses reqHistoricalData(keepUpToDate=True) which fires an updateEvent
    callback each time a new 1m bar closes.  On startup it also replays
    the last 2 trading days of history so the strategy has context.
    """

    def __init__(self, config: dict) -> None:
        ib_cfg = config.get("ibkr", {})
        self._host      = ib_cfg.get("host", "ib-gateway")
        self._port      = ib_cfg.get("port", 4004)
        self._client_id = ib_cfg.get("client_id", 1)
        self._account   = ib_cfg.get("account", "")

        sym = config["symbol"]
        self._symbol   = sym.get("ibkr_symbol", "MNQ")
        self._exchange = sym.get("ibkr_exchange", "CME")

        self._ib   = IB()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: asyncio.Queue = asyncio.Queue()

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        max_attempts = 30
        delay = 10
        for attempt in range(1, max_attempts + 1):
            try:
                await self._ib.connectAsync(
                    self._host, self._port, clientId=self._client_id, timeout=20
                )
                self._loop = asyncio.get_running_loop()
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

        # keepUpToDate=True: IB sends historical bars then keeps the list
        # live, firing updateEvent(bars, has_new_bar) each time a bar closes.
        logger.info("Subscribing to 1m historical bars (keepUpToDate)...")
        bars_list = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="2 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            keepUpToDate=True,
        )
        bars_list.updateEvent += self._on_bar_update
        logger.info(
            f"Subscription active — replaying {len(bars_list)} historical bars, "
            "then live..."
        )

        # Replay historical bars immediately so engines have context
        for bar in bars_list[:-1]:   # skip last (current, incomplete) bar
            candle = self._bar_to_candle(bar)
            if candle:
                yield candle

        logger.info("Historical replay done — entering live 1m candle loop...")
        try:
            while True:
                candle = await self._queue.get()
                yield candle
        finally:
            self._ib.cancelHistoricalData(bars_list)
            await self.disconnect()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _resolve_contract(self) -> Contract:
        """Return the front-month MNQ contract."""
        contract = Contract(
            symbol=self._symbol,
            secType="FUT",
            exchange=self._exchange,
            currency="USD",
        )
        details = await self._ib.reqContractDetailsAsync(contract)
        if not details:
            raise RuntimeError(f"No contract found for {self._symbol}")
        details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        front = details[0].contract
        logger.info(
            f"Front-month: {front.localSymbol} "
            f"expires={front.lastTradeDateOrContractMonth} conId={front.conId}"
        )
        return front

    def _on_bar_update(self, bars, has_new_bar: bool) -> None:
        """Called by ib_insync when the bar list updates.

        has_new_bar=True means a new bar just opened, so bars[-2] is the
        bar that just completed.
        """
        if not has_new_bar or len(bars) < 2:
            return
        candle = self._bar_to_candle(bars[-2])
        if candle and self._loop:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, candle)
            logger.debug(
                f"1m candle: {candle.timestamp} O={candle.open} "
                f"H={candle.high} L={candle.low} C={candle.close}"
            )

    @staticmethod
    def _bar_to_candle(bar: BarData) -> Optional[Candle]:
        try:
            d = bar.date
            if isinstance(d, datetime):
                ts = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            else:
                ts = datetime.fromtimestamp(float(d), tz=timezone.utc)
            return Candle(
                timestamp=ts,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
            )
        except Exception:
            return None
