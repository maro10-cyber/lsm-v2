"""
Paper broker — fills at candle.close + slippage (backtest) or immediate tick.

Fill price  = entry_price + slippage_ticks * tick_size  (longs)
            = entry_price - slippage_ticks * tick_size  (shorts)

Break-even management:
  When price hits 1:1 R, SL moves to entry + 2 ticks (commission buffer).
  This is handled inside update() on every bar.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from core.types import CloseReason, Direction, SetupContext, Trade
from execution.base import BrokerBase

logger = logging.getLogger(__name__)


class PaperBroker(BrokerBase):

    def __init__(self, config: dict) -> None:
        exec_cfg  = config.get("execution", {})
        risk_cfg  = config.get("risk", {})
        sym_cfg   = config.get("symbol", {})

        self._tick_size    = sym_cfg.get("tick_size", 0.25)
        self._point_value  = sym_cfg.get("point_value", 2.0)
        self._slippage     = exec_cfg.get("slippage_ticks", 1) * self._tick_size
        self._commission   = exec_cfg.get("commission_per_side", 0.50)

        # Break-even config
        self._be_trigger_rr   = risk_cfg.get("be_trigger_rr", 1.0)
        self._be_buffer_ticks = risk_cfg.get("be_buffer_ticks", 2)

    # ── BrokerBase ────────────────────────────────────────────────────────────

    def submit_entry(self, ctx: SetupContext, quantity: int) -> Optional[Trade]:
        if quantity <= 0:
            return None

        direction = ctx.sweep.direction
        if direction == Direction.BULLISH:
            fill_price = ctx.entry_price + self._slippage
        else:
            fill_price = ctx.entry_price - self._slippage

        entry_time = ctx.fvg.formed_at if ctx.fvg else ctx.sweep.candle.timestamp
        trade = Trade(
            direction=direction,
            entry_price=fill_price,
            sl_price=ctx.sl_price,
            tp_price=ctx.tp_price,
            quantity=quantity,
            entry_time=entry_time,
            setup=ctx,
        )
        logger.info(
            f"[Paper] FILL {direction.value.upper()} | "
            f"E={fill_price:.2f} SL={ctx.sl_price:.2f} TP={ctx.tp_price:.2f} qty={quantity}"
        )
        return trade

    def update(self, trade: Trade, high: float, low: float, close: float) -> Optional[Trade]:
        """
        Check SL/TP hit and manage break-even.
        Returns closed trade if hit, else None.
        """
        if trade.is_closed:
            return None

        # Track extremes for BE logic
        if trade.last_high is None or high > trade.last_high:
            trade.last_high = high
        if trade.last_low is None or low < trade.last_low:
            trade.last_low = low

        self._maybe_move_be(trade)

        if trade.direction == Direction.BULLISH:
            # SL hit
            if low <= trade.sl_price:
                return self._close_trade(trade, trade.sl_price, CloseReason.STOP_LOSS)
            # TP hit
            if high >= trade.tp_price:
                return self._close_trade(trade, trade.tp_price, CloseReason.TAKE_PROFIT)

        elif trade.direction == Direction.BEARISH:
            if high >= trade.sl_price:
                return self._close_trade(trade, trade.sl_price, CloseReason.STOP_LOSS)
            if low <= trade.tp_price:
                return self._close_trade(trade, trade.tp_price, CloseReason.TAKE_PROFIT)

        return None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _maybe_move_be(self, trade: Trade) -> None:
        if trade.be_moved:
            return

        risk_pts = abs(trade.entry_price - trade.sl_price)
        be_target = self._be_trigger_rr * risk_pts
        buffer = self._be_buffer_ticks * self._tick_size

        if trade.direction == Direction.BULLISH:
            unrealized_pts = trade.last_high - trade.entry_price if trade.last_high else 0
            if unrealized_pts >= be_target:
                trade.sl_price = trade.entry_price + buffer
                trade.be_moved = True
                logger.debug(
                    f"[Paper] BE moved to {trade.sl_price:.2f} (long)"
                )

        elif trade.direction == Direction.BEARISH:
            unrealized_pts = trade.entry_price - trade.last_low if trade.last_low else 0
            if unrealized_pts >= be_target:
                trade.sl_price = trade.entry_price - buffer
                trade.be_moved = True
                logger.debug(
                    f"[Paper] BE moved to {trade.sl_price:.2f} (short)"
                )

    def _close_trade(self, trade: Trade, exit_price: float, reason: CloseReason) -> Trade:
        direction_mult = 1 if trade.direction == Direction.BULLISH else -1
        pts   = (exit_price - trade.entry_price) * direction_mult
        gross = pts * self._point_value * trade.quantity
        comm  = self._commission * 2 * trade.quantity
        pnl   = gross - comm

        trade.exit_price   = exit_price
        trade.close_reason = reason
        trade.pnl          = pnl
        trade.is_closed    = True

        emoji = "✓" if pnl > 0 else "✗"
        logger.info(
            f"[Paper] CLOSE {emoji} {reason.value} | "
            f"E={trade.entry_price:.2f} X={exit_price:.2f} PNL=${pnl:.2f}"
        )
        return trade

    def force_close(self, trade: Trade, price: float) -> Trade:
        return self._close_trade(trade, price, CloseReason.EOD)
