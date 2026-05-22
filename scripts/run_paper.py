"""
Live paper trading runner — connects to Tradovate WebSocket, runs the
full strategy stack, executes paper orders, logs every signal and trade.

Usage:
    python scripts/run_paper.py --config configs/config.yaml

Environment variables (override config credentials):
    TV_USERNAME   Tradovate username
    TV_PASSWORD   Tradovate password
    TV_CID        Tradovate client ID
    TV_SECRET     Tradovate secret
    TV_DEMO       "true" (default) | "false"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.types import Direction, Trade
from data.feed import HTFAggregator
from data.ibkr_feed import IBKRFeed
from execution.paper import PaperBroker
from risk.manager import RiskManager
from strategy.bias import BiasEngine
from strategy.fvg import FVGEngine
from strategy.liquidity import LiquidityEngine
from strategy.session import SessionEngine
from strategy.state_machine import StateMachine
from strategy.structure import StructureEngine

logger = logging.getLogger(__name__)


class PaperTrader:

    def __init__(self, config: dict) -> None:
        self._cfg = config

        self._session   = SessionEngine(config)
        self._liquidity = LiquidityEngine(config)
        self._structure = StructureEngine(config)
        self._fvg_eng   = FVGEngine(config)
        self._bias_eng  = BiasEngine(config)
        self._htf_agg   = HTFAggregator(minutes=15)
        self._sm        = StateMachine(config, self._liquidity, self._structure, self._fvg_eng)
        self._risk      = RiskManager(config)
        self._broker    = PaperBroker(config)

        flt = config.get("filters", {})
        self._session_filter = flt.get("session_filter", "NY_ONLY")

        self._open_trades:   list[Trade] = []
        self._pending_trades: list[Trade] = []
        self._closed_trades: list[Trade] = []
        self._htf_bias_dir  = None

        # Journal file
        log_cfg  = config.get("logging", {})
        log_dir  = log_cfg.get("log_dir", "logs/")
        os.makedirs(log_dir, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        self._journal_path = os.path.join(log_dir, f"paper_{today}.json")
        self._bar_count = 0

    async def run(self, feed: TradovateFeed) -> None:
        logger.info("Paper trader started — waiting for candles...")
        async for candle in feed.candles():
            self._bar_count += 1
            self._process_bar(candle)

    def _process_bar(self, candle) -> None:
        ts = candle.timestamp

        # 1. Sessions
        newly_closed = self._session.update(candle)
        if newly_closed:
            levels = self._session.get_levels()
            self._liquidity.update_session_levels(levels)
            self._bias_eng.set_session_bias(self._session.get_daily_bias())

        # 2. Equal levels
        self._liquidity.detect_equal_levels(candle)

        # 3. Sweeps
        new_sweeps = self._liquidity.process_candle(candle)

        # 4. Structure
        self._structure.update(candle)

        # 5. FVG
        self._fvg_eng.update(candle)

        # 6. HTF bias
        htf_candle = self._htf_agg.update(candle)
        if htf_candle:
            bias = self._bias_eng.update(htf_candle)
            self._htf_bias_dir = bias.direction
            logger.info(f"HTF Bias: {bias.direction} (score={bias.score}) @ {ts}")

        # 7. Promote pending trades
        self._open_trades.extend(self._pending_trades)
        self._pending_trades.clear()

        # 8. Manage open trades
        self._manage_open_trades(candle)

        # 9. Session gate
        if not self._session.is_tradeable(ts, self._session_filter):
            return

        # 10. State machine
        self._sm.tick(candle, self._htf_bias_dir, new_sweeps)

        # 11. Execute entries
        entry_ctx = self._sm.pop_ready_entry()
        while entry_ctx is not None:
            if self._risk.can_trade(ts):
                qty = self._risk.size_trade(entry_ctx)
                if qty > 0:
                    trade = self._broker.submit_entry(entry_ctx, qty)
                    if trade:
                        self._risk.on_trade_opened()
                        self._pending_trades.append(trade)
                        self._sm.on_trade_opened(entry_ctx)
                        self._log_entry(trade, ts)
            entry_ctx = self._sm.pop_ready_entry()

    def _manage_open_trades(self, candle) -> None:
        still_open = []
        for trade in self._open_trades:
            closed = self._broker.update(trade, candle.high, candle.low, candle.close)
            if closed:
                self._on_trade_closed(closed)
            else:
                still_open.append(trade)
        self._open_trades = still_open

    def _on_trade_closed(self, trade: Trade) -> None:
        self._risk.on_trade_closed(trade.pnl)
        self._risk.update_balance(trade.pnl)
        self._closed_trades.append(trade)
        if trade.setup:
            self._sm.on_trade_closed(trade.setup)
        self._log_close(trade)
        self._save_journal()

    def _log_entry(self, trade: Trade, ts) -> None:
        logger.info(
            f"[PAPER ENTRY] {trade.direction.value.upper()} | "
            f"E={trade.entry_price} SL={trade.sl_price} TP={trade.tp_price} "
            f"qty={trade.quantity} risk={self._risk.current_risk_pct*100:.1f}% | {ts}"
        )

    def _log_close(self, trade: Trade) -> None:
        result = "WIN" if trade.pnl > 0 else "LOSS"
        logger.info(
            f"[PAPER CLOSE] {result} | {trade.direction.value.upper()} | "
            f"E={trade.entry_price} X={trade.exit_price} "
            f"PnL=${trade.pnl:.2f} reason={trade.close_reason.value} | "
            f"Balance=${self._risk.account_balance:.2f} DailyPnL=${self._risk.daily_pnl:.2f}"
        )

    def _save_journal(self) -> None:
        all_trades = self._closed_trades
        with open(self._journal_path, "w") as f:
            json.dump([t.to_dict() for t in all_trades], f, indent=2)

    def print_status(self) -> None:
        wins   = sum(1 for t in self._closed_trades if t.pnl > 0)
        total  = len(self._closed_trades)
        wr     = (wins / total * 100) if total else 0
        pnl    = sum(t.pnl for t in self._closed_trades)
        open_n = len(self._open_trades) + len(self._pending_trades)
        print(
            f"\n--- Paper Status | bars={self._bar_count} trades={total} "
            f"WR={wr:.1f}% PnL=${pnl:.2f} open={open_n} "
            f"balance=${self._risk.account_balance:.2f} ---"
        )


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Allow env var overrides for credentials
    tv = cfg.setdefault("tradovate", {})
    if os.getenv("TV_USERNAME"):  tv["username"] = os.environ["TV_USERNAME"]
    if os.getenv("TV_PASSWORD"):  tv["password"] = os.environ["TV_PASSWORD"]
    if os.getenv("TV_CID"):       tv["cid"]      = os.environ["TV_CID"]
    if os.getenv("TV_SECRET"):    tv["secret"]   = os.environ["TV_SECRET"]
    if os.getenv("TV_DEMO"):      tv["demo"]     = os.environ["TV_DEMO"].lower() != "false"
    return cfg


async def main(config_path: str) -> None:
    config = load_config(config_path)

    log_cfg = config.get("logging", {})
    log_dir = log_cfg.get("log_dir", "logs/")
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(log_dir, f"paper_{datetime.now().strftime('%Y%m%d')}.log")
            ),
        ],
    )

    trader = PaperTrader(config)
    feed   = IBKRFeed(config)

    # Graceful shutdown on Ctrl+C / SIGTERM
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _shutdown(sig, frame):
        logger.info(f"Shutting down ({sig})...")
        trader.print_status()
        trader._save_journal()
        loop.call_soon_threadsafe(stop.set_result, None)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_task = asyncio.create_task(trader.run(feed))
    await asyncio.wait([run_task, stop], return_when=asyncio.FIRST_COMPLETED)
    run_task.cancel()
    logger.info("Paper trader stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSM-v2 Paper Trader")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()
    asyncio.run(main(args.config))
