"""Abstract broker interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.types import SetupContext, Trade


class BrokerBase(ABC):

    @abstractmethod
    def submit_entry(self, ctx: SetupContext, quantity: int) -> Optional[Trade]:
        """Submit a market entry for the setup. Returns Trade on fill, None on rejection."""

    @abstractmethod
    def update(self, trade: Trade, high: float, low: float, close: float) -> Optional[Trade]:
        """
        Feed the latest bar's H/L/C to the broker for SL/TP hit detection.
        Returns the trade with exit fields populated if closed, else None.
        """
