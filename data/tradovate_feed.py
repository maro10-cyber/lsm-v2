"""
Tradovate WebSocket feed — authenticates, subscribes to MNQ 1m bars,
and yields Candle objects in real time.

Tradovate market data WebSocket:
  Demo:  wss://demo-d.tradovate.com/v1/websocket
  Live:  wss://live-d.tradovate.com/v1/websocket

Auth flow:
  1. POST /auth/accesstokenrequest  → access_token
  2. WS connect → send authorize frame → subscribeQuote / getChart

Bar subscription:
  {"op": "subscribe", "url": "md/getChart",
   "body": {"symbol": "MNQM4", "chartDescription": {"underlyingType": "MinuteBar",
             "elementSize": 1, "elementSizeUnit": "UnderlyingUnits"},
            "timeRange": {"asMuchAsElements": 1}}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import aiohttp

from core.types import Candle

logger = logging.getLogger(__name__)

# ── Endpoints ──────────────────────────────────────────────────────────────────
AUTH_DEMO = "https://demo-d.tradovate.com/v1/auth/accesstokenrequest"
AUTH_LIVE = "https://live-d.tradovate.com/v1/auth/accesstokenrequest"
WS_DEMO   = "wss://demo-d.tradovate.com/v1/websocket"
WS_LIVE   = "wss://live-d.tradovate.com/v1/websocket"

HEARTBEAT_INTERVAL = 2.5   # seconds — Tradovate requires heartbeat < 3s


class TradovateFeed:
    """
    Async iterator that yields closed 1m Candles from Tradovate WebSocket.

    Usage:
        feed = TradovateFeed(config)
        await feed.authenticate()
        async for candle in feed.candles():
            ...
    """

    def __init__(self, config: dict) -> None:
        tv = config.get("tradovate", {})
        self._username   = tv["username"]
        self._password   = tv["password"]
        self._app_id     = tv.get("app_id", "LSM-v2")
        self._app_version = tv.get("app_version", "1.0")
        self._cid        = tv.get("cid", "")
        self._secret     = tv.get("secret", "")
        self._demo       = tv.get("demo", True)
        self._symbol     = config["symbol"]["name"]   # e.g. "MNQ"

        self._auth_url = AUTH_DEMO if self._demo else AUTH_LIVE
        self._ws_url   = WS_DEMO   if self._demo else WS_LIVE
        self._token: Optional[str] = None
        self._token_expiry: float  = 0.0

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        payload = {
            "name":       self._username,
            "password":   self._password,
            "appId":      self._app_id,
            "appVersion": self._app_version,
            "cid":        self._cid,
            "sec":        self._secret,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self._auth_url, json=payload) as resp:
                data = await resp.json()
                if "accessToken" not in data:
                    raise RuntimeError(f"Tradovate auth failed: {data}")
                self._token = data["accessToken"]
                # expirationTime is ISO string
                exp = data.get("expirationTime", "")
                try:
                    dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    self._token_expiry = dt.timestamp()
                except Exception:
                    self._token_expiry = time.time() + 3600
                logger.info(f"Tradovate authenticated | demo={self._demo} | expires={exp}")

    # ── WebSocket feed ─────────────────────────────────────────────────────────

    async def candles(self) -> AsyncIterator[Candle]:
        if not self._token:
            await self.authenticate()

        req_id = 1

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self._ws_url) as ws:
                # 1. Authorize
                await ws.send_str(
                    f"authorize\n{req_id}\n\n{self._token}"
                )
                req_id += 1

                # 2. Subscribe to 1m chart
                chart_req = {
                    "symbol": self._symbol,
                    "chartDescription": {
                        "underlyingType": "MinuteBar",
                        "elementSize": 1,
                        "elementSizeUnit": "UnderlyingUnits",
                    },
                    "timeRange": {"asMuchAsElements": 2},  # last 2 bars to seed
                }
                subscribe_msg = f"md/subscribeChart\n{req_id}\n\n{json.dumps(chart_req)}"
                await ws.send_str(subscribe_msg)
                req_id += 1

                # 3. Heartbeat task
                async def heartbeat():
                    while True:
                        await asyncio.sleep(HEARTBEAT_INTERVAL)
                        try:
                            await ws.send_str("[]")
                        except Exception:
                            break

                hb_task = asyncio.create_task(heartbeat())

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            candle = self._parse_message(msg.data)
                            if candle:
                                yield candle
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"WS closed/error: {msg}")
                            break
                finally:
                    hb_task.cancel()

    # ── Parser ─────────────────────────────────────────────────────────────────

    def _parse_message(self, raw: str) -> Optional[Candle]:
        """
        Tradovate sends frames like:
          a[{"e":"md","d":{"charts":[{"id":1,"td":20240102,"ts":"2024-01-02T14:30:00.000Z",
             "open":16545.75,"high":16548.5,"low":16544.0,"close":16547.25,"upVolume":123,
             "downVolume":87,"upTicks":45,"downTicks":30,"bidVolume":87,"offerVolume":123}]}}]
        """
        if not raw or raw == "h" or raw == "[]":
            return None
        try:
            # Frames start with 'a' for array
            if raw.startswith("a"):
                frames = json.loads(raw[1:])
            else:
                return None

            for frame in frames:
                event = frame.get("e", "")
                if event != "md":
                    continue
                charts = frame.get("d", {}).get("charts", [])
                for chart in charts:
                    ts_str = chart.get("ts")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    return Candle(
                        timestamp=ts.astimezone(timezone.utc),
                        open=float(chart["open"]),
                        high=float(chart["high"]),
                        low=float(chart["low"]),
                        close=float(chart["close"]),
                        volume=float(chart.get("upVolume", 0) + chart.get("downVolume", 0)),
                    )
        except Exception as e:
            logger.debug(f"Parse error: {e} | raw={raw[:120]}")
        return None
