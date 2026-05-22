"""
Core type definitions — all shared dataclasses, enums, and constants.
Every engine imports from here. Nothing in this file imports from strategy/.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ── Enumerations ──────────────────────────────────────────────────────────────

class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SessionType(str, Enum):
    ASIA         = "asia"
    LONDON       = "london"
    NEW_YORK     = "new_york"
    PREVIOUS_DAY = "previous_day"


class SetupState(str, Enum):
    WAITING_FOR_LIQUIDITY = "waiting_for_liquidity"
    SWEEP_DETECTED        = "sweep_detected"
    WAITING_FOR_MSS       = "waiting_for_mss"
    WAITING_FOR_FVG       = "waiting_for_fvg"
    WAITING_FOR_RETRACE   = "waiting_for_retrace"
    ENTER_POSITION        = "enter_position"
    MANAGE_TRADE          = "manage_trade"
    EXIT                  = "exit"


class CloseReason(str, Enum):
    STOP_LOSS   = "stop_loss"
    TAKE_PROFIT = "take_profit"
    MANUAL      = "manual"
    SESSION_END = "session_end"
    KILL_SWITCH = "kill_switch"
    EOD         = "eod"


# ── Market Data ───────────────────────────────────────────────────────────────

@dataclass
class Candle:
    timestamp: datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float = 0.0

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> float:
        return self.high - self.low


# ── Liquidity ─────────────────────────────────────────────────────────────────

@dataclass
class LiquidityZone:
    zone_type:  str
    price:      float
    strength:   int = 1
    created_at: datetime = field(default_factory=datetime.utcnow)
    swept:      bool = False
    swept_at:   Optional[datetime] = None


@dataclass
class LiquiditySweep:
    zone:        LiquidityZone
    direction:   Direction
    candle:      Candle
    penetration: float
    rejected:    bool = False


# ── Market Structure ──────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    price:     float
    direction: Direction
    timestamp: datetime
    bar_index: int
    broken:    bool = False
    broken_at: Optional[datetime] = None


@dataclass
class MarketStructureShift:
    direction:    Direction
    sweep:        LiquiditySweep
    broken_swing: SwingPoint
    candle:       Candle
    mss_price:    float


# ── Fair Value Gap ────────────────────────────────────────────────────────────

@dataclass
class FairValueGap:
    direction:  Direction
    top:        float
    bottom:     float
    midpoint:   float
    formed_at:  datetime
    bar_index:  int
    candle1:    Candle = field(repr=False)
    candle2:    Candle = field(repr=False)
    candle3:    Candle = field(repr=False)
    mitigated:  bool = False
    invalidated: bool = False

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


# ── HTF Bias ──────────────────────────────────────────────────────────────────

@dataclass
class HTFBias:
    direction: Optional[Direction]   # None = NEUTRAL
    score:     int                   # −4 to +4
    vwap:      Optional[float]
    candle:    Candle                # the 15m candle that produced this bias


# ── Sessions ──────────────────────────────────────────────────────────────────

@dataclass
class SessionRange:
    session:     SessionType
    date:        str
    high:        float
    low:         float
    high_time:   datetime
    low_time:    datetime
    open_price:  float = 0.0
    close_price: float = 0.0
    is_complete: bool = False

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2


# ── Setup Context ─────────────────────────────────────────────────────────────

@dataclass
class SetupContext:
    """Travels through the 8-state machine. Holds all components of one setup."""
    sweep:       Optional[LiquiditySweep]       = None
    mss:         Optional[MarketStructureShift]  = None
    fvg:         Optional[FairValueGap]          = None
    bias:        Optional[HTFBias]               = None
    state:       SetupState                      = SetupState.WAITING_FOR_LIQUIDITY

    # Bar indices for timeout tracking
    sweep_bar:   Optional[int] = None
    mss_bar:     Optional[int] = None
    fvg_bar:     Optional[int] = None
    retrace_bar: Optional[int] = None

    # Entry parameters (populated in WAITING_FOR_RETRACE → ENTER_POSITION)
    entry_price: Optional[float] = None
    sl_price:    Optional[float] = None
    tp_price:    Optional[float] = None


# ── Trade ─────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    direction:   Direction
    entry_price: float
    sl_price:    float
    tp_price:    float
    quantity:    int
    entry_time:  datetime
    setup:       Optional[SetupContext] = None
    symbol:      str = "MNQ"
    trade_id:    str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    # Lifecycle
    exit_price:   Optional[float]       = None
    exit_time:    Optional[datetime]    = None
    pnl:          float                 = 0.0
    close_reason: Optional[CloseReason] = None
    is_closed:    bool                  = False

    # Break-even tracking
    be_moved:  bool           = False
    last_high: Optional[float] = None
    last_low:  Optional[float] = None

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.sl_price)

    @property
    def is_open(self) -> bool:
        return not self.is_closed

    def to_dict(self) -> dict:
        return {
            "trade_id":    self.trade_id,
            "symbol":      self.symbol,
            "direction":   self.direction.value,
            "entry_price": self.entry_price,
            "sl_price":    self.sl_price,
            "tp_price":    self.tp_price,
            "exit_price":  self.exit_price,
            "quantity":    self.quantity,
            "pnl":         self.pnl,
            "close_reason": self.close_reason.value if self.close_reason else None,
            "entry_time":  self.entry_time.isoformat() if self.entry_time else None,
            "exit_time":   self.exit_time.isoformat()  if self.exit_time  else None,
            "be_moved":    self.be_moved,
        }
