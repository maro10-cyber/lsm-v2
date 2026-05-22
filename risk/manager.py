"""
Risk Manager — position sizing, daily kill switch, progressive sizing.

Progressive sizing (anti-martingale):
  Start at mid (1.0%).  Win → step up; Loss → step down.
  Levels: [0.5%, 1.0%, 1.5%]  (configurable)

Daily kill switch:
  If realized P&L for the day crosses -max_daily_loss_pct, no new entries.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Optional

from core.types import Direction, SetupContext, Trade

logger = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, config: dict) -> None:
        risk = config["risk"]
        sym  = config["symbol"]

        self._tick_size     = sym["tick_size"]
        self._point_value   = sym["point_value"]

        self._account_bal   = risk["account_balance"]
        self._max_daily_pct = risk["max_daily_loss_pct"] / 100.0
        self._rr            = risk.get("reward_risk_ratio", 2.0)
        self._commission    = config.get("execution", {}).get("commission_per_side", 0.50)
        self._max_open      = risk.get("max_open_trades", 1)

        # Progressive sizing
        ps = risk.get("progressive_sizing", {})
        if ps.get("enabled", False):
            self._risk_levels = [p / 100.0 for p in ps.get("levels_pct", [0.5, 1.0, 1.5])]
            self._level_idx   = ps.get("start_level", 1)
        else:
            base = risk.get("risk_per_trade_pct", 1.0) / 100.0
            self._risk_levels = [base]
            self._level_idx   = 0

        # Daily tracking
        self._daily_pnl:  float = 0.0
        self._today:      Optional[date] = None
        self._open_trades: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def can_trade(self, ts: datetime) -> bool:
        """True if a new entry is allowed right now."""
        self._maybe_reset_day(ts)
        if self._open_trades >= self._max_open:
            return False
        if self._daily_pnl <= -(self._account_bal * self._max_daily_pct):
            logger.warning(
                f"[Risk] Daily loss limit hit ({self._daily_pnl:.2f}). No new entries."
            )
            return False
        return True

    def size_trade(self, ctx: SetupContext) -> int:
        """
        Return contract quantity for the setup.
        Uses the entry/SL from SetupContext.
        Returns 0 if sizing is impossible.
        """
        risk_pct  = self._risk_levels[self._level_idx]
        risk_amt  = self._account_bal * risk_pct

        sl_dist_pts = abs(ctx.entry_price - ctx.sl_price)
        if sl_dist_pts < self._tick_size:
            logger.warning(f"[Risk] SL distance too small ({sl_dist_pts:.2f}), skip")
            return 0

        sl_dist_usd = sl_dist_pts * self._point_value
        qty = math.floor(risk_amt / sl_dist_usd)
        qty = max(1, qty)
        logger.info(
            f"[Risk] Size={qty} | risk={risk_pct*100:.1f}% "
            f"SL_dist={sl_dist_pts:.2f}pts | ${sl_dist_usd*qty:.0f} at risk"
        )
        return qty

    def on_trade_opened(self) -> None:
        self._open_trades = min(self._open_trades + 1, self._max_open)

    def on_trade_closed(self, pnl: float) -> None:
        self._open_trades = max(0, self._open_trades - 1)
        self._daily_pnl  += pnl

        if pnl > 0:
            self._level_idx = min(len(self._risk_levels) - 1, self._level_idx + 1)
            logger.debug(
                f"[Risk] Win → risk level → {self._risk_levels[self._level_idx]*100:.1f}%"
            )
        else:
            self._level_idx = max(0, self._level_idx - 1)
            logger.debug(
                f"[Risk] Loss → risk level → {self._risk_levels[self._level_idx]*100:.1f}%"
            )

    def compute_pnl(self, trade: Trade) -> float:
        """Gross P&L minus round-trip commission."""
        direction_mult = 1 if trade.direction == Direction.BULLISH else -1
        pts  = (trade.exit_price - trade.entry_price) * direction_mult
        gross = pts * self._point_value * trade.quantity
        comm  = self._commission * 2 * trade.quantity
        return gross - comm

    # ── Internal ──────────────────────────────────────────────────────────────

    def _maybe_reset_day(self, ts: datetime) -> None:
        today = ts.date()
        if today != self._today:
            self._today    = today
            self._daily_pnl = 0.0
            logger.debug(f"[Risk] New trading day {today}, daily P&L reset")

    @property
    def current_risk_pct(self) -> float:
        return self._risk_levels[self._level_idx]

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def account_balance(self) -> float:
        return self._account_bal

    def update_balance(self, pnl: float) -> None:
        self._account_bal += pnl
