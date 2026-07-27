"""Strategy interface and a simple SMA-crossover example.

To add your own strategy, subclass Strategy and implement generate_signals().
"""

from abc import ABC, abstractmethod
from enum import Enum

import pandas as pd


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Strategy(ABC):
    @abstractmethod
    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, Signal]:
        """Map each symbol to a Signal, given its OHLCV history."""


class SMACrossover(Strategy):
    """Buy when the fast SMA crosses above the slow SMA; sell on the reverse cross.

    Deliberately simple — a starting point, not an edge.
    """

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow

    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, Signal]:
        signals: dict[str, Signal] = {}
        for sym, df in prices.items():
            close = df["Close"]
            if len(close) < self.slow + 1:
                signals[sym] = Signal.HOLD
                continue
            fast = close.rolling(self.fast).mean()
            slow = close.rolling(self.slow).mean()
            above_now = fast.iloc[-1] > slow.iloc[-1]
            above_prev = fast.iloc[-2] > slow.iloc[-2]
            if above_now and not above_prev:
                signals[sym] = Signal.BUY
            elif not above_now and above_prev:
                signals[sym] = Signal.SELL
            else:
                signals[sym] = Signal.HOLD
        return signals
