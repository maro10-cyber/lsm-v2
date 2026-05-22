"""
Performance metrics calculator.

Computes from a list of closed Trade objects:
  - Win rate, Profit factor, Expectancy
  - Sharpe ratio (annualised, 252 trading days)
  - Max drawdown (peak-to-trough on equity curve)
  - Average R-multiple
  - Equity curve (list of running balance)
"""

from __future__ import annotations

import math
from typing import List, Optional

from core.types import Trade


def compute_metrics(trades: List[Trade], initial_balance: float) -> dict:
    if not trades:
        return _empty_metrics(initial_balance)

    pnls   = [t.pnl for t in trades if t.is_closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    n       = len(pnls)
    n_wins  = len(wins)
    n_loss  = len(losses)
    wr      = n_wins / n if n > 0 else 0.0

    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")

    avg_win  = gross_w / n_wins  if n_wins  else 0.0
    avg_loss = gross_l / n_loss  if n_loss  else 0.0
    expectancy = (wr * avg_win) - ((1 - wr) * avg_loss)

    # Equity curve
    equity = [initial_balance]
    for p in pnls:
        equity.append(equity[-1] + p)

    # Max drawdown
    peak = initial_balance
    max_dd = 0.0
    max_dd_pct = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = peak - val
        dd_pct = dd / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd     = dd
            max_dd_pct = dd_pct

    # Sharpe (daily returns, annualised)
    sharpe = _sharpe(pnls, initial_balance)

    # Average R multiple
    r_multiples = _r_multiples(trades)
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

    total_pnl = sum(pnls)
    final_bal = initial_balance + total_pnl
    ret_pct   = (total_pnl / initial_balance) * 100

    return {
        "trades":         n,
        "wins":           n_wins,
        "losses":         n_loss,
        "win_rate_pct":   round(wr * 100, 2),
        "profit_factor":  round(pf, 3),
        "expectancy":     round(expectancy, 2),
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "avg_r":          round(avg_r, 3),
        "total_pnl":      round(total_pnl, 2),
        "final_balance":  round(final_bal, 2),
        "return_pct":     round(ret_pct, 2),
        "max_drawdown":   round(max_dd, 2),
        "max_dd_pct":     round(max_dd_pct * 100, 2),
        "sharpe":         round(sharpe, 3),
        "equity_curve":   equity,
    }


def _sharpe(pnls: List[float], initial_balance: float, periods_per_year: int = 252) -> float:
    if len(pnls) < 2:
        return 0.0
    # Use percent returns per trade (rough)
    rets = [p / initial_balance for p in pnls]
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std  = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def _r_multiples(trades: List[Trade]) -> List[float]:
    rs = []
    for t in trades:
        if not t.is_closed or t.entry_price is None or t.sl_price is None:
            continue
        risk = abs(t.entry_price - t.sl_price)
        if risk == 0:
            continue
        direction_mult = 1 if t.direction.value == "bullish" else -1
        r = (t.exit_price - t.entry_price) * direction_mult / risk
        rs.append(r)
    return rs


def _empty_metrics(initial_balance: float) -> dict:
    return {
        "trades": 0, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "profit_factor": 0.0,
        "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "avg_r": 0.0, "total_pnl": 0.0,
        "final_balance": initial_balance, "return_pct": 0.0,
        "max_drawdown": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0,
        "equity_curve": [initial_balance],
    }


def print_report(metrics: dict) -> None:
    print("\n" + "=" * 50)
    print("  BACKTEST RESULTS")
    print("=" * 50)
    print(f"  Trades       : {metrics['trades']} ({metrics['wins']}W / {metrics['losses']}L)")
    print(f"  Win Rate     : {metrics['win_rate_pct']:.2f}%")
    print(f"  Profit Factor: {metrics['profit_factor']:.3f}")
    print(f"  Expectancy   : ${metrics['expectancy']:.2f} / trade")
    print(f"  Avg Win      : ${metrics['avg_win']:.2f}")
    print(f"  Avg Loss     : ${metrics['avg_loss']:.2f}")
    print(f"  Avg R        : {metrics['avg_r']:.3f}R")
    print(f"  Total P&L    : ${metrics['total_pnl']:.2f}")
    print(f"  Return       : {metrics['return_pct']:.2f}%")
    print(f"  Final Bal    : ${metrics['final_balance']:.2f}")
    print(f"  Max Drawdown : ${metrics['max_drawdown']:.2f} ({metrics['max_dd_pct']:.2f}%)")
    print(f"  Sharpe Ratio : {metrics['sharpe']:.3f}")
    print("=" * 50 + "\n")
