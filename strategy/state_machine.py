"""
Deterministic 8-state setup machine.

States:
  WAITING_FOR_LIQUIDITY  → sweep detected
  SWEEP_DETECTED         → MSS confirmed (optional) OR skip to FVG
  WAITING_FOR_MSS        → MSS confirmed
  WAITING_FOR_FVG        → FVG formed after MSS/sweep
  WAITING_FOR_RETRACE    → price returns to FVG
  ENTER_POSITION         → entry order submitted
  MANAGE_TRADE           → trade open, manage SL/TP
  EXIT                   → trade closed (terminal)

Timeouts expire setups so stale contexts never trigger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.types import (
    Candle, Direction, FairValueGap, LiquiditySweep,
    MarketStructureShift, SetupContext, SetupState,
)
from strategy.fvg import FVGEngine
from strategy.liquidity import LiquidityEngine
from strategy.structure import StructureEngine

logger = logging.getLogger(__name__)


class StateMachine:
    """
    One instance manages ONE pending setup at a time.
    When a setup reaches EXIT or expires, it resets and watches for the next sweep.

    Caller responsibilities:
      - Call tick(candle) every 1m bar
      - Poll pop_ready_entry() to get entries to execute
      - Call on_trade_result() when broker confirms fill/close
    """

    def __init__(
        self,
        config: dict,
        liquidity: LiquidityEngine,
        structure: StructureEngine,
        fvg: FVGEngine,
    ) -> None:
        sm_cfg = config["state_machine"]
        self._max_sweep_age    = sm_cfg["max_sweep_age_bars"]
        self._max_mss_age      = sm_cfg["max_mss_age_bars"]
        self._max_fvg_age      = sm_cfg["max_fvg_age_bars"]
        self._max_retrace_age  = sm_cfg["max_retrace_age_bars"]

        flt = config.get("filters", {})
        self._require_mss         = flt.get("require_mss", True)
        self._require_htf_bias    = flt.get("require_htf_bias", True)
        self._require_rejection   = flt.get("require_rejected_sweep", True)
        self._cooldown_bars       = flt.get("cooldown_after_trade_bars", 0)

        risk = config["risk"]
        sym  = config["symbol"]
        self._tick_size   = sym["tick_size"]
        self._sl_buffer   = risk["sl_buffer_ticks"] * self._tick_size
        self._rr          = risk.get("reward_risk_ratio", 2.0)

        self._liq = liquidity
        self._str = structure
        self._fvg = fvg

        self._ctx: Optional[SetupContext] = None
        self._bar_index: int = 0
        self._ready_entries: List[SetupContext] = []
        self._last_trade_bar: int = -9999  # bar index of last closed trade

    # ── Main tick ─────────────────────────────────────────────────────────────

    def tick(
        self,
        candle: Candle,
        htf_bias_direction: Optional[Direction],
        new_sweeps: List[LiquiditySweep],
    ) -> None:
        self._bar_index += 1

        # Try to adopt the best new sweep if we're idle and cooldown elapsed
        cooldown_ok = (self._bar_index - self._last_trade_bar) >= self._cooldown_bars
        if cooldown_ok and (self._ctx is None or self._ctx.state == SetupState.WAITING_FOR_LIQUIDITY):
            self._try_adopt_sweep(new_sweeps, htf_bias_direction)

        if self._ctx is None:
            return

        state = self._ctx.state

        if state == SetupState.SWEEP_DETECTED:
            self._handle_sweep_detected(candle, htf_bias_direction)
        elif state == SetupState.WAITING_FOR_MSS:
            self._handle_waiting_mss(candle)
        elif state == SetupState.WAITING_FOR_FVG:
            self._handle_waiting_fvg(candle)
        elif state == SetupState.WAITING_FOR_RETRACE:
            self._handle_waiting_retrace(candle)
        # ENTER_POSITION / MANAGE_TRADE / EXIT handled externally

    # ── State handlers ────────────────────────────────────────────────────────

    def _try_adopt_sweep(
        self,
        new_sweeps: List[LiquiditySweep],
        htf_bias: Optional[Direction],
    ) -> None:
        for sweep in new_sweeps:
            if self._require_rejection and not sweep.rejected:
                logger.debug(f"Sweep skipped — no wick rejection")
                continue

            if self._require_htf_bias and htf_bias is not None:
                if sweep.direction != htf_bias:
                    logger.debug(
                        f"Sweep {sweep.direction.value} skipped — conflicts HTF bias {htf_bias}"
                    )
                    continue

            self._ctx = SetupContext(
                sweep=sweep,
                state=SetupState.SWEEP_DETECTED,
                sweep_bar=self._bar_index,
            )
            logger.info(
                f"[SM] SWEEP_DETECTED {sweep.direction.value.upper()} @ "
                f"{sweep.zone.price} | {sweep.candle.timestamp}"
            )
            return  # take first qualifying sweep

    def _handle_sweep_detected(
        self,
        candle: Candle,
        htf_bias: Optional[Direction],
    ) -> None:
        ctx = self._ctx
        age = self._bar_index - ctx.sweep_bar

        if age > self._max_sweep_age:
            logger.debug(f"[SM] Sweep expired (age={age})")
            self._reset()
            return

        if self._require_mss:
            # Transition to WAITING_FOR_MSS
            ctx.state = SetupState.WAITING_FOR_MSS
            ctx.mss_bar = self._bar_index
            self._handle_waiting_mss(candle)
        else:
            # Skip MSS — look for FVG directly
            ctx.state = SetupState.WAITING_FOR_FVG
            ctx.fvg_bar = self._bar_index
            self._handle_waiting_fvg(candle)

    def _handle_waiting_mss(self, candle: Candle) -> None:
        ctx = self._ctx
        age = self._bar_index - (ctx.mss_bar or ctx.sweep_bar)

        if age > self._max_mss_age:
            logger.debug(f"[SM] MSS wait expired")
            self._reset()
            return

        mss = self._str.check_mss(ctx.sweep, candle, self._max_mss_age)
        if mss is None:
            return

        ctx.mss = mss
        ctx.state = SetupState.WAITING_FOR_FVG
        ctx.fvg_bar = self._bar_index
        logger.info(
            f"[SM] MSS {mss.direction.value.upper()} confirmed @ {mss.mss_price:.2f} | {candle.timestamp}"
        )
        self._handle_waiting_fvg(candle)

    def _handle_waiting_fvg(self, candle: Candle) -> None:
        ctx = self._ctx
        age = self._bar_index - (ctx.fvg_bar or ctx.sweep_bar)

        if age > self._max_fvg_age:
            logger.debug(f"[SM] FVG wait expired")
            self._reset()
            return

        ref_ts = ctx.mss.candle.timestamp if ctx.mss else ctx.sweep.candle.timestamp
        fvg = self._fvg.find_fvg_above_sweep(ref_ts, ctx.sweep.direction, self._max_fvg_age)
        if fvg is None:
            return

        ctx.fvg = fvg
        ctx.state = SetupState.WAITING_FOR_RETRACE
        ctx.retrace_bar = self._bar_index
        logger.info(
            f"[SM] FVG {fvg.direction.value.upper()} [{fvg.bottom:.2f}–{fvg.top:.2f}] | {candle.timestamp}"
        )
        self._handle_waiting_retrace(candle)

    def _handle_waiting_retrace(self, candle: Candle) -> None:
        ctx = self._ctx
        age = self._bar_index - (ctx.retrace_bar or ctx.sweep_bar)

        if age > self._max_retrace_age:
            logger.debug(f"[SM] Retrace wait expired")
            self._reset()
            return

        fvg = ctx.fvg
        if fvg.invalidated:
            logger.debug(f"[SM] FVG invalidated during retrace wait")
            self._reset()
            return

        direction = ctx.sweep.direction
        triggered = False

        sweep_candle = ctx.sweep.candle

        if direction == Direction.BULLISH:
            # Price sweeps into bullish FVG (wick dips in) and closes back above bottom
            swept_in = candle.low <= fvg.top
            rejected = candle.close >= fvg.bottom
            if swept_in and rejected:
                triggered = True
                # SL below the sweep candle's low — the invalidation level
                sl_price = sweep_candle.low - self._sl_buffer

        elif direction == Direction.BEARISH:
            # Price sweeps into bearish FVG (wick rises into gap) and closes back below top
            swept_in = candle.high >= fvg.bottom
            rejected = candle.close <= fvg.top
            if swept_in and rejected:
                triggered = True
                # SL above the sweep candle's high — the invalidation level
                sl_price = sweep_candle.high + self._sl_buffer

        if triggered:
            entry_price = candle.close
            risk_pts    = abs(entry_price - sl_price)

            if direction == Direction.BULLISH:
                tp_price = entry_price + risk_pts * self._rr
            else:
                tp_price = entry_price - risk_pts * self._rr

            ctx.entry_price = entry_price
            ctx.sl_price    = sl_price
            ctx.tp_price    = tp_price
            ctx.state       = SetupState.ENTER_POSITION
            self._ready_entries.append(ctx)
            logger.info(
                f"[SM] ENTRY READY {direction.value.upper()} | "
                f"E={entry_price:.2f} SL={sl_price:.2f} TP={tp_price:.2f} | {candle.timestamp}"
            )
            # Reset so machine can watch for next sweep
            self._ctx = None

    # ── External callbacks ────────────────────────────────────────────────────

    def on_trade_opened(self, ctx: SetupContext) -> None:
        ctx.state = SetupState.MANAGE_TRADE

    def on_trade_closed(self, ctx: SetupContext) -> None:
        ctx.state = SetupState.EXIT
        self._last_trade_bar = self._bar_index

    # ── Utilities ─────────────────────────────────────────────────────────────

    def pop_ready_entry(self) -> Optional[SetupContext]:
        if self._ready_entries:
            return self._ready_entries.pop(0)
        return None

    def _reset(self) -> None:
        self._ctx = None

    @property
    def current_state(self) -> Optional[SetupState]:
        return self._ctx.state if self._ctx else SetupState.WAITING_FOR_LIQUIDITY
