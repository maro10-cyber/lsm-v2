"""
Backtest runner — wires all engines, replays CSV, produces a trade list.

Flow per 1m candle:
  1. SessionEngine.update()        → detect session open/close
  2. LiquidityEngine.update_session_levels() if levels changed
  3. LiquidityEngine.detect_equal_levels()
  4. LiquidityEngine.process_candle() → new_sweeps
  5. StructureEngine.update()
  6. FVGEngine.update()
  7. HTFAggregator.update() → if new 15m closes: BiasEngine.update()
  8. StateMachine.tick(candle, bias_direction, new_sweeps)
  9. For each ready entry: RiskManager.size → PaperBroker.submit_entry
 10. For each open trade:  PaperBroker.update() → close if SL/TP hit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from core.types import CloseReason, Direction, Trade
from data.feed import CSVFeed, HTFAggregator
from execution.paper import PaperBroker
from risk.manager import RiskManager
from strategy.bias import BiasEngine
from strategy.fvg import FVGEngine
from strategy.liquidity import LiquidityEngine
from strategy.session import SessionEngine
from strategy.state_machine import StateMachine
from strategy.structure import StructureEngine

logger = logging.getLogger(__name__)


class BacktestRunner:

    def __init__(self, config: dict) -> None:
        self._cfg = config

        self._session  = SessionEngine(config)
        self._liquidity = LiquidityEngine(config)
        self._structure = StructureEngine(config)
        self._fvg_eng   = FVGEngine(config)
        self._bias_eng  = BiasEngine(config)
        self._htf_agg   = HTFAggregator(minutes=15)
        self._sm        = StateMachine(config, self._liquidity, self._structure, self._fvg_eng)
        self._risk      = RiskManager(config)
        self._broker    = PaperBroker(config)

        self._open_trades: List[Trade] = []
        self._pending_trades: List[Trade] = []   # filled this bar, managed next bar
        self._closed_trades: List[Trade] = []
        self._htf_bias_dir: Optional[Direction] = None

        # Session filter
        flt = config.get("filters", {})
        self._session_filter = flt.get("session_filter", "NY_ONLY")

    async def run(self, csv_path: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> List[Trade]:
        feed = CSVFeed(csv_path, start=start, end=end)
        bar_count = 0

        async for candle in feed.candles():
            bar_count += 1
            self._process_bar(candle)

        # Force-close any still-open trades at last price
        # (they'd have no close price otherwise)
        last_price = None
        async for candle in CSVFeed(csv_path, start=start, end=end).candles():
            last_price = candle.close
        if last_price:
            for trade in list(self._open_trades) + list(self._pending_trades):
                closed = self._broker.force_close(trade, last_price)
                self._on_trade_closed(closed)

        logger.info(
            f"Backtest complete | bars={bar_count} trades={len(self._closed_trades)}"
        )
        return self._closed_trades

    def _process_bar(self, candle) -> None:
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

        # 7. Session gate
        if not self._session.is_tradeable(candle.timestamp, self._session_filter):
            # Still manage open trades
            self._manage_open_trades(candle)
            return

        # 8. Promote pending trades into the active list (filled on prior bar)
        self._open_trades.extend(self._pending_trades)
        self._pending_trades.clear()

        # 9. Manage open trades (before new fills so same-bar SL doesn't fire)
        self._manage_open_trades(candle)

        # 10. State machine
        self._sm.tick(candle, self._htf_bias_dir, new_sweeps)

        # 11. Execute ready entries — buffered into pending, managed next bar
        entry_ctx = self._sm.pop_ready_entry()
        while entry_ctx is not None:
            if self._risk.can_trade(candle.timestamp):
                qty = self._risk.size_trade(entry_ctx)
                if qty > 0:
                    trade = self._broker.submit_entry(entry_ctx, qty)
                    if trade:
                        self._risk.on_trade_opened()
                        self._pending_trades.append(trade)
                        self._sm.on_trade_opened(entry_ctx)
            entry_ctx = self._sm.pop_ready_entry()

    def _manage_open_trades(self, candle) -> None:
        still_open: List[Trade] = []
        for trade in self._open_trades:
            # Track HTF extremes for BE
            closed = self._broker.update(trade, candle.high, candle.low, candle.close)
            if closed:
                self._on_trade_closed(closed)
            else:
                still_open.append(trade)
        self._open_trades = still_open

    def _on_trade_closed(self, trade: Trade) -> None:
        pnl = trade.pnl
        self._risk.on_trade_closed(pnl)
        self._risk.update_balance(pnl)
        self._closed_trades.append(trade)
        if trade.setup:
            self._sm.on_trade_closed(trade.setup)


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


async def main(csv_path: str, config_path: str, start_str: str, end_str: str) -> None:
    from backtesting.metrics import compute_metrics, print_report

    config = load_config(config_path)

    # Set up logging
    log_cfg = config.get("logging", {})
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    start = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc) if start_str else None
    end   = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)   if end_str   else None

    bt_cfg = config.get("backtest", {})
    initial_balance = bt_cfg.get("initial_balance", config["risk"]["account_balance"])

    runner = BacktestRunner(config)
    trades = await runner.run(csv_path, start=start, end=end)

    metrics = compute_metrics(trades, initial_balance)
    print_report(metrics)

    # Save trade journal
    log_dir = log_cfg.get("log_dir", "logs/")
    os.makedirs(log_dir, exist_ok=True)
    journal_path = os.path.join(log_dir, "trades.json")
    with open(journal_path, "w") as f:
        json.dump([t.to_dict() for t in trades], f, indent=2)
    print(f"Journal saved: {journal_path}")
